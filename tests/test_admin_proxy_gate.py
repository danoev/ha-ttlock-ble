"""Additional RC16 publication-gate tests, synthetic HA only."""

from datetime import timedelta

import pytest
from homeassistant.components.http.config import async_get_and_load_store
from homeassistant.setup import async_setup_component
from ttlock_ble import AdministrativeInventory, InventoryResult

from custom_components.ttlock_ble.access_records import AccessRecordStore
from custom_components.ttlock_ble.management_http import URL
from tests.test_management_http import headers


@pytest.fixture
def proxy_mode(request):
    return getattr(request, "param", "trusted")


@pytest.fixture(autouse=True)
async def gate_http(hass, proxy_mode):
    assert await async_setup_component(
        hass,
        "http",
        {
            "http": {
                "use_x_forwarded_for": proxy_mode != "disabled",
                "trusted_proxies": ["127.0.0.1", "127.0.0.2"]
                if proxy_mode != "untrusted"
                else ["10.42.0.1"],
            }
        },
    )
    # This test instance has no operator to confirm the migrated HTTP settings.
    (await async_get_and_load_store(hass)).async_cancel_revert()


def forwarded(origin="https://ha.example.invalid", **changes):
    return {
        "Origin": origin,
        "X-TTLock-Admin": "1",
        "Sec-Fetch-Site": "same-origin",
        "X-Forwarded-For": "203.0.113.9",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "ha.example.invalid",
        **changes,
    }


@pytest.mark.parametrize(
    "mode",
    [
        "direct",
        "host_preserved",
        "forwarded_host",
        "multi_proxy",
        "nondefault_port",
        "default_port",
    ],
)
async def test_origin_through_real_ha_proxy_middleware(
    setup_integration, hass_client, mode
):
    client = await hass_client()
    hdr = forwarded()
    if mode == "direct":
        hdr = headers(client)
    if mode == "host_preserved":
        del hdr["X-Forwarded-Host"]
        hdr["Host"] = "ha.example.invalid"
    if mode == "multi_proxy":
        hdr["X-Forwarded-For"] = "203.0.113.9,127.0.0.2"
        hdr["X-Forwarded-Proto"] = "https,http"
    if mode == "nondefault_port":
        hdr["X-Forwarded-Host"] = "ha.example.invalid:8443"
        hdr["Origin"] = "https://ha.example.invalid:8443"
    if mode == "default_port":
        hdr["X-Forwarded-Host"] = "ha.example.invalid:443"
    response = await client.post(URL, json={"action": "list"}, headers=hdr)
    assert response.status == 200, (mode, await response.text())


@pytest.mark.parametrize("proxy_mode", ["untrusted", "disabled"], indirect=True)
async def test_untrusted_or_disabled_forwarding_rejected(
    setup_integration, hass_client, proxy_mode
):
    client = await hass_client()
    response = await client.post(URL, json={"action": "list"}, headers=forwarded())
    assert response.status == 400


@pytest.mark.parametrize(
    "bad_origin",
    [
        "https://evil.invalid",
        "http://ha.example.invalid",
        "https://ha.example.invalid:8443",
        "null",
        "https://ha.example.invalid/other",
    ],
)
async def test_proxy_does_not_weaken_origin_gate(
    setup_integration, hass_client, bad_origin
):
    client = await hass_client()
    response = await client.post(
        URL, json={"action": "list"}, headers=forwarded(bad_origin)
    )
    assert response.status == 403


@pytest.mark.parametrize("session", ["invalid", "expired", "ordinary", "long_lived"])
async def test_proxy_auth_contexts(
    hass, setup_integration, hass_client, hass_admin_user, session
):
    from pytest_homeassistant_custom_component.common import MockUser

    user = MockUser().add_to_hass(hass) if session == "ordinary" else hass_admin_user
    kwargs = {"client_id": "http://test/"}
    if session == "expired":
        kwargs["access_token_expiration"] = timedelta(seconds=-60)
    if session == "long_lived":
        kwargs.update(token_type="long_lived_access_token", client_name="gate")
    refresh = await hass.auth.async_create_refresh_token(user, **kwargs)
    token = hass.auth.async_create_access_token(refresh)
    if session == "invalid":
        token = "synthetic-invalid-session"
    client = await hass_client(token)
    response = await client.post(URL, json={"action": "list"}, headers=forwarded())
    assert response.status in {401, 403}


@pytest.mark.parametrize("field", ["pin", "uid", "fingerprint_id", "raw_packet"])
async def test_secret_submission_rejected_through_proxy(
    setup_integration, hass_client, field, caplog
):
    client = await hass_client()
    response = await client.post(
        URL,
        json={"action": "list", field: "synthetic-private-value"},
        headers=forwarded(),
    )
    assert response.status == 400
    assert "synthetic-private-value" not in await response.text() + caplog.text


async def test_old_empty_read_cannot_revive_deleted_store(hass):
    store = AccessRecordStore(hass, "gate")
    await store.async_load()
    revision = store.mark_stale("lock", "ic_card")
    await store.async_remove()
    empty = AdministrativeInventory(
        InventoryResult((), complete=True, limitation=""), ()
    )
    await store.async_reconcile(
        "lock", "ic_card", empty, cleaned=True, revision=revision
    )
    assert not store.snapshot("lock", "ic_card")["fresh"]


async def test_pre_removal_ticket_stays_invalid_after_store_reload(hass):
    store = AccessRecordStore(hass, "gate")
    await store.async_load()
    revision = store.mark_stale("lock", "ic_card")
    await store.async_remove()
    await store.async_load()
    empty = AdministrativeInventory(
        InventoryResult((), complete=True, limitation=""), ()
    )
    await store.async_reconcile(
        "lock", "ic_card", empty, cleaned=True, revision=revision
    )
    assert not store.snapshot("lock", "ic_card")["fresh"]


async def test_legacy_permission_scope_and_persistent_optout_for_all_three(
    hass, setup_integration, hass_client, mock_ttlock_connection
):
    from homeassistant.core import Context
    from homeassistant.exceptions import ServiceValidationError
    from pytest_homeassistant_custom_component.common import MockUser

    from tests.test_management_http import request_data
    from tests.test_services import _device_id

    user = MockUser().add_to_hass(hass)
    context = Context(user_id=user.id)
    calls = [
        ("add_passcode", {"code": "8675309", "type": "permanent"}),
        ("delete_passcode", {"code": "8675309", "type": "permanent"}),
        ("clear_passcodes", {}),
    ]
    for service, data in calls:
        await hass.services.async_call(
            "ttlock_ble",
            service,
            {"device_id": _device_id(hass), **data},
            context=context,
            blocking=True,
        )
    # Admin-panel restrictions do not retroactively secure legacy service handlers.
    assert mock_ttlock_connection.async_add_passcode.await_count == 1
    assert mock_ttlock_connection.async_delete_passcode.await_count == 1
    assert mock_ttlock_connection.async_clear_passcodes.await_count == 1
    client = await hass_client()
    response = await client.post(
        URL,
        json=request_data(setup_integration, "legacy", disabled=True),
        headers=headers(client),
    )
    assert response.status == 200
    restarted = AccessRecordStore(hass, setup_integration.entry_id)
    await restarted.async_load()
    setup_integration.runtime_data.access_records = restarted
    for service, data in calls:
        with pytest.raises(ServiceValidationError, match="disabled by administrator"):
            await hass.services.async_call(
                "ttlock_ble",
                service,
                {"device_id": _device_id(hass), **data},
                context=context,
                blocking=True,
            )
    assert mock_ttlock_connection.async_add_passcode.await_count == 1
    assert mock_ttlock_connection.async_delete_passcode.await_count == 1
    assert mock_ttlock_connection.async_clear_passcodes.await_count == 1
