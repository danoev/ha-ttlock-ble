"""Upgrade keeps the RC16 config entry, entity IDs and private metadata schema."""

from unittest.mock import patch

from homeassistant.helpers import entity_registry as er

from custom_components.ttlock_ble.connection import TtlockBleConnection
from custom_components.ttlock_ble.const import DOMAIN
from custom_components.ttlock_ble.management_http import STATIC_URL
from tests.test_pin_reconciliation import pin_inventory


async def test_reload_preserves_entry_entities_labels_and_legacy_optout(
    hass, setup_integration
):
    entry = setup_integration
    registry = er.async_get(hass)
    before = {
        (e.entity_id, e.unique_id)
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert len(before) == 5
    config = (entry.entry_id, entry.unique_id, entry.version, entry.data)
    store = entry.runtime_data.access_records
    await store.async_reconcile(
        "AA:BB:CC:DD:EE:FF", "keypad_pin", pin_inventory(), cleaned=True
    )
    ref = store.snapshot("AA:BB:CC:DD:EE:FF", "keypad_pin")["records"][0]["reference"]
    await store.async_name("AA:BB:CC:DD:EE:FF", "keypad_pin", ref, "Existing label")
    await store.async_set_legacy_disabled("AA:BB:CC:DD:EE:FF", disabled=True)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.domain == DOMAIN == "ttlock_ble"
    assert (entry.entry_id, entry.unique_id, entry.version, entry.data) == config
    assert {
        (e.entity_id, e.unique_id)
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    } == before
    state = entry.runtime_data.access_records.snapshot(
        "AA:BB:CC:DD:EE:FF", "keypad_pin"
    )
    assert state["records"][0]["reference"] == ref
    assert state["records"][0]["name"] == "Existing label"
    assert not state["fresh"]
    assert state["legacy_pin_writes_disabled"]
    assert STATIC_URL == "/ttlock-ble-admin/3.5.1.js"


async def test_missing_newer_bluetooth_diagnostics_cannot_break_setup(
    hass, sample_virtual_key
):
    connection = TtlockBleConnection(hass, sample_virtual_key)
    with patch(
        "custom_components.ttlock_ble.connection.BluetoothReachabilityIntent", None
    ):
        connection._record_reachability_diagnostic("no_route", 0)
    assert connection._reachability_diagnostic is None
    assert connection._client is None
