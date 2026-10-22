"""Administrator-only, read-only groundwork; never accept credential material."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components import websocket_api
from homeassistant.core import callback

from .access import access_capabilities
from .services import _connection_for_device
from .validation import vol

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@callback
def async_setup_admin_api(hass: HomeAssistant) -> None:
    """Register metadata only, with no secret submission or write handler."""
    websocket_api.async_register_command(hass, websocket_access_capabilities)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ttlock_ble/access_capabilities",
        vol.Required("device_id"): str,
    }
)
@websocket_api.require_admin
@callback
def websocket_access_capabilities(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Allow authenticated admins to inspect capabilities without triggering BLE."""
    lock = _connection_for_device(hass, msg["device_id"])
    connection.send_result(
        msg["id"],
        {
            "methods": access_capabilities(),
            "last_inventory": lock.inventory_diagnostics,
            "secret_entry": "not_implemented",
            "identity_mapping": "not_established",
            "new_credential_writes": "disabled",
        },
    )
