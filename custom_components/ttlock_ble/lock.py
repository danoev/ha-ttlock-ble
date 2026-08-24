"""Lock platform for ttlock_ble."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from ttlock_ble import LockState, TTLockError

from .connection import event_signal
from .const import DOMAIN, LOGGER
from .entity import TtlockBleEntity

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from ttlock_ble import LockEvent, VirtualKey

    from .connection import TtlockBleConnection
    from .coordinator import TtlockBleDataUpdateCoordinator
    from .data import TtlockBleConfigEntry


COMMAND_SETTLE_SECONDS = 4.0

# How long to wait after a command before reading the operation log. The
# lock writes the record for the command it just executed a moment after
# answering, so an immediate read would miss it.
LOG_FETCH_DELAY_SECONDS = 5.0


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: TtlockBleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one lock entity per known `VirtualKey`."""
    data = entry.runtime_data
    async_add_entities(
        TtlockBleLock(data.coordinator, key, data.connections[key.lockMac])
        for key in data.virtual_keys
    )


class TtlockBleLock(TtlockBleEntity, LockEntity):
    """
    Smart lock backed by a persistent `TtlockBleConnection`.

    While a command is in flight the entity reports the transitional
    states HA defines for the platform — `locking` and `unlocking` —
    covering the BLE session setup plus the round trip to the lock,
    which is seconds rather than milliseconds. `jammed`, `open` and
    `opening` are deliberately never reported: the firmware exposes no
    jam signal (only a bolt position and a success/failure byte) and
    has no latch command distinct from unlocking, so any value there
    would be a guess.

    Settled state is reported via `_attr_is_locked`. It is updated:
    - On every coordinator refresh that returned a known connected query.
    - Optimistically the moment `async_lock`/`async_unlock` succeed.
    - On a forced connected re-query triggered by any push-event hint.

    The post-command settle window (`COMMAND_SETTLE_SECONDS`) discards
    state readings that disagree with the just-commanded state during
    a short window after each command — the lock's BLE state can
    briefly disagree with the mechanical state and we don't want the
    UI to bounce.
    """

    _attr_translation_key = "lock"

    def __init__(
        self,
        coordinator: TtlockBleDataUpdateCoordinator,
        key: VirtualKey,
        connection: TtlockBleConnection,
    ) -> None:
        """Bind the entity to its connection + coordinator + key."""
        super().__init__(coordinator, key)
        self._connection = connection
        self._attr_is_locked = None
        self._attr_is_locking = False
        self._attr_is_unlocking = False
        self._settle_until: float = 0.0
        self._command_lock = asyncio.Lock()
        self._sync_from_coordinator()

    @property
    def unique_id(self) -> str:
        """Return a stable unique id for this entity."""
        return f"{self._key.lockMac}_lock"

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
        """Adopt the coordinator's freshest known lock state, if any."""
        self._sync_from_coordinator()
        super()._handle_coordinator_update()

    @callback
    def _on_lock_event(self, event: LockEvent) -> None:
        """
        Treat a push notification as an activity hint and query fresh state.

        The decoded state on the 3-byte heartbeat is not authoritative bolt
        position on all supported lock firmware. The 15-byte log-entry variant
        has no state at all. Both forms therefore request a connected state
        query instead of writing the push hint directly to Home Assistant.
        """
        LOGGER.debug(
            "Event-driven update for %s (cmd_echo=0x%02x status=%d lock_state=%s)",
            self._key.lockMac,
            event.cmd_echo,
            event.status,
            event.lock_state,
        )
        if event.lock_state is not None:
            LOGGER.debug(
                "Push state hint for %s (source=push, hint=%s); "
                "requesting authoritative query",
                self._key.lockMac,
                event.lock_state.name,
            )
        self._async_create_tracked_task(self._async_query_and_apply(), "query_state")

    def _apply_lock_state(
        self,
        raw_state: LockState,
        *,
        source: str,
        observed_at: float | None = None,
        rssi: int | None = None,
    ) -> bool:
        """Write `raw_state` onto `_attr_is_locked` respecting the settle window."""
        # `==`, not `is`: callers may hand us the plain int the wire carried
        # rather than the enum member, and identity would silently say False.
        new_locked = raw_state == LockState.LOCKED
        if time.monotonic() < self._settle_until and new_locked != self._attr_is_locked:
            LOGGER.debug(
                "Suppressing %s flip for %s during command settle window",
                "lock" if new_locked else "unlock",
                self._key.lockMac,
            )
            return False
        self._set_authoritative_state(
            locked=new_locked,
            source=source,
            observed_at=observed_at,
            rssi=rssi,
        )
        self._write_state_if_added()
        return True

    def _set_authoritative_state(
        self,
        *,
        locked: bool,
        source: str,
        observed_at: float | None = None,
        rssi: int | None = None,
    ) -> None:
        """Apply state and log every transition with safe source attribution."""
        previous = self._attr_is_locked
        if previous != locked:
            age = (
                0.0 if observed_at is None else max(time.monotonic() - observed_at, 0.0)
            )
            LOGGER.debug(
                "State transition for %s: %s -> %s (source=%s, age=%.1fs, RSSI=%s)",
                self._key.lockMac,
                _state_label(previous),
                _state_label(locked),
                source,
                age,
                rssi if rssi is not None else "unknown",
            )
        self._attr_is_locked = locked

    def _sync_from_coordinator(self) -> None:
        """
        Copy `locked` from the coordinator snapshot into `_attr_is_locked`.

        A `None` value (the lock is out of range, or the poll failed)
        leaves the cached state untouched — the entity keeps showing
        whatever was last known. The post-command settle
        window also suppresses conflicting coordinator data: the lock's
        BLE state can briefly disagree with the just-commanded state
        and we don't want the UI to bounce.
        """
        state = self._lock_state
        if state is None:
            return
        locked = state.get("locked")
        if locked is None:
            return
        if time.monotonic() < self._settle_until and locked != self._attr_is_locked:
            LOGGER.debug(
                "Suppressing coordinator flip for %s during command settle window",
                self._key.lockMac,
            )
            return
        attribution = self.coordinator.state_attribution(self._key.lockMac)
        self._set_authoritative_state(
            locked=locked,
            source=attribution.source
            if attribution is not None
            else "coordinator_snapshot",
            observed_at=attribution.observed_at if attribution is not None else None,
            rssi=attribution.rssi if attribution is not None else None,
        )

    async def async_lock(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Send LOCK over the persistent BLE connection."""
        await self._async_run_command("lock")

    async def async_unlock(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Send UNLOCK over the persistent BLE connection."""
        await self._async_run_command("unlock")

    async def _async_run_command(self, action: str) -> None:
        """
        Dispatch the BLE command, optimistically update state, refresh.

        The transitional state is cleared in `finally`, not on the error
        path: a command can also end in `asyncio.CancelledError` (HA
        cancels the service call when the client that issued it goes
        away mid-round-trip) and `LockEntity.state` ranks `is_locking`
        above `is_locked`, so anything that skips the reset leaves the
        entity claiming a movement that already ended.

        `_command_lock` serializes the bookkeeping, not just the wire:
        HA runs concurrent service calls, and the BLE layer's own lock
        only orders the round trips. Without it the first command to
        finish would clear the flags and publish a settled state while
        the second was still turning the bolt.
        """
        async with self._command_lock:
            self._set_in_flight(action)
            try:
                if action == "lock":
                    await self._connection.async_lock()
                else:
                    await self._connection.async_unlock()
            except TTLockError as exc:
                LOGGER.warning(
                    "BLE %s failed for %s: %s",
                    action,
                    self._key.lockMac,
                    exc,
                )
                msg = f"Failed to {action} {self._key.lockMac}: {exc}"
                raise HomeAssistantError(msg) from exc
            else:
                self._set_authoritative_state(
                    locked=action == "lock",
                    source="command",
                    rssi=self._connection.last_connection_rssi,
                )
                self._settle_until = time.monotonic() + COMMAND_SETTLE_SECONDS
                self.coordinator.async_apply_authoritative_state(
                    self._key.lockMac,
                    locked=action == "lock",
                    battery_level=None,
                    source="command",
                    rssi=self._connection.last_connection_rssi,
                )
            finally:
                self._clear_in_flight()
                self._write_state_if_added()
        self._async_create_tracked_task(
            self._async_fetch_log_after_command(),
            "fetch_log_after_command",
        )
        await self.coordinator.async_request_refresh()

    def _async_create_tracked_task(
        self,
        coro: Coroutine[None, None, None],
        name: str,
    ) -> None:
        """
        Run `coro` as a task the config entry owns.

        A bare `hass.async_create_task` is not tied to the entry, so it
        survives an unload and goes on talking to a connection that was
        already stopped.
        """
        self.coordinator.config_entry.async_create_task(
            self.hass,
            coro,
            name=f"{DOMAIN}.{name}.{self._key.lockMac}",
        )

    def _set_in_flight(self, action: str) -> None:
        """Publish the transitional state for a command that just started."""
        self._attr_is_locking = action == "lock"
        self._attr_is_unlocking = action != "lock"
        self._write_state_if_added()

    def _clear_in_flight(self) -> None:
        """Drop the transitional state without publishing it on its own."""
        self._attr_is_locking = False
        self._attr_is_unlocking = False

    def _write_state_if_added(self) -> None:
        """Publish state, unless the entity was removed while we were awaiting."""
        if self.hass is not None and self.entity_id is not None:
            self.async_write_ha_state()

    async def _async_fetch_log_after_command(self) -> None:
        """Fetch operation log shortly after a command to capture the new record."""
        await asyncio.sleep(LOG_FETCH_DELAY_SECONDS)
        await self._connection.async_get_operation_log()

    async def _async_query_and_apply(self) -> None:
        """Force-query state and apply it to `_attr_is_locked` if known."""
        result = await self._connection.async_query_state()
        if result is None:
            LOGGER.debug(
                "Forced query for %s returned no state",
                self._key.lockMac,
            )
            return
        raw_state, battery = result
        if raw_state is not None and self._apply_lock_state(
            raw_state,
            source="query_after_push",
            rssi=self._connection.last_connection_rssi,
        ):
            self.coordinator.async_apply_authoritative_state(
                self._key.lockMac,
                locked=raw_state == LockState.LOCKED,
                battery_level=battery,
                source="query_after_push",
                rssi=self._connection.last_connection_rssi,
            )


def _state_label(locked: bool | None) -> str:  # noqa: FBT001
    """Return a stable log label for a tri-state lock value."""
    if locked is None:
        return "UNKNOWN"
    return "LOCKED" if locked else "UNLOCKED"
