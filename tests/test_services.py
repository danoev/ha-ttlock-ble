from __future__ import annotations

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_get_all_descriptions
from ttlock_ble import InventoryEntry, InventoryResult, KeyboardPwdType, TTLockError

from custom_components.ttlock_ble.const import DOMAIN
from custom_components.ttlock_ble.services import async_setup_services


def _device_id(hass) -> str:
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, "aa:bb:cc:dd:ee:ff")}
    )
    assert device is not None
    return device.id


async def test_management_services_are_registered(hass, setup_integration) -> None:
    for service in (
        "add_passcode",
        "delete_passcode",
        "clear_passcodes",
        "get_auto_lock",
        "set_auto_lock",
        "get_access_inventory",
    ):
        assert hass.services.has_service(DOMAIN, service)


async def test_service_descriptions_load(hass, setup_integration) -> None:
    descriptions = await async_get_all_descriptions(hass)
    assert set(descriptions[DOMAIN]) >= {
        "add_passcode",
        "delete_passcode",
        "clear_passcodes",
        "get_auto_lock",
        "set_auto_lock",
        "get_access_inventory",
    }


async def test_service_setup_is_idempotent(hass, setup_integration) -> None:
    await async_setup_services(hass)
    assert hass.services.has_service(DOMAIN, "add_passcode")


async def test_add_permanent_passcode(
    hass,
    setup_integration,
    mock_ttlock_connection,
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "add_passcode",
        {
            "device_id": _device_id(hass),
            "code": "583921",
            "type": "permanent",
        },
        blocking=True,
    )
    mock_ttlock_connection.async_add_passcode.assert_awaited_once_with(
        "583921",
        pwd_type=KeyboardPwdType.PERMANENT,
        start_date="0001311400",
        end_date="9912311400",
    )


async def test_add_period_passcode_converts_window(
    hass,
    setup_integration,
    mock_ttlock_connection,
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "add_passcode",
        {
            "device_id": _device_id(hass),
            "code": "583921",
            "type": "period",
            "start": "2026-08-22T15:00:00",
            "end": "2026-08-25T10:00:00",
        },
        blocking=True,
    )
    mock_ttlock_connection.async_add_passcode.assert_awaited_once_with(
        "583921",
        pwd_type=KeyboardPwdType.PERIOD,
        start_date="2608221500",
        end_date="2608251000",
    )


async def test_period_passcode_requires_a_complete_window(
    hass,
    setup_integration,
) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "add_passcode",
            {
                "device_id": _device_id(hass),
                "code": "583921",
                "type": "period",
            },
            blocking=True,
        )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("not-a-date", "2026-08-25T10:00:00"),
        ("2026-08-25T10:00:00", "2026-08-22T15:00:00"),
    ],
)
async def test_period_passcode_rejects_invalid_windows(
    hass,
    setup_integration,
    start,
    end,
) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "add_passcode",
            {
                "device_id": _device_id(hass),
                "code": "583921",
                "type": "period",
                "start": start,
                "end": end,
            },
            blocking=True,
        )


async def test_invalid_passcode_error_does_not_echo_secret(
    hass,
    setup_integration,
) -> None:
    invalid_code = "12ab"
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            DOMAIN,
            "add_passcode",
            {
                "device_id": _device_id(hass),
                "code": invalid_code,
                "type": "permanent",
            },
            blocking=True,
        )
    assert invalid_code not in str(error.value)


async def test_delete_and_clear_passcodes(
    hass,
    setup_integration,
    mock_ttlock_connection,
) -> None:
    device_id = _device_id(hass)
    await hass.services.async_call(
        DOMAIN,
        "delete_passcode",
        {"device_id": device_id, "code": "583921", "type": "period"},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        "clear_passcodes",
        {"device_id": device_id},
        blocking=True,
    )
    mock_ttlock_connection.async_delete_passcode.assert_awaited_once_with(
        "583921",
        pwd_type=KeyboardPwdType.PERIOD,
    )
    mock_ttlock_connection.async_clear_passcodes.assert_awaited_once()


async def test_get_and_set_auto_lock(
    hass,
    setup_integration,
    mock_ttlock_connection,
) -> None:
    device_id = _device_id(hass)
    response = await hass.services.async_call(
        DOMAIN,
        "get_auto_lock",
        {"device_id": device_id},
        blocking=True,
        return_response=True,
    )
    await hass.services.async_call(
        DOMAIN,
        "set_auto_lock",
        {"device_id": device_id, "seconds": 0},
        blocking=True,
    )
    assert response == {"seconds": 30}
    mock_ttlock_connection.async_get_auto_lock_time.assert_awaited_once()
    mock_ttlock_connection.async_set_auto_lock_time.assert_awaited_once_with(0)


async def test_unknown_device_is_rejected(hass, setup_integration) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "clear_passcodes",
            {"device_id": "missing-device"},
            blocking=True,
        )


async def test_unloaded_device_is_rejected(hass, setup_integration) -> None:
    device_id = _device_id(hass)
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "clear_passcodes",
            {"device_id": device_id},
            blocking=True,
        )


async def test_access_inventory_pin_response_is_credential_free(
    hass, setup_integration, mock_ttlock_connection
) -> None:
    mock_ttlock_connection.async_get_access_inventory.return_value = InventoryResult(
        (InventoryEntry("permanent"), InventoryEntry("time_window")),
        complete=True,
        limitation="active batch PINs can be omitted",
    )
    response = await hass.services.async_call(
        DOMAIN,
        "get_access_inventory",
        {"device_id": _device_id(hass), "method": "keypad_pin"},
        blocking=True,
        return_response=True,
    )
    assert response == {
        "method": "keypad_pin",
        "read_status": "partial",
        "count": 2,
        "validity_counts": {"permanent": 1, "time_window": 1},
        "enumeration_complete": True,
        "skipped_unknown_layouts": 0,
        "limitation": "active batch PINs can be omitted",
        "credential_names": "unavailable",
        "create_capability": "supported",
        "modify_capability": "unsupported",
        "delete_capability": "supported",
    }
    mock_ttlock_connection.async_get_access_inventory.assert_awaited_once_with(
        "keypad_pin"
    )


async def test_access_inventory_unverified_admin_skips_ble(
    hass, setup_integration, mock_ttlock_connection
) -> None:
    mock_ttlock_connection.inventory_admin_available = False
    response = await hass.services.async_call(
        DOMAIN,
        "get_access_inventory",
        {"device_id": _device_id(hass), "method": "fingerprint"},
        blocking=True,
        return_response=True,
    )
    assert response == {
        "method": "fingerprint",
        "read_status": "unverified",
        "reason": "admin_key_required",
    }
    mock_ttlock_connection.async_get_access_inventory.assert_not_awaited()


async def test_access_inventory_failure_is_unverified_without_sensitive_error(
    hass, setup_integration, mock_ttlock_connection
) -> None:
    mock_ttlock_connection.async_get_access_inventory.side_effect = TTLockError(
        "synthetic-sensitive-token"
    )
    response = await hass.services.async_call(
        DOMAIN,
        "get_access_inventory",
        {"device_id": _device_id(hass), "method": "ic_card"},
        blocking=True,
        return_response=True,
    )
    assert response == {
        "method": "ic_card",
        "read_status": "unverified",
        "reason": "read_failed",
    }


async def test_access_inventory_partial_card_discovery(
    hass, setup_integration, mock_ttlock_connection
) -> None:
    mock_ttlock_connection.async_get_access_inventory.return_value = InventoryResult(
        (InventoryEntry("permanent"),),
        complete=False,
        limitation="eight-byte IC-card layout unverified",
    )
    response = await hass.services.async_call(
        DOMAIN,
        "get_access_inventory",
        {"device_id": _device_id(hass), "method": "ic_card"},
        blocking=True,
        return_response=True,
    )
    assert response["read_status"] == "partial"
    assert response["count"] == 1
    assert response["enumeration_complete"] is False
