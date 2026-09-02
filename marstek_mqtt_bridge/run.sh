#!/usr/bin/env bash
set -e

echo "[run.sh] Starting Marstek MQTT Bridge..."

# /data/options.json is provided automatically by the Home Assistant Supervisor
# based on config.yaml's schema/options. bridge.py reads it directly, falling
# back to /app/config.yaml's "options" block for standalone/local testing.
export OPTIONS_FILE="${OPTIONS_FILE:-/data/options.json}"

exec python3 /app/bridge.py
