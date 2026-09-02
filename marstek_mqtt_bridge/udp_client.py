"""UDP JSON-RPC client for the Marstek "Open API" (Rev 2.0).

Retry policy (as specified):
    - Up to 5 attempts total.
    - First attempt timeout: 2s.
    - Each subsequent attempt's timeout is increased by +5s over the previous
      one (2, 7, 12, 17, 22).
    - If all 5 attempts time out, the call is considered "hung" and the
      caller is responsible for reflecting a Communication Fail state.

A single UDP socket is reused and all sends are serialized with a lock,
since both the poll loop and MQTT command callbacks can trigger sends.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from itertools import count
from typing import Any, Optional

logger = logging.getLogger("marstek.udp")

RETRY_TIMEOUTS = [2, 7, 12, 17, 22]  # seconds, one per attempt


class MarstekUDPError(Exception):
    """Raised when a device call exhausts all retries without a response."""


class MarstekUDPClient:
    def __init__(self, ip: str, port: int, local_port: int = 0):
        self.ip = ip
        self.port = port
        self._id_counter = count(1)
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", local_port))

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _next_id(self) -> int:
        return next(self._id_counter)

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        """Send a JSON-RPC request and return the "result" dict.

        Raises MarstekUDPError if all retry attempts are exhausted, or if the
        device returns a JSON-RPC "error" object.
        """
        req_id = self._next_id()
        payload = {
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        raw = json.dumps(payload).encode("utf-8")

        with self._lock:
            for attempt, timeout in enumerate(RETRY_TIMEOUTS, start=1):
                self._sock.settimeout(timeout)
                try:
                    self._sock.sendto(raw, (self.ip, self.port))
                    logger.debug(
                        "-> %s (id=%s, attempt %d/%d, timeout=%ds) params=%s",
                        method, req_id, attempt, len(RETRY_TIMEOUTS), timeout, params,
                    )
                    data, _addr = self._sock.recvfrom(65535)
                except socket.timeout:
                    logger.warning(
                        "No response for %s (id=%s) on attempt %d/%d (timeout=%ds)",
                        method, req_id, attempt, len(RETRY_TIMEOUTS), timeout,
                    )
                    continue

                try:
                    response = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    logger.error("Malformed response for %s: %s", method, exc)
                    continue

                if response.get("id") != req_id:
                    # Stray/late packet from a previous timed-out attempt; ignore and
                    # keep waiting within the same attempt's timeout budget once.
                    logger.debug("Ignoring response with mismatched id: %s", response)
                    continue

                if "error" in response:
                    err = response["error"]
                    logger.error("%s returned error: %s", method, err)
                    raise MarstekUDPError(f"{method} error: {err}")

                logger.debug("<- %s (id=%s) result=%s", method, req_id, response.get("result"))
                return response.get("result", {})

        # All attempts exhausted
        raise MarstekUDPError(
            f"{method} timed out after {len(RETRY_TIMEOUTS)} attempts "
            f"({sum(RETRY_TIMEOUTS)}s total) - device appears unreachable"
        )

    # --- Convenience wrappers for every documented method -----------------

    def get_device(self, ble_mac: str = "0") -> dict:
        return self.call("Marstek.GetDevice", {"ble_mac": ble_mac})

    def wifi_get_status(self, instance_id: int = 0) -> dict:
        return self.call("Wifi.GetStatus", {"id": instance_id})

    def ble_get_status(self, instance_id: int = 0) -> dict:
        return self.call("BLE.GetStatus", {"id": instance_id})

    def bat_get_status(self, instance_id: int = 0) -> dict:
        return self.call("Bat.GetStatus", {"id": instance_id})

    def pv_get_status(self, instance_id: int = 0) -> dict:
        return self.call("PV.GetStatus", {"id": instance_id})

    def es_get_status(self, instance_id: int = 0) -> dict:
        return self.call("ES.GetStatus", {"id": instance_id})

    def es_get_mode(self, instance_id: int = 0) -> dict:
        return self.call("ES.GetMode", {"id": instance_id})

    def em_get_status(self, instance_id: int = 0) -> dict:
        return self.call("EM.GetStatus", {"id": instance_id})

    def es_set_mode(self, config: dict, instance_id: int = 0) -> dict:
        return self.call("ES.SetMode", {"id": instance_id, "config": config})

    def dod_set(self, value: int) -> dict:
        return self.call("DOD.SET", {"value": value})

    def ble_adv_set(self, enable: int) -> dict:
        return self.call("Ble.Adv", {"enable": enable})

    def led_ctrl_set(self, state: int) -> dict:
        return self.call("Led.Ctrl", {"state": state})
