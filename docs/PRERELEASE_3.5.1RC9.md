# TTLock BLE 3.5.1rc9

This is a **hardware-validation-only prerelease**, not a production-certified
release. RC1 through RC8 are superseded for testing but their tags and commits
remain unchanged. RC9 keeps `ttlock-ble==0.1.11` and does not merge PR #2.

## Investigation result

### Confirmed

- HA refreshes aggregate advertisement history before its unchanged-payload
  callback guard. Diagnostics can therefore say “last advertisement 9s ago”
  while an exact-address callback receives nothing.
- RC8 rejected all synchronous history older than callback registration. HA
  core issue #173400 documents the same reject-replay plus deduplication
  deadlock, and merged PR #173488 replaced it with an age-based filter.
- HA documents `async_clear_advertisement_history()` for static advertisements
  that require a GATT connection. It clears exact-address advertisement
  deduplication, not integration matcher history, and works at the shared
  manager layer for local adapters and remote proxies.
- The pinned SDK exposes only the final connection result after its connector
  retry policy; RC9 cannot selectively shorten internal retries safely.

### Strongly inferred

- HA deduplication explains the RC8 fallback timeout despite a packet being
  recorded during its 25-second window.
- Clearing exact-address history immediately before the one-shot wait, then
  applying a bounded age filter, is safer than either a strict scan-start gate
  or accepting arbitrary retained history.
- When HA has learned the lock's advertising interval, a route older than both
  the normal 25-second acquisition and two learned maximum intervals has missed
  enough expected packets to justify scan-first handling for an active-capable
  call. Recent candidates still receive the full connector retry policy.

### Unknown

- Which scanner/backend supplied the packet recorded nine seconds before the
  RC8 timeout.
- The learned advertising interval stored for the hardware at that instant.
- Whether a future SDK will expose a typed early device-absent result.

## RC9 behaviour

- Existing live sessions remain first choice.
- Recent cached/per-scanner candidates retain the existing SDK connection path.
- A cadence-proven old aggregate candidate goes directly to the one bounded
  exact-address HA Active acquisition while the adapter remains Auto.
- Immediately before waiting, RC9 clears only that address's advertisement
  deduplication state. The next identical packet can reach the callback.
- Callback history older than the device-aware window is rejected; recent
  history and truly new local/proxy callbacks are accepted.
- A failed recent pre-command connection still receives the same single Active
  fallback introduced by RC8.
- Background maintenance remains non-active. Authentication, control-result
  reconciliation, authoritative state, and operation-log behaviour are
  unchanged. A lock/unlock control frame is never resent.

## Validation and hardware gate

Automated tests cover ancient and recent callback history, clear-before-wait
ordering for a static advertisement, cadence-proven scan-first selection,
stale-route failure followed by one successful fresh candidate, timeout,
cancellation, direct/proxy routes, startup authoritative state, background
non-active behaviour, and single control dispatch. Record final local and
hosted results only after every gate completes. Local validation passed with
336 tests at 98.15% coverage, Ruff formatting and lint, configured mypy, and
JSON/YAML/version consistency.

Use the updated [hardware-validation runbook](HARDWARE_VALIDATION.md). The key
acceptance trace is:

```text
old route rejected or fails before authentication
  -> exact-address advertisement history cleared
  -> one HA-managed Active window under Auto
  -> unchanged TTLock advertisement reaches callback
  -> fresh GATT succeeds
  -> one control frame and one physical operation
```
