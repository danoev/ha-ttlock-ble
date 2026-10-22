# TTLock BLE local-management project status

Last updated: 2026-08-24

## Current branches

- Integration: `codex/rc10-release-candidate`, created from validated RC9 commit
  `ed78634b9e52d4f7588f2eac4bee6c93be1b7361`.
- SDK: `codex/rc10-connect-budget`, created from SDK tag `v0.1.11`.
- RC9 and SDK 0.1.11 tag history remain unchanged. Neither default branch nor
  either upstream has been modified or merged.

The maintained forks are [`danoev/ha-ttlock-ble`](https://github.com/danoev/ha-ttlock-ble)
and [`danoev/ttlock-ble`](https://github.com/danoev/ttlock-ble). The original
MIT projects by [`roquerodrigo`](https://github.com/roquerodrigo) remain credited
in Git history, licensing, and documentation. GPL reference implementations
were not copied.

## RC10 investigation conclusions

Confirmed:

- RC9 blocks coordinator publication on operation-log retrieval after the
  authoritative state query has already succeeded.
- A failed coordinator poll can replace a previously authoritative state and
  battery with Unknown.
- The protocol 5.3 lock can return a short log page and later expose a full
  historical page; page length is not a reliable baseline-completion signal.
- `ttlock-ble==0.1.11` hard-codes three `bleak-retry-connector` attempts. The
  integration cannot safely shorten only the speculative cached route through
  its public API.
- HACS stores and displays the repository `full_name` used at registration.
  There is no supported `owner` key in `hacs.json`; the fork's GitHub metadata
  correctly reports `danoev/ha-ttlock-ble`.

Strongly inferred:

- A one-attempt cached-route budget should remove most of the observed
  36-second stale-history penalty while preserving one robust fresh-route
  attempt sequence. Hardware timing remains required.
- `has_new_records` and state-bit changes can be useful activity hints, but
  firmware behavior for each external method remains unverified.

Unknown/deferred:

- Whether every fingerprint, PIN, card, key, and official-app operation toggles
  a usable advertisement hint on this firmware.
- The exact lock-RTC drift bound. RC10 therefore suppresses undated or
  pre-start ambiguous log entries rather than risking historical automation
  floods.
- Friendly credential/user labels. Neither the pinned SDK nor the integration's
  existing cloud bootstrap exposes an authoritative non-secret mapping API.

## RC10 implementation

- Authoritative state/battery publish before asynchronously scheduled log sync.
- Failed polls retain the last authoritative state and battery.
- Decoded advertisements log safe state/new-record/setting/battery/RSSI fields;
  changed activity hints request connected reconciliation.
- A private bounded storage journal persists SHA-256 identities made only from
  non-secret record metadata. Historical, duplicate, replayed, and ambiguous
  records are suppressed across restart.
- Default lifecycle is on demand after startup. Unknown state retries with
  bounded backoff; the hourly poll remains. Explicit legacy maintenance and
  permanent connection are retained as options.
- SDK commit `53c78183e3ab7c1e96d6f9912a983b515388546c`
  exposes a per-client connector-attempt budget. RC10 gives a cached route one
  speculative attempt and a fresh route the normal three.
- Events gain safe method classification, including physical keys. The SDK's
  overloaded credential field is never exported.

## HACS ownership

The fork repository, manifest, README HACS button, issues, topics, and source
codeowners point to `danoev`. HACS's current source constructs repository data
from the registered `owner/name` and returns that `full_name` to its frontend.
If an existing HACS card still shows `roquerodrigo`, remove the upstream custom
repository entry, add `https://github.com/danoev/ha-ttlock-ble` as an Integration,
enable prerelease versions, refresh repository information, and select RC10.
Changing `hacs.json` cannot migrate HACS's persisted upstream registration.

## Release gate

Automated results are recorded only after local and hosted pytest/coverage,
Ruff, mypy, hassfest, HACS, and CodeQL finish. RC10 remains hardware-validation
only until [the ordered real-lock runbook](docs/HARDWARE_VALIDATION.md) passes.
