# 3.5.1rc14 publication handoff — not published

This candidate builds on published RC13. SDK dependency:
`5bb8f4fa97f0b53f33b7713b5d1658ee1554fe4c` on the preserved 0.1.11 lineage,
archive SHA-256 `4dd4aa77f45775142fe5b630b474608340c2f82aa8abda101033d4f7d820bf98`.

## Release-note requirements

- Credential-free read-only inventory now distinguishes discovery, GATT, admin,
  parser, transport, deadline and cleanup failures. Safe PIN response-shape
  diagnostics enable a targeted S31 retest without decrypted frames.
- S31 fingerprint and four-byte card counts (two each) were corroborated against
  the phone app; names, individual identities, complete schedules and all new
  credential writes remain unverified/disabled.
- S31 PIN count one is still partial. Matching the app does not prove enumeration
  completion; the unrecognised final response needs a read-only diagnostic retest.
- Inventory retains RC13's bounded/preemptible policy and cleanup ownership;
  scanner attribution is separate from unverified GATT backend attribution.
- Administrator-only metadata and dormant protected-reference models are
  groundwork, not a secret-entry interface or household identity mapping.
- Existing PIN service inputs can appear in HA events, Recorder and automation
  traces. WebSocket input logging also prevents treating a simple admin command
  as a safe secret transport. No new secret submission is provided.
- RC14 hardware regression validation remains outstanding. Automatic Alarmo
  disarming remains disabled; the Aqara contact remains the intrusion sensor.

## Separate approval required

Before publication, verify exact live SDK/integration heads, all hosted checks,
immutable archive hash, clean candidate worktree and consistent source/package
versions. Confirm `v3.5.1rc14` and its release do not already exist. Record the
accepted exact integration SHA, create a new annotated tag at that SHA only,
and create a GitHub **prerelease** after explicit user approval.

The existing `publish-hacs-zip.yml` runs only on `release: published`. It checks
out the released tag, validates all version sources, packages tracked files
with `git archive HEAD:custom_components/ttlock_ble`, validates the root manifest
and attaches `ttlock_ble.zip`. No workflow trigger change is needed. CI on a
feature PR cannot publish this release. Do not reuse an existing tag/asset.

Verify workflow success, ZIP paths and exact bytes against the tagged integration,
manifest `3.5.1rc14`, asset SHA-256, and `hacs.json` (`zip_release: true`, filename
`ttlock_ble.zip`). Then install through HACS with prereleases enabled. Never copy
developer files into HA. RC12 and RC13 remain immutable fallback prereleases.

See [RC14 design, support matrix and safe hardware checklist](RC14_ACCESS_MANAGEMENT.md).
No stable-release verdict follows from software CI alone.
