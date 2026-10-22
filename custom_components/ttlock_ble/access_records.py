"""Private, lifecycle-aware aliases; credentials are never persisted here."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import asdict, dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.helpers import storage

from .credential_references import ProtectedCredentialReferences

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ttlock_ble import AdministrativeInventory

METHODS = frozenset({"keypad_pin", "fingerprint", "ic_card"})
FRESH_SECONDS = 300
MAX_RECORDS = 512
MAX_NAME = 64
SPACE_ORDINAL = 32
_HEX = vol.Match(r"^[0-9a-f]{32,64}$")
_RECORD = vol.Schema(
    {
        vol.Required("base"): _HEX,
        vol.Required("reference"): _HEX,
        vol.Required("record_type"): vol.In(
            {
                "pin_type_1",
                "pin_type_3",
                "pin_type_4",
                "fingerprint_record",
                "card_4byte",
            }
        ),
        vol.Required("validity"): vol.In(
            {"permanent", "time_window", "cyclic", "unknown"}
        ),
        vol.Required("alias"): vol.All(str, vol.Length(max=64)),
        vol.Required("active"): bool,
    }
)
_SCOPE = vol.Schema(
    {
        vol.Required("epoch"): _HEX,
        vol.Required("records"): vol.All([_RECORD], vol.Length(max=MAX_RECORDS)),
    }
)
_DATA = vol.Schema(
    {vol.Required("scopes"): {str: _SCOPE}, vol.Required("legacy_disabled"): [str]}
)


@dataclass
class PrivateRecord:
    """Pseudonymous local record; alias is an admin label, never identity."""

    base: str
    reference: str
    record_type: str
    validity: str
    alias: str = ""
    active: bool = True


class AccessRecordStore:
    """Entry-specific atomic metadata, never an authorization or write token."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Create stores without loading any device or key material."""
        self._refs = ProtectedCredentialReferences(hass, entry_id)
        self._store: storage.Store[dict[str, Any]] = storage.Store(
            hass,
            1,
            f"ttlock_ble.access_records.{entry_id}",
            private=True,
            atomic_writes=True,
        )
        self._lock = asyncio.Lock()
        self._loaded = False
        self._scopes: dict[str, dict[str, Any]] = {}
        self._legacy_disabled: set[str] = set()
        self._fresh: dict[str, float] = {}
        self._revision: dict[str, int] = {}
        self._confirmed: set[str] = set()

    async def async_load(self) -> None:
        """Restart is stale; malformed storage fails closed without data echo."""
        async with self._lock:
            if self._loaded:
                return
            data = await self._store.async_load()
            if data is not None:
                try:
                    valid = _DATA(data)
                except vol.Invalid:
                    msg = "Private access metadata is invalid"
                    raise ValueError(msg) from None
                self._scopes = valid["scopes"]
                self._legacy_disabled = set(valid["legacy_disabled"])
            await self._refs.async_load()
            self._loaded = True

    @staticmethod
    def _scope(lock: str, method: str) -> str:
        if method not in METHODS:
            msg = "Unsupported access method"
            raise ValueError(msg)
        return f"{lock}/{method}"

    def mark_stale(self, lock: str, method: str) -> int:
        """Invalidate older reads as well as freshness; return an operation ticket."""
        scope = self._scope(lock, method)
        self._fresh.pop(scope, None)
        self._revision[scope] = self._revision.get(scope, 0) + 1
        return self._revision[scope]

    def legacy_disabled(self, lock: str) -> bool:
        """Explicit opt-out preserves compatibility until an admin disables it."""
        return lock in self._legacy_disabled

    async def async_set_legacy_disabled(self, lock: str, *, disabled: bool) -> None:
        """Only the authenticated admin HTTP handler calls this local setting."""
        async with self._lock:
            updated = (
                self._legacy_disabled | {lock}
                if disabled
                else self._legacy_disabled - {lock}
            )
            await self._store.async_save(
                {"scopes": self._scopes, "legacy_disabled": sorted(updated)}
            )
            self._legacy_disabled = updated

    async def async_reconcile(
        self,
        lock: str,
        method: str,
        inventory: AdministrativeInventory,
        *,
        cleaned: bool,
        revision: int | None = None,
    ) -> None:
        """Complete current reads reconcile presence and retire missing references."""
        scope = self._scope(lock, method)
        if revision is None:
            revision = self.mark_stale(lock, method)
        if not self._loaded or revision != self._revision.get(scope):
            return
        if not cleaned or not inventory.records_complete:
            return
        if any(r.method != method for r in inventory.records):
            msg = "Private inventory method mismatch"
            raise ValueError(msg)
        if len(inventory.records) > MAX_RECORDS:
            msg = "Private inventory capacity exceeded"
            raise ValueError(msg)
        async with self._lock:
            if revision != self._revision.get(scope):
                return
            state = self._scopes.setdefault(
                scope, {"epoch": secrets.token_hex(16), "records": []}
            )
            records = [PrivateRecord(**item) for item in state["records"]]
            active = {r.base: r for r in records if r.active}
            seen = set()
            for item in inventory.records:
                # Include record type in the HMAC domain. Never persist the source ID.
                base = self._refs.from_verified_record(
                    lock,
                    method,
                    state["epoch"] + "/" + item.record_type,
                    item.identifier,
                ).token
                seen.add(base)
                record = active.get(base)
                if record is None:
                    record = PrivateRecord(
                        base, secrets.token_hex(16), item.record_type, item.validity
                    )
                    records.append(record)
                record.validity = item.validity
            for record in records:
                if record.base not in seen:
                    record.active = False
                    self._confirmed.discard(record.reference)
            # Retain missing references; cap growth without reusing aliases.
            if len(records) > MAX_RECORDS:
                msg = "Private inventory capacity exceeded; reset local references"
                raise ValueError(msg)
            state["records"] = [asdict(r) for r in records]
            await self._save()
            if revision == self._revision.get(scope):
                self._fresh[scope] = monotonic()

    def snapshot(self, lock: str, method: str) -> dict[str, Any]:
        """Explicit safe projection; no generic SDK/dataclass serialization."""
        scope = self._scope(lock, method)
        fresh = (
            scope in self._fresh and monotonic() - self._fresh[scope] < FRESH_SECONDS
        )
        state = self._scopes.get(scope, {"records": []})
        return {
            "fresh": fresh,
            "records": [
                {
                    "reference": r["reference"],
                    "name": r["alias"],
                    "record_type": r["record_type"],
                    "validity": r["validity"],
                    "schedule_verified": False,
                    "identity_verified": False,
                    "present": r["active"],
                    "stale": not fresh or not r["active"],
                    "alias_confirmed_this_session": r["reference"] in self._confirmed,
                }
                for r in state["records"]
            ],
            "legacy_pin_writes_disabled": self.legacy_disabled(lock),
        }

    async def async_name(
        self, lock: str, method: str, reference: str, name: str
    ) -> None:
        """Rename only a fresh active reference in this lock/method scope."""
        if len(name) > MAX_NAME or any(ord(c) < SPACE_ORDINAL for c in name):
            msg = "Invalid display name"
            raise ValueError(msg)
        scope = self._scope(lock, method)
        async with self._lock:
            snapshot = self.snapshot(lock, method)
            if not snapshot["fresh"]:
                msg = "Read a fresh inventory before assigning a name"
                raise ValueError(msg)
            for record in self._scopes[scope]["records"]:
                if record["reference"] == reference and record["active"]:
                    record["alias"] = name.strip()
                    await self._save()
                    self._confirmed.add(reference)
                    return
            msg = "Reference is stale or belongs to a different scope"
            raise ValueError(msg)

    async def async_reset(self, lock: str, method: str) -> None:
        """Explicit local forget only: no BLE deletion or household identity effects."""
        scope = self._scope(lock, method)
        async with self._lock:
            state = self._scopes.pop(scope, None)
            if state:
                self._confirmed.difference_update(
                    r["reference"] for r in state["records"]
                )
            self.mark_stale(lock, method)
            await self._save()

    async def _save(self) -> None:
        await self._store.async_save(
            {"scopes": self._scopes, "legacy_disabled": sorted(self._legacy_disabled)}
        )

    async def async_remove(self) -> None:
        """Config-entry deletion forgets aliases and private key, without BLE I/O."""
        async with self._lock:
            self._revision = {
                scope: revision + 1 for scope, revision in self._revision.items()
            }
            self._fresh.clear()
            self._confirmed.clear()
            self._scopes.clear()
            self._legacy_disabled.clear()
            self._loaded = False
            await self._store.async_remove()
            await self._refs.async_remove()
