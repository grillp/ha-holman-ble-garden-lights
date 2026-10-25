"""Tests for the Holman Garden Lights config flow.

Covers the discovery/manual-entry paths flagged in SYNOPSIS.md section 4
item 1 ("Could not find the device" when adding) as having been fixed:
UUID match, name-fallback match, scanning both connectable and
non-connectable advertisements, and the always-present manual-entry
escape hatch.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.holman_garden_lights.const import CONF_ADDRESS, DOMAIN, SERVICE_UUID

UUID_MATCH_ADDRESS = "11:11:11:11:11:11"
NAME_MATCH_ADDRESS = "22:22:22:22:22:22"
NO_MATCH_ADDRESS = "33:33:33:33:33:33"


def _discovery(address: str, name: str, service_uuids: list[str]) -> SimpleNamespace:
    """A minimal stand-in for BluetoothServiceInfoBleak - config_flow.py
    only reads .address, .name and .service_uuids off this object."""
    return SimpleNamespace(address=address, name=name, service_uuids=service_uuids)


def _patched_discovered_service_info(discoveries: list[SimpleNamespace]):
    def _fake(hass, connectable):
        return discoveries

    return _fake


async def test_user_step_lists_uuid_and_name_matches_plus_manual_entry(hass):
    discoveries = [
        _discovery(UUID_MATCH_ADDRESS, "Vibrance_WW", [SERVICE_UUID]),
        _discovery(NAME_MATCH_ADDRESS, "Holman Garden Controller", []),
        _discovery(NO_MATCH_ADDRESS, "Some Other BLE Thing", []),
    ]

    with patch(
        "custom_components.holman_garden_lights.config_flow.bluetooth.async_discovered_service_info",
        _patched_discovered_service_info(discoveries),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    options = result["data_schema"].schema[CONF_ADDRESS].container
    assert UUID_MATCH_ADDRESS in options
    assert NAME_MATCH_ADDRESS in options
    assert NO_MATCH_ADDRESS not in options
    assert "__manual__" in options  # always-present escape hatch


async def test_user_step_selecting_discovered_device_creates_entry(hass):
    discoveries = [_discovery(UUID_MATCH_ADDRESS, "Vibrance_WW", [SERVICE_UUID])]

    with patch(
        "custom_components.holman_garden_lights.config_flow.bluetooth.async_discovered_service_info",
        _patched_discovered_service_info(discoveries),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: UUID_MATCH_ADDRESS}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ADDRESS] == UUID_MATCH_ADDRESS


async def test_manual_entry_rejects_invalid_address(hass):
    with patch(
        "custom_components.holman_garden_lights.config_flow.bluetooth.async_discovered_service_info",
        _patched_discovered_service_info([]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: "__manual__"}
        )
        assert result["step_id"] == "manual"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: "not-a-mac-address"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ADDRESS: "invalid_address"}


async def test_manual_entry_accepts_valid_mac_and_creates_entry(hass):
    with patch(
        "custom_components.holman_garden_lights.config_flow.bluetooth.async_discovered_service_info",
        _patched_discovered_service_info([]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: "__manual__"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: "aa:bb:cc:dd:ee:ff"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ADDRESS] == "AA:BB:CC:DD:EE:FF"


async def test_bluetooth_discovery_confirms_and_creates_entry(hass):
    discovery_info = _discovery(UUID_MATCH_ADDRESS, "Vibrance_WW", [SERVICE_UUID])

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=discovery_info,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ADDRESS] == UUID_MATCH_ADDRESS
