# 3.5.2 candidate: external-unlock state reconciliation

This is an unpublished maintenance candidate based on stable v3.5.1
(`81ae135dbd37adc2506982c4fb8e2680132a8fc8`). It keeps SDK
`73e6c89544dd178cb09de9b88f29f5cf204b2429` and its immutable archive SHA-256
`49ae5d7ccc53c16a96de49089a31ebcbce424d9bbde447263df1b313269cd112`.

## Confirmed software cause and unproven physical cause

RC16 and 3.5.1 query after a push/activity hint and changed/new-record
advertisements. A query can legitimately see Unlocked before automatic re-lock.
There was no delayed verification after that observation. An unchanged record
flag, missed advertisement, idle BLE disconnection or failed query can therefore
leave the cached Unlocked state until another useful hint or the default hourly
poll. Known-state failures deliberately preserve the previous authoritative value;
the unknown-state bootstrap retry does not apply to an already known Unlocked lock.

Successful inventory previously disconnected without reading bolt state. Neither
an access record nor successful inventory establishes the current bolt position.
The reported incident does not include an independent bolt observation or a later
connected state response. Whether the S31 physically auto-locked, missed a
notification, or failed the next query remains a hardware question. An idle BLE
disconnection is not itself a locking failure or proof of either bolt position.

The SDK's existing `query_state` exchanges SEARCH_BICYCLE_STATUS under its command
lock; it returns Locked, Unlocked or unknown plus battery. No SDK protocol or
physical-control change is needed for this fix. Operation-log classification,
event publication and advertisement decoding retain their existing authority.

## Finite verification policy

- A successful connected Unlocked observation (including after a physical push)
  starts one per-lock cycle. A successful unlock hint can also schedule verification
  if the immediate query fails. Hints never change bolt state or Last Verified State.
- There are at most **three additional state-query operations**. With no recently
  read native delay, wait 10, then 20, then 60 seconds, measured after the preceding
  operation and its cleanup. Slow work never creates a catch-up burst.
- If `get_auto_lock` has successfully read a positive native delay in this entry
  lifetime within the past hour, the first wait is at least that delay plus two
  seconds. A MODIFY acknowledgement invalidates the cache; unknown/disabled/stale
  values use the fallback. The integration does not automatically read or write
  configuration. External setting changes can invalidate any cached expectation;
  a timer never establishes Locked. Native delays longer than the fallback window
  need a reliable readback or a later authoritative refresh.
- A confirmed Locked answer cancels remaining checks. A newer explicit command
  cancels/replaces the cycle; repeated Unlocked readings cannot extend it.
  Replayed unlock hints are deduplicated and subject to a 120-second admission
  cooldown. A genuinely new authoritative Locked-to-Unlocked transition can start
  a new cycle. No cycle is a permanent keep-warm loop.
- Each background operation skips an occupied serialisation lock or pending
  explicit control instead of queuing. Acquisition uses the existing 16-second
  routine budget, with one exact-address fresh HA-managed Active fallback. The
  whole acquisition/query has a 20-second deadline, excluding mandatory owned
  teardown. Explicit control cancels acquisition or reading and waits for that
  teardown; no physical command is resent.
- Successful and failed background reads release the session. Uncertain teardown
  retains backend ownership and prevents a replacement GATT connection. Shutdown
  detaches callbacks, cancels delayed/active work and clears freshness timers.

**Additional successful BLE sessions per observed unlock: 0–3**, plus the existing
initial observation query. A live session may serve the first check, reducing new
sessions. Every operation can try a cached candidate and at most one fresh fallback
within its acquisition budget; candidate/connector attempts are not extra queries
or unbounded retry cycles. Busy slots consume an attempt without a session. A
cycle with all three full 20-second operations takes about 150 seconds plus owned
cleanup with fallback waits; a known native delay can make the first wait longer.

If *all* external activity is invisible (no push, useful advertisement or connected
Unlocked query), there is no evidence to trigger this cycle: the normal poll or an
explicit read is still needed. This patch cannot prove radio coverage or physical
mechanical reliability.

## Opportunistic inventory state query

Public read-only and administrator private PIN/fingerprint/card inventory paths
share one optional state query on the already acquired client, before owned
teardown. It adds **zero connections**, takes at most three seconds and uses only
remaining time from the existing 30-second inventory budget. Interrupted transport
skips the query. A failure/unknown answer preserves the inventory result, the bolt
value and last authoritative timestamp, and marks state uncertain. Cancellation
still propagates; explicit Lock/Unlock can preempt this phase too.

Successful replies publish `query_after_inventory` synchronously before cleanup
can yield. This preserves per-lock revision ordering: late cleanup cannot age an
old read past a newer command or query. Inventory itself never establishes state.
Incomplete PIN coverage remains incomplete, regardless of a successful state read.

## Freshness and privacy

Last Verified State retains its timestamp, unique ID and authoritative source.
The lock and freshness sensor expose only `state_source`, `state_verification`
and `state_uncertain`. States include `confirmation_pending`, `query_failed`,
`followup_exhausted`, `verification_interrupted`, `stale`, `never_verified` and
`verified`. Verified means a recent actual observation, not a continuing physical
guarantee. After five minutes without another authoritative reading a local timer
updates uncertainty; it causes no BLE query. Failed observations/hints never advance
the timestamp or erase a known bolt state. Lock position and freshness should be
considered together in automations.

No credential values, individual identifiers, raw packets or exception text are
added to these diagnostics. No new credential writes, confidential PIN-entry
claims, household identity attribution or automatic Alarmo disarming are enabled.
The independent door contact remains the intrusion sensor, not proof of bolt state.

## Supervised read-only hardware validation after a separately approved release

Install only a separately approved GitHub/HACS build; do not copy development files
into HA. Preserve the existing lock configuration and physical auto-lock delay.

1. Record the installed version and current lock/Last Verified State attributes.
   Keep the access-inventory panel closed. Ensure independent safe entry remains
   available; do not alter credentials or issue test Lock/Unlock commands.
2. During an ordinary household fingerprint unlock, note UTC time and the observed
   physical bolt movement. Observe the subsequent natural auto-lock safely. The
   door-contact transition and fingerprint log alone cannot prove bolt position.
3. Capture the initial authoritative Unlocked reading, then a
   `query_after_activity` Locked reading and newer Last Verified State timestamp.
   Expect up to three queries, spaced as above. A later successful query must
   recover even if the first still sees Unlocked or fails. Hints alone must never
   flip the entity. Record any missed queries, uncertainty and idle disconnects.
4. With independent physical observation and without moving the bolt, optionally
   run each read-only inventory once. Confirm inventory success remains unchanged
   and a successful optional bolt query uses `query_after_inventory`; inspect
   cleanup diagnostics. A failed optional read must not fabricate state or fail a
   completed inventory. Do not export private identifiers or credential payloads.
5. Observe route/scanner and cleanup diagnostics if available. Advertisement
   source does not establish the GATT backend; do not assume the MR5U handled it.
   Do not change radio adapters or the lock's sleep settings to make this test pass.
6. Check reload cancels scheduled work and leaves no follow-up acquisitions.
   Exercise explicit-control priority only in a separately authorised physical
   test; deterministic synthetic tests cover that race in this candidate.

Recommend 3.5.2 as the maintenance version because 3.5.1 is already published.
Do not merge or publish this candidate until review and the separately controlled
hardware/release decision. Existing stable and RC tags/releases remain unchanged.
