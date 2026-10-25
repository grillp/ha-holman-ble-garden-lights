"""Tests for the HolmanLight entity's worker-task wiring and timeout bounds.

These include the regression tests that would have caught the startup-hang
bug (observed as a 317s delay on a real HA instance): the worker task must
be created via ConfigEntry.async_create_background_task(), never via
hass.async_create_task() - the latter is tracked by HA's bootstrap and
awaited by hass.async_block_till_done(); the former is explicitly exempt
("Will not block startup", "Calls to async_block_till_done will not wait
for completion" - see homeassistant/core.py's own docstring).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.holman_garden_lights.const import CONF_ADDRESS, DOMAIN
from custom_components.holman_garden_lights.light import HolmanLight

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _make_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDRESS})
    entry.add_to_hass(hass)
    return entry


async def test_worker_task_uses_config_entry_background_task_api(hass):
    """The regression test for the startup-hang bug itself: assert the
    worker task is created through the config-entry background-task API
    and never through hass.async_create_task()."""
    entry = _make_entry(hass)
    light = HolmanLight(hass, entry, ADDRESS)
    light.async_write_ha_state = MagicMock()

    background_calls: list = []
    bootstrap_blocking_calls: list = []

    real_background = entry.async_create_background_task
    real_blocking = hass.async_create_task

    def tracked_background(*args, **kwargs):
        background_calls.append((args, kwargs))
        return real_background(*args, **kwargs)

    def tracked_blocking(*args, **kwargs):
        bootstrap_blocking_calls.append((args, kwargs))
        return real_blocking(*args, **kwargs)

    entry.async_create_background_task = tracked_background
    hass.async_create_task = tracked_blocking

    async def instant_send(on: bool, pct: int) -> None:
        return None

    # DebouncedCommandWorker captured the bound _async_send at construction
    # time, so patch its seam directly rather than light._async_send.
    light._command_worker._send = instant_send  # type: ignore[assignment]
    await light._async_request(on=True, pct=50)

    assert len(background_calls) == 1
    assert not bootstrap_blocking_calls, (
        "Worker task must never be created via hass.async_create_task(); "
        "that API is what HA's bootstrap waits on and is exactly what "
        "caused the 317s startup hang."
    )


async def test_hung_ble_operation_does_not_block_async_block_till_done(hass):
    """Simulate the real failure: a BLE send that never returns (matching
    a currently-broken BLE routing setup per SYNOPSIS.md, where the
    controller is unreachable). Because the worker task is a background
    task, hass.async_block_till_done() - which is what HA's bootstrap
    effectively waits on during startup - must return promptly instead of
    hanging for the lifetime of the stuck call.
    """
    entry = _make_entry(hass)
    light = HolmanLight(hass, entry, ADDRESS)
    light.async_write_ha_state = MagicMock()

    hung = asyncio.Event()

    async def never_returns(on: bool, pct: int) -> None:
        hung.set()
        await asyncio.Event().wait()  # never set; simulates a wedged BLE op

    light._command_worker._send = never_returns  # type: ignore[assignment]

    # Fire the request but don't await it - a real service call wouldn't
    # await an entity's turn_on forever either once it's a background task.
    request_task = asyncio.ensure_future(light._async_request(on=True, pct=50))
    await hung.wait()

    # The critical assertion: block_till_done returns quickly even though
    # the worker task backing this request is still stuck.
    await asyncio.wait_for(hass.async_block_till_done(), timeout=5.0)

    # Cleanup so the test doesn't leak a running task.
    light._command_worker.stop()
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task


async def test_async_send_bounds_total_time_when_device_never_responds(hass):
    """A single hung BLE device (async_set_state that never returns) must
    not be able to hang _async_send beyond roughly
    MAX_ATTEMPTS * (OPERATION_TIMEOUT + RETRY_BACKOFF), thanks to the
    asyncio.wait_for() bound around each attempt. We shrink the timeout
    constants via monkeypatched light.py module globals so the test runs
    fast instead of waiting for the real ~51s worst case.
    """
    import custom_components.holman_garden_lights.light as light_module

    entry = _make_entry(hass)
    light = HolmanLight(hass, entry, ADDRESS)

    class _NeverRespondsDevice:
        async def async_set_state(self, *, on: bool, brightness_pct: int) -> None:
            await asyncio.Event().wait()  # never resolves

    light._device = _NeverRespondsDevice()  # type: ignore[assignment]

    original_timeout = light_module.OPERATION_TIMEOUT
    original_backoff = light_module.RETRY_BACKOFF
    light_module.OPERATION_TIMEOUT = 0.05
    light_module.RETRY_BACKOFF = 0.01
    try:
        from homeassistant.exceptions import HomeAssistantError

        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(HomeAssistantError):
            await asyncio.wait_for(
                light._async_send(True, 50), timeout=2.0
            )
        elapsed = loop.time() - start
        # Generous margin over MAX_ATTEMPTS * (0.05 + 0.01); the point of
        # this assertion is "bounded", not "exactly this many seconds".
        assert elapsed < 1.0
    finally:
        light_module.OPERATION_TIMEOUT = original_timeout
        light_module.RETRY_BACKOFF = original_backoff


async def test_turn_on_and_turn_off_update_assumed_state(hass):
    """Basic entity-level sanity check: a successful send updates is_on /
    brightness the way the UI's assumed-state display depends on."""
    entry = _make_entry(hass)
    light = HolmanLight(hass, entry, ADDRESS)
    light.hass = hass
    light.entity_id = "light.test_holman"
    light.async_write_ha_state = MagicMock()

    sent: list = []

    async def fake_send(on: bool, pct: int) -> None:
        sent.append((on, pct))

    light._command_worker._send = fake_send  # type: ignore[assignment]

    await light.async_turn_on(brightness=204)  # 80%
    assert light._attr_is_on is True
    assert light._attr_brightness == round(80 / 100 * 255)
    assert sent == [(True, 80)]

    await light.async_turn_off()
    assert light._attr_is_on is False
    assert sent[-1] == (False, 0)
