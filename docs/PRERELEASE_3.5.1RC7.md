# TTLock BLE 3.5.1rc7

> **Superseded for testing by 3.5.1rc8.** This document and tag remain only as
> a reproducible development artefact.

This is a **hardware-validation-only prerelease**, not a production-certified
release. RC1 through RC6 are superseded for testing but remain tagged as
reproducible development artefacts. RC7 keeps `ttlock-ble==0.1.11` and does not
merge the hardware-validation branch or draft pull request.

## RC7 hardware finding

RC6 correctly made advertisement and short push state non-authoritative, but a
real lock could remain Unknown after startup when Home Assistant had no current
connectable candidate. A later `get_auto_lock` action proved that the existing
exact-address HA-managed Active acquisition works with the adapter configured
Auto, and that a subsequent connected `query_state()` publishes the correct
physical state. The missing path was therefore the initial authoritative state
query, not the query parser or HA Auto-mode route.

## RC7 change

- The first coordinator poll for an Unknown lock requests an authoritative
  query with bounded active acquisition enabled. Existing aggregate and
  per-scanner candidates are reused before one 25-second exact-address
  `async_process_advertisements()` Active window is requested from Home
  Assistant.
- A successful connected query publishes Locked or Unlocked with query source
  attribution. A timeout leaves state Unknown; advertisements and short push
  frames still cannot invent physical state.
- A changed advertisement hint and an explicit `homeassistant.update_entity`
  refresh may request the same active-capable authoritative path.
- Once an authoritative state is known, ordinary hourly coordinator polling
  stays non-active. The persistent advertisement callback remains Passive and
  no scanner is switched globally to Active.
- The per-lock connection mutex continues to serialize acquisition and GATT,
  so concurrent Unknown-state refreshes cannot open duplicate active waits or
  simultaneous lock connections.
- Only Home Assistant Bluetooth APIs are used. Direct adapters and ESPHome
  active Bluetooth Proxies retain HA's normal routing and cancellation.

## Preserved RC6 behaviour

- Ambiguous post-write lock/unlock outcomes are never automatically resent and
  may be reconciled only by one fresh connected state query.
- Advertisement bit 0 and decoded short-heartbeat state remain hints rather
  than authoritative bolt position.
- Initial operation-log pagination remains silent history seeding; later new
  records emit once.
- Explicit lock/unlock, passcode add/delete, and auto-lock management retain
  existing candidate selection, bounded active acquisition, reachability
  diagnostics, and credential-safe logging.
- Background maintenance is unchanged. A successful maintenance connection
  alone does not query or publish authoritative state; failed candidate
  acquisition follows its own bounded backoff and is not an hourly schedule.

Use [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md). First prove the startup
Unknown-to-Locked/Unlocked bootstrap with the HA adapter in **Auto**, then run
the retained cold-idle command-result/state checks. Do not proceed to deferred
credential or passage-mode validation until those gates pass.
