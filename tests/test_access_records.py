"""Private reference lifecycle without storing real or synthetic source credentials."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest
from ttlock_ble import (
    AdministrativeInventory,
    AdministrativeRecord,
    InventoryEntry,
    InventoryResult,
)

from custom_components.ttlock_ble.access_records import AccessRecordStore


def inventory(identifier=b"synthetic-private-card", *, complete=True):
    return AdministrativeInventory(
        InventoryResult((InventoryEntry("permanent"),), complete, ""),
        (AdministrativeRecord("ic_card", "card_4byte", "permanent", identifier),),
    )


async def test_restart_presence_reenrolment_reset_and_private_storage(
    hass, hass_storage
):
    store = AccessRecordStore(hass, "entry")
    await store.async_load()
    await store.async_load()
    await store.async_reconcile("lock-a", "ic_card", inventory(), cleaned=True)
    state = store.snapshot("lock-a", "ic_card")
    ref = state["records"][0]["reference"]
    await store.async_name("lock-a", "ic_card", ref, "Spare card")
    assert store.snapshot("lock-a", "ic_card")["records"][0][
        "alias_confirmed_this_session"
    ]
    assert "synthetic-private-card" not in repr(hass_storage) + repr(state)
    assert b"synthetic-private-card".hex() not in repr(hass_storage) + repr(state)
    assert "base" not in state["records"][0]
    restarted = AccessRecordStore(hass, "entry")
    await restarted.async_load()
    stale = restarted.snapshot("lock-a", "ic_card")
    assert not stale["fresh"]
    assert stale["records"][0]["name"] == "Spare card"
    assert not stale["records"][0]["alias_confirmed_this_session"]
    with pytest.raises(ValueError, match="fresh inventory"):
        await restarted.async_name("lock-a", "ic_card", ref, "Invalid")
    await restarted.async_reconcile("lock-a", "ic_card", inventory(), cleaned=True)
    assert restarted.snapshot("lock-a", "ic_card")["records"][0]["reference"] == ref
    empty = AdministrativeInventory(
        InventoryResult((), complete=True, limitation=""), ()
    )
    await restarted.async_reconcile("lock-a", "ic_card", empty, cleaned=True)
    assert not restarted.snapshot("lock-a", "ic_card")["records"][0]["present"]
    await restarted.async_reconcile("lock-a", "ic_card", inventory(), cleaned=True)
    records = restarted.snapshot("lock-a", "ic_card")["records"]
    assert records[-1]["reference"] != ref
    assert records[-1]["name"] == ""
    assert records[0]["stale"]
    await restarted.async_reset("lock-a", "ic_card")
    await restarted.async_reconcile("lock-a", "ic_card", inventory(), cleaned=True)
    assert restarted.snapshot("lock-a", "ic_card")["records"][0]["reference"] not in {
        r["reference"] for r in records
    }
    await restarted.async_remove()
    assert not restarted.snapshot("lock-a", "ic_card")["records"]


@pytest.mark.parametrize(
    "failure", ["partial", "transport", "cleanup", "duplicate", "missing_record"]
)
async def test_failed_reconciliation_retains_labels_but_never_fresh(hass, failure):
    store = AccessRecordStore(hass, "entry")
    await store.async_load()
    result = inventory()
    await store.async_reconcile("a", "ic_card", result, cleaned=True)
    ref = store.snapshot("a", "ic_card")["records"][0]["reference"]
    await store.async_name("a", "ic_card", ref, "Spare")
    if failure == "partial":
        result = replace(result, summary=replace(result.summary, complete=False))
    if failure == "transport":
        result = replace(
            result, summary=replace(result.summary, transport_interrupted=True)
        )
    if failure == "duplicate":
        result = replace(result, records=result.records * 2)
    if failure == "missing_record":
        result = replace(result, records=())
    await store.async_reconcile("a", "ic_card", result, cleaned=failure != "cleanup")
    state = store.snapshot("a", "ic_card")
    assert not state["fresh"]
    assert state["records"][0]["name"] == "Spare"
    with pytest.raises(ValueError, match="fresh inventory"):
        await store.async_name("a", "ic_card", ref, "Should fail")


async def test_lock_method_scope_expiry_validation_and_legacy_optout(hass):
    store = AccessRecordStore(hass, "entry")
    await store.async_load()
    await store.async_reconcile("a", "ic_card", inventory(), cleaned=True)
    ref = store.snapshot("a", "ic_card")["records"][0]["reference"]
    await store.async_reconcile("b", "ic_card", inventory(), cleaned=True)
    assert store.snapshot("b", "ic_card")["records"][0]["reference"] != ref
    with pytest.raises(ValueError, match="different scope"):
        await store.async_name("b", "ic_card", ref, "Wrong lock")
    for name in ("x" * 65, "bad\x00label"):
        with pytest.raises(ValueError, match="Invalid display"):
            await store.async_name("a", "ic_card", ref, name)
    with (
        patch(
            "custom_components.ttlock_ble.access_records.monotonic", return_value=10**12
        ),
        pytest.raises(ValueError, match="fresh inventory"),
    ):
        await store.async_name("a", "ic_card", ref, "Expired")
    assert not store.legacy_disabled("a")
    await store.async_set_legacy_disabled("a", disabled=True)
    loaded = AccessRecordStore(hass, "entry")
    await loaded.async_load()
    assert loaded.legacy_disabled("a")
    assert not loaded.legacy_disabled("b")
    await loaded.async_set_legacy_disabled("a", disabled=False)
    assert not loaded.legacy_disabled("a")
    with pytest.raises(ValueError, match="Unsupported"):
        store.snapshot("a", "unverified")


async def test_corrupt_private_mapping_fails_closed_with_fixed_error(
    hass, hass_storage
):
    hass_storage["ttlock_ble.access_records.entry"] = {
        "version": 1,
        "data": {"secret": "must-not-echo"},
    }
    with pytest.raises(ValueError, match="metadata is invalid") as error:
        await AccessRecordStore(hass, "entry").async_load()
    assert "must-not-echo" not in str(error.value)


async def test_wrong_method_and_capacity_never_become_fresh(hass):
    store = AccessRecordStore(hass, "entry")
    await store.async_load()
    with pytest.raises(ValueError, match="method mismatch"):
        await store.async_reconcile("a", "fingerprint", inventory(), cleaned=True)
    with (
        patch("custom_components.ttlock_ble.access_records.MAX_RECORDS", 0),
        pytest.raises(ValueError, match="capacity exceeded"),
    ):
        await store.async_reconcile("a", "ic_card", inventory(), cleaned=True)
    assert not store.snapshot("a", "ic_card")["fresh"]


async def test_failed_persistence_does_not_certify_fresh_records(hass):
    from unittest.mock import AsyncMock

    store = AccessRecordStore(hass, "entry")
    await store.async_load()
    store._store.async_save = AsyncMock(side_effect=OSError("synthetic-store-failure"))
    with pytest.raises(OSError, match="synthetic-store-failure"):
        await store.async_reconcile("a", "ic_card", inventory(), cleaned=True)
    assert not store.snapshot("a", "ic_card")["fresh"]


async def test_failed_reenable_does_not_bypass_persisted_legacy_optout(hass):
    from unittest.mock import AsyncMock

    store = AccessRecordStore(hass, "entry")
    await store.async_load()
    await store.async_set_legacy_disabled("a", disabled=True)
    store._store.async_save = AsyncMock(side_effect=OSError("synthetic-store-failure"))
    with pytest.raises(OSError, match="synthetic-store-failure"):
        await store.async_set_legacy_disabled("a", disabled=False)
    assert store.legacy_disabled("a")


async def test_late_read_cannot_override_reset_or_newer_failed_attempt(hass):
    import asyncio
    from unittest.mock import AsyncMock

    store = AccessRecordStore(hass, "entry")
    await store.async_load()
    old = store.mark_stale("a", "ic_card")
    await store.async_reset("a", "ic_card")
    await store.async_reconcile("a", "ic_card", inventory(), cleaned=True, revision=old)
    assert not store.snapshot("a", "ic_card")["records"]
    started, release = asyncio.Event(), asyncio.Event()

    async def save(_data):
        started.set()
        await release.wait()

    store._store.async_save = AsyncMock(side_effect=save)
    old = store.mark_stale("a", "ic_card")
    task = asyncio.create_task(
        store.async_reconcile("a", "ic_card", inventory(), cleaned=True, revision=old)
    )
    await started.wait()
    store.mark_stale("a", "ic_card")
    release.set()
    await task
    assert not store.snapshot("a", "ic_card")["fresh"]
