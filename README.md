# Home Assistant TTLock BLE

> [!IMPORTANT]
> This is a maintained development fork of
> [`roquerodrigo/ha-ttlock-ble`](https://github.com/roquerodrigo/ha-ttlock-ble),
> whose upstream work remains credited and preserved. Report problems specific
> to this fork to the
> [`danoev/ha-ttlock-ble` issue tracker](https://github.com/danoev/ha-ttlock-ble/issues),
> not to the upstream project.

[![CI](https://github.com/danoev/ha-ttlock-ble/actions/workflows/ci.yml/badge.svg)](https://github.com/danoev/ha-ttlock-ble/actions/workflows/ci.yml)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=danoev&repository=ha-ttlock-ble&category=integration)

---

Local control of TTLock smart locks over Bluetooth, for [Home Assistant](https://www.home-assistant.io/). Lock / unlock, battery level and real-time push events flow over BLE — no cloud round-trip on every operation. Built on the sibling Python SDK [`ttlock-ble`](https://github.com/roquerodrigo/ttlock-ble).

> [!CAUTION]
> Version `3.5.1rc9` is a hardware-validation prerelease, not a
> production-certified release. It retains released `ttlock-ble==0.1.11` and
> makes one-shot fresh-route acquisition safe for TTLock's static
> advertisements under HA deduplication, while retaining RC8's stale-route
> fallback, RC7's startup bootstrap, and RC6's command/state/log corrections. Follow the
> staged [real-lock checklist](docs/HARDWARE_VALIDATION.md). Passage mode and
> further credential work remain excluded.

## Features

- **Local BLE control** — lock, unlock, and state queries run over the lock's BLE link; the TTLock cloud is only contacted once at setup to download per-lock keys.
- **Real-time push events** — keypad presses, fingerprint reads, IC-card swipes, mechanical key turns, and auto-lock fires arrive as Home Assistant events the moment the lock emits them.
- **Battery sensor** — diagnostic entity refreshed by every poll *and* every push, no extra BLE traffic.
- **2FA-aware config flow** — handles TTLock's "new device" verification by emailing a one-time code and prompting for it.
- **Works without a cloud account** — a lock initialised locally can be added by entering its key directly, no TTLock account involved at any point.
- **Passive Bluetooth hints** — advertisements update battery and signal a possible state change without claiming their protocol bit is persistent bolt position; connected state queries remain authoritative.
- **Persistent BLE session with a post-drop cooldown** — keeps the link warm to receive push events, and waits before reconnecting so a lock in idle-sleep isn't thrashed.
- **Reauth + reconfigure** — re-prompt for credentials in place when the cloud rejects the cached login, or edit them via the integration's three-dot menu.
- **Diagnostics** — downloadable dump with credentials/keys redacted.
- **Local management actions** — create/delete permanent or time-windowed
  keypad passcodes and read/set/disable the lock's native auto-lock delay.
- **Bounded command acquisition** — an explicit command that cannot immediately
  resolve aggregate connectable history also checks HA's per-connectable-scanner
  paths, then requests at most one 25-second Home Assistant active scan when no
  path exists, learned timing proves aggregate history stale, or a recent
  pre-command route fails. The accepted candidate goes through the SDK's normal
  retried GATT connection flow.
- **Authoritative startup bootstrap** — while state is Unknown, the first
  coordinator query may use the same bounded exact-address Home Assistant
  Active window, without treating advertisement or push hints as bolt state.
- **Stale-route recovery** — an active-capable operation whose cached HA route
  exhausts its pre-command GATT attempt requests exactly one fresh exact-address
  Active acquisition. A control command is still issued at most once.
- **Static-advertisement acquisition** — before that one-shot wait, the
  integration clears HA's exact-address advertisement deduplication state so
  the next identical local/proxy packet reaches the acquisition callback.
- **Translations** — English and Brazilian Portuguese (parity enforced by tests).

## Entities

Each configured lock produces one HA device with four entities:

| Entity | Domain | Purpose |
|---|---|---|
| `lock.<alias>` | `lock` | Locked/unlocked state, with optimistic updates, `locking`/`unlocking` transitional states and a post-command settle window. |
| `sensor.<alias>_battery` | `sensor` | Battery percentage (diagnostic). |
| `binary_sensor.<alias>_connection` | `binary_sensor` | Live BLE link state (connectivity, diagnostic). |
| `event.<alias>_log` | `event` | Fires for every new operation-log record read from the lock. |

The event entity classifies each record as `unlock`, `lock`, `unlock_failed`, `password_change` or `other`, and attaches `record_type` and `battery` always, plus `timestamp`, `uid`, `credential`, `key_id` and `accessory_battery` when the record carries them. `credential` is only populated for record types where the value is an identifier (card number, fingerprint id, fob MAC) — record types where it would be a working door code never expose it.

## Installation

1. Install via HACS using the button above, or add this repo as a custom HACS repository (category: Integration).
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **TTLock BLE**.
4. Choose how the keys are obtained:
   - **Sign in to a TTLock account** — enter the email + password you use in the official app. If TTLock has never seen this Home Assistant before it emails a verification code; paste it into the next step. Every lock on the account is synced at once.
   - **Enter a lock key manually** — for a lock initialised outside the cloud. See below.
5. From this point on, all lock / unlock / state operations stay on Bluetooth.

### Adding a lock without a TTLock account

A lock initialised by a local BLE bridge never goes through the cloud, and its owner already holds what Bluetooth needs. That key can be entered directly, one lock per entry:

| Field | Notes |
|---|---|
| Lock MAC address | `AA:BB:CC:DD:EE:FF` |
| AES key | 16 bytes, continuous hex or separated (`2c,3d,…`) |
| Unlock key | digits, entered verbatim — not the obfuscated form the cloud returns |
| Admin passcode | optional, digits; only needed for managing passcodes |
| Protocol type / version / scene / group ID / organisation ID | the frame header the lock expects. The defaults (5, 3, 2, 1, 1) suit most V3 locks |

Only these reach the wire. The rest of what the cloud returns per key — user id, lock-flag position, validity window — is never read by the Bluetooth layer, which addresses the lock with a zeroed user id and the firmware's "permanent key" date literals regardless.

If the lock is in range when the form is submitted, the protocol type, version and scene are checked against what it broadcasts, so a wrong value is caught there instead of becoming a lock that never answers. Getting a value wrong later is fixable through the integration's three-dot menu → **Reconfigure**.

The Bluetooth radio HA already manages (USB dongle, built-in adapter, or proxy) discovers the lock automatically — no additional configuration.

## Options

Settings → Devices & Services → TTLock BLE → **Configure** lets you tune:

- `scan_interval` (default 3600 s, minimum 60 s) — how often the coordinator opens a BLE session for an authoritative state read. Advertisement hints do not postpone this poll; lowering the interval adds connections and battery drain.
- `reconnect_interval` (default 300 s, minimum 10 s) — how long the connection layer waits after the lock drops the BLE session before reconnecting. The lock closes every idle session within seconds, so this is effectively how often a session is reopened to listen for push events.
- `permanent_connection` (default off) — reconnect immediately after every drop, keeping the session open as continuously as the lock allows. Overrides `reconnect_interval` and increases the lock's battery drain; push events (keypad, auto-lock) arrive in real time in exchange.

To edit credentials without removing and re-adding, use the integration's three-dot menu → **Reconfigure**.

## Local management actions

The first management backend is exposed as Home Assistant actions. Select the
lock through the device picker; each action then connects through Home
Assistant's Bluetooth manager, so a local adapter and an ESPHome active
Bluetooth Proxy follow the same path.

| Action | Purpose |
|---|---|
| `ttlock_ble.add_passcode` | Add a permanent or date/time-windowed keypad passcode. |
| `ttlock_ble.delete_passcode` | Delete the specified keypad passcode. |
| `ttlock_ble.clear_passcodes` | Irreversibly remove every keypad passcode stored by the lock. |
| `ttlock_ble.get_auto_lock` | Return `seconds`, the current native auto-lock delay. |
| `ttlock_ble.set_auto_lock` | Set the native delay; `seconds: 0` disables auto-lock. |

Temporary windows are interpreted in Home Assistant's configured time zone and
sent to the lock with minute precision. Correct behaviour therefore depends on
the lock's RTC representing the same local wall clock; verify the clock before
using a temporary credential on real hardware.

The integration does not retain a submitted passcode, write it to its logs, add
it to entity attributes, or include it in diagnostics/events. A passcode placed
directly in an automation or script is still stored in that Home Assistant YAML
and may appear in Home Assistant's automation trace, so protect the automation
configuration accordingly.

Example temporary passcode:

```yaml
action: ttlock_ble.add_passcode
data:
  device_id: 0123456789abcdef0123456789abcdef
  code: "583921"
  type: period
  start: "2026-08-22T15:00:00"
  end: "2026-08-25T10:00:00"
```

## How it works

The lock's TTLock firmware aggressively closes idle BLE sessions (~5 s of silence and it drops). The integration:

1. Passively reads battery and a protocol state-change hint from advertisements. Neither overwrites authoritative connected state.
2. On startup while state is Unknown, performs an authoritative query and may request one bounded exact-address HA-managed Active window if no route is cached or a cached route proves unreachable before the query. Known-state routine polls remain non-active.
3. Keeps a persistent BLE session via `connection.py`, reconnecting on every drop signalled by bleak, and waiting out the configured `reconnect_interval` before doing so — or none at all with `permanent_connection`.
4. After a user-initiated `lock`/`unlock`, the SDK keeps the link alive for 25 s so push events (the lock's reports of keypad operations, auto-locks, etc.) reach Home Assistant in real time.
5. Advertisement and short-heartbeat push state are hints. A change triggers a connected `SEARCH_BICYCLE_STATUS` query, and every applied transition logs its safe source, age, and route RSSI.
6. If lock/unlock wrote its complete control frame but lost the acknowledgement, the command is never resent. One fresh state query may reconcile it to success; otherwise Home Assistant reports the outcome as unknown.

### Passive versus connectable Bluetooth

Home Assistant can receive a TTLock passive advertisement without currently
having a connectable `BLEDevice`. The passive packet is sufficient for battery
and change detection, but authoritative state, lock/unlock, and local
credential or auto-lock management need a connectable path.

For explicit operations, Unknown-state startup bootstrap, or an explicit
entity refresh, the integration first checks Home Assistant's
connectable-device cache. If the lock is absent, or that route's full
pre-command connection attempt fails, it starts one
bounded 25-second exact-address `async_process_advertisements()` wait in Active
scanning mode. Home Assistant schedules that temporary window while the adapter
remains configured as Auto; local adapters and ESPHome active Bluetooth Proxies
stay behind the same HA Bluetooth API. A matching callback is accepted as
connectable only when its age fits the device-aware freshness window; ancient history is rejected, while recent
history and the next post-clear static advertisement remain usable. That
callback's HA route then enters the existing SDK client path.
The fallback is bounded to one Active acquisition and cannot resend a control
frame because it completes before authentication or command dispatch. Timeout retains the
detailed Bluetooth reachability diagnosis. The persistent advertisement
tracker is Passive, and coordinator polling plus the background reconnect loop
do not globally activate a scanner. Routine polling after an authoritative
state is known and the background reconnect loop remain non-active.

## Useful commands

```bash
scripts/setup      # install dependencies (uv sync, dev + lint groups)
scripts/develop    # start Home Assistant in debug mode with the integration loaded

# Lint and test directly (config lives in pyproject.toml):
uv run ruff format --check .
uv run ruff check .
uv run mypy custom_components/ttlock_ble
uv run pytest
```

HA runs with config in `config/` and `PYTHONPATH` pointing at the repo root. To reset entity/device IDs during development:

```bash
rm config/.storage/core.entity_registry config/.storage/core.device_registry
```

## Layout

```
custom_components/ttlock_ble/
├── __init__.py        # config-entry lifecycle: setup / unload / reload
├── advertisement.py   # passive battery and state-change hints
├── api.py             # TtlockBleApiClient: TTLockCloud wrapper (cloud bootstrap only)
├── binary_sensor.py   # TtlockBleConnectionBinarySensor: live BLE link state
├── client.py          # explicit-control stage and outcome attribution
├── brand/             # icon / logo PNGs (local placeholder for HA brand registry)
├── config_flow.py     # menu / cloud / manual / verify_code / reauth / reconfigure
├── connection.py      # TtlockBleConnection: persistent BLE session per lock
├── const.py           # DOMAIN, LOGGER, defaults
├── coordinator.py     # DataUpdateCoordinator polling each connection
├── data/              # one TypedDict/dataclass per file + type aliases in __init__.py
├── diagnostics.py     # redacted credentials/keys
├── entity.py          # base CoordinatorEntity with DeviceInfo
├── event.py           # TtlockBleLogEvent: operation-log records as HA events
├── exceptions/        # one file per exception class
├── lock.py            # TtlockBleLock: LockEntity backed by the connection
├── manifest.json
├── manual_key.py      # TtlockBleManualKey: key entry for cloud-less locks
├── options_flow.py    # TtlockBleOptionsFlow: scan_interval, reconnect_interval, permanent_connection
├── sensor.py          # TtlockBleBatterySensor backed by polls + pushes
├── services.py        # device-targeted passcode and auto-lock actions
├── services.yaml      # action descriptions and selectors
└── translations/
    ├── en.json
    └── pt-BR.json
```

Conventions for contributors live in [`CODE_STYLE.md`](./CODE_STYLE.md); architectural notes for AI agents in [`CLAUDE.md`](./CLAUDE.md).

## Pre-commit hooks

Install once per clone (after `scripts/setup`):

```bash
pre-commit install
```

This wires ruff, mypy and basic file hygiene checks (`.pre-commit-config.yaml`) into every commit, mirroring the CI lint job.

## CI

- **`ci.yml`** — lint (ruff check + format, mypy), tests (pytest with the 90 % coverage gate) and validation (`hassfest` + HACS) via the shared reusable workflows
- **`codeql.yml`** — GitHub CodeQL security scan; push/PR to `main` and a weekly cron
- **`release.yml`** — release-please opens a release PR on every push to `main` based on conventional commits
- **`auto-assign.yml`** — assigns the repository owner to new issues and pull requests

## License

[MIT](LICENSE)
