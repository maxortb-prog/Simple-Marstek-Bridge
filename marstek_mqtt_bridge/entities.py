"""HA MQTT-discovery entity definitions, grouped into device groups as requested:

    Marstek System           -> Marstek.GetDevice, Wifi.GetStatus, BLE.GetStatus,
                                 DOD (number), Ble_block (switch), Led_Ctrl (switch),
                                 Communication status (binary_sensor)
    Marstek Battery          -> Bat.GetStatus
    Marstek PV               -> PV.GetStatus (Venus D/A only)
    Marstek Energy Status    -> ES.GetStatus
    Marstek Energy Mode      -> ES.GetMode
    Marstek Energy Control   -> ES.SetMode (select + number helper entities)

Each "group" is published as its own MQTT-discovery `device` block so it shows up as a
separate device card in Home Assistant, all tagged with the same suggested_area so they
sit together. All devices share `via_device` pointing at the System device.
"""

from __future__ import annotations

from typing import Any, Optional


def slugify(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value).strip("_").lower()


class DeviceGroup:
    """One HA "device" (a card in Settings -> Devices) for discovery purposes."""

    def __init__(self, base_id: str, suffix: str, name: str, cfg: dict,
                 model: Optional[str] = None, via_system: bool = True):
        self.id = f"{base_id}_{suffix}"
        self.name = name
        self.cfg = cfg
        self.model = model or "Marstek Battery"
        self.via_system = via_system and suffix != "system"

    def ha_device_block(self, base_id: str) -> dict:
        block = {
            "identifiers": [self.id],
            "name": self.name,
            "manufacturer": "Marstek",
            "model": self.model,
            "suggested_area": self.cfg["mqtt_suggested_area"],
        }
        if self.via_system:
            block["via_device"] = f"{base_id}_system"
        return block


class EntityRegistry:
    """Builds discovery topics/payloads and tracks state/command topics by key."""

    def __init__(self, cfg: dict, base_id: str):
        self.cfg = cfg
        self.base_id = base_id
        self.discovery_prefix = cfg["mqtt_discovery_prefix"].rstrip("/")
        self.base_topic = cfg["mqtt_base_topic"].strip("/")
        self._entities: dict[str, dict] = {}

    # -- topic helpers -------------------------------------------------

    def state_topic(self, object_id: str) -> str:
        return f"{self.base_topic}/{object_id}/state"

    def command_topic(self, object_id: str) -> str:
        return f"{self.base_topic}/{object_id}/set"

    def availability_topic(self) -> str:
        return f"{self.base_topic}/bridge/status"

    def discovery_topic(self, component: str, object_id: str) -> str:
        return f"{self.discovery_prefix}/{component}/{self.base_id}/{object_id}/config"

    # -- registration ----------------------------------------------------

    def register(
        self,
        component: str,
        object_id: str,
        name: str,
        device: DeviceGroup,
        *,
        device_class: Optional[str] = None,
        unit: Optional[str] = None,
        state_class: Optional[str] = None,
        options: Optional[list[str]] = None,
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        step: Optional[float] = None,
        entity_category: Optional[str] = None,
        icon: Optional[str] = None,
        commandable: bool = False,
        payload_on: Any = "ON",
        payload_off: Any = "OFF",
    ) -> dict:
        """Register + return the discovery payload for one entity."""
        uid = f"{self.base_id}_{object_id}"
        payload: dict[str, Any] = {
            "name": name,
            "unique_id": uid,
            "object_id": uid,
            "state_topic": self.state_topic(object_id),
            "availability_topic": self.availability_topic(),
            "device": device.ha_device_block(self.base_id),
        }
        if device_class:
            payload["device_class"] = device_class
        if unit:
            payload["unit_of_measurement"] = unit
        if state_class:
            payload["state_class"] = state_class
        if entity_category:
            payload["entity_category"] = entity_category
        if icon:
            payload["icon"] = icon

        if component == "binary_sensor":
            payload["payload_on"] = payload_on
            payload["payload_off"] = payload_off

        if component == "switch":
            payload["command_topic"] = self.command_topic(object_id)
            payload["payload_on"] = payload_on
            payload["payload_off"] = payload_off
            payload["state_on"] = payload_on
            payload["state_off"] = payload_off

        if component == "select":
            payload["command_topic"] = self.command_topic(object_id)
            payload["options"] = options or []

        if component == "number":
            payload["command_topic"] = self.command_topic(object_id)
            if min_value is not None:
                payload["min"] = min_value
            if max_value is not None:
                payload["max"] = max_value
            if step is not None:
                payload["step"] = step

        entry = {
            "component": component,
            "object_id": object_id,
            "payload": payload,
            "commandable": commandable or component in ("switch", "select", "number"),
        }
        self._entities[object_id] = entry
        return entry

    def all_entities(self):
        return self._entities.values()
