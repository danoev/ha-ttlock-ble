"""External activity needs bounded connected verification, never timer inference."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ttlock_ble import (
    AdministrativeInventory,
    InventoryReason,
    InventoryResult,
    LockState,
)

from custom_components.ttlock_ble.connection import TtlockBleConnection
from custom_components.ttlock_ble.coordinator import TtlockBleDataUpdateCoordinator
from custom_components.ttlock_ble.lock import TtlockBleLock
from custom_components.ttlock_ble.sensor import TtlockBleLastVerifiedStateSensor

COORD = "custom_components.ttlock_ble.coordinator"
CONN = "custom_components.ttlock_ble.connection"


def coordinator_for(hass, key, connection):
    coordinator = TtlockBleDataUpdateCoordinator(
        hass, timedelta(hours=1), {key.lockMac: connection}
    )
    coordinator.async_apply_authoritative_state(
        key.lockMac, locked=True, battery_level=80, source="query", rssi=-70
    )
    return coordinator


async def test_external_unlock_then_auto_lock_without_further_advertisements(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
):
    """Regression: no inventory panel, notification or hourly poll is needed."""
    conn = TtlockBleConnection(hass, sample_virtual_key)
    coordinator = coordinator_for(hass, sample_virtual_key, conn)
    lock = TtlockBleLock(coordinator, sample_virtual_key, conn)
    mock_ttlock_client.query_state.side_effect = [
        (LockState.UNLOCKED, 80),
        (LockState.LOCKED, 80),
    ]
    with patch(f"{COORD}.sleep", new=AsyncMock(), create=True):
        await lock._async_query_and_apply()
        await hass.async_block_till_done()
    try:
        assert coordinator.data[sample_virtual_key.lockMac]["locked"] is True
        assert lock.is_locked is False  # Not added: the real platform subscribes below.
        lock._sync_from_coordinator()
        assert lock.is_locked is True
        assert (
            coordinator.state_attribution(sample_virtual_key.lockMac).source
            == "query_after_activity"
        )
        assert mock_ttlock_client.query_state.await_count == 2
        mock_ttlock_client.disconnect.assert_awaited_once()
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize(
    "states",
    [
        [LockState.UNLOCKED, LockState.LOCKED],
        [OSError("synthetic-private-frame"), LockState.LOCKED],
        [LockState.UNLOCKED, LockState.UNLOCKED, LockState.UNLOCKED],
        [OSError("private"), OSError("private"), OSError("private")],
    ],
)
async def test_delayed_auto_lock_failure_recovery_and_finite_budget(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client, states, caplog
):
    conn = TtlockBleConnection(hass, sample_virtual_key)
    coordinator = coordinator_for(hass, sample_virtual_key, conn)
    mock_ttlock_client.query_state.side_effect = [
        item if isinstance(item, Exception) else (item, 80) for item in states
    ]
    mac = sample_virtual_key.lockMac
    with patch(f"{COORD}.sleep", new=AsyncMock()) as delay:
        coordinator.async_apply_authoritative_state(
            mac, locked=False, battery_level=80, source="query_after_push", rssi=-70
        )
        await hass.async_block_till_done()
    try:
        expected_locked = states[-1] == LockState.LOCKED
        assert coordinator.data[mac]["locked"] is expected_locked
        assert mock_ttlock_client.connect.await_count == len(states)
        assert mock_ttlock_client.disconnect.await_count == len(states)
        assert [call.args[0] for call in delay.await_args_list] == [10, 20, 60][
            : len(states)
        ]
        assert (
            coordinator.state_freshness(mac)["state_uncertain"] is not expected_locked
        )
        assert not coordinator._activity_tasks
        assert "private" not in caplog.text
        mock_ttlock_client.lock.assert_not_awaited()
        mock_ttlock_client.unlock.assert_not_awaited()
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("method", ["keypad_pin", "fingerprint", "ic_card"])
@pytest.mark.parametrize("private", [False, True])
async def test_inventory_reuses_one_connection_to_publish_real_bolt_state(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client, method, private
):
    summary = InventoryResult((), complete=True, limitation="")
    result = AdministrativeInventory(summary, ()) if private else summary
    method_name = (
        "get_administrative_inventory"
        if private
        else {
            "keypad_pin": "get_passcodes",
            "fingerprint": "get_fingerprints",
            "ic_card": "get_cards",
        }[method]
    )
    setattr(mock_ttlock_client, method_name, AsyncMock(return_value=result))
    conn = TtlockBleConnection(hass, sample_virtual_key)
    coordinator = coordinator_for(hass, sample_virtual_key, conn)
    coordinator.data[sample_virtual_key.lockMac]["locked"] = False
    read = (
        conn.async_get_administrative_inventory
        if private
        else conn.async_get_access_inventory
    )
    try:
        assert await read(method) is result
        assert coordinator.data[sample_virtual_key.lockMac]["locked"] is True
        assert (
            coordinator.state_attribution(sample_virtual_key.lockMac).source
            == "query_after_inventory"
        )
        mock_ttlock_client.connect.assert_awaited_once()
        mock_ttlock_client.query_state.assert_awaited_once()
        mock_ttlock_client.disconnect.assert_awaited_once()
        assert conn.inventory_diagnostics["cleanup_confirmed"] is True
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize(
    "failure", [OSError("synthetic-private-frame"), "timeout", "unknown"]
)
async def test_optional_query_failure_preserves_inventory_and_attribution(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client, caplog, failure
):
    result = InventoryResult((), complete=True, limitation="")
    mock_ttlock_client.get_passcodes = AsyncMock(return_value=result)
    if failure == "timeout":
        mock_ttlock_client.query_state.side_effect = asyncio.Event().wait
    elif failure == "unknown":
        mock_ttlock_client.query_state.return_value = (None, 80)
    else:
        mock_ttlock_client.query_state.side_effect = failure
    conn = TtlockBleConnection(hass, sample_virtual_key)
    coordinator = coordinator_for(hass, sample_virtual_key, conn)
    before = coordinator.state_attribution(sample_virtual_key.lockMac)
    with patch(f"{CONN}.OPTIONAL_STATE_TIMEOUT_SECONDS", 0.001):
        assert await conn.async_get_access_inventory("keypad_pin") is result
    try:
        assert coordinator.state_attribution(sample_virtual_key.lockMac) is before
        assert conn.inventory_diagnostics["cleanup_confirmed"] is True
        mock_ttlock_client.disconnect.assert_awaited_once()
        assert "synthetic-private-frame" not in caplog.text
    finally:
        await coordinator.async_shutdown()


async def test_interrupted_inventory_skips_optional_query(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
):
    result = InventoryResult(
        (),
        complete=False,
        limitation="interrupted",
        transport_interrupted=True,
        reason=InventoryReason.TRANSPORT,
    )
    mock_ttlock_client.get_cards = AsyncMock(return_value=result)
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_get_access_inventory("ic_card") is result
    mock_ttlock_client.query_state.assert_not_awaited()
    mock_ttlock_client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("operation", ["inventory", "activity"])
@pytest.mark.parametrize("phase", ["acquisition", "query"])
async def test_explicit_control_cancels_background_query_before_owned_cleanup(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client, operation, phase
):
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release_cleanup = asyncio.Event()
    order = []

    async def block():
        started.set()
        await asyncio.Future()

    async def connect():
        if phase == "acquisition" and mock_ttlock_client.connect.await_count == 1:
            await block()

    async def disconnect():
        order.append("cleanup")
        cleaning.set()
        await release_cleanup.wait()

    async def control():
        order.append("unlock")

    mock_ttlock_client.connect.side_effect = connect
    if phase == "query":
        mock_ttlock_client.query_state.side_effect = block
    mock_ttlock_client.disconnect.side_effect = disconnect
    mock_ttlock_client.unlock.side_effect = control
    mock_ttlock_client.get_passcodes = AsyncMock(
        return_value=InventoryResult((), complete=True, limitation="")
    )
    conn = TtlockBleConnection(hass, sample_virtual_key)
    read = (
        conn.async_reconcile_state()
        if operation == "activity"
        else conn.async_get_access_inventory("keypad_pin")
    )
    task = asyncio.create_task(read)
    await asyncio.wait_for(started.wait(), 1)
    explicit = asyncio.create_task(conn.async_unlock())
    await asyncio.wait_for(cleaning.wait(), 1)
    assert conn._lock.locked()
    mock_ttlock_client.unlock.assert_not_awaited()
    task.cancel()  # Repeated cancellation must still retain cleanup ownership.
    release_cleanup.set()
    await asyncio.wait_for(explicit, 1)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert order == ["cleanup", "unlock"]
    mock_ttlock_client.unlock.assert_awaited_once()
    assert mock_ttlock_client.connect.await_count == 2


async def test_scheduled_work_deduplicated_and_cancelled_by_confirmation_and_shutdown(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
):
    conn = TtlockBleConnection(hass, sample_virtual_key)
    coordinator = coordinator_for(hass, sample_virtual_key, conn)
    mac = sample_virtual_key.lockMac
    coordinator.async_note_unlock_hint(mac)
    first = coordinator._activity_tasks[mac]
    coordinator.async_note_unlock_hint(mac)
    assert coordinator._activity_tasks[mac] is first
    assert coordinator.data[mac]["locked"] is True
    await asyncio.sleep(0)
    coordinator.async_apply_authoritative_state(
        mac, locked=True, battery_level=80, source="query", rssi=-70
    )
    await asyncio.gather(first, return_exceptions=True)
    assert not coordinator._activity_tasks
    coordinator.async_apply_authoritative_state(
        mac, locked=False, battery_level=80, source="query", rssi=-70
    )
    second = coordinator._activity_tasks[mac]
    await asyncio.sleep(0)
    await coordinator.async_shutdown()
    assert second.cancelled()
    assert not coordinator._activity_tasks
    assert not coordinator._freshness_timers
    mock_ttlock_client.connect.assert_not_awaited()
    conn._publish_state_read((LockState.LOCKED, 80), "query_after_inventory")
    assert coordinator.data[mac]["locked"] is False


async def test_recent_read_delay_is_used_without_configuration_write(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
):
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_get_auto_lock_time() == 30
    coordinator = coordinator_for(hass, sample_virtual_key, conn)
    with patch(f"{COORD}.sleep", new=AsyncMock()) as delay:
        coordinator.async_apply_authoritative_state(
            sample_virtual_key.lockMac,
            locked=False,
            battery_level=80,
            source="query",
            rssi=-70,
        )
        await hass.async_block_till_done()
    try:
        assert delay.await_args.args == (32.0,)
        mock_ttlock_client.set_auto_lock_time.assert_not_awaited()
        with patch(f"{CONN}.monotonic", return_value=conn._auto_lock_delay[1] + 3601):
            assert conn.auto_lock_delay is None
    finally:
        await coordinator.async_shutdown()


async def test_failed_and_stale_state_keep_last_verified_timestamp(
    hass, sample_virtual_key
):
    conn = MagicMock(auto_lock_delay=None)
    coordinator = coordinator_for(hass, sample_virtual_key, conn)
    sensor = TtlockBleLastVerifiedStateSensor(coordinator, sample_virtual_key)
    verified = sensor.native_value
    attribution = coordinator.state_attribution(sample_virtual_key.lockMac)
    coordinator.async_note_query_failed(sample_virtual_key.lockMac)
    assert sensor.extra_state_attributes["state_uncertain"] is True
    assert sensor.extra_state_attributes["state_verification"] == "query_failed"
    assert abs((sensor.native_value - verified).total_seconds()) < 1
    assert coordinator.data[sample_virtual_key.lockMac]["locked"] is True
    coordinator._verification_status[sample_virtual_key.lockMac] = "verified"
    with patch(f"{COORD}.monotonic", return_value=attribution.observed_at + 301):
        assert sensor.extra_state_attributes["state_verification"] == "stale"
    await coordinator.async_shutdown()


async def test_real_ha_entity_updates_after_fingerprint_hint_without_inventory(
    hass, setup_integration, sample_virtual_key, mock_ttlock_connection
):
    from homeassistant.helpers.dispatcher import async_dispatcher_send
    from ttlock_ble import LockEvent

    from custom_components.ttlock_ble.connection import event_signal

    coordinator = setup_integration.runtime_data.coordinator
    mac = sample_virtual_key.lockMac
    mock_ttlock_connection.async_query_state.return_value = (LockState.UNLOCKED, 80)

    async def reconcile():
        coordinator._apply_connection_state(
            mac, LockState.LOCKED, 79, "query_after_activity"
        )
        return True

    mock_ttlock_connection.async_reconcile_state = AsyncMock(side_effect=reconcile)
    mock_ttlock_connection.auto_lock_delay = None
    with patch(f"{COORD}.sleep", new=AsyncMock()):
        async_dispatcher_send(
            hass, event_signal(mac), LockEvent.from_payload(0x14, 1, bytes([80, 1, 2]))
        )
        await hass.async_block_till_done()
    state = hass.states.async_all("lock")[0]
    assert state.state == "locked"
    assert state.attributes["state_source"] == "query_after_activity"
    assert state.attributes["state_uncertain"] is False
    mock_ttlock_connection.async_get_administrative_inventory.assert_not_called()
    mock_ttlock_connection.async_get_access_inventory.assert_not_awaited()
    await hass.config_entries.async_unload(setup_integration.entry_id)
    assert coordinator._shutting_down
    assert not coordinator._activity_tasks


@pytest.mark.parametrize("busy", ["control", "lock", "closing"])
async def test_background_never_queues_behind_busy_operations(
    hass, sample_virtual_key, mock_ttlock_client, busy
):
    conn = TtlockBleConnection(hass, sample_virtual_key)
    if busy == "control":
        conn._control_requests = 1
    elif busy == "lock":
        await conn._lock.acquire()
    else:
        conn._closing = True
    try:
        assert await conn.async_reconcile_state() is False
        mock_ttlock_client.connect.assert_not_awaited()
    finally:
        if busy == "lock":
            conn._lock.release()


async def test_background_query_timeout_and_unknown_state_are_not_proof(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
):
    conn = TtlockBleConnection(hass, sample_virtual_key)
    coordinator = coordinator_for(hass, sample_virtual_key, conn)
    before = coordinator.state_attribution(sample_virtual_key.lockMac)
    mock_ttlock_client.query_state.side_effect = asyncio.Event().wait
    with patch(f"{CONN}.ACTIVITY_STATE_TIMEOUT_SECONDS", 0.001):
        assert await conn.async_reconcile_state() is False
    mock_ttlock_client.query_state.side_effect = None
    mock_ttlock_client.query_state.return_value = (None, 80)
    assert await conn.async_reconcile_state() is False
    assert coordinator.state_attribution(sample_virtual_key.lockMac) is before
    assert mock_ttlock_client.disconnect.await_count == 2
    await coordinator.async_shutdown()


async def test_no_candidate_and_unconfirmed_cleanup_prevent_replacement(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client, caplog
):
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with patch.object(
        conn, "_async_ensure_connected_locked", new=AsyncMock(return_value=None)
    ):
        assert await conn.async_reconcile_state() is False
    mock_ttlock_client.disconnect.side_effect = OSError("synthetic-private-cleanup")
    assert await conn.async_reconcile_state() is True
    assert conn._pending_client is mock_ttlock_client
    assert await conn.async_reconcile_state() is False
    mock_ttlock_client.connect.assert_awaited_once()
    assert "synthetic-private-cleanup" not in caplog.text


async def test_inventory_observation_cannot_be_aged_past_newer_command_by_cleanup(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
):
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def cleanup():
        cleanup_started.set()
        await release_cleanup.wait()

    conn = TtlockBleConnection(hass, sample_virtual_key)
    coordinator = coordinator_for(hass, sample_virtual_key, conn)
    mock_ttlock_client.get_cards = AsyncMock(
        return_value=InventoryResult((), complete=True, limitation="")
    )
    mock_ttlock_client.disconnect.side_effect = cleanup
    inventory = asyncio.create_task(conn.async_get_access_inventory("ic_card"))
    await cleanup_started.wait()
    assert (
        coordinator.state_attribution(sample_virtual_key.lockMac).source
        == "query_after_inventory"
    )
    coordinator.async_apply_authoritative_state(
        sample_virtual_key.lockMac,
        locked=False,
        battery_level=79,
        source="command",
        rssi=-70,
    )
    command = coordinator.state_attribution(sample_virtual_key.lockMac)
    release_cleanup.set()
    await inventory
    assert coordinator.state_attribution(sample_virtual_key.lockMac) is command
    assert coordinator.data[sample_virtual_key.lockMac]["locked"] is False
    await coordinator.async_shutdown()


async def test_entry_unload_cancels_delayed_task_without_gatt_access(
    hass, setup_integration, sample_virtual_key, mock_ttlock_connection
):
    coordinator = setup_integration.runtime_data.coordinator
    mac = sample_virtual_key.lockMac
    coordinator.async_apply_authoritative_state(
        mac, locked=False, battery_level=80, source="query", rssi=-70
    )
    task = coordinator._activity_tasks[mac]
    await asyncio.sleep(0)
    await hass.config_entries.async_unload(setup_integration.entry_id)
    assert task.cancelled()
    mock_ttlock_connection.async_reconcile_state.assert_not_called()


async def test_readback_unknown_or_modify_ack_invalidates_cached_auto_lock_delay(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
):
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_get_auto_lock_time() == 30
    assert conn.auto_lock_delay == 30
    await conn.async_set_auto_lock_time(15)
    assert conn.auto_lock_delay is None
    mock_ttlock_client.get_auto_lock_time.return_value = -1
    assert await conn.async_get_auto_lock_time() == -1
    assert conn.auto_lock_delay is None
