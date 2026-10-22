> Subsequent RC15 hardware result: the S31 now confirms one permanent PIN,
> cursor completion, the exact short-end-marker predicate, no interruption and
> confirmed cleanup in4,340ms. Fingerprint2/card2 also match the app. See the
> [RC16 current matrix](RC16_SECURE_ACCESS.md). The investigation below records
> the evidence available before that controlled test, not a current failure.

# RC15: exact PIN-list termination, with S31 confirmation pending

RC15 starts at published RC14 integration
`cfb71bdbf4c4163c674b16795960b81aea8d6c5e` and SDK
`5bb8f4fa97f0b53f33b7713b5d1658ee1554fe4c`. This is a narrow read-only
pagination correction. RC14 remains immutable; no main branch, existing draft,
live Home Assistant instance or credential has been modified.

## New S31 observation

The user supplied an RC14 read with one permanent PIN and then:

- `reason: truncated_response`, `stage: inventory`;
- page 1 (second request), requested cursor 336;
- full response length 4, command echo 7, status 1 (SUCCESS);
- `enumeration_complete: false`, `transport_interrupted: false`;
- `cleanup_confirmed: true`, elapsed 15,836 ms.

The phone app also reports one PIN. Fingerprint/card reads on this S31 were
already complete and count-corroborated at two each. Admin authentication and
connected state/GATT queries have succeeded. These are user-reported hardware
findings, not new hardware tests performed by RC15 development.

**The remaining two response bytes were not supplied. No fixture claims their
value.** App-count equality does not prove termination, and a successful status
alone cannot distinguish a valid empty marker from a truncated record.

## Confirmed parser defect and evidence

Current upstream SDK was fetched without changing local branches. Its main
remains `8025f57cebc7de63a66035fd19e1efb042d0208f`.

The important distinction is **data length versus full response length**:

1. Upstream `tests/test_commands.py::_real_plain` prepends CMD `07` and SUCCESS
   `01` to captured data. `test_end_of_list_is_a_bare_two_byte_response` calls
   that helper with the known two-byte zero data marker. Its full plaintext is
   therefore **four** bytes, not two.
2. Upstream `tests/test_client_ble.py::test_get_passcodes_full_real_capture`
   replays ten captured records and that same terminal data through encrypted
   fake-BLE frames. The capture ends after a request cursor of 336. It is a
   different hardware capture, not proof of the S31's undisclosed bytes.
3. RC14 special-cased a literal two-byte marker before envelope decoding, but
   rejected the captured wrapped form because its two data bytes are shorter
   than the regular four-byte cursor header. This is a reproducible parser
   defect. Whether it explains the S31 result is still conditional on a match.

The upstream implementation also accepts any successful data shorter than four
bytes as completion and ignores the echoed command there. Its tests exercise a
synthetic shortened success; its documentation calls other short/header-only
forms defensive, not captured. RC15 does **not** import that broad fallback.

Sources, with immutable provenance:

- [MIT upstream parser](https://github.com/roquerodrigo/ttlock-ble/blob/8025f57cebc7de63a66035fd19e1efb042d0208f/src/ttlock_ble/commands/passcode_list.py)
- [MIT helper and terminal fixture](https://github.com/roquerodrigo/ttlock-ble/blob/8025f57cebc7de63a66035fd19e1efb042d0208f/tests/test_commands.py)
- [MIT encrypted capture replay](https://github.com/roquerodrigo/ttlock-ble/blob/8025f57cebc7de63a66035fd19e1efb042d0208f/tests/test_client_ble.py)
- [JavaScript protocol reference](https://github.com/kind3r/ttlock-sdk-js/blob/8c84cda63828d7f36f464ff02ad680de76dd1d57/src/api/Commands/GetKeyboardPasswordsCommand.ts):
  its command base removes the envelope; the PIN reader interprets a zero
  two-byte data length as an empty result. This corroborates the protocol
  interpretation but is **GPL-3.0**, not an MIT implementation source. No GPL
  code was copied. Original MIT authorship/notices remain preserved.

## Narrow implementation and safe predicate

SDK `fda4233d3fb15ed256f5aa66ebd0f971587e4e9e` accepts exactly the known wrapped
zero-data terminal after validating CMD 07 and SUCCESS. All other four-byte
successful short responses remain rejected. Three/five-byte malformed replies,
failed status and wrong opcode remain rejected. Existing two-byte terminal
forms, normal records, final-record cursor zero and full-header paths remain
unchanged. A known marker on the first request represents an empty enumeration;
an unknown early short reply must not fabricate zero credentials.

The existing response metadata adds one optional predicate:

| `pin_short_end_marker_matches` | Meaning |
| --- | --- |
| `true` | Exact known four-byte CMD 07 / SUCCESS terminal matched |
| `false` | Four-byte CMD 07 / SUCCESS, but its data does not match |
| `null` | Different response shape, status, opcode or method |

This discloses a protocol predicate, not raw bytes, a hash, PIN, UID, fingerprint
identifier or slot. Successful exact termination also returns safe response
metadata so a controlled S31 read can provide positive evidence. Unknown tails
remain `truncated_response`, with false rather than their values. No raw error
chain is exposed. Only parsed credential-free counts survive a later failure.

These two concepts remain deliberately separate:

- `enumeration_complete: true`: supported cursor traversal reached a validated
  terminator (or existing valid final-record cursor zero).
- `read_status: partial`, `reason: incomplete_inventory`: even completed PIN
  pagination may omit unused offline/batch PINs. It is not a complete list of
  everyone who can enter, nor proof of a person's identity.

The SDK and HA regression tests cover one/multiple entries, both existing
two-byte endings, the exact wrapped marker, all 65,535 unmatched two-byte data
values, early malformed replies, wrong opcode/status, three/five-byte payloads,
cursor repetition/cap, timeout, cancellation, encrypted admin/BLE traversal,
privacy, control preemption and retained uncertain cleanup. Test PINs/records
are synthetic. No supplied live credential material was accessed.

## Controlled hardware test after separate publication approval

RC15 is an unpublished candidate. Do not install developer files or substitute
a local SDK. After explicit publication approval, use the actual GitHub/HACS
prerelease in the next separately controlled hardware session.

1. Run `ttlock_ble.get_access_inventory` once with `method: keypad_pin`, without
   concurrent control. Save only the public service response and safe cleanup/
   source diagnostics. No raw BLE/decrypted dumps are needed or requested.
2. If the lock still has one PIN and uses the known marker, expect count 1,
   permanent 1, `enumeration_complete: true`, `read_status: partial`,
   `reason: incomplete_inventory`, no transport interruption, and metadata
   page 1 / cursor 336 / length 4 / `pin_short_end_marker_matches: true`.
   This positively establishes the marker match without exporting its bytes.
3. If the predicate is false, enumeration must remain false with the retained
   count and `truncated_response`. The exact additional proof is an independently
   established protocol rule/capture for that alternative shape, or a separately
   approved secure investigation. Repeating the app's count is not such proof.
   Null requires interpreting the already safe length/opcode/status/category.
4. Require `cleanup_confirmed: true`. Stop repeated reads if cleanup is
   uncertain; a valid terminator cannot permit replacement of an owned backend.
   Elapsed time includes mandatory cleanup. MR5U advertisement attribution
   still does not prove the actual GATT backend route.
5. If desired, perform one read-only fingerprint/card regression read each;
   expect two and complete enumeration if lock contents are unchanged. This
   establishes no identity, full schedules or write capability.

No acquisition deadlines were increased. Explicit control remains higher
priority than inventory, single-send, and blocked until owned cleanup permits
it. Authentication, private-reference storage, normal services, state ordering,
fingerprint/card parsers and Alarmo policy have no runtime change in this patch.

## Individual access-management boundary

Existing PIN add/delete/clear actions remain legacy interfaces, with HA service
event/Recorder/trace privacy exposure and no integration-level administrator
gate. No new write, management service, secret-entry interface or identity
mapping is introduced. Private references remain dormant groundwork. Real
individual management still needs secure admin secret transport and mapping
lifecycle, firmware-validated write/auth/acknowledgement/cancellation contracts,
and explicit partial outcomes while preserving independent administrator access.

Automatic Alarmo disarming remains disabled. The Aqara contact remains the
intrusion sensor; unlocked state alone is not evidence of authorised entry.
