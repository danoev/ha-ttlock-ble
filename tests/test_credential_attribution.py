"""Synthetic inventory, log, administration and Recorder-facing event pipeline."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.util import dt as dt_util
from ttlock_ble import (
    AdministrativeInventory,
    AdministrativeRecord,
    InventoryEntry,
    InventoryResult,
)
from ttlock_ble.commands.administrative_records import parse_administrative_record
from ttlock_ble.commands.operate_log import parse_operate_log_response

from custom_components.ttlock_ble.access_records import AccessRecordStore
from custom_components.ttlock_ble.connection import TtlockBleConnection
from custom_components.ttlock_ble.management_http import URL
from tests.test_management_http import headers, request_data

ATTRIBUTION = "custom_components.ttlock_ble.attribution"
CARD = b"\x00\x12\x23\x34"
FINGER = b"\x00\x13\x24\x35\x46\x07"
PIN = b"004831"
OWNER = "synthetic-owner"


def _inventory(*records):
    return AdministrativeInventory(
        InventoryResult(
            tuple(InventoryEntry(r.validity) for r in records),
            complete=True,
            limitation="",
        ),
        records,
    )


def _record(method, identifier):
    dates = b"\x00" * 5 + bytes([99, 1, 1, 0, 0])
    if method == "fingerprint":
        frame = b"\x06\x01\x50\x06\x00\x02" + identifier + dates
    elif method == "ic_card":
        frame = b"\x05\x01\x50\x01\x00\x02" + identifier + dates
    else:
        frame = (
            b"\x07\x01\x50\x00\x00\x02\x00\x01"
            + bytes([len(identifier)])
            + identifier
            + b"\x00"
            + dates[-5:]
        )
    return parse_administrative_record(method, frame)


def _entry(sequence, code, when, payload):
    date = bytes(
        [when.year % 100, when.month, when.day, when.hour, when.minute, when.second]
    )
    record = bytes([code]) + date + b"\x50" + payload
    frame = (
        b"\x25\x01"
        + (len(record) + 5).to_bytes(2, "big")
        + sequence.to_bytes(2, "big")
        + bytes([len(record)])
        + record
    )
    return parse_operate_log_response(frame, strict=True)[0][0]


def _clock():
    return dt_util.now().replace(tzinfo=None, microsecond=0) + timedelta(seconds=2)


@pytest.mark.parametrize(
    ("method", "identifier", "code"),
    [("fingerprint", FINGER, 20), ("ic_card", CARD, 17), ("keypad_pin", PIN, 4)],
)
async def test_exact_sdk_inventory_and_log_match_without_guessing(
    hass, method, identifier, code
):
    store = AccessRecordStore(hass, "entry")
    await store.async_load()
    await store.async_reconcile(
        "front", method, _inventory(_record(method, identifier)), cleaned=True
    )
    ref = store.snapshot("front", method)["records"][0]["reference"]
    payload = (
        bytes([len(identifier)]) + identifier + b"\x00"
        if method == "keypad_pin"
        else identifier
    )
    record = _entry(9, code, _clock(), payload)
    match = store.match_credential("front", method, record.credential_identifier)
    assert match.status == "matched"
    assert match.reference == ref
    assert store.match_credential("back", method, identifier).status == "stale"
    assert (
        store.match_credential("front", method, identifier[::-1]).status == "unmatched"
    )
    assert (
        store.match_credential("front", method, identifier[:-1] + b"9").status
        == "unmatched"
    )
    assert store.attribution.observe("front", record) is None


async def test_duplicate_shared_pin_and_missing_pin_are_never_guessed(
    hass, hass_storage
):
    store = AccessRecordStore(hass, "entry")
    await store.async_load()
    pin = _record("keypad_pin", PIN)
    timed = AdministrativeRecord("keypad_pin", "pin_type_3", "time_window", PIN)
    await store.async_reconcile(
        "front", "keypad_pin", _inventory(pin, timed), cleaned=True
    )
    assert store.match_credential("front", "keypad_pin", PIN).status == "ambiguous"
    await store.async_reconcile("front", "keypad_pin", _inventory(timed), cleaned=True)
    assert store.match_credential("front", "keypad_pin", PIN).status == "unsupported"
    await store.async_reconcile("front", "keypad_pin", _inventory(), cleaned=True)
    assert store.match_credential("front", "keypad_pin", PIN).status == "stale"
    assert PIN.decode() not in repr(hass_storage)
    assert PIN.hex() not in repr(hass_storage)


async def test_admin_guidance_to_public_event_opt_in_and_revocation(  # noqa: PLR0915 -- one end-to-end disclosure and revocation journey
    hass,
    setup_integration,
    mock_ttlock_connection,
    mock_ble_resolver,
    mock_ttlock_client,
    sample_virtual_key,
    hass_client,
    hass_storage,
    caplog,
):
    store = setup_integration.runtime_data.access_records
    mac = sample_virtual_key.lockMac
    conn = TtlockBleConnection(hass, sample_virtual_key, access_records=store)
    mock_ttlock_connection.async_check_identification_log = AsyncMock(
        side_effect=conn.async_check_identification_log
    )
    mock_ttlock_client.get_operation_log = AsyncMock(return_value=[])
    await store.async_reconcile(
        mac, "ic_card", _inventory(_record("ic_card", CARD)), cleaned=True
    )
    ref = store.snapshot(mac, "ic_card")["records"][0]["reference"]
    client = await hass_client()
    admin_responses = []

    async def post(action, expected=200, **kwargs):
        response = await client.post(
            URL,
            json=request_data(setup_integration, action, **kwargs),
            headers=headers(client),
        )
        assert response.status == expected
        data = await response.json()
        admin_responses.append(data)
        return data

    start = _clock()
    trial = _entry(2, 17, start + timedelta(seconds=2), CARD)
    with patch(f"{ATTRIBUTION}._now", return_value=start) as clock:
        ready = await post("identify_start", reference=ref)
        assert not ready["publication_enabled"]
        assert ready["identification"]["status"] == "waiting"
        nonce = ready["identification"]["session"]
        await post(
            "identify_confirm",
            expected=400,
            session=nonce,
            confirm="observed_credential_use",
        )
        # A historically dated match is not sufficient; the new trial is.
        old = _entry(1, 17, start - timedelta(seconds=60), CARD)
        mock_ttlock_client.get_operation_log.return_value = [old, trial]
        clock.return_value = start + timedelta(seconds=3)
        checked = await post("identify_check", session=nonce)
        assert checked["identification"]["can_confirm"]
        confirmed = await post(
            "identify_confirm", session=nonce, confirm="observed_credential_use"
        )
        assert confirmed["verified_references"] == [ref]
        await post("name", reference=ref, name="Household card")
        await post(
            "name", expected=400, reference=ref, name=str(int.from_bytes(CARD, "big"))
        )
        private = _entry(3, 17, start + timedelta(seconds=4), CARD)
        mock_ttlock_client.get_operation_log.return_value = [trial, private]
        clock.return_value = start + timedelta(seconds=5)
        safe = await conn.async_get_operation_log()
        assert len(safe) == 1
        assert safe[0].attribution is None
        await post("publication", enabled=True, confirm="share_labels_with_recorder")
        shared = _entry(4, 17, start + timedelta(seconds=6), CARD)
        mock_ttlock_client.get_operation_log.return_value = [shared]
        clock.return_value = start + timedelta(seconds=7)
        assert (await conn.async_get_operation_log())[
            0
        ].attribution.label == "Household card"
        await hass.async_block_till_done()
        state = hass.states.async_all("event")[0]
        assert state.attributes["event_type"] == "unlock"
        assert state.attributes["credential_label"] == "Household card"
        assert state.attributes["credential_method"] == "ic_card"
        assert state.attributes["attribution_status"] == "credential_confirmed"
        assert "uid" not in state.attributes
        assert "key_id" not in state.attributes
        material = (
            repr(state.attributes)
            + caplog.text
            + json.dumps(admin_responses)
            + repr(hass_storage)
        )
        assert CARD.hex() not in material
        assert str(int.from_bytes(CARD, "big")) not in material
        assert ref not in repr(state.attributes)
        assert await conn.async_get_operation_log() == []  # replay
        new = _entry(5, 17, start + timedelta(seconds=8), CARD)
        mutation = _entry(6, 15, start + timedelta(seconds=9), CARD)
        mock_ttlock_client.get_operation_log.return_value = [new, mutation]
        clock.return_value = start + timedelta(seconds=10)
        assert all(
            row.attribution is None for row in await conn.async_get_operation_log()
        )
    loaded = AccessRecordStore(hass, setup_integration.entry_id)
    await loaded.async_load()
    assert loaded.snapshot(mac, "ic_card")["records"][0]["name"] == "Household card"
    assert (
        loaded.attribution.snapshot(mac, "ic_card", OWNER)["publication_enabled"]
        is False
    )
    assert (
        loaded.attribution.snapshot(mac, "ic_card", OWNER)["verified_references"] == []
    )
    mock_ttlock_client.unlock.assert_not_awaited()
    mock_ttlock_client.lock.assert_not_awaited()


@pytest.mark.parametrize(
    "failure",
    ["ambiguous", "unmatched", "unsupported", "expired", "reset", "incomplete"],
)
async def test_confirmation_fails_closed(hass, failure):
    store = AccessRecordStore(hass, "entry")
    await store.async_load()
    await store.async_reconcile(
        "front", "ic_card", _inventory(_record("ic_card", CARD)), cleaned=True
    )
    ref = store.snapshot("front", "ic_card")["records"][0]["reference"]
    a = store.attribution
    start = _clock()
    read = AsyncMock(return_value=True)
    with patch(f"{ATTRIBUTION}._now", return_value=start) as clock:
        await a.async_begin("front", "ic_card", ref, OWNER, read)
        nonce = a.snapshot("front", "ic_card", OWNER)["identification"]["session"]
        assert a.snapshot("front", "ic_card", "other")["identification"] == {
            "status": "busy"
        }
        with pytest.raises(ValueError, match="another session"):
            a.cancel("front", "other", nonce)
        clock.return_value = start + timedelta(seconds=5)
        data = (
            CARD[::-1]
            if failure == "unmatched"
            else CARD * 2
            if failure == "unsupported"
            else CARD
        )
        a.observe("front", _entry(10, 17, start + timedelta(seconds=2), data))
        if failure == "ambiguous":
            a.observe("front", _entry(11, 17, start + timedelta(seconds=3), CARD))
        await a.async_check("front", OWNER, nonce, read)
        if failure in {"ambiguous", "unmatched", "unsupported"}:
            assert (
                a.snapshot("front", "ic_card", OWNER)["identification"]["status"]
                == failure
            )
        if failure == "reset":
            await store.async_reset("front", "ic_card")
        if failure == "incomplete":
            a.note_incomplete("front")
        with (
            patch(f"{ATTRIBUTION}.monotonic", return_value=10**12)
            if failure == "expired"
            else patch(f"{ATTRIBUTION}._now", return_value=clock.return_value),
            pytest.raises(ValueError, match=r"expired|No unique"),
        ):
            a.confirm("front", OWNER, nonce)


async def test_start_reservation_cancellation_and_stale_scope(hass):
    store = AccessRecordStore(hass, "entry")
    await store.async_load()
    await store.async_reconcile(
        "front", "fingerprint", _inventory(_record("fingerprint", FINGER)), cleaned=True
    )
    ref = store.snapshot("front", "fingerprint")["records"][0]["reference"]
    a = store.attribution
    started = asyncio.Event()

    async def blocked_read():
        started.set()
        await asyncio.Future()

    task = asyncio.create_task(
        a.async_begin("front", "fingerprint", ref, OWNER, blocked_read)
    )
    await started.wait()
    with pytest.raises(ValueError, match="busy"):
        await a.async_begin(
            "front", "fingerprint", ref, "other", AsyncMock(return_value=True)
        )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert a.snapshot("front", "fingerprint", OWNER)["identification"] is None
    await a.async_begin(
        "front", "fingerprint", ref, OWNER, AsyncMock(return_value=True)
    )
    nonce = a.snapshot("front", "fingerprint", OWNER)["identification"]["session"]
    a.cancel("front", OWNER, nonce)
    assert a.snapshot("front", "fingerprint", OWNER)["identification"] is None
    store.mark_stale("front", "fingerprint")
    assert store.match_credential("front", "fingerprint", FINGER).status == "stale"


async def test_gap_and_identifier_reuse_revoke_proof_before_event_publication(hass):
    store = AccessRecordStore(hass, "entry")
    await store.async_load()
    inventory = _inventory(_record("fingerprint", FINGER))
    await store.async_reconcile("front", "fingerprint", inventory, cleaned=True)
    ref = store.snapshot("front", "fingerprint")["records"][0]["reference"]
    a = store.attribution
    start = _clock()
    read = AsyncMock(return_value=True)
    with patch(f"{ATTRIBUTION}._now", return_value=start) as clock:
        await a.async_begin("front", "fingerprint", ref, OWNER, read)
        nonce = a.snapshot("front", "fingerprint", OWNER)["identification"]["session"]
        clock.return_value = start + timedelta(seconds=2)
        trial = _entry(31, 20, clock.return_value, FINGER)
        a.note_unseen("front", trial)
        a.observe("front", trial)
        await a.async_check("front", OWNER, nonce, read)
        a.confirm("front", OWNER, nonce)
        await store.async_name("front", "fingerprint", ref, "Spare finger")
        a.set_publication("front", enabled=True)
        clock.return_value = start + timedelta(seconds=4)
        gap = _entry(34, 20, clock.return_value, FINGER)
        a.note_unseen("front", gap)
        assert a.observe("front", gap) is None
        assert a.snapshot("front", "fingerprint", OWNER)["verified_references"] == []
        a.note_unseen("front", _entry(35, 21, clock.return_value, FINGER))
        assert not store.snapshot("front", "fingerprint")["fresh"]
        await store.async_reconcile("front", "fingerprint", inventory, cleaned=True)
        assert a.observe("front", gap) is None


async def test_explicit_control_preempts_identification_read_and_owns_cleanup(
    hass,
    sample_virtual_key,
    mock_ble_resolver,
    mock_ttlock_client,
):
    conn = TtlockBleConnection(hass, sample_virtual_key)
    entered = asyncio.Event()
    released = asyncio.Event()
    order = []

    async def waiting_for_log(**_kwargs):
        entered.set()
        await asyncio.Future()

    async def disconnect():
        order.append("cleanup")
        released.set()

    async def lock():
        assert released.is_set()
        order.append("control")

    mock_ttlock_client.get_operation_log.side_effect = waiting_for_log
    mock_ttlock_client.disconnect.side_effect = disconnect
    mock_ttlock_client.lock.side_effect = lock
    read = asyncio.create_task(conn.async_check_identification_log())
    await entered.wait()
    await conn.async_lock()
    with pytest.raises(asyncio.CancelledError):
        await read
    assert order == ["cleanup", "control"]
    mock_ttlock_client.lock.assert_awaited_once()
    mock_ttlock_client.unlock.assert_not_awaited()
