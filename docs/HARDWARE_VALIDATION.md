# TTLock BLE 3.5.1rc10 hardware validation

RC10 is a hardware-validation prerelease, not a stable release. RC9 remains the
validated rollback checkpoint. Keep the Home Assistant Bluetooth adapter in
**Auto**, use the current USB extension/radio position, close phone BLE apps,
disable native auto-lock for alternating command tests, and do not improve the
intentional -80 to -90 dBm RF conditions.

Use only these safe debug loggers:

```yaml
logger:
  default: warning
  logs:
    custom_components.ttlock_ble: debug
    bleak_retry_connector: debug
```

Never enable `ttlock_ble.client: debug`; the pinned SDK source can log raw wire
material. Never share an AES key, unlock key, admin code, PIN, cloud credential,
raw decrypted frame, Home Assistant automation trace containing a PIN, or an
unreviewed diagnostics archive.

## A. Startup state and state/log ordering

1. Upgrade to RC10, leave background maintenance and permanent connection off,
   physically lock the door, and reboot Home Assistant.
2. Require an authoritative `Unknown -> Locked` connected query under Auto.
3. Repeat with the mechanism physically unlocked; require
   `Unknown -> Unlocked`.
4. On one reboot with old operation history present, verify the entity state is
   visible before operation-log synchronization completes.
5. Let one later routine poll fail because of RF/stale routing. The prior
   authoritative state and battery must remain displayed; the connectivity
   sensor may be disconnected. A later successful query must resynchronize.

Expected safe evidence resembles:

```text
Connection acquisition started ... reason=authoritative state bootstrap, active_scan=True
BLE connection established ... source=hci0, RSSI=-84
state UNKNOWN -> UNLOCKED, source=query, age=0.0s, RSSI=-84
get_operation_log ...
```

Stop if a state hint writes bolt state directly, operation logs delay state, a
failed poll changes a known state to Unknown, or global Active is required.

## B. Command reliability and stale-route latency

1. Leave the lock untouched/disconnected for at least three minutes.
2. From Locked, invoke one Unlock. Repeat for three independent cold-idle
   Unlocks, restoring Locked through Home Assistant only.
3. From Unlocked, perform three independent cold-idle Locks with the same
   preparation.
4. Record request time, cached route source/age/RSSI, cached connect duration,
   Active acquisition time, fresh route, GATT time, control stages, physical
   result, HA result, and total latency for all six actions.
5. Require exactly one physical operation and one complete control-frame write
   per action, 6/6 correct physical results, no false reversal, and no resend
   after an ambiguous post-write result.
6. Specifically verify a stale cached route reports
   `speculative_cached_route=True, connect_attempts=1`; if it fails before
   authentication/control, require exactly one fresh address-scoped Active
   acquisition followed by `connect_attempts=3`.

Expected safe evidence resembles:

```text
Opening BLE connection ... speculative_cached_route=True, connect_attempts=1
Cached BLE route failed before command/authentication ... starting one fresh address-scoped acquisition
Clearing Bluetooth advertisement deduplication history ...
Connection candidate became available ... resolution=fresh active advertisement, RSSI=-83
Opening BLE connection ... speculative_cached_route=False, connect_attempts=3
Control stage ... control_frame_written
Control stage ... control_acknowledged
```

The latency goal is a typical 8-15 seconds when the lock advertises promptly,
not a fixed deadline. Stop on any duplicate control frame or physical action.

## C. External-operation responsiveness

For each method available, begin from a known authoritative state, allow the
BLE session to go idle, perform exactly one operation, and record physical,
advertisement, query, HA-state, and event timestamps in this order:

1. Fingerprint unlock.
2. Physical/manual key lock or unlock.
3. PIN unlock.
4. IC-card unlock.
5. Official TTLock-app unlock.

For each operation capture state hint, `has_new_records`, `is_setting_mode`,
battery, RSSI, whether an authoritative query was requested, final state,
operation-log event type, and safe method. RC10 must not expose the SDK's
overloaded credential field or invent a friendly user name.

Expected safe evidence resembles:

```text
Advertisement hint ... hint=UNLOCKED, new_records=True, setting_mode=False, battery=98, RSSI=-82
Advertisement activity ... new_records_appeared=True; requesting authoritative state and operation-log synchronization
state LOCKED -> UNLOCKED, source=query, age=0.0s, RSSI=-82
Operation-log classification ... live=1, historical_or_ambiguous=0, duplicate=0
```

Record a firmware limitation rather than a software pass if no hint changes.

## D. Operation-log replay safety

1. Reboot with substantial pre-existing TTLock history, including the observed
   short-page/full-page pattern. Require zero historical HA events.
2. Create one new fingerprint operation after startup. Require exactly one HA
   event with `method=fingerprint` and no credential/PIN attribute.
3. Repeat with one other available method. Require one event.
4. Force another log sync; duplicate/replayed pages must create zero events.
5. Restart Home Assistant and sync again; old events must not fire again.
6. Let one log request timeout/fail. The lock state must remain correct and the
   coordinator update must remain successful.

Undated or otherwise ambiguous entries may be conservatively suppressed. That
is safer than firing historical unlock automations.

## E. Lifecycle and extended gate

1. With state known and default options, observe for at least 15 minutes.
   There must be no five-minute keep-warm GATT loop.
2. Confirm the hourly coordinator sanity poll still runs without requesting an
   Active scan solely because the state is known.
3. Reboot out of range. Unknown state must retry with bounded backoff sooner
   than one hour and must remain Unknown until a query succeeds.
4. Enable background maintenance explicitly and confirm `reconnect_interval`
   still controls it; then disable it again.
5. Enable permanent connection only as a separate battery-cost test, confirm it
   reconnects, then turn it off.
6. If A-D and the six-command smoke test pass, expand to 10 Unlocks + 10 Locks
   using the same three-minute cold-idle preparation. Require 20/20 physical
   success, truthful HA outcomes/state, zero resends, zero history flood, and no
   global Active requirement before considering a stable release.

Stop immediately on an unintended operation, leaked secret, stale-state
overwrite, duplicate command, historical-event flood, raw BlueZ/private scanner
path, or requirement to configure the HA adapter globally Active.
