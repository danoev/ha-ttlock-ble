"""Match HA's schema engine while retaining the declared older HA floor."""

from typing import TYPE_CHECKING

from homeassistant.const import MAJOR_VERSION, MINOR_VERSION

if TYPE_CHECKING or (MAJOR_VERSION, MINOR_VERSION) >= (2026, 10):
    import probatio as vol
else:
    import voluptuous as vol

__all__ = ["vol"]
