"""Private, bounded evidence for individually supervised credential attribution."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from time import monotonic
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from ttlock_ble import LogOperate

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from ttlock_ble import LogEntry

    from .access_records import AccessRecordStore

UNLOCK_METHODS: dict[int, str] = {
    LogOperate.KEYBOARD_PASSWORD_UNLOCK: "keypad_pin",
    LogOperate.FR_UNLOCK_SUCCEED: "fingerprint",
    LogOperate.IC_UNLOCK_SUCCEED: "ic_card",
}
RECORD_TYPES = {
    "keypad_pin": ("pin_type_1", "pin_type_3", "pin_type_4"),
    "fingerprint": ("fingerprint_record",),
    "ic_card": ("card_4byte",),
}
_MUTATIONS: dict[int, str] = {
    LogOperate.KEYBOARD_MODIFY_PASSWORD: "keypad_pin",
    LogOperate.KEYBOARD_REMOVE_SINGLE_PASSWORD: "keypad_pin",
    LogOperate.KEYBOARD_REMOVE_ALL_PASSWORDS: "keypad_pin",
    LogOperate.KEYBOARD_PASSWORD_KICKED: "keypad_pin",
    LogOperate.USE_DELETE_CODE: "keypad_pin",
    LogOperate.ADD_PASSCODE_SUCCESSFULLY: "keypad_pin",
    LogOperate.ADD_IC: "ic_card",
    LogOperate.DELETE_IC_SUCCEED: "ic_card",
    LogOperate.CLEAR_IC_SUCCEED: "ic_card",
    LogOperate.ADD_FR: "fingerprint",
    LogOperate.DELETE_FR_SUCCEED: "fingerprint",
    LogOperate.CLEAR_FR_SUCCEED: "fingerprint",
}
IDENTIFICATION_SECONDS = 90
PIN_MIN, PIN_MAX = 4, 9
FINGERPRINT_BYTES, CARD_BYTES = 6, 4
_SEQUENCE_MASK = 0xFFFF
MAX_EVENT_AGE = timedelta(seconds=120)
_HEX_LABEL = re.compile(r"(?<!\w)(?:[a-fA-F][ :.\-]*){7,}[a-fA-F](?!\w)")


def human_label(name: str) -> bool:
    """Keep numeric/hex-like secrets out of newly entered display labels."""
    return not any(char.isnumeric() for char in name) and not _HEX_LABEL.search(name)


def supported_identifier(method: str, identifier: bytes) -> bool:
    """Accept only exact SDK evidence; never parse an overloaded decimal field."""
    if method == "keypad_pin":
        return PIN_MIN <= len(identifier) <= PIN_MAX and identifier.isdigit()
    return (method == "fingerprint" and len(identifier) == FINGERPRINT_BYTES) or (
        method == "ic_card" and len(identifier) == CARD_BYTES
    )


def _now() -> datetime:
    return dt_util.now().replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class Match:
    """A private result; an exact match is not by itself verified evidence."""

    status: str
    reference: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class PublishedCredential:
    """Only approved label metadata can cross the Home Assistant event boundary."""

    label: str
    method: str


@dataclass(slots=True)
class _Identification:
    method: str
    reference: str = field(repr=False)
    owner: str = field(repr=False)
    nonce: str = field(repr=False)
    deadline: float
    cutoff: datetime | None = None
    observations: int = 0
    status: str = "preparing"
    checked: bool = False
    checking: bool = False


class CredentialAttribution:
    """Hold only opaque evidence in memory; reload returns to private and unverified."""

    def __init__(self, records: AccessRecordStore) -> None:
        """Share the inventory store's epochs and revocation boundary."""
        self._records = records
        self._sessions: dict[str, _Identification] = {}
        self._verified: dict[tuple[str, str, str], datetime] = {}
        self._publication: dict[str, datetime] = {}
        self._sequences: dict[str, tuple[int, int | None]] = {}

    def _active(self, lock: str) -> _Identification | None:
        session = self._sessions.get(lock)
        if session is not None and session.deadline <= monotonic():
            self._sessions.pop(lock)
            return None
        return session

    def _owned(self, lock: str, owner: str, nonce: str) -> _Identification:
        session = self._active(lock)
        if session is None or session.owner != owner or session.nonce != nonce:
            msg = "Identification expired, cancelled or belongs to another session"
            raise ValueError(msg)
        return session

    async def async_begin(
        self,
        lock: str,
        method: str,
        reference: str,
        owner: str,
        read_log: Callable[[], Awaitable[bool]],
    ) -> None:
        """Drain a complete bounded baseline before accepting a new unlock."""
        if self._active(lock) is not None or not self._records.attribution_eligible(
            lock, method, reference
        ):
            msg = "Identification busy or inventory stale or unsupported"
            raise ValueError(msg)
        session = _Identification(
            method,
            reference,
            owner,
            secrets.token_hex(16),
            monotonic() + IDENTIFICATION_SECONDS,
        )
        self._sessions[lock] = session
        armed = False
        try:
            if not await read_log() or self._active(lock) is not session:
                msg = "Complete baseline unavailable; read again"
                raise ValueError(msg)
            if not self._records.attribution_eligible(lock, method, reference):
                msg = "Inventory stale or unsupported"
                raise ValueError(msg)
            session.cutoff = _now()
            session.status = "waiting"
            session.deadline = monotonic() + IDENTIFICATION_SECONDS
            armed = True
        finally:
            if not armed and self._sessions.get(lock) is session:
                self._sessions.pop(lock)

    async def async_check(
        self, lock: str, owner: str, nonce: str, read_log: Callable[[], Awaitable[bool]]
    ) -> None:
        """Require a complete post-operation read, with no success on cancellation."""
        session = self._owned(lock, owner, nonce)
        if session.cutoff is None or session.checking:
            msg = "Identification not ready"
            raise ValueError(msg)
        session.checked = False
        session.checking = True
        try:
            session.checked = await read_log()
        finally:
            session.checking = False
        if not session.checked:
            msg = "Complete identification log read unavailable"
            raise ValueError(msg)

    def cancel(self, lock: str, owner: str, nonce: str) -> None:
        """Cancel only the caller's current challenge."""
        self._owned(lock, owner, nonce)
        self._sessions.pop(lock)

    def confirm(self, lock: str, owner: str, nonce: str) -> None:
        """Activate evidence only on explicit admin confirmation of one exact match."""
        session = self._owned(lock, owner, nonce)
        if (
            not session.checked
            or session.checking
            or session.status != "matched"
            or not self._records.attribution_eligible(
                lock, session.method, session.reference
            )
        ):
            msg = "No unique current credential evidence to confirm"
            raise ValueError(msg)
        self._records.require_label_review(session.reference)
        self._verified[(lock, session.method, session.reference)] = _now()
        self._sessions.pop(lock)

    def revoke(self, lock: str, method: str) -> None:
        """Drop observations and grants on stale reads, mutations and resets."""
        session = self._sessions.get(lock)
        if session is not None and session.method == method:
            self._sessions.pop(lock)
        for key in tuple(self._verified):
            if key[:2] == (lock, method):
                del self._verified[key]

    def close(self) -> None:
        """Discard all live evidence and disclosure permission on unload."""
        self._sessions.clear()
        self._verified.clear()
        self._publication.clear()
        self._sequences.clear()

    def note_unseen(self, lock: str, entry: LogEntry) -> None:
        """Revoke on newly seen changes even if the record is historical."""
        self._note_sequence(lock, entry.record_number)
        kind = entry.record_type
        method = _MUTATIONS.get(kind)
        if method is not None:
            self._records.mark_stale(lock, method)
        elif kind == LogOperate.DOOR_REBOOT:
            for method in RECORD_TYPES:
                self._records.mark_stale(lock, method)
        elif kind in UNLOCK_METHODS and (
            entry.operate_date is None or entry.operate_date > _now()
        ):
            session = self._active(lock)
            if session is not None and session.cutoff is not None:
                session.checked = False
                session.observations += 1
                session.status = "unsupported"

    def _note_sequence(self, lock: str, sequence: int) -> None:
        session = self._active(lock)
        if not (session is not None and session.cutoff is not None) and not any(
            key[0] == lock for key in self._verified
        ):
            self._sequences.pop(lock, None)
            return
        current = sequence & _SEQUENCE_MASK
        previous = self._sequences.get(lock)
        direction = None
        if previous is not None:
            last, known_direction = previous
            direction = (current - last) & _SEQUENCE_MASK
            if direction not in {1, _SEQUENCE_MASK} or known_direction not in {
                None,
                direction,
            }:
                self.note_incomplete(lock)
                return
        self._sequences[lock] = (current, direction)

    def note_incomplete(self, lock: str) -> None:
        """Revoke when an incomplete read may hide another event or mutation."""
        self._sequences.pop(lock, None)
        for method in RECORD_TYPES:
            self.revoke(lock, method)

    def set_publication(self, lock: str, *, enabled: bool) -> None:
        """Opt in only prospectively and only for this config-entry lifetime."""
        if enabled:
            self._publication[lock] = _now()
        else:
            self._publication.pop(lock, None)

    def observe(self, lock: str, entry: LogEntry) -> PublishedCredential | None:
        """Consume only replay-filtered successful unlock evidence, synchronously."""
        method = UNLOCK_METHODS.get(entry.record_type)
        when = entry.operate_date
        now = _now()
        if method is None or when is None or not now - MAX_EVENT_AGE <= when <= now:
            return None
        identifier = entry.credential_identifier
        match = (
            self._records.match_credential(lock, method, identifier)
            if identifier is not None
            else Match("unsupported")
        )
        session = self._active(lock)
        if session is not None and session.cutoff and when > session.cutoff:
            session.checked = False
            session.observations += 1
            session.status = (
                "ambiguous"
                if session.observations > 1
                else "matched"
                if method == session.method and match.reference == session.reference
                else "unmatched"
                if match.status == "matched"
                else match.status
            )
            return None  # supervised verification is private, even when sharing is on
        if match.reference is None:
            return None
        verified = self._verified.get((lock, method, match.reference))
        consent = self._publication.get(lock)
        if verified is None or consent is None or when <= max(verified, consent):
            return None
        name = self._records.attribution_label(lock, method, match.reference)
        return PublishedCredential(name, method) if name and human_label(name) else None

    def snapshot(self, lock: str, method: str, owner: str) -> dict[str, object]:
        """Admin-only status and opaque handles, never a source identifier."""
        session = self._active(lock)
        identification: dict[str, object] | None = None
        if session is not None:
            identification = {"status": "busy"}
            if session.owner == owner:
                identification = {
                    "session": session.nonce,
                    "method": session.method,
                    "reference": session.reference,
                    "status": session.status,
                    "can_confirm": session.checked
                    and not session.checking
                    and session.status == "matched",
                    "seconds_remaining": max(0, int(session.deadline - monotonic())),
                }
        return {
            "identification": identification,
            "publication_enabled": lock in self._publication,
            "verified_references": [
                ref
                for mac, kind, ref in self._verified
                if mac == lock
                and kind == method
                and self._records.attribution_eligible(lock, method, ref)
            ],
        }
