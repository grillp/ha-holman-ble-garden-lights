"""BLE protocol implementation for Holman Vibrance WW garden light controllers.

Protocol ported from danward79/holman-ESP32-ble-mqtt-gateway
(https://github.com/danward79/holman-ESP32-ble-mqtt-gateway, MIT licensed),
an ESPHome/MQTT bridge for the same hardware - see the integration README's
Acknowledgments section. The controller expects:
  1. A 20-byte all-zero "authorization" write to unlock the GATT link.
  2. A 20-byte "manual override" command write with the desired state.

Both writes go to the same characteristic (WRITE_CHARACTERISTIC_UUID).

NOTE: this mirrors the *actual bytes the working firmware sends*, which
diverge from that project's own README in the timer-related fields (see
integration README for details). Bytes 7-8 (0x17, 0x3B) are reproduced
verbatim as unexplained-but-confirmed-working constants.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import (
    POST_AUTH_DELAY,
    POST_COMMAND_DELAY,
    POST_CONNECT_DELAY,
    WRITE_CHARACTERISTIC_UUID,
)

_LOGGER = logging.getLogger(__name__)

AUTH_PAYLOAD = bytes(20)

# Seam for tests: a callable with establish_connection()'s signature. Real
# callers use bleak_retry_connector.establish_connection (the default);
# tests can inject a fake that raises or returns a stub client without
# touching real BLE.
Connector = Callable[..., Awaitable[Any]]


def build_command(level_pct: int) -> bytes:
    """Build the 20-byte manual-override payload for a given brightness (0-100).

    level_pct <= 0 sends the dedicated OFF command (scene byte = 0xFF).
    """
    if level_pct <= 0:
        return bytes([0x00, 0x2E, 0xFF] + [0x00] * 17)

    brightness_byte = max(1, min(255, round((level_pct / 100.0) * 255)))

    payload = bytearray(20)
    payload[0] = 0x00
    payload[1] = 0x2E
    # bytes 2-6 intentionally left 0x00: timer/scene/day-mask disabled
    payload[7] = 0x17
    payload[8] = 0x3B
    # byte 9 reserved (0x00)
    payload[10] = 0xFF  # start of static padding block, matches OFF frame shape
    # bytes 11-15 remain 0x00
    payload[16] = 0x64  # fixed scale (100)
    # byte 17 remains 0x00
    payload[18] = brightness_byte
    # byte 19 tail remains 0x00
    return bytes(payload)


class HolmanBLEError(Exception):
    """Raised when a command could not be delivered to the controller."""


class HolmanBLEDevice:
    """Wraps a single Holman BLE controller and serializes writes to it."""

    def __init__(
        self, ble_device: BLEDevice, *, connector: Connector = establish_connection
    ) -> None:
        self._ble_device = ble_device
        self._lock = asyncio.Lock()
        # Injectable so tests can exercise retry/timeout behavior without
        # real Bluetooth. Defaults to the real bleak_retry_connector.
        self._connector = connector

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the underlying BLEDevice (e.g. after a fresh advertisement)."""
        self._ble_device = ble_device

    async def async_set_state(self, *, on: bool, brightness_pct: int) -> None:
        """Connect, authorize, and push a state command to the controller."""
        level = brightness_pct if on else 0
        command = build_command(level)

        async with self._lock:
            client = None
            try:
                # Connection failures must become HolmanBLEError too, otherwise
                # the caller's retry loop never sees them.
                #
                # max_attempts=1: our own caller (light.py's _async_send)
                # already retries MAX_ATTEMPTS times with its own backoff.
                # establish_connection() retries internally too (by default
                # up to 4 times for connect/timeout errors and up to 9 for
                # "transient" errors), each attempt using its own hardcoded
                # ~20-60s timeouts regardless of any timeout= kwarg we pass.
                # Left nested, those two retry loops multiply out to the
                # 300+s startup hang this was fixed for; max_attempts=1
                # here means only our own outer loop retries.
                client = await self._connector(
                    BleakClientWithServiceCache,
                    self._ble_device,
                    self._ble_device.address,
                    max_attempts=1,
                )
                await asyncio.sleep(POST_CONNECT_DELAY)

                _LOGGER.debug("Sending authorization payload")
                await client.write_gatt_char(
                    WRITE_CHARACTERISTIC_UUID, AUTH_PAYLOAD, response=True
                )
                await asyncio.sleep(POST_AUTH_DELAY)

                _LOGGER.debug("Sending state command: %s", command.hex())
                await client.write_gatt_char(
                    WRITE_CHARACTERISTIC_UUID, command, response=True
                )
                await asyncio.sleep(POST_COMMAND_DELAY)
            except Exception as err:  # noqa: BLE001
                raise HolmanBLEError(str(err)) from err
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception as err:  # noqa: BLE001
                        # A failed disconnect must not mask the real result.
                        _LOGGER.debug("Disconnect raised: %s", err)
