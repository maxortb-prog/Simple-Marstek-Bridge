# Changelog

## 1.0.0
- Initial release: UDP<->MQTT bridge for Marstek Open API Rev 2.0.
- Init sequence with 5-attempt retry/backoff (2/7/12/17/22s).
- MQTT discovery across 6 device groups (System, Battery, PV, Energy Status, Energy Mode,
  Energy Control).
- Configurable poll intervals per endpoint.
- MQTT reconnect watchdog, colored logging.
