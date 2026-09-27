"""
Event platform for ttlock_ble.

Surfaces historical operation records read from the lock's on-device
storage (fingerprint, keypad, IC card, etc.) every time the integration
connects or polls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.event import EventEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from ttlock_ble import LogOperate

from .connection import log_signal
from .entity import TtlockBleEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from ttlock_ble import LogEntry

    from .data import TtlockBleConfigEntry, TtlockBleLogEventAttributes


LOG_EVENT_TYPES: list[str] = [
    "unlock",
    "lock",
    "unlock_failed",
    "password_change",
    "other",
]

# The SDK's field named ``password`` is overloaded across working passcodes,
# card numbers, fingerprint ids, and fob addresses. None of it is published:
# event attributes are retained by Recorder and exposed through HA's API, and
# RC10 cannot authoritatively distinguish a harmless label from a credential.
PASSCODE_RECORD_TYPES: frozenset[int] = frozenset(
    {
        LogOperate.KEYBOARD_PASSWORD_UNLOCK,
        LogOperate.KEYBOARD_MODIFY_PASSWORD,
        LogOperate.KEYBOARD_REMOVE_SINGLE_PASSWORD,
        LogOperate.ERROR_PASSWORD_UNLOCK,
        LogOperate.KEYBOARD_REMOVE_ALL_PASSWORDS,
        LogOperate.KEYBOARD_PASSWORD_KICKED,
        LogOperate.USE_DELETE_CODE,
        LogOperate.PASSCODE_EXPIRED,
        LogOperate.SPACE_INSUFFICIENT,
        LogOperate.PASSCODE_IN_BLACK_LIST,
        LogOperate.PASSCODE_LOCK,
        LogOperate.PASSCODE_UNLOCK_FAILED_LOCK_REVERSE,
        LogOperate.DOUBLE_CHECK_PASSCODE_UNLOCK,
        LogOperate.ADMIN_CODE_UNLOCK,
        LogOperate.ADD_PASSCODE_SUCCESSFULLY,
    },
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: TtlockBleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create log-event entities per `VirtualKey`."""
    data = entry.runtime_data
    async_add_entities(
        TtlockBleLogEvent(data.coordinator, key) for key in data.virtual_keys
    )


UNLOCK_RECORD_TYPES: frozenset[int] = frozenset(
    {
        LogOperate.MOBILE_UNLOCK,
        LogOperate.SERVER_UNLOCK,
        LogOperate.KEYBOARD_PASSWORD_UNLOCK,
        LogOperate.IC_UNLOCK_SUCCEED,
        LogOperate.FR_UNLOCK_SUCCEED,
        LogOperate.BONG_UNLOCK,
        LogOperate.OPERATE_KEY_UNLOCK,
        LogOperate.GATEWAY_UNLOCK,
        LogOperate.WIRELESS_KEY_FOB,
        LogOperate.WIRELESS_KEY_PAD,
        LogOperate.REMOTE_CONTROL_KEY,
        LogOperate.QR_CODE_UNLOCK_SUCCESS,
        LogOperate.FACE_3D_UNLOCK_SUCCESS,
        LogOperate.APP_AUTH_KEY_UNLOCK_SUCCESS,
        LogOperate.GATEWAY_AUTH_KEY_UNLOCK_SUCCESS,
        LogOperate.DOUBLE_CHECK_KEY_UNLOCK,
        LogOperate.DOUBLE_CHECK_PASSCODE_UNLOCK,
        LogOperate.DOUBLE_CHECK_FINGER_PRINT_UNLOCK,
        LogOperate.DOUBLE_CHECK_CARD_UNLOCK,
        LogOperate.DOUBLE_CHECK_FACE_UNLOCK,
        LogOperate.DOUBLE_CHECK_KEY_FOB_UNLOCK,
        LogOperate.DOUBLE_CHECK_PALM_VEIN_UNLOCK,
        LogOperate.PALM_VEIN_UNLOCK_SUCCESS,
        LogOperate.ADMIN_CODE_UNLOCK,
        LogOperate.THIRD_DEVICE_UNLOCK_SUCCESS,
    },
)

LOCK_RECORD_TYPES: frozenset[int] = frozenset(
    {
        LogOperate.OPERATE_BLE_LOCK,
        LogOperate.OPERATE_KEY_LOCK,
        LogOperate.PASSCODE_LOCK,
        LogOperate.IC_LOCK,
        LogOperate.FR_LOCK,
        LogOperate.FACE_3D_LOCK,
        LogOperate.PALM_VEIN_LOCK,
        LogOperate.THIRD_DEVICE_LOCK_SUCCESS,
    },
)

UNLOCK_FAILED_RECORD_TYPES: frozenset[int] = frozenset(
    {
        LogOperate.ERROR_PASSWORD_UNLOCK,
        LogOperate.FR_UNLOCK_FAILED,
        LogOperate.IC_UNLOCK_FAILED,
        LogOperate.APP_UNLOCK_FAILED_LOCK_REVERSE,
        LogOperate.PASSCODE_UNLOCK_FAILED_LOCK_REVERSE,
        LogOperate.IC_UNLOCK_FAILED_LOCK_REVERSE,
        LogOperate.FR_UNLOCK_FAILED_LOCK_REVERSE,
        LogOperate.PASSCODE_EXPIRED,
        LogOperate.PASSCODE_IN_BLACK_LIST,
        LogOperate.IC_UNLOCK_FAILED_BLACKLIST,
        LogOperate.QR_CODE_UNLOCK_FAILED,
        LogOperate.FACE_3D_UNLOCK_FAILED_LOCK_REVERSE,
        LogOperate.FACE_3D_UNLOCK_FAILED_INVALID_TIME,
        LogOperate.PALM_VEIN_UNLOCK_FAILED_LOCK_REVERSE,
        LogOperate.PALM_VEIN_UNLOCK_FAILED,
        LogOperate.CARD_UNLOCK_FAILED,
        LogOperate.THIRD_DEVICE_UNLOCK_FAILED_LOCK_REVERSE,
        LogOperate.THIRD_DEVICE_UNLOCK_FAILED_INVALID_TIME,
    },
)

PASSWORD_CHANGE_RECORD_TYPES: frozenset[int] = frozenset(
    {
        LogOperate.KEYBOARD_MODIFY_PASSWORD,
        LogOperate.KEYBOARD_REMOVE_SINGLE_PASSWORD,
        LogOperate.KEYBOARD_REMOVE_ALL_PASSWORDS,
        LogOperate.KEYBOARD_PASSWORD_KICKED,
        LogOperate.USE_DELETE_CODE,
        LogOperate.ADD_IC,
        LogOperate.CLEAR_IC_SUCCEED,
        LogOperate.DELETE_IC_SUCCEED,
        LogOperate.ADD_FR,
        LogOperate.DELETE_FR_SUCCEED,
    },
)


def _classify_record(record_type: int) -> str:
    """Map a LogOperate record type to an HA event type."""
    if record_type in UNLOCK_RECORD_TYPES:
        return "unlock"
    if record_type in LOCK_RECORD_TYPES:
        return "lock"
    if record_type in UNLOCK_FAILED_RECORD_TYPES:
        return "unlock_failed"
    if record_type in PASSWORD_CHANGE_RECORD_TYPES:
        return "password_change"
    return "other"


def _record_type_name(record_type: int) -> str:
    """Return a human-friendly name for the record type."""
    try:
        return LogOperate(record_type).name.lower()
    except ValueError:
        return str(record_type)


def _operation_method(record_type: int) -> str:
    """Return a stable, human-readable operation method."""
    method_types: tuple[tuple[str, frozenset[int]], ...] = (
        (
            "physical_key",
            frozenset({LogOperate.OPERATE_KEY_UNLOCK, LogOperate.OPERATE_KEY_LOCK}),
        ),
        (
            "fingerprint",
            frozenset(
                {
                    LogOperate.FR_UNLOCK_SUCCEED,
                    LogOperate.FR_UNLOCK_FAILED,
                    LogOperate.FR_LOCK,
                    LogOperate.DOUBLE_CHECK_FINGER_PRINT_UNLOCK,
                },
            ),
        ),
        (
            "ic_card",
            frozenset(
                {
                    LogOperate.IC_UNLOCK_SUCCEED,
                    LogOperate.IC_UNLOCK_FAILED,
                    LogOperate.IC_LOCK,
                    LogOperate.DOUBLE_CHECK_CARD_UNLOCK,
                },
            ),
        ),
        (
            "passcode",
            frozenset(
                {
                    LogOperate.KEYBOARD_PASSWORD_UNLOCK,
                    LogOperate.ERROR_PASSWORD_UNLOCK,
                    LogOperate.PASSCODE_LOCK,
                    LogOperate.DOUBLE_CHECK_PASSCODE_UNLOCK,
                    LogOperate.ADMIN_CODE_UNLOCK,
                },
            ),
        ),
        (
            "ttlock_app",
            frozenset(
                {
                    LogOperate.MOBILE_UNLOCK,
                    LogOperate.OPERATE_BLE_LOCK,
                    LogOperate.APP_AUTH_KEY_UNLOCK_SUCCESS,
                },
            ),
        ),
        (
            "gateway",
            frozenset(
                {
                    LogOperate.GATEWAY_UNLOCK,
                    LogOperate.GATEWAY_AUTH_KEY_UNLOCK_SUCCESS,
                },
            ),
        ),
        ("key_fob", frozenset({LogOperate.WIRELESS_KEY_FOB})),
        ("wireless_keypad", frozenset({LogOperate.WIRELESS_KEY_PAD})),
        ("remote_control", frozenset({LogOperate.REMOTE_CONTROL_KEY})),
        ("qr_code", frozenset({LogOperate.QR_CODE_UNLOCK_SUCCESS})),
        ("face", frozenset({LogOperate.FACE_3D_UNLOCK_SUCCESS})),
        ("palm_vein", frozenset({LogOperate.PALM_VEIN_UNLOCK_SUCCESS})),
        ("third_party_device", frozenset({LogOperate.THIRD_DEVICE_UNLOCK_SUCCESS})),
    )
    for method, record_types in method_types:
        if record_type in record_types:
            return method
    return "other"


class TtlockBleLogEvent(TtlockBleEntity, EventEntity):
    """Fires when a new operation log entry is retrieved from the lock."""

    _attr_translation_key = "log"
    _attr_event_types = LOG_EVENT_TYPES

    @property
    def unique_id(self) -> str:
        """Return a stable unique id for this entity."""
        return f"{self._key.lockMac}_log"

    async def async_added_to_hass(self) -> None:
        """Subscribe to the log dispatcher signal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                log_signal(self._key.lockMac),
                self._on_log_entry,
            ),
        )

    @callback
    def _on_log_entry(self, entry: LogEntry) -> None:
        """Translate a LogEntry into an HA event fire."""
        event_type = _classify_record(entry.record_type)
        attributes: TtlockBleLogEventAttributes = {
            "record_type": _record_type_name(entry.record_type),
            "method": _operation_method(entry.record_type),
            "battery": entry.lock_battery,
        }
        if entry.operate_date is not None:
            attributes["timestamp"] = entry.operate_date.isoformat()
        if entry.uid is not None:
            attributes["uid"] = entry.uid
        if entry.key_id is not None:
            attributes["key_id"] = entry.key_id
        if entry.accessory_battery is not None:
            attributes["accessory_battery"] = entry.accessory_battery
        self._trigger_event(event_type, dict(attributes))
        self.async_write_ha_state()
