# RC14: S31 evidence, diagnostics and protected administration groundwork

This development candidate starts at published RC13 integration
`430dad2e4d3d26c0a85c249313cd38905949b414` and SDK
`60604872e004d9dc7fd74aaf3ad260da9143b4c1`. RC12/RC13, their releases and
existing draft PRs remain unchanged. RC14 is not published by this work.

## Hardware evidence supplied on 22 October 2026

These are user-reported S31 / TTLock 5.3 findings, independently compared with
the phone app, not new hardware tests performed by this change:

| Method | BLE / app count | Proven read evidence | Still unproved |
| --- | --- | --- | --- |
| PIN | 1 / 1 | One permanent entry; partial, no transport interruption | Final response shape and enumeration completion; unused offline/batch coverage |
| Fingerprint | 2 / 2 | Complete indexed read; counts corroborated | Person identity, full schedules and every write |
| IC card | 2 / 2 | Complete four-byte indexed read; counts corroborated | Identity, eight-byte layout, full schedules and every write |
| Fob | Not established separately | A fob may use the card protocol | Separate inventory/provisioning capability |
| Passage / administrator management | No evidence | Nothing newly enabled | Queries, writes, acknowledgement and cancellation semantics |

The selected advertisement scanner was the SLZB-MR5U, RSSI improved from about
−101 to −95 dBm, a GATT session took about 3.6 seconds, a connected query returned
LOCKED, and CHECK_ADMIN/CHECK_RANDOM succeeded. The advertisement source does
**not** establish the actual GATT backend route; the existing USB adapter makes
that distinction material. RC14 reports `gatt_backend_source: unverified`.

## PIN parsing decision and safe diagnostics

RC13's S31 result was `partial`, count 1, `enumeration_complete: false`,
`transport_interrupted: false`, `PIN response unrecognized`. No response bytes
were supplied. Count equality with the app cannot prove an end marker.

The current upstream parser recognises a bare `00 00` and cursor-zero
termination, but also accepts short success headers defensively without a
confirmed capture. RC14 preserves the existing recognised envelopes and strict
truncation, echo and status checks. It does not adopt that permissive rule.
Synthetic tests cover known termination, failed status, every truncated cursor
length, unknown types, a second-page rejection and cancellation. They are not
claimed S31 captures.

On rejection/malformed data, `response_metadata` contains only method,
zero-based page, query cursor/index, response length, command echo, response
status and closed parser category. It never contains a payload, credential,
UID, biometric ID or credential slot. The query index is pagination metadata,
not a returned credential slot. The same safe shape can appear in SDK debug
logs; no raw exception chain is exposed by the inventory boundary.

The next PIN read must supply this metadata, not decrypted frames. A shape
alone may still be insufficient to prove termination: obtain a documented
protocol rule or separately authorised, securely handled fixture before
relaxing the parser. Complete PIN pagination still does not establish complete
access coverage because unused offline/batch PINs may be omitted.

## Stable failure contract

| `reason` | `stage` | Meaning |
| --- | --- | --- |
| `connection_busy` | `queue` | Inventory did not start; retry after the active operation |
| `inventory_preempted`, `inventory_cancelled` | Last active stage | Cancellation propagates; last-attempt diagnostics record it |
| `no_advertisement_candidate` | `discovery` | No usable candidate was resolved |
| `bluetooth_discovery_timeout` | `discovery` | The bounded HA advertisement wait expired |
| `gatt_connection_failed` | `gatt` | A candidate existed but setup failed |
| `admin_key_required`, `admin_challenge_rejected`, `admin_challenge_timeout` | `admin` | Required admin material missing or challenge failed |
| `operation_rejected` | `inventory` | Inventory command returned a failed status; support is not inferred |
| `unrecognised_response_layout`, `truncated_response` | `inventory` | No completion inferred from malformed/unknown data |
| `pagination_error`, `pagination_limit` | `inventory` | Cursor/position failed or safety cap reached |
| `incomplete_inventory` | `inventory` | Known coverage limitation remains |
| `transport_interrupted` | `inventory` or `admin` | Exchange failed; partial evidence is not a retained healthy session |
| `read_deadline_exceeded` | `inventory` | Shared read deadline expired |
| `cleanup_uncertain` | `cleanup` | Teardown not confirmed; ownership retained, replacement blocked |
| `read_failed` | `inventory` | Unclassified failure, with no device-controlled text |

Successful complete indexed reads use `complete`. Partial results preserve
credential-free counts with their reason and completion flags. Failed reads
before any result do not manufacture count zero. Connection diagnostics include
elapsed milliseconds, cleanup confirmation, advertisement source and the
explicitly unverified GATT route. A busy response does not reuse a previous
attempt's cleanup result. Last-attempt diagnostics are available in the HA
diagnostics download and the admin metadata command; they contain no inventory
entries and are in memory only.

The existing 16-second acquisition / 30-second shared operation budget is
unchanged. Explicit Lock/Unlock cancels inventory, waits for owned cleanup and
still sends at most one physical control command. Cleanup may outlast the read
deadline. Uncertain cleanup retains the backend and blocks a replacement.
Advertisement hints never advance authoritative bolt state. No raw/private
BlueZ, global Active configuration or default reconnect loop is introduced.

## Administration and identity threat model

The only new administration endpoint is authenticated and administrator-only
WebSocket metadata: `ttlock_ble/access_capabilities`, with `device_id`. It
performs no BLE operation and accepts no credential/name/identifier fields.
It reports implementation capability separately from the S31 evidence above;
that evidence is not automatic support detection for every configured lock.

Home Assistant 2026.8 emits the original `call_service` data before integration
handlers, scripts retain prepared parameters in traces, and Recorder can retain
service events. Existing add/delete PIN actions therefore remain an exposed
legacy interface. Handler redaction, password selectors and entity exclusions
cannot make them confidential. **Do not place PINs in automations when that
retention is unacceptable.** Disabling one retention path does not close the
others. Existing PIN behaviour is preserved, not newly certified private.

HA's WebSocket HTTP layer also debug-logs incoming message data before command
validation. An administrator-only WebSocket carrying a secret would therefore
not solve this problem. Never send secrets to the metadata command, even in
invalid extra fields. No new secret-entry UI/transport is implemented.

`ProtectedCredentialReferences` is dormant internal groundwork, not a public
mapping service. On explicit future use it creates a random 32-byte HMAC key in
an atomic HA `Store(private=True)` scoped to the config entry. HA's private
writer keeps owner-only file permissions. A handle binds length-prefixed lock,
method, lifecycle epoch and in-memory identifier with HMAC-SHA256; only the key
is stored. No raw IDs, PINs, names or identity associations are persisted. Tests
cover concurrent/idempotent loading, failed-save fail-closed behaviour,
restart stability, lock/method/epoch separation, corruption and removal.

The key is **not encryption against the HA host, administrators, other trusted
integration code or backups**. Key access permits guessing low-entropy inputs.
Handles must stay out of ordinary entities/events/traces too. Future callers
must prove the record belongs to the lock and rotate its epoch after resets,
re-enrolment or ambiguous reconciliation. The current runtime neither creates
this store nor establishes such mappings; deletion is a tested explicit helper,
and config-entry integration must be added with the future mapping lifecycle.
A handle is not authorization, person identity or proof of an access event.

A future secret interface needs an audited authenticated/admin-restricted
transport outside the service bus, trace/event exclusion, request-body logging
review, origin/CSRF checks, proxy/browser/cache controls, short in-memory
lifetime and explicit final outcomes. Do not add enrollment before that review.

## Protocol research and implementation boundary

The following is protocol research, not new enabled command builders. All new
writes remain disabled. Required admin challenge/random authentication is known
for current inventory; write-specific session/authentication requirements must
be demonstrated, not inferred from the operation number alone.

| Operation | Evidence for opcode / payload | Ack, atomicity and cancellation | S31 / next gate |
| --- | --- | --- | --- |
| PIN read | `07`, cursor; existing strict parser | Successful envelope plus genuine terminal cursor/marker; malformed responses remain partial | One record proved, terminal shape unknown |
| PIN modify | JS reference `03/05`: type, old/new ASCII lengths and values, two five-byte dates; older op `04` deprecated | Single-op candidate, not proof of atomic replacement. Matching response/failure/ambiguous-write semantics not validated | Disabled; prove opcode and preserve old access on every failure |
| Fingerprint read | `06/06`, index; 20-byte data record, `FF FF` end position | Existing tested parser; separate cyclic command `70` not read | Two counts corroborated; identity/schedule incomplete |
| Fingerprint enroll/change/delete | JS `06/02` / `06/05` enter mode or six-byte composite ID+slot and two dates; `06/03` delete | Enter/progress/template/success are distinct statuses. Completion/abort after disconnect not established; validity edits reported broken in reference | Disabled; multi-response state machine and separately authorised hardware tests required |
| Card read | SDK PR #96 `05/01`, index, four-byte UID layout | Existing parser/tests; eight-byte layout explicitly rejected as unverified | Two counts corroborated |
| Card enroll/change/delete | JS `05/02` / `05/05` enter mode or UID4/8 plus dates; `05/03` delete | Builder presence is not a working transaction. Eight-byte and edit paths lack sufficient evidence; interrupted enrollment may already persist | Disabled; prove four-byte ack and recoverability first |
| Validity/schedules | Date windows appear in reads/builders; cyclic `70` separate | A date window does not prove full cyclic restrictions; reference validity edits have known limitations | No new editor; time zone/RTC and round-trip evidence required |
| Passage mode | JS `66`: query `01`+cursor; add/delete `02/03`+mode+day+month+start/end hour/minute; clear `04` | Ack validation, exhaustive pagination and cancel/partial persistence insufficiently established | Disabled, no S31 evidence |

Sources and provenance:

- [MIT upstream PIN parser at 8025f57](https://github.com/roquerodrigo/ttlock-ble/blob/8025f57cebc7de63a66035fd19e1efb042d0208f/src/ttlock_ble/commands/passcode_list.py),
  [fingerprint protocol](https://github.com/roquerodrigo/ttlock-ble/blob/8025f57cebc7de63a66035fd19e1efb042d0208f/src/ttlock_ble/commands/fingerprint.py).
- [MIT card PR #96](https://github.com/roquerodrigo/ttlock-ble/pull/96), inspected
  at `d11c0782ae48071ba0dd7766e2256070f1682387`; four-byte hardware evidence
  underlies the existing RC13 parser, not new RC14 write support.
- [kind3r JS reference at 8c84cda](https://github.com/kind3r/ttlock-sdk-js/tree/8c84cda63828d7f36f464ff02ad680de76dd1d57/src)
  is **GPL-3.0**. Protocol facts were inspected; no GPL source was copied into
  either MIT project. Its README documents fingerprint/card validity limitations.
- [Official TTLock React Native SDK](https://github.com/ttlock/react-native-ttlock)
  exposes management APIs, but API existence does not prove a BLE payload,
  S31 compatibility, atomicity or safe cancellation. Original project authorship
  and MIT notices remain preserved.
- HA 2026.8 sources: `core.py` service event, `helpers/script.py` prepared trace
  parameters, Recorder event listener, `websocket_api/http.py` incoming debug log,
  `websocket_api/decorators.py` admin gate, `helpers/storage.py` and
  `util/file.py` private/atomic writer. Reviewed in the pinned test environment.

## Next safe hardware session and write-development order

After separate publication approval, install only the resulting GitHub/HACS
prerelease. Do not copy developer files or substitute a local SDK into HA.

1. With no concurrent control, read `keypad_pin` once. Save the credential-free
   response: count, validity counts, reason/stage, completion/interruption flags,
   response metadata, elapsed/cleanup/source diagnostics. A matching count of 1
   still means partial. Do not collect raw frames or enable unrelated BLE dumps.
2. Read fingerprint and card once each; expect the existing two counts only if
   the lock contents have not changed. Confirm `enumeration_complete` and no
   transport interruption. This is a regression check, not identity validation.
3. Confirm teardown and absence of a retained unhealthy session. If cleanup is
   uncertain, stop repeated reads and obtain safe cleanup evidence first.
   Record HA scanner selection separately from verified backend-route evidence;
   RC14 deliberately continues to say GATT route unverified.
4. A cancelled/preempted call does not return success; inspect last-attempt
   diagnostics. Any separate physical control test needs the user's normal
   hardware supervision and must verify one physical attempt only.

Next development: secure secret transport and mapping lifecycle; read-only
record identity/epoch validation; offline tests for genuine PIN op-5 response
and atomicity; separately authorised non-destructive PIN hardware trial with
independent admin access; four-byte card enrollment/revocation; fingerprint
progress/cancel state machine; verified schedules/passage. Never replace a PIN
by silently deleting it first. Multi-step operations need explicit partial and
ambiguous outcomes, preserved administrative access and a recovery journal.

The Aqara contact remains Alarmo's intrusion sensor. Verified bolt state and
freshness remain separate; unlocked is not authorised entry. Automatic disarm
stays disabled until identity, freshness, anti-replay, independent correlation
and fail-closed policy have separate evidence and explicit authorization.

RC14 can be a read-only diagnostic prerelease after software validation; it is
not a stable complete credential-management release. Unresolved PIN termination,
secret-entry privacy, mapping lifecycle, write acknowledgements/cancellation and
real proxy/backend reliability remain concrete stable-release blockers.
