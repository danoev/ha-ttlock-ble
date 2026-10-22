"""
DataUpdateCoordinator for ttlock_ble.

Reads lock state through the per-lock `TtlockBleConnection` (which keeps
a persistent BLE session and pushes events out-of-band). The
coordinator only owns the periodic state refresh; it does not open BLE
connections itself.

`async_apply_advertisement` publishes battery and tracks the decoded protocol
state bit as a change hint. Only a connected query publishes authoritative
lock state; RC5 lacked the source attribution needed to prove the historical
advertisement-bit persistence assumption on this firmware.
"""

from __future__ import annotations

import asyncio
from asyncio import sleep
from dataclasses import dataclass
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from datetime import timedelta

    from homeassistant.core import HomeAssistant

    from ttlock_ble import LockAdvertisement, LockState

    from .connection import TtlockBleConnection
    from .data import (
        TtlockBleConfigEntry,
        TtlockBleCoordinatorData,
        TtlockBleLockState,
    )


LOCK_STATE_LOCKED = 0
LOCK_STATE_UNLOCKED = 1
BOOTSTRAP_RETRY_DELAYS_SECONDS = (60.0, 120.0, 300.0)
ACTIVITY_RECHECK_DELAYS_SECONDS = (10.0, 20.0, 60.0)
STATE_FRESH_SECONDS = 300.0
ACTIVITY_HINT_COOLDOWN_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class StateAttribution:
    """Safe provenance for the latest authoritative coordinator state."""

    source: str
    observed_at: float
    rssi: int | None
    revision: int


@dataclass(frozen=True, slots=True)
class _StateObservation:
    """A query result ordered when that lock answered, before aggregation."""

    state: TtlockBleLockState
    attribution: StateAttribution


class TtlockBleDataUpdateCoordinator(DataUpdateCoordinator["TtlockBleCoordinatorData"]):
    """Periodically poll BLE state via each lock's persistent connection."""

    config_entry: TtlockBleConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        scan_interval: timedelta,
        connections: dict[str, TtlockBleConnection],
    ) -> None:
        """Pin the polling interval and the per-MAC connection map."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=scan_interval,
        )
        self._connections = connections
        self._state_attribution: dict[str, StateAttribution] = {}
        self._observation_revisions: dict[str, int] = {}
        self._advertisement_hints: dict[str, LockState] = {}
        self._new_records_hints: dict[str, bool] = {}
        self._active_scan_requested: set[str] = set()
        self._log_tasks: dict[str, asyncio.Task[None]] = {}
        self._bootstrap_retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._activity_tasks: dict[str, asyncio.Task[None]] = {}
        self._activity_last_started: dict[str, float] = {}
        self._freshness_timers: dict[str, asyncio.TimerHandle] = {}
        self._verification_status: dict[str, str] = {}
        self._shutting_down = False
        for mac, connection in connections.items():
            connection.set_state_listener(partial(self._apply_connection_state, mac))

    def state_freshness(self, mac: str) -> dict[str, str | bool | None]:
        """Expose uncertainty separately from the retained authoritative value."""
        attribution = self.state_attribution(mac)
        status = self._verification_status.get(mac, "never_verified")
        if (
            status == "verified"
            and attribution is not None
            and monotonic() - attribution.observed_at > STATE_FRESH_SECONDS
        ):
            status = "stale"
        return {
            "state_source": attribution.source if attribution else None,
            "state_verification": status,
            "state_uncertain": status != "verified",
        }

    def _apply_connection_state(
        self, mac: str, raw_state: LockState | None, battery: int | None, source: str
    ) -> None:
        """Sequence a direct read at completion, before BLE cleanup can yield."""
        locked = _parse_lock_state(raw_state)
        if self._shutting_down:
            return
        if locked is None:
            self.async_note_query_failed(mac)
        else:
            self.async_apply_authoritative_state(
                mac,
                locked=locked,
                battery_level=battery,
                source=source,
                rssi=self._connections[mac].last_connection_rssi,
            )

    @callback
    def async_note_unlock_hint(self, mac: str) -> None:
        """Bound replayed hints without granting them state authority."""
        if (
            monotonic() - self._activity_last_started.get(mac, float("-inf"))
            < ACTIVITY_HINT_COOLDOWN_SECONDS
        ):
            return
        self._schedule_activity_verification(mac)

    @callback
    def async_note_query_failed(self, mac: str) -> None:
        """Retain the last verified value and timestamp, but expose uncertainty."""
        self._verification_status[mac] = "query_failed"
        self.async_update_listeners()

    @callback
    def _after_state_observation(
        self, mac: str, *, locked: bool, previous: bool | None, source: str
    ) -> None:
        """Start one finite unlock cycle without renewing it on repeated reads."""
        self._verification_status[mac] = "verified"
        if (timer := self._freshness_timers.pop(mac, None)) is not None:
            timer.cancel()
        if not self._shutting_down:
            self._freshness_timers[mac] = self.hass.loop.call_later(
                STATE_FRESH_SECONDS, self.async_update_listeners
            )
        if locked or source == "command":
            task = self._activity_tasks.pop(mac, None)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        if locked or self._shutting_down or mac not in self._connections:
            return
        if previous is False and source != "command":
            if mac in self._activity_tasks:
                self._verification_status[mac] = "confirmation_pending"
            return
        if mac in self._activity_tasks:
            self._verification_status[mac] = "confirmation_pending"
            return
        self._schedule_activity_verification(mac)

    def _schedule_activity_verification(self, mac: str) -> None:
        if (
            self._shutting_down
            or mac not in self._connections
            or mac in self._activity_tasks
        ):
            return
        self._verification_status[mac] = "confirmation_pending"
        self._activity_last_started[mac] = monotonic()
        task = self.hass.async_create_background_task(
            self._async_verify_after_unlock(mac),
            name=f"{DOMAIN}.auto_lock_verification.{mac}",
            eager_start=False,
        )
        self._activity_tasks[mac] = task

    async def _async_verify_after_unlock(self, mac: str) -> None:
        """Query at most three times, releasing the BLE slot after every read."""
        connection = self._connections[mac]
        delays = list(ACTIVITY_RECHECK_DELAYS_SECONDS)
        cached_delay = connection.auto_lock_delay
        if isinstance(cached_delay, int) and cached_delay > 0:
            delays[0] = max(delays[0], cached_delay + 2.0)
        try:
            for delay in delays:
                await sleep(delay)
                if self._shutting_down:
                    return
                success = await connection.async_reconcile_state()
                if success and (self.data or {}).get(mac, {}).get("locked") is True:
                    return
                self._verification_status[mac] = (
                    "confirmation_pending" if success else "query_failed"
                )
                self.async_update_listeners()
            # An unlocked observation is real, but auto-lock was not confirmed.
            if self._verification_status.get(mac) != "query_failed":
                self._verification_status[mac] = "followup_exhausted"
            self.async_update_listeners()
        except asyncio.CancelledError:
            if self._activity_tasks.get(mac) is asyncio.current_task():
                self._verification_status[mac] = "verification_interrupted"
                self.async_update_listeners()
            raise
        finally:
            if self._activity_tasks.get(mac) is asyncio.current_task():
                self._activity_tasks.pop(mac, None)

    @property
    def connections(self) -> dict[str, TtlockBleConnection]:
        """Return the per-MAC connection map this coordinator polls."""
        return self._connections

    @callback
    def async_has_state(self, mac: str) -> bool:
        """Report whether a lock state has ever been read for `mac`, by any channel."""
        snapshot = (self.data or {}).get(mac)
        return snapshot is not None and snapshot.get("locked") is not None

    @callback
    def async_apply_advertisement(
        self,
        mac: str,
        advertisement: LockAdvertisement,
        *,
        rssi: int | None,
    ) -> None:
        """
        Publish advertised battery and treat bit 0 only as a state-change hint.

        The hint never overwrites an authoritative connected query or command.
        A changed hint requests a refresh, which resolves physical state through
        ``SEARCH_BICYCLE_STATUS`` without forcing the scanner globally active.
        """
        LOGGER.debug(
            "Advertisement hint for %s "
            "(source=advertisement, hint=%s, new_records=%s, "
            "setting_mode=%s, battery=%d, RSSI=%s)",
            mac,
            advertisement.lock_state.name,
            advertisement.has_new_records,
            advertisement.is_setting_mode,
            advertisement.battery,
            rssi if rssi is not None else "unknown",
        )
        previous_hint = self._advertisement_hints.get(mac)
        previous_new_records = self._new_records_hints.get(mac)
        self._advertisement_hints[mac] = advertisement.lock_state
        self._new_records_hints[mac] = advertisement.has_new_records
        current = (self.data or {}).get(mac, {})
        snapshot: TtlockBleLockState = {
            "locked": current.get("locked"),
            "battery_level": advertisement.battery,
        }
        self.data = {**(self.data or {}), mac: snapshot}
        self.async_update_listeners()
        state_hint_changed = (
            previous_hint is not None and previous_hint != advertisement.lock_state
        )
        new_records_appeared = (
            advertisement.has_new_records and previous_new_records is not True
        )
        if state_hint_changed or new_records_appeared:
            if advertisement.lock_state == LOCK_STATE_UNLOCKED:
                self.async_note_unlock_hint(mac)
            LOGGER.debug(
                "Advertisement activity for %s "
                "(state_hint_changed=%s, new_records_appeared=%s); requesting "
                "authoritative state and operation-log synchronization",
                mac,
                state_hint_changed,
                new_records_appeared,
            )
            self.async_request_active_state_refresh(mac)
            self.hass.async_create_task(self.async_request_refresh())

    @callback
    def async_request_active_state_refresh(self, mac: str) -> None:
        """Mark one lock's next authoritative refresh as active-capable."""
        if mac in self._connections:
            self._active_scan_requested.add(mac)

    def state_attribution(self, mac: str) -> StateAttribution | None:
        """Return provenance for the latest authoritative state of ``mac``."""
        return self._state_attribution.get(mac)

    def _next_attribution(
        self,
        mac: str,
        *,
        source: str,
        rssi: int | None,
    ) -> StateAttribution:
        """Sequence a completed observation without an await or wall-clock ordering."""
        revision = self._observation_revisions.get(mac, 0) + 1
        self._observation_revisions[mac] = revision
        return StateAttribution(
            source=source,
            observed_at=monotonic(),
            rssi=rssi,
            revision=revision,
        )

    @callback
    def async_apply_authoritative_state(
        self,
        mac: str,
        *,
        locked: bool,
        battery_level: int | None,
        source: str,
        rssi: int | None,
    ) -> None:
        """Publish a command or fresh direct-query state with attribution."""
        current = (self.data or {}).get(mac, {})
        self._after_state_observation(
            mac, locked=locked, previous=current.get("locked"), source=source
        )
        snapshot: TtlockBleLockState = {
            "locked": locked,
            "battery_level": battery_level
            if battery_level is not None
            else current.get("battery_level"),
        }
        self._state_attribution[mac] = self._next_attribution(
            mac,
            source=source,
            rssi=rssi,
        )
        self.async_set_updated_data({**(self.data or {}), mac: snapshot})

    async def _async_update_data(self) -> TtlockBleCoordinatorData:
        """Poll every connection once and return the aggregated state map."""
        requested = set(self._active_scan_requested)
        revisions = dict(self._observation_revisions)
        self._active_scan_requested.difference_update(requested)
        poll_tasks = {
            mac: self._async_poll(
                mac,
                connection,
                active_scan=mac in requested or not self.async_has_state(mac),
            )
            for mac, connection in self._connections.items()
        }
        results = await asyncio.gather(*poll_tasks.values(), return_exceptions=True)
        state: TtlockBleCoordinatorData = dict(self.data or {})
        for mac, result in zip(poll_tasks, results, strict=True):
            if isinstance(result, BaseException):
                LOGGER.warning("Failed to poll %s", mac)
                if self._observation_revisions.get(mac, 0) == revisions.get(mac, 0):
                    self._verification_status[mac] = "query_failed"
                state.setdefault(mac, {"locked": None, "battery_level": None})
                self._async_schedule_bootstrap_retry(mac)
                continue
            if result is None:
                if self._observation_revisions.get(mac, 0) == revisions.get(mac, 0):
                    self._verification_status[mac] = "query_failed"
                state.setdefault(mac, {"locked": None, "battery_level": None})
                self._async_schedule_bootstrap_retry(mac)
                continue
            latest = self._state_attribution.get(mac)
            if latest is not None and result.attribution.revision <= latest.revision:
                # A different lock's slow response must not age this query past
                # a command or query already published for the same lock.
                continue
            reading = result.state
            current = state.get(mac, {})
            state[mac] = {
                "locked": reading.get("locked")
                if reading.get("locked") is not None
                else current.get("locked"),
                "battery_level": reading.get("battery_level")
                if reading.get("battery_level") is not None
                else current.get("battery_level"),
            }
            if reading.get("locked") is not None:
                self._after_state_observation(
                    mac,
                    locked=bool(reading["locked"]),
                    previous=current.get("locked"),
                    source="query",
                )
                connection = self._connections[mac]
                self._state_attribution[mac] = result.attribution
                self.hass.loop.call_soon(
                    self._async_schedule_operation_log,
                    connection,
                )
        return state

    @callback
    def _async_schedule_bootstrap_retry(self, mac: str) -> None:
        """Retry an as-yet-unknown lock without running a keep-warm loop."""
        if self.async_has_state(mac):
            return
        current = self._bootstrap_retry_tasks.get(mac)
        if current is not None and not current.done():
            return
        task = self.hass.async_create_background_task(
            self._async_retry_unknown_state(mac),
            name=f"{DOMAIN}.bootstrap_retry.{mac}",
        )
        self._bootstrap_retry_tasks[mac] = task

        def _remove_bootstrap_task(_completed: asyncio.Task[None]) -> None:
            self._bootstrap_retry_tasks.pop(mac, None)

        task.add_done_callback(_remove_bootstrap_task)

    async def _async_retry_unknown_state(self, mac: str) -> None:
        """Use bounded HA-managed active acquisitions until state is known."""
        attempt = 0
        while not self.async_has_state(mac):
            delay = BOOTSTRAP_RETRY_DELAYS_SECONDS[
                min(attempt, len(BOOTSTRAP_RETRY_DELAYS_SECONDS) - 1)
            ]
            LOGGER.debug(
                "Lock %s has no authoritative state; retrying bootstrap in %.0fs",
                mac,
                delay,
            )
            await asyncio.sleep(delay)
            if self.async_has_state(mac):
                return
            self.async_request_active_state_refresh(mac)
            await self.async_request_refresh()
            attempt += 1

    async def _async_poll(
        self,
        mac: str,
        connection: TtlockBleConnection,
        *,
        active_scan: bool,
    ) -> _StateObservation | None:
        """Query one lock through its persistent connection."""
        result = await connection.async_query_state(active_scan=active_scan)
        if result is None:
            return None
        raw_state, battery = result
        locked = _parse_lock_state(raw_state)
        if locked is None:
            return None
        return _StateObservation(
            state={"locked": locked, "battery_level": battery},
            attribution=self._next_attribution(
                mac,
                source="query",
                rssi=connection.last_connection_rssi,
            ),
        )

    @callback
    def _async_schedule_operation_log(
        self,
        connection: TtlockBleConnection,
    ) -> None:
        """Fetch supplementary history without delaying primary state publication."""
        mac = connection.key.lockMac
        current = self._log_tasks.get(mac)
        if current is not None and not current.done():
            return
        task = self.hass.async_create_background_task(
            self._async_sync_operation_log(connection),
            name=f"{DOMAIN}.operation_log.{mac}",
        )
        self._log_tasks[mac] = task

        def _remove_log_task(_completed: asyncio.Task[None]) -> None:
            self._log_tasks.pop(mac, None)

        task.add_done_callback(_remove_log_task)

    async def _async_sync_operation_log(
        self,
        connection: TtlockBleConnection,
    ) -> None:
        """Synchronize history while keeping failures separate from lock state."""
        try:
            await connection.async_get_operation_log()
        except Exception:  # noqa: BLE001
            # The log feeds the event entity only. Failing the poll here
            # would throw away the state and battery already read, and the
            # entity would keep whatever HA last wrote optimistically —
            # a confidently wrong value that silently disables any
            # state-based automation built on it.
            LOGGER.debug(
                "Operation log read failed for %s; keeping the state read",
                connection.key.lockMac,
                exc_info=True,
            )

    async def async_shutdown(self) -> None:
        """Cancel supplementary work before the coordinator is discarded."""
        self._shutting_down = True
        for timer in self._freshness_timers.values():
            timer.cancel()
        self._freshness_timers.clear()
        for connection in self._connections.values():
            connection.set_state_listener(None)
        tasks = [
            *self._log_tasks.values(),
            *self._bootstrap_retry_tasks.values(),
            *self._activity_tasks.values(),
        ]
        self._log_tasks.clear()
        self._bootstrap_retry_tasks.clear()
        self._activity_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().async_shutdown()


def _parse_lock_state(raw: int | None) -> bool | None:
    """Translate the SDK's tri-state lock value into HA's `bool | None`."""
    if raw == LOCK_STATE_LOCKED:
        return True
    if raw == LOCK_STATE_UNLOCKED:
        return False
    return None
