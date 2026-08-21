# TTLock BLE 3.5.1rc1

This is a **hardware-validation-only prerelease**. It is not production
certified and should be installed only on a lock with a tested recovery route.

The build continues to use the released `ttlock-ble==0.1.11` SDK. Passage mode
is excluded; its SDK work remains isolated on `codex/passage-mode` and is not
present or claimed here.

Features under real-lock test:

- add, physically use and delete one disposable permanent PIN
- add and test one disposable period PIN before, during and after its window
- delete a single disposable PIN
- read and set the native auto-lock delay
- test auto-lock disable with `seconds: 0`, then restore the original delay

Do not initially test `clear_passcodes`, administrator-code changes, factory
reset, unpairing, or passage-mode clearing. Follow the repository's
[staged hardware-validation runbook](docs/HARDWARE_VALIDATION.md) and its
restricted logging/redaction guidance.
