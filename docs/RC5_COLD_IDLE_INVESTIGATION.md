# RC5 cold-idle connection investigation

This note records the conclusion reached before changing connection behaviour.
It applies to Home Assistant 2026.8.0, `habluetooth==6.26.5`,
`bleak-retry-connector==4.6.3`, and released `ttlock-ble==0.1.11`.

## Technical conclusion

1. **Why RC4 fails — confirmed.** RC4 treats
   `async_ble_device_from_address(..., connectable=True)` as the only usable
   connection candidate. In HA 2026.8 that aggregate lookup reads the
   Bluetooth manager's connectable history. The manager can simultaneously
   have a device record for the lock from a registered connectable scanner in
   `async_scanner_devices_by_address(..., connectable=True)`. RC4 ignores those
   records and therefore waits for an aggregate-history classification change
   instead of attempting GATT.

2. **Why the background loop succeeds — confirmed mechanism, inferred timing.**
   Background maintenance and explicit commands call the same resolver and
   client-construction path. The background loop does not have a more capable
   connection algorithm. Its repeated, indefinitely scheduled attempts can
   eventually coincide with the lock appearing in aggregate connectable
   history; that timing explanation is strongly inferred from the code and
   observed periodic success. The exact radio event that updates that history
   on the real adapter remains unknown.

3. **Why the official app succeeds — confirmed outcome, unknown proprietary
   mechanism.** The real iOS app has proved that the lock accepts a
   software-initiated cold-idle connection. It is strongly inferred that the
   app discovers the known peripheral and initiates a connection without HA's
   aggregate-history gate. Its exact Core Bluetooth scan, cache, and retry
   sequence is proprietary and has not been established.

4. **Role of HA connectable history — confirmed.** It is useful evidence that
   HA recently selected a connectable route, but it is not the complete set of
   device records currently held by connectable scanners. HA's public
   `async_scanner_devices_by_address` API exposes those per-scanner records.
   HA's Bleak client wrapper independently resolves and scores the best local
   adapter or proxy path by address when the actual connection starts.

5. **Is bleak-retry-connector prematurely bypassed? — confirmed.** Released
   SDK 0.1.11 calls `establish_connection(..., max_attempts=3,
   use_services_cache=True)` when given a `BLEDevice`. RC4 never constructs the
   SDK client when the aggregate lookup is `None`, so those GATT attempts and
   their transient-failure backoff never run in the failing state.

6. **Are explicit and background acquisition racing? — confirmed no.** A
   single per-lock `asyncio.Lock` covers device resolution, client creation,
   connection, and every command. Background maintenance, coordinator reads,
   operation-log reads, management actions, and explicit lock/unlock therefore
   cannot open simultaneous connections. If background connection acquisition
   finishes first, a waiting explicit operation acquires the mutex next and
   reuses that live client.

7. **Layer that should own the fix — confirmed.** The HA integration owns HA
   scanner/device lifecycle; the SDK owns TTLock protocol and GATT transport.
   The smallest clean fix is to let the integration obtain a candidate from
   HA's per-connectable-scanner records when aggregate connectable history is
   absent, then continue through the unchanged
   `TTLockClient.from_ble_device()` path. No SDK change or dependency change is
   justified.

8. **Proxy compatibility — confirmed from HA architecture.** The fallback asks
   HA only for records belonging to scanners registered as connectable. Those
   scanners can be local controllers or supported remote proxies. The HA Bleak
   wrapper makes the final route/slot/RSSI choice at connection time, so the
   integration neither pins `hci0` nor bypasses HA with a standalone scanner.

## Selected algorithm

For one serialized explicit operation:

1. Reuse an existing live per-lock SDK client.
2. Try HA's aggregate connectable-device lookup.
3. If that misses, inspect HA's records for the same address from connectable
   scanners and use the strongest record as a connection candidate. There is
   no integration-defined RSSI cutoff; HA still selects the actual route.
4. If no candidate exists, request one HA-managed active-scan window and poll
   both in-memory representations for at most 25 seconds.
5. Give the first candidate to `TTLockClient.from_ble_device()` immediately.
   SDK 0.1.11 then owns the actual GATT connection and its three attempts.
6. On acquisition timeout, retain the HA human-readable reachability
   diagnostic. Cancellation, unload, or shutdown cancels the scan and forbids
   a late retained connection.

Passive advertisement processing and background maintenance remain unchanged:
they do not request active scans. The change does not clear advertisement
history, start a local Bleak scanner, keep a permanent connection, or add
periodic radio traffic.

## Reference findings

- Current HA Bluetooth documentation identifies both
  `async_ble_device_from_address` and `async_scanner_devices_by_address` as
  supported APIs and requires integrations to use HA's shared scanner.
- HA 2026.8 source confirms aggregate connectable history and per-scanner
  device records are separate manager stores. Its Bleak wrapper resolves the
  best available backend/device by address immediately before connecting.
- Released SDK 0.1.11 skips its standalone scanner when constructed from a
  supplied `BLEDevice`, then delegates connection retries, service caching, and
  connection-slot handling to bleak-retry-connector.
- `kind3r/ttlock-sdk-js` searches for the known lock and connects after it is
  discovered; its passive status analysis names bit `0x08` as `isTouch`.
  `kind3r/esp32-ble-gateway` records address/type during discovery and stops
  scanning before connecting. These GPL projects were used only as behavioural
  references; no code was copied.
- `rabeckas/ttlock-local-ble` serializes per-lock BLE operations, supplies an
  HA-resolved `BLEDevice` for local/proxy support, and delegates connection to
  bleak-retry-connector. Its current aggregate-lookup prerequisite does not
  address this RC4 edge case; no code was copied.

`isTouch` remains an optional diagnostic research signal. It is not part of
the selected connection algorithm because cold-idle operation must not depend
on physical touch, and the confirmed HA-layer gate can be fixed independently.
