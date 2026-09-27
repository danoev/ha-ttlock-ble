"""Conservative, persistent operation-log replay protection."""

from __future__ import annotations

from collections import deque
from hashlib import sha256
from typing import TYPE_CHECKING

from homeassistant.helpers import storage
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    import datetime as dt

    from homeassistant.core import HomeAssistant

    from ttlock_ble import LogEntry


STORAGE_VERSION = 1
MAX_PERSISTED_IDENTITIES_PER_LOCK = 512


class TtlockBleLogHistory:
    """Classify operation records without replaying historical activity."""

    def __init__(self, hass: HomeAssistant, entry_id: str | None = None) -> None:
        """Capture this load's local-time boundary and configure optional storage."""
        self._startup_cutoff: dt.datetime = dt_util.now().replace(tzinfo=None)
        self._recent: dict[str, deque[str]] = {}
        self._recent_sets: dict[str, set[str]] = {}
        self._store: storage.Store[dict[str, object]] | None = (
            storage.Store(
                hass,
                STORAGE_VERSION,
                f"{DOMAIN}.operation_log.{entry_id}",
                private=True,
                atomic_writes=True,
            )
            if entry_id is not None
            else None
        )

    async def async_load(self) -> None:
        """Load the bounded replay journal, ignoring malformed legacy data."""
        if self._store is None:
            return
        stored = await self._store.async_load()
        if stored is None:
            return
        locks = stored.get("locks")
        if not isinstance(locks, dict):
            return
        for mac, identities in locks.items():
            if not isinstance(mac, str) or not isinstance(identities, list):
                continue
            safe_identities = [item for item in identities if isinstance(item, str)][
                -MAX_PERSISTED_IDENTITIES_PER_LOCK:
            ]
            self._recent[mac] = deque(
                safe_identities,
                maxlen=MAX_PERSISTED_IDENTITIES_PER_LOCK,
            )
            self._recent_sets[mac] = set(safe_identities)

    def classify(self, mac: str, entries: list[LogEntry]) -> list[LogEntry]:
        """Return definitely post-start unseen records and journal every identity."""
        recent = self._recent.setdefault(
            mac,
            deque(maxlen=MAX_PERSISTED_IDENTITIES_PER_LOCK),
        )
        recent_set = self._recent_sets.setdefault(mac, set(recent))
        live: list[LogEntry] = []
        changed = False
        suppressed_history = 0
        suppressed_duplicate = 0
        for entry in entries:
            identity = _record_identity(entry)
            if identity in recent_set:
                suppressed_duplicate += 1
                continue
            if len(recent) == MAX_PERSISTED_IDENTITIES_PER_LOCK:
                evicted = recent[0]
                recent_set.discard(evicted)
            recent.append(identity)
            recent_set.add(identity)
            changed = True
            if (
                entry.operate_date is not None
                and entry.operate_date > self._startup_cutoff
            ):
                live.append(entry)
            else:
                suppressed_history += 1
        if changed and self._store is not None:
            self._store.async_delay_save(self._serialize, delay=5)
        LOGGER.debug(
            "Operation-log classification for %s: fetched=%d, live=%d, "
            "historical_or_ambiguous=%d, duplicate=%d, journal=%d",
            mac,
            len(entries),
            len(live),
            suppressed_history,
            suppressed_duplicate,
            len(recent),
        )
        return live

    async def async_save(self) -> None:
        """Flush the current bounded journal during config-entry unload."""
        if self._store is not None:
            await self._store.async_save(self._serialize())

    def _serialize(self) -> dict[str, object]:
        """Build the non-secret JSON representation."""
        return {"locks": {mac: list(entries) for mac, entries in self._recent.items()}}


def _record_identity(entry: LogEntry) -> str:
    """Hash stable non-secret fields so sequence wrap does not create a collision."""
    safe_parts = (
        entry.record_number,
        int(entry.record_type),
        entry.operate_date.isoformat() if entry.operate_date is not None else None,
        entry.uid,
        entry.record_id,
        entry.key_id,
    )
    return sha256(repr(safe_parts).encode()).hexdigest()
