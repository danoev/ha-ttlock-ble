"""Admin-only local metadata HTTP boundary, outside service/WS message logging."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yarl
from aiohttp import web
from homeassistant.auth.models import TOKEN_TYPE_NORMAL
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.components.http.const import KEY_HASS_REFRESH_TOKEN_ID, KEY_HASS_USER
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.config_entries import ConfigEntryState

from .access import access_capabilities
from .const import DOMAIN
from .services import _inventory_response
from .validation import vol

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .access_records import AccessRecordStore
    from .connection import TtlockBleConnection
    from .data import TtlockBleConfigEntry

URL = "/api/ttlock_ble/administration"
STATIC_URL = "/ttlock-ble-admin/3.5.2.js"
_BODY_LIMIT = 4096
_ID = vol.All(str, vol.Length(min=1, max=128))
_BASE = {
    vol.Required("action"): str,
    vol.Required("entry_id"): _ID,
    vol.Required("lock"): _ID,
}
_METHOD = vol.In({"keypad_pin", "fingerprint", "ic_card"})
_SCHEMAS = {
    "list": vol.Schema({vol.Required("action"): vol.Equal("list")}),
    "state": vol.Schema({**_BASE, vol.Required("method"): _METHOD}),
    "read": vol.Schema({**_BASE, vol.Required("method"): _METHOD}),
    "name": vol.Schema(
        {
            **_BASE,
            vol.Required("method"): _METHOD,
            vol.Required("reference"): vol.Match(r"^[0-9a-f]{32}$"),
            vol.Required("name"): vol.All(str, vol.Length(max=64)),
        }
    ),
    "reset": vol.Schema(
        {
            **_BASE,
            vol.Required("method"): _METHOD,
            vol.Required("confirm"): vol.Equal("forget_local_labels"),
        }
    ),
    "legacy": vol.Schema(
        {**_BASE, vol.Required("method"): _METHOD, vol.Required("disabled"): bool}
    ),
}


def _response(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(
        data,
        status=status,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _same_origin(request: web.Request) -> bool:
    """Compare HA-normalized authority, including equivalent default ports."""
    try:
        origin = yarl.URL(request.headers.get("Origin", ""))
        expected = request.url
        return origin == origin.origin() and (
            origin.scheme,
            origin.raw_host,
            origin.port,
        ) == (expected.scheme, expected.raw_host, expected.port)
    except ValueError:
        return False


class AdministrationView(HomeAssistantView):
    """Normal active admin session required on every request, never opaque-ref auth."""

    url = URL
    name = "api:ttlock_ble:administration"
    requires_auth = True
    cors_allowed = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Retain only HA, not requests, tokens or SDK records."""
        self._hass = hass

    def _authorized(self, request: web.Request) -> bool:
        user = request.get(KEY_HASS_USER)
        token_id = request.get(KEY_HASS_REFRESH_TOKEN_ID)
        token = self._hass.auth.async_get_refresh_token(token_id) if token_id else None
        return bool(
            user
            and user.is_active
            and user.is_admin
            and not user.system_generated
            and token
            and token.user.id == user.id
            and token.token_type == TOKEN_TYPE_NORMAL
            and request.headers.get("Authorization", "").startswith("Bearer ")
            and not request.query
            and _same_origin(request)
            and request.headers.get("X-TTLock-Admin") == "1"
            and request.headers.get("Sec-Fetch-Site", "same-origin") == "same-origin"
        )

    async def post(self, request: web.Request) -> web.Response:
        """Validate a small metadata-only request; never return raw exception text."""
        if not self._authorized(request):
            return _response({"error": "admin_session_required"}, 403)
        try:
            async with asyncio.timeout(3):
                data = await self._body(request)
            async with asyncio.timeout(40):
                return _response(await self._dispatch(data))
        except ValueError, vol.Invalid, TimeoutError:
            return _response({"error": "invalid_request_or_stale_reference"}, 400)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # Host/proxy request-body logging is outside this integration's boundary.
            return _response({"error": "administration_failed_read_again"}, 503)

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        if (
            request.content_type != "application/json"
            or (request.content_length or 0) > _BODY_LIMIT
        ):
            msg = "Invalid administration request"
            raise ValueError(msg)
        raw = bytearray()
        while chunk := await request.content.read(_BODY_LIMIT + 1 - len(raw)):
            raw.extend(chunk)
            if len(raw) > _BODY_LIMIT:
                msg = "Invalid administration request"
                raise ValueError(msg)
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("action") in _SCHEMAS:
                return cast("dict[str, Any]", _SCHEMAS[data["action"]](data))
        except ValueError, TypeError, vol.Invalid:
            pass
        msg = "Invalid administration request"
        raise ValueError(msg) from None

    def _target(
        self, data: dict[str, Any]
    ) -> tuple[TtlockBleConnection, AccessRecordStore]:
        raw_entry = self._hass.config_entries.async_get_entry(data["entry_id"])
        if (
            raw_entry is None
            or raw_entry.domain != DOMAIN
            or raw_entry.state is not ConfigEntryState.LOADED
        ):
            msg = "Entry unavailable"
            raise ValueError(msg)
        entry = cast("TtlockBleConfigEntry", raw_entry)
        records = entry.runtime_data.access_records
        connection = entry.runtime_data.connections.get(data["lock"])
        if records is None or connection is None:
            msg = "Lock unavailable"
            raise ValueError(msg)
        return connection, records

    async def _dispatch(self, data: dict[str, Any]) -> dict[str, Any]:
        action = data["action"]
        if action == "list":
            return {
                "locks": [
                    {
                        "entry_id": e.entry_id,
                        "lock": mac,
                        "name": k.lockAlias or k.lockName,
                        "admin_key_available": e.runtime_data.connections[
                            mac
                        ].inventory_admin_available,
                    }
                    for raw in self._hass.config_entries.async_entries(DOMAIN)
                    if raw.state is ConfigEntryState.LOADED
                    for e in [cast("TtlockBleConfigEntry", raw)]
                    for k in e.runtime_data.virtual_keys
                    for mac in [k.lockMac]
                ],
                "capabilities": access_capabilities(),
                "new_writes": "disabled",
            }
        connection, records = self._target(data)
        lock, method = data["lock"], data.get("method", "keypad_pin")
        response: dict[str, Any] = {}
        if action == "read":
            revision = records.mark_stale(lock, method)
            inventory = await connection.async_get_administrative_inventory(method)
            await records.async_reconcile(
                lock,
                method,
                inventory,
                cleaned=connection.inventory_diagnostics.get("cleanup_confirmed")
                is True,
                revision=revision,
            )
            response["inventory"] = _inventory_response(method, inventory.summary)
            # Raw IDs go out of scope here, never into the response or storage.
            del inventory
        elif action == "name":
            await records.async_name(lock, method, data["reference"], data["name"])
        elif action == "reset":
            await records.async_reset(lock, method)
        elif action == "legacy":
            await records.async_set_legacy_disabled(lock, disabled=data["disabled"])
        return {**response, **records.snapshot(lock, method), "new_writes": "disabled"}


async def async_setup_management(hass: HomeAssistant) -> None:
    """Register local HTTP and, when frontend is loaded, its admin-only panel."""
    hass.http.register_view(AdministrationView(hass))
    if "frontend" not in hass.config.components:
        return
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                STATIC_URL,
                str(Path(__file__).parent / "frontend" / "administration.js"),
                cache_headers=False,
            )
        ]
    )
    await async_register_panel(
        hass,
        frontend_url_path="ttlock-access",
        webcomponent_name="ttlock-access-panel",
        sidebar_title="TTLock access",
        sidebar_icon="mdi:key-chain",
        module_url=STATIC_URL,
        require_admin=True,
    )
