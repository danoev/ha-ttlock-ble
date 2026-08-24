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
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class StateAttribution:
    """Safe provenance for the latest authoritative coordinator state."""

    source: str
    observed_at: float
    rssi: int | None


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
        self._advertisement_hints: dict[str, LockState] = {}
        self._new_records_hints: dict[str, bool] = {}
        self._active_scan_requested: set[str] = set()
        self._log_tasks: dict[str, asyncio.Task[None]] = {}

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
        snapshot: TtlockBleLockState = {
            "locked": locked,
            "battery_level": battery_level
            if battery_level is not None
            else current.get("battery_level"),
        }
        self._state_attribution[mac] = StateAttribution(
            source=source,
            observed_at=monotonic(),
            rssi=rssi,
        )
        self.async_set_updated_data({**(self.data or {}), mac: snapshot})

    async def _async_update_data(self) -> TtlockBleCoordinatorData:
        """Poll every connection once and return the aggregated state map."""
        requested = set(self._active_scan_requested)
        self._active_scan_requested.difference_update(requested)
        poll_tasks = {
            mac: self._async_poll(
                connection,
                active_scan=mac in requested or not self.async_has_state(mac),
            )
            for mac, connection in self._connections.items()
        }
        results = await asyncio.gather(*poll_tasks.values(), return_exceptions=True)
        state: TtlockBleCoordinatorData = dict(self.data or {})
        for mac, result in zip(poll_tasks, results, strict=True):
            if isinstance(result, BaseException):
                LOGGER.warning("Failed to poll %s: %s", mac, result)
                state.setdefault(mac, {"locked": None, "battery_level": None})
                continue
            if result is None:
                state.setdefault(mac, {"locked": None, "battery_level": None})
                continue
            current = state.get(mac, {})
            state[mac] = {
                "locked": result.get("locked")
                if result.get("locked") is not None
                else current.get("locked"),
                "battery_level": result.get("battery_level")
                if result.get("battery_level") is not None
                else current.get("battery_level"),
            }
            if result.get("locked") is not None:
                connection = self._connections[mac]
                self._state_attribution[mac] = StateAttribution(
                    source="query",
                    observed_at=monotonic(),
                    rssi=connection.last_connection_rssi,
                )
                self.hass.loop.call_soon(
                    self._async_schedule_operation_log,
                    connection,
                )
        return state

    async def _async_poll(
        self,
        connection: TtlockBleConnection,
        *,
        active_scan: bool,
    ) -> TtlockBleLockState | None:
        """Query one lock through its persistent connection."""
        result = await connection.async_query_state(active_scan=active_scan)
        if result is None:
            return None
        raw_state, battery = result
        return {
            "locked": _parse_lock_state(raw_state),
            "battery_level": battery,
        }

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
        tasks = list(self._log_tasks.values())
        self._log_tasks.clear()
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
