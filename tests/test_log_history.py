from __future__ import annotations

import datetime as dt
import json

from ttlock_ble import LogEntry, LogOperate

from custom_components.ttlock_ble.log_history import TtlockBleLogHistory

MAC = "AA:BB:CC:DD:EE:FF"
CUTOFF = dt.datetime(2026, 8, 24, 12, 0, 0)  # noqa: DTZ001 -- lock RTC is naive


def _entry(
    number: int,
    when: dt.datetime | None,
    *,
    record_type: LogOperate = LogOperate.MOBILE_UNLOCK,
    password: str | None = None,
) -> LogEntry:
    return LogEntry(
        record_number=number,
        record_type=record_type,
        operate_date=when,
        lock_battery=80,
        uid=123,
        password=password,
    )


async def test_short_then_full_historical_pages_are_suppressed(hass) -> None:
    history = TtlockBleLogHistory(hass)
    history._startup_cutoff = CUTOFF
    short = [_entry(1, CUTOFF - dt.timedelta(days=2))]
    later_full = [
        _entry(number, CUTOFF - dt.timedelta(days=1))
        for number in range(2, 27)
    ]

    assert history.classify(MAC, short) == []
    assert history.classify(MAC, later_full) == []


async def test_genuinely_new_record_dispatches_once(hass) -> None:
    history = TtlockBleLogHistory(hass)
    history._startup_cutoff = CUTOFF
    new = _entry(10, CUTOFF + dt.timedelta(seconds=1))

    assert history.classify(MAC, [new]) == [new]
    assert history.classify(MAC, [new]) == []


async def test_undated_record_is_suppressed_as_ambiguous(hass) -> None:
    history = TtlockBleLogHistory(hass)
    history._startup_cutoff = CUTOFF

    assert history.classify(MAC, [_entry(3, None)]) == []


async def test_record_number_wrap_uses_timestamp_in_identity(hass) -> None:
    history = TtlockBleLogHistory(hass)
    history._startup_cutoff = CUTOFF
    before_wrap = _entry(0, CUTOFF + dt.timedelta(seconds=1))
    after_wrap = _entry(0, CUTOFF + dt.timedelta(seconds=2))

    assert history.classify(MAC, [before_wrap]) == [before_wrap]
    assert history.classify(MAC, [after_wrap]) == [after_wrap]


async def test_restart_loads_journal_and_never_replays_existing_record(hass) -> None:
    first = TtlockBleLogHistory(hass, "entry-id")
    first._startup_cutoff = CUTOFF
    record = _entry(11, CUTOFF + dt.timedelta(seconds=1))
    assert first.classify(MAC, [record]) == [record]
    await first.async_save()

    restarted = TtlockBleLogHistory(hass, "entry-id")
    restarted._startup_cutoff = CUTOFF + dt.timedelta(hours=1)
    await restarted.async_load()

    assert restarted.classify(MAC, [record]) == []


async def test_persisted_journal_contains_no_passcode_or_cloud_secret(hass) -> None:
    history = TtlockBleLogHistory(hass, "entry-id-secret-check")
    history._startup_cutoff = CUTOFF
    passcode = "583921"
    history.classify(
        MAC,
        [
            _entry(
                12,
                CUTOFF + dt.timedelta(seconds=1),
                record_type=LogOperate.KEYBOARD_PASSWORD_UNLOCK,
                password=passcode,
            ),
        ],
    )

    serialized = json.dumps(history._serialize())

    assert passcode not in serialized
