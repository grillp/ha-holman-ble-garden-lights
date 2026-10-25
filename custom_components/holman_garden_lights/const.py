"""Constants for the Holman Garden Lights integration."""
from __future__ import annotations

DOMAIN = "holman_garden_lights"

# GATT UUIDs (from Holman Vibrance WW BLE controller protocol)
SERVICE_UUID = "a876f000-7f10-4d70-b606-7df77c3eee0c"
WRITE_CHARACTERISTIC_UUID = "0000f004-0000-1000-8000-00805f9b34fb"
NOTIFY_CHARACTERISTIC_UUID = "a876f003-7f10-4d70-b606-7df77c3eee0c"

CONF_ADDRESS = "address"

DEFAULT_NAME = "Holman Garden Lights"
MANUFACTURER = "Holman"
MODEL = "Vibrance WW Controller"

# Connection tuning (mirrors delays used by the working ESPHome firmware —
# the controller appears to need settling time between writes)
#
# NOTE on CONNECT_TIMEOUT: bleak_retry_connector.establish_connection() does
# NOT use this value as the timeout for its own connect attempts. The kwarg
# is only forwarded into the underlying BleakClient constructor; the actual
# `client.connect()` call inside establish_connection() is hardcoded to a
# 20s timeout per attempt (plus a 60s outer safety timeout), and by default
# it retries internally up to 4 times for connect/timeout errors and up to
# 9 times for "transient" errors (e.g. device-not-found / out-of-slots,
# which is exactly what we get while BLE routing is broken per
# SYNOPSIS.md). That nested retrying, run 3x by our own MAX_ATTEMPTS loop
# below, is what turns an unreachable device into a multi-minute hang.
# We pass max_attempts=1 to establish_connection() (see holman_ble.py) so
# only our own outer retry loop retries, and we additionally bound each
# whole attempt with OPERATION_TIMEOUT so a single hung BLE call can never
# run away regardless of what bleak/bleak_retry_connector do internally.
CONNECT_TIMEOUT = 10.0
OPERATION_TIMEOUT = 15.0
POST_CONNECT_DELAY = 0.3
POST_AUTH_DELAY = 0.4
POST_COMMAND_DELAY = 0.4
MAX_ATTEMPTS = 3
RETRY_BACKOFF = 2.0

# Minimum quiet period after a command completes before the next one is sent.
# The original ESPHome firmware used 1200ms; the controller's BLE stack can
# wedge (stop advertising until power-cycled) if hit with rapid reconnects.
COOLDOWN = 1.2
