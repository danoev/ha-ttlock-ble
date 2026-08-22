# TTLock BLE 3.5.1rc3

This is a **hardware-validation-only prerelease**. It is not production
certified and should be installed only on a lock with a tested recovery route.

The build continues to use the released `ttlock-ble==0.1.11` SDK. Passage mode
is excluded; its SDK work remains isolated on `codex/passage-mode` and is not
present or claimed here.

Change since `3.5.1rc2`:

- Explicit lock, unlock, add/delete passcode and auto-lock actions now request
  one Home Assistant-managed active scan when no connectable BLE path is
  initially available. The scan matches only the configured lock MAC and waits
  for a connectable advertisement for at most 25 seconds.
- After a matching advertisement, the integration resolves the connectable
  `BLEDevice` again and continues through the unchanged
  `TTLockClient.from_ble_device()` connection path.
- Simultaneous operations on the same connection share the existing operation
  mutex, so only the first starts a scan. Cancellation and integration unload
  cancel the wait and cannot open a late BLE connection.
- Coordinator state queries, operation-log polling, passive advertisement
  tracking, the background reconnect loop and `clear_passcodes` do not trigger
  the active scan.
- If the scan times out or produces no connectable path, the `3.5.1rc2`
  Home Assistant Bluetooth reachability diagnostic is logged and propagated.
  TTLock AES/unlock keys, administrator PINs and submitted keypad PINs remain
  excluded from logs and errors.

Features under real-lock test remain:

- add, physically use and delete one disposable permanent PIN
- add and test one disposable period PIN before, during and after its window
- delete a single disposable PIN
- read and set the native auto-lock delay
- test auto-lock disable with `seconds: 0`, then restore the original delay

Do not initially test `clear_passcodes`, administrator-code changes, factory
reset, unpairing, or passage-mode clearing. Follow the repository's
[staged hardware-validation runbook](https://github.com/danoev/ha-ttlock-ble/blob/v3.5.1rc3/docs/HARDWARE_VALIDATION.md)
and its restricted logging/redaction guidance.
