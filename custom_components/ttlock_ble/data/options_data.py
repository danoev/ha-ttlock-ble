"""Shape of the options writable by the options flow."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class TtlockBleOptionsData(TypedDict, total=False):
    """Shape of the options writable by the options flow."""

    scan_interval: NotRequired[int]
    reconnect_interval: NotRequired[int]
    background_maintenance: NotRequired[bool]
    permanent_connection: NotRequired[bool]
