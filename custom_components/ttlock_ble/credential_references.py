"""Private reference groundwork; never persist a raw credential identifier."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.helpers import storage

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

METHODS = frozenset({"keypad_pin", "fingerprint", "ic_card", "fob"})
REFERENCE_KEY_BYTES = 32


@dataclass(frozen=True, slots=True)
class ProtectedCredentialReference:
    """An opaque local handle conveys neither credential data nor person identity."""

    token: str = field(repr=False)
    method: str
    identity_verified: bool = False


class ProtectedCredentialReferences:
    """
    Private HMAC key storage, used only by the separate admin metadata store.

    The admin mapping flow derives a lock/method/epoch-bound handle from
    verified evidence in memory. Raw IDs/PINs are never persisted. Host admins and
    backups can access the private key; this is not encryption from the HA host.
    Names are explicit private labels in AccessRecordStore; no identity binding.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Configure lazy, atomic, private storage scoped to one config entry."""
        self._store: storage.Store[dict[str, str]] = storage.Store(
            hass,
            1,
            f"ttlock_ble.credential_references.{entry_id}",
            private=True,
            atomic_writes=True,
        )
        self._key: bytes | None = None
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load or create a private key without accepting any live credentials."""
        async with self._lock:
            if self._key is not None:
                return
            await self._async_load_locked()

    async def _async_load_locked(self) -> None:
        data = await self._store.async_load()
        if data is None:
            key = secrets.token_bytes(REFERENCE_KEY_BYTES)
            await self._store.async_save({"key": key.hex()})
            self._key = key
            return
        try:
            key = bytes.fromhex(data["key"])
        except KeyError, ValueError, TypeError:
            msg = "Protected credential reference storage is invalid"
            raise ValueError(msg) from None
        if len(key) != REFERENCE_KEY_BYTES:
            msg = "Protected credential reference storage is invalid"
            raise ValueError(msg)
        self._key = key

    def from_verified_record(
        self, lock: str, method: str, epoch: str, identifier: bytes
    ) -> ProtectedCredentialReference:
        """
        Derive an opaque handle; callers must establish record authenticity.

        This does not assert a person's identity or enable any lock operation.
        Epoch must change after reset/re-enrolment; refs are not authorization.
        """
        if self._key is None or method not in METHODS or not epoch or not identifier:
            msg = "Protected credential reference cannot be derived"
            raise ValueError(msg)
        parts = (lock.encode(), method.encode(), epoch.encode(), identifier)
        material = b"".join(len(part).to_bytes(4, "big") + part for part in parts)
        token = hmac.new(self._key, material, hashlib.sha256).hexdigest()
        return ProtectedCredentialReference(token, method)

    async def async_remove(self) -> None:
        """Forget the key on explicit future mapping removal/config-entry deletion."""
        async with self._lock:
            self._key = None
            await self._store.async_remove()
