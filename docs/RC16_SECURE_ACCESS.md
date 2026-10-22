# RC16 administrator access foundation (unpublished)

RC16 starts at the immutable RC15 integration `00b8d42c46a64486b0c1aaa9d0cf02f3139ae181`
and SDK `fda4233d3fb15ed256f5aa66ebd0f971587e4e9e`. No release, main or earlier
PR is replaced. This is a development candidate, not authorization to operate
credentials or install developer files in HA.

## S31 evidence supplied by the administrator

RC15 PIN cursor traversal is now hardware corroborated: one permanent PIN,
matching the official app, `enumeration_complete=true`, known four-byte
CMD07/SUCCESS end-marker predicate true at cursor336, no interrupted transport,
cleanup confirmed, 4,340ms. Fingerprint2 and four-byte card2 also match the app.
This proves the three supported inventory traversals on this S31. It does not
prove complete unused offline/batch PIN discovery, schedules, household
identity, writes, other firmware, or which adapter performed GATT.

| Access method | Inventory on this S31 | RC16 individual records | Create/modify/delete |
|---|---|---|---|
| Keypad PIN (recognised types1/3/4) | Cursor end corroborated, coverage partial | Private scoped references and explicit labels | New controls disabled; legacy add/delete/clear retained with privacy warning and explicit per-lock opt-out |
| Fingerprint | Complete count2, app2 | ID+slot composite transiently decoded; private reference only | Offline candidates only; enrolment/cancel/ack not hardware proven |
| IC card, four-byte UID | Complete count2, app2 | UID transiently decoded; private reference only | Offline candidates only; no provisioning/deletion |
| Eight-byte card / generic fob | Unverified, no generic fob classification | Unsupported | Disabled |
| Schedules / passage mode | Validity classification; full schedule unverified | `schedule_verified=false`; no asserted schedules | Disabled |
| Administrative credentials / identity | Existing key required for authenticated inventory | No person association or access authorization | No admin-key changes |

## Available interface

With HA's frontend loaded, an administrator sees **TTLock access** in the sidebar.
Select a lock and PIN/fingerprint/card method. **Read fresh inventory** performs
one authenticated, bounded, low-priority read through the established connection
owner. It yields to explicit Lock/Unlock and always cleans up. Only complete,
uninterrupted, distinct private records with confirmed cleanup become fresh.
Public `get_access_inventory` remains credential-free and unchanged.

The panel can assign a local display name to each observed record, review
presence/staleness and forget local labels/references. Names are not identity:
do not put PINs, card IDs or biometric IDs into a label. No credential entities
are created. The ordinary registry, state machine, Recorder, service bus,
operation-log identity and diagnostics do not receive individual references,
labels or identifiers. No PIN/UID/biometric input field or write route exists.

## Security boundary and audited source

Audited against installed HA2026.8.0, using these public HA interfaces:

- `components/http/auth.py`: validates bearer tokens and records authenticated
  user/refresh-token ID. It logs path/auth type, not a JSON request body.
- `helpers/http.py`: `HomeAssistantView` requires authentication; normal handler
  logging includes path/remote/auth status. Our handler catches errors and returns
  a fixed vocabulary without raw exceptions or credential-bearing tracebacks.
- `auth/models.py`: normal, system and long-lived token types are distinct.
- `core.py`: service-call data reaches the event bus before our handler; it may
  then reach Recorder and automation traces. Hiding fields or redacting the
  integration logger cannot fix this.
- `components/websocket_api/http.py`: incoming message contents can be debug
  logged before handler validation. Do not use it to submit confidential values.
- `components/panel_custom`: local module and `require_admin=true` sidebar panel.
  Actual authorization is independently enforced by the HTTP backend.
- Frontend [`fetchWithAuth`](https://github.com/home-assistant/frontend/blob/dev/src/util/fetch-with-auth.ts)
  supplies the current bearer token; the panel never handles a refresh token or
  writes a token into its own storage.

`POST /api/ttlock_ble/administration` independently checks an active, non-system
administrator, a normal authenticated refresh-token context and explicit bearer
header on **every** request. Long-lived tokens (including usual REST automation
credentials), Supervisor/system, ordinary users, unauthenticated requests,
signed URLs, any query parameters, cross-origin/missing-origin requests and
wrong Fetch-Site/custom-header contexts are denied. POST JSON only, strict
operation schemas, no additional fields, 4KiB body cap/3s body deadline, 40s
handler deadline; BLE acquisition/read and owned cleanup retain tighter existing
bounds. `cors_allowed=false`, same origin and a custom JSON request header prevent
an unrelated web origin from issuing an accepted browser request. Responses use
`Cache-Control:no-store`; no raw data is placed in paths or query strings.

**Threat model limits:** an ordinary normal-session bearer token can be used by
whoever holds it. This boundary cannot prove a human click or stop an automation
that steals an administrator's normal frontend token and forges headers. It
rejects unauthorized/ordinary automation token contexts, not a compromised admin
session. The HA host, trusted browser/extensions, proxy and backups remain
trusted. Reverse-proxy/APM request/response-body capture, debugger-local capture,
malicious administrator labels and an already compromised host are outside the
integration's protection. Use HTTPS over untrusted networks; this introduces no
cloud service or mandatory external communication. Do not turn this foundation
into a secret-submission/write API without a fresh audit and an explicit step-up
and hardware authorization design. No confidential write UI is claimed here.

## Persistence and lifecycle

A private, atomic HA Store per entry holds scoped HMACs, random per-generation
opaque references, type, validity class, presence, local aliases and the explicit
legacy opt-out. A separate private store holds the random HMAC key. Raw PINs,
card UIDs, fingerprint IDs/slots and SDK record containers are never persisted.
HMACs bind the config entry key, lock, method, epoch and record type. This is
pseudonymization from public surfaces, not encryption against the HA host; a host
administrator possessing the key may brute-force low-entropy PIN candidates.
Private backups should receive normal HA credential protection.

Freshness expires after five minutes and is never persisted. Restart retains
labels but marks records stale and aliases unconfirmed; a fresh authenticated
read is required before naming, and an admin explicitly re-saves labels to
confirm them. Missing records retire their generation. Reappearance creates a
new reference without inheriting an old alias. Partial/failed/cancelled reads or
uncertain cleanup mark the scope stale without inferring deletion. Operation
revision tickets prevent a late read/save from overriding a newer attempt or
local reset. This same
invalidation/read reconciliation is the foundation for future ambiguous writes;
RC16 dispatches none. Explicit local reset rotates the namespace and drops
labels; config-entry deletion removes stores. A physical reset, changes while
HA is offline or ID reuse between observations cannot be detected reliably:
forget local references and relabel after such events. A matching opaque handle
never establishes identity or permission to write.

Corrupt/inaccessible metadata blocks administration and legacy PIN writes, while
ordinary lock state/control still load. Store contents are schema checked and
malformed values are not echoed. References never authorize a lock write.

## Legacy PIN compatibility and deprecation

Existing add/delete/clear service names and default behaviour are retained;
installations and automations are not silently broken. Service descriptions and
the panel warn that these operations are **not confidential**. An administrator
can explicitly disable all three per lock in the panel; that choice persists.
The handler then refuses BLE writes. It cannot retract a PIN already published
by HA's service event or trace infrastructure. Remove confidential PIN-valued
automations yourself; do not submit another PIN to test the disable switch.
The panel has no replacement secret input. A confidential replacement needs a
separately reviewed flow, not migration of secrets into WebSocket messages.

## Offline operation contracts and provenance

The SDK's `commands.management_candidates` is a pure, transport-free module:

| Candidate | Input / protocol fact | Response contract | Authentication / recovery still required |
|---|---|---|---|
| PIN modify03/op5 | Existing+new ASCII4–9 digits, explicit type1/3, ordered calendar window | Envelope acknowledgement only, never physical confirmation | Demonstrate write-session authentication and read verification; no delete/add emulation |
| Four-byte card05/op2 | Enter enrolment; or UID4 with validity fixture | Mode entry or ambiguous final response | Prove secure enrolment interaction, final UID handling, explicit cancel/timeout cleanup |
| Card05/op5/op3 | UID4 + dates for validity edit / UID4 delete | Envelope acknowledgement only | Disposable card, exact identifier binding and post-read reconciliation; no eight-byte layout |
| Fingerprint06/op2 | Enter enrolment; composite ID4+slot2 fixtures | Mode/progress/template phases distinguished; no success claim | Prove challenge, multi-step enrolment, template lifetime and abort semantics |
| Fingerprint06/op5/op3 | Composite6 + dates / composite6 delete | Envelope acknowledgement only | Disposable finger/template, exact target binding, cancellation and post-read proof |

All candidates report `write_enabled=false`, `hardware_validated=false`, no
retry allowed and reconciliation required. No client/HTTP/service dispatcher
uses them. Admin inventory authentication does not prove write authentication.
Reject/unknown/timeout/disconnect never triggers resend or automatic rollback.
No automatic restore using an old credential, no destructive clear fallback.

Wire facts were inspected in
[kind3r/ttlock-sdk-js 8c84cda](https://github.com/kind3r/ttlock-sdk-js/tree/8c84cda63828d7f36f464ff02ad680de76dd1d57/src/api/Commands)
(GPL-3.0, protocol reference only, no copied code). Inventory decoders retain
MIT upstream provenance. The official
[TTLock Android SDK](https://github.com/ttlock/Android_SDK_Demo) documents API
availability, not these raw wire/authentication contracts. Schedule-edit claims
in reference implementations lack sufficient hardware evidence for support.

## First future supervised write: separate authorization required

1. Publish/install a separately approved prerelease through GitHub/HACS only.
   Confirm normal physical key/admin app access and an independent authorized
   person available. Keep the door open, Alarmo auto-disarm disabled, use the
   Aqara contact for intrusion sensing. Do not use an established household PIN.
2. Capture credential-free baseline counts and private fresh references. First
   establish exact firmware-specific write authentication and acknowledgment
   through non-destructive fixtures/protocol evidence, plus cancellation tests.
3. Prefer **adding one disposable four-byte test card** under supervision, with
   no old credential replaced. Enter enrolment once; present only that disposable
   card; require validated completion and a fresh authenticated inventory proving
   exactly one additional record. Do not infer completion from envelope/mode
   entry or elapsed time. If enrolment-cancel semantics are not proven, stop
   before opening enrolment mode. No RC16 function currently sends this command.
4. If response/transport/cleanup is ambiguous, do not resend, delete, modify or
   clear. Retain cleanup ownership and reconcile with a read after cleanup;
   verify physical access using only the disposable credential and app evidence.
5. Only after explicit review and **separate authorization**, demonstrate
   cancellation/timeout recovery and disposal of that exact test record. PIN
   modification and established-user lifecycle tests come later. Re-enrolment
   must create a new local reference; no automatic household identity link.

A future verified-entry policy also needs trustworthy fresh event attribution,
identity verification and independent safeguards. Unlock alone never disarms
Alarmo. RC16 adds none of these automation permissions.
