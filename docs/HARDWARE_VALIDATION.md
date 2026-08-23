# Real-lock hardware-validation runbook

This runbook is for the `3.5.1rc9` prerelease only. It is a hardware
validation build, not production-certified. It deliberately uses released
`ttlock-ble==0.1.11`; passage mode is excluded from the build and stays
isolated on the SDK development branch.

RC5 already proved 6/6 physical cold-idle control. RC8 then proved stale-route
fallback starts correctly, but an unchanged TTLock advertisement received
during the Active window was deduplicated by HA and never reached the callback.
RC9 must prove that exact-address dedup-history clearing makes the next static
advertisement observable, then retain RC8's single fallback, RC7's startup
bootstrap, and RC6's command/state/log correctness.
Do not run PIN, auto-lock, passage, card, fingerprint, or other management
mutations during the first RC9 session.

Use disposable PINs that have never protected the door. Keep a mechanical key
or another verified recovery route available. Do not run `clear_passcodes`,
change an administrator code, factory-reset, or unpair the lock in the initial
session.

## Evidence and safe logging

Record the following non-secret facts once:

- Home Assistant version and installation type
- integration version, commit and SDK version (`ttlock-ble==0.1.11`)
- lock make/model and firmware/protocol version
- direct Bluetooth adapter or ESPHome proxy name/version
- approximate RSSI, distance and whether the TTLock app succeeds
- local time zone and the lock's apparent wall-clock accuracy

Use this temporary Home Assistant logger configuration:

```yaml
logger:
  default: warning
  logs:
    custom_components.ttlock_ble: debug
    bleak_retry_connector: debug
```

Do **not** enable `ttlock_ble.client` at debug level in this build. Released SDK
0.1.11 can log raw encrypted BLE frames and decrypted response bytes. The
integration namespace is sufficient for connection route, proxy, RSSI,
advertisement and action-failure evidence. Restart Home Assistant after adding
the configuration and remove it after the session.

For each step, record timestamp, transport route, entity state before/after,
expected result, actual result, elapsed time and pass/fail. Record only a
numeric/hex status code from an error; never copy the PIN, administrator PIN,
account email/password, AES key, unlock key, full diagnostics archive, or raw
BLE payload into shared evidence. Search exported logs for every secret before
sharing and redact the entire matching line. Home Assistant script/automation
YAML and traces can retain values entered there, so do not share them and
delete disposable traces/scripts when finished.

Replace `LOCK_DEVICE_ID`, `lock.front_door`, `DISPOSABLE_PIN` and timestamps in
the examples. Run action YAML from Developer Tools > Actions. The response from
`get_auto_lock` is also shown in that panel; in a script it can be captured as:

```yaml
sequence:
  - action: ttlock_ble.get_auto_lock
    data:
      device_id: LOCK_DEVICE_ID
    response_variable: auto_lock_result
```

## RC9 authoritative startup and dedup-safe stale-route acquisition

1. Keep the Home Assistant Bluetooth adapter set to **Auto**. Close TTLock,
   LightBlue, and other phone BLE tools. Do not use a global Active setting.
2. Physically establish and record the lock's starting position. Leave the
   lock untouched and disconnected for at least three minutes; do not prime it
   with `get_auto_lock`, a keypad, a phone app, or another management action.
3. Restart Home Assistant or reload the TTLock BLE config entry. Record the
   lock entity's startup state, active-acquisition start/result, selected HA
   route, RSSI, GATT connection, authoritative query, and final state.
4. Require exactly one bounded exact-address HA-managed Active acquisition
   when no cached/per-scanner connectable candidate exists. No simultaneous
   GATT attempts or standalone Bleak scanner may appear.
5. Require the connected query to change Unknown to the lock's physically
   verified Locked or Unlocked state. Advertisement and short-push hints may
   update battery or trigger the query, but must not write physical state.
6. Repeat once from the opposite physical state. Then use
   `homeassistant.update_entity` once after allowing the route to disappear;
   require the same bounded acquisition and authoritative result.
7. For the timeout case, move the lock out of range and reload the entry. The
   bounded wait must finish without a false Locked/Unlocked state, late GATT
   attempt, or credential-bearing log line. Restore range before continuing.
8. After state is known, observe one ordinary scheduled poll. It must not ask
   HA for an Active window solely because the routine interval elapsed.
9. Exercise the RC9 regression explicitly: allow a previously connectable HA
   route for this address to become several minutes old without clearing HA's
   Bluetooth history, then invoke one lock or unlock action. Require the cached
   route to be rejected from learned timing or its pre-command GATT attempt to
   fail, exactly one address-scoped Active acquisition to accept a newer
   callback route, and the requested command to be sent once through that route.
10. If HA replays the old service-info record immediately after callback
    registration, require genuinely ancient history to be ignored while a
    recent usable record remains eligible. Require the log to show that
    exact-address advertisement history was cleared before waiting, and that a
    subsequent byte-identical packet reached the callback. Record the old and
    accepted route/source, advertisement age, learned interval, freshness
    limit, scan count, GATT attempt count, control-write count, elapsed time,
    and physical result.

Stop if startup remains Unknown while a valid route was acquired, a hint writes
state directly, more than one concurrent scan/GATT attempt occurs, the scanner
must be switched globally Active, or a timeout invents physical state.

## RC9 command-result and state smoke test

Use the direct USB adapter and present installation first. The target lock is
`B6:D4:1E:DB:15:F8`, protocol 5.3, scene 2. Do not improve radio placement
until this baseline is recorded.

1. Set the Home Assistant Bluetooth adapter scanning mode to **Auto**, not
   Active. Close TTLock, LightBlue, and every other phone BLE tool. Disable their
   background access if necessary. Do not touch the keypad, fingerprint reader,
   handle, or lock body during acquisition.
2. Disable native auto-lock and physically verify it remains disabled. Keep the
   door closed and the current short USB extension/radio position unchanged.
3. Restart Home Assistant with the safe loggers above and pass the RC9 startup
   bootstrap above. Allow initial operation log synchronisation to finish. It
   must create **zero** historical HA events.
   Confirm passive battery advertisements arrive, then leave the lock untouched
   and disconnected for at least three minutes before every command.
4. From Locked, invoke `lock.unlock` once. Record the requested action, route,
   RSSI, candidate/GATT/total timing, control stage, physical result, HA action
   result, entity state immediately afterward, and entity state after at least
   two minutes. Do not use a phone app, keypad, or physical control to prepare
   or rescue the command.
5. Repeat step 4 for three independent cold-idle Unlocks. Prepare the locked
   starting position through Home Assistant only, with a fresh three-minute
   disconnected idle period before the formal command.
6. From Unlocked, perform three independent cold-idle Locks with the same
   preparation and evidence. Native auto-lock must remain disabled.
7. Require all of the following before expanding the run:
   - physical operation 6/6;
   - no false 6-second failure when a fresh connected query positively confirms
     the requested final state;
   - no duplicate physical command or second control-frame write after an
     ambiguous acknowledgement;
   - entity state remains equal to physical state after every operation;
   - state-transition logs identify `source=command` or a connected query, with
     age and RSSI, and no advertisement/push hint directly flips the state;
   - no historical operation-log flood, and one later real operation emits one
     event only;
   - no requirement to change the HA scanner from Auto to global Active.
8. Verify logs show at most one address-scoped active acquisition per command,
   no simultaneous GATT attempts for the lock, and a real SDK connection after
   either aggregate/per-scanner history or the exact-address callback. The
   active request must be the HA-managed Auto window, not a standalone scanner.
9. Run one controlled out-of-range timeout. Require the detailed HA
   reachability diagnosis, then restore range and prove a fresh cold-idle
   command succeeds.
10. Run one cancellation and one integration-unload test while acquisition is
   pending. Require prompt completion, no late connection/command, no dangling
   task, and no entity stranded in `locking` or `unlocking`.

Use one row per command:

| # | Command | RSSI | Control stage/result | Physical | HA immediate / +2 min | Log events | Pass |
|---|---|---:|---|---|---|---:|---|
| 1 | unlock |  |  |  |  |  |  |

RC9 first checks HA's aggregate and per-connectable-scanner records. When both
are empty, or the selected cached route exhausts its pre-command connection
attempt, exact-address `async_process_advertisements()` registers a connectable
callback and asks HA to schedule one bounded Active window. It first clears
HA's exact-address advertisement deduplication state, so the next byte-identical
TTLock packet is dispatched. Ancient history is rejected using a
device-cadence-aware age window, while a recent usable record is not hidden by
a strict scan-start cutoff. The callback's own HA route is used directly. This
is compatible with Auto-mode local adapters and ESPHome active proxies.
Background maintenance remains non-active.

Stop on the first unintended/duplicate physical operation, unreconciled false
failure, state mismatch, historical event flood, requirement for global Active,
late command after cancellation, or evidence of a secret in logs. A result
below 6/6 is not a smoke-test pass.

## Expanded cold-idle release-readiness test

Only after the six-action smoke test passes, repeat the same Auto-mode,
three-minute disconnected preparation for **10 Unlocks and 10 Locks**. Require
20/20 physical operations, truthful action outcomes, stable physical state,
zero duplicate commands, zero historical flood, and no global Active setting
before calling the integration release-ready.

## Deferred management sequence — do not run until RC9 smoke passes

### A. Eight-step baseline

1. Restart Home Assistant; confirm the TTLock device and its lock, battery,
   connection and event entities load without repair/config errors.
2. Record the lock entity state and battery while the lock is idle.
3. Operate the lock physically; confirm the advertisement updates battery and a
   connected query eventually confirms the entity's physical state.
4. Lock from Home Assistant:

   ```yaml
   action: lock.lock
   target:
     entity_id: lock.front_door
   ```

5. Unlock from Home Assistant:

   ```yaml
   action: lock.unlock
   target:
     entity_id: lock.front_door
   ```

6. Lock and unlock once by a normal existing method outside Home Assistant;
   confirm the entity corrects after each operation and no optimistic state is
   left behind.
7. Repeat steps 4 and 5 through the directly connected Bluetooth adapter and
   record connection route, latency and result.
8. Repeat steps 4 and 5 through the ESPHome active Bluetooth Proxy and record
   proxy, RSSI, latency and result. If either transport is unavailable, mark
   that step not run rather than passed.

Stop if baseline lock/unlock or state reconciliation is unreliable.

### B. Auto-lock read, set, disable and restore

1. Read the current delay and record it as `ORIGINAL_SECONDS`:

   ```yaml
   action: ttlock_ble.get_auto_lock
   data:
     device_id: LOCK_DEVICE_ID
   ```

2. Set a short safe delay, for example 15 seconds:

   ```yaml
   action: ttlock_ble.set_auto_lock
   data:
     device_id: LOCK_DEVICE_ID
     seconds: 15
   ```

3. Read it back with `ttlock_ble.get_auto_lock`; require `seconds: 15`.
4. Unlock once and physically confirm one native re-lock near the configured
   delay; confirm Home Assistant returns to locked.
5. Only while the door can safely remain unlocked, set `seconds: 0`, read it
   back, then unlock and wait longer than the prior delay. Treat anything other
   than an explicit zero plus no native re-lock as unsupported, not as success.
6. In a `finally`-style safety step, restore `ORIGINAL_SECONDS`, read it back,
   and physically verify normal auto-lock behaviour. Restore it even if any
   earlier assertion failed.

Set and restore use the same action with different seconds:

```yaml
action: ttlock_ble.set_auto_lock
data:
  device_id: LOCK_DEVICE_ID
  seconds: ORIGINAL_SECONDS
```

### C. One disposable permanent PIN

1. Create a new 4-9 digit disposable PIN:

   ```yaml
   action: ttlock_ble.add_passcode
   data:
     device_id: LOCK_DEVICE_ID
     code: "DISPOSABLE_PIN"
     type: permanent
   ```

2. Lock the door and physically confirm that PIN unlocks exactly once.
3. Delete that same PIN:

   ```yaml
   action: ttlock_ble.delete_passcode
   data:
     device_id: LOCK_DEVICE_ID
     code: "DISPOSABLE_PIN"
     type: permanent
   ```

4. Lock the door and physically confirm the deleted PIN is rejected. Do not
   proceed if deletion cannot be proven.

### D. One disposable period PIN

Choose a fresh PIN. Set `start` about five minutes in the future and `end`
about ten minutes after `start`. Use ISO 8601 timestamps with an explicit local
UTC offset; both boundaries are sent with minute precision.

```yaml
action: ttlock_ble.add_passcode
data:
  device_id: LOCK_DEVICE_ID
  code: "DISPOSABLE_PERIOD_PIN"
  type: period
  start: "2026-08-22T15:05:00+01:00"
  end: "2026-08-22T15:15:00+01:00"
```

1. Before `start`, physically confirm rejection.
2. At least one minute after `start`, physically confirm one successful unlock.
3. At least one minute after `end`, physically confirm rejection.
4. Clean up the expired PIN with `ttlock_ble.delete_passcode`, using the same
   PIN and `type: period`, then confirm it remains rejected.

Do not infer success from the action call alone: the before/during/after keypad
results are the hardware assertions. A failed boundary test may indicate lock
RTC/time-zone drift rather than payload rejection; capture both wall clocks.

## Stop conditions and known risks

- `clear_passcodes` is destructive and intentionally not part of this run.
- There is no list/read-PIN action, so physical rejection is the deletion
  proof available in this build.
- Firmware may reject `seconds: 0` or clamp a delay; always restore the value
  read at the start.
- Period PIN behaviour depends on the lock RTC matching the intended local wall
  clock and DST offset.
- BLE proxy reachability and response timing can differ from a direct adapter;
  report each route separately.
- SDK 0.1.11 debug output is not safe to share; keep its logger below debug.
- Stop after a repeated timeout, state mismatch, unexpected unlock, failure to
  restore auto-lock, or failure to delete a disposable PIN. Preserve sanitized
  evidence and recover through a previously verified method.

## Passage-mode preparation (later session only)

Do not install the passage SDK branch until the baseline, auto-lock, permanent
PIN and period PIN sections all pass. In a separate development environment,
use this staged order only:

1. Query the device capability mask; record the mask and derived booleans.
2. Query existing passage schedules without mutation.
3. Add one short interval for the current weekday.
4. Query again and require that exact interval.
5. Physically verify native passage behaviour during the interval without
   repeated Home Assistant unlock calls.
6. Delete that exact interval and query to prove removal.
7. Confirm normal locking/auto-lock behaviour returns.

Do not call `clear_passage_modes` in the first passage session. Do not claim
model support until capability, query, add, physical behaviour, delete and
return-to-normal all pass.
