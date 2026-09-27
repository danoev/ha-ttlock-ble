# TTLock local-management capability matrix

This matrix records the Phase 0 investigation performed against the modern
`roquerodrigo` SDK and Home Assistant integration, plus the requested protocol
references. It describes repository state at the start of the
`codex/management-actions` work.

## Licensing boundary

- `roquerodrigo/ttlock-ble` and `roquerodrigo/ha-ttlock-ble` are MIT licensed.
- `kind3r/ttlock-sdk-js` and `kind3r/esp32-ble-gateway` are GPL-3.0 licensed.
- `kind3r/hass-addons/ttlock-hass-integration` is Apache-2.0 licensed.
- `rabeckas/ttlock-local-ble` is MIT licensed.

The Kind3r repositories are protocol references only. New Python code is an
independent implementation of observed command behaviour and is covered by
new byte-level tests; GPL source is not copied into either MIT repository.

## Capability matrix

| Feature | Modern SDK | Modern HA integration | Kind3r reference | Other reference | Work required |
|---|---|---|---|---|---|
| Native HA Bluetooth and ESPHome proxy transport | Supported through a caller-supplied `BLEDevice` | Supported through HA's Bluetooth manager | Uses Noble/custom gateway | `ttlock-local-ble` uses HA Bluetooth | Retain the native HA path; do not revive the bespoke gateway |
| Lock/unlock | Supported | Lock entity | Supported | Supported | Hardware regression test |
| Passive bolt state and battery | Advertisement decoder | Coordinator and entities | Supported | Limited | Hardware regression test across the proxy path |
| BLE connectivity | Client state | Binary sensor | Supported | Limited | Retain |
| Operation log and push events | Supported, with decoded records | Event entity with PIN redaction | Broad record mapping | Basic protocol notes | Remove any remaining sensitive plaintext debug output; extend classifications only from verified records |
| Lock RTC read/sync | Supported | Not user exposed | Supported | Protocol notes | Add a safe management action or periodic drift policy after hardware validation |
| Auto-lock read/set/disable | Supported (`0` disables) | Not exposed | Supported | `ttlock-local-ble` only simulates the UI state timeout | Add HA control/action; verify supported range on hardware |
| Permanent passcodes | Add/delete/clear | Not exposed | Add/update/delete/list | Protocol notes | Add secure HA actions; never expose PINs in states/events/logs |
| Period passcodes | Add/delete/clear | Not exposed | Add/update/delete/list | Protocol notes | Add secure HA actions and datetime conversion |
| Count passcodes | Type enum and generic payload path, not hardware verified | Not exposed | Type/list parsing present | Protocol notes | Treat as experimental until payload semantics and remaining-use reporting are verified |
| Cyclic passcodes | Type enum and generic payload path, not hardware verified | Not exposed | Declared incomplete in the reference README | None verified | Do not claim support until packet and hardware fixtures exist |
| Passcode listing | Absent | Absent | Implemented | Protocol opcode documented | Independently implement parser, then verify pagination and redaction on hardware |
| Passcode update | Absent | Absent | Implemented | None verified | Independently implement only after add/delete hardware tests are stable |
| Friendly credential names | Not a lock capability | Absent | Add-on UI stores metadata | None | Store only non-secret aliases locally and clearly label them as HA metadata |
| Passage mode schedules | Absent | Absent | Query/add/delete/clear | None verified | Independently implement weekly/monthly payloads and response parser, then hardware test |
| Capability discovery | Opcode declared, no parser/model | Absent | Feature bit set parser | None verified | Add typed capability model and suppress unsupported HA controls when reliable |
| Lock sound | Absent | Absent | Query/modify | None verified | Add after capability discovery and hardware fixture |
| IC cards | Absent | Absent | Enrol/list/delete/clear | None verified | Later phase; interactive enrolment needs HA-appropriate UX |
| Fingerprints | Absent | Absent | Enrol/list/delete/clear | None verified | Later phase; enrolment must be an explicit progress workflow |
| Pairing/init | Partial SDK primitives, not a supported HA flow | Cloud or manual-key bootstrap | Supported | Protocol notes | Keep cloud/manual bootstrap for now; do not risk lock ownership during early phases |

## Baseline validation

- SDK: ruff, strict mypy, and 287 tests pass; measured coverage is 98.44%.
- HA integration: ruff, mypy, and 269 tests pass; measured coverage is 100%.
- The HA `uv.lock` is accepted with `uv sync --frozen`; current `uv sync
  --locked` reports that the upstream lockfile metadata would be rewritten.
  The baseline lockfile was intentionally left unchanged.

## Implementation order

1. Secure protocol primitives and tests in `ttlock-ble`.
2. Home Assistant management actions and native entities.
3. Real-lock validation through Home Assistant/ESPHome Bluetooth Proxy.
4. Credential-management UX after the backend is stable.
