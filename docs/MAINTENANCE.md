# Maintaining the danoev distribution and contributing upstream

Rodrigo Roque's original TTLock BLE integration and SDK remain the upstream
projects, not obsolete editions. This MIT-licensed fork keeps original notices,
authorship and history, and directs fork support to danoev. No upstream endorsement
is implied. Repository names and HA domain stay unchanged.

## Upstream participation

- [SDK PR #101](https://github.com/roquerodrigo/ttlock-ble/pull/101), commit
  `c1f3c02f988f5947f00cdaaab9de0266547d1e18`, is based directly on original SDK
  main `8025f57cebc7de63a66035fd19e1efb042d0208f`. It removes decrypted PIN logging
  and secret-bearing parser/traceback diagnostics without changing protocol or
  pagination. 469 tests / 99.22% coverage, Ruff/format/mypy passed locally. Public
  CI/CodeQL require the upstream maintainer's first-contributor approval; this is
  not a local test failure. Respond to review on the same small branch, retain
  compatibility tests and original authorship, and never merge upstream yourself.
- [Card inventory PR #96](https://github.com/roquerodrigo/ttlock-ble/pull/96)
  remains another contributor's work. Our
  [hardware evidence comment](https://github.com/roquerodrigo/ttlock-ble/pull/96#issuecomment-6347025134)
  reports two four-byte S31 records from our independent implementation, not a
  hardware test of that PR. No eight-byte layout or schedule claim is made.
- [Fingerprint tracking PR #116](https://github.com/roquerodrigo/ha-ttlock-ble/pull/116)
  is still under review. Do not duplicate its devices/identity functionality.
  Complementary future discussion can cover credential-safe diagnostics, incomplete
  inventory uncertainty, lifecycle ordering and avoiding labels becoming claimed
  identity. Its HA2026.9 child-device requirement is not imported into our fork.

Separate future SDK patches can sanitize operation-log/fingerprint plaintext,
generic envelope/frame errors and handshake/token diagnostics. These were observed
in current original source and intentionally excluded from the first PIN-focused
PR. Use synthetic fixtures and review each data flow; do not post live captures.
The maintained older SDK already has its own safe diagnostic contract. Potential
state-ordering/cleanup contributions must be adapted to current upstream transport,
rather than cherry-picking the entire older fork.

The 22 October comparison found no new upstream runtime SDK commit since the
previous 27 September comparison: recent SDK commits are release configuration.
The integration's recent main changes are predominantly dependency maintenance.
Adopt compatible security/HA fixes deliberately; matching upstream version numbers
is not a reason to replace a tested transport. Upstream's lockfile release metadata
pattern is useful for a later automation change, but is not a runtime dependency.

## Repeatable maintenance cycle

1. Fetch original integration/SDK main and release refs without resetting local
   work. Record exact old/new SHAs, review only new runtime/security changes,
   changelogs and advisories. Check active upstream PRs before duplicating work.
2. Create an isolated `codex/` branch from the maintained distribution's main.
   Preserve release tags and unknown worktrees. Keep the immutable SDK pin/hash
   unless an independently tested SDK change is explicitly selected.
3. Port small compatible changes with source-commit attribution and focused
   regression tests. For upstream contributions, branch directly from current
   original main, omit fork metadata and keep scope narrow. Respond constructively
   to maintainer feedback; hardware claims must distinguish independent protocol
   evidence from tests of another contributor's exact code.
4. Update manifest, project, lock project and release manifest together. Run the
   full SDK/integration suite, Ruff/format/mypy, browser tests, compatibility/upgrade
   tests and dependency review. The Linux/Apple Silicon development lock avoids
   the current HA/ESPHome cryptography conflict on Intel macOS; it does not alter
   HA's runtime platform or dependency management.
5. Open a fork validation PR against maintained main. Require CI, CodeQL, HACS,
   hassfest, versions and browser checks at the exact head. Never accept a changed
   automation head silently. Advance main through a normal reviewed merge or
   verified fast-forward; never force-push, reset a shared branch, or merge a
   preserved historical draft merely for tidiness.
6. Run `python scripts/validate_release.py --tag vVERSION`. Confirm an unused
   version/tag and exact tested commit, create an annotated tag, then explicitly
   publish stable or prerelease as approved. The existing release-published
   workflow builds `ttlock_ble.zip`; compare every installed file with the tagged
   tree and compare the downloaded SHA-256 with GitHub's asset digest. Confirm
   default-branch HACS metadata and tagged metadata agree. Never modify old tags.
7. Install only the released asset through HACS. Hardware/UI verification is
   separately controlled; never operate household credentials or Alarmo as part
   of development. Keep release notes candid about coverage, freshness, identity,
   privacy and firmware limits.

The existing release-please job grooms a release PR after green main CI; it is
not permission to merge it or bypass these gates. Review generated version changes
in all four metadata files before future releases. New credential-management
development stays separate until protocol, authentication, acknowledgment,
cancellation and disposable-credential hardware evidence support each operation.
