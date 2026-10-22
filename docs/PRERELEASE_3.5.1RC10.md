# TTLock BLE 3.5.1rc10

RC10 is a hardware-validation-only prerelease. RC1-RC9 are superseded for new
testing but remain unchanged as reproducible artifacts. Nothing is merged and
RC9 remains the rollback checkpoint.

## Evidence-backed changes

- Successful connected state is published before operation-log retrieval;
  log timeout, malformed data, or disconnect cannot invalidate that state.
- A failed later poll preserves the last authoritative state and battery.
- Passive advertisement battery remains immediate. State and new-record bits
  remain hints and request an authoritative query/log sync on meaningful change.
- Operation-log replay protection uses a bounded persisted journal of hashes
  derived from non-secret metadata plus a per-start timestamp boundary. Page
  length is no longer treated as proof that history is exhausted.
- Default operation is on demand after startup. Never-authoritative Unknown
  state retries with bounded backoff; hourly polling remains the sanity check.
  Legacy explicitly configured reconnect maintenance and permanent connection
  remain opt-in paths.
- A cached active-capable route receives one speculative connector attempt.
  Failure before authentication/control permits RC9's single fresh HA-managed
  Active acquisition; the fresh route retains three connector attempts.
- Operation events include safer method attribution, including physical key.
  The SDK's overloaded credential field is never written to Recorder.

## SDK pin

RC10 pins immutable SDK commit
`53c78183e3ab7c1e96d6f9912a983b515388546c` from
[`danoev/ttlock-ble`](https://github.com/danoev/ttlock-ble). Its sole RC10 API
change makes connector attempts configurable per client. It does not change
protocol frames, command retries, acknowledgement handling, or credentials.

## HACS prerelease packaging

The RC10 GitHub prerelease must include `ttlock_ble.zip`. The archive contains
only the tracked contents of `custom_components/ttlock_ble/`, with
`manifest.json` at the archive root, matching the fork's `hacs.json` release
asset declaration. The fork owns the packaging workflow and it uses only the
repository-scoped GitHub token; it does not call an upstream reusable workflow
or use an upstream secret.

HACS discovers versions from published GitHub Releases, not tags alone. Enable
the tracked repository's prerelease switch before selecting RC10. If HACS still
identifies the repository as `roquerodrigo/ha-ttlock-ble`, remove that upstream
custom-repository registration and add
`https://github.com/danoev/ha-ttlock-ble` as type **Integration**. This does not
require removing the existing Home Assistant TTLock config entry.

## Preserved safety invariants

- Lock/unlock control payloads remain single-send.
- A completed write with lost acknowledgement is never automatically resent.
- Reconciliation uses one authoritative state query only.
- Advertisement/push bits never directly set physical state.
- Active acquisition remains exact-address, bounded, HA-native, Auto-mode and
  compatible with local scanners and ESPHome Bluetooth proxies.
- No global Active mode, raw BlueZ, private scanner, fixed `hci0`, or RSSI cutoff
  was introduced.

Run [the RC10 hardware checklist](HARDWARE_VALIDATION.md) before judging this
candidate release-ready.
