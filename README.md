# BeyondBox Home Assistant Add-ons

Custom Home Assistant add-on repository for the [BeyondBox platform](https://app.beyondbuildings.de).

Connects your Zigbee sensors to the BeyondBox cloud platform — the same data pipeline used by BeyondBox Raspberry Pi gateways.

## Add-ons

| Add-on | Description |
|---|---|
| **BeyondBox Gateway** | Forwards Zigbee sensor data to the BeyondBox platform via Telegraf |

## Installation

### 1. Prerequisites

Install these add-ons from the standard HA stores first:

- **Mosquitto broker** — MQTT broker (official HA add-on store)
- **Zigbee2MQTT** — Zigbee coordinator bridge ([community add-on](https://github.com/zigbee2mqtt/hassio-zigbee2mqtt))

Configure Z2M to publish to your Mosquitto broker and create a dedicated MQTT user for BeyondBox (e.g. `telegraf`).

### 2. Add this repository

In Home Assistant:
1. Go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu → **Repositories**
3. Add: `https://github.com/beyondbuildings/beyondbox-ha-addons`

### 3. Install BeyondBox Gateway

1. Find **BeyondBox Gateway** in the add-on store and install it
2. Configure the add-on (see below)
3. Start the add-on

### 4. Configuration

| Option | Default | Description |
|---|---|---|
| `mqtt_host` | `localhost` | Mosquitto broker hostname |
| `mqtt_port` | `1883` | Mosquitto broker port |
| `mqtt_username` | — | MQTT username for BeyondBox |
| `mqtt_password` | — | MQTT password for BeyondBox |
| `mqtt_topic_prefix` | `zigbee2mqtt` | Z2M base topic (must match Z2M config) |
| `gateway_name` | `Mein Zuhause` | Display name in the BeyondBox platform |
| `management_url` | `https://app.beyondbuildings.de` | BeyondBox management platform URL |

### 5. Claim your device

After the first start, the add-on automatically registers with the BeyondBox platform.
Log in to [app.beyondbuildings.de](https://app.beyondbuildings.de), find your new device under **Devices**, and assign it to a unit.

Once assigned, sensor data will start flowing within one heartbeat cycle (≤ 60 seconds).

## How it works

```
Zigbee sensors
     │  (Zigbee radio)
     ▼
Zigbee2MQTT (HA add-on)
     │  MQTT topics: zigbee2mqtt/<device>
     ▼
Mosquitto (HA add-on)
     │  tcp://localhost:1883
     ▼
BeyondBox Gateway (this add-on)
  ├── client.py  — registration, heartbeat, credential management
  └── Telegraf   — reads MQTT, writes to idb.beyondbuildings.de
     │
     ▼
idb.beyondbuildings.de → app.beyondbuildings.de
```

## Differences from the Pi Gateway

| Feature | Pi Gateway | HA Add-on |
|---|---|---|
| Device ID | Pi serial (`/proc/cpuinfo`) | Machine ID (`/etc/machine-id`) |
| Local InfluxDB | Yes (366 days retention) | No (cloud only) |
| Remote access | Cloudflare Tunnel | HA Cloud / Nabu Casa |
| Setup UI | BeyondBox setup wizard | HA add-on config |

## Sensor fields

The same fields as the Pi gateway are collected:

| Field | Type | Description |
|---|---|---|
| `temperature` | float | Air temperature (°C) |
| `local_temperature` | float | Thermostat measured temperature (°C) |
| `occupied_heating_setpoint` | float | Thermostat target temperature (°C) |
| `humidity` | float | Relative humidity (%) |
| `pressure` | float | Air pressure (hPa) |
| `battery` | float | Battery level (%) |
| `linkquality` | int | Zigbee link quality |
| `occupancy` | boolean | Motion/presence detected |
| `contact` | boolean | Door/window open |
| `thermostat_active` | int | Thermostat on (1) / off (0) |
| `heating_active` | int | Currently heating (1) / idle (0) |
