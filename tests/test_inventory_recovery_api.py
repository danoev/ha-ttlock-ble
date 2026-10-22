"""A failed synthetic BLE read retains private metadata through the real HA API."""

from unittest.mock import AsyncMock

from ttlock_ble import (
    AdministrativeInventory,
    AdministrativeRecord,
    InventoryEntry,
    InventoryResult,
)

from custom_components.ttlock_ble.access_records import AccessRecordStore
from custom_components.ttlock_ble.management_http import URL
from tests.test_management_http import headers, request_data


async def test_read_failure_state_reload_and_recovery_preserve_pin_label(
    hass, setup_integration, mock_ttlock_connection, hass_client, caplog
):
    client = await hass_client()
    result = AdministrativeInventory(
        InventoryResult(
            (InventoryEntry("permanent"),),
            complete=True,
            limitation="Limited PIN coverage",
        ),
        (
            AdministrativeRecord(
                "keypad_pin", "pin_type_1", "permanent", b"synthetic-pin"
            ),
        ),
    )
    conn = mock_ttlock_connection
    conn.async_get_administrative_inventory = AsyncMock(return_value=result)
    conn.inventory_diagnostics = {"cleanup_confirmed": True}
    body = request_data(setup_integration, "read", method="keypad_pin")
    response = await client.post(URL, json=body, headers=headers(client))
    data = await response.json()
    ref = data["records"][0]["reference"]
    response = await client.post(
        URL,
        json={**body, "action": "name", "reference": ref, "name": "Stored label"},
        headers=headers(client),
    )
    assert response.status == 200
    conn.async_get_administrative_inventory.side_effect = OSError(
        "synthetic-private-transport"
    )
    response = await client.post(URL, json=body, headers=headers(client))
    assert response.status == 503
    assert "synthetic-private-transport" not in await response.text() + caplog.text
    response = await client.post(
        URL, json={**body, "action": "state"}, headers=headers(client)
    )
    data = await response.json()
    assert not data["fresh"]
    assert data["records"][0]["name"] == "Stored label"
    assert data["records"][0]["observation"] == "previously_observed_unverified"
    restarted = AccessRecordStore(hass, setup_integration.entry_id)
    await restarted.async_load()
    setup_integration.runtime_data.access_records = restarted
    conn.async_get_administrative_inventory.side_effect = None
    response = await client.post(URL, json=body, headers=headers(client))
    data = await response.json()
    assert data["fresh"]
    assert data["inventory"]["enumeration_complete"]
    assert data["inventory"]["read_status"] == "partial"
    assert len(data["records"]) == 1
    assert data["records"][0]["reference"] == ref
    assert data["records"][0]["name"] == "Stored label"
    conn.async_add_passcode.assert_not_awaited()
