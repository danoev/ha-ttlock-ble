"""Private reads retain bounded acquisition, preemption and owned cleanup."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from ttlock_ble import AdministrativeInventory, InventoryResult, TTLockError

from custom_components.ttlock_ble.connection import TtlockBleConnection


@pytest.mark.parametrize("method", ["keypad_pin", "fingerprint", "ic_card"])
@pytest.mark.parametrize("phase", ["acquisition", "read"])
async def test_private_inventory_yields_to_control_only_after_cleanup(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client, method, phase
):
    started = asyncio.Event()
    order = []

    async def wait(*_args):
        started.set()
        try:
            await asyncio.Future()
        finally:
            order.append("read_cancelled")

    async def connect():
        if phase == "acquisition" and mock_ttlock_client.connect.await_count == 1:
            await wait()

    async def disconnect():
        order.append("cleaned")

    async def unlock():
        order.append("unlock_once")

    mock_ttlock_client.connect.side_effect = connect
    mock_ttlock_client.get_administrative_inventory = AsyncMock(side_effect=wait)
    mock_ttlock_client.disconnect.side_effect = disconnect
    mock_ttlock_client.unlock.side_effect = unlock
    conn = TtlockBleConnection(hass, sample_virtual_key)
    task = asyncio.create_task(conn.async_get_administrative_inventory(method))
    await started.wait()
    await asyncio.wait_for(conn.async_unlock(), 1)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert order == ["read_cancelled", "cleaned", "unlock_once"]
    assert conn.inventory_diagnostics["cleanup_confirmed"] is True
    assert conn._inventory_task is None
    mock_ttlock_client.unlock.assert_awaited_once()


async def test_private_deadline_and_admin_rejection_no_replacement_leak(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
):
    async def read(_method):
        await asyncio.Event().wait()

    mock_ttlock_client.get_administrative_inventory = AsyncMock(side_effect=read)
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with (
        patch(
            "custom_components.ttlock_ble.connection.INVENTORY_OPERATION_TIMEOUT_SECONDS",
            0.001,
        ),
        pytest.raises(TTLockError, match="read_deadline_exceeded"),
    ):
        await conn.async_get_administrative_inventory("ic_card")
    assert conn._client is None
    sample_virtual_key.adminPs = ""
    with pytest.raises(TTLockError, match="admin key"):
        await conn.async_get_administrative_inventory("ic_card")
    sample_virtual_key.adminPs = "999999"
    result = AdministrativeInventory(
        InventoryResult((), complete=True, limitation=""), ()
    )
    mock_ttlock_client.get_administrative_inventory.side_effect = None
    mock_ttlock_client.get_administrative_inventory.return_value = result
    assert await conn.async_get_administrative_inventory("ic_card") is result
    assert mock_ttlock_client.connect.await_count == 2
    assert conn.inventory_diagnostics["cleanup_confirmed"] is True
