# 3.5.1rc15 publication handoff — unpublished candidate

Accepted starting integration: `cfb71bdbf4c4163c674b16795960b81aea8d6c5e` (RC14).
Candidate SDK pin: `fda4233d3fb15ed256f5aa66ebd0f971587e4e9e` (0.1.11 lineage).
SDK archive SHA-256: `274d9be35212d618543b38d48132ab9f4747a3350e5cedf0f0e5bc59d0916725`.

The correction recognises the exact enveloped marker demonstrated in upstream's
hardware fixtures. It does not accept every short successful response and does
not assume the S31's undisclosed bytes. See [root cause, evidence, predicates
and controlled read-only test](RC15_PIN_PAGINATION.md).

Release notes must preserve the distinction between complete supported PIN
cursor enumeration and incomplete offline/batch coverage. S31 fingerprint/card
counts are already corroborated; individual identities, complete schedules and
new writes remain unsupported/unverified. Existing PIN service privacy and
administrator-gating limitations remain. Automatic Alarmo disarming is disabled.

No publication is authorised by development approval. After separate explicit
approval, verify exact candidate heads/checks, immutable SDK archive/hash and
all version sources, confirm `v3.5.1rc15` is unused, and create a new annotated
tag at the approved exact integration SHA. Publish a GitHub **prerelease** only.

The unchanged `publish-hacs-zip.yml` workflow responds to `release: published`,
checks out the tag and validates versions before packaging tracked integration
files at ZIP root and attaching `ttlock_ble.zip`. Verify file equivalence,
manifest `3.5.1rc15`, exact SDK requirement, digest and `hacs.json` metadata.
Use HACS for subsequent hardware installation; never copy developer files.
RC12/RC13/RC14 tags/releases, main branches and prior draft PRs remain unchanged.
