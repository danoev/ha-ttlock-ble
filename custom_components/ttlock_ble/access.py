"""Closed failure vocabulary and capability metadata for local administration."""

from __future__ import annotations

from enum import StrEnum

from ttlock_ble import TTLockError


class AccessFailure(StrEnum):
    """Safe integration-owned classifications; no device exception text."""

    BUSY = "connection_busy"
    PREEMPTED = "inventory_preempted"
    CANCELLED = "inventory_cancelled"
    NO_CANDIDATE = "no_advertisement_candidate"
    DISCOVERY_TIMEOUT = "bluetooth_discovery_timeout"
    GATT = "gatt_connection_failed"
    DEADLINE = "read_deadline_exceeded"
    CLEANUP = "cleanup_uncertain"
    UNKNOWN = "read_failed"


class AccessInventoryError(TTLockError):
    """Stable error metadata that is safe for ordinary HA responses."""

    def __init__(self, reason: AccessFailure, stage: str) -> None:
        """Keep the exception and all exposed fields free of credential data."""
        self.reason = reason
        self.stage = stage
        super().__init__(reason.value)


def access_capabilities() -> dict[str, dict[str, str]]:
    """Distinguish implementation support from evidence on a particular lock."""
    return {
        method: {
            "read": "partially_discoverable"
            if method == "keypad_pin"
            else "implemented",
            "device_support": "requires_read_confirmation",
            "s31_read_evidence": "partial"
            if method == "keypad_pin"
            else "count_corroborated",
            "identity": "unverified",
            "schedule_coverage": "incomplete",
            "create": "legacy_pin_action" if method == "keypad_pin" else "disabled",
            "modify": "disabled",
            "delete": "legacy_pin_action" if method == "keypad_pin" else "disabled",
        }
        for method in ("keypad_pin", "fingerprint", "ic_card")
    }
