"""Home Assistant actions for local TTLock management."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.device_registry import format_mac
from homeassistant.util import dt as dt_util

from ttlock_ble import KeyboardPwdType

from .const import DOMAIN

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

    from .connection import TtlockBleConnection
    from .data import TtlockBleConfigEntry

SERVICE_ADD_PASSCODE = "add_passcode"
SERVICE_DELETE_PASSCODE = "delete_passcode"
SERVICE_CLEAR_PASSCODES = "clear_passcodes"
SERVICE_GET_AUTO_LOCK = "get_auto_lock"
SERVICE_SET_AUTO_LOCK = "set_auto_lock"

ATTR_DEVICE_ID = "device_id"
ATTR_CODE = "code"
ATTR_PASSCODE_TYPE = "type"
ATTR_START = "start"
ATTR_END = "end"
ATTR_SECONDS = "seconds"

PASSCODE_TYPE_PERMANENT = "permanent"
PASSCODE_TYPE_PERIOD = "period"
MIN_PASSCODE_LENGTH = 4
MAX_PASSCODE_LENGTH = 9

ERROR_DEVICE_NOT_FOUND = "device_not_found"
ERROR_DEVICE_NOT_LOADED = "device_not_loaded"
ERROR_INVALID_PASSCODE = "invalid_passcode"
ERROR_INVALID_DATETIME = "invalid_datetime"
ERROR_PERIOD_WINDOW_REQUIRED = "period_window_required"
ERROR_INVALID_PERIOD_WINDOW = "invalid_period_window"
PASSCODE_TYPES = {
    PASSCODE_TYPE_PERMANENT: KeyboardPwdType.PERMANENT,
    PASSCODE_TYPE_PERIOD: KeyboardPwdType.PERIOD,
}

ADD_PASSCODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_CODE): cv.string,
        vol.Required(ATTR_PASSCODE_TYPE): vol.In(PASSCODE_TYPES),
        vol.Optional(ATTR_START): cv.string,
        vol.Optional(ATTR_END): cv.string,
    }
)
DELETE_PASSCODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_CODE): cv.string,
        vol.Required(ATTR_PASSCODE_TYPE): vol.In(PASSCODE_TYPES),
    }
)
CLEAR_PASSCODES_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})
GET_AUTO_LOCK_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})
SET_AUTO_LOCK_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_SECONDS): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=0xFFFF)
        ),
    }
)


def _validation_error(key: str) -> ServiceValidationError:
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key)


def _validate_code(code: str) -> None:
    if (
        not (MIN_PASSCODE_LENGTH <= len(code) <= MAX_PASSCODE_LENGTH)
        or not code.isdigit()
    ):
        raise _validation_error(ERROR_INVALID_PASSCODE)


def _parse_lock_datetime(value: str) -> datetime:
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        raise _validation_error(ERROR_INVALID_DATETIME)
    if parsed.tzinfo is not None:
        parsed = dt_util.as_local(parsed)
    return parsed


def _period_window(call: ServiceCall) -> tuple[str, str]:
    start_raw = call.data.get(ATTR_START)
    end_raw = call.data.get(ATTR_END)
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        raise _validation_error(ERROR_PERIOD_WINDOW_REQUIRED)
    start = _parse_lock_datetime(start_raw)
    end = _parse_lock_datetime(end_raw)
    if end <= start:
        raise _validation_error(ERROR_INVALID_PERIOD_WINDOW)
    return start.strftime("%y%m%d%H%M"), end.strftime("%y%m%d%H%M")


def _connection_for_device(hass: HomeAssistant, device_id: str) -> TtlockBleConnection:
    device = async_get_device_registry(hass).async_get(device_id)
    if device is None:
        raise _validation_error(ERROR_DEVICE_NOT_FOUND)
    device_macs = {
        identifier for domain, identifier in device.identifiers if domain == DOMAIN
    }
    for raw_entry in hass.config_entries.async_entries(DOMAIN):
        if raw_entry.state is not ConfigEntryState.LOADED:
            continue
        entry = cast("TtlockBleConfigEntry", raw_entry)
        for mac, connection in entry.runtime_data.connections.items():
            if format_mac(mac) in device_macs:
                return connection
    raise _validation_error(ERROR_DEVICE_NOT_LOADED)


async def _async_add_passcode(hass: HomeAssistant, call: ServiceCall) -> None:
    code = cast("str", call.data[ATTR_CODE])
    _validate_code(code)
    passcode_type = cast("str", call.data[ATTR_PASSCODE_TYPE])
    pwd_type = PASSCODE_TYPES[passcode_type]
    if pwd_type is KeyboardPwdType.PERIOD:
        start_date, end_date = _period_window(call)
    else:
        start_date, end_date = "0001311400", "9912311400"
    await _connection_for_device(
        hass,
        cast("str", call.data[ATTR_DEVICE_ID]),
    ).async_add_passcode(
        code,
        pwd_type=pwd_type,
        start_date=start_date,
        end_date=end_date,
    )


async def _async_delete_passcode(hass: HomeAssistant, call: ServiceCall) -> None:
    code = cast("str", call.data[ATTR_CODE])
    _validate_code(code)
    await _connection_for_device(
        hass,
        cast("str", call.data[ATTR_DEVICE_ID]),
    ).async_delete_passcode(
        code,
        pwd_type=PASSCODE_TYPES[cast("str", call.data[ATTR_PASSCODE_TYPE])],
    )


async def _async_clear_passcodes(hass: HomeAssistant, call: ServiceCall) -> None:
    await _connection_for_device(
        hass,
        cast("str", call.data[ATTR_DEVICE_ID]),
    ).async_clear_passcodes()


async def _async_get_auto_lock(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    seconds = await _connection_for_device(
        hass,
        cast("str", call.data[ATTR_DEVICE_ID]),
    ).async_get_auto_lock_time()
    return cast("ServiceResponse", {ATTR_SECONDS: seconds})


async def _async_set_auto_lock(hass: HomeAssistant, call: ServiceCall) -> None:
    await _connection_for_device(
        hass,
        cast("str", call.data[ATTR_DEVICE_ID]),
    ).async_set_auto_lock_time(cast("int", call.data[ATTR_SECONDS]))


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register local management actions once for the integration."""
    if hass.services.has_service(DOMAIN, SERVICE_ADD_PASSCODE):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_PASSCODE,
        partial(_async_add_passcode, hass),
        schema=ADD_PASSCODE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_PASSCODE,
        partial(_async_delete_passcode, hass),
        schema=DELETE_PASSCODE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_PASSCODES,
        partial(_async_clear_passcodes, hass),
        schema=CLEAR_PASSCODES_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_AUTO_LOCK,
        partial(_async_get_auto_lock, hass),
        schema=GET_AUTO_LOCK_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_AUTO_LOCK,
        partial(_async_set_auto_lock, hass),
        schema=SET_AUTO_LOCK_SCHEMA,
    )
