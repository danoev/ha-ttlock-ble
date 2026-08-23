from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.bluetooth import (
    MONOTONIC_TIME,
    BluetoothReachabilityIntent,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from ttlock_ble import KeyboardPwdType, LockEvent, LockState, TTLockClient, TTLockError

from custom_components.ttlock_ble.client import ControlOutcomeUnknownError
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


def _scanner_device(
    device: object,
    *,
    source: str,
    rssi: int,
) -> SimpleNamespace:
    """Build the public HA per-scanner device shape used by the resolver."""
    return SimpleNamespace(
        scanner=SimpleNamespace(source=source),
        ble_device=device,
        advertisement=SimpleNamespace(rssi=rssi),
    )


def _fresh_service_info(
    device: object,
    *,
    source: str = "hci0",
    rssi: int = -70,
) -> SimpleNamespace:
    """Build a connectable HA callback received after acquisition started."""
    return SimpleNamespace(
        device=device,
        source=source,
        rssi=rssi,
        time=MONOTONIC_TIME() + 0.001,
    )


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
    caplog,
) -> None:
    """Management failures include HA's diagnosis but never the submitted PIN."""
    mock_ble_resolver.return_value = None
    diagnostic = "connectable scanner paths are full"
    secret_code = "583921"
    with (
        patch(
            "custom_components.ttlock_ble.connection."
            "async_address_reachability_diagnostics",
            return_value=diagnostic,
        ),
        caplog.at_level(logging.DEBUG, logger="custom_components.ttlock_ble"),
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
    assert secret_code not in caplog.text
    for credential in (
        sample_virtual_key.aesKeyStr,
        sample_virtual_key.unlockKey,
        sample_virtual_key.adminPs,
    ):
        assert credential not in str(error.value)
        assert credential not in caplog.text


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


async def test_unknown_state_query_reuses_immediate_connectable_device(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """Startup does not scan when HA already has a connectable route."""
    conn = TtlockBleConnection(hass, sample_virtual_key)

    assert await conn.async_query_state(active_scan=True) == (0, 80)

    mock_ble_resolver.assert_called_once_with(
        hass,
        sample_virtual_key.lockMac,
        connectable=True,
    )
    mock_active_scan.assert_not_awaited()
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.query_state.assert_awaited_once()


async def test_unknown_state_query_uses_exact_address_active_acquisition(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """An active-capable state query uses HA Auto-mode acquisition once."""
    mock_ble_resolver.return_value = None

    async def _advertise(_hass, callback, *_args) -> None:
        callback(_fresh_service_info(mock_ble_device))

    mock_active_scan.side_effect = _advertise
    conn = TtlockBleConnection(hass, sample_virtual_key)

    assert await conn.async_query_state(active_scan=True) == (0, 80)

    mock_active_scan.assert_awaited_once()
    scan_args = mock_active_scan.await_args.args
    assert scan_args[0] is hass
    assert scan_args[2]["address"] == sample_virtual_key.lockMac
    assert scan_args[2]["connectable"] is True
    assert scan_args[3].value == "active"
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.query_state.assert_awaited_once()


async def test_concurrent_unknown_state_queries_share_active_acquisition(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """The per-lock mutex prevents duplicate startup scans and GATT attempts."""
    mock_ble_resolver.return_value = None
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()

    async def _advertise(_hass, callback, *_args) -> None:
        scan_started.set()
        await release_scan.wait()
        callback(_fresh_service_info(mock_ble_device))

    mock_active_scan.side_effect = _advertise
    conn = TtlockBleConnection(hass, sample_virtual_key)
    first = asyncio.create_task(conn.async_query_state(active_scan=True))
    second = asyncio.create_task(conn.async_query_state(active_scan=True))
    await asyncio.wait_for(scan_started.wait(), timeout=1)
    release_scan.set()

    assert await asyncio.gather(first, second) == [(0, 80), (0, 80)]
    mock_active_scan.assert_awaited_once()
    mock_ttlock_client.connect.assert_awaited_once()
    assert mock_ttlock_client.query_state.await_count == 2


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


async def test_stale_cached_route_falls_back_to_one_fresh_active_candidate(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """A pre-command stale-route failure reacquires once and sends once."""
    from custom_components.ttlock_ble import connection as connection_module

    fresh_device = MagicMock(name="FreshBLEDevice")
    fresh_device.address = sample_virtual_key.lockMac
    stale_info = SimpleNamespace(
        device=mock_ble_device,
        source="hci0",
        rssi=-90,
        time=MONOTONIC_TIME() - 180,
    )
    fresh_info = _fresh_service_info(
        fresh_device,
        source="esp32-proxy-hall",
        rssi=-82,
    )
    fresh_client = MagicMock(name="FreshTTLockClient", is_connected=True)
    fresh_client.connect = AsyncMock(return_value=None)
    fresh_client.disconnect = AsyncMock(return_value=None)
    fresh_client.lock = AsyncMock(return_value=None)
    fresh_client.add_event_listener = MagicMock()
    mock_ttlock_client.connect.side_effect = TTLockError("device not found")
    connection_module.TtlockBleClient.from_ble_device.side_effect = [
        mock_ttlock_client,
        fresh_client,
    ]

    async def _advertise(_hass, callback, *_args) -> None:
        assert callback(stale_info) is False
        assert callback(fresh_info) is True

    mock_active_scan.side_effect = _advertise
    with patch(
        "custom_components.ttlock_ble.connection.async_last_service_info",
        return_value=stale_info,
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        await conn.async_lock()

    mock_active_scan.assert_awaited_once()
    assert connection_module.TtlockBleClient.from_ble_device.call_count == 2
    assert (
        connection_module.TtlockBleClient.from_ble_device.call_args_list[0].args[0]
        is mock_ble_device
    )
    assert (
        connection_module.TtlockBleClient.from_ble_device.call_args_list[1].args[0]
        is fresh_device
    )
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.disconnect.assert_awaited_once()
    mock_ttlock_client.lock.assert_not_awaited()
    fresh_client.connect.assert_awaited_once()
    fresh_client.lock.assert_awaited_once()


async def test_failed_fresh_candidate_does_not_repeat_active_acquisition(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """A stale route and its fresh replacement cannot create a retry loop."""
    from custom_components.ttlock_ble import connection as connection_module

    fresh_device = MagicMock(name="FreshBLEDevice")
    fresh_device.address = sample_virtual_key.lockMac
    fresh_client = MagicMock(name="FreshTTLockClient", is_connected=False)
    fresh_client.connect = AsyncMock(side_effect=TTLockError("still unreachable"))
    fresh_client.disconnect = AsyncMock(return_value=None)
    fresh_client.lock = AsyncMock(return_value=None)
    fresh_client.unlock = AsyncMock(return_value=None)
    mock_ttlock_client.connect.side_effect = TTLockError("stale route")
    connection_module.TtlockBleClient.from_ble_device.side_effect = [
        mock_ttlock_client,
        fresh_client,
    ]

    async def _advertise(_hass, callback, *_args) -> None:
        assert callback(_fresh_service_info(fresh_device)) is True

    mock_active_scan.side_effect = _advertise
    conn = TtlockBleConnection(hass, sample_virtual_key)

    with pytest.raises(TTLockError, match="not reachable"):
        await conn.async_unlock()

    mock_active_scan.assert_awaited_once()
    assert connection_module.TtlockBleClient.from_ble_device.call_count == 2
    mock_ttlock_client.connect.assert_awaited_once()
    fresh_client.connect.assert_awaited_once()
    mock_ttlock_client.unlock.assert_not_awaited()
    fresh_client.unlock.assert_not_awaited()


@pytest.mark.parametrize("source", ["hci0", "esp32-proxy-kitchen"])
async def test_cold_idle_uses_connectable_scanner_path_without_aggregate_history(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
    source: str,
) -> None:
    """A local or proxy scanner path can start GATT despite aggregate miss."""
    mock_ble_resolver.return_value = None
    scanner_path = _scanner_device(mock_ble_device, source=source, rssi=-82)
    with patch(
        "custom_components.ttlock_ble.connection.async_scanner_devices_by_address",
        return_value=[scanner_path],
    ) as scanner_resolver:
        conn = TtlockBleConnection(hass, sample_virtual_key)
        await conn.async_unlock()

    scanner_resolver.assert_called_once_with(
        hass,
        sample_virtual_key.lockMac,
        connectable=True,
    )
    mock_active_scan.assert_not_awaited()
    from custom_components.ttlock_ble import connection as connection_module

    connection_module.TtlockBleClient.from_ble_device.assert_called_once_with(
        mock_ble_device,
        sample_virtual_key,
        disconnected_callback=conn._on_disconnected,
    )
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.unlock.assert_awaited_once()


async def test_active_scan_accepts_delayed_connectable_scanner_path(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """An exact-address callback resolves a proxy-specific connectable path."""
    mock_ble_resolver.return_value = None
    scan_started = asyncio.Event()

    async def _advertise_after_start(_hass, callback, *_args) -> None:
        scan_started.set()
        callback(
            _fresh_service_info(
                mock_ble_device,
                source="esp32-proxy-hall",
                rssi=-78,
            )
        )

    mock_active_scan.side_effect = _advertise_after_start
    with patch(
        "custom_components.ttlock_ble.connection.async_scanner_devices_by_address",
        return_value=[],
    ) as scanner_resolver:
        conn = TtlockBleConnection(hass, sample_virtual_key)
        await conn.async_lock()

    assert scan_started.is_set()
    scanner_resolver.assert_called_once_with(
        hass,
        sample_virtual_key.lockMac,
        connectable=True,
    )
    scan_args = mock_active_scan.await_args.args
    assert scan_args[0] is hass
    assert scan_args[2]["address"] == sample_virtual_key.lockMac
    assert scan_args[2]["connectable"] is True
    assert scan_args[3].value == "active"
    assert scan_args[4] == EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.lock.assert_awaited_once()


async def test_sdk_connection_boundary_allows_transient_retry_success(
    sample_virtual_key,
    mock_ble_device,
) -> None:
    """Released SDK configures three connector attempts and accepts retry success."""
    sdk_client = TTLockClient.from_ble_device(mock_ble_device, sample_virtual_key)
    bleak_client = MagicMock(is_connected=True)
    bleak_client.start_notify = AsyncMock(return_value=None)
    notify_char = MagicMock()
    attempts = 0

    async def _establish(*_args, max_attempts: int, **_kwargs):
        nonlocal attempts
        while attempts < max_attempts:
            attempts += 1
            if attempts == 1:
                continue  # Simulated transient connection-layer failure.
            return bleak_client
        raise AssertionError

    async def _discover_chars() -> None:
        sdk_client._notify_char = notify_char

    with (
        patch("ttlock_ble.client.establish_connection", side_effect=_establish),
        patch.object(
            sdk_client,
            "_discover_chars",
            side_effect=_discover_chars,
        ),
        patch.object(sdk_client, "_wake_battery_read", new=AsyncMock()),
        patch("ttlock_ble.client.asyncio.sleep", new=AsyncMock()),
    ):
        await sdk_client.connect()

    assert attempts == 2
    assert sdk_client.is_connected


async def test_explicit_command_active_scan_uses_fresh_callback_device(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """A fresh HA callback supplies the route without rereading stale history."""
    mock_ble_resolver.return_value = None
    scan_started = asyncio.Event()

    async def _advertise_after_start(_hass, callback, *_args) -> None:
        scan_started.set()
        callback(_fresh_service_info(mock_ble_device))

    mock_active_scan.side_effect = _advertise_after_start

    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_lock()

    assert scan_started.is_set()
    mock_ble_resolver.assert_called_once_with(
        hass,
        sample_virtual_key.lockMac,
        connectable=True,
    )
    scan_args = mock_active_scan.await_args.args
    assert scan_args[0] is hass
    assert scan_args[2]["address"] == sample_virtual_key.lockMac
    assert scan_args[4] == EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.lock.assert_awaited_once()


async def test_explicit_acquisition_timeout_does_not_connect(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """The exact-address wait times out without a repeated GATT attempt."""
    mock_ble_resolver.return_value = None
    scan_started = asyncio.Event()

    async def _time_out_active_scan(*_args) -> None:
        scan_started.set()
        raise TimeoutError

    active_scan = AsyncMock(side_effect=_time_out_active_scan)
    with (
        patch(
            "custom_components.ttlock_ble.connection.async_process_advertisements",
            new=active_scan,
        ),
        patch(
            "custom_components.ttlock_ble.connection.EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS",
            0.015,
        ),
        patch(
            "custom_components.ttlock_ble.connection."
            "async_address_reachability_diagnostics",
            return_value="only in non-connectable history (no connectable path)",
        ),
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        with pytest.raises(TTLockError, match="non-connectable history"):
            await conn.async_lock()

    assert scan_started.is_set()
    active_scan.assert_awaited_once()
    assert mock_ble_resolver.call_count == 1
    mock_ttlock_client.connect.assert_not_awaited()
    mock_ttlock_client.lock.assert_not_awaited()


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

    async def _run_active_scan(*_args) -> None:
        scan_started.set()
        try:
            await asyncio.Future()
        finally:
            scan_cancelled.set()

    active_scan = AsyncMock(side_effect=_run_active_scan)
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


async def test_closing_race_after_cache_hit_does_not_open_late_connection(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """A cache hit racing unload is rejected before client construction."""
    conn = TtlockBleConnection(hass, sample_virtual_key)
    lookups = 0

    def _resolve(*_args, **_kwargs):
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            return None
        conn._closing = True
        conn._closing_event.set()
        return mock_ble_device

    mock_ble_resolver.side_effect = _resolve
    with pytest.raises(TTLockError, match="not reachable"):
        await conn.async_lock()

    mock_active_scan.assert_awaited_once()
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

    async def _run_active_scan(*_args) -> None:
        scan_started.set()
        try:
            await asyncio.Future()
        finally:
            scan_cancelled.set()

    active_scan = AsyncMock(side_effect=_run_active_scan)
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
    mock_ble_resolver.return_value = None
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()

    async def _run_active_scan(_hass, callback, *_args) -> None:
        scan_started.set()
        await release_scan.wait()
        callback(_fresh_service_info(mock_ble_device))

    active_scan = AsyncMock(side_effect=_run_active_scan)
    with (
        patch(
            "custom_components.ttlock_ble.connection.async_process_advertisements",
            new=active_scan,
        ),
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


async def test_explicit_command_reuses_background_connection_in_progress(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """The mutex lets explicit work reuse the background acquisition result."""
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()

    async def _connect() -> None:
        connect_started.set()
        await release_connect.wait()

    mock_ttlock_client.connect.side_effect = _connect
    conn = TtlockBleConnection(hass, sample_virtual_key)

    async def _background_acquire():
        async with conn._lock:
            return await conn._async_ensure_connected_locked(
                reason="background maintenance",
            )

    background = asyncio.create_task(_background_acquire())
    await asyncio.wait_for(connect_started.wait(), timeout=1)
    explicit = asyncio.create_task(conn.async_unlock())
    await asyncio.sleep(0)
    mock_ttlock_client.unlock.assert_not_awaited()

    release_connect.set()
    assert await background is mock_ttlock_client
    await explicit

    mock_ble_resolver.assert_called_once()
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.unlock.assert_awaited_once()
    mock_active_scan.assert_not_awaited()


async def test_background_connect_failure_remains_non_active(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """Background maintenance never escalates a failed cached route to Active."""
    mock_ttlock_client.connect.side_effect = TTLockError("stale background route")
    conn = TtlockBleConnection(hass, sample_virtual_key)

    async with conn._lock:
        result = await conn._async_ensure_connected_locked(
            reason="background maintenance",
        )

    assert result is None
    mock_ttlock_client.connect.assert_awaited_once()
    mock_active_scan.assert_not_awaited()


async def test_explicit_command_runs_after_failed_background_attempt(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """Queued user work gets the mutex next and may actively reacquire."""
    from custom_components.ttlock_ble import connection as connection_module

    background_started = asyncio.Event()
    release_background = asyncio.Event()
    background_error = TTLockError("stale background route")

    async def _background_connect() -> None:
        background_started.set()
        await release_background.wait()
        raise background_error

    background_client = mock_ttlock_client
    background_client.connect.side_effect = _background_connect
    explicit_stale_client = MagicMock(name="ExplicitStaleClient", is_connected=False)
    explicit_stale_client.connect = AsyncMock(side_effect=TTLockError("stale route"))
    explicit_stale_client.disconnect = AsyncMock(return_value=None)
    fresh_client = MagicMock(name="FreshExplicitClient", is_connected=True)
    fresh_client.connect = AsyncMock(return_value=None)
    fresh_client.disconnect = AsyncMock(return_value=None)
    fresh_client.unlock = AsyncMock(return_value=None)
    fresh_client.add_event_listener = MagicMock()
    connection_module.TtlockBleClient.from_ble_device.side_effect = [
        background_client,
        explicit_stale_client,
        fresh_client,
    ]

    async def _advertise(_hass, callback, *_args) -> None:
        assert callback(_fresh_service_info(mock_ble_device)) is True

    mock_active_scan.side_effect = _advertise
    conn = TtlockBleConnection(hass, sample_virtual_key)

    async def _background_acquire():
        async with conn._lock:
            return await conn._async_ensure_connected_locked(
                reason="background maintenance",
            )

    background = asyncio.create_task(_background_acquire())
    await asyncio.wait_for(background_started.wait(), timeout=1)
    explicit = asyncio.create_task(conn.async_unlock())
    await asyncio.sleep(0)
    explicit_stale_client.connect.assert_not_awaited()

    release_background.set()
    assert await background is None
    await explicit

    assert connection_module.TtlockBleClient.from_ble_device.call_count == 3
    mock_active_scan.assert_awaited_once()
    fresh_client.unlock.assert_awaited_once()


async def test_cancelling_during_gatt_connect_disconnects_partial_client(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """Cancellation during SDK acquisition cannot retain or finish a late link."""
    connect_started = asyncio.Event()

    async def _connect() -> None:
        connect_started.set()
        await asyncio.Future()

    mock_ttlock_client.connect.side_effect = _connect
    conn = TtlockBleConnection(hass, sample_virtual_key)
    operation = asyncio.create_task(conn.async_lock())
    await asyncio.wait_for(connect_started.wait(), timeout=1)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation

    mock_ttlock_client.disconnect.assert_awaited_once()
    mock_ttlock_client.lock.assert_not_awaited()
    assert conn._client is None


async def test_unload_during_gatt_connect_discards_late_connection(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """Unload/shutdown wins a connect race and closes the resulting client."""
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()

    async def _connect() -> None:
        connect_started.set()
        await release_connect.wait()

    mock_ttlock_client.connect.side_effect = _connect
    conn = TtlockBleConnection(hass, sample_virtual_key)
    operation = asyncio.create_task(conn.async_unlock())
    await asyncio.wait_for(connect_started.wait(), timeout=1)
    stopping = asyncio.create_task(conn.async_stop())
    await asyncio.sleep(0)
    release_connect.set()

    with pytest.raises(TTLockError, match="not reachable"):
        await operation
    await stopping

    mock_ttlock_client.disconnect.assert_awaited_once()
    mock_ttlock_client.unlock.assert_not_awaited()
    assert conn._client is None


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
    mock_ble_resolver.return_value = None

    async def _advertise(_hass, callback, *_args) -> None:
        callback(_fresh_service_info(mock_ble_device))

    mock_active_scan.side_effect = _advertise

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


async def test_ambiguous_unlock_is_reconciled_without_command_resend(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """Fresh matching state converts a lost acknowledgement to success."""
    mock_ttlock_client.unlock = AsyncMock(
        side_effect=ControlOutcomeUnknownError(
            "unlock",
            disconnected=False,
            routed_echoes=(0x14,),
            response_unclassified=False,
        )
    )
    mock_ttlock_client.query_state = AsyncMock(return_value=(LockState.UNLOCKED, 80))
    conn = TtlockBleConnection(hass, sample_virtual_key)

    await conn.async_unlock()

    mock_ttlock_client.unlock.assert_awaited_once()
    mock_ttlock_client.query_state.assert_awaited_once()
    mock_ttlock_client.disconnect.assert_not_awaited()
    mock_active_scan.assert_not_awaited()


async def test_ambiguous_control_with_contradictory_state_stays_unknown(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """A contradictory query cannot claim success and must never resend."""
    unknown = ControlOutcomeUnknownError(
        "unlock",
        disconnected=False,
        routed_echoes=(),
        response_unclassified=False,
    )
    mock_ttlock_client.unlock = AsyncMock(side_effect=unknown)
    mock_ttlock_client.query_state = AsyncMock(return_value=(LockState.LOCKED, 80))
    conn = TtlockBleConnection(hass, sample_virtual_key)

    with pytest.raises(ControlOutcomeUnknownError):
        await conn.async_unlock()

    mock_ttlock_client.unlock.assert_awaited_once()
    mock_ttlock_client.query_state.assert_awaited_once()
    mock_ttlock_client.disconnect.assert_awaited_once()


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


async def test_background_acquisition_does_not_use_explicit_scanner_fallback(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """RC5 does not make background maintenance more radio-aggressive."""
    mock_ble_resolver.return_value = None
    scanner_path = _scanner_device(mock_ble_device, source="hci0", rssi=-72)
    with patch(
        "custom_components.ttlock_ble.connection.async_scanner_devices_by_address",
        return_value=[scanner_path],
    ) as scanner_resolver:
        conn = TtlockBleConnection(hass, sample_virtual_key)
        async with conn._lock:
            client = await conn._async_ensure_connected_locked(
                reason="background maintenance",
            )

    assert client is None
    scanner_resolver.assert_not_called()
    mock_ttlock_client.connect.assert_not_awaited()


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
    mock_active_scan,
) -> None:
    """No reachable device means no client, so the log fetch yields nothing."""
    mock_ble_resolver.return_value = None
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_get_operation_log() == []
    mock_ttlock_client.get_operation_log.assert_not_called()
    mock_active_scan.assert_not_awaited()


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


async def test_full_initial_log_pages_are_all_seeded_without_events(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """Pagination cannot turn later backlog pages into live operations."""
    from custom_components.ttlock_ble.connection import MAX_LOG_ENTRIES_PER_FETCH

    received: list[object] = []
    async_dispatcher_connect(
        hass,
        log_signal(sample_virtual_key.lockMac),
        received.append,
    )
    full_page = [
        _log_entry(number) for number in range(1, MAX_LOG_ENTRIES_PER_FETCH + 1)
    ]
    final_history_page = [_log_entry(number) for number in range(26, 31)]
    live_page = [_log_entry(31)]
    mock_ttlock_client.get_operation_log = AsyncMock(
        side_effect=[full_page, final_history_page, live_page]
    )
    conn = TtlockBleConnection(hass, sample_virtual_key)

    assert await conn.async_get_operation_log() == []
    assert await conn.async_get_operation_log() == []
    assert await conn.async_get_operation_log() == live_page
    await hass.async_block_till_done()

    assert [entry.record_number for entry in received] == [31]


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
