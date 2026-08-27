from __future__ import annotations

import asyncio
import datetime as dt
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError
from homeassistant.components.bluetooth import (
    MONOTONIC_TIME,
    BluetoothReachabilityIntent,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from ttlock_ble import KeyboardPwdType, LockEvent, LockState, TTLockClient, TTLockError

from custom_components.ttlock_ble.client import (
    ControlAbortedForShutdownError,
    ControlOutcomeUnknownError,
    ControlStage,
    TtlockBleClient,
)
from custom_components.ttlock_ble.connection import (
    AMBIGUOUS_RECONCILIATION_ACQUISITION_TIMEOUT_SECONDS,
    CACHED_CONNECT_TIMEOUT_SECONDS,
    EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS,
    EXPLICIT_CONTROL_ACQUISITION_TIMEOUT_SECONDS,
    FRESH_CONNECT_TIMEOUT_SECONDS,
    MANAGEMENT_ACQUISITION_TIMEOUT_SECONDS,
    ROBUST_CONNECT_ATTEMPTS,
    ROUTINE_QUERY_ACQUISITION_TIMEOUT_SECONDS,
    SPECULATIVE_CACHED_CONNECT_ATTEMPTS,
    UNKNOWN_BOOTSTRAP_ACQUISITION_TIMEOUT_SECONDS,
    TtlockBleConnection,
    _acquisition_budget,
    _AcquisitionIntent,
    connection_signal,
    event_signal,
    log_signal,
)

OLD_LOG_DATE = dt.datetime(2020, 1, 1)  # noqa: DTZ001 -- lock RTC is naive
NEW_LOG_DATE = dt.datetime(2099, 1, 1)  # noqa: DTZ001 -- lock RTC is naive


def _log_entry(
    record_number: int,
    *,
    operate_date: dt.datetime | None = OLD_LOG_DATE,
) -> SimpleNamespace:
    """Build a minimal LogEntry stand-in keyed by `record_number`."""
    return SimpleNamespace(
        record_number=record_number,
        record_type=1,
        operate_date=operate_date,
        uid=None,
        record_id=None,
        key_id=None,
    )


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


class _BlockedSdkConnect:
    """Event-controlled model of one blocked stage inside SDK connect()."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.slot = asyncio.Semaphore(1)
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.observed_task: asyncio.Task | None = None
        self.notify_char = MagicMock(name="NotifyCharacteristic")
        self.backend = MagicMock(name="BleakBackend", is_connected=True)
        self.backend.start_notify = AsyncMock(side_effect=self.start_notify)
        self.backend.stop_notify = AsyncMock(return_value=None)
        self.backend.disconnect = AsyncMock(side_effect=self.disconnect_backend)

    async def block(self) -> None:
        self.observed_task = asyncio.current_task()
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.cancelled.set()

    async def establish(self, *_args, **_kwargs):
        await self.slot.acquire()
        if self.stage == "connector":
            try:
                await self.block()
            finally:
                self.slot.release()
        return self.backend

    async def discover(self, client) -> None:
        if self.stage == "service_discovery":
            await self.block()
        client._notify_char = self.notify_char

    async def start_notify(self, *_args) -> None:
        if self.stage == "notification_setup":
            await self.block()

    async def settle(self, _delay: float) -> None:
        if self.stage == "post_notify_settle":
            await self.block()

    async def wake_battery(self, _client) -> None:
        if self.stage == "battery_wake_read":
            await self.block()

    async def disconnect_backend(self) -> None:
        if self.slot.locked():
            self.slot.release()


def test_event_signal_lowercases_mac() -> None:
    assert event_signal("AA:BB:CC:DD:EE:FF") == "ttlock_ble_event_aa:bb:cc:dd:ee:ff"


def test_connection_signal_lowercases_mac() -> None:
    assert (
        connection_signal("AA:BB:CC:DD:EE:FF")
        == "ttlock_ble_connection_aa:bb:cc:dd:ee:ff"
    )


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        (
            _AcquisitionIntent.EXPLICIT_CONTROL,
            EXPLICIT_CONTROL_ACQUISITION_TIMEOUT_SECONDS,
        ),
        (
            _AcquisitionIntent.MANAGEMENT,
            MANAGEMENT_ACQUISITION_TIMEOUT_SECONDS,
        ),
        (
            _AcquisitionIntent.ROUTINE_QUERY,
            ROUTINE_QUERY_ACQUISITION_TIMEOUT_SECONDS,
        ),
        (
            _AcquisitionIntent.UNKNOWN_BOOTSTRAP,
            UNKNOWN_BOOTSTRAP_ACQUISITION_TIMEOUT_SECONDS,
        ),
        (
            _AcquisitionIntent.AMBIGUOUS_RECONCILIATION,
            AMBIGUOUS_RECONCILIATION_ACQUISITION_TIMEOUT_SECONDS,
        ),
        (
            _AcquisitionIntent.OPERATION_LOG,
            ROUTINE_QUERY_ACQUISITION_TIMEOUT_SECONDS,
        ),
        (
            _AcquisitionIntent.BACKGROUND_MAINTENANCE,
            ROUTINE_QUERY_ACQUISITION_TIMEOUT_SECONDS,
        ),
    ],
)
def test_acquisition_intents_have_explicit_validation_budgets(
    intent,
    expected,
) -> None:
    """Acquisition policy is typed and never inferred from diagnostic text."""
    assert _acquisition_budget(intent) == expected


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


@pytest.mark.parametrize(
    "error",
    [
        BleakError("adapter gone"),
        RuntimeError("transport failed"),
        ValueError("bad frame"),
    ],
)
async def test_query_state_contains_unwrapped_sdk_errors(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    error,
) -> None:
    """Transport/decrypt errors obey the state-or-None query contract."""
    mock_ttlock_client.query_state.side_effect = error
    conn = TtlockBleConnection(hass, sample_virtual_key)

    assert await conn.async_query_state() is None
    assert conn.is_connected is False
    mock_ttlock_client.disconnect.assert_awaited_once()


async def test_query_state_lets_cancellation_through(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """Unload cancellation must not be converted into an ordinary failed read."""
    mock_ttlock_client.query_state.side_effect = asyncio.CancelledError
    conn = TtlockBleConnection(hass, sample_virtual_key)

    with pytest.raises(asyncio.CancelledError):
        await conn.async_query_state()


async def test_query_state_contains_unwrapped_connection_setup_error(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """A raw notification/GATT setup failure cannot escape a state query."""
    mock_ttlock_client.connect.side_effect = BleakError("adapter gone")
    conn = TtlockBleConnection(hass, sample_virtual_key)

    assert await conn.async_query_state() is None
    mock_ttlock_client.disconnect.assert_awaited_once()


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
    mock_ble_resolver.learned_advertising_interval.return_value = 2.0
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
    mock_ble_resolver.clear_advertisement_history.assert_called_once_with(
        hass,
        sample_virtual_key.lockMac,
    )
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
    mock_ble_resolver.clear_advertisement_history.assert_called_once_with(
        hass,
        sample_virtual_key.lockMac,
    )
    assert connection_module.TtlockBleClient.from_ble_device.call_count == 2
    assert (
        connection_module.TtlockBleClient.from_ble_device.call_args_list[0].args[0]
        is mock_ble_device
    )
    assert (
        connection_module.TtlockBleClient.from_ble_device.call_args_list[0].kwargs[
            "connect_attempts"
        ]
        == SPECULATIVE_CACHED_CONNECT_ATTEMPTS
    )
    assert (
        connection_module.TtlockBleClient.from_ble_device.call_args_list[1].args[0]
        is fresh_device
    )
    assert (
        connection_module.TtlockBleClient.from_ble_device.call_args_list[1].kwargs[
            "connect_attempts"
        ]
        == ROBUST_CONNECT_ATTEMPTS
    )
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.disconnect.assert_awaited_once()
    mock_ttlock_client.lock.assert_not_awaited()
    fresh_client.connect.assert_awaited_once()
    fresh_client.lock.assert_awaited_once()


async def test_learned_stale_cached_route_scans_before_gatt(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """A route well beyond HA's learned cadence does not burn GATT retries."""
    from custom_components.ttlock_ble import connection as connection_module

    stale_info = SimpleNamespace(
        device=mock_ble_device,
        source="hci0",
        rssi=-83,
        time=MONOTONIC_TIME() - 156,
    )
    fresh_device = MagicMock(name="FreshBLEDevice")
    fresh_device.address = sample_virtual_key.lockMac
    mock_ble_resolver.learned_advertising_interval.return_value = 2.0

    async def _advertise(_hass, callback, *_args) -> None:
        assert callback(_fresh_service_info(fresh_device, rssi=-82)) is True

    mock_active_scan.side_effect = _advertise
    with patch(
        "custom_components.ttlock_ble.connection.async_last_service_info",
        return_value=stale_info,
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        await conn.async_lock()

    mock_active_scan.assert_awaited_once()
    connection_module.TtlockBleClient.from_ble_device.assert_called_once_with(
        fresh_device,
        sample_virtual_key,
        disconnected_callback=conn._on_disconnected,
        connect_attempts=ROBUST_CONNECT_ATTEMPTS,
    )
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.lock.assert_awaited_once()


async def test_active_acquisition_accepts_recent_history(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """A usable recent callback is not hidden by a scan-start timestamp gate."""
    mock_ble_resolver.return_value = None
    recent_info = SimpleNamespace(
        device=mock_ble_device,
        source="hci0",
        rssi=-83,
        time=MONOTONIC_TIME() - 9,
    )

    async def _advertise(_hass, callback, *_args) -> None:
        assert callback(recent_info) is True

    mock_active_scan.side_effect = _advertise
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_unlock()

    mock_ble_resolver.clear_advertisement_history.assert_called_once_with(
        hass,
        sample_virtual_key.lockMac,
    )
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.unlock.assert_awaited_once()


async def test_static_advertisement_is_observed_after_history_clear(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """Dedup history is cleared before waiting for the next identical packet."""
    mock_ble_resolver.return_value = None

    async def _advertise(_hass, callback, *_args) -> None:
        mock_ble_resolver.clear_advertisement_history.assert_called_once_with(
            hass,
            sample_virtual_key.lockMac,
        )
        assert callback(_fresh_service_info(mock_ble_device)) is True

    mock_active_scan.side_effect = _advertise
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_lock()

    mock_active_scan.assert_awaited_once()
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.lock.assert_awaited_once()


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


async def test_cached_deadline_awaits_cleanup_before_one_fresh_fallback(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """A timed-out stale route cannot overlap its fresh HA acquisition."""
    from custom_components.ttlock_ble import connection as connection_module

    connect_cancelled = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def _stale_connect() -> None:
        try:
            await asyncio.Future()
        finally:
            connect_cancelled.set()

    async def _stale_disconnect() -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished.set()

    stale_client = mock_ttlock_client
    stale_client.connect.side_effect = _stale_connect
    stale_client.disconnect.side_effect = _stale_disconnect
    fresh_client = MagicMock(name="FreshClient", is_connected=True)
    fresh_client.connect = AsyncMock(return_value=None)
    fresh_client.disconnect = AsyncMock(return_value=None)
    fresh_client.lock = AsyncMock(return_value=None)
    fresh_client.add_event_listener = MagicMock()
    connection_module.TtlockBleClient.from_ble_device.side_effect = [
        stale_client,
        fresh_client,
    ]

    async def _fresh_advertisement(_hass, callback, *_args) -> None:
        assert cleanup_finished.is_set()
        assert callback(_fresh_service_info(mock_ble_device)) is True

    mock_active_scan.side_effect = _fresh_advertisement
    with patch(
        "custom_components.ttlock_ble.connection.CACHED_CONNECT_TIMEOUT_SECONDS",
        0.02,
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        operation = asyncio.create_task(conn.async_lock())
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        mock_active_scan.assert_not_awaited()
        release_cleanup.set()
        await operation

    assert connect_cancelled.is_set()
    assert cleanup_finished.is_set()
    stale_client.disconnect.assert_awaited_once()
    mock_active_scan.assert_awaited_once()
    fresh_client.connect.assert_awaited_once()
    fresh_client.lock.assert_awaited_once()
    assert connection_module.TtlockBleClient.from_ble_device.call_count == 2


async def test_closing_after_cached_cleanup_prevents_active_fallback(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """Shutdown winning after cached cleanup starts no new scan or GATT route."""
    mock_ttlock_client.connect.side_effect = TTLockError("cached route failed")
    conn = TtlockBleConnection(hass, sample_virtual_key)

    async def _disconnect_and_close() -> None:
        conn._closing = True
        conn._closing_event.set()

    mock_ttlock_client.disconnect.side_effect = _disconnect_and_close

    with pytest.raises(TTLockError, match="not reachable"):
        await conn.async_lock()

    mock_active_scan.assert_not_awaited()
    mock_ble_resolver.clear_advertisement_history.assert_not_called()
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.lock.assert_not_awaited()


async def test_cached_timeout_cleanup_failure_stops_before_fresh_fallback(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """A teardown error cannot permit a possibly live cached route to overlap."""

    async def _stale_connect() -> None:
        await asyncio.Future()

    mock_ttlock_client.connect.side_effect = _stale_connect
    mock_ttlock_client.disconnect.side_effect = BleakError("disconnect failed")
    with patch(
        "custom_components.ttlock_ble.connection.CACHED_CONNECT_TIMEOUT_SECONDS",
        0.02,
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        with pytest.raises(TTLockError, match="not reachable"):
            await conn.async_lock()

    mock_ttlock_client.disconnect.assert_awaited_once()
    mock_active_scan.assert_not_awaited()
    mock_ttlock_client.lock.assert_not_awaited()


async def test_failed_candidate_teardown_blocks_later_acquisition_until_clean(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """A failed candidate stays owned across services until teardown succeeds."""
    from custom_components.ttlock_ble import connection as connection_module

    stale_client = mock_ttlock_client
    stale_client.connect.side_effect = TTLockError("stale route")
    stale_client.disconnect.side_effect = [
        BleakError("disconnect failed once"),
        BleakError("disconnect failed twice"),
        None,
    ]
    fresh_client = MagicMock(name="FreshClient", is_connected=True)
    fresh_client.connect = AsyncMock(return_value=None)
    fresh_client.disconnect = AsyncMock(return_value=None)
    fresh_client.lock = AsyncMock(return_value=None)
    fresh_client.add_event_listener = MagicMock()
    connection_module.TtlockBleClient.from_ble_device.side_effect = [
        stale_client,
        fresh_client,
    ]
    conn = TtlockBleConnection(hass, sample_virtual_key)

    with pytest.raises(TTLockError, match="not reachable"):
        await conn.async_lock()
    assert conn._pending_client is stale_client
    assert connection_module.TtlockBleClient.from_ble_device.call_count == 1
    mock_active_scan.assert_not_awaited()

    with pytest.raises(TTLockError, match="not reachable"):
        await conn.async_lock()
    assert conn._pending_client is stale_client
    assert connection_module.TtlockBleClient.from_ble_device.call_count == 1
    mock_active_scan.assert_not_awaited()

    await conn.async_lock()

    assert conn._pending_client is None
    assert connection_module.TtlockBleClient.from_ble_device.call_count == 2
    stale_client.disconnect.assert_awaited()
    assert stale_client.disconnect.await_count == 3
    fresh_client.connect.assert_awaited_once()
    fresh_client.lock.assert_awaited_once()


async def test_installed_disconnect_failure_retains_ownership_and_broadcast(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """Failed installed teardown cannot publish down or permit a replacement."""
    from custom_components.ttlock_ble import connection as connection_module

    states: list[bool] = []
    async_dispatcher_connect(
        hass,
        connection_signal(sample_virtual_key.lockMac),
        states.append,
    )
    installed = MagicMock(name="InstalledClient", is_connected=True)
    installed.query_state = AsyncMock(side_effect=BleakError("query failed"))
    installed.disconnect = AsyncMock(
        side_effect=[
            BleakError("disconnect failed once"),
            BleakError("disconnect failed twice"),
            None,
        ]
    )
    installed.remove_event_listener = MagicMock()
    conn = TtlockBleConnection(hass, sample_virtual_key)
    conn._client = installed
    conn._broadcast_connection_state(connected=True)

    assert await conn.async_query_state() is None
    assert conn._client is None
    assert conn._pending_client is installed
    assert conn.is_connected is True
    assert states == [True]

    assert await conn.async_query_state() is None
    assert conn._pending_client is installed
    assert connection_module.TtlockBleClient.from_ble_device.call_count == 0
    assert states == [True]

    assert await conn.async_query_state() == (0, 80)
    assert conn._pending_client is None
    assert connection_module.TtlockBleClient.from_ble_device.call_count == 1
    assert states == [True, False, True]


async def test_failed_disconnect_reports_down_only_after_backend_disconnect(
    hass,
    sample_virtual_key,
) -> None:
    """A teardown exception is not itself proof of a disconnected BLE link."""
    states: list[bool] = []
    async_dispatcher_connect(
        hass,
        connection_signal(sample_virtual_key.lockMac),
        states.append,
    )
    installed = MagicMock(name="InstalledClient", is_connected=True)
    installed.disconnect = AsyncMock(side_effect=BleakError("still connected"))
    installed.remove_event_listener = MagicMock()
    conn = TtlockBleConnection(hass, sample_virtual_key)
    conn._client = installed
    conn._broadcast_connection_state(connected=True)

    async with conn._lock:
        assert await conn._async_disconnect_locked() is False

    assert conn._pending_client is installed
    assert states == [True]

    installed.is_connected = False
    conn._on_disconnected(MagicMock())
    async with conn._lock:
        assert (
            await conn._async_retry_pending_cleanup_locked(
                reason="test confirmed disconnect"
            )
            is True
        )

    assert conn._pending_client is None
    assert states == [True, False]


async def test_fresh_route_deadline_terminates_without_second_acquisition(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """A newly observed route times out once and cannot restart the scan loop."""
    mock_ble_resolver.return_value = None
    connect_cancelled = asyncio.Event()

    async def _fresh_connect() -> None:
        try:
            await asyncio.Future()
        finally:
            connect_cancelled.set()

    async def _fresh_advertisement(_hass, callback, *_args) -> None:
        assert callback(_fresh_service_info(mock_ble_device)) is True

    mock_ttlock_client.connect.side_effect = _fresh_connect
    mock_active_scan.side_effect = _fresh_advertisement
    with patch(
        "custom_components.ttlock_ble.connection.FRESH_CONNECT_TIMEOUT_SECONDS",
        0.02,
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        with pytest.raises(TTLockError, match="not reachable"):
            await conn.async_lock()

    assert connect_cancelled.is_set()
    mock_active_scan.assert_awaited_once()
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.disconnect.assert_awaited_once()
    mock_ttlock_client.lock.assert_not_awaited()


async def test_one_absolute_deadline_clips_cached_scan_and_fresh_phases(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """Nominal 16+25+42 phases share one 60-second control deadline."""
    from custom_components.ttlock_ble import connection as connection_module

    clock = [0.0]
    connect_windows: list[float] = []
    scan_windows: list[float] = []
    fresh_client = MagicMock(name="FreshClient", is_connected=False)
    connection_module.TtlockBleClient.from_ble_device.side_effect = [
        mock_ttlock_client,
        fresh_client,
    ]

    async def _time_out_connect(_client, *, phase_timeout, **_kwargs):
        connect_windows.append(phase_timeout)
        clock[0] += phase_timeout
        conn._pending_client = None
        return SimpleNamespace(
            acquisition_elapsed=phase_timeout,
            cleanup=SimpleNamespace(elapsed=0.0, succeeded=True),
        )

    conn = TtlockBleConnection(hass, sample_virtual_key)

    async def _active_acquisition(*, deadline, reason):
        scan_window = min(
            EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS,
            deadline - clock[0],
        )
        scan_windows.append(scan_window)
        clock[0] += scan_window
        return conn._resolve_connection_candidate(
            include_scanner_paths=True,
            reason=reason,
        )

    with (
        patch(
            "custom_components.ttlock_ble.connection.monotonic",
            side_effect=lambda: clock[0],
        ),
        patch.object(
            conn,
            "_async_connect_before_deadline",
            new=AsyncMock(side_effect=_time_out_connect),
        ),
        patch.object(
            conn,
            "_async_wait_for_connectable_device_locked",
            new=AsyncMock(side_effect=_active_acquisition),
        ),
        pytest.raises(TTLockError, match="not reachable"),
    ):
        await conn.async_lock()

    assert connect_windows == [
        CACHED_CONNECT_TIMEOUT_SECONDS,
        EXPLICIT_CONTROL_ACQUISITION_TIMEOUT_SECONDS
        - CACHED_CONNECT_TIMEOUT_SECONDS
        - EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS,
    ]
    assert scan_windows == [EXPLICIT_CONNECT_SCAN_TIMEOUT_SECONDS]
    assert connect_windows[1] < FRESH_CONNECT_TIMEOUT_SECONDS
    assert sum(connect_windows) + sum(scan_windows) == (
        EXPLICIT_CONTROL_ACQUISITION_TIMEOUT_SECONDS
    )
    assert connection_module.TtlockBleClient.from_ble_device.call_count == 2


async def test_active_scan_window_is_clipped_to_remaining_absolute_deadline(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_active_scan,
) -> None:
    """HA's whole-second scan timeout never exceeds the intent remainder."""
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with patch(
        "custom_components.ttlock_ble.connection.monotonic",
        return_value=100.0,
    ):
        assert (
            await conn._async_wait_for_connectable_device_locked(
                deadline=110.9,
                reason="deadline test",
            )
            is None
        )

    assert mock_active_scan.await_args.args[4] == 10


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
        connect_attempts=SPECULATIVE_CACHED_CONNECT_ATTEMPTS,
    )
    mock_ttlock_client.connect.assert_awaited_once()
    mock_ttlock_client.unlock.assert_awaited_once()


@pytest.mark.parametrize("source", ["hci0", "esp32-proxy-hall"])
async def test_active_scan_accepts_delayed_connectable_scanner_path(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
    source: str,
) -> None:
    """An exact-address callback resolves a local or proxy connectable path."""
    mock_ble_resolver.return_value = None
    scan_started = asyncio.Event()

    async def _advertise_after_start(_hass, callback, *_args) -> None:
        scan_started.set()
        callback(
            _fresh_service_info(
                mock_ble_device,
                source=source,
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


@pytest.mark.parametrize(
    ("active_scan", "nominal_attempts", "literal_attempts"),
    [
        (True, SPECULATIVE_CACHED_CONNECT_ATTEMPTS, 5),
        (False, ROBUST_CONNECT_ATTEMPTS, 7),
    ],
)
async def test_wall_clock_deadline_bounds_connector_transient_attempts(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
    caplog,
    *,
    active_scan: bool,
    nominal_attempts: int,
    literal_attempts: int,
) -> None:
    """Connector transient accounting may exceed max_attempts; time still wins."""
    from custom_components.ttlock_ble import connection as connection_module

    attempts = 0
    connect_finished = asyncio.Event()

    async def _connector_with_independent_transient_budget() -> None:
        nonlocal attempts
        try:
            for _ in range(literal_attempts):
                attempts += 1
                await asyncio.sleep(0)
            await asyncio.Future()
        finally:
            connect_finished.set()

    mock_ttlock_client.connect.side_effect = (
        _connector_with_independent_transient_budget
    )
    with (
        patch(
            "custom_components.ttlock_ble.connection.CACHED_CONNECT_TIMEOUT_SECONDS",
            0.02,
        ),
        caplog.at_level(logging.DEBUG, logger="custom_components.ttlock_ble"),
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        if active_scan:
            with pytest.raises(TTLockError, match="not reachable"):
                await conn.async_lock()
        else:
            assert await conn.async_query_state() is None

    assert attempts == literal_attempts
    assert attempts > nominal_attempts
    assert connect_finished.is_set()
    assert (
        connection_module.TtlockBleClient.from_ble_device.call_args_list[0].kwargs[
            "connect_attempts"
        ]
        == nominal_attempts
    )
    mock_ttlock_client.disconnect.assert_awaited_once()
    mock_ttlock_client.query_state.assert_not_awaited()
    mock_ttlock_client.lock.assert_not_awaited()
    if active_scan:
        mock_active_scan.assert_awaited_once()
    else:
        mock_active_scan.assert_not_awaited()
    for credential in (
        sample_virtual_key.aesKeyStr,
        sample_virtual_key.unlockKey,
        sample_virtual_key.adminPs,
    ):
        assert credential not in caplog.text


@pytest.mark.parametrize(
    "blocked_stage",
    [
        "connector",
        "service_discovery",
        "notification_setup",
        "post_notify_settle",
        "battery_wake_read",
    ],
)
async def test_connect_stage_deadline_cancels_before_auth_and_releases_slot(
    hass,
    sample_virtual_key,
    mock_ble_device,
    mock_ble_resolver,
    mock_active_scan,
    blocked_stage: str,
) -> None:
    """Every SDK connect stage stays on the cancellable acquisition side."""
    from custom_components.ttlock_ble.client import TtlockBleClient

    scenario = _BlockedSdkConnect(blocked_stage)
    created_clients: list[TtlockBleClient] = []
    original_factory = TtlockBleClient.from_ble_device

    def _factory(*args, **kwargs) -> TtlockBleClient:
        client = original_factory(*args, **kwargs)
        created_clients.append(client)
        return client

    async def _discover(client: TtlockBleClient) -> None:
        await scenario.discover(client)

    async def _wake_battery(client: TtlockBleClient) -> None:
        await scenario.wake_battery(client)

    auth = AsyncMock(name="check_user_time")
    control = AsyncMock(name="lock_control")
    with (
        patch(
            "custom_components.ttlock_ble.connection.CACHED_CONNECT_TIMEOUT_SECONDS",
            0.02,
        ),
        patch(
            "custom_components.ttlock_ble.connection.TtlockBleClient.from_ble_device",
            side_effect=_factory,
        ),
        patch(
            "ttlock_ble.client.establish_connection",
            side_effect=scenario.establish,
        ),
        patch.object(TtlockBleClient, "_discover_chars", new=_discover),
        patch.object(
            TtlockBleClient,
            "_wake_battery_read",
            new=_wake_battery,
        ),
        patch("ttlock_ble.client.asyncio.sleep", new=scenario.settle),
        patch.object(TtlockBleClient, "_check_user_time", new=auth),
        patch.object(TtlockBleClient, "lock", new=control),
    ):
        conn = TtlockBleConnection(hass, sample_virtual_key)
        operation = asyncio.create_task(conn.async_lock())
        await asyncio.wait_for(scenario.started.wait(), timeout=1)
        with pytest.raises(TTLockError, match="not reachable"):
            await operation

    assert scenario.cancelled.is_set()
    assert scenario.observed_task is operation
    assert len(created_clients) == 1
    assert created_clients[0]._client is None
    assert created_clients[0]._ha_control_frame_written is False
    auth.assert_not_awaited()
    control.assert_not_awaited()
    mock_active_scan.assert_awaited_once()
    await asyncio.wait_for(scenario.slot.acquire(), timeout=0.1)
    scenario.slot.release()


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
            1,
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
    mock_ble_resolver.clear_advertisement_history.assert_called_once_with(
        hass,
        sample_virtual_key.lockMac,
    )
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
                intent=_AcquisitionIntent.BACKGROUND_MAINTENANCE,
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
            intent=_AcquisitionIntent.BACKGROUND_MAINTENANCE,
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
                intent=_AcquisitionIntent.BACKGROUND_MAINTENANCE,
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
    mock_active_scan,
) -> None:
    """Cancellation during SDK acquisition cannot retain or finish a late link."""
    connect_started = asyncio.Event()
    connect_cancelled = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def _connect() -> None:
        connect_started.set()
        try:
            await asyncio.Future()
        finally:
            connect_cancelled.set()

    async def _disconnect() -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished.set()

    mock_ttlock_client.connect.side_effect = _connect
    mock_ttlock_client.disconnect.side_effect = _disconnect
    conn = TtlockBleConnection(hass, sample_virtual_key)
    operation = asyncio.create_task(conn.async_lock())
    await asyncio.wait_for(connect_started.wait(), timeout=1)
    operation.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    assert connect_cancelled.is_set()
    assert not operation.done()
    mock_active_scan.assert_not_awaited()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert cleanup_finished.is_set()
    mock_ttlock_client.disconnect.assert_awaited_once()
    mock_ttlock_client.lock.assert_not_awaited()
    mock_active_scan.assert_not_awaited()
    assert conn._client is None


async def test_second_cancellation_cannot_orphan_candidate_cleanup(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """Repeated cancellation waits for owned teardown before propagating."""
    from custom_components.ttlock_ble import connection as connection_module

    connect_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def _connect() -> None:
        connect_started.set()
        await asyncio.Future()

    async def _disconnect() -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    stale_client = mock_ttlock_client
    stale_client.connect.side_effect = _connect
    stale_client.disconnect.side_effect = _disconnect
    fresh_client = MagicMock(name="FreshClient", is_connected=True)
    fresh_client.connect = AsyncMock(return_value=None)
    fresh_client.disconnect = AsyncMock(return_value=None)
    fresh_client.lock = AsyncMock(return_value=None)
    fresh_client.add_event_listener = MagicMock()
    connection_module.TtlockBleClient.from_ble_device.side_effect = [
        stale_client,
        fresh_client,
    ]
    conn = TtlockBleConnection(hass, sample_virtual_key)
    operation = asyncio.create_task(conn.async_lock())
    await asyncio.wait_for(connect_started.wait(), timeout=1)

    operation.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    operation.cancel()
    operation.cancel()
    await asyncio.sleep(0)

    assert operation.done() is False
    assert conn._pending_client is stale_client
    assert conn._cleanup_task is not None
    mock_active_scan.assert_not_awaited()

    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert conn._pending_client is None
    assert conn._cleanup_task is None
    assert connection_module.TtlockBleClient.from_ble_device.call_count == 1
    mock_active_scan.assert_not_awaited()

    await conn.async_lock()
    assert connection_module.TtlockBleClient.from_ble_device.call_count == 2
    fresh_client.lock.assert_awaited_once()


async def test_maintenance_timeout_cleanup_survives_async_stop_cancellation(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    mock_active_scan,
) -> None:
    """Unload waits for maintenance teardown even after cancelling its caller."""
    connect_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def _connect() -> None:
        connect_started.set()
        await asyncio.Future()

    async def _disconnect() -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    mock_ttlock_client.connect.side_effect = _connect
    mock_ttlock_client.disconnect.side_effect = _disconnect
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with patch(
        "custom_components.ttlock_ble.connection.CACHED_CONNECT_TIMEOUT_SECONDS",
        0.02,
    ):
        await conn.async_start()
        await asyncio.wait_for(connect_started.wait(), timeout=1)
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        stopping = asyncio.create_task(conn.async_stop())
        await asyncio.sleep(0)

        assert stopping.done() is False
        assert conn._pending_client is mock_ttlock_client
        assert conn._cleanup_task is not None
        mock_active_scan.assert_not_awaited()

        release_cleanup.set()
        await asyncio.wait_for(stopping, timeout=1)

    assert conn._pending_client is None
    assert conn._cleanup_task is None
    assert conn._task is None
    mock_ttlock_client.disconnect.assert_awaited_once()


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


async def test_shutdown_before_live_command_authentication_aborts_control(
    hass,
    sample_virtual_key,
) -> None:
    """A command queued after closing cannot authenticate on a retained live link."""
    conn = TtlockBleConnection(hass, sample_virtual_key)
    client = TtlockBleClient(
        sample_virtual_key,
        device=MagicMock(),
    )
    client.set_control_allowed(lambda: not conn._closing_event.is_set())
    client._client = MagicMock(is_connected=True)
    client.disconnect = AsyncMock(return_value=None)
    client._restart_keep_alive = MagicMock()
    client._check_user_time = AsyncMock(return_value=123)
    client._control_lock = AsyncMock()
    conn._client = client
    conn._broadcast_connection_state(connected=True)
    conn._closing = True
    conn._closing_event.set()

    with pytest.raises(TTLockError, match="not reachable"):
        await conn.async_lock()

    client._check_user_time.assert_not_awaited()
    client._control_lock.assert_not_awaited()
    assert client._ha_control_frame_written is False
    await conn.async_stop()


async def test_shutdown_during_live_authentication_aborts_before_control(
    hass,
    sample_virtual_key,
) -> None:
    """Unload waits for auth to unwind, then the pre-control gate aborts safely."""
    conn = TtlockBleConnection(hass, sample_virtual_key)
    auth_started = asyncio.Event()
    release_auth = asyncio.Event()
    client = TtlockBleClient(
        sample_virtual_key,
        device=MagicMock(),
    )
    client.set_control_allowed(lambda: not conn._closing_event.is_set())
    client._client = MagicMock(is_connected=True)
    client.disconnect = AsyncMock(return_value=None)

    async def _authenticate() -> int:
        auth_started.set()
        await release_auth.wait()
        return 123

    client._check_user_time = AsyncMock(side_effect=_authenticate)
    client._control_lock = AsyncMock()
    conn._client = client
    conn._broadcast_connection_state(connected=True)
    operation = asyncio.create_task(conn.async_lock())
    await asyncio.wait_for(auth_started.wait(), timeout=1)

    stopping = asyncio.create_task(conn.async_stop())
    await asyncio.wait_for(conn._closing_event.wait(), timeout=1)
    assert stopping.done() is False
    release_auth.set()

    with pytest.raises(ControlAbortedForShutdownError, match="before control"):
        await operation
    await asyncio.wait_for(stopping, timeout=1)

    client._control_lock.assert_not_awaited()
    assert client._ha_control_frame_written is False
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize(
    ("action", "ambiguous", "expected_state"),
    [
        ("lock", False, LockState.LOCKED),
        ("unlock", True, LockState.UNLOCKED),
    ],
)
async def test_shutdown_after_control_begins_waits_without_resend(
    hass,
    sample_virtual_key,
    *,
    action: str,
    ambiguous: bool,
    expected_state: LockState,
) -> None:
    """Unload cannot cancel or duplicate control after its ambiguity boundary."""
    conn = TtlockBleConnection(hass, sample_virtual_key)
    control_started = asyncio.Event()
    release_control = asyncio.Event()
    client = TtlockBleClient(
        sample_virtual_key,
        device=MagicMock(),
    )
    client.set_control_allowed(lambda: not conn._closing_event.is_set())
    client._client = MagicMock(is_connected=True)
    client.disconnect = AsyncMock(return_value=None)
    client._restart_keep_alive = MagicMock()
    client.query_state = AsyncMock(return_value=(expected_state, 80))
    client._check_user_time = AsyncMock(return_value=123)

    async def _control_once(*_args) -> None:
        client._ha_control_stage = ControlStage.CONTROL_FRAME_WRITTEN
        client._ha_control_frame_written = True
        control_started.set()
        await release_control.wait()
        if ambiguous:
            message = "acknowledgement lost"
            raise TTLockError(message)

    client._control_lock = AsyncMock(side_effect=_control_once)
    conn._client = client
    conn._broadcast_connection_state(connected=True)
    operation = asyncio.create_task(getattr(conn, f"async_{action}")())
    await asyncio.wait_for(control_started.wait(), timeout=1)

    stopping = asyncio.create_task(conn.async_stop())
    await asyncio.wait_for(conn._closing_event.wait(), timeout=1)
    assert stopping.done() is False
    release_control.set()

    await operation
    await asyncio.wait_for(stopping, timeout=1)

    client._control_lock.assert_awaited_once()
    if ambiguous:
        client.query_state.assert_awaited_once()
    else:
        client.query_state.assert_not_awaited()
    client.disconnect.assert_awaited_once()


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


async def test_disconnect_failure_retains_connected_client(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    mock_ttlock_client.disconnect = AsyncMock(side_effect=RuntimeError("boom"))
    conn = TtlockBleConnection(hass, sample_virtual_key)
    await conn.async_query_state()
    await conn.async_stop()
    assert conn.is_connected is True
    assert conn._client is None
    assert conn._pending_client is mock_ttlock_client


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


async def test_async_start_can_leave_default_connection_on_demand(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """Normal RC10 mode has no keep-warm reconnect task."""
    conn = TtlockBleConnection(hass, sample_virtual_key)

    await conn.async_start(maintain=False)

    assert conn._task is None
    mock_ble_resolver.assert_not_called()
    mock_ttlock_client.connect.assert_not_awaited()


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
                intent=_AcquisitionIntent.BACKGROUND_MAINTENANCE,
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
    second = [
        _log_entry(1),
        _log_entry(2),
        _log_entry(3, operate_date=NEW_LOG_DATE),
    ]
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
    live_page = [_log_entry(31, operate_date=NEW_LOG_DATE)]
    mock_ttlock_client.get_operation_log = AsyncMock(
        side_effect=[full_page, final_history_page, live_page]
    )
    conn = TtlockBleConnection(hass, sample_virtual_key)

    assert await conn.async_get_operation_log() == []
    assert await conn.async_get_operation_log() == []
    assert await conn.async_get_operation_log() == live_page
    await hass.async_block_till_done()

    assert [entry.record_number for entry in received] == [31]


async def test_short_page_then_later_historical_full_page_stays_suppressed(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
) -> None:
    """A misleading short page cannot make later old firmware history live."""
    from custom_components.ttlock_ble.connection import MAX_LOG_ENTRIES_PER_FETCH

    received: list[object] = []
    async_dispatcher_connect(
        hass,
        log_signal(sample_virtual_key.lockMac),
        received.append,
    )
    first_short = [_log_entry(1), _log_entry(2)]
    later_history = [
        _log_entry(number) for number in range(3, MAX_LOG_ENTRIES_PER_FETCH + 3)
    ]
    new_record = _log_entry(28, operate_date=NEW_LOG_DATE)
    mock_ttlock_client.get_operation_log = AsyncMock(
        side_effect=[first_short, later_history, [new_record]],
    )
    conn = TtlockBleConnection(hass, sample_virtual_key)

    assert await conn.async_get_operation_log() == []
    assert await conn.async_get_operation_log() == []
    assert await conn.async_get_operation_log() == [new_record]
    await hass.async_block_till_done()

    assert [entry.record_number for entry in received] == [28]


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
