"""Inventory must yield to physical control and relinquish its BLE session."""

from __future__ import annotations

import asyncio
import logging
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bleak.exc import BleakError
from ttlock_ble import InventoryEntry, InventoryReason, TTLockError

from custom_components.ttlock_ble.connection import TtlockBleConnection


@pytest.mark.parametrize("phase", ["acquisition", "read"])
async def test_explicit_control_preempts_inventory_read_after_cleanup(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client, phase
) -> None:
    started = asyncio.Event()
    order = []

    async def read():
        started.set()
        try:
            await asyncio.Future()
        finally:
            order.append("cancel_read")

    async def disconnect():
        order.append("disconnect")

    async def unlock():
        order.append("unlock")

    mock_ttlock_client.get_cards = AsyncMock(side_effect=read)
    if phase == "acquisition":

        async def connect():
            if mock_ttlock_client.connect.await_count == 1:
                await read()

        mock_ttlock_client.connect.side_effect = connect
    mock_ttlock_client.disconnect.side_effect = disconnect
    mock_ttlock_client.unlock.side_effect = unlock
    conn = TtlockBleConnection(hass, sample_virtual_key)
    inventory = asyncio.create_task(conn.async_get_access_inventory("ic_card"))
    try:
        await started.wait()
        await asyncio.wait_for(conn.async_unlock(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await inventory
        assert order == ["cancel_read", "disconnect", "unlock"]
        assert conn.inventory_diagnostics["reason"] == "inventory_preempted"
        assert conn.inventory_diagnostics["cleanup_confirmed"] is True
        mock_ttlock_client.unlock.assert_awaited_once()
    finally:
        inventory.cancel()
        await asyncio.gather(inventory, return_exceptions=True)


async def test_partial_transport_result_does_not_retain_a_healthy_session(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
) -> None:
    result = SimpleNamespace(
        entries=(InventoryEntry("permanent"),),
        complete=False,
        limitation="inventory read interrupted",
        skipped_unknown=0,
        transport_interrupted=True,
        reason=InventoryReason.TRANSPORT,
    )
    mock_ttlock_client.get_cards = AsyncMock(return_value=result)
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_get_access_inventory("ic_card") is result
    mock_ttlock_client.disconnect.assert_awaited_once()
    assert conn._client is None
    assert conn.inventory_diagnostics["reason"] == "transport_interrupted"
    assert conn.inventory_diagnostics["cleanup_confirmed"] is True


async def test_inventory_is_rejected_while_control_is_in_progress(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def unlock():
        started.set()
        await release.wait()

    mock_ttlock_client.unlock.side_effect = unlock
    conn = TtlockBleConnection(hass, sample_virtual_key)
    control = asyncio.create_task(conn.async_unlock())
    try:
        await started.wait()
        with pytest.raises(TTLockError, match="connection_busy"):
            await conn.async_get_access_inventory("ic_card")
    finally:
        release.set()
        await control
    mock_ttlock_client.unlock.assert_awaited_once()


async def test_inventory_timeout_closes_session_and_suppresses_raw_error(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
) -> None:
    mock_ttlock_client.get_cards = AsyncMock(side_effect=asyncio.Event().wait)
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with (
        patch(
            "custom_components.ttlock_ble.connection.INVENTORY_OPERATION_TIMEOUT_SECONDS",
            0.01,
        ),
        pytest.raises(TTLockError, match="read_deadline_exceeded"),
    ):
        await conn.async_get_access_inventory("ic_card")
    mock_ttlock_client.disconnect.assert_awaited_once()
    assert conn._client is None
    assert conn._pending_client is None


async def test_repeated_cancellation_keeps_cleanup_owned_before_unlock(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
) -> None:
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def read():
        started.set()
        await asyncio.Future()

    async def disconnect():
        cleanup_started.set()
        await release_cleanup.wait()

    mock_ttlock_client.get_cards = AsyncMock(side_effect=read)
    mock_ttlock_client.disconnect.side_effect = disconnect
    conn = TtlockBleConnection(hass, sample_virtual_key)
    inventory = asyncio.create_task(conn.async_get_access_inventory("ic_card"))
    await started.wait()
    inventory.cancel()
    await cleanup_started.wait()
    control = asyncio.create_task(conn.async_unlock())
    await asyncio.sleep(0)
    inventory.cancel()
    assert conn._pending_client is mock_ttlock_client
    assert conn._lock.locked()
    mock_ttlock_client.unlock.assert_not_awaited()
    release_cleanup.set()
    await asyncio.wait_for(control, timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await inventory
    mock_ttlock_client.disconnect.assert_awaited_once()
    mock_ttlock_client.unlock.assert_awaited_once()
    assert conn.inventory_diagnostics["reason"] == "inventory_cancelled"
    assert conn.inventory_diagnostics["cleanup_confirmed"] is True


async def test_interrupted_read_retains_failed_cleanup_and_blocks_replacement(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    result = SimpleNamespace(
        transport_interrupted=True, reason=InventoryReason.TRANSPORT
    )
    mock_ttlock_client.get_cards = AsyncMock(return_value=result)
    mock_ttlock_client.disconnect.side_effect = BleakError("synthetic-private-cleanup")
    conn = TtlockBleConnection(hass, sample_virtual_key)
    assert await conn.async_get_access_inventory("ic_card") is result
    assert conn._client is None
    assert conn._pending_client is mock_ttlock_client
    with pytest.raises(TTLockError, match="not reachable"):
        await conn.async_unlock()
    mock_ttlock_client.unlock.assert_not_awaited()
    assert mock_ttlock_client.connect.await_count == 1
    mock_ttlock_client.disconnect.side_effect = None
    await conn.async_unlock()
    mock_ttlock_client.unlock.assert_awaited_once()
    assert mock_ttlock_client.connect.await_count == 2
    assert "synthetic-private-cleanup" not in caplog.text


async def test_shutdown_cancels_inventory_and_waits_for_cleanup(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
) -> None:
    started = asyncio.Event()

    async def read():
        started.set()
        await asyncio.Future()

    mock_ttlock_client.get_cards = AsyncMock(side_effect=read)
    conn = TtlockBleConnection(hass, sample_virtual_key)
    inventory = asyncio.create_task(conn.async_get_access_inventory("ic_card"))
    await started.wait()
    await asyncio.wait_for(conn.async_stop(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await inventory
    mock_ttlock_client.disconnect.assert_awaited_once()
    assert conn._pending_client is None


async def test_inventory_failure_does_not_render_raw_traceback(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client, caplog
) -> None:
    mock_ttlock_client.get_cards = AsyncMock(
        side_effect=OSError("synthetic-secret-frame")
    )
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with pytest.raises(TTLockError) as error:
        await conn.async_get_access_inventory("ic_card")
    assert "synthetic-secret-frame" not in "".join(
        traceback.format_exception(error.value)
    )
    assert "synthetic-secret-frame" not in caplog.text
    mock_ttlock_client.disconnect.assert_awaited_once()
