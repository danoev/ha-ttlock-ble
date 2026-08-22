# TTLock BLE 3.5.1rc2

This is a **hardware-validation-only prerelease**. It is not production
certified and should be installed only on a lock with a tested recovery route.

The build continues to use the released `ttlock-ble==0.1.11` SDK. Passage mode
is excluded; its SDK work remains isolated on `codex/passage-mode` and is not
present or claimed here.

Change since `3.5.1rc1`:

- When Home Assistant has no connectable BLE device for the lock, the
  integration now requests Home Assistant's Bluetooth reachability diagnosis.
  The human-readable scanner, history, connection-slot and proxy information
  is written to integration debug logs and included in an attempted lock or
  management action's existing `TTLockError`.
- Diagnostic generation is best-effort and does not change device selection,
  connection attempts, retry timing, command execution or state-query return
  behaviour.
- TTLock AES/unlock keys, administrator PINs and submitted keypad PINs are not
  passed to or included in the reachability diagnostic.

Features under real-lock test remain:

- add, physically use and delete one disposable permanent PIN
- add and test one disposable period PIN before, during and after its window
- delete a single disposable PIN
- read and set the native auto-lock delay
- test auto-lock disable with `seconds: 0`, then restore the original delay

Do not initially test `clear_passcodes`, administrator-code changes, factory
reset, unpairing, or passage-mode clearing. Follow the repository's
[staged hardware-validation runbook](https://github.com/danoev/ha-ttlock-ble/blob/v3.5.1rc2/docs/HARDWARE_VALIDATION.md)
and its restricted logging/redaction guidance.
