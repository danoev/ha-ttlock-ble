"""Diagnostic sensors for TTLock battery and authoritative state freshness."""

from __future__ import annotations

from datetime import timedelta
from time import monotonic
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .connection import event_signal
from .entity import TtlockBleEntity

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from ttlock_ble import LockEvent, VirtualKey

    from .coordinator import TtlockBleDataUpdateCoordinator
    from .data import TtlockBleConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: TtlockBleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create battery and authoritative-state diagnostic sensors per lock."""
    data = entry.runtime_data
    async_add_entities(
        entity
        for key in data.virtual_keys
        for entity in (
            TtlockBleBatterySensor(data.coordinator, key),
            TtlockBleLastVerifiedStateSensor(data.coordinator, key),
        )
    )


class TtlockBleLastVerifiedStateSensor(TtlockBleEntity, SensorEntity):
    """Timestamp of the last connected query or acknowledged control result."""

    _attr_translation_key = "last_verified_state"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        """Keep one stable freshness sensor per lock."""
        return f"{self._key.lockMac}_last_verified_state"

    @property
    def native_value(self) -> datetime | None:
        """Map monotonic attribution to UTC without trusting advertisements."""
        attribution = self.coordinator.state_attribution(self._key.lockMac)
        if attribution is None:
            return None
        elapsed = max(0.0, monotonic() - attribution.observed_at)
        return dt_util.utcnow() - timedelta(seconds=elapsed)

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | None]:
        """Preserve timestamp/source while exposing failed or pending confirmation."""
        return self.coordinator.state_freshness(self._key.lockMac)


class TtlockBleBatterySensor(TtlockBleEntity, SensorEntity):
    """Battery level reported by the lock — refreshed on poll and on every push."""

    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: TtlockBleDataUpdateCoordinator,
        key: VirtualKey,
    ) -> None:
        """Bind the sensor to its key + coordinator."""
        super().__init__(coordinator, key)
        self._attr_native_value: int | None = None
        self._sync_from_coordinator()

    @property
    def unique_id(self) -> str:
        """Return a stable unique id for this entity."""
        return f"{self._key.lockMac}_battery"

    async def async_added_to_hass(self) -> None:
        """Subscribe to push-event notifications for the lock's MAC."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                event_signal(self._key.lockMac),
                self._on_lock_event,
            ),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Adopt the coordinator's freshest battery reading, if any."""
        self._sync_from_coordinator()
        super()._handle_coordinator_update()

    @callback
    def _on_lock_event(self, event: LockEvent) -> None:
        """Adopt the battery byte the lock embedded in its push payload."""
        if event.battery is None:
            return
        self._attr_native_value = event.battery
        self.async_write_ha_state()

    def _sync_from_coordinator(self) -> None:
        """Copy `battery_level` from the coordinator snapshot, if known."""
        state = self._lock_state
        if state is None:
            return
        battery = state.get("battery_level")
        if battery is None:
            return
        self._attr_native_value = battery
