"""Unit tests for build_command() and HolmanBLEDevice.

HolmanBLEDevice's `connector` seam lets us exercise the connect/auth/write/
disconnect sequence and its error handling without any real Bluetooth
hardware or bleak backend.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.holman_garden_lights.holman_ble import (
    AUTH_PAYLOAD,
    HolmanBLEDevice,
    HolmanBLEError,
    build_command,
)
from custom_components.holman_garden_lights.const import WRITE_CHARACTERISTIC_UUID


# --------------------------------------------------------------------- #
# build_command()
# --------------------------------------------------------------------- #


def test_build_command_off():
    """Also matches SYNOPSIS.md's documented OFF frame verbatim:
    OFF: 00 2e ff 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
    """
    expected = bytes.fromhex("002eff0000000000000000000000000000000000")
    assert build_command(0) == bytes([0x00, 0x2E, 0xFF] + [0x00] * 17) == expected


def test_build_command_negative_is_also_off():
    assert build_command(-5) == build_command(0)


@pytest.mark.parametrize(
    ("pct", "expected_brightness_byte"),
    [
        (1, round(1 / 100 * 255)),
        (50, round(50 / 100 * 255)),
        (80, round(80 / 100 * 255)),
        (100, 255),
    ],
)
def test_build_command_on_brightness_byte(pct, expected_brightness_byte):
    command = build_command(pct)
    assert len(command) == 20
    assert command[0] == 0x00
    assert command[1] == 0x2E
    assert command[7] == 0x17
    assert command[8] == 0x3B
    assert command[16] == 0x64
    assert command[18] == expected_brightness_byte


def test_build_command_on_80_percent_matches_known_good_frame():
    """Regression check against the byte-for-byte frame documented in
    SYNOPSIS.md as verified against the working ESPHome firmware:
    ON, 80%: 00 2e 00 00 00 00 00 17 3b 00 ff 00 00 00 00 00 64 00 cc 00
    """
    expected = bytes.fromhex("002e0000000000173b00ff00000000006400cc00")
    assert build_command(80) == expected


def test_build_command_brightness_byte_is_clamped():
    assert build_command(1)[18] >= 1
    assert build_command(1000)[18] == 255


# --------------------------------------------------------------------- #
# HolmanBLEDevice
# --------------------------------------------------------------------- #


class _FakeClient:
    def __init__(self, writes: list, fail_disconnect: bool = False):
        self._writes = writes
        self._fail_disconnect = fail_disconnect

    async def write_gatt_char(self, uuid, data, response=True):
        self._writes.append((uuid, bytes(data)))

    async def disconnect(self):
        if self._fail_disconnect:
            raise RuntimeError("disconnect failed")


class _FakeBLEDevice:
    address = "AA:BB:CC:DD:EE:FF"


async def test_async_set_state_writes_auth_then_command_and_disconnects():
    writes: list = []
    client = _FakeClient(writes)

    async def connector(client_class, ble_device, name, **kwargs):
        return client

    device = HolmanBLEDevice(_FakeBLEDevice(), connector=connector)
    await device.async_set_state(on=True, brightness_pct=80)

    assert len(writes) == 2
    assert writes[0] == (WRITE_CHARACTERISTIC_UUID, AUTH_PAYLOAD)
    assert writes[1][0] == WRITE_CHARACTERISTIC_UUID
    assert writes[1][1] == build_command(80)


async def test_async_set_state_off_sends_off_frame():
    writes: list = []
    client = _FakeClient(writes)

    async def connector(client_class, ble_device, name, **kwargs):
        return client

    device = HolmanBLEDevice(_FakeBLEDevice(), connector=connector)
    await device.async_set_state(on=False, brightness_pct=0)

    assert writes[1][1] == build_command(0)


async def test_connector_failure_becomes_holman_ble_error():
    async def connector(client_class, ble_device, name, **kwargs):
        raise OSError("device unreachable")

    device = HolmanBLEDevice(_FakeBLEDevice(), connector=connector)

    with pytest.raises(HolmanBLEError):
        await device.async_set_state(on=True, brightness_pct=50)


async def test_write_failure_becomes_holman_ble_error_and_still_disconnects():
    writes: list = []
    disconnected = asyncio.Event()

    class _FailingWriteClient(_FakeClient):
        async def write_gatt_char(self, uuid, data, response=True):
            raise RuntimeError("gatt write failed")

        async def disconnect(self):
            disconnected.set()

    client = _FailingWriteClient(writes)

    async def connector(client_class, ble_device, name, **kwargs):
        return client

    device = HolmanBLEDevice(_FakeBLEDevice(), connector=connector)

    with pytest.raises(HolmanBLEError):
        await device.async_set_state(on=True, brightness_pct=50)

    assert disconnected.is_set()


async def test_disconnect_failure_does_not_mask_success():
    """A real send that succeeded must not be reported as failed just
    because the best-effort disconnect afterwards raised."""
    writes: list = []
    client = _FakeClient(writes, fail_disconnect=True)

    async def connector(client_class, ble_device, name, **kwargs):
        return client

    device = HolmanBLEDevice(_FakeBLEDevice(), connector=connector)
    # Should not raise, despite disconnect() failing internally.
    await device.async_set_state(on=True, brightness_pct=50)
    assert len(writes) == 2


async def test_connector_called_with_max_attempts_one():
    """establish_connection's own internal retries must be disabled (our
    caller already retries), otherwise the two retry loops nest and
    multiply delays - see const.py's OPERATION_TIMEOUT comment and the
    startup-hang this was fixed for."""
    seen_kwargs = {}

    async def connector(client_class, ble_device, name, **kwargs):
        seen_kwargs.update(kwargs)
        return _FakeClient([])

    device = HolmanBLEDevice(_FakeBLEDevice(), connector=connector)
    await device.async_set_state(on=True, brightness_pct=50)

    assert seen_kwargs.get("max_attempts") == 1
