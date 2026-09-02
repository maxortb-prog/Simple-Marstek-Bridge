# Changelog

## 1.0.1
- **Fix: command topics were never subscribed on first connect.** `_on_connect` iterated
  the entity registry to subscribe to command topics, but the very first MQTT connect
  happens *before* `initialize()`/`build_discovery()` populates that registry — so the
  subscribe loop ran over an empty list and no command (buttons, DOD, BLE lock, LED,
  energy mode select) ever reached the bridge until an unrelated MQTT reconnect happened to
  occur afterwards. Fixed by explicitly calling the (new) `subscribe_all_commands()` right
  after discovery is published in `initialize()`, in addition to keeping it in `_on_connect`
  for actual reconnects.
- Command feedback: `dod`, `ble_block`, `led_ctrl`, `energy_mode` now publish an explicit
  `*_feedback` diagnostic sensor (`OK` / `FAILED: <reason>`) after every write, instead of
  silently doing nothing on a failed `set_result` or a UDP timeout.
- Manual refresh buttons: any endpoint whose `poll_interval_*` is set to `0` now gets an MQTT
  `button` entity to trigger an immediate one-off poll from Home Assistant.
- Per-endpoint instance IDs (experimental): new `use_per_endpoint_instance_ids` toggle plus
  `instance_id_<endpoint>` options, to test whether the `id` field inside `params` should
  differ per call type instead of always being `0`. Default keeps prior behavior.

## 1.0.0
- Initial release: UDP<->MQTT bridge for Marstek Open API Rev 2.0.
- Init sequence with 5-attempt retry/backoff (2/7/12/17/22s).
- MQTT discovery across 6 device groups (System, Battery, PV, Energy Status, Energy Mode,
  Energy Control).
- Configurable poll intervals per endpoint.
- MQTT reconnect watchdog, colored logging.
