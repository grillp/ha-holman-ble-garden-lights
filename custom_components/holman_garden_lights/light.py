"""Light platform for Holman Garden Lights."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ADDRESS,
    COOLDOWN,
    DOMAIN,
    MANUFACTURER,
    MAX_ATTEMPTS,
    MODEL,
    OPERATION_TIMEOUT,
    RETRY_BACKOFF,
)
from .holman_ble import HolmanBLEDevice, HolmanBLEError
from .worker import DebouncedCommandWorker

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Holman light from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    async_add_entities([HolmanLight(hass, entry, address)])


class HolmanLight(LightEntity):
    """Representation of a Holman BLE garden light controller.

    Commands are debounced the same way the original ESPHome firmware did it:
    callers only record the *latest* desired state, a single worker task sends
    it, and a cooldown follows every send. Intermediate states from rapid
    toggling or slider drags are dropped, which keeps the controller's fragile
    BLE stack from being hammered with back-to-back reconnects.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    # No state feedback path (notify characteristic isn't parsed), so the UI
    # shows an assumed state rather than a hardware-confirmed one.
    _attr_assumed_state = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, address: str) -> None:
        self._hass = hass
        self._entry = entry
        self._address = address
        self._device: HolmanBLEDevice | None = None

        # Debounce/coalescing + retry scheduling lives in a framework-agnostic
        # worker (see worker.py) so it can be unit-tested on its own. The
        # worker task itself is created via the config entry's background
        # task API, not hass.async_create_task(): a plain task created with
        # hass.async_create_task() is tracked by HA's bootstrap and will
        # block startup until it finishes (or HA's own bootstrap timeout
        # fires) if anything triggers a turn_on/turn_off during boot - e.g.
        # an automation on homeassistant_start. A background task never
        # blocks startup and is auto-cancelled on unload/shutdown.
        self._command_worker = DebouncedCommandWorker(
            send=self._async_send,
            cooldown=COOLDOWN,
            create_task=self._create_worker_task,
            on_success=self._on_command_success,
        )

        self._attr_unique_id = address
        self._attr_is_on = False
        self._attr_brightness = 255

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    async def async_added_to_hass(self) -> None:
        """Register for Bluetooth advertisement updates once added."""
        ble_device = bluetooth.async_ble_device_from_address(
            self._hass, self._address, connectable=True
        )
        if ble_device:
            self._device = HolmanBLEDevice(ble_device)

        self.async_on_remove(
            bluetooth.async_register_callback(
                self._hass,
                self._async_handle_advertisement,
                BluetoothCallbackMatcher(address=self._address),
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Stop the worker and release anyone still waiting on it."""
        self._command_worker.stop()

    def _create_worker_task(
        self, coro: Coroutine[Any, Any, None]
    ) -> asyncio.Task[None]:
        """Create the worker task via the non-startup-blocking background API.

        ConfigEntry.async_create_background_task() (not
        hass.async_create_task()) so a slow/hung BLE operation can never
        delay Home Assistant startup, and so the task is auto-cancelled if
        the config entry is unloaded.
        """
        return self._entry.async_create_background_task(
            self._hass, coro, f"{DOMAIN}_worker_{self._address}"
        )

    def _on_command_success(self, on: bool, pct: int) -> None:
        """Update entity state after a successful send."""
        self._attr_is_on = on
        if on:
            self._attr_brightness = round((pct / 100) * 255)
        self.async_write_ha_state()

    def _async_handle_advertisement(
        self, service_info: bluetooth.BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        """Keep our BLEDevice reference fresh as advertisements come in."""
        ble_device = bluetooth.async_ble_device_from_address(
            self._hass, self._address, connectable=True
        )
        if ble_device is None:
            return
        if self._device is None:
            self._device = HolmanBLEDevice(ble_device)
        else:
            self._device.set_ble_device(ble_device)

    # ------------------------------------------------------------------ #
    # Service calls: record the latest target, wake the worker, wait.
    # ------------------------------------------------------------------ #

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get("brightness")
        if brightness is None:
            brightness = self._attr_brightness or 255
        pct = max(1, round((brightness / 255) * 100))
        await self._async_request(on=True, pct=pct)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_request(on=False, pct=0)

    async def _async_request(self, *, on: bool, pct: int) -> None:
        """Queue a target state and wait for the worker to resolve it.

        If newer requests arrive before the worker picks this one up, this
        one is superseded: it resolves successfully together with the final
        request rather than sending its own (now stale) command. See
        worker.DebouncedCommandWorker for the coalescing/cooldown logic.
        """
        await self._command_worker.request(on=on, pct=pct)

    async def _async_send(self, on: bool, pct: int) -> None:
        """Send one command, retrying like the original ESPHome worker loop.

        Each attempt is bounded by OPERATION_TIMEOUT via asyncio.wait_for()
        so a single hung BLE call can't run away - whether that's because
        HA startup is waiting on this (it no longer is, see worker task
        creation above) or because a human is waiting on a toggle to take
        effect. Worst case total time is about
        MAX_ATTEMPTS * (OPERATION_TIMEOUT + RETRY_BACKOFF).
        """
        if self._device is None:
            ble_device = bluetooth.async_ble_device_from_address(
                self._hass, self._address, connectable=True
            )
            if ble_device is None:
                raise HomeAssistantError(
                    "Holman controller not currently visible over a connectable "
                    "Bluetooth adapter or proxy"
                )
            self._device = HolmanBLEDevice(ble_device)

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                await asyncio.wait_for(
                    self._device.async_set_state(on=on, brightness_pct=pct),
                    timeout=OPERATION_TIMEOUT,
                )
                return
            except (HolmanBLEError, TimeoutError) as err:
                last_error = err
                _LOGGER.warning(
                    "Holman BLE attempt %s/%s failed: %s", attempt, MAX_ATTEMPTS, err
                )
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(RETRY_BACKOFF)

        raise HomeAssistantError(
            f"Failed to update Holman light after {MAX_ATTEMPTS} attempts: {last_error}"
        )
