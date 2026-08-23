# TTLock BLE 3.5.1rc8

This is a **hardware-validation-only prerelease**, not a production-certified
release. RC1 through RC7 are superseded for testing but their tags and commits
remain unchanged as reproducible development artefacts. RC8 keeps the released
`ttlock-ble==0.1.11` dependency and does not merge the development branch.

## Investigation result

### Confirmed

- Home Assistant's aggregate connectable lookup can return retained
  connectable history without applying an age limit; its service-info record
  exposes a monotonic receipt timestamp.
- An exact-address `async_process_advertisements()` wait in Active mode is the
  Home Assistant-native mechanism for a temporary active window while the
  adapter remains configured Auto, for both local scanners and active proxies.
- That wait may synchronously replay matching cached history, so callback
  delivery alone does not prove that a route is fresh.
- The SDK finishes `connect()` before authentication, state query, or control
  dispatch. A fallback after `connect()` fails is therefore still pre-command.
- The released SDK delegates GATT retry policy to `bleak-retry-connector`; its
  device-not-found path can consume several retries before returning the final
  `TTLockError` to the integration.

### Strongly inferred

- RC7's nine device-not-found errors came from retrying one stale retained
  local route rather than from nine integration-level command attempts.
- Failure of the selected route's full pre-command connect attempt is the
  safest route-validity signal. An RSSI or advertisement-age threshold would
  reject valid weak/slow locks and would not prove reachability.
- Accepting only a callback whose receipt timestamp is newer than the new
  acquisition window, then using that callback's own `BLEDevice`, avoids
  immediately selecting the same stale aggregate record again.

### Unknown

- Whether the stale object was retained by BlueZ, a scanner backend, or only
  Home Assistant's aggregate history at the exact failure instant.
- Which local adapter or ESPHome proxy will supply the next fresh route on any
  given installation.
- Whether a future SDK should expose a narrower non-retrying connection error;
  RC8 does not require or invent such an API.

## RC8 behaviour

An active-capable explicit operation or authoritative Unknown-state query now:

1. reuses an existing live client;
2. tries HA aggregate/per-scanner connectable history;
3. if that complete pre-command connection attempt fails, rejects the route for
   this acquisition;
4. requests exactly one bounded, exact-address HA-managed Active wait;
5. ignores service-info records received before the new wait began;
6. connects through the fresh callback's local/proxy route and proceeds through
   the existing SDK path.

No RSSI cutoff, raw BlueZ access, standalone Bleak scanner, global Active mode,
or second control-frame write is introduced. Background maintenance and
known-state routine polling remain non-active. A final miss or timeout preserves
the existing credential-safe Home Assistant reachability diagnostic.

## Automated coverage

Regression tests cover immediate candidates, stale cached-route failure followed
by one fresh direct/proxy callback, replay rejection, fresh-route failure without
another scan, active-scan timeout, cancellation/unload, queued explicit work
after a failed background attempt, non-active background maintenance, mutex
serialization, single command dispatch, and secret-free logs. RC6 command
reconciliation, state-source ordering, and operation-log seeding tests remain in
the full suite.

Local validation passed with 332 tests and 98.13% coverage, Ruff formatting and
lint, configured mypy, and JSON/YAML/version consistency. The GitHub prerelease
is published only after the branch's hosted pytest, Ruff, mypy, version,
hassfest, HACS, and CodeQL checks pass.

Use the [hardware-validation runbook](HARDWARE_VALIDATION.md), including its
explicit several-minutes-old connectable-history case, before treating RC8 as
suitable for any broader rollout.
