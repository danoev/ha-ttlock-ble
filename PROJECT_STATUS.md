# TTLock BLE local-management project status

Last updated: 2026-08-23

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
  `codex/hardware-validation-0`, created from `codex/management-actions` for
  the `3.5.1rc8` prerelease candidate. The original repository remains
  the `upstream` fetch-only remote and
  [`danoev/ha-ttlock-ble`](https://github.com/danoev/ha-ttlock-ble) as
  `origin`.
- Both feature branches are pushed to their forks and tracked locally. Draft
  PRs [SDK #1](https://github.com/danoev/ttlock-ble/pull/1) and
  [HA #2](https://github.com/danoev/ha-ttlock-ble/pull/2) exist only to run
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
- Diagnosed the RC4 cold-idle gate and recorded the confirmed/inferred/unknown
  evidence in [the RC5 investigation](docs/RC5_COLD_IDLE_INVESTIGATION.md).
- Refined explicit BLE acquisition for RC5: after an aggregate connectable
  history miss, the integration accepts a record for the exact address from an
  HA-registered connectable local/proxy scanner and starts the existing SDK
  GATT/retry path. One HA-managed 25-second active scan remains the fallback
  only when neither representation exists. Background acquisition remains
  non-active and does not use the new explicit scanner-path fallback.
- RC5 real hardware proved 6/6 physical cold-idle lock/unlock but exposed five
  false acknowledgement failures, three false later Locked states, and a
  historical operation-log flood.
- RC6 adds safe command-stage attribution and a typed post-write unknown
  outcome. It never resends ambiguous lock/unlock and reconciles only from one
  fresh connected query.
- RC6 treats advertisement and decoded short-heartbeat state as hints, records
  source/age/RSSI for authoritative transitions, and keeps full initial log
  pages in seed/history mode until a short page completes synchronisation.
- RC6 changes the persistent exact-address tracker to Passive and uses HA's
  exact-address `async_process_advertisements` Active wait for explicit misses,
  allowing the adapter to remain in Auto and preserving proxy routing.
- RC7 uses the same HA-native bounded acquisition for the first authoritative
  query while coordinator state is Unknown. Successful query state is
  published; a timeout remains Unknown. Known-state routine polling and the
  background maintenance loop remain non-active.
- RC8 treats a cached/per-scanner route that exhausts its pre-command GATT
  attempt as stale for that acquisition. Active-capable callers then request
  exactly one bounded exact-address HA Active window, reject replayed history
  by service-info receipt time, and use the fresh callback's direct/proxy route.
  Authentication and command dispatch occur only after this fallback, so lock
  and unlock remain single-send. Background maintenance stays non-active.

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
- `ha-ttlock-ble`: 332 tests passed with 98.13% coverage; Ruff and configured
  mypy pass locally. Hosted RC8 checks are pending publication.
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

HACS derives the displayed integration authors from `manifest.json` at the
version it has selected for download. Fork prerelease tags use `@danoev`, but
the fork's default `main` still points at the upstream stable source containing
`@roquerodrigo`. With prerelease display disabled—or before a forced repository
refresh clears cached metadata—HACS therefore continues to show the stable
default-branch author. The RC8 source metadata itself is correct; this is a
version-selection/cache effect and is non-blocking for Bluetooth validation.

A local Home Assistant `check_config` run is not a faithful gate in this
workspace because Home Assistant's internal dependency command splits the
workspace path containing spaces; this is an environment limitation, not a
passing validation claim.

## Background connection and battery assessment

The background maintenance loop exists to keep re-establishing a GATT session
after TTLock's short idle disconnect so SDK push events can be received when a
session happens to be live. The connection binary sensor also reflects those
sessions. Passive battery and state-change hints do not depend on the loop;
authoritative state now requires a connected query.

Push events do depend on a live authenticated client and are therefore only
real-time during an open session. Operation logs do not depend on the maintain
loop itself; they are explicitly read after coordinator state queries and
successful lock/unlock commands, using whatever connection is available then.
Removing background sessions would lose live push delivery between on-demand
connections and could delay log discovery until a later explicit/coordinator
read, but it would not remove passive battery/hint updates or command support.

With a roughly five-second idle session and the default 300-second post-drop
cooldown, the loop can establish on the order of 280 background GATT sessions
per day when the lock is continuously reachable. Each establishment requires
radio activity and authentication, so its battery cost is necessarily higher
than passive-advertisement plus on-demand operation, although the actual life
impact needs a controlled current or battery-duration measurement on real
hardware.

Recommendation: make an on-demand-only mode the preferred low-battery design
for users who accept losing between-command real-time push events, while
retaining periodic/persistent listening as an explicit opt-in. The existing
`permanent_connection` option should remain explicit because reconnecting
immediately after every idle drop is the highest-drain mode. RC8 deliberately
does not change the maintenance loop or its defaults; that architecture change
needs separate UX, migration, event-loss and hardware-battery validation.

A successful background maintenance connection authenticates a client but does
not itself call `query_state()` or publish coordinator state. Candidate misses
back off through the connection loop's bounded retry cadence and can therefore
be quiet in the UI. A long observed gap between successful connections is not
evidence of a configured one-hour reconnect interval; RC8 leaves this separate
battery/event-delivery design question unchanged.

Advertisement-history clearing was reviewed but is not used. Home Assistant
documents it for forcing an otherwise identical advertisement to be processed
as new. RC8 instead reads existing HA aggregate and per-scanner representations;
the current HA implementation also removes connectable history, so clearing it
could discard useful state and affect other Bluetooth consumers of that
address.

## Hardware status

The protocol 5.3 / scene 2 lock at `B6:D4:1E:DB:15:F8` has proved passive
battery, authenticated GATT, and 6/6 physical cold-idle lock/unlock through Home
Assistant under RC5. RC7 then produced four successful cold-idle commands under
Auto before a later command repeatedly used approximately three-minute-old HA
connectable history. RC8 must prove fresh-route fallback for that case, retain
authoritative startup bootstrap, and pass the 3 Unlock + 3 Lock smoke test
without false failures, state reversal, duplicate commands, or historical-log
flood. The 10 + 10 gate remains required for release readiness.

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

1. Install the `3.5.1rc8` GitHub prerelease as a HACS custom-repository version
   and run the Auto-mode stale cached-route case plus Unknown-state startup
   bootstrap in the updated runbook, followed by the cold-idle 3 unlock + 3
   lock smoke test with no phone/keypad interaction. Expand to 10 + 10 only
   after all three pass.
2. Do not resume PIN, passage, card, fingerprint, or other feature development
   until that run passes and its sanitized timing/route evidence is reviewed.
