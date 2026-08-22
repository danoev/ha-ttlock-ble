# TTLock BLE 3.5.1rc5

This is a **hardware-validation-only prerelease**. It is not production
certified. Its release gate is repeatable cold-idle, no-touch lock/unlock on the
real protocol 5.3 / scene 2 lock; management and passage-mode work must remain
paused until that test passes.

RC4 could receive passive advertisements and list a connectable scanner in its
reachability diagnosis while still reporting no aggregate connectable history.
It treated that aggregate result as a prerequisite, so it never constructed an
SDK client and never reached the existing connection retries.

Changes since `3.5.1rc4`:

- Explicit lock/unlock and the already-scoped management acquisition path still
  reuse a live per-lock client and try HA's aggregate connectable lookup first.
- After an aggregate miss, RC5 checks HA's public per-scanner records for the
  exact address, restricted to scanners registered as connectable. A record can
  come from a local controller or supported Bluetooth proxy.
- The strongest reported record becomes a connection candidate immediately.
  There is no integration RSSI cutoff; HA's Bleak wrapper still chooses the
  actual backend/path when GATT starts.
- Only when neither representation has a candidate does RC5 request one
  HA-managed active scan and poll both in-memory representations for at most 25
  seconds. It does not create a standalone scanner.
- The candidate continues through unchanged
  `TTLockClient.from_ble_device()`. Released `ttlock-ble==0.1.11` delegates the
  actual GATT connection to bleak-retry-connector with three attempts and
  service caching.
- The existing per-lock mutex remains the single authority. Background,
  coordinator, log, management, and explicit operations cannot open competing
  connections; an explicit operation waiting behind successful background
  acquisition reuses that client.
- Safe timing logs distinguish reason, passive history, aggregate resolution,
  scanner source/RSSI, active-scan use, candidate time, GATT time, total time,
  and failure. Keys, administrator credentials, submitted PINs, and raw BLE
  payloads are not logged.
- Cancellation or unload during GATT now disconnects a partial or late client
  before it can be retained or used.

Battery behaviour is unchanged outside an explicit bounded acquisition: RC5
does not continuously active-scan, add polling traffic, alter passive
advertisement processing, or shorten the background reconnect interval. The
existing background-maintenance battery trade-off remains a known issue for a
separate phase.

The connection design is proxy compatible because candidate discovery and the
final connection remain inside Home Assistant's Bluetooth abstractions. No
BlueZ shell command, local-only scanner, raw adapter access, hard-coded `hci0`,
or address-only Bleak client is used.

See the [pre-change investigation](RC5_COLD_IDLE_INVESTIGATION.md) and run the
exact [10 unlock + 10 lock cold-idle procedure](HARDWARE_VALIDATION.md). A
single successful command is not validation. Remaining unknowns include the
official app's proprietary cold-idle sequence and the exact radio event that
causes aggregate HA connectable history to appear intermittently.
