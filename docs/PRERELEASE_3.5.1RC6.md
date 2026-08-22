# TTLock BLE 3.5.1rc6

This is a **hardware-validation-only prerelease**, not a production-certified
release. RC1 through RC5 are superseded for testing but remain tagged as
reproducible development artefacts. RC6 keeps `ttlock-ble==0.1.11` and does not
merge the hardware-validation branch or draft pull request.

## Investigation result

### Confirmed

- RC5 physically executed all six formal cold-idle commands. A control frame
  cannot be constructed until `CHECK_USER_TIME` succeeds, so each of the five
  later 6-second timeouts occurred after authentication, in the control
  exchange. The complete control frame had already been handed to the BLE
  stack in the reproduced failure model.
- SDK 0.1.11 sends the control frame once and does not automatically retry it.
  Its public exception previously did not distinguish a pre-write failure from
  a lost post-write acknowledgement, so Home Assistant surfaced both as the
  same command failure.
- Four paths could write lock state: command completion, coordinator query,
  manufacturer advertisement bit 0, and decoded `0x14` push state. RC5 logs did
  not include source attribution, so the exact historical writer cannot be
  reconstructed. Real hardware nevertheless disproved the assumption that
  advertisement/heartbeat state is always safe as persistent physical state.
- The first short operation-log page completed seeding. A full 25-record page
  also incorrectly completed seeding, allowing later historical pages to be
  emitted as new events.
- Home Assistant's exact-address `async_process_advertisements()` API registers
  the callback before requesting a temporary Active window. This is the native
  Auto-mode path and preserves ESPHome proxy routing. A permanent exact-address
  tracker does not need Active mode to receive ordinary manufacturer data.

### Strongly inferred

- Manual global Active improved RC5 because it continuously supplied a current
  connectable route at weak RSSI. RC6 obtains that route only for an explicit
  operation through HA's bounded Auto-mode active scheduler.
- The false Locked transitions most likely came from the two un-attributed
  short-state writers (advertisement or `0x14` push), with the C3 timing making
  the reconnect push path especially plausible. A fresh connected
  `SEARCH_BICYCLE_STATUS` reply remains the best available state authority.

### Unknown pending RC6 hardware evidence

- Whether the missing control acknowledgement is absent on air, lost during
  disconnect ordering, malformed, or replaced by an unsolicited `0x14` frame.
- Whether advertisement bit 0 is transient/event state on this exact firmware,
  or a persistent state transmitted late or out of order.
- Which of advertisement or push caused each historical RC5 state reversal.

## Cumulative RC6 behaviour

- Exact-address Bluetooth reachability diagnostics remain credential-safe.
- Explicit lock/unlock, passcode add/delete, and auto-lock management first use
  existing HA aggregate/per-scanner connectable routes. On a miss, one
  25-second exact-address Active wait is scheduled by HA while the adapter may
  remain Auto. The persistent advertisement callback is Passive. No raw BlueZ,
  fixed adapter, standalone Bleak scanner, or proxy-bypassing path is used.
- Explicit control logs safe milestones: before authentication, authentication
  write started/sent, authenticated, control write started/completed,
  acknowledgement received/accepted/rejected, routed response echoes, and
  disconnect ordering. Keys, PINs, decrypted payloads, and credential frames
  are not logged by the integration.
- A completed control write with a lost acknowledgement has an explicit unknown
  outcome. It is never resent. One fresh connected state query may reconcile a
  matching requested state to success; unavailable or contradictory evidence
  preserves the unknown result.
- Advertisement bit 0 and decoded short-heartbeat push state are hints, never
  authoritative state writes. Battery still updates passively. Changed hints
  request a connected query. Applied state transitions identify command/query
  source, observation age, and selected-route RSSI in safe debug logs.
- Every initial full operation-log page remains history. Seeding completes only
  after a short or empty successful page; later genuinely new record numbers
  emit once with their original SDK timestamp.
- Existing RC5 connection candidate selection, cancellation/unload cleanup,
  per-lock serialization, reachability fallback, and background reconnect
  cadence remain intact.

Use [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md): first run three cold-idle
Unlocks and three cold-idle Locks with the HA adapter in **Auto**. Expand to ten
of each only after the six-action smoke test passes every physical, result,
state, duplicate-command, log, and scan-mode requirement.
