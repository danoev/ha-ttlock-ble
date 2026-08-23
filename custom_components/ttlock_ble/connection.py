"""
Persistent BLE connection wrapper for ttlock_ble.

Each `TtlockBleConnection` owns a long-lived `TTLockClient` for a single
`VirtualKey`, runs a background reconnect loop, and serializes state
queries and lock commands through a single `asyncio.Lock`. Push events
arriving on that connection are dispatched live via HA's dispatcher
under the signal `ttlock_ble_event_<mac>`.

The reconnect loop waits on an `asyncio.Event` that the SDK's
`disconnected_callback` toggles, so the watchdog wakes up the instant
the BLE link drops instead of poll-sleeping.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from homeassistant.components.bluetooth import (
    MONOTONIC_TIME,
    BluetoothCallbackMatcher,
    BluetoothReachabilityIntent,
    BluetoothScanningMode,
    async_address_reachability_diagnostics,
    async_ble_device_from_address,
    async_last_service_info,
    async_process_advertisements,
    async_scanner_devices_by_address,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ttlock_ble import KeyboardPwdType, LockState, TTLockClient, TTLockError

from .client import ControlOutcomeUnknownError, TtlockBleClient
from .const import DEFAULT_RECONNECT_INTERVAL_SECONDS, DOMAIN, LOGGER

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bleak import BleakClient
    from bleak.backends.device import BLEDevice
    from home_assistant_bluetooth import BluetoothServiceInfoBleak
    from homeassistant.core import HomeAssistant

    from ttlock_ble import LockEvent, LogEntry, VirtualKey


RECONNECT_INITIAL_BACKOFF = 1.0
RECONNECT_MAX_BACKOFF = 300.0
EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS = 25

# The lock answers the operation log one record per BLE frame, each with
# its own timeout, and the SDK holds its command lock for the whole
# pagination. Unbounded, a lock with a long unsynced history makes the
# first fetch of a session sit in front of a user pressing Unlock for
# tens of seconds. Anything older than this batch is picked up by the
# next poll.
MAX_LOG_ENTRIES_PER_FETCH = 25


@dataclass(frozen=True, slots=True)
class _ConnectionCandidate:
    """One HA-owned connectable representation of the lock."""

    device: BLEDevice
    resolution: str
    source: str
    rssi: int | None
    advertisement_time: float | None


def event_signal(mac: str) -> str:
    """Dispatcher signal that carries `LockEvent`s for `mac`."""
    return f"{DOMAIN}_event_{mac.lower()}"


def log_signal(mac: str) -> str:
    """Dispatcher signal that carries `LogEntry` records for `mac`."""
    return f"{DOMAIN}_log_{mac.lower()}"


def connection_signal(mac: str) -> str:
    """Dispatcher signal that carries BLE up/down transitions for `mac`."""
    return f"{DOMAIN}_connection_{mac.lower()}"


class TtlockBleConnection:
    """Maintain a long-lived BLE session with one TTLock lock."""

    def __init__(
        self,
        hass: HomeAssistant,
        key: VirtualKey,
        reconnect_cooldown_seconds: float = DEFAULT_RECONNECT_INTERVAL_SECONDS,
    ) -> None:
        """
        Bind to the HA instance and the credentials for a single lock.

        `reconnect_cooldown_seconds` paces the maintain loop after a BLE
        drop; `0` means reconnect immediately, keeping the session
        permanently open at the cost of the lock's battery.
        """
        self._hass = hass
        self._key = key
        self._reconnect_cooldown_seconds = reconnect_cooldown_seconds
        self._client: TTLockClient | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._closing_event = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._seen_records: set[int] = set()
        self._log_seeded = False
        self._broadcast_connected = False
        self._reachability_diagnostic: str | None = None
        self._last_connection_rssi: int | None = None

    @property
    def key(self) -> VirtualKey:
        """Return the `VirtualKey` this connection wraps."""
        return self._key

    @property
    def is_connected(self) -> bool:
        """True iff the underlying `TTLockClient` is currently connected."""
        return self._client is not None and self._client.is_connected

    @property
    def last_connection_rssi(self) -> int | None:
        """Return the RSSI of the most recently selected HA connection route."""
        return self._last_connection_rssi

    async def async_start(self) -> None:
        """Begin maintaining the BLE connection in the background."""
        if self._task is not None:
            return
        self._closing = False
        self._closing_event.clear()
        self._task = self._hass.async_create_background_task(
            self._async_maintain(),
            name=f"ttlock_ble.connection.{self._key.lockMac}",
        )

    async def async_stop(self) -> None:
        """Cancel the background loop and release the BLE connection."""
        self._closing = True
        self._closing_event.set()
        self._disconnected.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        async with self._lock:
            await self._async_disconnect_locked()

    async def async_query_state(
        self,
        *,
        active_scan: bool = False,
    ) -> tuple[LockState | None, int | None] | None:
        """
        Return `(lock_state, battery)` through the live connection.

        Returns `None` when the lock is out of range or the query failed.
        ``active_scan`` opts an authoritative bootstrap or explicit refresh
        into one bounded exact-address HA-managed Active window when no cached
        connectable route exists. Routine known-state polls leave it false.
        Every caller is already rate-limited, so the reconnect cooldown the
        maintain loop keeps is deliberately not consulted here: it paces the
        background loop, not reads.
        """
        async with self._lock:
            client = await self._async_ensure_connected_locked(
                active_scan=active_scan,
                reason="authoritative state bootstrap"
                if active_scan
                else "state query",
            )
            if client is None:
                return None
            try:
                return await client.query_state()
            except TTLockError as exc:
                LOGGER.warning(
                    "query_state failed for %s: %s",
                    self._key.lockMac,
                    exc,
                )
                await self._async_disconnect_locked()
                return None

    async def async_lock(self) -> None:
        """Send a LOCK command on the live connection (raises on failure)."""
        await self._async_run_command("lock")

    async def async_unlock(self) -> None:
        """Send an UNLOCK command on the live connection (raises on failure)."""
        await self._async_run_command("unlock")

    async def async_get_auto_lock_time(self) -> int:
        """Read the native auto-lock delay from the lock."""
        return await self._async_run_management_command(
            "read auto-lock delay",
            lambda client: client.get_auto_lock_time(),
            active_scan=True,
        )

    async def async_set_auto_lock_time(self, seconds: int) -> None:
        """Set the native auto-lock delay; zero disables it."""
        await self._async_run_management_command(
            "set auto-lock delay",
            lambda client: client.set_auto_lock_time(seconds),
            active_scan=True,
        )

    async def async_add_passcode(
        self,
        code: str,
        *,
        pwd_type: KeyboardPwdType,
        start_date: str,
        end_date: str,
    ) -> None:
        """Add a permanent or time-windowed keypad passcode."""
        await self._async_run_management_command(
            "add passcode",
            lambda client: client.add_passcode(
                code,
                pwd_type=pwd_type,
                start_date=start_date,
                end_date=end_date,
            ),
            active_scan=True,
        )

    async def async_delete_passcode(
        self,
        code: str,
        *,
        pwd_type: KeyboardPwdType,
    ) -> None:
        """Delete one keypad passcode without retaining it locally."""
        await self._async_run_management_command(
            "delete passcode",
            lambda client: client.delete_passcode(code, pwd_type=pwd_type),
            active_scan=True,
        )

    async def async_clear_passcodes(self) -> None:
        """Remove every keypad passcode from the lock."""
        await self._async_run_management_command(
            "clear passcodes",
            lambda client: client.clear_passcodes(),
            active_scan=False,
        )

    async def async_get_operation_log(self) -> list[LogEntry]:
        """
        Fetch operation records from the lock and dispatch the new ones.

        The first fetch that actually reaches the lock only seeds
        `_seen_records` and returns nothing. The lock hands back
        everything unsynced since its last cursor sync, and that set is
        history — replaying it through the event entity would fire
        automations for unlocks that happened days ago. `_seen_records`
        lives in memory, so the seeding pass runs once per HA start,
        which is exactly when the backlog would otherwise arrive.

        Seeding is tied to a successful fetch, not to an attempt: a lock
        out of range at startup gets its seeding pass whenever it first
        answers.
        """
        async with self._lock:
            client = await self._async_ensure_connected_locked(reason="operation log")
            if client is None:
                LOGGER.warning("get_operation_log: no client for %s", self._key.lockMac)
                return []
            try:
                entries = await client.get_operation_log(
                    max_entries=MAX_LOG_ENTRIES_PER_FETCH,
                )
            except Exception as exc:  # noqa: BLE001
                # `TTLockError` alone is not enough: the SDK's log path
                # reaches `aes_decrypt` unwrapped and raises `ValueError`
                # on a garbled frame, and `bleak` raises `BleakError` when
                # the link drops mid-fetch. Letting those out would fail
                # the whole poll and discard the state read that already
                # succeeded, leaving the lock entity reporting a stale
                # value instead of merely losing its history.
                LOGGER.warning(
                    "get_operation_log failed for %s: %s",
                    self._key.lockMac,
                    exc,
                )
                return []
        LOGGER.debug(
            "get_operation_log for %s: %d entries, seen=%d",
            self._key.lockMac,
            len(entries),
            len(self._seen_records),
        )
        new_entries: list[LogEntry] = []
        for entry in entries:
            if entry.record_number not in self._seen_records:
                self._seen_records.add(entry.record_number)
                new_entries.append(entry)
        if not self._log_seeded:
            LOGGER.debug(
                "Lock %s: seeded %d existing log records from this page, "
                "none dispatched (page_full=%s)",
                self._key.lockMac,
                len(new_entries),
                len(entries) >= MAX_LOG_ENTRIES_PER_FETCH,
            )
            # A full bounded page is not proof that history is exhausted.
            # Keep seeding until the first short/empty page so later historical
            # pages cannot be mistaken for live operations.
            self._log_seeded = len(entries) < MAX_LOG_ENTRIES_PER_FETCH
            return []
        if new_entries:
            LOGGER.debug(
                "Lock %s: dispatching %d new log entries",
                self._key.lockMac,
                len(new_entries),
            )
            for entry in new_entries:
                async_dispatcher_send(
                    self._hass,
                    log_signal(self._key.lockMac),
                    entry,
                )
        return new_entries

    async def _async_run_command(self, action: str) -> None:
        """
        Acquire the lock, ensure connected, then call `lock`/`unlock`.

        Everything that is not already a `TTLockError` is converted into
        one so callers only ever see the integration's own exception
        hierarchy. That is not belt-and-suspenders: the SDK's command
        path reaches `bleak.write_gatt_char` unwrapped (`BleakError`
        when the link drops mid-command), raises a bare `RuntimeError`
        when the lock rejects `checkUserTime`, and lets `ValueError` out
        of `aes_decrypt` on a garbled frame. `TTLockError` subclasses
        `RuntimeError`, so it has to be caught first.
        """
        started = monotonic()
        LOGGER.debug(
            "Explicit %s requested for %s",
            action,
            self._key.lockMac,
        )
        async with self._lock:
            client = await self._async_ensure_connected_locked(
                active_scan=True,
                reason=f"explicit {action}",
            )
            if client is None:
                msg = f"Lock {self._key.lockMac} not reachable via Bluetooth"
                if self._reachability_diagnostic is not None:
                    msg = f"{msg}: {self._reachability_diagnostic}"
                raise TTLockError(msg)
            try:
                if action == "lock":
                    await client.lock()
                else:
                    await client.unlock()
                LOGGER.debug(
                    "Explicit %s completed for %s (elapsed=%.1fs)",
                    action,
                    self._key.lockMac,
                    monotonic() - started,
                )
            except ControlOutcomeUnknownError:
                if await self._async_reconcile_control_locked(action, client):
                    LOGGER.info(
                        "Explicit %s reconciled from a fresh connected state for %s "
                        "(elapsed=%.1fs; no command retry)",
                        action,
                        self._key.lockMac,
                        monotonic() - started,
                    )
                    return
                await self._async_disconnect_locked()
                raise
            except TTLockError:
                await self._async_disconnect_locked()
                raise
            except TimeoutError as exc:
                await self._async_disconnect_locked()
                msg = f"Lock {self._key.lockMac} timed out responding to {action}"
                raise TTLockError(msg) from exc
            except Exception as exc:
                await self._async_disconnect_locked()
                msg = f"Lock {self._key.lockMac} failed to {action}: {exc}"
                raise TTLockError(msg) from exc

    async def _async_reconcile_control_locked(
        self,
        action: str,
        client: TTLockClient,
    ) -> bool:
        """Confirm an ambiguous write with one fresh state query, never a resend."""
        if not client.is_connected:
            await self._async_disconnect_locked()
            replacement = await self._async_ensure_connected_locked(
                active_scan=True,
                reason=f"reconcile ambiguous {action}",
            )
            if replacement is None:
                LOGGER.warning(
                    "Could not reconcile ambiguous %s for %s: no fresh BLE connection",
                    action,
                    self._key.lockMac,
                )
                return False
            client = replacement
        try:
            state, _battery = await client.query_state()
        except Exception as exc:  # noqa: BLE001 -- ambiguity must remain explicit
            LOGGER.warning(
                "Could not reconcile ambiguous %s for %s with a fresh state query: %s",
                action,
                self._key.lockMac,
                exc,
            )
            return False
        expected = LockState.LOCKED if action == "lock" else LockState.UNLOCKED
        matched = state == expected
        LOGGER.debug(
            "Ambiguous control reconciliation for %s "
            "(action=%s, fresh_state=%s, matched=%s, RSSI=%s)",
            self._key.lockMac,
            action,
            state.name if state is not None else "unknown",
            matched,
            self._last_connection_rssi
            if self._last_connection_rssi is not None
            else "unknown",
        )
        return matched

    async def _async_run_management_command[T](
        self,
        action: str,
        operation: Callable[[TTLockClient], Awaitable[T]],
        *,
        active_scan: bool,
    ) -> T:
        """Run management without including its secret inputs in errors."""
        started = monotonic()
        LOGGER.debug(
            "Explicit %s requested for %s",
            action,
            self._key.lockMac,
        )
        async with self._lock:
            client = await self._async_ensure_connected_locked(
                active_scan=active_scan,
                reason=f"management {action}",
            )
            if client is None:
                msg = f"Lock {self._key.lockMac} is not reachable via Bluetooth"
                if self._reachability_diagnostic is not None:
                    msg = f"{msg}: {self._reachability_diagnostic}"
                raise TTLockError(msg)
            try:
                result = await operation(client)
            except TTLockError:
                await self._async_disconnect_locked()
                raise
            except TimeoutError as exc:
                await self._async_disconnect_locked()
                msg = f"Lock {self._key.lockMac} timed out during {action}"
                raise TTLockError(msg) from exc
            except Exception as exc:
                await self._async_disconnect_locked()
                msg = f"Lock {self._key.lockMac} failed to {action}"
                raise TTLockError(msg) from exc
            LOGGER.debug(
                "Explicit %s completed for %s (elapsed=%.1fs)",
                action,
                self._key.lockMac,
                monotonic() - started,
            )
            return result

    async def _async_ensure_connected_locked(
        self,
        *,
        active_scan: bool = False,
        reason: str,
    ) -> TTLockClient | None:
        """
        Return a live client, opening a new BLE session if needed.

        Caller must hold `self._lock`. Returns `None` on failure (lock
        not discoverable or BLE connect raised), and once `async_stop`
        has run: a late caller that opened a session then would take the
        lock's single central slot with nobody left to close it, and
        block the connection the reloaded entry is trying to make.
        """
        started = monotonic()
        self._reachability_diagnostic = None
        LOGGER.debug(
            "Connection acquisition started for %s "
            "(reason=%s, existing_client_connected=%s, active_scan=%s)",
            self._key.lockMac,
            reason,
            self.is_connected,
            active_scan,
        )
        if self._closing:
            LOGGER.debug(
                "Connection acquisition skipped for %s (reason=%s, failure=closing)",
                self._key.lockMac,
                reason,
            )
            return None
        if self._client is not None and self._client.is_connected:
            LOGGER.debug(
                "Reusing live BLE connection for %s (reason=%s, elapsed=%.1fs)",
                self._key.lockMac,
                reason,
                monotonic() - started,
            )
            return self._client
        await self._async_disconnect_locked()
        candidate = self._resolve_connection_candidate(
            include_scanner_paths=active_scan,
            reason=reason,
            log_miss=True,
        )
        active_acquisition_attempted = False
        if candidate is None and active_scan:
            active_acquisition_attempted = True
            candidate = await self._async_wait_for_connectable_device_locked(
                reason=reason,
            )
        while candidate is not None and not self._closing_event.is_set():
            client = TtlockBleClient.from_ble_device(
                candidate.device,
                self._key,
                disconnected_callback=self._on_disconnected,
            )
            connect_started = monotonic()
            age = (
                max(0.0, MONOTONIC_TIME() - candidate.advertisement_time)
                if candidate.advertisement_time is not None
                else None
            )
            LOGGER.debug(
                "Opening BLE connection for %s "
                "(reason=%s, resolution=%s, source=%s, RSSI=%s, age=%s, "
                "candidate_elapsed=%.1fs; GATT retries delegated to ttlock-ble)",
                self._key.lockMac,
                reason,
                candidate.resolution,
                candidate.source,
                candidate.rssi if candidate.rssi is not None else "unknown",
                f"{age:.1f}s" if age is not None else "unknown",
                connect_started - started,
            )
            try:
                await client.connect()
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await client.disconnect()
                raise
            except TTLockError as exc:
                with contextlib.suppress(Exception):
                    await client.disconnect()
                LOGGER.debug(
                    "BLE connect failed for %s "
                    "(reason=%s, resolution=%s, source=%s, age=%s, "
                    "connect_elapsed=%.1fs, total_elapsed=%.1fs): %s",
                    self._key.lockMac,
                    reason,
                    candidate.resolution,
                    candidate.source,
                    f"{age:.1f}s" if age is not None else "unknown",
                    monotonic() - connect_started,
                    monotonic() - started,
                    exc,
                )
                if not active_scan or active_acquisition_attempted:
                    candidate = None
                    break
                active_acquisition_attempted = True
                LOGGER.debug(
                    "Cached BLE route failed before command/authentication for %s; "
                    "starting one fresh address-scoped acquisition (reason=%s)",
                    self._key.lockMac,
                    reason,
                )
                candidate = await self._async_wait_for_connectable_device_locked(
                    reason=reason,
                )
                continue
            if self._closing_event.is_set():
                with contextlib.suppress(Exception):
                    await client.disconnect()
                LOGGER.debug(
                    "Discarded late BLE connection for %s (reason=%s, failure=closing)",
                    self._key.lockMac,
                    reason,
                )
                return None
            LOGGER.debug(
                "BLE connection established for %s "
                "(reason=%s, connect_elapsed=%.1fs, total_elapsed=%.1fs)",
                self._key.lockMac,
                reason,
                monotonic() - connect_started,
                monotonic() - started,
            )
            client.add_event_listener(self._on_event)
            self._client = client
            self._last_connection_rssi = candidate.rssi
            self._disconnected.clear()
            self._broadcast_connection_state(connected=True)
            return client
        if not self._closing_event.is_set():
            self._record_reachability_diagnostic(reason, started)
        return None

    def _record_reachability_diagnostic(
        self,
        reason: str,
        started: float,
    ) -> None:
        """Record HA's credential-safe explanation for a final route failure."""
        try:
            diagnostic = async_address_reachability_diagnostics(
                self._hass,
                self._key.lockMac,
                BluetoothReachabilityIntent.CONNECTION,
            )
        except Exception:  # noqa: BLE001 -- diagnostics must not mask failure
            LOGGER.debug(
                "Could not build Bluetooth reachability diagnostics for %s",
                self._key.lockMac,
                exc_info=True,
            )
            return
        self._reachability_diagnostic = diagnostic
        LOGGER.debug(
            "Lock %s is not reachable via Bluetooth (reason=%s, elapsed=%.1fs): %s",
            self._key.lockMac,
            reason,
            monotonic() - started,
            diagnostic,
        )

    def _resolve_connection_candidate(
        self,
        *,
        include_scanner_paths: bool,
        reason: str,
        log_miss: bool = False,
    ) -> _ConnectionCandidate | None:
        """Resolve a route from HA aggregate history or connectable scanners."""
        address = self._key.lockMac
        passive_info = async_last_service_info(
            self._hass,
            address,
            connectable=False,
        )
        device = async_ble_device_from_address(
            self._hass,
            address,
            connectable=True,
        )
        if device is not None:
            service_info = async_last_service_info(
                self._hass,
                address,
                connectable=True,
            )
            candidate = _ConnectionCandidate(
                device=device,
                resolution="aggregate connectable history",
                source=service_info.source if service_info is not None else "unknown",
                rssi=service_info.rssi if service_info is not None else None,
                advertisement_time=service_info.time
                if service_info is not None
                else None,
            )
            LOGGER.debug(
                "Connection candidate resolved for %s "
                "(reason=%s, passive_history=%s, resolution=%s, "
                "source=%s, RSSI=%s)",
                address,
                reason,
                passive_info is not None,
                candidate.resolution,
                candidate.source,
                candidate.rssi if candidate.rssi is not None else "unknown",
            )
            return candidate

        scanner_devices = (
            async_scanner_devices_by_address(
                self._hass,
                address,
                connectable=True,
            )
            if include_scanner_paths
            else []
        )
        if scanner_devices:
            scanner_device = max(
                scanner_devices,
                key=lambda path: path.advertisement.rssi,
            )
            candidate = _ConnectionCandidate(
                device=scanner_device.ble_device,
                resolution="connectable scanner path",
                source=scanner_device.scanner.source,
                rssi=scanner_device.advertisement.rssi,
                advertisement_time=None,
            )
            LOGGER.debug(
                "Connection candidate resolved for %s "
                "(reason=%s, passive_history=%s, "
                "aggregate_connectable_history=False, scanner_paths=%d, "
                "resolution=%s, source=%s, RSSI=%s)",
                address,
                reason,
                passive_info is not None,
                len(scanner_devices),
                candidate.resolution,
                candidate.source,
                candidate.rssi,
            )
            return candidate

        if log_miss:
            LOGGER.debug(
                "No connection candidate for %s "
                "(reason=%s, passive_history=%s, "
                "aggregate_connectable_history=False, scanner_paths=%s)",
                address,
                reason,
                passive_info is not None,
                "0" if include_scanner_paths else "not checked",
            )
        return None

    async def _async_wait_for_connectable_device_locked(
        self,
        *,
        reason: str,
    ) -> _ConnectionCandidate | None:
        """Wait for this address through HA's Auto-mode active-scan scheduler."""
        address = self._key.lockMac
        started = monotonic()
        fresh_after = MONOTONIC_TIME()
        LOGGER.debug(
            "Starting address-scoped active acquisition for %s "
            "(reason=%s, timeout=%ds)",
            address,
            reason,
            EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS,
        )
        resolved: list[_ConnectionCandidate] = []

        def _candidate_available(service_info: BluetoothServiceInfoBleak) -> bool:
            if service_info.time <= fresh_after:
                LOGGER.debug(
                    "Ignoring replayed connectable history for %s during active "
                    "acquisition (reason=%s, age=%.1fs)",
                    address,
                    reason,
                    max(0.0, fresh_after - service_info.time),
                )
                return False
            candidate = _ConnectionCandidate(
                device=service_info.device,
                resolution="fresh active advertisement",
                source=service_info.source,
                rssi=service_info.rssi,
                advertisement_time=service_info.time,
            )
            resolved.append(candidate)
            return True

        advertisement_task = self._hass.async_create_task(
            async_process_advertisements(
                self._hass,
                _candidate_available,
                BluetoothCallbackMatcher(address=address, connectable=True),
                BluetoothScanningMode.ACTIVE,
                EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS,
            ),
            name=f"{DOMAIN}.active_scan.{address}",
        )
        closing_task = self._hass.async_create_task(
            self._closing_event.wait(),
            name=f"{DOMAIN}.active_scan_stop.{address}",
        )
        tasks = (advertisement_task, closing_task)
        try:
            done, _pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._closing or closing_task in done:
                return None
            try:
                await advertisement_task
            except TimeoutError:
                LOGGER.debug(
                    "Address-scoped active acquisition timed out after %ds for %s",
                    EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS,
                    address,
                )
                return None
            except Exception:  # noqa: BLE001 -- retain diagnostic fallback
                LOGGER.debug(
                    "Address-scoped active Bluetooth acquisition failed for %s",
                    address,
                    exc_info=True,
                )
                return None
            if not resolved:
                return None
            candidate = resolved[-1]
            LOGGER.debug(
                "Connection candidate became available for %s "
                "(reason=%s, elapsed=%.1fs, resolution=%s, source=%s, RSSI=%s)",
                address,
                reason,
                monotonic() - started,
                candidate.resolution,
                candidate.source,
                candidate.rssi if candidate.rssi is not None else "unknown",
            )
            return candidate
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _async_disconnect_locked(self) -> None:
        """Tear down the BLE session if up. Caller must hold `self._lock`."""
        if self._client is None:
            return
        client = self._client
        self._client = None
        self._broadcast_connection_state(connected=False)
        client.remove_event_listener(self._on_event)
        with contextlib.suppress(Exception):
            await client.disconnect()

    def _broadcast_connection_state(self, *, connected: bool) -> None:
        """
        Notify subscribers that the BLE link to this lock just changed.

        Repeats are dropped: the down edge is announced the moment bleak
        reports the drop, and the teardown that eventually follows must
        not announce it a second time.
        """
        if connected == self._broadcast_connected:
            return
        self._broadcast_connected = connected
        async_dispatcher_send(
            self._hass,
            connection_signal(self._key.lockMac),
            connected,
        )

    def _on_event(self, event: LockEvent) -> None:
        """Forward a push event onto HA's dispatcher (called by the BLE layer)."""
        async_dispatcher_send(
            self._hass,
            event_signal(self._key.lockMac),
            event,
        )

    def _on_disconnected(self, _client: BleakClient) -> None:
        """
        Wake the maintain loop the moment bleak signals a drop.

        The drop is also announced here rather than from
        `_async_disconnect_locked`: that runs after the reconnect
        cooldown, so the connectivity sensor claimed a live link for
        most of every cooldown cycle.
        """
        self._disconnected.set()
        self._broadcast_connection_state(connected=False)

    async def _async_maintain(self) -> None:
        """
        Background loop that opens one BLE session and cools down on drop.

        After any disconnect, sleeps `_reconnect_cooldown_seconds` before
        reconnecting. No immediate retry by default — locks that drop us
        aggressively (TTLock's idle-sleep) would otherwise produce a
        reconnect storm that drains the lock's battery. A cooldown of `0`
        opts into exactly that storm: the permanent-connection option
        trades battery for an always-open session. Connect failures
        (device not yet advertising) use a separate exponential backoff
        so first-boot scans don't wait the full cooldown.

        The cooldown paces this loop only. State stays fresh meanwhile
        through the lock's advertisements, which cost no session at all.
        """
        backoff = RECONNECT_INITIAL_BACKOFF
        while not self._closing:
            try:
                async with self._lock:
                    client = await self._async_ensure_connected_locked(
                        reason="background maintenance",
                    )
                if client is None:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)
                    continue
                backoff = RECONNECT_INITIAL_BACKOFF
                await self._disconnected.wait()
                await asyncio.sleep(self._reconnect_cooldown_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.exception(
                    "Connection maintenance error for %s",
                    self._key.lockMac,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)
