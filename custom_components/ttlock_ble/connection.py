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
from enum import Enum
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING, cast

from homeassistant.components.bluetooth import (
    MONOTONIC_TIME,
    BluetoothCallbackMatcher,
    BluetoothReachabilityIntent,
    BluetoothScanningMode,
    async_address_reachability_diagnostics,
    async_ble_device_from_address,
    async_clear_advertisement_history,
    async_get_learned_advertising_interval,
    async_last_service_info,
    async_process_advertisements,
    async_scanner_devices_by_address,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ttlock_ble import KeyboardPwdType, LockState, TTLockClient, TTLockError

from .client import ControlOutcomeUnknownError, TtlockBleClient
from .const import DEFAULT_RECONNECT_INTERVAL_SECONDS, DOMAIN, LOGGER
from .log_history import TtlockBleLogHistory

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
SPECULATIVE_CACHED_CONNECT_ATTEMPTS = 1
ROBUST_CONNECT_ATTEMPTS = 3
CACHED_CONNECT_TIMEOUT_SECONDS = 16.0
FRESH_CONNECT_TIMEOUT_SECONDS = 42.0
EXPLICIT_CONTROL_ACQUISITION_TIMEOUT_SECONDS = 60.0
MANAGEMENT_ACQUISITION_TIMEOUT_SECONDS = 60.0
ROUTINE_QUERY_ACQUISITION_TIMEOUT_SECONDS = 16.0
UNKNOWN_BOOTSTRAP_ACQUISITION_TIMEOUT_SECONDS = 48.0
AMBIGUOUS_RECONCILIATION_ACQUISITION_TIMEOUT_SECONDS = 48.0

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


@dataclass(frozen=True, slots=True)
class _CleanupResult:
    """Outcome of fully awaiting one candidate's explicit teardown."""

    elapsed: float
    succeeded: bool


@dataclass(frozen=True, slots=True)
class _ConnectTimeoutResult:
    """Outcome from a connection phase that reached its cancellation deadline."""

    acquisition_elapsed: float
    cleanup: _CleanupResult


class _AcquisitionIntent(Enum):
    """Semantic reason for acquiring a BLE connection."""

    EXPLICIT_CONTROL = "explicit_control"
    ROUTINE_QUERY = "routine_query"
    UNKNOWN_BOOTSTRAP = "unknown_bootstrap"
    MANAGEMENT = "management"
    AMBIGUOUS_RECONCILIATION = "ambiguous_reconciliation"
    OPERATION_LOG = "operation_log"
    BACKGROUND_MAINTENANCE = "background_maintenance"


def _acquisition_budget(intent: _AcquisitionIntent) -> float:
    """Return the validation budget for one semantic acquisition intent."""
    if intent is _AcquisitionIntent.EXPLICIT_CONTROL:
        return EXPLICIT_CONTROL_ACQUISITION_TIMEOUT_SECONDS
    if intent is _AcquisitionIntent.MANAGEMENT:
        return MANAGEMENT_ACQUISITION_TIMEOUT_SECONDS
    if intent is _AcquisitionIntent.UNKNOWN_BOOTSTRAP:
        return UNKNOWN_BOOTSTRAP_ACQUISITION_TIMEOUT_SECONDS
    if intent is _AcquisitionIntent.AMBIGUOUS_RECONCILIATION:
        return AMBIGUOUS_RECONCILIATION_ACQUISITION_TIMEOUT_SECONDS
    return ROUTINE_QUERY_ACQUISITION_TIMEOUT_SECONDS


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
        log_history: TtlockBleLogHistory | None = None,
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
        self._pending_client: TTLockClient | None = None
        self._cleanup_task: asyncio.Task[_CleanupResult] | None = None
        self._cleanup_task_client: TTLockClient | None = None
        self._control_task: (
            asyncio.Task[Exception | asyncio.CancelledError | None] | None
        ) = None
        self._reconciliation_task: asyncio.Task[Exception | None] | None = None
        self._next_client_generation = 0
        self._owned_client_generation: int | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._closing_event = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._log_history = log_history or TtlockBleLogHistory(hass)
        self._broadcast_connected = False
        self._reachability_diagnostic: str | None = None
        self._last_connection_rssi: int | None = None

    @property
    def key(self) -> VirtualKey:
        """Return the `VirtualKey` this connection wraps."""
        return self._key

    @property
    def is_connected(self) -> bool:
        """True iff any client still owned by this object reports connected."""
        owned_client = self._client or self._pending_client
        return owned_client is not None and owned_client.is_connected

    @property
    def last_connection_rssi(self) -> int | None:
        """Return the RSSI of the most recently selected HA connection route."""
        return self._last_connection_rssi

    def _control_is_allowed(self) -> bool:
        """Return whether a new physical control write may still begin."""
        return not self._closing_event.is_set()

    async def async_start(self, *, maintain: bool = True) -> None:
        """Start optional reconnect maintenance for this lock."""
        if not maintain or self._task is not None:
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
            disconnected = await self._async_disconnect_locked(
                reason="config-entry shutdown"
            )
            if not disconnected:
                disconnected = await self._async_retry_pending_cleanup_locked(
                    reason="config-entry shutdown retry"
                )
            if not disconnected:
                msg = (
                    f"Could not confirm BLE disconnect for lock {self._key.lockMac} "
                    "during shutdown"
                )
                raise TTLockError(msg)

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
                intent=(
                    _AcquisitionIntent.UNKNOWN_BOOTSTRAP
                    if active_scan
                    else _AcquisitionIntent.ROUTINE_QUERY
                ),
                reason="authoritative state bootstrap"
                if active_scan
                else "state query",
            )
            if client is None:
                return None
            try:
                return await client.query_state()
            except Exception as exc:  # noqa: BLE001
                # The SDK's transport/decrypt path can also surface unwrapped
                # BleakError, RuntimeError, or ValueError.  Keep the public
                # query contract (state or None) consistent for every caller;
                # asyncio cancellation inherits BaseException and still exits.
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

        Classification uses a bounded persisted identity journal and this
        config-entry load's wall-clock boundary. That survives record-number
        wrap and firmware replay while conservatively suppressing entries whose
        timestamp cannot prove they happened after this load began.
        """
        async with self._lock:
            client = await self._async_ensure_connected_locked(
                intent=_AcquisitionIntent.OPERATION_LOG,
                reason="operation log",
            )
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
            "get_operation_log for %s: %d entries",
            self._key.lockMac,
            len(entries),
        )
        new_entries = self._log_history.classify(self._key.lockMac, entries)
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
                intent=_AcquisitionIntent.EXPLICIT_CONTROL,
                reason=f"explicit {action}",
            )
            if client is None:
                msg = f"Lock {self._key.lockMac} not reachable via Bluetooth"
                if self._reachability_diagnostic is not None:
                    msg = f"{msg}: {self._reachability_diagnostic}"
                raise TTLockError(msg)
            try:
                operation = client.lock if action == "lock" else client.unlock
                task = self._hass.async_create_task(
                    self._async_capture_control_outcome(operation),
                    name=f"{DOMAIN}.control.{action}.{self._key.lockMac}",
                )
                self._control_task = task
                await self._async_await_control_task_locked(
                    task,
                    cast("TtlockBleClient", client),
                )
                LOGGER.debug(
                    "Explicit %s completed for %s (elapsed=%.1fs)",
                    action,
                    self._key.lockMac,
                    monotonic() - started,
                )
            except ControlOutcomeUnknownError as unknown:
                reconciliation_task = self._hass.async_create_task(
                    self._async_capture_reconciliation_outcome(
                        action,
                        client,
                        unknown,
                    ),
                    name=f"{DOMAIN}.reconcile.{action}.{self._key.lockMac}",
                )
                self._reconciliation_task = reconciliation_task
                await self._async_await_reconciliation_task_locked(
                    reconciliation_task,
                    unknown,
                )
                LOGGER.info(
                    "Explicit %s reconciled from a fresh connected state for %s "
                    "(elapsed=%.1fs; no command retry)",
                    action,
                    self._key.lockMac,
                    monotonic() - started,
                )
                return
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
            finally:
                if self._control_task is not None and self._control_task.done():
                    self._control_task = None
                if (
                    self._reconciliation_task is not None
                    and self._reconciliation_task.done()
                ):
                    self._reconciliation_task = None

    async def _async_await_control_task_locked(
        self,
        task: asyncio.Task[Exception | asyncio.CancelledError | None],
        client: TtlockBleClient,
    ) -> None:
        """Retain a committed command despite repeated caller cancellation."""
        while True:
            try:
                outcome = await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    outcome = task.result()
                    if outcome is not None:
                        raise outcome from None
                    return
                if not client.control_committed:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    raise
                LOGGER.debug(
                    "Caller cancellation retained committed control for %s",
                    self._key.lockMac,
                )
            else:
                if outcome is not None:
                    raise outcome
                return

    @staticmethod
    async def _async_capture_control_outcome(
        operation: Callable[[], Awaitable[None]],
    ) -> Exception | asyncio.CancelledError | None:
        """Return one physical attempt's outcome for its durable owner to classify."""
        try:
            await operation()
        except asyncio.CancelledError as exc:
            return exc
        except Exception as exc:  # noqa: BLE001 -- owner classifies every SDK escape
            return exc
        return None

    async def _async_await_reconciliation_task_locked(
        self,
        task: asyncio.Task[Exception | None],
        unknown: ControlOutcomeUnknownError,
    ) -> None:
        """Retain one ambiguity resolution despite repeated caller cancellation."""
        while True:
            try:
                outcome = await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    LOGGER.debug(
                        "Caller cancellation retained control reconciliation for %s",
                        self._key.lockMac,
                    )
                    continue
                if task.cancelled():
                    raise unknown from None
                outcome = task.result()
            if outcome is not None:
                raise outcome from None
            return

    async def _async_capture_reconciliation_outcome(
        self,
        action: str,
        client: TTLockClient,
        unknown: ControlOutcomeUnknownError,
    ) -> Exception | None:
        """Resolve one ambiguity or return its original error after owned cleanup."""
        try:
            if await self._async_reconcile_control_locked(action, client):
                return None
        except asyncio.CancelledError:
            LOGGER.debug(
                "Control reconciliation was cancelled internally for %s",
                self._key.lockMac,
            )
        except Exception:  # noqa: BLE001 -- original ambiguity remains authoritative
            LOGGER.warning(
                "Control reconciliation failed unexpectedly for %s",
                self._key.lockMac,
                exc_info=True,
            )
        try:
            await self._async_disconnect_locked()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 -- retained ownership remains in connection
            LOGGER.warning(
                "Control reconciliation cleanup failed for %s",
                self._key.lockMac,
                exc_info=True,
            )
        return unknown

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
                intent=_AcquisitionIntent.AMBIGUOUS_RECONCILIATION,
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
                intent=_AcquisitionIntent.MANAGEMENT,
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

    async def _async_ensure_connected_locked(  # noqa: PLR0912, PLR0915
        self,
        *,
        active_scan: bool = False,
        intent: _AcquisitionIntent,
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
        budget = _acquisition_budget(intent)
        deadline = started + budget
        self._reachability_diagnostic = None
        LOGGER.debug(
            "Connection acquisition started for %s "
            "(reason=%s, intent=%s, total_budget=%.1fs, "
            "existing_client_connected=%s, active_scan=%s)",
            self._key.lockMac,
            reason,
            intent.value,
            budget,
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
        if not await self._async_retry_pending_cleanup_locked(
            reason=f"retry retained client before {reason}"
        ):
            LOGGER.debug(
                "Connection acquisition blocked for %s "
                "(reason=%s, failure=uncertain_prior_teardown)",
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
        if not await self._async_disconnect_locked(
            reason=f"replace disconnected client before {reason}"
        ):
            LOGGER.debug(
                "Connection acquisition blocked for %s "
                "(reason=%s, failure=installed_client_teardown_uncertain)",
                self._key.lockMac,
                reason,
            )
            return None
        (
            candidate,
            active_acquisition_attempted,
        ) = await self._async_resolve_initial_candidate_locked(
            active_scan=active_scan,
            deadline=deadline,
            reason=reason,
        )
        while candidate is not None and not self._closing_event.is_set():
            cached_route = not active_acquisition_attempted
            speculative_cached_route = active_scan and cached_route
            connect_attempts = (
                SPECULATIVE_CACHED_CONNECT_ATTEMPTS
                if speculative_cached_route
                else ROBUST_CONNECT_ATTEMPTS
            )
            phase_cap = (
                CACHED_CONNECT_TIMEOUT_SECONDS
                if cached_route
                else FRESH_CONNECT_TIMEOUT_SECONDS
            )
            remaining = self._remaining_acquisition_time(deadline)
            phase_timeout = min(phase_cap, remaining)
            if phase_timeout <= 0:
                LOGGER.debug(
                    "Connection acquisition deadline exhausted for %s before GATT "
                    "(reason=%s, intent=%s, total_budget=%.1fs, "
                    "total_elapsed=%.1fs)",
                    self._key.lockMac,
                    reason,
                    intent.value,
                    budget,
                    monotonic() - started,
                )
                break
            if self._pending_client is not None:
                msg = "Cannot create a second TTLock BLE candidate"
                raise RuntimeError(msg)
            self._next_client_generation += 1
            generation = self._next_client_generation
            client = TtlockBleClient.from_ble_device(
                candidate.device,
                self._key,
                disconnected_callback=partial(
                    self._on_disconnected,
                    generation=generation,
                ),
                connect_attempts=connect_attempts,
            )
            self._pending_client = client
            self._owned_client_generation = generation
            client.set_control_allowed(self._control_is_allowed)
            connect_started = monotonic()
            age = (
                max(0.0, MONOTONIC_TIME() - candidate.advertisement_time)
                if candidate.advertisement_time is not None
                else None
            )
            LOGGER.debug(
                "Opening BLE connection for %s "
                "(reason=%s, resolution=%s, source=%s, RSSI=%s, age=%s, "
                "candidate_elapsed=%.1fs, speculative_cached_route=%s, "
                "connect_attempts=%d, intent=%s, phase_cap=%.1fs, "
                "phase_timeout=%.1fs, remaining_intent_time=%.1fs; "
                "GATT retries delegated to ttlock-ble)",
                self._key.lockMac,
                reason,
                candidate.resolution,
                candidate.source,
                candidate.rssi if candidate.rssi is not None else "unknown",
                f"{age:.1f}s" if age is not None else "unknown",
                connect_started - started,
                speculative_cached_route,
                connect_attempts,
                intent.value,
                phase_cap,
                phase_timeout,
                remaining,
            )
            try:
                timeout_result = await self._async_connect_before_deadline(
                    client,
                    phase_timeout=phase_timeout,
                    intent=intent,
                    reason=reason,
                )
            except TTLockError as exc:
                cleanup = await self._async_cleanup_candidate(
                    client,
                    reason=reason,
                )
                LOGGER.debug(
                    "BLE connect failed for %s "
                    "(reason=%s, resolution=%s, source=%s, age=%s, "
                    "connect_elapsed=%.1fs, cleanup_elapsed=%.1fs, "
                    "total_elapsed=%.1fs): %s",
                    self._key.lockMac,
                    reason,
                    candidate.resolution,
                    candidate.source,
                    f"{age:.1f}s" if age is not None else "unknown",
                    monotonic() - connect_started,
                    cleanup.elapsed,
                    monotonic() - started,
                    exc,
                )
                if not cleanup.succeeded:
                    break
            except Exception as exc:  # noqa: BLE001
                # The SDK wraps connector failures, but later notification/GATT
                # setup can still surface a raw exception. Contain and clean it
                # without adding retries or changing the acquisition policy.
                await self._async_discard_failed_setup(client, reason, exc)
                break
            else:
                if timeout_result is None:
                    return await self._async_adopt_connected_candidate(
                        client,
                        candidate=candidate,
                        elapsed=(
                            monotonic() - connect_started,
                            monotonic() - started,
                        ),
                        intent=intent,
                        reason=reason,
                    )
                LOGGER.debug(
                    "BLE connection acquisition timed out for %s "
                    "(reason=%s, intent=%s, resolution=%s, "
                    "configured_phase_deadline=%.1fs, timeout_elapsed=%.1fs, "
                    "cleanup_elapsed=%.1fs, total_budget=%.1fs, "
                    "total_elapsed=%.1fs)",
                    self._key.lockMac,
                    reason,
                    intent.value,
                    candidate.resolution,
                    phase_timeout,
                    timeout_result.acquisition_elapsed,
                    timeout_result.cleanup.elapsed,
                    budget,
                    monotonic() - started,
                )
                if not timeout_result.cleanup.succeeded:
                    break

            if (
                self._closing_event.is_set()
                or not active_scan
                or active_acquisition_attempted
            ):
                break
            active_acquisition_attempted = True
            LOGGER.debug(
                "Cached BLE route ended before command/authentication for %s; "
                "starting one fresh address-scoped acquisition after cleanup "
                "(reason=%s, intent=%s, remaining_intent_time=%.1fs)",
                self._key.lockMac,
                reason,
                intent.value,
                self._remaining_acquisition_time(deadline),
            )
            candidate = await self._async_wait_for_connectable_device_locked(
                deadline=deadline,
                reason=reason,
            )
        if not self._closing_event.is_set():
            self._record_reachability_diagnostic(reason, started)
        return None

    async def _async_adopt_connected_candidate(
        self,
        client: TTLockClient,
        *,
        candidate: _ConnectionCandidate,
        elapsed: tuple[float, float],
        intent: _AcquisitionIntent,
        reason: str,
    ) -> TTLockClient | None:
        """Publish a completed connection unless config-entry shutdown won."""
        if self._closing_event.is_set():
            await self._async_cleanup_candidate(client, reason=reason)
            LOGGER.debug(
                "Discarded late BLE connection for %s (reason=%s, failure=closing)",
                self._key.lockMac,
                reason,
            )
            return None
        if self._pending_client is not client:
            msg = "Connected TTLock candidate is no longer owned"
            raise RuntimeError(msg)
        LOGGER.debug(
            "BLE connection established for %s "
            "(reason=%s, intent=%s, connect_elapsed=%.1fs, total_elapsed=%.1fs)",
            self._key.lockMac,
            reason,
            intent.value,
            elapsed[0],
            elapsed[1],
        )
        client.add_event_listener(self._on_event)
        self._pending_client = None
        self._client = client
        self._last_connection_rssi = candidate.rssi
        self._disconnected.clear()
        self._broadcast_connection_state(connected=True)
        return client

    @staticmethod
    def _remaining_acquisition_time(deadline: float) -> float:
        """Return non-negative time remaining on one absolute intent deadline."""
        return max(0.0, deadline - monotonic())

    async def _async_connect_before_deadline(
        self,
        client: TTLockClient,
        *,
        phase_timeout: float,
        intent: _AcquisitionIntent,
        reason: str,
    ) -> _ConnectTimeoutResult | None:
        """Connect in this task, cancelling and cleaning up at the phase deadline."""
        connect_started = monotonic()
        timeout_context = asyncio.timeout(phase_timeout)
        try:
            async with timeout_context:
                await client.connect()
        except asyncio.CancelledError:
            cleanup = await self._async_cleanup_candidate(
                client,
                reason=reason,
            )
            LOGGER.debug(
                "BLE acquisition externally cancelled for %s "
                "(reason=%s, intent=%s, acquisition_elapsed=%.1fs, "
                "cleanup_elapsed=%.1fs)",
                self._key.lockMac,
                reason,
                intent.value,
                monotonic() - connect_started,
                cleanup.elapsed,
            )
            raise
        except TimeoutError:
            # A TimeoutError raised by the SDK itself is an ordinary setup
            # failure. Only the timeout context's own cancellation is the
            # integration's acquisition deadline.
            if not timeout_context.expired():
                raise
            acquisition_elapsed = monotonic() - connect_started
            cleanup = await self._async_cleanup_candidate(
                client,
                reason=reason,
            )
            return _ConnectTimeoutResult(
                acquisition_elapsed=acquisition_elapsed,
                cleanup=cleanup,
            )
        return None

    async def _async_cleanup_candidate(
        self,
        client: TTLockClient,
        *,
        reason: str,
    ) -> _CleanupResult:
        """Await owned teardown despite repeated caller cancellation."""
        if self._pending_client is not client:
            msg = "Cannot clean up an unowned TTLock BLE client"
            raise RuntimeError(msg)
        task = self._cleanup_task
        if task is None:
            task = self._hass.async_create_task(
                self._async_disconnect_pending_client_once(client, reason=reason),
                name=f"{DOMAIN}.cleanup.{self._key.lockMac}",
            )
            self._cleanup_task = task
            self._cleanup_task_client = client
        elif self._cleanup_task_client is not client:
            msg = "Another TTLock BLE client teardown is already in progress"
            raise RuntimeError(msg)

        interrupted: asyncio.CancelledError | None = None
        while True:
            try:
                cleanup = await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    self._cleanup_task = None
                    self._cleanup_task_client = None
                    raise
                if interrupted is None:
                    interrupted = exc

        if self._cleanup_task is task:
            self._cleanup_task = None
            self._cleanup_task_client = None
        if cleanup.succeeded and self._pending_client is client:
            self._pending_client = None
            self._owned_client_generation = None
            self._broadcast_connection_state(connected=False)
        if interrupted is not None:
            raise interrupted
        return cleanup

    async def _async_disconnect_pending_client_once(
        self,
        client: TTLockClient,
        *,
        reason: str,
    ) -> _CleanupResult:
        """Attempt one backend-agnostic disconnect while ownership is retained."""
        cleanup_started = monotonic()
        if self._client_is_disconnected(client):
            return _CleanupResult(
                elapsed=monotonic() - cleanup_started,
                succeeded=True,
            )
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 -- cleanup failure must not mask outcome
            if self._client_is_disconnected(client):
                LOGGER.debug(
                    "BLE candidate cleanup raised after disconnect for %s (reason=%s)",
                    self._key.lockMac,
                    reason,
                    exc_info=True,
                )
                return _CleanupResult(
                    elapsed=monotonic() - cleanup_started,
                    succeeded=True,
                )
            LOGGER.debug(
                "BLE candidate cleanup failed for %s (reason=%s)",
                self._key.lockMac,
                reason,
                exc_info=True,
            )
            return _CleanupResult(
                elapsed=monotonic() - cleanup_started,
                succeeded=False,
            )
        return _CleanupResult(
            elapsed=monotonic() - cleanup_started,
            succeeded=True,
        )

    @staticmethod
    def _client_is_disconnected(client: TTLockClient) -> bool:
        """Use the public client abstraction as confirmation of link teardown."""
        try:
            return not client.is_connected
        except Exception:  # noqa: BLE001 -- an unreadable state remains uncertain
            return False

    async def _async_retry_pending_cleanup_locked(self, *, reason: str) -> bool:
        """Retry an uncertain teardown before allowing another BLE client."""
        client = self._pending_client
        if client is None:
            return True
        cleanup = await self._async_cleanup_candidate(client, reason=reason)
        return cleanup.succeeded

    async def _async_discard_failed_setup(
        self,
        client: TTLockClient,
        reason: str,
        exc: Exception,
    ) -> None:
        """Clean up an unwrapped SDK setup failure without changing retries."""
        await self._async_cleanup_candidate(client, reason=reason)
        LOGGER.warning(
            "BLE setup failed for %s (reason=%s): %s",
            self._key.lockMac,
            reason,
            exc,
        )

    async def _async_resolve_initial_candidate_locked(
        self,
        *,
        active_scan: bool,
        deadline: float,
        reason: str,
    ) -> tuple[_ConnectionCandidate | None, bool]:
        """Resolve history or perform the one initial active acquisition."""
        candidate = self._resolve_connection_candidate(
            include_scanner_paths=active_scan,
            reason=reason,
            log_miss=True,
        )
        if not active_scan or (
            candidate is not None
            and not self._candidate_is_stale_for_active_acquisition(candidate)
        ):
            return candidate, False
        return (
            await self._async_wait_for_connectable_device_locked(
                deadline=deadline,
                reason=reason,
            ),
            True,
        )

    def _candidate_is_stale_for_active_acquisition(
        self,
        candidate: _ConnectionCandidate,
    ) -> bool:
        """Prefer fresh discovery when HA has proved a cached route missed cadence."""
        if candidate.advertisement_time is None:
            return False
        learned_interval = async_get_learned_advertising_interval(
            self._hass,
            self._key.lockMac,
        )
        if learned_interval is None:
            return False
        age = max(0.0, MONOTONIC_TIME() - candidate.advertisement_time)
        freshness_seconds = max(
            float(EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS),
            learned_interval * 2,
        )
        if age <= freshness_seconds:
            return False
        LOGGER.debug(
            "Cached BLE route for %s missed its learned advertisement cadence; "
            "using fresh address-scoped acquisition before GATT "
            "(resolution=%s, source=%s, age=%.1fs, learned_interval=%.1fs, "
            "freshness_limit=%.1fs)",
            self._key.lockMac,
            candidate.resolution,
            candidate.source,
            age,
            learned_interval,
            freshness_seconds,
        )
        return True

    def _active_history_freshness_seconds(self) -> float:
        """Return a device-aware upper age for active-wait history replay."""
        learned_interval = async_get_learned_advertising_interval(
            self._hass,
            self._key.lockMac,
        )
        return max(
            float(EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS),
            learned_interval * 2 if learned_interval is not None else 0.0,
        )

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
        deadline: float,
        reason: str,
    ) -> _ConnectionCandidate | None:
        """Wait for this address through HA's Auto-mode active-scan scheduler."""
        address = self._key.lockMac
        started = monotonic()
        scan_timeout = min(
            float(EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS),
            self._remaining_acquisition_time(deadline),
        )
        # HA's public API takes whole seconds. Flooring preserves the absolute
        # intent deadline; a sub-second remainder is too short to start a scan.
        scan_timeout_seconds = int(scan_timeout)
        if scan_timeout_seconds <= 0:
            LOGGER.debug(
                "Skipping address-scoped active acquisition for %s "
                "(reason=%s, remaining_intent_time=0.0s)",
                address,
                reason,
            )
            return None
        max_history_age = self._active_history_freshness_seconds()
        LOGGER.debug(
            "Starting address-scoped active acquisition for %s "
            "(reason=%s, timeout=%.1fs, max_history_age=%.1fs)",
            address,
            reason,
            scan_timeout_seconds,
            max_history_age,
        )
        resolved: list[_ConnectionCandidate] = []

        def _candidate_available(service_info: BluetoothServiceInfoBleak) -> bool:
            age = max(0.0, MONOTONIC_TIME() - service_info.time)
            if age > max_history_age:
                LOGGER.debug(
                    "Ignoring replayed connectable history for %s during active "
                    "acquisition (reason=%s, age=%.1fs)",
                    address,
                    reason,
                    age,
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

        # TTLock advertisements can be byte-for-byte static. HA deliberately
        # suppresses callbacks for identical data, even though it still refreshes
        # aggregate history and diagnostics. Clearing this address's deduplication
        # state makes the next real local/proxy advertisement observable here.
        LOGGER.debug(
            "Clearing Bluetooth advertisement deduplication history for %s "
            "before active acquisition (reason=%s)",
            address,
            reason,
        )
        async_clear_advertisement_history(self._hass, address)
        advertisement_task = self._hass.async_create_task(
            async_process_advertisements(
                self._hass,
                _candidate_available,
                BluetoothCallbackMatcher(address=address, connectable=True),
                BluetoothScanningMode.ACTIVE,
                scan_timeout_seconds,
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
                    "Address-scoped active acquisition timed out after %.1fs for %s",
                    scan_timeout_seconds,
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

    async def _async_disconnect_locked(
        self,
        *,
        reason: str = "connection teardown",
    ) -> bool:
        """Tear down the installed client without losing uncertain ownership."""
        if self._client is None:
            return await self._async_retry_pending_cleanup_locked(reason=reason)
        if self._pending_client is not None:
            msg = "Cannot tear down an installed client while another client is pending"
            raise RuntimeError(msg)
        client = self._client
        self._pending_client = client
        self._client = None
        client.remove_event_listener(self._on_event)
        cleanup = await self._async_cleanup_candidate(client, reason=reason)
        return cleanup.succeeded

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

    def _on_disconnected(
        self,
        _client: BleakClient,
        *,
        generation: int | None = None,
    ) -> None:
        """
        Wake the maintain loop the moment bleak signals a drop.

        The drop is also announced here rather than from
        `_async_disconnect_locked`: that runs after the reconnect
        cooldown, so the connectivity sensor claimed a live link for
        most of every cooldown cycle.
        """
        if generation is not None and generation != self._owned_client_generation:
            LOGGER.debug(
                "Ignored disconnect callback from retired client for %s",
                self._key.lockMac,
            )
            return
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
                        intent=_AcquisitionIntent.BACKGROUND_MAINTENANCE,
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
