"""The sole credential-free projection allowed across the log dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from ttlock_ble import LogEntry

    from .attribution import PublishedCredential


@dataclass(frozen=True, slots=True)
class PublicLogRecord:
    """No SDK identifiers, raw frames, private references or access tokens."""

    record_number: int
    record_type: int
    operate_date: datetime | None
    lock_battery: int
    accessory_battery: int | None = None
    attribution: PublishedCredential | None = None

    @classmethod
    def from_entry(
        cls, entry: LogEntry, attribution: PublishedCredential | None = None
    ) -> PublicLogRecord:
        """Copy only harmless fields from the private SDK record."""
        return cls(
            entry.record_number,
            entry.record_type,
            entry.operate_date,
            entry.lock_battery,
            entry.accessory_battery,
            attribution,
        )
