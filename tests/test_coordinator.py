from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ttlock_ble.coordinator import (
    TtlockBleDataUpdateCoordinator,
    _parse_lock_state,
)


def test_parse_lock_state_locked() -> None:
    assert _parse_lock_state(0) is True


def test_parse_lock_state_unlocked() -> None:
    assert _parse_lock_state(1) is False


@pytest.mark.parametrize("raw_state", [-1, 2, 9])
def test_parse_lock_state_unknown(raw_state: int) -> None:
    assert _parse_lock_state(raw_state) is None


def _mock_connection(*, query_return=(0, 80)) -> MagicMock:
    conn = MagicMock()
    conn.async_query_state = AsyncMock(return_value=query_return)
    conn.async_get_operation_log = AsyncMock(return_value=[])
    conn.last_connection_rssi = -70
    return conn


def _coordinator(hass, connections):
    return TtlockBleDataUpdateCoordinator(
        hass=hass,
        scan_interval=timedelta(seconds=60),
        connections=connections,
    )


async def test_poll_keeps_state_when_log_read_fails(hass) -> None:
    """The operation log only feeds the event entity; it must not void the state."""
    conn = _mock_connection(query_return=(0, 80))
    conn.async_get_operation_log = AsyncMock(side_effect=ValueError("garbled frame"))
    coordinator = _coordinator(hass, {"AA:BB:CC:DD:EE:FF": conn})
    data = await coordinator._async_update_data()
    assert data["AA:BB:CC:DD:EE:FF"] == {"locked": True, "battery_level": 80}


async def test_coordinator_exposes_connections(hass) -> None:
    connections = {"AA:BB:CC:DD:EE:FF": _mock_connection()}
    coordinator = _coordinator(hass, connections)
    assert coordinator.connections is connections


async def test_coordinator_polls_state_locked(hass, sample_virtual_key) -> None:
    conn = _mock_connection(query_return=(0, 75))
    coordinator = _coordinator(hass, {sample_virtual_key.lockMac: conn})
    data = await coordinator._async_update_data()
    state = data[sample_virtual_key.lockMac]
    assert state["locked"] is True
    assert state["battery_level"] == 75
    conn.async_query_state.assert_awaited_once_with(active_scan=True)


async def test_coordinator_polls_state_unlocked(hass, sample_virtual_key) -> None:
    conn = _mock_connection(query_return=(1, 60))
    coordinator = _coordinator(hass, {sample_virtual_key.lockMac: conn})
    data = await coordinator._async_update_data()
    assert data[sample_virtual_key.lockMac]["locked"] is False


async def test_coordinator_blanks_the_readings_when_a_query_returns_none(
    hass,
    sample_virtual_key,
) -> None:
    conn = _mock_connection(query_return=None)
    coordinator = _coordinator(hass, {sample_virtual_key.lockMac: conn})
    data = await coordinator._async_update_data()
    state = data[sample_virtual_key.lockMac]
    assert state["locked"] is None
    assert state["battery_level"] is None
    conn.async_query_state.assert_awaited_once_with(active_scan=True)


async def test_known_state_routine_poll_does_not_request_active_scan(
    hass,
    sample_virtual_key,
) -> None:
    """Hourly polling remains battery-conscious after bootstrap succeeds."""
    conn = _mock_connection(query_return=(0, 80))
    coordinator = _coordinator(hass, {sample_virtual_key.lockMac: conn})
    coordinator.async_set_updated_data(
        {sample_virtual_key.lockMac: {"locked": True, "battery_level": 80}}
    )

    await coordinator._async_update_data()

    conn.async_query_state.assert_awaited_once_with(active_scan=False)


async def test_coordinator_polls_every_connection_once(
    hass, sample_virtual_key
) -> None:
    other_key = sample_virtual_key.lockMac.replace("AA", "11")
    conns = {
        sample_virtual_key.lockMac: _mock_connection(),
        other_key: _mock_connection(query_return=(1, 50)),
    }
    coordinator = _coordinator(hass, conns)
    data = await coordinator._async_update_data()
    assert set(data) == set(conns)
    for conn in conns.values():
        conn.async_query_state.assert_awaited_once()


def _advertisement(*, unlocked: bool, battery: int = 66):
    from ttlock_ble import LockAdvertisement, LockState

    return LockAdvertisement(
        protocol_type=5,
        protocol_version=3,
        scene=2,
        lock_state=LockState.UNLOCKED if unlocked else LockState.LOCKED,
        has_new_records=False,
        is_setting_mode=False,
        battery=battery,
        lock_mac="AA:BB:CC:DD:EE:FF",
    )


async def test_apply_advertisement_publishes_battery_without_overwriting_state(
    hass,
    sample_virtual_key,
) -> None:
    conn = _mock_connection()
    coordinator = _coordinator(hass, {sample_virtual_key.lockMac: conn})
    coordinator.async_apply_advertisement(
        sample_virtual_key.lockMac,
        _advertisement(unlocked=True, battery=66),
        rssi=-72,
    )
    state = coordinator.data[sample_virtual_key.lockMac]
    assert state["locked"] is None
    assert state["battery_level"] == 66
    conn.async_query_state.assert_not_awaited()


async def test_changed_advertisement_hint_requests_active_authoritative_query(
    hass,
    sample_virtual_key,
) -> None:
    """A changed hint may reacquire a route but never becomes physical state."""
    mac = sample_virtual_key.lockMac
    conn = _mock_connection(query_return=(1, 65))
    coordinator = _coordinator(hass, {mac: conn})
    coordinator.async_set_updated_data({mac: {"locked": True, "battery_level": 70}})
    coordinator.async_request_refresh = AsyncMock(return_value=None)
    coordinator.async_apply_advertisement(
        mac,
        _advertisement(unlocked=False, battery=69),
        rssi=-83,
    )
    coordinator.async_apply_advertisement(
        mac,
        _advertisement(unlocked=True, battery=68),
        rssi=-82,
    )

    await coordinator._async_update_data()

    conn.async_query_state.assert_awaited_once_with(active_scan=True)
    assert coordinator.data[mac]["locked"] is True


async def test_apply_advertisement_keeps_the_other_locks(hass) -> None:
    coordinator = _coordinator(hass, {})
    coordinator.async_set_updated_data({"OTHER": {"locked": None}})
    coordinator.async_apply_advertisement(
        "AA:BB:CC:DD:EE:FF", _advertisement(unlocked=False), rssi=-72
    )
    assert coordinator.data["OTHER"] == {"locked": None}
    assert coordinator.data["AA:BB:CC:DD:EE:FF"]["locked"] is None


async def test_has_state_is_false_until_a_lock_state_is_known(hass) -> None:
    coordinator = _coordinator(hass, {})
    assert coordinator.async_has_state("AA:BB:CC:DD:EE:FF") is False
    coordinator.async_set_updated_data(
        {"AA:BB:CC:DD:EE:FF": {"locked": None}},
    )
    assert coordinator.async_has_state("AA:BB:CC:DD:EE:FF") is False
    coordinator.async_apply_advertisement(
        "AA:BB:CC:DD:EE:FF", _advertisement(unlocked=False), rssi=-72
    )
    assert coordinator.async_has_state("AA:BB:CC:DD:EE:FF") is False


async def test_stale_advertisement_cannot_overwrite_authoritative_state(hass) -> None:
    """Protocol bit 0 is a hint and cannot revert a newer connected query."""
    mac = "AA:BB:CC:DD:EE:FF"
    coordinator = _coordinator(hass, {})
    coordinator.async_set_updated_data({mac: {"locked": True, "battery_level": 80}})
    coordinator.async_apply_authoritative_state(
        mac,
        locked=False,
        battery_level=None,
        source="command",
        rssi=-83,
    )

    coordinator.async_apply_advertisement(
        mac,
        _advertisement(unlocked=False, battery=79),
        rssi=-84,
    )

    assert coordinator.data[mac] == {"locked": False, "battery_level": 79}
    attribution = coordinator.state_attribution(mac)
    assert attribution is not None
    assert attribution.source == "command"
    assert attribution.rssi == -83


async def test_query_state_attribution_is_deterministic(
    hass,
    sample_virtual_key,
) -> None:
    """A successful connected query records its source and selected-route RSSI."""
    conn = _mock_connection(query_return=(1, 60))
    coordinator = _coordinator(hass, {sample_virtual_key.lockMac: conn})

    await coordinator._async_update_data()

    attribution = coordinator.state_attribution(sample_virtual_key.lockMac)
    assert attribution is not None
    assert attribution.source == "query"
    assert attribution.rssi == -70


async def test_poll_that_raises_blanks_only_that_lock(hass, sample_virtual_key) -> None:
    """One lock throwing must not take the others' readings down with it."""
    other = "11:BB:CC:DD:EE:FF"
    failing = _mock_connection()
    failing.async_query_state = AsyncMock(side_effect=RuntimeError("adapter gone"))
    coordinator = _coordinator(
        hass,
        {
            sample_virtual_key.lockMac: failing,
            other: _mock_connection(query_return=(1, 50)),
        },
    )
    data = await coordinator._async_update_data()
    assert data[sample_virtual_key.lockMac] == {"locked": None, "battery_level": None}
    assert data[other]["locked"] is False
