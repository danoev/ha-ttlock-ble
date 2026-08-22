from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.bluetooth import (
    BluetoothReachabilityIntent,
    BluetoothScanningMode,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from ttlock_ble import KeyboardPwdType, LockEvent, TTLockError

from custom_components.ttlock_ble.connection import (
    EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS,
    TtlockBleConnection,
    connection_signal,
    event_signal,
    log_signal,
)


def _log_entry(record_number: int) -> SimpleNamespace:
    """Build a minimal LogEntry stand-in keyed by `record_number`."""
    return SimpleNamespace(record_number=record_number)


def test_event_signal_lowercases_mac() -> None:
    assert event_signal("AA:BB:CC:DD:EE:FF") == "ttlock_ble_event_aa:bb:cc:dd:ee:ff"


def test_connection_signal_lowercases_mac() -> None:
    assert (
        connection_signal("AA:BB:CC:DD:EE:FF")
        == "ttlock_ble_connection_aa:bb:cc:dd:ee:ff"
    )


async def test_is_connected_false_before_start(hass, sample_virtual_key) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert conn.is_connected is False


async def test_key_exposed(hass, sample_virtual_key) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert conn.key is sample_virtual_key


async def test_query_state_returns_none_when_device_missing(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    mock_ble_resolver.return_value = None
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_query_state() is None
    mock_ttlock_client.connect.assert_not_awaited()
    mock_active_scan.assert_not_awaited()


async def test_missing_device_logs_reachability_diagnostic(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    caplog,
) -> None:
    """A missing device reports HA's scanner/path explanation at debug level."""
    mock_ble_resolver.return_value = None
    diagnostic = "unknown; 2 scanners registered, 1 scanning, 1 connectable"
    with (
        patch(
            "custom_components.ttlock_ble.connection."
            "async_address_reachability_diagnostics",
            return_value=diagnostic,
        ) as reachability,
        caplog.at_level(logging.DEBUG, logger="custom_components.ttlock_ble"),
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        assert await conn.async_query_state() is None

    reachability.assert_called_once_with(
        hass,
        sample_virtual_key.lockMac,
        BluetoothReachabilityIntent.CONNECTION,
    )
    assert diagnostic in caplog.text
    for credential in (
        sample_virtual_key.aesKeyStr,
        sample_virtual_key.unlockKey,
        sample_virtual_key.adminPs,
    ):
        assert credential not in caplog.text


async def test_missing_device_propagates_reachability_diagnostic_to_command(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_active_scan,
) -> None:
    """A scan timeout keeps TTLockError and includes HA's safe diagnosis."""
    mock_ble_resolver.return_value = None
    diagnostic = "only in non-connectable history (no connectable path)"
    with patch(
        "custom_components.ttlock_ble.connection."
        "async_address_reachability_diagnostics",
        return_value=diagnostic,
    ) as reachability:
        conn = TtlockBleConnection(hass, sample_virtual_key)
        with pytest.raises(TTLockError) as error:
            await conn.async_lock()

    reachability.assert_called_once_with(
        hass,
        sample_virtual_key.lockMac,
        BluetoothReachabilityIntent.CONNECTION,
    )
    assert diagnostic in str(error.value)
    mock_active_scan.assert_awaited_once()
    for credential in (
        sample_virtual_key.aesKeyStr,
        sample_virtual_key.unlockKey,
        sample_virtual_key.adminPs,
    ):
        assert credential not in str(error.value)


async def test_missing_device_propagates_diagnostic_without_management_secret(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_active_scan,
) -> None:
    """Management failures include HA's diagnosis but never the submitted PIN."""
    mock_ble_resolver.return_value = None
    diagnostic = "connectable scanner paths are full"
    secret_code = "583921"
    with patch(
        "custom_components.ttlock_ble.connection."
        "async_address_reachability_diagnostics",
        return_value=diagnostic,
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        with pytest.raises(TTLockError) as error:
            await conn.async_add_passcode(
                secret_code,
                pwd_type=KeyboardPwdType.PERMANENT,
                start_date="0001311400",
                end_date="9912311400",
            )

    assert diagnostic in str(error.value)
    mock_active_scan.assert_awaited_once()
    assert secret_code not in str(error.value)
    for credential in (
        sample_virtual_key.aesKeyStr,
        sample_virtual_key.unlockKey,
        sample_virtual_key.adminPs,
    ):
        assert credential not in str(error.value)


async def test_reachability_diagnostic_failure_preserves_missing_device_behavior(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """A diagnostic failure cannot replace the original missing-device result."""
    mock_ble_resolver.return_value = None
    with patch(
        "custom_components.ttlock_ble.connection."
        "async_address_reachability_diagnostics",
        side_effect=RuntimeError("diagnostics unavailable"),
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        assert await conn.async_query_state() is None
    mock_ttlock_client.connect.assert_not_awaited()


async def test_query_state_returns_none_when_connect_fails(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    mock_ttlock_client.connect = AsyncMock(side_effect=TTLockError("ble fail"))
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_query_state() is None


async def test_query_state_happy_path(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    mock_ttlock_client.query_state = AsyncMock(return_value=(0, 88))
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_query_state() == (0, 88)
    mock_ttlock_client.add_event_listener.assert_called_once()


async def test_query_state_disconnects_on_ttlock_error(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    mock_ttlock_client.query_state = AsyncMock(side_effect=TTLockError("read fail"))
    conn = TtlockBleConnection(hass, sample_virtual_key)
    result = await conn.async_query_state()
    assert result is None
    mock_ttlock_client.disconnect.assert_awaited()


async def test_query_state_reuses_open_connection(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_query_state()
    await conn.async_query_state()
    # Only one BLE connect because the client stays connected.
    assert mock_ttlock_client.connect.await_count == 1


async def test_lock_happy(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_lock()
    mock_ttlock_client.lock.assert_awaited_once()
    mock_active_scan.assert_not_awaited()


async def test_explicit_command_active_scans_then_resolves_connectable_device(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """An exact connectable advertisement lets the existing client path continue."""
    mock_ble_resolver.side_effect = [None, mock_ble_device]
    mock_active_scan.side_effect = None
    mock_active_scan.return_value = SimpleNamespace(address=sample_virtual_key.lockMac)

    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_lock()

    assert mock_ble_resolver.call_count == 2
    for call in mock_ble_resolver.call_args_list:
        assert call.args == (hass, sample_virtual_key.lockMac)
        assert call.kwargs == {"connectable": True}
    mock_active_scan.assert_awaited_once()
    scan_args = mock_active_scan.await_args.args
    assert scan_args[0] is hass
    assert scan_args[1](SimpleNamespace(address=sample_virtual_key.lockMac)) is True
    assert scan_args[1](SimpleNamespace(address="00:00:00:00:00:00")) is False
    assert scan_args[2] == {
        "address": sample_virtual_key.lockMac,
        "connectable": True,
    }
    assert scan_args[3] is BluetoothScanningMode.ACTIVE
    assert scan_args[4] == EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS == 25
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.lock.assert_awaited_once()


async def test_unload_interrupts_explicit_active_scan(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """Unload does not wait out the scan timeout or open a late connection."""
    mock_ble_resolver.return_value = None
    scan_started = asyncio.Event()
    scan_cancelled = asyncio.Event()

    async def _wait_for_advertisement(*_args) -> None:
        scan_started.set()
        try:
            await asyncio.Future()
        finally:
            scan_cancelled.set()

    active_scan = AsyncMock(side_effect=_wait_for_advertisement)
    with patch(
        "custom_components.ttlock_ble.connection.async_process_advertisements",
        new=active_scan,
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        operation = asyncio.create_task(conn.async_lock())
        await asyncio.wait_for(scan_started.wait(), timeout=1)
        await asyncio.wait_for(conn.async_stop(), timeout=1)
        with pytest.raises(TTLockError, match="not reachable"):
            await operation

    assert scan_cancelled.is_set()
    mock_ttlock_client.connect.assert_not_awaited()


async def test_explicit_operation_cancellation_cleans_up_active_scan(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """Cancelling the user operation also cancels its HA scan waiter."""
    mock_ble_resolver.return_value = None
    scan_started = asyncio.Event()
    scan_cancelled = asyncio.Event()

    async def _wait_for_advertisement(*_args) -> None:
        scan_started.set()
        try:
            await asyncio.Future()
        finally:
            scan_cancelled.set()

    active_scan = AsyncMock(side_effect=_wait_for_advertisement)
    with patch(
        "custom_components.ttlock_ble.connection.async_process_advertisements",
        new=active_scan,
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        operation = asyncio.create_task(conn.async_unlock())
        await asyncio.wait_for(scan_started.wait(), timeout=1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation

    assert scan_cancelled.is_set()
    mock_ttlock_client.connect.assert_not_awaited()


async def test_simultaneous_commands_share_one_active_scan(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """The per-lock operation mutex prevents duplicate simultaneous scans."""
    mock_ble_resolver.side_effect = [None, mock_ble_device]
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()

    async def _wait_for_advertisement(*_args) -> SimpleNamespace:
        scan_started.set()
        await release_scan.wait()
        return SimpleNamespace(address=sample_virtual_key.lockMac)

    active_scan = AsyncMock(side_effect=_wait_for_advertisement)
    with patch(
        "custom_components.ttlock_ble.connection.async_process_advertisements",
        new=active_scan,
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        lock_task = asyncio.create_task(conn.async_lock())
        unlock_task = asyncio.create_task(conn.async_unlock())
        await asyncio.wait_for(scan_started.wait(), timeout=1)
        release_scan.set()
        await asyncio.gather(lock_task, unlock_task)

    active_scan.assert_awaited_once()
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.lock.assert_awaited_once()
    mock_ttlock_client.unlock.assert_awaited_once()


async def test_unlock_happy(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_unlock()
    mock_ttlock_client.unlock.assert_awaited_once()


async def test_auto_lock_management(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_get_auto_lock_time() == 30
    await conn.async_set_auto_lock_time(0)
    mock_ttlock_client.get_auto_lock_time.assert_awaited_once()
    mock_ttlock_client.set_auto_lock_time.assert_awaited_once_with(0)


async def test_auto_lock_management_active_scans_when_initially_missing(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """Auto-lock actions opt into the same one-shot connectable scan."""
    mock_ble_resolver.side_effect = [None, mock_ble_device]
    mock_active_scan.side_effect = None
    mock_active_scan.return_value = SimpleNamespace(address=sample_virtual_key.lockMac)

    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_get_auto_lock_time() == 30

    mock_active_scan.assert_awaited_once()
    mock_ttlock_client.get_auto_lock_time.assert_awaited_once()


async def test_passcode_management(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_add_passcode(
        "583921",
        pwd_type=KeyboardPwdType.PERIOD,
        start_date="2608221500",
        end_date="2608251000",
    )
    await conn.async_delete_passcode(
        "583921",
        pwd_type=KeyboardPwdType.PERIOD,
    )
    await conn.async_clear_passcodes()
    mock_ttlock_client.add_passcode.assert_awaited_once_with(
        "583921",
        pwd_type=KeyboardPwdType.PERIOD,
        start_date="2608221500",
        end_date="2608251000",
    )
    mock_ttlock_client.delete_passcode.assert_awaited_once_with(
        "583921",
        pwd_type=KeyboardPwdType.PERIOD,
    )
    mock_ttlock_client.clear_passcodes.assert_awaited_once()


async def test_clear_passcodes_does_not_trigger_active_scan(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_active_scan,
) -> None:
    """The destructive clear action is outside the initial active-scan scope."""
    mock_ble_resolver.return_value = None
    with patch(
        "custom_components.ttlock_ble.connection."
        "async_address_reachability_diagnostics",
        return_value="unknown (never seen by any scanner)",
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        with pytest.raises(TTLockError, match="not reachable"):
            await conn.async_clear_passcodes()
    mock_active_scan.assert_not_awaited()


async def test_management_error_does_not_echo_passcode(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    secret_code = "583921"
    mock_ttlock_client.add_passcode = AsyncMock(side_effect=ValueError(secret_code))
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with pytest.raises(TTLockError) as error:
        await conn.async_add_passcode(
            secret_code,
            pwd_type=KeyboardPwdType.PERMANENT,
            start_date="0001311400",
            end_date="9912311400",
        )
    assert secret_code not in str(error.value)


async def test_lock_raises_when_device_missing(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_active_scan,
) -> None:
    mock_ble_resolver.return_value = None
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with pytest.raises(TTLockError, match="not reachable"):
        await conn.async_lock()


async def test_lock_propagates_and_disconnects_on_command_error(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    mock_ttlock_client.lock = AsyncMock(side_effect=TTLockError("bad psFromLock"))
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with pytest.raises(TTLockError, match="bad psFromLock"):
        await conn.async_lock()
    mock_ttlock_client.disconnect.assert_awaited()


async def test_event_listener_dispatches_to_signal(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    received: list[LockEvent] = []
    async_dispatcher_connect(
        hass,
        event_signal(sample_virtual_key.lockMac),
        received.append,
    )
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_query_state()
    listener = mock_ttlock_client.add_event_listener.call_args[0][0]
    pushed = LockEvent(cmd_echo=0x47, status=1, data=b"\x01")
    listener(pushed)
    await hass.async_block_till_done()
    assert received == [pushed]


async def test_disconnect_swallows_exceptions(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    mock_ttlock_client.disconnect = AsyncMock(side_effect=RuntimeError("boom"))
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_query_state()
    await conn.async_stop()
    assert conn.is_connected is False


async def test_async_start_creates_task(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with patch.multiple(
        "custom_components.ttlock_ble.connection",
        RECONNECT_INITIAL_BACKOFF=0.01,
        RECONNECT_MAX_BACKOFF=0.05,
    ):
        await conn.async_start()
        await asyncio.sleep(0.05)
        await conn.async_stop()
    assert conn.is_connected is False


async def test_async_start_idempotent(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with patch.multiple(
        "custom_components.ttlock_ble.connection",
        RECONNECT_INITIAL_BACKOFF=0.01,
        RECONNECT_MAX_BACKOFF=0.05,
    ):
        await conn.async_start()
        first_task = conn._task
        await conn.async_start()
        assert conn._task is first_task
        await conn.async_stop()


async def test_async_stop_without_start_is_safe(hass, sample_virtual_key) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_stop()


async def test_maintain_loop_keeps_trying_when_device_missing(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    mock_ble_resolver.return_value = None
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with patch.multiple(
        "custom_components.ttlock_ble.connection",
        RECONNECT_INITIAL_BACKOFF=0.005,
        RECONNECT_MAX_BACKOFF=0.01,
    ):
        await conn.async_start()
        await asyncio.sleep(0.05)
        await conn.async_stop()
    # The resolver must have been hit multiple times by the maintain loop.
    assert mock_ble_resolver.call_count >= 2
    mock_active_scan.assert_not_awaited()


async def test_maintain_loop_logs_unexpected_error(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    # Make ensure_connected raise a non-CancelledError so the broad except branch runs.
    mock_ble_resolver.side_effect = [RuntimeError("kaboom"), None, None, None]
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with patch.multiple(
        "custom_components.ttlock_ble.connection",
        RECONNECT_INITIAL_BACKOFF=0.005,
        RECONNECT_MAX_BACKOFF=0.01,
    ):
        await conn.async_start()
        await asyncio.sleep(0.05)
        await conn.async_stop()
    assert mock_ble_resolver.call_count >= 1


async def test_query_state_reads_while_the_loop_is_cooling_down(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """A poll still reaches the lock while the maintain loop waits out a drop.

    Regression: the post-drop cooldown used to short-circuit every
    coordinator poll, and the loop re-armed it on each drop, so the
    configured `scan_interval` was silently never honoured.
    """
    mock_ttlock_client.query_state = AsyncMock(return_value=(0, 90))
    conn = TtlockBleConnection(
        hass,
        sample_virtual_key,
        reconnect_cooldown_seconds=30.0,
    )
    with patch.multiple(
        "custom_components.ttlock_ble.connection",
        RECONNECT_INITIAL_BACKOFF=0.005,
        RECONNECT_MAX_BACKOFF=0.01,
    ):
        await conn.async_start()
        for _ in range(20):
            await asyncio.sleep(0.005)
            if mock_ttlock_client.connect.await_count >= 1:
                break
        conn._on_disconnected(mock_ttlock_client)
        await asyncio.sleep(0.02)
        assert await conn.async_query_state() == (0, 90)
        await conn.async_stop()


async def test_on_disconnected_wakes_maintain_loop(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_query_state()
    # Simulate bleak's disconnected_callback firing.
    assert not conn._disconnected.is_set()
    conn._on_disconnected(mock_ttlock_client)
    assert conn._disconnected.is_set()


async def test_maintain_loop_waits_before_reconnecting_after_a_drop(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """After a disconnect the loop sits out the cooldown instead of retrying."""
    conn = TtlockBleConnection(
        hass,
        sample_virtual_key,
        reconnect_cooldown_seconds=30.0,
    )
    with patch.multiple(
        "custom_components.ttlock_ble.connection",
        RECONNECT_INITIAL_BACKOFF=0.005,
        RECONNECT_MAX_BACKOFF=0.01,
    ):
        await conn.async_start()
        # Let the loop connect and arm `_disconnected.wait()`.
        for _ in range(20):
            await asyncio.sleep(0.005)
            if mock_ttlock_client.connect.await_count >= 1:
                break
        connects_before_drop = mock_ttlock_client.connect.await_count
        mock_ttlock_client.is_connected = False
        conn._on_disconnected(mock_ttlock_client)
        await asyncio.sleep(0.05)
        assert mock_ttlock_client.connect.await_count == connects_before_drop
        await conn.async_stop()


async def test_zero_cooldown_reconnects_immediately_after_a_drop(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """A zero cooldown (permanent connection) skips the post-drop wait."""
    conn = TtlockBleConnection(
        hass,
        sample_virtual_key,
        reconnect_cooldown_seconds=0.0,
    )
    with patch.multiple(
        "custom_components.ttlock_ble.connection",
        RECONNECT_INITIAL_BACKOFF=0.005,
        RECONNECT_MAX_BACKOFF=0.01,
    ):
        await conn.async_start()
        for _ in range(20):
            await asyncio.sleep(0.005)
            if mock_ttlock_client.connect.await_count >= 1:
                break
        connects_before_drop = mock_ttlock_client.connect.await_count
        mock_ttlock_client.is_connected = False
        conn._on_disconnected(mock_ttlock_client)
        for _ in range(20):
            await asyncio.sleep(0.005)
            if mock_ttlock_client.connect.await_count > connects_before_drop:
                break
        assert mock_ttlock_client.connect.await_count > connects_before_drop
        await conn.async_stop()


async def test_connection_signal_fires_on_connect_and_disconnect(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """Successful connect emits True; tearing the session down emits False."""
    received: list[bool] = []
    async_dispatcher_connect(
        hass,
        connection_signal(sample_virtual_key.lockMac),
        received.append,
    )
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_query_state()
    await hass.async_block_till_done()
    assert received == [True]
    await conn.async_stop()
    await hass.async_block_till_done()
    assert received == [True, False]


async def test_connection_signal_not_emitted_when_connect_fails(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """If BLE connect raises, nothing is broadcast — state stayed `down`."""
    received: list[bool] = []
    async_dispatcher_connect(
        hass,
        connection_signal(sample_virtual_key.lockMac),
        received.append,
    )
    mock_ttlock_client.connect = AsyncMock(side_effect=TTLockError("ble fail"))
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_query_state()
    await hass.async_block_till_done()
    assert received == []


async def test_get_operation_log_returns_empty_when_device_missing(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """No reachable device means no client, so the log fetch yields nothing."""
    mock_ble_resolver.return_value = None
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_get_operation_log() == []
    mock_ttlock_client.get_operation_log.assert_not_called()


async def test_get_operation_log_returns_empty_on_ttlock_error(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """A TTLockError during the fetch is swallowed and returns an empty list."""
    mock_ttlock_client.get_operation_log = AsyncMock(
        side_effect=TTLockError("read log fail"),
    )
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_get_operation_log() == []


@pytest.mark.parametrize(
    "error",
    [
        ValueError(
            "The length of the provided data is not a multiple of the block length"
        ),
        RuntimeError("lock rejected checkUserTime"),
    ],
)
async def test_get_operation_log_returns_empty_on_unwrapped_error(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    error: Exception,
) -> None:
    """The SDK's log path raises outside TTLockError; the fetch still returns []."""
    mock_ttlock_client.get_operation_log = AsyncMock(side_effect=error)
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_get_operation_log() == []


async def test_get_operation_log_dispatches_only_new_records(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """Each record is dispatched once; subsequent fetches skip seen records."""
    received: list[object] = []
    async_dispatcher_connect(
        hass,
        log_signal(sample_virtual_key.lockMac),
        received.append,
    )
    first = [_log_entry(1), _log_entry(2)]
    mock_ttlock_client.get_operation_log = AsyncMock(return_value=first)
    conn = TtlockBleConnection(hass, sample_virtual_key)

    # The first successful fetch is the seeding pass: the backlog is history.
    new_entries = await conn.async_get_operation_log()
    await hass.async_block_till_done()
    assert new_entries == []
    assert received == []

    # A later fetch returning the same records plus a new one only emits the new.
    second = [_log_entry(1), _log_entry(2), _log_entry(3)]
    mock_ttlock_client.get_operation_log = AsyncMock(return_value=second)
    new_entries = await conn.async_get_operation_log()
    await hass.async_block_till_done()
    assert [e.record_number for e in new_entries] == [3]
    assert [e.record_number for e in received] == [3]


async def test_seeding_waits_for_a_fetch_that_reached_the_lock(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """A lock out of range at startup must not spend its seeding pass."""
    received: list[object] = []
    async_dispatcher_connect(
        hass,
        log_signal(sample_virtual_key.lockMac),
        received.append,
    )
    conn = TtlockBleConnection(hass, sample_virtual_key)

    # Out of range: no client, so nothing was seen and nothing was seeded.
    with patch(
        "custom_components.ttlock_ble.connection.async_ble_device_from_address",
        return_value=None,
    ):
        assert await conn.async_get_operation_log() == []

    # And the fetch that does reach the lock is still the seeding pass.
    backlog = [_log_entry(1), _log_entry(2), _log_entry(3)]
    mock_ttlock_client.get_operation_log = AsyncMock(return_value=backlog)
    assert await conn.async_get_operation_log() == []
    await hass.async_block_till_done()
    assert received == []


async def test_seeding_is_not_spent_by_a_failed_fetch(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """A fetch that raised did not observe the backlog, so it cannot seed."""
    received: list[object] = []
    async_dispatcher_connect(
        hass,
        log_signal(sample_virtual_key.lockMac),
        received.append,
    )
    conn = TtlockBleConnection(hass, sample_virtual_key)
    mock_ttlock_client.get_operation_log = AsyncMock(
        side_effect=TTLockError("read log fail"),
    )
    assert await conn.async_get_operation_log() == []

    mock_ttlock_client.get_operation_log = AsyncMock(return_value=[_log_entry(9)])
    assert await conn.async_get_operation_log() == []
    await hass.async_block_till_done()
    assert received == []


async def test_run_command_wraps_timeout_error(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """A bare `TimeoutError` from the SDK is converted to a `TTLockError`."""
    mock_ttlock_client.lock = AsyncMock(side_effect=TimeoutError)
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with pytest.raises(TTLockError, match="timed out responding to lock"):
        await conn.async_lock()
    mock_ttlock_client.disconnect.assert_awaited()


@pytest.mark.parametrize(
    "escape",
    [
        RuntimeError("checkUserTime FAILED: status=0x0 err=03"),
        ValueError("invalid padding"),
        OSError("le-connection-abort-by-local"),
    ],
)
async def test_run_command_wraps_non_ttlock_escapes(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    escape,
) -> None:
    """Exceptions the SDK does not wrap still reach callers as `TTLockError`."""
    mock_ttlock_client.unlock = AsyncMock(side_effect=escape)
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with pytest.raises(TTLockError, match="failed to unlock"):
        await conn.async_unlock()
    mock_ttlock_client.disconnect.assert_awaited()


async def test_drop_is_broadcast_immediately_and_only_once(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """The down edge must not wait for the reconnect cooldown to elapse."""
    from custom_components.ttlock_ble.connection import connection_signal

    received: list[bool] = []
    async_dispatcher_connect(
        hass,
        connection_signal(sample_virtual_key.lockMac),
        received.append,
    )
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_query_state()
    await hass.async_block_till_done()
    assert received == [True]

    # bleak reports the drop; the teardown that follows must not repeat it.
    mock_ttlock_client.is_connected = False
    conn._on_disconnected(mock_ttlock_client)
    await hass.async_block_till_done()
    assert received == [True, False]

    await conn.async_stop()
    await hass.async_block_till_done()
    assert received == [True, False]


async def test_get_operation_log_is_bounded(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """The fetch holds the same lock commands need, so it has to be bounded."""
    from custom_components.ttlock_ble.connection import MAX_LOG_ENTRIES_PER_FETCH

    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_get_operation_log()
    assert (
        mock_ttlock_client.get_operation_log.await_args.kwargs["max_entries"]
        == MAX_LOG_ENTRIES_PER_FETCH
    )


async def test_stopped_connection_refuses_to_reconnect(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """A late caller must not reopen the lock's single slot after unload."""
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_query_state()
    mock_ttlock_client.connect.reset_mock()
    await conn.async_stop()

    assert await conn.async_query_state() is None
    assert await conn.async_get_operation_log() == []
    with pytest.raises(TTLockError, match="not reachable"):
        await conn.async_lock()
    mock_ttlock_client.connect.assert_not_awaited()


async def test_run_command_lets_cancellation_through(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """Cancellation is not swallowed by the catch-all — callers must see it."""
    import asyncio

    mock_ttlock_client.lock = AsyncMock(side_effect=asyncio.CancelledError)
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with pytest.raises(asyncio.CancelledError):
        await conn.async_lock()
