# Marstek Add-ons Repository

This repository contains the **Marstek MQTT Bridge** Home Assistant add-on, which bridges a
Marstek battery device (Venus A/C/D/E, tested against Venus A) to MQTT via the device's local
UDP JSON-RPC "Open API" (Rev 2.0).

## Installation

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Add this repository's GitHub URL.
3. Install **Marstek MQTT Bridge** from the store.
4. Configure the add-on options (see `marstek_mqtt_bridge/README.md`) and start it.

See [marstek_mqtt_bridge/README.md](./marstek_mqtt_bridge/README.md) for full documentation,
entity list, and a list of design decisions/assumptions I made where your spec was ambiguous or
incomplete — please review those before relying on this in production.
