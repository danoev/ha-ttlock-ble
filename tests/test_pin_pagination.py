"""Run the real SDK PIN parser through HA's public response and cleanup path."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError
from ttlock_ble import TTLockClient, TTLockError

from custom_components.ttlock_ble.connection import TtlockBleConnection
from tests.test_services import _device_id


def _record() -> bytes:
    """Synthetic PIN record, using the S31's observed next cursor only."""
    code = b"8675309"
    item = bytes([0, 1, len(code)]) + code + bytes([len(code)]) + code
    return b"\x07\x01\x00\x00\x01\x50" + item + bytes([0, 1, 1, 0, 0])


def _sdk_reader(key, pages):
    sdk = TTLockClient(key, device=MagicMock())
    sdk._inventory_admin_handshake = AsyncMock()
    sdk._inventory_exchange = AsyncMock(side_effect=pages)
    return sdk.get_passcodes


@pytest.mark.parametrize(
    ("terminal", "complete", "predicate"),
    [
        (b"\x07\x01\x00\x00", True, True),
        (b"\x07\x01\x00\x01", False, False),
        (b"\x07\x01\x00", False, None),
        (b"\x07\x01\x00\x00\x00", False, None),
    ],
)
async def test_real_sdk_result_stays_partial_in_ha_and_owns_cleanup(
    hass,
    setup_integration,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
    terminal,
    complete,
    predicate,
    caplog,
) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    mock_ttlock_client.get_passcodes = _sdk_reader(
        sample_virtual_key, [_record(), terminal]
    )
    with patch(
        "custom_components.ttlock_ble.services._connection_for_device",
        return_value=conn,
    ):
        response = await hass.services.async_call(
            "ttlock_ble",
            "get_access_inventory",
            {"device_id": _device_id(hass), "method": "keypad_pin"},
            blocking=True,
            return_response=True,
        )
    assert response["count"] == 1
    assert response["validity_counts"] == {"permanent": 1}
    assert response["read_status"] == "partial"
    assert response["enumeration_complete"] is complete
    assert response["reason"] == (
        "incomplete_inventory" if complete else "truncated_response"
    )
    assert response["transport_interrupted"] is False
    metadata = response["response_metadata"]
    assert metadata["pin_short_end_marker_matches"] is predicate
    assert metadata["page"] == 1
    assert metadata["cursor"] == 336
    assert "8675309" not in json.dumps(response) + caplog.text
    assert "38363735333039" not in json.dumps(response) + caplog.text
    assert "raw" not in metadata
    assert "data" not in metadata
    assert response["connection_diagnostics"]["cleanup_confirmed"] is True
    assert conn._client is conn._pending_client is None
    mock_ttlock_client.disconnect.assert_awaited_once()
    mock_ttlock_client.lock.assert_not_awaited()
    mock_ttlock_client.unlock.assert_not_awaited()


async def test_unknown_initial_short_success_has_no_fabricated_zero_count(
    hass, setup_integration, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    mock_ttlock_client.get_passcodes = _sdk_reader(
        sample_virtual_key, [b"\x07\x01\x00\x01"]
    )
    with patch(
        "custom_components.ttlock_ble.services._connection_for_device",
        return_value=conn,
    ):
        response = await hass.services.async_call(
            "ttlock_ble",
            "get_access_inventory",
            {"device_id": _device_id(hass), "method": "keypad_pin"},
            blocking=True,
            return_response=True,
        )
    assert response["read_status"] == "unverified"
    assert response["reason"] == "truncated_response"
    assert "count" not in response
    assert "enumeration_complete" not in response
    assert response["response_metadata"]["pin_short_end_marker_matches"] is False
    assert response["connection_diagnostics"]["cleanup_confirmed"] is True


async def test_validated_end_cannot_hide_uncertain_cleanup_or_allow_replacement(
    hass, setup_integration, sample_virtual_key, mock_ble_resolver, mock_ttlock_client
) -> None:
    conn = TtlockBleConnection(hass, sample_virtual_key)
    mock_ttlock_client.get_passcodes = _sdk_reader(
        sample_virtual_key, [_record(), b"\x07\x01\x00\x00"]
    )
    mock_ttlock_client.disconnect.side_effect = BleakError("synthetic-private-cleanup")
    with patch(
        "custom_components.ttlock_ble.services._connection_for_device",
        return_value=conn,
    ):
        response = await hass.services.async_call(
            "ttlock_ble",
            "get_access_inventory",
            {"device_id": _device_id(hass), "method": "keypad_pin"},
            blocking=True,
            return_response=True,
        )
    assert response["count"] == 1
    assert response["enumeration_complete"] is True
    assert response["read_status"] == "partial"
    assert response["reason"] == "cleanup_uncertain"
    assert response["connection_diagnostics"]["cleanup_confirmed"] is False
    assert conn._pending_client is mock_ttlock_client
    with pytest.raises(TTLockError, match="not reachable"):
        await conn.async_unlock()
    assert mock_ttlock_client.connect.await_count == 1
    mock_ttlock_client.unlock.assert_not_awaited()
    mock_ttlock_client.disconnect.side_effect = None
    await conn.async_stop()
