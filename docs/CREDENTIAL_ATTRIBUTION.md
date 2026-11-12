# Individual credential attribution (3.6.0 draft)

This feature preserves the existing `event.<lock>_log` entity, unique ID, event
classification, physical lock state, domain `ttlock_ble` and HACS name
**TTLock BLE (danoev)**. It starts from the 3.5.2 reconciliation candidate; the
published 3.5.2rc1 and stable 3.5.1 remain unchanged. The original Rodrigo Roque
projects and MIT licences are preserved.

## What the software establishes

The pinned SDK keeps exact wire bytes for the three recognised **successful**
unlock layouts, in a private repr-redacted field. The legacy decimal/text
`LogEntry.password` field is not evidence. The integration never parses that
field, pads numbers, reverses bytes, truncates values or maps list position to ID.

| Method | Inventory bytes | Only eligible log form | Evidence still needed |
|---|---|---|---|
| Fingerprint | Full `plain[6:12]` six-byte ID/slot composite after the indexed envelope | Type 20, exactly six bytes, in received order | A log's six-byte unsigned big-endian display number can represent the same bytes; this does not prove that its slot is the inventory slot. Compare all six bytes, with no split or assumed index. Verify each fingerprint independently. |
| IC card | `plain[6:10]`, exactly four bytes | Type 17, exactly four bytes in received order | Legacy unsigned big-endian decimal loses leading zero bytes and original width. Preserving the wire width avoids a four/eight-byte collision. Verify each four-byte card independently; eight-byte layouts are unverified. |
| Keypad PIN | Length-delimited ASCII PIN from the validated private record | Type 4, ASCII digits (4–9), exact length prefix and empty replacement field | Preserve leading zeros. Do not trim, coerce to a number or select a PIN by type/count. A successful PIN cursor walk can still omit unused offline/batch PINs. Validate the PIN path separately. |

The firmware's slot semantics, inventory/log equivalence and log continuity on
the S31 have **not** been demonstrated by the earlier inventory-count tests.
Synthetic byte equality proves only the parser and matching rule. No global
S31 support switch is enabled. A row must be positively confirmed by an admin
using that exact physical credential in an ordinary permitted unlock. A wrong
slot, wrong byte order, different record or method, truncated payload, bad
clock, unsupported validity or more than one possible observation cannot
qualify. Only currently observed permanent inventory rows are eligible. Timed,
cyclic, unknown, double-check, admin-code, failed, app, key and unsupported
records remain unattributed. Two known records sharing a PIN are ambiguous;
a single shared PIN may have a name such as *Household PIN*, not an identity.

## Workflow and privacy

In **TTLock access**, read the appropriate inventory, select **Identify
credential** for one row, and wait for Ready. The integration first reads a
complete log baseline, using the existing replay journal. One challenge per
lock is allowed, bound to the authenticated admin frontend session and row.
Use the chosen credential once after Ready (allow the next clock second), then
**Check operation log**, **Confirm observed credential**, and **Save name**.
The 90-second challenge expires or can be cancelled. A current exact match and
complete follow-up read are required before confirmation. The panel does not
operate the lock, enrol credentials, ask for a PIN or infer who used a row.
If the rows are not independently distinguishable, try a separate supervised
challenge for a candidate row; do not assign a name to an unmatched row.

Private matches use the existing HMAC key and lock/method/record-type/epoch
scope. Only opaque references, local names and existing safe metadata are
persisted; no new secret store or schema change. Names persist, but individual
confirmation and sharing are **in memory only** and default off on reload.
A newly confirmed row needs a separate label save, including after reuse.
This first milestone deliberately limits evidence to a fresh inventory window
(up to five minutes); a new inventory read, incomplete read, detected log gap,
credential mutation, reboot, reset, cancellation where delivery is uncertain,
or reload can revoke it. A complete log read and consecutive observed counters
reduce risk from another reader skipping records; unseen external mutation or
silent same-ID reuse cannot be inferred from bytes alone. Do not use this
experimental evidence as a security authorisation decision.

Successful post-confirmation matches can enrich the existing event only after
explicit **Share credential labels with Home Assistant** consent for the lock.
`credential_label`, `credential_method`, and `attribution_status` (value
`credential_confirmed`) convey credential-use evidence, never person identity.
No new person field is published. Home Assistant events, state, automations and
Recorder may retain opted-in labels. Turning sharing off stops future labels;
it does not erase earlier events or the last displayed event. Newly entered
labels should be words, not codes. The panel refuses numeric and hex-like
labels. Raw `uid` and `key_id` attributes are removed from the log entity;
these IDs were not valid credential or person evidence. Raw SDK identifiers,
HMACs and handles do not cross the public event boundary.

No new credential-write route is added. Existing legacy PIN actions retain
their documented HA event/trace privacy limitation and administrator opt-out;
they invalidate PIN evidence before a write attempt. Alarmo automatic disarming
remains disabled. Optional identification reads skip occupied BLE slots, use
the existing acquisition budget and a 20-second read deadline, yield to explicit
control and await owned cleanup. Reconciliation and bolt authority are separate.

## Separately authorised supervised hardware validation

No live test is included in this draft. Use a separately approved test build
and keep another permitted entry route available. Do not change credentials,
lock settings, Alarmo, or automatic disarming. Use only ordinary permitted
unlocks; no BLE/HA Lock or Unlock commands from this workflow.

1. With sharing off, read a complete fingerprint inventory. Independently choose
   each existing fingerprint and try a separate challenge with its candidate row.
   Confirm only one genuinely new exact success for that credential, then repeat
   with the other fingerprint. Use negative cross-checks: the other fingerprint
   must not qualify for the first row. Record only the method, exact/mismatch
   outcome, timing and cleanup/continuity status. Do not capture raw IDs.
2. Repeat independently for **each existing IC card**, including a negative
   cross-check. A match on one card says nothing about the other or about an
   eight-byte firmware layout. Test cancellation and expiry with no unlock.
3. Validate PINs separately. Use an existing permitted PIN directly on the lock,
   never type it into HA. Verify exact matching and that no PIN or raw identifier
   appears in local logs, administration responses, public events or Recorder.
   A shared PIN does not identify an individual, and coverage is incomplete.
4. Only after exact per-record confirmation, optionally enable event sharing
   and observe another ordinary use. Verify the approved credential label and
   method only; opt out and confirm the next event has no label. Check reload
   retains local labels but drops consent and confirmation. Monitor bolt state
   independently using the preserved reconciliation diagnostics.

If a complete log baseline or follow-up cannot be proved, the clock is skewed,
record counters are discontinuous, or any layout differs, record the safe failure
category and leave attribution unverified. Do not weaken the matcher to make a
fixture or a hardware trial pass.
