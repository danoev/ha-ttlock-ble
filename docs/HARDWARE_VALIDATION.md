# Real-lock validation checklist

Use a disposable test PIN that has never protected the door. Do not paste AES
keys, unlock keys, administrator PINs, account passwords, or active door codes
into an issue or shared log.

## Test environment record

Record these non-secret facts with every result:

- Home Assistant version and installation type
- integration commit and SDK commit/version
- lock make/model and firmware/protocol version
- direct Bluetooth adapter or ESPHome proxy name/version
- approximate RSSI and distance
- whether the same operation succeeds in the TTLock app

## 1. Baseline local operation

1. Restart Home Assistant and confirm the device and four baseline entities load.
2. Confirm passive advertisements update lock state and battery without opening a
   BLE connection.
3. Lock and unlock once from Home Assistant.
4. Lock and unlock once outside Home Assistant (keypad/app/mechanical control)
   and confirm the advertisement or event corrects the entity.
5. Repeat through an ESPHome active Bluetooth Proxy.

Expected: no TTLock gateway or phone is required after key bootstrap; a proxy
path is selected by Home Assistant and command failures do not leave an
optimistic state behind.

## 2. Auto-lock

1. Call `ttlock_ble.get_auto_lock` and record the returned delay.
2. Set a short, safe delay and read it back.
3. Unlock and confirm the lock re-locks once at that delay.
4. Set `seconds: 0`, read it back, and verify whether this firmware really
   disables auto-lock.
5. Restore the original value before ending the test.

Capture the status/error code only when rejected; do not capture encrypted key
material.

## 3. Passcodes

Use a new disposable PIN for each test:

1. Add a permanent PIN, unlock once, delete it, and confirm it no longer works.
2. Add a period PIN whose start is five minutes in the future and end is ten
   minutes later. Confirm rejection before start, success inside the window,
   and rejection after expiry.
3. Run `clear_passcodes` only on a lock where removing every keypad credential
   is acceptable and recovery through the administrator/app has been verified.

Expected: submitted PINs are absent from integration logs, diagnostics, entity
attributes, and event data. Home Assistant automation YAML/traces may still
contain values that the user placed there.

## 4. Capability and passage-mode SDK branch

With the `codex/passage-mode` SDK branch installed in a development environment:

1. Read the device-feature mask and record only the hexadecimal mask plus the
   derived booleans.
2. Query passage schedules before mutation and retain the decoded non-secret
   schedule list.
3. Add one short weekly interval for the current weekday.
4. Query again and confirm the exact interval is present.
5. Unlock during the interval and verify native passage behaviour survives
   without repeated Home Assistant unlock calls.
6. Delete the exact interval and confirm normal auto-lock behaviour returns.
7. Test `clear_passage_modes` only after individual delete succeeds.

Do not claim passage support for the model until add, query, behaviour, and
delete all pass on the physical lock.

## 5. Safe diagnostic capture

Enable debug logging only for `custom_components.ttlock_ble` and
`ttlock_ble.client`. Before sharing, search the exported text for the test PIN,
account email, AES key, unlock key, and administrator PIN. Redact the entire
line if any appears.

Useful safe artifacts are:

- non-secret lock model/protocol fields
- advertisement manufacturer data
- GATT service/characteristic UUIDs
- command opcode, response status, payload length, and timing
- capability mask
- decoded passage schedule fields

Turn debug logging off after the test.
