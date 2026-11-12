"""Attributes published with a decoded operation-log event."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class TtlockBleLogEventAttributes(TypedDict):
    """
    Attributes published with a decoded operation-log event.

    The SDK's overloaded `password` field is deliberately absent. Depending
    on record type it can be a working passcode, card number, fob address, or
    identifier, and RC10 does not have an authoritative way to separate safe
    labels from usable credentials.
    """

    record_type: str
    method: str
    battery: int
    timestamp: NotRequired[str]
    credential_label: NotRequired[str]
    credential_method: NotRequired[str]
    attribution_status: NotRequired[str]
    accessory_battery: NotRequired[int]
