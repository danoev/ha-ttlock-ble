# RC16 publication handoff — not published

This candidate requires separate publication approval after exact draft-PR CI,
CodeQL, HACS and hassfest gates are green. Preserve `v3.5.1rc12` through
`v3.5.1rc15`, their annotated tags/releases and both main branches. Never merge
these lineage-specific development PRs into current SDK main implicitly.

Metadata is `3.5.1rc16` in manifest, project, lockfile and release manifest.
The manifest/project/lockfile pin SDK
`73e6c89544dd178cb09de9b88f29f5cf204b2429`, immutable archive SHA-256
`49ae5d7ccc53c16a96de49089a31ebcbce424d9bbde447263df1b313269cd112`.
The dependency remains the preserved 0.1.11 transport lineage.

The existing `publish-hacs-zip.yml` runs only on a **published GitHub Release**.
It archives the exact tag's `custom_components/ttlock_ble` tree at the ZIP root;
this includes `frontend/administration.js`. Python/tests/dev scripts/docs are not
installed except the integration's own runtime files. `hacs.json` already sets
`zip_release=true`, `filename=ttlock_ble.zip`; no workflow trigger changes needed.
No tag or GitHub Release is created by preparing or opening the draft PR.

On separately authorized publication: reverify exact heads, checks, dependency
archive digest and unused tag; create an annotated `v3.5.1rc16` at the approved
integration SHA; publish as **prerelease**; let the existing workflow attach the
ZIP; verify root paths, exact source bytes, all versions, SDK pin and asset digest.
Only then install through HACS with prereleases enabled and restart HA. Never
copy these developer files into the live HA instance.

Release notes must clearly identify the admin-only local **metadata** panel,
explicit names/private lifecycle, legacy service privacy/opt-out, positive S31
read evidence and still-partial PIN coverage. New credential writes, verified
person identity, full schedules and automatic Alarmo disarming remain disabled.
No live HA or credential operation occurred during this software cycle.
See [security boundary and future hardware plan](RC16_SECURE_ACCESS.md).
