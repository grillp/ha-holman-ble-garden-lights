"""Config flow for Holman Garden Lights."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import DOMAIN, SERVICE_UUID

_LOGGER = logging.getLogger(__name__)

MANUAL_ENTRY = "__manual__"

# Some Holman controllers appear to omit the custom 128-bit service UUID from
# the primary advertisement (it may only be present in the scan response, or
# only surfaced to HA's Bluetooth dashboard via a different data path than
# the one config-flow discovery reads). Fall back to matching on the
# advertised local name so devices still show up even when the UUID match
# misses.
NAME_MATCH_PREFIXES = ("vibrance", "holman")


def _address_looks_like_mac(address: str) -> bool:
    parts = address.split(":")
    if len(parts) != 6:
        return False
    return all(len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p) for p in parts)


class HolmanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Holman Garden Lights."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a discovered Holman controller via HA's Bluetooth integration."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered device."""
        assert self._discovery_info is not None
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovery_info.name or self._discovery_info.address,
                data={CONF_ADDRESS: self._discovery_info.address},
            )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovery_info.name or self._discovery_info.address
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a manually-initiated flow, listing nearby matching devices."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            if address == MANUAL_ENTRY:
                return await self.async_step_manual()

            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._discovered_devices[address],
                data={CONF_ADDRESS: address},
            )

        current_addresses = self._async_current_ids()
        # Scan both connectable and non-connectable advertisements — a device
        # only reachable via a passive-only proxy would otherwise never show
        # up here, even though it's visible in HA's Bluetooth dashboard.
        seen: dict[str, BluetoothServiceInfoBleak] = {}
        for connectable in (True, False):
            for discovery in bluetooth.async_discovered_service_info(
                self.hass, connectable
            ):
                seen.setdefault(discovery.address, discovery)

        for address, discovery in seen.items():
            if address in current_addresses or address in self._discovered_devices:
                continue

            advertised_uuids = {u.lower() for u in discovery.service_uuids}
            uuid_match = SERVICE_UUID.lower() in advertised_uuids

            name = (discovery.name or "").lower()
            name_match = any(p in name for p in NAME_MATCH_PREFIXES)

            if uuid_match or name_match:
                label = discovery.name or address
                if name_match and not uuid_match:
                    label += " (matched by name — service UUID not in advertisement)"
                self._discovered_devices[address] = label

        # Always offer a manual-entry escape hatch, regardless of whether
        # discovery found anything, so you're never blocked on advertisement
        # quirks.
        options = dict(self._discovered_devices)
        options[MANUAL_ENTRY] = "Enter Bluetooth address manually…"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(options)}),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user type in the controller's Bluetooth address directly."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            if not _address_looks_like_mac(address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Holman Garden Lights ({address})",
                    data={CONF_ADDRESS: address},
                )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
            errors=errors,
        )
