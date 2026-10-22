# TTLock BLE (danoev) 3.5.1

This independently maintained distribution builds on Rodrigo Roque's original
[integration](https://github.com/roquerodrigo/ha-ttlock-ble) and
[SDK](https://github.com/roquerodrigo/ttlock-ble). Both retain MIT licensing,
original copyright and history. Rodrigo does not maintain or endorse this fork.
See [LICENSE](../LICENSE) and the [maintenance process](MAINTENANCE.md).

## Stable scope

Local BLE Lock/Unlock retains bounded, prioritized HA-managed acquisition,
single physical control dispatch, query-only ambiguity reconciliation and owned
cleanup. Advertisements remain hints; only connected queries/acknowledgements
establish authoritative bolt state. No raw/private BlueZ, global Active scanner,
RSSI cutoff or default reconnect storm is introduced.

The administrator-only TTLock access panel supports read-only PIN, fingerprint
and four-byte card inventory, private opaque references, local display labels,
freshness, explicit local reference reset and legacy PIN-service opt-out. The
API requires an active normal administrator frontend session and same origin;
long-lived automation tokens are rejected. Reverse proxies must use HA's trusted
forwarding settings. No credential secret may be entered as a label.

## Observation and reconciliation contract

| Evidence | Display/state | Allowed local naming | Physical revocation proven? |
|---|---|---|---|
| Complete, uninterrupted, distinct record read with confirmed cleanup | Currently observed and fresh | Yes, until freshness expires | No |
| Read fails, is partial/cancelled, or cleanup is uncertain | Previously observed; currently unverified; last-known label retained | No | No |
| PIN absent from cursor-complete limited read | Not observed in this limited PIN read; status unknown; same reference and label retained | No | No |
| Previously observed fingerprint/four-byte card absent from complete indexed read | No longer observed; prior reference/label retained as stale history | No | No |
| Future verified deletion operation | Not implemented; no current path assigns this state | No current operation | Not implemented |

PIN cursor completion and inventory coverage are independent. Unused offline or
batch-generated PINs may be omitted. Missing PINs retain their scoped reference
instead of creating a duplicate on reappearance. A matching PIN record is not
person identity, proof of uninterrupted physical enrollment, or a write token.
Already duplicated RC16 generations are not silently merged: review/reset local
references explicitly if old history is ambiguous. Fingerprint/card reappearance
after authoritative non-observation still creates a new generation without
inheriting an old label. No inventory absence is called physical deletion.

Failures in the browser preserve only the selected lock/method's cached metadata
and mark it unverified; different locks never inherit that cache. Server-side
freshness/revision checks remain authoritative. A failed request never triggers a
reference reset. Disconnection aborts the owned request and invalidates its UI
generation; a late reply cannot repaint a newly attached panel. Restart keeps
labels/references but drops freshness; local reset
and config-entry removal invalidate older in-flight reads. Storage stays private,
atomic, version 1 and credential-free; host administrators/backups remain trusted.

## Hardware evidence and limitations

The administrator supplied S31 protocol 5.3 evidence: one permanent PIN, two
fingerprints and two four-byte IC cards, matching the official TTLock app. PIN
CMD07 traversal reached the exact validated terminal marker. RC16 labels persisted
through HA restart; intermittent reads could fail and later recover. This is
evidence for that lock/firmware, not all TTLock hardware or exhaustive PIN discovery.
Actual GATT adapter/proxy attribution, full schedules and person attribution are
unverified. No claim is made that the SMLIGHT proxy executed a connection.

No new confidential create/modify/delete interface, physical enrollment or new
credential-write dispatcher is enabled. Offline SDK command models are not
operational support. Existing legacy PIN add/delete/clear services remain enabled
by default with their old permissions: their inputs can reach service events,
Recorder and automation traces before the integration sees them. The admin panel
does not secure those legacy inputs. Its per-lock opt-out blocks future BLE writes
but cannot retract an already submitted PIN. Do not test it by sending a PIN.

Automatic Alarmo disarming remains disabled. An independent door contact is the
intrusion sensor; an unlocked bolt or a label alone never authorizes disarming.
Radio reliability still depends on signal, slots, adapters and firmware.

## Upgrade and compatibility

Use the same danoev HACS repository and select stable v3.5.1, restart HA and refresh
the browser. Keep the existing integration/config entry; do not install a second
copy. HACS displays **TTLock BLE (danoev)**. The internal manifest name TTLock BLE,
domain/directory ttlock_ble, all entity/unique IDs and private storage schema stay
unchanged. The frontend uses a new versioned module URL to avoid reusing RC16's
module in an already open browser session.

The HACS compatibility floor remains HA2026.5.1/HACS2.0.5. Newer Bluetooth
reachability diagnostics gracefully disappear on the older HA version; acquisition
and control do not change. A small schema adapter selects HA's legacy voluptuous
engine before 2026.10 and probatio from 2026.10; validation rules and service
permissions stay the same. Minimum-version setup/entity/admin tests and current
HA2026.10.2 validation are synthetic; no live HA instance or lock was operated.
Use current HA security patches. A compatibility floor is not a recommendation to
run an old HA core or override its global dependency pins.

The SDK remains exactly `73e6c89544dd178cb09de9b88f29f5cf204b2429`, archive
SHA-256 `49ae5d7ccc53c16a96de49089a31ebcbce424d9bbde447263df1b313269cd112`.
No migration to upstream's 0.3.2 transport is hidden in this release.

See the [dependency security assessment](SECURITY_3.5.1.md) for the distinction
between the SDK dependency gate and findings in HA core/test tooling. No
claim is made that an old HA installation becomes secure merely by upgrading
this custom integration.
