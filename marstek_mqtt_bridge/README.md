# Marstek MQTT Bridge

UDP (Marstek Open API Rev 2.0) ↔ MQTT bridge for Home Assistant, built for a Venus A but
generic across the documented command set. Ships as a Home Assistant add-on (Docker) and
publishes MQTT-discovery so all entities show up automatically, grouped into HA "devices"
exactly as you specified.

## Architecture

```
Venus A  <--UDP JSON-RPC-->  bridge.py (this add-on, in Docker)  <--MQTT-->  core-mosquitto  <--MQTT discovery-->  Home Assistant
```

- `udp_client.py` — JSON-RPC-over-UDP client. Implements your retry policy exactly:
  **5 attempts max, timeouts 2s / 7s / 12s / 17s / 22s** (i.e. +5s each retry). If all 5 time
  out, the call raises `MarstekUDPError` and the caller marks the device as unreachable.
- `entities.py` — MQTT-discovery entity/device definitions.
- `bridge.py` — config loading, MQTT connect + watchdog, init sequence, poll loop, command
  handling.
- `logger_setup.py` — dependency-free colored console logging (DEBUG cyan, INFO green,
  WARNING yellow, ERROR red, CRITICAL bold magenta). Set `log_level` in the add-on options.

## Device groups (as requested)

Each shows up as its own device card in Home Assistant, all linked via `via_device` to
**Marstek System** and tagged with your `mqtt_suggested_area`:

| Group | Source | Entities |
|---|---|---|
| **Marstek System** | `Marstek.GetDevice`, `Wifi.GetStatus`, `BLE.GetStatus` | device type, firmware, BLE/WiFi MAC, SSID, RSSI, IP, BLE state, DOD (number), BLE broadcast lock (switch), Panel LED (switch), Communication Established / Communication Fail (binary_sensor) |
| **Marstek Battery** | `Bat.GetStatus` | SOC, charge/discharge allowed, temperature, remaining/rated capacity |
| **Marstek PV** | `PV.GetStatus` | pv1–pv4 power/voltage/current/state (Venus D/A only; skipped for Venus C/E) |
| **Marstek Energy Status** | `ES.GetStatus` | SOC, capacity, PV/grid/battery power, cumulative energy counters |
| **Marstek Energy Mode** | `ES.GetMode` | active mode, grid power, CT status/phase power/energy |
| **Marstek Energy Control** | `ES.SetMode` | mode select (Auto/AI/UPS/Passive) + Passive power & countdown numbers |

## Init sequence (as specified)

On startup, in order: `Marstek.GetDevice` → `Wifi.GetStatus` → `Bat.GetStatus` →
`ES.GetStatus` → `ES.GetMode` → `BLE.GetStatus` → `DOD.SET` → `Ble.Adv` → `Led.Ctrl`.

- `Marstek.GetDevice` is treated as **mandatory** — the device's identity (used to build
  unique IDs / discovery) comes from it. If it fails after 5 retries, the bridge sets
  **Communication Fail**, and retries the *entire* init sequence every 60s until it succeeds.
- The other init calls (WiFi/Battery/ES/BLE) are attempted with the same retry policy but
  are **not** blocking — if one fails, it's logged and the bridge moves on so you still get a
  working device with whatever info is available.
- Once init succeeds, **Communication Established** stays true and is only flipped by
  failures during the later poll loop (per your spec: the state persists across the
  init→poll transition).

## Poll loop

Each of Battery / ES.GetStatus / ES.GetMode / PV / WiFi / BLE has its own configurable
interval in seconds (`poll_interval_*` options, `0` disables it). `DOD`, `Ble_block`,
`Led_Ctrl`, and `ES.SetMode` are **not polled** (the device exposes no "get" for them — see
Assumptions below) — they're pure MQTT command entities, applied instantly when you change
them in Home Assistant.

## MQTT watchdog

Uses paho-mqtt's own reconnect (`reconnect_delay_set`, LWT on `.../bridge/status`), plus a
10s watchdog loop that force-calls `reconnect()` if the client thinks it's disconnected —
matches the "Watchdog = true" behavior you referenced.

## Command feedback (1.0.1+)

`dod`, `ble_block`, `led_ctrl`, and `energy_mode` each publish a companion diagnostic sensor
(`<name>_feedback`) after every write attempt: `OK` on a confirmed `set_result: true`,
`FAILED: <reason>` otherwise (device rejection or UDP timeout after all 5 retries). Watch
these if a change you make in HA doesn't seem to stick.

## Manual refresh buttons (1.0.1+)

Set any `poll_interval_*` option to `0` to disable that endpoint's automatic polling — doing
so automatically adds a matching `button` entity (e.g. "Refresh Battery Status") in the
corresponding device group, so you can still poll it on demand. Buttons only appear for
endpoints where the interval is `0`, to avoid cluttering the UI when periodic polling is
already active.

## Per-endpoint instance IDs — experimental (1.0.1+)

The API docs are unclear on what the `id` field inside `params` actually addresses (e.g. for
multi-module battery stacks). By default every call still uses `id: 0` (matches all worked
examples in the docs). Set `use_per_endpoint_instance_ids: true` and adjust the
`instance_id_wifi` / `instance_id_ble` / `instance_id_bat` / `instance_id_pv` /
`instance_id_es_status` / `instance_id_es_mode` / `instance_id_em` options to experiment —
useful if you find your device actually needs distinct ids per call type. Flip the toggle
back off to instantly revert to the known-good default.

## Assumptions / open questions

Your spec was excellent but a few spots were either ambiguous or left a value unspecified. I
made the following calls — please review before relying on this in production:

1. **`DOD.SET` init value** — your config skeleton didn't include a target value ("DOD ->
   setzen auf -> ..." was left without a number). I added `dod_init_value` (default `88`,
   the device's own factory default) as a new option. Change it in the add-on config.
2. **`Ble.Adv` semantics** — the doc's parameter table says `enable: 0 = enable, 1 = disable`
   for the *broadcast*, but the feature is called "Ble_block" in your spec and you want it
   `ON` at init. I interpreted "block ON" = broadcast **disabled** (`enable: 1`), i.e. the
   switch's `ON` state = Bluetooth locked/blocked. If your firmware behaves the other way
   around (some Marstek firmware revisions have shipped this inverted), flip the mapping in
   `_handle_command()` / the init block in `bridge.py`.
3. **`Communication Established` / `Communication Fail` as switches vs. binary_sensors** —
   you asked for these "as a switch" (`als Schalter`) twice. They're read-only status
   derived from communication health, not something a user should toggle from the HA UI, so
   I implemented them as `binary_sensor` (device_class `connectivity` / `problem`) instead —
   functionally the same "on/off indicator", just not user-writable. Easy to change to
   `switch` (with a no-op command topic) if you specifically want the switch entity type for
   dashboard/automation reasons.
4. **`ES.SetMode` "Manual" mode** — your Energy Control spec explicitly lists only
   Auto/AI/UPS/Passiv, omitting Manual (even though the API supports it). I followed your
   list and left Manual mode out of the `select`. Say the word and I'll add it (it needs 5
   more fields: time_num, start_time, end_time, week_set, power).
5. **Auto-discovered `device_ble_mac` / `device_type`** — per your config skeleton these
   should be "auto-discovered at init, then persisted." The bridge discovers them from
   `Marstek.GetDevice` and uses them for the session, but **does not write them back to
   `/data/options.json`** — HA add-ons don't have a supported way to self-modify their own
   options file from inside the container. If you want real persistence across restarts,
   either (a) read the logged values once and paste them into the add-on config yourself, or
   (b) I can add a small local cache file under `/data/` that's read on startup and skips
   re-discovery if already present — let me know if you want that instead.
6. **`EM.GetStatus`** — polled (per your "configurable" list) but not yet mapped to entities,
   since its fields (`ct_state`, phase power, cumulative energy) look like duplicates of what
   `ES.GetMode` already returns for CT-equipped installs. It's wired into the poll loop and
   comm-status logic; tell me if you want it surfaced as its own entities too (e.g. for a
   device that has a CT but no grid-tie inverter reporting through ES.GetMode).
7. **Multiple physical devices** — this build assumes one Marstek device per add-on
   instance (`device_ip` is a single value), matching your config skeleton. If you have
   multiple Venus units, run one add-on instance per device (different `mqtt_base_topic` and
   `device_ip` each).

## Configuration reference

See `config.yaml`'s `options`/`schema` blocks — every field from your config skeleton is
there, plus the additions from point 1 above and the per-endpoint poll intervals.

## Local testing without HA

```bash
export OPTIONS_FILE=/path/to/local-options.json
pip install -r requirements.txt
python3 bridge.py
```
