# TTLock BLE 3.5.1rc4

This is a **hardware-validation-only prerelease**. It is not production
certified and should be installed only on a lock with a tested recovery route.

The build continues to use the released `ttlock-ble==0.1.11` SDK. Passage mode
is excluded; its SDK work remains isolated on `codex/passage-mode` and is not
present or claimed here.

Changes since `3.5.1rc3`:

- Explicit lock, unlock, add/delete passcode and auto-lock operations still
  perform an immediate connectable-device lookup first. A cache hit connects
  without requesting an active scan.
- After an initial miss, RC4 requests one Home Assistant-managed active scan
  for at most 25 seconds and checks HA's local connectable-device cache every
  0.5 seconds. Success no longer depends on a new advertisement callback.
- The cache loop does not create repeated scanners, GATT connections or TTLock
  traffic. The first connectable `BLEDevice` continues immediately through the
  existing `TTLockClient.from_ble_device()` path.
- The per-lock operation mutex prevents simultaneous commands from opening
  overlapping scans or GATT connections. Cancellation and integration unload
  stop local acquisition and cannot create a late connection.
- Timeout preserves the detailed Home Assistant Bluetooth reachability
  diagnosis. Debug logs add safe acquisition and connection timing, source and
  RSSI without logging keys, administrator credentials or submitted passcodes.
- Advertisement history is not cleared: the HA API is intended to force
  identical advertisements through callbacks and would also discard the
  connectable history RC4 is polling.
- Fork-specific HACS metadata and the development-fork/upstream notice added
  after RC3 are included.

Passive advertisement tracking, coordinator refreshes, operation-log polling
and the background reconnect loop do not invoke the explicit active scan.
Follow the repository's
[staged RC4 hardware-validation runbook](https://github.com/danoev/ha-ttlock-ble/blob/v3.5.1rc4/docs/HARDWARE_VALIDATION.md)
and its restricted logging/redaction guidance. Do not proceed to passage mode,
IC cards, fingerprints or further credential work until RC4 acquisition is
validated against the real lock.
