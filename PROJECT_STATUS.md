# TTLock BLE local-management project status

Last updated: 2026-08-21

## Architecture

The project retains the existing split between the two modern MIT-licensed
upstreams:

```text
TTLock-family lock
  -> Bluetooth LE
  -> Home Assistant Bluetooth manager / ESPHome active Bluetooth Proxy
  -> ha-ttlock-ble
  -> ttlock-ble
```

`ha-ttlock-ble` remains the HACS custom-integration chassis. `ttlock-ble`
owns BLE transport and TTLock V3 protocol behaviour. The Kind3r projects are
used only as protocol references: their GPL-3.0 implementation source is not
copied into either MIT project.

## Repository and branch state

- SDK checkout: `ttlock-ble`, branch `codex/passage-mode`, original repository
  configured as the fetch-only `upstream` remote and
  [`danoev/ttlock-ble`](https://github.com/danoev/ttlock-ble) as `origin`.
- Home Assistant checkout: `ha-ttlock-ble`, branch
  `codex/management-actions`, original repository configured as the `upstream`
  fetch-only remote and
  [`danoev/ha-ttlock-ble`](https://github.com/danoev/ha-ttlock-ble) as
  `origin`.
- Both feature branches are pushed to their forks and tracked locally. Draft
  PRs [SDK #1](https://github.com/danoev/ttlock-ble/pull/1) and
  [HA #1](https://github.com/danoev/ha-ttlock-ble/pull/1) exist only to run
  hosted checks; neither fork default branch nor either upstream is modified.
- The upstream Git history is preserved and the work is split into small
  protocol, integration, and documentation commits.

## Completed investigation and implementation

- Audited the modern SDK and integration, all four requested reference
  projects, repository licences, relevant open issues, and recent upstream
  changes. See [the capability matrix](docs/CAPABILITY_MATRIX.md).
- Confirmed the existing local lock/unlock, state, battery, advertisement,
  operation-log, Home Assistant Bluetooth manager, and ESPHome proxy paths are
  retained without architectural replacement.
- Added an independently implemented SDK passage-mode command (`0x66`) with
  weekly/monthly models and query, add, delete, and clear operations.
- Added an arbitrary-width lock-feature parser and typed capability model.
- Removed/redacted protocol logging and error paths that could reveal PINs.
- Added Home Assistant actions for permanent and date/time-window passcode
  creation, passcode deletion/clearing, and auto-lock read/set/disable.
- Added action schemas, device targeting, timezone conversion, translated
  errors, unloaded-device handling, and secret-redaction tests.
- Added a [real-lock validation checklist](docs/HARDWARE_VALIDATION.md).

The Home Assistant integration intentionally continues to depend on the
released `ttlock-ble==0.1.11`. Its passcode and auto-lock actions therefore
remain dependency-resolvable for HACS. Passage-mode support is isolated on the
SDK feature branch until the byte-level implementation is verified on real
hardware and an SDK release exists; the integration does not claim passage
mode support yet.

## Validation status

Baseline, before feature changes:

- `ttlock-ble`: 287 tests passed, 98.44% coverage; Ruff and configured strict
  mypy passed.
- `ha-ttlock-ble`: 269 tests passed, 100% coverage; Ruff and configured mypy
  passed.

Current local branches:

- `ttlock-ble`: 303 tests passed, 97.31% coverage; Ruff and configured strict
  mypy pass.
- `ha-ttlock-ble`: 285 tests passed, 99.38% coverage; Ruff and configured mypy
  pass.
- `manifest.json`, `hacs.json`, translation JSON, and `services.yaml` parse;
  the manifest and project versions agree.

Hosted validation on the draft PRs passes:

- SDK: Ruff, strict mypy, pytest, and CodeQL.
- Home Assistant: Ruff, mypy, pytest, version consistency, hassfest, HACS, and
  CodeQL.

The first HACS run identified fork metadata defaults rather than source
errors: the new fork had no topics and Issues were disabled. The fork now has
the same HACS topics as upstream and Issues enabled; the unchanged HACS job
passes on rerun. The integration code/layout, manifest, and `hacs.json` checks
all pass.

A local Home Assistant `check_config` run is not a faithful gate in this
workspace because Home Assistant's internal dependency command splits the
workspace path containing spaces; this is an environment limitation, not a
passing validation claim.

## Known-working locks

No physical lock model has been validated on these feature branches yet. The
existing upstream-supported behaviour and fixtures continue to pass, but no
new management feature should be labelled hardware-supported until the
checklist captures the lock model, firmware, transport route, and sanitized
request/result evidence.

## Unsupported or intentionally deferred features

| Feature | Status | Likely difficulty / reason |
|---|---|---|
| Count/use-limited PIN | Deferred | Medium: the existing enum/payload route needs real-lock semantics and remaining-use evidence |
| Recurring/cyclic PIN | Deferred | High: reference support is incomplete and packet/hardware fixtures are missing |
| List/read or update PINs | Deferred | Medium-high: pagination, response parsing, and strict secret handling need independent implementation and hardware traces |
| Friendly credential aliases | Deferred | Medium: requires a clearly separated, non-secret HA metadata store and lifecycle design |
| Passage mode in HA | Deferred pending SDK validation | Medium-high: SDK bytes exist, but scheduling semantics and firmware support require hardware proof and capability gating |
| Sound setting | Deferred | Medium: needs query/modify protocol work and capability/lock-variant validation |
| IC cards | Deferred | High: enrolment, listing, validity, and cancellation need an interactive HA workflow |
| Fingerprints | Deferred | High: enrolment is interactive and requires progress/cancellation UX plus hardware validation |
| Lock-clock UI/drift policy | Deferred | Medium: SDK primitives exist; safe synchronization policy and temporary-code interaction need hardware testing |
| Richer operation classifications | Deferred | Medium: only verified record values may be exposed, and credential values must stay redacted |
| Custom credential-management UI | Deferred | High: backend and hardware behaviour must stabilize first |

## Unresolved protocol and hardware questions

- Does the test lock advertise the SDK feature bits expected for passage mode,
  each PIN type, cards, fingerprints, sound, and lock clock?
- Does passage-mode opcode `0x66` use the documented weekly/monthly schedule
  layout and paging sequence on the test firmware?
- Does auto-lock value `0` reliably disable the feature, and what delay range
  does the lock enforce?
- Which PIN types can be added, queried, updated, and deleted locally on this
  model, and what data can the lock safely return?
- Are BLE writes and notifications reliable through the user's ESPHome active
  proxy for each management command?

## Next milestone

1. Install the HA branch in the user's test Home Assistant environment and run
   the baseline plus passcode/auto-lock portions of the hardware checklist.
2. Capture capability bytes and sanitized passage-mode responses, convert them
   into reusable SDK fixtures, refine the protocol if required, and only then
   expose capability-gated passage-mode actions in Home Assistant.
