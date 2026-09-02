#!/usr/bin/env python3
"""Marstek UDP <-> MQTT bridge for Home Assistant.

See README.md in this folder for the full design write-up, entity list, and a
list of assumptions made where the original spec was ambiguous or incomplete.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from typing import Optional

import paho.mqtt.client as mqtt

from entities import DeviceGroup, EntityRegistry, slugify
from logger_setup import setup_logging
from udp_client import MarstekUDPClient, MarstekUDPError

CONFIG_YAML_FALLBACK = os.path.join(os.path.dirname(__file__), "config.yaml")


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------

def load_config() -> dict:
    """Load HA Supervisor options.json, falling back to config.yaml's
    "options" block for local/standalone testing outside HA."""
    options_file = os.environ.get("OPTIONS_FILE", "/data/options.json")

    if os.path.isfile(options_file):
        with open(options_file, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # Fallback: parse the "options:" block out of config.yaml without requiring
    # PyYAML as a dependency (keeps the container image small).
    if os.path.isfile(CONFIG_YAML_FALLBACK):
        import yaml  # local, optional import
        with open(CONFIG_YAML_FALLBACK, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        return doc.get("options", {})

    raise FileNotFoundError(
        f"No options file found at {options_file} and no local config.yaml fallback available."
    )


# --------------------------------------------------------------------------
# Bridge
# --------------------------------------------------------------------------

class MarstekBridge:
    def __init__(self, cfg: dict, logger):
        self.cfg = cfg
        self.log = logger
        self.udp = MarstekUDPClient(cfg["device_ip"], int(cfg["device_udp_port"]))

        # Identity is only fully known after init; start with something usable.
        seed = cfg.get("device_ble_mac") or cfg["device_ip"]
        self.base_id = slugify(f"marstek_{seed}")

        self.reg = EntityRegistry(cfg, self.base_id)
        self.groups: dict[str, DeviceGroup] = {}

        self.mqttc = mqtt.Client(client_id=f"{self.base_id}_bridge", clean_session=True)
        if cfg.get("mqtt_username"):
            self.mqttc.username_pw_set(cfg["mqtt_username"], cfg.get("mqtt_password") or None)
        self.mqttc.on_connect = self._on_connect
        self.mqttc.on_disconnect = self._on_disconnect
        self.mqttc.on_message = self._on_message
        # Built-in "watchdog": automatic, backing-off reconnect handled by paho itself.
        self.mqttc.reconnect_delay_set(min_delay=1, max_delay=30)
        self.mqttc.will_set(self.reg.availability_topic(), payload="offline", retain=True)

        self._connected = threading.Event()
        self._udp_lock_state = threading.Lock()
        self._comm_ok: Optional[bool] = None  # None = unknown yet
        self._stop = threading.Event()

        # last known ES.SetMode helper values, used to assemble Passive commands
        self._passive_power = 100
        self._passive_cd_time = 300

    # ---------------------------------------------------------------- MQTT

    def connect_mqtt(self):
        self.log.info("Connecting to MQTT broker %s:%s ...", self.cfg["mqtt_host"], self.cfg["mqtt_port"])
        self.mqttc.connect(self.cfg["mqtt_host"], int(self.cfg["mqtt_port"]), keepalive=30)
        self.mqttc.loop_start()
        if not self._connected.wait(timeout=15):
            self.log.warning("MQTT connect is taking a while, continuing to retry in background...")

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.log.info("MQTT connected.")
            self._connected.set()
            client.publish(self.reg.availability_topic(), payload="online", retain=True)
            self.subscribe_all_commands()
        else:
            self.log.error("MQTT connect failed, rc=%s", rc)
            self._connected.clear()

    def subscribe_all_commands(self):
        """(Re-)subscribe to every commandable entity's command topic. Called
        from _on_connect (covers reconnects) AND explicitly right after
        discovery is (re-)built in initialize() - the very first connect
        happens before any entities exist yet, so on_connect alone is not
        enough to catch the initial subscription."""
        count = 0
        for entry in self.reg.all_entities():
            if entry["commandable"]:
                topic = self.reg.command_topic(entry["object_id"])
                self.mqttc.subscribe(topic)
                self.log.debug("Subscribed to %s", topic)
                count += 1
        if count:
            self.log.info("Subscribed to %d command topic(s).", count)

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear()
        if rc != 0:
            self.log.warning("MQTT disconnected unexpectedly (rc=%s); paho will auto-reconnect.", rc)
        else:
            self.log.info("MQTT disconnected.")

    def mqtt_watchdog_loop(self):
        """Belt-and-braces watchdog on top of paho's own reconnect: logs and
        nudges reconnect if the client thinks it's disconnected for too long."""
        while not self._stop.is_set():
            time.sleep(10)
            if not self.mqttc.is_connected():
                self.log.warning("Watchdog: MQTT still not connected, forcing reconnect() ...")
                try:
                    self.mqttc.reconnect()
                except Exception as exc:  # noqa: BLE001
                    self.log.error("Watchdog reconnect failed: %s", exc)

    def publish_discovery_all(self):
        for entry in self.reg.all_entities():
            topic = self.reg.discovery_topic(entry["component"], entry["object_id"])
            self.mqttc.publish(topic, json.dumps(entry["payload"]), retain=True)
        self.log.info("Published discovery config for %d entities.", len(list(self.reg.all_entities())))

    def publish_state(self, object_id: str, value):
        if isinstance(value, bool):
            value = "ON" if value else "OFF"
        self.mqttc.publish(self.reg.state_topic(object_id), str(value), retain=False)

    def _feedback(self, base_object_id: str, success: bool, detail: str = ""):
        """Publish an explicit success/failure result for a command entity so
        HA never has to guess whether a write actually took effect."""
        text = "OK" if success else f"FAILED: {detail}" if detail else "FAILED"
        self.publish_state(f"{base_object_id}_feedback", text)

    def _iid(self, key: str) -> int:
        """Resolve the params 'id' (instance id) to use for a given endpoint.
        See README "Assumptions" #3 - defaults to 0 (shared) unless the
        per-endpoint experiment is switched on in the add-on options."""
        if not self.cfg.get("use_per_endpoint_instance_ids"):
            return 0
        return int(self.cfg.get(f"instance_id_{key}", 0))

    # ------------------------------------------------------------- Commands

    def _on_message(self, client, userdata, msg):
        object_id = None
        m = re.match(rf"^{re.escape(self.reg.base_topic)}/(.+)/set$", msg.topic)
        if m:
            object_id = m.group(1)
        payload = msg.payload.decode("utf-8", errors="replace")
        self.log.info("Command received: %s = %s", object_id, payload)
        try:
            self._handle_command(object_id, payload)
        except MarstekUDPError as exc:
            self.log.error("Command %s failed: %s", object_id, exc)
            self._set_comm_status(False)
        except Exception as exc:  # noqa: BLE001
            self.log.exception("Unhandled error processing command %s: %s", object_id, exc)

    def _handle_command(self, object_id: str, payload: str):
        if object_id == "dod":
            value = int(float(payload))
            try:
                result = self.udp.dod_set(value)
            except MarstekUDPError as exc:
                self._feedback("dod", False, str(exc))
                self._set_comm_status(False)
                return
            ok = bool(result.get("set_result"))
            if ok:
                self.publish_state("dod", value)
            self._feedback("dod", ok, "" if ok else "device rejected value")
            self._set_comm_status(True)

        elif object_id == "ble_block":
            enable = 1 if payload.upper() == "ON" else 0
            try:
                result = self.udp.ble_adv_set(enable)
            except MarstekUDPError as exc:
                self._feedback("ble_block", False, str(exc))
                self._set_comm_status(False)
                return
            ok = bool(result.get("set_result"))
            if ok:
                self.publish_state("ble_block", payload.upper())
            self._feedback("ble_block", ok, "" if ok else "device rejected value")
            self._set_comm_status(True)

        elif object_id == "led_ctrl":
            state = 1 if payload.upper() == "ON" else 0
            try:
                result = self.udp.led_ctrl_set(state)
            except MarstekUDPError as exc:
                self._feedback("led_ctrl", False, str(exc))
                self._set_comm_status(False)
                return
            ok = bool(result.get("set_result"))
            if ok:
                self.publish_state("led_ctrl", payload.upper())
            self._feedback("led_ctrl", ok, "" if ok else "device rejected value")
            self._set_comm_status(True)

        elif object_id == "energy_mode":
            mode = payload.strip()
            try:
                config = self._build_set_mode_config(mode)
                result = self.udp.es_set_mode(config)
            except MarstekUDPError as exc:
                self._feedback("energy_mode", False, str(exc))
                self._set_comm_status(False)
                return
            except ValueError as exc:
                self._feedback("energy_mode", False, str(exc))
                return
            ok = bool(result.get("set_result"))
            if ok:
                self.publish_state("energy_mode", mode)
            self._feedback("energy_mode", ok, "" if ok else "device rejected mode")
            self._set_comm_status(True)

        elif object_id == "energy_mode_passive_power":
            self._passive_power = int(float(payload))
            self.publish_state("energy_mode_passive_power", self._passive_power)

        elif object_id == "energy_mode_passive_cd_time":
            self._passive_cd_time = int(float(payload))
            self.publish_state("energy_mode_passive_cd_time", self._passive_cd_time)

        elif object_id == "bat_status_refresh":
            self._safe_poll_bat()
        elif object_id == "es_status_refresh":
            self._safe_poll_es_status()
        elif object_id == "es_mode_refresh":
            self._safe_poll_es_mode()
        elif object_id == "pv_status_refresh":
            self._safe_poll_pv()
        elif object_id == "wifi_status_refresh":
            self._safe_poll_wifi()
        elif object_id == "ble_status_refresh":
            self._safe_poll_ble()
        elif object_id == "em_status_refresh":
            self._safe_poll_em()

        else:
            self.log.warning("Unknown command object_id: %s", object_id)

    def _build_set_mode_config(self, mode: str) -> dict:
        # NOTE: Manual mode intentionally omitted - see README "Assumptions".
        if mode == "Auto":
            return {"mode": "Auto", "auto_cfg": {"enable": 1}}
        if mode == "AI":
            return {"mode": "AI", "ai_cfg": {"enable": 1}}
        if mode == "UPS":
            return {"mode": "UPS", "ups_cfg": {"enable": 1}}
        if mode == "Passive":
            return {
                "mode": "Passive",
                "passive_cfg": {
                    "power": self._passive_power,
                    "cd_time": self._passive_cd_time,
                },
            }
        raise ValueError(f"Unsupported energy mode: {mode}")

    # ------------------------------------------------------------- Comm status

    def _set_comm_status(self, ok: bool):
        if ok == self._comm_ok:
            return
        self._comm_ok = ok
        self.publish_state("communication_established", ok)
        self.publish_state("communication_fail", not ok)
        if ok:
            self.log.info("Communication with device RESTORED / established.")
        else:
            self.log.error("Communication with device FAILED (device appears to hang).")

    # ------------------------------------------------------------- Discovery setup

    def build_discovery(self, device_info: dict):
        cfg = self.cfg
        model = device_info.get("device", cfg.get("device_type") or "Marstek Battery")

        self.groups["system"] = DeviceGroup(self.base_id, "system", "Marstek System", cfg, model=model)
        self.groups["battery"] = DeviceGroup(self.base_id, "battery", "Marstek Battery", cfg, model=model)
        self.groups["pv"] = DeviceGroup(self.base_id, "pv", "Marstek PV", cfg, model=model)
        self.groups["energy_status"] = DeviceGroup(self.base_id, "energy_status", "Marstek Energy Status", cfg, model=model)
        self.groups["energy_mode"] = DeviceGroup(self.base_id, "energy_mode", "Marstek Energy Mode", cfg, model=model)
        self.groups["energy_control"] = DeviceGroup(self.base_id, "energy_control", "Marstek Energy Control", cfg, model=model)

        sysg, batg, pvg = self.groups["system"], self.groups["battery"], self.groups["pv"]
        esg, emg, ecg = self.groups["energy_status"], self.groups["energy_mode"], self.groups["energy_control"]
        R = self.reg

        # --- Marstek System ---------------------------------------------
        R.register("sensor", "device_type", "Device Type", sysg, entity_category="diagnostic")
        R.register("sensor", "firmware_version", "Firmware Version", sysg, entity_category="diagnostic")
        R.register("sensor", "ble_mac", "BLE MAC", sysg, entity_category="diagnostic")
        R.register("sensor", "wifi_mac", "WiFi MAC", sysg, entity_category="diagnostic")
        R.register("sensor", "wifi_ssid", "WiFi SSID", sysg, entity_category="diagnostic")
        R.register("sensor", "wifi_rssi", "WiFi Signal", sysg, unit="dBm",
                    device_class="signal_strength", state_class="measurement", entity_category="diagnostic")
        R.register("sensor", "wifi_ip", "Device IP", sysg, entity_category="diagnostic")
        R.register("sensor", "ble_state", "Bluetooth State", sysg, entity_category="diagnostic")

        R.register("number", "dod", "Depth of Discharge Limit", sysg, unit="%",
                    min_value=30, max_value=88, step=1, icon="mdi:battery-arrow-down")
        R.register("switch", "ble_block", "Bluetooth Broadcast Lock", sysg,
                    entity_category="config", icon="mdi:bluetooth-off")
        R.register("switch", "led_ctrl", "Panel LED", sysg, entity_category="config", icon="mdi:led-outline")

        R.register("binary_sensor", "communication_established", "Communication Established", sysg,
                    device_class="connectivity")
        R.register("binary_sensor", "communication_fail", "Communication Fail", sysg,
                    device_class="problem")

        # Explicit command-result feedback (requested: don't silently swallow failures)
        R.register("sensor", "dod_feedback", "DOD Set Feedback", sysg,
                    entity_category="diagnostic", icon="mdi:message-alert-outline")
        R.register("sensor", "ble_block_feedback", "Bluetooth Lock Set Feedback", sysg,
                    entity_category="diagnostic", icon="mdi:message-alert-outline")
        R.register("sensor", "led_ctrl_feedback", "Panel LED Set Feedback", sysg,
                    entity_category="diagnostic", icon="mdi:message-alert-outline")

        # --- Marstek Battery ----------------------------------------------
        R.register("sensor", "bat_soc", "Battery SOC", batg, unit="%", device_class="battery",
                    state_class="measurement")
        R.register("binary_sensor", "bat_charg_flag", "Charging Allowed", batg, device_class="power")
        R.register("binary_sensor", "bat_dischrg_flag", "Discharging Allowed", batg, device_class="power")
        R.register("sensor", "bat_temp", "Battery Temperature", batg, unit="°C",
                    device_class="temperature", state_class="measurement")
        R.register("sensor", "bat_capacity", "Battery Remaining Capacity", batg, unit="Wh",
                    device_class="energy_storage", state_class="measurement")
        R.register("sensor", "bat_rated_capacity", "Battery Rated Capacity", batg, unit="Wh",
                    entity_category="diagnostic")

        # --- Marstek PV (Venus D/A only) -----------------------------------
        for i in (1, 2, 3, 4):
            R.register("sensor", f"pv{i}_power", f"PV{i} Power", pvg, unit="W",
                        device_class="power", state_class="measurement")
            R.register("sensor", f"pv{i}_voltage", f"PV{i} Voltage", pvg, unit="V",
                        device_class="voltage", state_class="measurement")
            R.register("sensor", f"pv{i}_current", f"PV{i} Current", pvg, unit="A",
                        device_class="current", state_class="measurement")
            R.register("sensor", f"pv{i}_state", f"PV{i} State", pvg)

        # --- Marstek Energy Status (ES.GetStatus) ---------------------------
        R.register("sensor", "es_bat_soc", "Total Battery SOC", esg, unit="%",
                    device_class="battery", state_class="measurement")
        R.register("sensor", "es_bat_cap", "Total Battery Capacity", esg, unit="Wh")
        R.register("sensor", "es_pv_power", "Solar Charging Power", esg, unit="W",
                    device_class="power", state_class="measurement")
        R.register("sensor", "es_ongrid_power", "Grid-Tied Power", esg, unit="W",
                    device_class="power", state_class="measurement")
        R.register("sensor", "es_offgrid_power", "Off-Grid Power", esg, unit="W",
                    device_class="power", state_class="measurement")
        R.register("sensor", "es_bat_power", "Battery Power", esg, unit="W",
                    device_class="power", state_class="measurement")
        R.register("sensor", "es_total_pv_energy", "Total Solar Energy", esg, unit="Wh",
                    device_class="energy", state_class="total_increasing")
        R.register("sensor", "es_total_grid_output_energy", "Total Grid Export Energy", esg, unit="Wh",
                    device_class="energy", state_class="total_increasing")
        R.register("sensor", "es_total_grid_input_energy", "Total Grid Import Energy", esg, unit="Wh",
                    device_class="energy", state_class="total_increasing")
        R.register("sensor", "es_total_load_energy", "Total Load Energy", esg, unit="Wh",
                    device_class="energy", state_class="total_increasing")

        # --- Marstek Energy Mode (ES.GetMode) -------------------------------
        R.register("sensor", "em_mode", "Active Mode", emg)
        R.register("sensor", "em_ongrid_power", "Grid-Tied Power", emg, unit="W", device_class="power")
        R.register("sensor", "em_offgrid_power", "Off-Grid Power", emg, unit="W", device_class="power")
        R.register("sensor", "em_bat_soc", "Battery SOC", emg, unit="%", device_class="battery")
        R.register("binary_sensor", "em_ct_state", "CT Connected", emg, device_class="connectivity")
        R.register("sensor", "em_a_power", "Phase A Power", emg, unit="W", device_class="power")
        R.register("sensor", "em_b_power", "Phase B Power", emg, unit="W", device_class="power")
        R.register("sensor", "em_c_power", "Phase C Power", emg, unit="W", device_class="power")
        R.register("sensor", "em_total_power", "CT Total Power", emg, unit="W", device_class="power")
        R.register("sensor", "em_input_energy", "CT Cumulative Input Energy", emg, unit="Wh",
                    device_class="energy", state_class="total_increasing")
        R.register("sensor", "em_output_energy", "CT Cumulative Output Energy", emg, unit="Wh",
                    device_class="energy", state_class="total_increasing")

        # --- Marstek Energy Control (ES.SetMode) ----------------------------
        # NOTE: Manual mode intentionally excluded - see README "Assumptions".
        R.register("select", "energy_mode", "Energy Mode", ecg, options=["Auto", "AI", "UPS", "Passive"],
                    icon="mdi:tune-variant")
        R.register("number", "energy_mode_passive_power", "Passive Mode: Power", ecg, unit="W",
                    min_value=-2500, max_value=2500, step=1)
        R.register("number", "energy_mode_passive_cd_time", "Passive Mode: Countdown", ecg, unit="s",
                    min_value=0, max_value=86400, step=1)
        R.register("sensor", "energy_mode_feedback", "Energy Mode Set Feedback", ecg,
                    entity_category="diagnostic", icon="mdi:message-alert-outline")

        # --- Manual refresh buttons ------------------------------------------
        # Only exposed for endpoints whose poll interval is set to 0 (polling
        # disabled), so you can still refresh them on demand from HA.
        refresh_buttons = [
            ("bat_status_refresh", "Refresh Battery Status", batg, "poll_interval_bat_status"),
            ("es_status_refresh", "Refresh Energy Status", esg, "poll_interval_es_status"),
            ("es_mode_refresh", "Refresh Energy Mode", emg, "poll_interval_es_mode"),
            ("pv_status_refresh", "Refresh PV Status", pvg, "poll_interval_pv_status"),
            ("wifi_status_refresh", "Refresh WiFi Status", sysg, "poll_interval_wifi_status"),
            ("ble_status_refresh", "Refresh Bluetooth Status", sysg, "poll_interval_ble_status"),
            ("em_status_refresh", "Refresh Energy Meter Status", esg, "poll_interval_em_status"),
        ]
        for object_id, name, group, interval_key in refresh_buttons:
            if int(cfg.get(interval_key, 1)) == 0:
                R.register("button", object_id, name, group, icon="mdi:refresh")

    # ------------------------------------------------------------- Init sequence

    def initialize(self) -> bool:
        """Runs the mandatory identification sequence. Returns True if the
        device answered at least Marstek.GetDevice (identity is then known
        and discovery can be published). Individual optional calls that fail
        are logged and simply left at their default/unknown state."""
        cfg = self.cfg
        self.log.info("Starting device initialization sequence...")

        try:
            device = self.udp.get_device(cfg.get("device_ble_mac") or "0")
        except MarstekUDPError as exc:
            self.log.error("Marstek.GetDevice failed: %s", exc)
            self._set_comm_status(False)
            return False

        # Persist auto-discovered identity into the running config (not written
        # back to options.json automatically - see README for why).
        cfg.setdefault("device_ble_mac", device.get("ble_mac", ""))
        cfg.setdefault("device_type", device.get("device", ""))
        self.base_id = slugify(f"marstek_{device.get('ble_mac') or cfg['device_ip']}")
        self.reg = EntityRegistry(cfg, self.base_id)

        self.build_discovery(device)
        self.publish_discovery_all()
        self.subscribe_all_commands()
        self._set_comm_status(True)

        self.publish_state("device_type", device.get("device"))
        self.publish_state("firmware_version", device.get("ver"))
        self.publish_state("ble_mac", device.get("ble_mac"))
        self.publish_state("wifi_mac", device.get("wifi_mac"))
        self.publish_state("wifi_ip", device.get("ip"))

        # Optional calls: attempt, log + continue on failure
        self._safe_poll_wifi()
        self._safe_poll_bat()
        self._safe_poll_es_status()
        self._safe_poll_es_mode()
        self._safe_poll_ble()
        if cfg.get("device_type", "") in ("VenusD", "VenusA") or "pv" in cfg.get("device_type", "").lower():
            self._safe_poll_pv()

        # Apply init-only set commands
        try:
            self.udp.dod_set(int(cfg["dod_init_value"]))
            self.publish_state("dod", cfg["dod_init_value"])
        except MarstekUDPError as exc:
            self.log.warning("Init DOD.SET failed: %s", exc)

        try:
            enable = 1 if cfg.get("ble_block_init_enable") else 0
            self.udp.ble_adv_set(enable)
            self.publish_state("ble_block", "ON" if enable else "OFF")
        except MarstekUDPError as exc:
            self.log.warning("Init Ble.Adv failed: %s", exc)

        try:
            state = 1 if cfg.get("led_init_state") else 0
            self.udp.led_ctrl_set(state)
            self.publish_state("led_ctrl", "ON" if state else "OFF")
        except MarstekUDPError as exc:
            self.log.warning("Init Led.Ctrl failed: %s", exc)

        self.log.info("Initialization sequence complete.")
        return True

    # ------------------------------------------------------------- Poll helpers

    def _safe_poll_wifi(self):
        try:
            s = self.udp.wifi_get_status(self._iid("wifi"))
            self.publish_state("wifi_ssid", s.get("ssid"))
            self.publish_state("wifi_rssi", s.get("rssi"))
            self.publish_state("wifi_ip", s.get("sta_ip"))
            self._set_comm_status(True)
        except MarstekUDPError as exc:
            self.log.warning("Wifi.GetStatus failed: %s", exc)
            self._set_comm_status(False)

    def _safe_poll_ble(self):
        try:
            s = self.udp.ble_get_status(self._iid("ble"))
            self.publish_state("ble_state", s.get("state"))
            self._set_comm_status(True)
        except MarstekUDPError as exc:
            self.log.warning("BLE.GetStatus failed: %s", exc)
            self._set_comm_status(False)

    def _safe_poll_bat(self):
        try:
            s = self.udp.bat_get_status(self._iid("bat"))
            self.publish_state("bat_soc", s.get("soc"))
            self.publish_state("bat_charg_flag", s.get("charg_flag"))
            self.publish_state("bat_dischrg_flag", s.get("dischrg_flag"))
            self.publish_state("bat_temp", s.get("bat_temp"))
            self.publish_state("bat_capacity", s.get("bat_capacity"))
            self.publish_state("bat_rated_capacity", s.get("rated_capacity"))
            self._set_comm_status(True)
        except MarstekUDPError as exc:
            self.log.warning("Bat.GetStatus failed: %s", exc)
            self._set_comm_status(False)

    def _safe_poll_pv(self):
        try:
            s = self.udp.pv_get_status(self._iid("pv"))
            for i in (1, 2, 3, 4):
                self.publish_state(f"pv{i}_power", s.get(f"pv{i}_power"))
                self.publish_state(f"pv{i}_voltage", s.get(f"pv{i}_voltage"))
                self.publish_state(f"pv{i}_current", s.get(f"pv{i}_current"))
                self.publish_state(f"pv{i}_state", s.get(f"pv{i}_state"))
            self._set_comm_status(True)
        except MarstekUDPError as exc:
            self.log.warning("PV.GetStatus failed: %s", exc)
            self._set_comm_status(False)

    def _safe_poll_es_status(self):
        try:
            s = self.udp.es_get_status(self._iid("es_status"))
            self.publish_state("es_bat_soc", s.get("bat_soc"))
            self.publish_state("es_bat_cap", s.get("bat_cap"))
            self.publish_state("es_pv_power", s.get("pv_power"))
            self.publish_state("es_ongrid_power", s.get("ongrid_power"))
            self.publish_state("es_offgrid_power", s.get("offgrid_power"))
            self.publish_state("es_bat_power", s.get("bat_power"))
            self.publish_state("es_total_pv_energy", s.get("total_pv_energy"))
            self.publish_state("es_total_grid_output_energy", s.get("total_grid_output_energy"))
            self.publish_state("es_total_grid_input_energy", s.get("total_grid_input_energy"))
            self.publish_state("es_total_load_energy", s.get("total_load_energy"))
            self._set_comm_status(True)
        except MarstekUDPError as exc:
            self.log.warning("ES.GetStatus failed: %s", exc)
            self._set_comm_status(False)

    def _safe_poll_es_mode(self):
        try:
            s = self.udp.es_get_mode(self._iid("es_mode"))
            self.publish_state("em_mode", s.get("mode"))
            self.publish_state("energy_mode", s.get("mode"))  # keep select in sync
            self.publish_state("em_ongrid_power", s.get("ongrid_power"))
            self.publish_state("em_offgrid_power", s.get("offgrid_power"))
            self.publish_state("em_bat_soc", s.get("bat_soc"))
            self.publish_state("em_ct_state", bool(s.get("ct_state")))
            self.publish_state("em_a_power", s.get("a_power"))
            self.publish_state("em_b_power", s.get("b_power"))
            self.publish_state("em_c_power", s.get("c_power"))
            self.publish_state("em_total_power", s.get("total_power"))
            self.publish_state("em_input_energy", s.get("input_energy"))
            self.publish_state("em_output_energy", s.get("output_energy"))
            self._set_comm_status(True)
        except MarstekUDPError as exc:
            self.log.warning("ES.GetMode failed: %s", exc)
            self._set_comm_status(False)

    def _safe_poll_em(self):
        try:
            self.udp.em_get_status(self._iid("em"))
            self._set_comm_status(True)
        except MarstekUDPError as exc:
            self.log.warning("EM.GetStatus failed: %s", exc)
            self._set_comm_status(False)

    # ------------------------------------------------------------- Main loop

    def run(self):
        self.connect_mqtt()
        threading.Thread(target=self.mqtt_watchdog_loop, daemon=True).start()

        ok = self.initialize()
        while not ok and not self._stop.is_set():
            self.log.error("Initial identification failed; retrying in 60s...")
            time.sleep(60)
            ok = self.initialize()

        cfg = self.cfg
        pollers = {
            "bat": (self._safe_poll_bat, int(cfg.get("poll_interval_bat_status", 30))),
            "es_status": (self._safe_poll_es_status, int(cfg.get("poll_interval_es_status", 15))),
            "es_mode": (self._safe_poll_es_mode, int(cfg.get("poll_interval_es_mode", 30))),
            "pv": (self._safe_poll_pv, int(cfg.get("poll_interval_pv_status", 30))),
            "wifi": (self._safe_poll_wifi, int(cfg.get("poll_interval_wifi_status", 60))),
            "ble": (self._safe_poll_ble, int(cfg.get("poll_interval_ble_status", 60))),
            "em": (self._safe_poll_em, int(cfg.get("poll_interval_em_status", 30))),
        }
        next_due = {name: 0.0 for name in pollers}

        self.log.info("Entering poll loop.")
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                for name, (fn, interval) in pollers.items():
                    if interval <= 0:
                        continue
                    if now >= next_due[name]:
                        fn()
                        next_due[name] = now + interval
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self):
        self._stop.set()
        try:
            self.mqttc.publish(self.reg.availability_topic(), payload="offline", retain=True)
            self.mqttc.loop_stop()
            self.mqttc.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.udp.close()


def main():
    cfg = load_config()
    logger = setup_logging(cfg.get("log_level", "info"))
    logger.info("Marstek MQTT Bridge starting (device %s:%s)", cfg["device_ip"], cfg["device_udp_port"])
    bridge = MarstekBridge(cfg, logger)
    bridge.run()


if __name__ == "__main__":
    main()
