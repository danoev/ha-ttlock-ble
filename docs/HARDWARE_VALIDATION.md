# Real-lock hardware-validation runbook

This runbook is for the `3.5.1rc5` prerelease only. It is a hardware
validation build, not production-certified. It deliberately uses released
`ttlock-ble==0.1.11`; passage mode is excluded from the build and stays
isolated on the SDK development branch.

RC5 has one release-blocking objective: repeatable cold-idle lock/unlock with
no physical interaction. Do not run PIN, auto-lock, passage, card, fingerprint,
or other management mutations during this session.

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

## RC5 cold-idle repeatability test

Use the direct USB adapter and present installation first. The target lock is
`B6:D4:1E:DB:15:F8`, protocol 5.3, scene 2. Do not improve radio placement
until this baseline is recorded.

1. Close TTLock, LightBlue, and every other phone BLE tool. Disable their
   background access if necessary. Do not touch the keypad, fingerprint reader,
   handle, or lock body during acquisition.
2. Restart Home Assistant with the safe loggers above. Confirm passive state
   and battery advertisements arrive, then wait at least three minutes and
   require the connection entity to be `disconnected` before attempt 1.
3. Invoke `lock.unlock` once. Record source, RSSI, candidate resolution,
   candidate-acquisition time, GATT connection time, total command time,
   physical result, entity result, and pass/fail. A successful result requires
   the physical lock to unlock with no human interaction at the lock.
4. Return the lock to the locked starting state through Home Assistant only
   where practical. Wait for the BLE connection to drop, then leave the lock
   idle for at least three minutes before the next independent unlock.
5. Repeat until there are 10 independent cold-idle unlock attempts. Run at
   least five with the door closed in its normal position and current radio
   placement. Target: **10/10**. Record median and worst successful latency;
   bounded acquisition remains 25 seconds.
6. Starting from an unlocked state, repeat the same disconnected/three-minute
   idle preparation and measurement for 10 independent Home Assistant lock
   commands where mechanically meaningful. Target: **10/10** without physical
   interaction, including at least five door-closed attempts.
7. Verify logs show at most one active-acquisition request per command, no
   simultaneous GATT attempts for the lock, and a real SDK connection attempt
   after either `aggregate connectable history` or `connectable scanner path`
   resolution. `bleak_retry_connector` debug records connection attempts; do
   not enable `ttlock_ble.client` debug.
8. Run one controlled out-of-range timeout. Require the detailed HA
   reachability diagnosis, then restore range and prove a fresh cold-idle
   command succeeds.
9. Run one cancellation and one integration-unload test while acquisition is
   pending. Require prompt completion, no late connection/command, no dangling
   task, and no entity stranded in `locking` or `unlocking`.

Use one row per command:

| # | Command | Door | Source | RSSI | Candidate s | GATT s | Total s | Physical/entity result | Pass |
|---|---|---|---|---:|---:|---:|---:|---|---|
| 1 | unlock | closed/open | hci0/proxy |  |  |  |  |  |  |

The cache polling interval is not a radio polling interval. RC5 first checks
HA's aggregate history and per-connectable-scanner records. Only when both are
empty does it request one bounded HA-managed active window and re-read those
in-memory representations approximately every 0.5 seconds.

Stop on the first unintended physical operation, repeated timeout, late command
after cancellation, persistent entity transition, or evidence of a secret in
logs. A result below 10/10 is not a pass; preserve sanitized evidence and do
not proceed to management features.

## Deferred management sequence — do not run for RC5

### A. Eight-step baseline

1. Restart Home Assistant; confirm the TTLock device and its lock, battery,
   connection and event entities load without repair/config errors.
2. Record the lock entity state and battery while the lock is idle.
3. Operate the lock physically; confirm passive BLE advertisements correct the
   entity state/battery without Home Assistant opening a command connection.
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
