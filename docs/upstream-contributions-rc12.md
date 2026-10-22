# Focused upstream contribution proposal

Keep upstream submissions separate from the fork's RC12 transport and HA
integration work. Do not target upstream `main` with this branch's combined
diff. Current upstream SDK main uses a newer transport lineage, so each
candidate must be rebased conceptually and tested there independently.

1. **Credential-safe SDK diagnostics (first candidate).** Replace the raw
   passcode in `commands/passcode.py` validation errors and suppress decrypted
   plaintext hex in inventory parser/debug failures. Add tests asserting that
   deliberately invalid credential values never appear in exception text or
   logs. This is generic, small and has no protocol or public API change.
2. **Inventory response models/tests (separate candidate).** Offer a typed
   completeness/limitation model and tests for cursor termination, duplicate
   cursor, partial transport failure and the hardware-tested four-byte IC-card
   response from SDK PR #96. Preserve the PR author's contribution and cite
   commit `d11c0782`. Treat eight-byte data as unverified. Agree with upstream
   whether it wants raw credential APIs or an additional redacted summary API.
3. **Integration state ordering (only if still needed).** Compare current
   upstream coordinator's query/advertisement races with RC12's per-lock
   revision contract. Submit an isolated race test and minimal fix only if the
   current upstream architecture reproduces the issue; this must not import
   the fork's HA scanner/acquisition rewrite.

Fingerprint integration PR #116 (`8422881`) currently uses hex-derived
fingerprint identifiers in entity identity and names. Before contributing
there, discuss a privacy-safe identity model and Recorder lifecycle with its
author. Do not submit fork-specific metadata or claim tested enrollment.

As of 22 October, upstream SDK's only open PR is #96. Upstream integration's
other open PR #129 is release maintenance, not new BLE access management.
`jbergler/hass-ttlock` uses a cloud API for its passcode/card/fingerprint
services; its PIN list response contains plaintext codes. It is useful as a
product-function reference, not as a local BLE opcode or privacy precedent.

No upstream PR should be opened until its focused patch is adapted to current
upstream main, reviewed and validated in that repository. Original MIT
authorship and cited source commits remain in the proposal.
