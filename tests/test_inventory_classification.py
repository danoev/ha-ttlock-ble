"""Failure stages preserve counts, cancellation and source-attribution limits."""

from __future__ import annotations

import logging

import pytest
from ttlock_ble import (
    InventoryDiagnostic,
    InventoryEntry,
    InventoryError,
    InventoryReason,
    InventoryResult,
)

from custom_components.ttlock_ble.access import AccessFailure, AccessInventoryError
from custom_components.ttlock_ble.connection import TtlockBleConnection
from tests.test_services import _device_id


@pytest.mark.parametrize(
    ("scan_error", "reason"),
    [
        (None, AccessFailure.NO_CANDIDATE),
        (TimeoutError(), AccessFailure.DISCOVERY_TIMEOUT),
    ],
)
async def test_discovery_classification_from_real_acquisition_path(
    hass, sample_virtual_key, mock_ble_resolver, mock_active_scan, scan_error, reason
) -> None:
    mock_ble_resolver.return_value = None
    mock_active_scan.side_effect = scan_error
    conn = TtlockBleConnection(hass, sample_virtual_key)
    with pytest.raises(AccessInventoryError) as error:
        await conn.async_get_access_inventory("ic_card")
    assert error.value.reason == reason
    assert error.value.stage == "discovery"
    assert conn.inventory_diagnostics["cleanup_confirmed"] is True


async def test_gatt_failure_is_distinct_and_does_not_claim_backend_source(
    hass, sample_virtual_key, mock_ble_resolver, mock_ttlock_client, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    conn = TtlockBleConnection(hass, sample_virtual_key)
    mock_ttlock_client.connect.side_effect = RuntimeError("synthetic-private")
    with pytest.raises(AccessInventoryError) as error:
        await conn.async_get_access_inventory("ic_card")
    assert error.value.reason == AccessFailure.GATT
    assert error.value.stage == "gatt"
    assert conn.inventory_diagnostics["gatt_backend_source"] == "unverified"
    assert conn.inventory_diagnostics["advertisement_source"] == "hci0"
    assert "synthetic-private" not in str(error.value) + caplog.text


@pytest.mark.parametrize(
    ("reason", "stage"),
    [
        (InventoryReason.ADMIN_REJECTED, "admin"),
        (InventoryReason.ADMIN_TIMEOUT, "admin"),
        (InventoryReason.REJECTED, "inventory"),
        (InventoryReason.LAYOUT, "inventory"),
    ],
)
async def test_sdk_classification_reaches_public_service_without_count(
    hass, setup_integration, mock_ttlock_connection, reason, stage
) -> None:
    mock_ttlock_connection.async_get_access_inventory.side_effect = InventoryError(
        reason, stage
    )
    response = await hass.services.async_call(
        "ttlock_ble",
        "get_access_inventory",
        {"device_id": _device_id(hass), "method": "keypad_pin"},
        blocking=True,
        return_response=True,
    )
    assert response["reason"] == reason.value
    assert response["stage"] == stage
    assert "count" not in response


async def test_partial_pin_shape_and_uncertain_cleanup_are_visible(
    hass, setup_integration, mock_ttlock_connection
) -> None:
    mock_ttlock_connection.async_get_access_inventory.return_value = InventoryResult(
        (InventoryEntry("permanent"),),
        complete=False,
        limitation="PIN response unrecognized",
        reason=InventoryReason.TRUNCATED,
        diagnostic=InventoryDiagnostic(
            "keypad_pin", 1, 5, 3, 7, 1, InventoryReason.TRUNCATED
        ),
    )
    mock_ttlock_connection.inventory_diagnostics = {
        "reason": "cleanup_uncertain",
        "stage": "cleanup",
        "cleanup_confirmed": False,
        "gatt_backend_source": "unverified",
        "elapsed_ms": 25,
    }
    response = await hass.services.async_call(
        "ttlock_ble",
        "get_access_inventory",
        {"device_id": _device_id(hass), "method": "keypad_pin"},
        blocking=True,
        return_response=True,
    )
    assert response["count"] == 1
    assert response["read_status"] == "partial"
    assert response["reason"] == "cleanup_uncertain"
    assert response["response_metadata"]["category"] == "truncated_response"
    assert response["enumeration_complete"] is False
    assert response["connection_diagnostics"]["cleanup_confirmed"] is False


async def test_busy_response_does_not_reuse_previous_attempt_cleanup(
    hass, setup_integration, mock_ttlock_connection
) -> None:
    mock_ttlock_connection.inventory_diagnostics = {
        "reason": "cleanup_uncertain",
        "stage": "cleanup",
    }
    mock_ttlock_connection.async_get_access_inventory.side_effect = (
        AccessInventoryError(AccessFailure.BUSY, "queue")
    )
    response = await hass.services.async_call(
        "ttlock_ble",
        "get_access_inventory",
        {"device_id": _device_id(hass), "method": "ic_card"},
        blocking=True,
        return_response=True,
    )
    assert response["reason"] == "connection_busy"
    assert "connection_diagnostics" not in response
