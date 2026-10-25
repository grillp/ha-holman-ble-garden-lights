# Holman Garden Lights for Home Assistant

A proper Home Assistant integration for Holman's Vibrance Warm White BLE
garden light controllers. On/off and brightness, straight over Bluetooth.
No ESPHome gateway, no MQTT broker in the middle, just a native `light`
entity.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=grillp&repository=ha-holman-ble-garden-lights&category=integration)

## Why this exists

Holman's BLE garden light controllers have no official Home Assistant
support, and the protocol isn't documented anywhere public.
[danward79's ESPHome/MQTT bridge](https://github.com/danward79/holman-ESP32-ble-mqtt-gateway)
worked out how to talk to them first. This integration ports that
protocol work straight into HA, so the light behaves like any other
entity: auto-discovered, scriptable, no extra bridge device to keep
running. See Credits below.

## Features

- **Auto-discovery**: shows up under Settings → Devices & Services once HA
  spots the controller's BLE advert (random address and all, no manual
  config needed). Manual entry works too if you'd rather punch in the MAC
  yourself.
- **Native `light` entity**: on/off and brightness, works anywhere a normal
  light entity works.
- **Plays nice with shared Bluetooth**: goes through HA's own `bluetooth`
  integration and `bleak-retry-connector`, so it queues politely behind
  other BLE traffic and proxies instead of hogging the radio.
- **Debounced sends**: flick the toggle a few times fast, or drag the
  brightness slider around, and only the final state gets sent, with a
  short cooldown after each send so the controller's BLE stack doesn't
  get hit with back to back reconnects.
- **Retries built in**.

## You'll need a connectable Bluetooth adapter

Scan only Bluetooth, a lot of smart plugs and similar gear included, can
see the controller advertising but can't open a GATT connection to
actually drive it. You want either a USB Bluetooth adapter on the HA
host itself, or an ESP32 running ESPHome's `bluetooth_proxy` in active
mode, somewhere in range of the light. HA picks the right scanner on its
own.

## Installing (HACS)

Click the badge up top and it'll open the "add repository" dialog
straight in your own HA instance, if you've got
[My Home Assistant](https://my.home-assistant.io/) linked up. Otherwise
do it by hand:

1. HACS → Integrations → ⋮ → Custom repositories → add this repo,
   category "Integration".
2. Install "Holman Garden Lights", restart HA.
3. Settings → Devices & Services, it should turn up under "Discovered".
   If not, add it manually and pick the controller from the list (or
   type in the MAC yourself).

## Good to know

- No feedback from the controller itself. The light's state in HA is
  "last thing we sent", not a confirmed reading.
- Manual on/off/brightness only. Doesn't touch the controller's own
  saved timer schedules.
- Single-channel warm white, no RGB. That's just what this Holman line is.

## Credits

The actual BLE protocol this integration speaks (GATT UUIDs, the auth
handshake, the 20-byte command frame) comes from
[danward79/holman-ESP32-ble-mqtt-gateway](https://github.com/danward79/holman-ESP32-ble-mqtt-gateway)
(MIT licensed), an ESPHome/MQTT bridge for the same hardware. Thanks for
working out how these controllers actually talk. This integration
wouldn't exist without that groundwork. A handful of bytes here differ
slightly from their README (this matches what their firmware actually
sends instead), but the protocol itself is all theirs.
