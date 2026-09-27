# RC12 corrective candidate and validation handoff

This is a local candidate, not a published release. No push, tag, release,
merge, main update, PR modification or branch cleanup was performed.

## Candidate provenance

Integration: `/Users/daniel/Documents/ChatGPT/HA TTLock/ha-ttlock-ble`,
branch `codex/rc12-release-candidate`, based exactly on published RC11
`113fed005a755ae1767f7ebcf2bc21756b0b8ece`. RC11 was first fast-forwarded
locally from `1295e089b0c179a0fa8e047937cbb68b038784b7` through the three
verified linear commits `f90cf9a`, `274a37b`, `113fed0`.
Use `git rev-parse codex/rc12-release-candidate` for the final integration SHA.

SDK: `/Users/daniel/Documents/ChatGPT/HA TTLock/ttlock-ble`, branch
`codex/rc12-connection-safety`, final SHA
`65a196a4415516039fd98fe40ccdccc7dccfec23`, based exactly on immutable
RC11 pin `53c78183e3ab7c1e96d6f9912a983b515388546c` (SDK 0.1.11).
Its changes were not based on upstream SDK 0.3.x.
The pre-existing untracked SDK `.coverage 2` remains untouched.

Protected integration refs remain: main `698bb4680620a3ae9a4d33e4f14abf47a59f0e87`,
RC10 branch/tag `c5127ca3d02cad62a39d9dd15c80c05cd3184d46`, exception audit
`66f17d32330bab762143d376426fc36409a17725`, RC11 branch/tag target `113fed0`.
The annotated, unsigned RC11 tag and existing release/ZIP were not rewritten.

## Reviewable commits

Integration, in order:

- `b819e36` test: reproduce authoritative state ordering race
- `000ea91` fix: order authoritative observations per lock
- `7930d74` fix: require explicit SDK connection cleanup evidence
- `0dfd69b` test: enforce release version consistency
- `0c2c507` chore: prepare 3.5.1rc12 metadata
- `e4f0f99` chore: pin final SDK diagnostic safety commit
- `0691243` docs: record RC12 validation and hardware handoff
- `29fe6e9` fix: reject connections that suppress acquisition cancellation
- Final documentation correction: see `git log`.

SDK, in order:

- `ca0c0d8` test: define pending connection teardown contract
- `8f5b768` fix: retain truthful BLE setup ownership
- `06349ae` fix: redact credential-bearing SDK diagnostics
- `d3095d3` fix: require released handle in SDK cleanup evidence
- `42c5e45` test: exercise connector ownership and transient retries
- `65a196a` fix: redact malformed frame diagnostics

## Authority ordering

The coordinator owns a monotonic integer revision for each lock. It stamps a
connected query immediately when that query returns, before waiting for any
other lock. Acknowledged commands/direct authoritative queries receive the
same per-lock sequence. Aggregation starts with current coordinator data and
rejects query observations older than the published revision. Attribution
retains query-time monotonic age, source and RSSI, rather than aggregation time.
Advertisements and supplementary logs never advance authority. A newer query
can supersede an older acknowledged command. Entity command settle behavior
is retained from RC11.

Six deterministic ordering tests cover slow lock B, old query/new command,
new query/old command, aggregation cancellation, concurrent per-lock results,
attribution, advertisements/logs and a newer directly published query.
Failed polling/log work continues to preserve known state.

## Truthful cleanup contract and limits

A minimal SDK-owned subclass of the connector's public Bleak client registers
its handle during construction, before the first connection await. The SDK
retains this handle when establishment raises or is cancelled. Disconnect is
attempted even when the public connected flag is false. Explicit evidence is
true only before setup was attempted, or after owned backend disconnect
returned successfully, the public connected flag is false and the SDK released
its handle. A missing handle after an attempted setup is uncertainty, not proof.

Integration cleanup now requires that explicit property. Uncertain candidates
remain pending; replacement acquisition is blocked; unload reports failure
rather than silently dropping ownership. Existing retained cleanup tasks,
generation callbacks, deadline cancellation/draining and committed-control
reconciliation remain in place. Repeated cancellation tests remain enabled.
A post-connect guard rejects a normal return if the acquisition timeout expired
or the caller remains cancelled. Two deterministic regressions reproduced both
unsafe late-adoption paths before the guard and pass with it; neither path can
reach authentication/control. This changes no budget values.

This is the strongest evidence available through public backend APIs. It does
not attest to physical RF or proxy-slot release independently of the backend.
A constructor failure before registration is conservatively quarantined and
cannot be recovered automatically through an unavailable handle. Cleanup or
cancellation draining can exceed the acquisition budget if a backend stalls;
we retain ownership rather than claim release or start overlapping work.

No timing retune: cached phase 16s; Active discovery <=25s clipped to remaining
time; fresh phase <=42s; control/management 60s total; routine known state 16s;
Unknown bootstrap and ambiguous reconciliation 48s. Cancellation is drained
before cleanup/replacement and cannot progress into integration authentication
or control after acquisition expiry. Commands remain single-send; ambiguous
post-write outcomes use queries only. HA manages the one fresh Active path;
no global Active, raw/private BlueZ, RSSI cutoff or default background maintenance.

## SDK diagnostic safety

Payload dumps were removed from state, log, auto-lock, authentication, passcode,
control and clock diagnostics. TX/RX logs retain opcode/size, not wire bytes.
Authentication success no longer prints psFromLock. Status/result/echo/count
information remains. Payload-bearing parser and malformed-frame errors retain
size/status instead of bytes. Invalid cloud key-row diagnostics retain exception
type only. Push failures cannot log arbitrary payload-bearing exception text.
No protocol encoding, encryption, payload construction or command behavior changed.

Thirteen logger/parser safety cases assert that AES/unlock/admin material,
PINs, log credential samples, authentication tokens, ASCII, hex and full wire
samples are absent from diagnostics, including rejection and malformed push.

## Versions and packaging

Manifest, pyproject, release-please metadata and root uv.lock package all report
`3.5.1rc12`. The release workflow validates source versions against the release
tag before creating the archive and repeats the check against the ZIP manifest
before upload. Root-only install ZIP structure is still checked. Tests reject
stale source/project/release/lock/ZIP versions, wrong tags, nested archives and
RC11's tag/source defect. This does not rewrite RC11.

Manifest, dev requirements and lock all name the final SDK SHA archive URL.
The previous SDK archive hash was removed, not reused or fabricated for a new
commit. `uv lock --check --offline` passes, but the new GitHub archive is not
available until SDK publication is separately authorised. Before publication,
fetch that exact archive, verify its source against the SDK commit, resolve its
real archive hash into the lock and rerun validation. No unverified remote
archive/hash is claimed here.

Local testing installed a wheel built from the final SDK with no dependency
changes. All 40 installed SDK source files match final SDK HEAD byte-for-byte.
Wheel SHA256: `d27a7c481705db8b75ad76db0888ad29f59f5d288082d90298cefaa98030b921`.
Runtime: HA 2026.8.0, Bleak 3.0.2, bleak-retry-connector 4.6.3,
habluetooth 6.26.5. Isolated connector 4.7.0 tests leave those pins unchanged.

## Validation results and exact rerun commands

Integration: the pre-guard full run passed 441 tests at 96.69% coverage; the
final suite adds two cancellation-suppression cases (443 tests; 90% coverage gate).
See the final handoff for its exact coverage. SDK: 288 tests,
98.28% coverage. Ruff, format and mypy pass (38 integration/39 SDK source files).
The ten SDK ownership tests pass with 4.6.3 and isolated 4.7.0, including actual
connector transient retries beyond max_attempts=1 while retaining one backend.

Hassfest official image reports 1 integration, 0 invalid; the existing
CONFIG_SCHEMA warning remains. HACS official applicable validators use exact
local files plus read-only public GitHub repository metadata. This validates
integration manifest, hacs.json, information, brands, archived status,
description, issues, topics and license. It does not validate an unpublished
remote RC12 ref, release asset or dependency availability. The standard HACS
GitHub action must be rerun on the hosted exact candidate before release.

Run commands individually and stop on failure:

```sh
cd '/Users/daniel/Documents/ChatGPT/HA TTLock/ttlock-ble'
git rev-parse HEAD
.venv/bin/pytest
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy
.venv/bin/pytest tests/test_connection_ownership.py -q --no-cov
PYTHONPATH=/private/tmp/ttlock-rc12-connector47:src .venv/bin/pytest tests/test_connection_ownership.py -q --no-cov
git diff --check 53c78183e3ab7c1e96d6f9912a983b515388546c..HEAD

cd '/Users/daniel/Documents/ChatGPT/HA TTLock/ha-ttlock-ble'
git rev-parse HEAD
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
UV_CACHE_DIR='/Users/daniel/Documents/ChatGPT/HA TTLock/.tools/uv-cache' ../.tools/uv-latest/bin/uv lock --check --offline
git diff --check 113fed005a755ae1767f7ebcf2bc21756b0b8ece..HEAD
git archive --format=zip --output /private/tmp/ttlock_ble-rc12.zip HEAD:custom_components/ttlock_ble
.venv/bin/python scripts/validate_release.py --tag v3.5.1rc12 --archive /private/tmp/ttlock_ble-rc12.zip
/opt/homebrew/bin/docker run --rm -v '/Users/daniel/Documents/ChatGPT/HA TTLock/ha-ttlock-ble:/github/workspace:ro' ghcr.io/home-assistant/hassfest@sha256:cf7088cd0eaf291024165629853a1853f1a9fc2652b74141c5ed133499738760
/opt/homebrew/bin/docker run --rm --entrypoint python -e PYTHONPATH=/hacs -v '/Users/daniel/Documents/ChatGPT/HA TTLock/ha-ttlock-ble:/candidate:ro' -v '/Users/daniel/Documents/ChatGPT/HA TTLock/.tools/rc12-hacs-local.py:/audit/rc12-hacs-local.py:ro' -v '/Users/daniel/Documents/ChatGPT/HA TTLock/.tools/rc12-hacs-metadata.json:/audit/rc12-hacs-metadata.json:ro' ghcr.io/hacs/action@sha256:dc92fdad2f6ffbe74bffb7269d781ea8e064f52d9bb486cdf3925d74e7ab6ebf /audit/rc12-hacs-local.py
```

HACS adapter/metadata are preserved in the workspace `.tools` directory. Refresh
public metadata with read-only `gh api repos/danoev/ha-ttlock-ble` before reuse.
The isolated 4.7.0 installation is in `/private/tmp/ttlock-rc12-connector47`;
recreate it separately if that temporary directory is gone. Do not install it
into HA's environment. Docker QEMU platform/jemalloc notices are environmental.

## Files changed

Integration: coordinator.py, connection.py, manifest.json; tests/test_coordinator.py,
tests/test_connection.py, tests/test_packaging.py; scripts/validate_release.py,
scripts/__init__.py; publish-hacs-zip workflow; pyproject.toml, uv.lock,
.release-please-manifest.json, CHANGELOG.md and this document.

SDK: src/ttlock_ble/client.py, cloud.py, commands/clock.py,
commands/envelope.py, commands/session.py, protocol/frame.py;
tests/test_connection_ownership.py and tests/test_diagnostic_safety.py.

## Upstreamability and deferred work

Comparison used the external audit's exact upstream snapshots: integration
`98c4585a1ddb64917fe42683c7c050efa69f6f77`, SDK
`a1e9c8dce6bb13c3d9e43ad9b7b7b6d13ed77c24`. No upstream merge/adoption occurred.

| Generic patch | Applicability / existing upstream solution | Dependencies and evidence |
| --- | --- | --- |
| Integration b819e36 + 000ea91, per-lock ordering | Upstream still gathers results before aggregation and does not sequence observations; adaptation is needed for its broader snapshot fields. | Query-at-completion stamping, authoritative command hook, six race tests. |
| SDK ca0c0d8 + 8f5b768 + d3095d3 + 42c5e45, owned teardown | Upstream transport still assigns only after establish_connection returns and drops handles when disconnected; its split transport architecture is not an ownership fix. | Public connector subclass/kwargs contract; actual 4.6.3 and 4.7.0 tests; teardown uncertainty semantics must be agreed. |
| Integration 7930d74, require explicit cleanup evidence | Applicable where callers retain pending acquisition; upstream integration lifecycle differs. | New SDK property; blocked-replacement/unload tests plus existing repeated cancellation tests. |
| SDK 06349ae + 65a196a, safe diagnostics | Strong upstream candidate: upstream still logs psFromLock, plaintext operation/passcode lists and raw transport TX/RX. Split modules require adapting callsites, including new upstream features. | No integration dependency; thirteen payload/logger cases; opcode/status/size preserved. |
| Integration 0dfd69b, release version guard | General invariant, but release-please/ZIP/uv metadata details are fork-specific. | Standard library script and 18 packaging cases total; fail-before-upload workflow ordering. |

No upstream PRs were opened. Fingerprint entities, passage mode, beep/volume,
clock writes, Last Heard, device information and wider upstream refactors remain
future work. Assess them individually only after hardware validation; do not
bundle them into RC12.

## Required hardware validation next

1. Install only the reviewed RC12 tree with the exact local SDK commit in a
   controlled HA test environment. Verify HA reports 3.5.1rc12 and record HA,
   Bleak, connector and ESPHome/proxy versions. Keep background maintenance off.
2. Reproduce lock A's query before a newer acknowledged Unlock while slow/offline
   lock B delays the poll. After aggregation, A must remain UNLOCKED with command
   attribution. Repeat with Lock. Then physically change A and obtain a newer
   connected query: query attribution and the newer physical state must win.
3. Exercise cold-idle locks, unavailable/out-of-slot proxies and stale routes.
   Record timestamps for cached acquisition, the one Active window, fresh phase,
   acquisition expiry and cleanup separately. Confirm 16/25/42/60/48s policies;
   no late authentication or write after acquisition expires.
4. Cancel/unload during pending GATT acquisition, notification setup and cleanup;
   repeat cancellation. Confirm owned tasks drain, no replacement starts while
   uncertain, unload explicitly fails when cleanup cannot be established, and a
   known successful cleanup releases the proxy slot for the next acquisition.
5. Cancel immediately before and after a control write; simulate a lost reply.
   Confirm exactly one Lock/Unlock send. Positive acknowledgements finish; an
   ambiguous outcome performs only reconciliation queries and remains unknown
   on contradictory/failed reconciliation. Never infer state from advertisements.
6. Repeat known-state failed polls and supplementary log failures: preserve
   state/battery. Replay historical multi-page logs: no old operation-event flood.
7. Enable SDK debug logging locally, exercise passcode/log/authentication/error
   paths with test credentials and inspect output for plaintext, hex, tokens and
   wire dumps. Retain opcode/status/size evidence without copying credentials.
8. After separately authorised SDK hosting, fetch the exact archive, verify it,
   restore a real archive hash, rerun all checks and the standard hosted HACS
   action on the exact candidate. Verify a locally generated install ZIP reports
   rc12 and contains only component-root files before any publication decision.

## Readiness

Local implementation is reviewable and validated. Publication readiness is NO:
SDK hosting/archive verification and hash resolution, hosted exact-ref HACS
validation, and real HA/proxy hardware validation remain. No hardware behavior
or physical backend-release guarantee is claimed from mock tests. Stop here;
publishing/merging/tagging requires a separate explicit instruction.
