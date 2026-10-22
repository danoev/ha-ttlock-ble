# RC13 publication and read-only hardware handoff

Version: **3.5.1rc13**, tag reserved for a future approved publication:
**v3.5.1rc13**. This document does not authorise publication. Keep published
RC12 at `49f4243b88bb490facaa96ed5e656a9c66a4c968` and its SDK pin unchanged.
The immutable SDK pin is `60604872e004d9dc7fd74aaf3ad260da9143b4c1`.
The exact RC13 candidate SHA is recorded in the final review report and the
existing integration draft PR. Do not merge either PR into main.

## Software acceptance

- Inventory acquisition uses at most 16 seconds inside a shared 30-second
  acquisition/read deadline. It does not queue behind active operations.
- Explicit Lock/Unlock cancels inventory, then waits for RC12-owned cleanup
  before its normal single physical attempt. Shutdown also cancels inventory.
- Cleanup is outside the read deadline. If the backend cannot prove teardown,
  ownership stays pending and no replacement connection or control write is
  allowed. A hard actuation deadline cannot safely override uncertain cleanup.
- Every inventory session closes, including successful and partial reads.
  `transport_interrupted: true` preserves partial safe evidence while explicitly
  rejecting a healthy-session interpretation. Cancellation propagates.
- An ended cursor is `enumeration_complete`, not complete credential coverage.
  PINs can be absent until physically used; cyclic restrictions and names are
  not fully discoverable; eight-byte card records remain unverified.

Run the complete local and hosted SDK/integration checks against the accepted
SHAs. Verify PR head SHA after all checks, including `update-pr-branch`.
Manifest, pyproject, release metadata and uv.lock must all say `3.5.1rc13`.

## Publication handoff — separate approval required

1. Verify remote `v3.5.1rc13` and its Release do not exist. Stop on a collision;
   never move a tag or overwrite a pre-existing release.
2. Use the exact accepted integration commit, not a moving branch. Validate
   `python scripts/validate_release.py --tag v3.5.1rc13`. Build a local review
   ZIP using the same tracked integration subdirectory as the release workflow
   and validate it with `--archive`. This ZIP is for packaging inspection only.
3. Once publication is explicitly approved, create a new annotated tag at that
   exact commit and a GitHub **prerelease** for it. Keep it separate from stable
   and preserve RC12. Do not use release-please to merge into main for this RC.
4. `.github/workflows/publish-hacs-zip.yml` triggers on `release: published`,
   including prereleases. It checks out the released tag, validates all source
   versions, archives only tracked integration files at ZIP root, validates the
   ZIP manifest and attaches `ttlock_ble.zip`. Its existing upload step uses
   `--clobber`; do not rerun it against an existing RC12 publication or asset.
5. Verify the completed workflow, release target, prerelease flag, asset name,
   ZIP root/manifest, immutable SDK URL and downloaded asset hash. Confirm HACS
   can discover RC13 with prereleases enabled before any hardware testing.

No manual developer-file, local ZIP or SDK replacement in HA. Install only the
eventual GitHub Release through HACS. Roll back through HACS to published RC12
if inventory or cleanup causes regressions.

## Read-only hardware instructions after publication

1. Keep the independent Aqara contact and Alarmo policy unchanged. Ensure an
   independent administrator access method remains available. Record HA,
   firmware and Bluetooth/proxy versions without exporting keys or credentials.
2. Install the approved RC13 prerelease through HACS and restart HA. Confirm the
   integration version and exact SDK dependency. Capture ordinary connection
   source attribution to distinguish MR5U from the existing USB adapter; a
   successful unlock alone does not identify the route.
3. In HA's Actions response UI, call `ttlock_ble.get_access_inventory` with the
   lock's device ID and `method: keypad_pin`. Record only count, validity counts,
   coverage limitation, enumeration and interruption status. Compare counts
   privately with the app; never paste actual PINs or credential identifiers.
   Missing unused/offline PINs are expected and do not prove revoked access.
4. Repeat separately with `method: fingerprint`, then `method: ic_card`. An
   eight-byte card response must remain partial/unverified. Names, IDs, UIDs,
   slots and cyclic schedules are not established by these summaries.
5. Check that sessions close after success, failure and cancellation, and that a
   subsequent normal state refresh works. Do not generate RF interference or
   reboot shared adapters during someone else's HA audit. Use the deterministic
   tests for interruption/preemption until a separate hardware window is agreed.
6. Check exported diagnostics, logs, entity attributes, action responses and
   Recorder for new inventory secrets. Do not enable decrypted-frame logging.
   Report safe metadata and source attribution only. Do not call add/delete/
   clear PIN, fingerprint enrollment/deletion, or card provisioning/deletion as
   part of this read-only test.

## Existing PIN-write privacy boundary

The integration does not put submitted PINs in its own logs, entity attributes
or inventory responses. This does **not** make existing add/delete PIN actions
secret from Home Assistant. Reviewed HA 2026.8 source emits original
`service_data` in `call_service` before the integration handler runs; Recorder
records non-state events unless excluded. Script/automation tracing also saves
the prepared service parameters. A regression test demonstrates the service
event exposure with a synthetic PIN.

Do not automate PIN writes if that retention is unacceptable. Recorder may be
configured to exclude the `call_service` event type (this excludes all service
events, not just TTLock). Excluding the lock entity alone is insufficient.
Trace storage, action YAML, secrets, backups and prior retained history need
separate handling by the HA administrator. This branch does not change HA's
global Recorder/trace configuration or erase history. Inventory responses are
credential-free even if a caller elects to store the response in a trace.

## Longer-term access management

Inventory is one stage toward firmware-validated create/modify/delete support.
Next work must prove each local opcode, admin check, response, identity mapping
and cancellation outcome on the actual firmware. PIN modification remains
unsupported until an atomic opcode is demonstrated or an explicit non-atomic
workflow can report each partial outcome while preserving admin access. New
fingerprint/card writes and Alarmo automatic disarming require separate
authorisation and hardware evidence.
