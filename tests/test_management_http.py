"""Real HA HTTP middleware, admin permissions, request privacy and no write route."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.auth.models import (
    TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
    TOKEN_TYPE_SYSTEM,
)
from homeassistant.core import EVENT_CALL_SERVICE
from pytest_homeassistant_custom_component.common import MockUser
from ttlock_ble import (
    AdministrativeInventory,
    AdministrativeRecord,
    InventoryEntry,
    InventoryResult,
)

from custom_components.ttlock_ble.management_http import (
    URL,
    AdministrationView,
    async_setup_management,
)
from tests.test_services import _device_id


def headers(client):
    return {
        "Origin": str(client.make_url("/")).rstrip("/"),
        "X-TTLock-Admin": "1",
        "Sec-Fetch-Site": "same-origin",
    }


def request_data(entry, action="state", **kwargs):
    return {
        "action": action,
        "entry_id": entry.entry_id,
        "lock": "AA:BB:CC:DD:EE:FF",
        "method": "ic_card",
        **kwargs,
    }


async def test_admin_private_records_names_reset_no_service_bus_or_raw_log(
    hass, setup_integration, mock_ttlock_connection, hass_client, caplog
):
    caplog.set_level(logging.DEBUG)
    client = await hass_client()
    events = []
    hass.bus.async_listen(EVENT_CALL_SERVICE, events.append)
    result = AdministrativeInventory(
        InventoryResult((InventoryEntry("permanent"),), complete=True, limitation=""),
        (
            AdministrativeRecord(
                "ic_card", "card_4byte", "permanent", b"synthetic-card-uid"
            ),
        ),
    )
    mock_ttlock_connection.async_get_administrative_inventory = AsyncMock(
        return_value=result
    )
    mock_ttlock_connection.inventory_diagnostics = {"cleanup_confirmed": True}
    response = await client.post(URL, json={"action": "list"}, headers=headers(client))
    assert response.status == 200
    assert (await response.json())["locks"][0]["name"] == "Front door"
    response = await client.post(
        URL, json=request_data(setup_integration, "read"), headers=headers(client)
    )
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = await response.json()
    assert data["fresh"]
    assert data["new_writes"] == "disabled"
    assert "synthetic-card-uid" not in json.dumps(data) + caplog.text
    assert b"synthetic-card-uid".hex() not in json.dumps(data) + caplog.text
    assert data["inventory"]["enumeration_complete"]
    record = data["records"][0]
    assert set(record) == {
        "reference",
        "name",
        "record_type",
        "validity",
        "schedule_verified",
        "identity_verified",
        "present",
        "stale",
        "alias_confirmed_this_session",
    }
    response = await client.post(
        URL,
        json=request_data(
            setup_integration,
            "name",
            reference=record["reference"],
            name="Spare <script>",
        ),
        headers=headers(client),
    )
    assert response.status == 200
    assert (await response.json())["records"][0]["name"] == "Spare <script>"
    response = await client.post(
        URL,
        json=request_data(setup_integration, "reset", confirm="forget_local_labels"),
        headers=headers(client),
    )
    assert not (await response.json())["records"]
    assert not events
    mock_ttlock_connection.async_add_passcode.assert_not_awaited()
    mock_ttlock_connection.async_delete_passcode.assert_not_awaited()
    mock_ttlock_connection.async_clear_passcodes.assert_not_awaited()


@pytest.mark.parametrize("user_kind", ["ordinary", "system", "long_lived", "inactive"])
async def test_unauthorized_token_contexts_cannot_start_ble(
    hass, setup_integration, hass_client, mock_ttlock_connection, user_kind
):
    user = MockUser(
        is_owner=user_kind != "ordinary", system_generated=user_kind == "system"
    ).add_to_hass(hass)
    token_type = {
        "system": TOKEN_TYPE_SYSTEM,
        "long_lived": TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
    }.get(user_kind, "normal")
    refresh = await hass.auth.async_create_refresh_token(
        user,
        token_type=token_type,
        client_id=None if user_kind == "system" else "http://test/",
        client_name="test" if user_kind == "long_lived" else None,
    )
    token = hass.auth.async_create_access_token(refresh)
    if user_kind == "inactive":
        user.is_active = False
    client = await hass_client(token)
    response = await client.post(
        URL, json=request_data(setup_integration, "read"), headers=headers(client)
    )
    assert response.status in {401, 403}
    mock_ttlock_connection.async_get_access_inventory.assert_not_awaited()


async def test_unauthenticated_get_signed_url_and_csrf_denied(
    hass, setup_integration, hass_client, hass_client_no_auth
):
    client = await hass_client_no_auth()
    assert (
        await client.post(URL, json={"action": "list"}, headers=headers(client))
    ).status == 401
    client = await hass_client()
    assert (await client.get(URL, headers=headers(client))).status == 405
    for changes in (
        {"Origin": "https://evil.invalid"},
        {"X-TTLock-Admin": "0"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        response = await client.post(
            URL, json={"action": "list"}, headers={**headers(client), **changes}
        )
        assert response.status == 403
    response = await client.post(
        URL + "?authSig=synthetic", json={"action": "list"}, headers=headers(client)
    )
    assert response.status == 403
    response = await client.post(URL, json={"action": "list"})
    assert response.status == 403


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"action": "write", "pin": "synthetic-secret"},
        {"action": "list", "pin": "synthetic-secret"},
        {"action": ["list"]},
        {"action": "list", "unknown": True},
    ],
)
async def test_malformed_or_write_request_is_fixed_error_without_echo(
    setup_integration, hass_client, body, caplog
):
    client = await hass_client()
    response = await client.post(URL, json=body, headers=headers(client))
    assert response.status == 400
    assert "synthetic-secret" not in await response.text() + caplog.text


async def test_bad_content_and_oversize_inputs_are_bounded(
    setup_integration, hass_client
):
    client = await hass_client()
    for body, ctype in [
        ("not-json", "application/json"),
        ("x" * 4097, "application/json"),
        ("{}", "text/plain"),
    ]:
        response = await client.post(
            URL, data=body, headers={**headers(client), "Content-Type": ctype}
        )
        assert response.status == 400


async def test_read_failure_is_stale_cleanup_is_required_and_no_raw_traceback(
    hass, setup_integration, hass_client, mock_ttlock_connection, caplog
):
    client = await hass_client()
    conn = mock_ttlock_connection
    conn.async_get_administrative_inventory = AsyncMock(
        side_effect=OSError("synthetic-private-frame")
    )
    response = await client.post(
        URL, json=request_data(setup_integration, "read"), headers=headers(client)
    )
    assert response.status == 503
    assert "synthetic-private-frame" not in await response.text() + caplog.text
    assert not setup_integration.runtime_data.access_records.snapshot(
        "AA:BB:CC:DD:EE:FF", "ic_card"
    )["fresh"]


async def test_legacy_optout_preserves_compatibility_but_cannot_retract_service_event(
    hass, setup_integration, hass_client, mock_ttlock_connection
):
    client = await hass_client()
    response = await client.post(
        URL,
        json=request_data(setup_integration, "legacy", disabled=True),
        headers=headers(client),
    )
    assert response.status == 200
    events = []
    hass.bus.async_listen(EVENT_CALL_SERVICE, events.append)
    with pytest.raises(Exception, match="disabled by administrator"):
        await hass.services.async_call(
            "ttlock_ble",
            "delete_passcode",
            {"device_id": _device_id(hass), "code": "8675309", "type": "permanent"},
            blocking=True,
        )
    await hass.async_block_till_done()
    assert any(e.data.get("service_data", {}).get("code") == "8675309" for e in events)
    mock_ttlock_connection.async_delete_passcode.assert_not_awaited()


async def test_frontend_registration_is_admin_only_and_local(hass, setup_integration):
    hass.config.components.add("frontend")
    with (
        patch.object(hass.http, "register_view"),
        patch.object(
            hass.http, "async_register_static_paths", new_callable=AsyncMock
        ) as static,
        patch(
            "custom_components.ttlock_ble.management_http.async_register_panel",
            new_callable=AsyncMock,
        ) as panel,
    ):
        await async_setup_management(hass)
    assert panel.call_args.kwargs["require_admin"] is True
    assert panel.call_args.kwargs["module_url"].startswith("/")
    assert static.call_args.args[0][0].cache_headers is False


async def test_http_cancellation_propagates_without_inventing_records(hass):
    view = AdministrationView(hass)
    with (
        patch.object(view, "_authorized", return_value=True),
        patch.object(
            view, "_body", new_callable=AsyncMock, return_value={"action": "read"}
        ),
        patch.object(
            view,
            "_dispatch",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError,
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await view.post(None)


@pytest.mark.parametrize(("interrupted", "cleaned"), [(True, True), (False, False)])
async def test_partial_or_uncertain_cleanup_cannot_freshen_private_refs(
    setup_integration, hass_client, mock_ttlock_connection, interrupted, cleaned
):
    client = await hass_client()
    result = AdministrativeInventory(
        InventoryResult(
            (InventoryEntry("permanent"),),
            complete=True,
            limitation="",
            transport_interrupted=interrupted,
        ),
        (
            AdministrativeRecord(
                "ic_card", "card_4byte", "permanent", b"synthetic-card-uid"
            ),
        ),
    )
    mock_ttlock_connection.async_get_administrative_inventory = AsyncMock(
        return_value=result
    )
    mock_ttlock_connection.inventory_diagnostics = {"cleanup_confirmed": cleaned}
    response = await client.post(
        URL, json=request_data(setup_integration, "read"), headers=headers(client)
    )
    data = await response.json()
    assert not data["fresh"]
    assert not data["records"]
    assert data["inventory"]["transport_interrupted"] is interrupted


@pytest.mark.parametrize(
    "target", [{"entry_id": "missing"}, {"lock": "wrong-lock"}, {"method": "fob"}]
)
async def test_wrong_entry_lock_or_unverified_method_fails_closed(
    setup_integration, hass_client, target
):
    client = await hass_client()
    response = await client.post(
        URL, json=request_data(setup_integration, **target), headers=headers(client)
    )
    assert response.status == 400


async def test_bad_metadata_keeps_lock_available_and_blocks_legacy(
    hass, setup_integration, hass_storage, mock_ttlock_connection, caplog
):
    from homeassistant.config_entries import ConfigEntryState

    hass_storage[f"ttlock_ble.access_records.{setup_integration.entry_id}"] = {
        "version": 1,
        "data": {"secret": "synthetic-do-not-echo"},
    }
    # Force the same private storage read that occurs after a process restart.
    from custom_components.ttlock_ble.access_records import AccessRecordStore

    with patch.object(
        AccessRecordStore, "async_load", side_effect=ValueError("synthetic-do-not-echo")
    ):
        await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.LOADED
    assert setup_integration.runtime_data.access_records is None
    assert hass.states.get("lock.front_door_lock").state == "locked"
    with pytest.raises(Exception, match="disabled by administrator"):
        await hass.services.async_call(
            "ttlock_ble",
            "clear_passcodes",
            {"device_id": _device_id(hass)},
            blocking=True,
        )
    mock_ttlock_connection.async_clear_passcodes.assert_not_awaited()
    assert "synthetic-do-not-echo" not in caplog.text
