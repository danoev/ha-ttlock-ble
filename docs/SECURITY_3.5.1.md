# Stable security validation scope (22 October 2026)

The RC16 SDK pin and its safe diagnostic contract are unchanged. Synthetic tests
exercise administrator sessions, non-admin/invalid/expired/long-lived contexts,
trusted proxies, cancellation, safe failure classification, credential redaction,
private persistence and stale/lifecycle ordering. No credentials, raw captures or
physical operations are needed for this gate. CodeQL supplements these tests;
neither static analysis nor test coverage proves firmware or host security.

## Dependency assessment

The initial full HA2026.8 development environment audit reported 87 advisory
entries across 11 packages (including duplicate advisory aliases). The stable
development baseline moves to HA2026.10.2 and the matching official test harness,
with compatible security updates to anyio, certifi, multidict, urllib3 and
virtualenv. No SDK transport migration or forced HA runtime dependency override
is made. All SDK runtime dependencies in this current validation environment
pass pip-audit with **no known vulnerabilities**; the immutable fork itself is
reviewed as source rather than confused with a same-version PyPI distribution.
The browser-test-only npm lock also audits clean.

**The complete HA/test environment audit is not clean:** 36 advisory entries
remain in two upstream-controlled packages. They are recorded rather than hidden
or described as a green global scan:

- HA2026.10.2 pins Pillow12.3.0, for which the database reports 35 advisory
  entries, generally fixed in13.0.0. These concern image/font/PDF processing.
  This integration/SDK does not import Pillow, decode uploaded media, accept
  images, process PDFs or route credential data through those functions. Its
  image files are static repository-owned branding. These findings remain a
  **HA host responsibility**, not a claim that those packages are safe for other
  HA integrations. Track HA/Pillow security releases and avoid unsafe untrusted
  image-processing workflows. Do not override HA's exact pins from a custom
  integration just to silence an audit.
- The official HA test harness pins uv0.12.23; PYSEC-2026-4321 concerns a
  compromised CA in uv's bundled trust roots, fixed in0.13.4. uv is development
  tooling, not an installed integration/SDK runtime dependency or HTTP credential
  path. Use a patched external uv CLI or system certificate store for development
  downloads. Never send live secrets to a test environment.

The scoped release gate is that no known affected SDK runtime dependency or new
application path remains unaddressed, with the HA host/tooling findings above
explicitly retained. It is not a declaration that all packages in HA are free of
advisories. Compatibility with HA2026.5.1 is tested separately; that older version
is not the recommended security baseline. HA administrators remain responsible
for current core/security updates, trusted proxies and private backup protection.

## Preserved boundaries

No new credential-write interface exists. Legacy PIN services keep their original
permission/event/trace limitations unless the administrator opts out per lock.
Opaque references, local names, non-observation and bolt state do not establish
person identity or permission to write/disarm. Automatic Alarmo disarming stays
disabled. Fingerprint/card secrets, PINs, raw packets and private references never
become public entities or Recorder attributes; private labels remain visible to
the trusted HA host and its protected backups.

The upstream PIN disclosure fix is independently submitted as
[SDK PR #101](https://github.com/roquerodrigo/ttlock-ble/pull/101), based on the
original modern SDK. First-contributor CI/CodeQL approval there belongs to its
maintainer; local tests and the fork's separately hosted gates are not presented
as upstream approval.
