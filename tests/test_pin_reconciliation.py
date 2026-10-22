"""Limited PIN enumeration is never evidence of physical revocation."""

from dataclasses import replace

import pytest
from ttlock_ble import (
    AdministrativeInventory,
    AdministrativeRecord,
    InventoryEntry,
    InventoryResult,
)

from custom_components.ttlock_ble.access_records import AccessRecordStore


def pin_inventory(*, empty=False):
    return AdministrativeInventory(
        InventoryResult(
            () if empty else (InventoryEntry("permanent"),),
            complete=True,
            limitation="Unused offline/batch PINs may be omitted",
        ),
        ()
        if empty
        else (
            AdministrativeRecord(
                "keypad_pin", "pin_type_1", "permanent", b"synthetic-pin"
            ),
        ),
    )


async def test_empty_complete_pin_read_preserves_reference_and_label(hass):
    store = AccessRecordStore(hass, "limited-pin")
    await store.async_load()
    await store.async_reconcile("lock", "keypad_pin", pin_inventory(), cleaned=True)
    record = store.snapshot("lock", "keypad_pin")["records"][0]
    await store.async_name("lock", "keypad_pin", record["reference"], "Spare PIN")
    await store.async_reconcile(
        "lock", "keypad_pin", pin_inventory(empty=True), cleaned=True
    )
    missing = store.snapshot("lock", "keypad_pin")["records"][0]
    assert missing["name"] == "Spare PIN"
    assert missing["stale"]
    assert missing["observation"] == "not_observed"
    assert missing["limited_pin_coverage"]
    assert not missing["revocation_verified"]
    with pytest.raises(ValueError, match="stale or belongs"):
        await store.async_name(
            "lock", "keypad_pin", record["reference"], "Not observed"
        )
    await store.async_reconcile("lock", "keypad_pin", pin_inventory(), cleaned=True)
    records = store.snapshot("lock", "keypad_pin")["records"]
    assert len(records) == 1
    assert records[0]["reference"] == record["reference"]
    assert records[0]["name"] == "Spare PIN"


@pytest.mark.parametrize("failure", ["partial", "transport", "cleanup"])
async def test_failed_pin_read_restart_and_reappearance_preserve_label(hass, failure):
    store = AccessRecordStore(hass, "limited-pin")
    await store.async_load()
    await store.async_reconcile("lock", "keypad_pin", pin_inventory(), cleaned=True)
    ref = store.snapshot("lock", "keypad_pin")["records"][0]["reference"]
    await store.async_name("lock", "keypad_pin", ref, "Stored label")
    result = pin_inventory(empty=True)
    result = replace(
        result,
        summary=replace(
            result.summary,
            complete=failure != "partial",
            transport_interrupted=failure == "transport",
        ),
    )
    await store.async_reconcile(
        "lock", "keypad_pin", result, cleaned=failure != "cleanup"
    )
    assert not store.snapshot("lock", "keypad_pin")["fresh"]
    restarted = AccessRecordStore(hass, "limited-pin")
    await restarted.async_load()
    await restarted.async_reconcile(
        "lock", "keypad_pin", pin_inventory(empty=True), cleaned=True
    )
    await restarted.async_reconcile("lock", "keypad_pin", pin_inventory(), cleaned=True)
    records = restarted.snapshot("lock", "keypad_pin")["records"]
    assert len(records) == 1
    assert records[0]["reference"] == ref
    assert records[0]["name"] == "Stored label"


async def test_old_pin_results_cannot_revive_after_failure_reset_or_reload(hass):
    store = AccessRecordStore(hass, "limited-pin")
    await store.async_load()
    await store.async_reconcile("lock", "keypad_pin", pin_inventory(), cleaned=True)
    original = store.snapshot("lock", "keypad_pin")["records"][0]["reference"]
    old = store.mark_stale("lock", "keypad_pin")
    store.mark_stale("lock", "keypad_pin")
    await store.async_reconcile(
        "lock", "keypad_pin", pin_inventory(), cleaned=True, revision=old
    )
    assert not store.snapshot("lock", "keypad_pin")["fresh"]
    old = store.mark_stale("lock", "keypad_pin")
    await store.async_reset("lock", "keypad_pin")
    await store.async_reconcile(
        "lock", "keypad_pin", pin_inventory(), cleaned=True, revision=old
    )
    assert not store.snapshot("lock", "keypad_pin")["records"]
    await store.async_reconcile("lock", "keypad_pin", pin_inventory(), cleaned=True)
    assert store.snapshot("lock", "keypad_pin")["records"][0]["reference"] != original
    old = store.mark_stale("lock", "keypad_pin")
    await store.async_remove()
    await store.async_load()
    await store.async_reconcile(
        "lock", "keypad_pin", pin_inventory(), cleaned=True, revision=old
    )
    assert not store.snapshot("lock", "keypad_pin")["fresh"]


async def test_rc16_retired_pin_reference_is_recovered_without_duplicate(
    hass, hass_storage
):
    store = AccessRecordStore(hass, "limited-pin")
    await store.async_load()
    await store.async_reconcile("lock", "keypad_pin", pin_inventory(), cleaned=True)
    original = store.snapshot("lock", "keypad_pin")["records"][0]["reference"]
    await store.async_name("lock", "keypad_pin", original, "RC16 label")
    # The unchanged v1 schema also accepts an RC16 record retired by an omission.
    data = hass_storage["ttlock_ble.access_records.limited-pin"]["data"]
    data["scopes"]["lock/keypad_pin"]["records"][0]["active"] = False
    upgraded = AccessRecordStore(hass, "limited-pin")
    await upgraded.async_load()
    assert not upgraded.snapshot("lock", "keypad_pin")["fresh"]
    await upgraded.async_reconcile("lock", "keypad_pin", pin_inventory(), cleaned=True)
    records = upgraded.snapshot("lock", "keypad_pin")["records"]
    assert len(records) == 1
    assert records[0]["reference"] == original
    assert records[0]["name"] == "RC16 label"
