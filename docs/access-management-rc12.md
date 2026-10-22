# Local access management after RC12

This branch adds **on-demand, read-only summaries** of access inventory. Use
`ttlock_ble.get_access_inventory` with a TTLock device and `keypad_pin`,
`fingerprint`, or `ic_card`. The response contains counts, validity classes,
completeness and limitations. It does not expose PINs, card UIDs, fingerprint
IDs or credential slots, and it creates no credential entities or Recorder
attributes. Inventory needs the stored administrator password; a key without
one returns `admin_key_required` before BLE acquisition. The actual front-door
firmware has not yet been tested against these queries.

| Method | Read | Create | Modify | Delete | Current qualification |
| --- | --- | --- | --- | --- | --- |
| Keypad PIN | Partially discoverable | Existing add action | Unsupported | Existing delete/clear actions | The BLE list omits unused batch/offline PINs. A completed cursor does not establish complete access coverage. Existing writes are unchanged. |
| Fingerprint | Protocol-supported summary; lock unverified | Unsupported | Unsupported | Unsupported | Indexed `0x06/0x06` query returns counts and validity only. Cyclic constraints and identity are not represented. |
| IC card | Four-byte protocol summary; lock unverified | Unsupported | Unsupported | Unsupported | Indexed `0x05/0x01` is adapted from upstream SDK PR #96's hardware-tested four-byte layout. An eight-byte layout remains unverified and is reported as incomplete. |
| Fob | Unsupported inventory | Unsupported | Unsupported | Unsupported | Some operation records may classify a fob, but there is no verified local inventory/provisioning contract. |
| Schedules | Partially discoverable | Existing period PIN action | Unsupported | Existing period PIN delete | Period/cyclic validity is a summary, not an authoritative schedule. No schedule editor or cyclic provisioning is implemented. |
| Passage mode | Unverified | Unsupported | Unsupported | Unsupported | No verified command, state or acknowledgement path. |
| Administrator access | Stored admin credential capability only | Unsupported | Unsupported | Unsupported | Never enumerate or rotate administrator credentials through this integration. |

All new list commands require a fresh admin challenge on the existing
connection. They use the RC12 HA-owned Bluetooth acquisition and cleanup path,
with a bounded on-demand operation. A pagination error, transport interruption
or unknown layout produces a partial result when safe metadata was already
observed. No inventory query runs in background polling. A failed list must not
be interpreted as an empty lock.

Credential labels and method classification are deliberately coarse. The
response uses `permanent`, `time_window` or `cyclic` validity rather than
claiming a human-assigned name that BLE does not prove. Operation-log method
classification is evidence of an event, not proof that an inventory entry
belongs to a particular person.

Do not implement PIN modification by silently deleting the old PIN and adding
a new one. Unless a verified atomic local modification opcode is found, a
future explicit two-step operation must require the old and new PIN, preserve
at least one independent administrator entry, journal each acknowledged step,
and return a typed **partial outcome** if either step is ambiguous or fails.
Never report success after only one step. Enrollment/deletion of fingerprints
or cards needs separately authorised hardware testing of opcode, authentication,
responses and cancellation before it can be advertised as supported.

## Alarmo security boundary

The independent Aqara contact is the door intrusion sensor. TTLock provides
physical bolt state plus operation-log evidence. The `Last verified state`
diagnostic timestamp advances only after an authoritative connected query or
acknowledged control result, and is retained through failed polls or
advertisements. Alarmo policy should compare that timestamp with the current
time and treat old/unknown state as stale; it must not infer a verified entry
from `unlocked` alone. Availability and stale state are separate conditions.

Automatic disarming remains **disabled**. A future credential-verified disarm
would require a fresh operation event after the armed state began, validated
method and locally enrolled identity attribution, a bounded event-age window,
independent door/contact correlation, replay/duplicate suppression and a
fail-closed response to missing logs or ambiguous BLE control. None of those
identities are established by the current credential-free inventory. Two
observed physical unlocks also do not identify whether the MR5U or USB adapter
handled BLE; scanner/proxy attribution logs are needed before drawing that
conclusion.

## Provenance and validation boundary

The protocol concepts were inspected in the MIT-licensed original
`roquerodrigo/ttlock-ble` 0.1.11 inventory implementation and SDK PR #96
(`d11c0782`); upstream integration PR #116 (`8422881`) was reviewed but its
per-fingerprint entities and hex-derived names were not copied. These
protocol-level concepts are reimplemented as credential-free summaries against
the fork's pinned RC12 SDK lineage. Current upstream main has since moved to
a newer transport lineage. The published RC12 release, SDK pin and all RC12
tags remain untouched by this development branch.
