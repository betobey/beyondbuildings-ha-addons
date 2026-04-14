"""
Beyond Integration ZHA – Home Assistant Add-on

Liest States direkt aus HA Supervisor API und schreibt sie in InfluxDB 2.
Für Wohnungen mit ZHA oder anderen nativen HA-Integrationen (kein Zigbee2MQTT).
"""

import logging
import os
import socket
import time
from datetime import datetime, timezone

import requests
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("beyondintegration-zha")

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
SUPERVISOR_URL   = "http://supervisor"


# ── Options ────────────────────────────────────────────────────────────────────

def load_options() -> dict:
    resp = requests.get(
        f"{SUPERVISOR_URL}/addons/self/options/config",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["data"]


# ── Device Identity ────────────────────────────────────────────────────────────

def get_device_id() -> str:
    from pathlib import Path
    machine_id = Path("/etc/machine-id")
    if machine_id.exists():
        mid = machine_id.read_text().strip()[:16].upper()
        return f"BB-ZHA-{mid}"
    return f"BB-ZHA-{socket.gethostname().upper()}"


def get_mac_from_device_id(device_id: str) -> str:
    import hashlib
    h = hashlib.md5(device_id.encode()).hexdigest()
    return ":".join(h[i:i+2] for i in range(0, 12, 2))


def get_local_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


# ── Registration ───────────────────────────────────────────────────────────────

def register(management_url: str, gateway_name: str) -> str:
    device_id = get_device_id()
    log.info("Registering device: id=%s name=%s", device_id, gateway_name)
    resp = requests.post(
        f"{management_url}/api/v1/devices/register",
        json={
            "serial_number": device_id,
            "mac_address":   get_mac_from_device_id(device_id),
            "gateway_name":  gateway_name,
            "type":          "ha_addon_zha",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    log.info("Registration successful — device_id=%s", data["id"])
    return data["api_key"]


# ── Heartbeat ──────────────────────────────────────────────────────────────────

def send_heartbeat(management_url: str, api_key: str) -> dict:
    resp = requests.post(
        f"{management_url}/api/v1/devices/heartbeat",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "firmware_version": "beyondintegration-zha-1.0.0",
            "ip_local":         get_local_ip(),
            "uptime_seconds":   0,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── HA States ─────────────────────────────────────────────────────────────────

def get_ha_states() -> list[dict]:
    resp = requests.get(
        f"{SUPERVISOR_URL}/core/api/states",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def parse_state_value(state: str) -> float | None:
    try:
        return float(state)
    except (ValueError, TypeError):
        return None


def states_to_points(states: list[dict], include_domains: list[str],
                     exclude_entities: list[str], influx_bucket: str,
                     gateway_name: str) -> list:
    points = []
    for s in states:
        entity_id = s.get("entity_id", "")
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        if domain not in include_domains:
            continue
        if entity_id in exclude_entities:
            continue

        state_val = s.get("state", "")
        attributes = s.get("attributes", {})

        # Numerischen Wert extrahieren
        numeric = parse_state_value(state_val)

        # Boolean states mappen
        if state_val == "on":
            numeric = 1.0
        elif state_val == "off":
            numeric = 0.0
        elif state_val == "open":
            numeric = 1.0
        elif state_val == "closed":
            numeric = 0.0

        if numeric is None:
            continue  # Text-States überspringen (z.B. "unavailable", "unknown")

        unit = attributes.get("unit_of_measurement", "")
        friendly_name = attributes.get("friendly_name", entity_id)

        try:
            last_changed = s.get("last_changed")
            ts = datetime.fromisoformat(last_changed.replace("Z", "+00:00")) if last_changed else datetime.now(timezone.utc)
        except Exception:
            ts = datetime.now(timezone.utc)

        point = (
            Point("sensor_data")
            .tag("entity_id",    entity_id)
            .tag("domain",       domain)
            .tag("unit",         unit)
            .tag("friendly_name", friendly_name)
            .tag("apartment",    influx_bucket)
            .tag("gateway",      gateway_name)
            .field("value",      numeric)
            .time(ts, WritePrecision.SECONDS)
        )
        points.append(point)

    return points


# ── InfluxDB Write ─────────────────────────────────────────────────────────────

def write_to_influx(points: list, influx: dict) -> None:
    with InfluxDBClient(
        url=influx["influx_url"],
        token=influx["influx_token"],
        org=influx["influx_org"],
    ) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=influx["influx_bucket"], record=points)
    log.info("Wrote %d points to InfluxDB v2", len(points))


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Beyond Integration ZHA starting")

    options        = load_options()
    management_url = options.get("management_url", "https://app.beyondbuildings.de").rstrip("/")
    gateway_name   = options["gateway_name"]
    poll_interval  = int(options.get("poll_interval", 60))
    include_domains = [d.strip() for d in options.get("include_domains", "sensor,binary_sensor").split(",") if d.strip()]
    exclude_entities = [e.strip() for e in options.get("exclude_entities", "").split(",") if e.strip()]

    log.info("Gateway: %s | Management: %s | Domains: %s",
             gateway_name, management_url, include_domains)

    # ── Registration ──────────────────────────────────────────────────────────
    api_key: str | None = None
    while api_key is None:
        try:
            api_key = register(management_url, gateway_name)
        except Exception as e:
            log.error("Registration failed: %s — retrying in 30s", e)
            time.sleep(30)

    # ── Initial heartbeat → InfluxDB v2 credentials ───────────────────────────
    influx: dict | None = None
    while influx is None:
        try:
            hb = send_heartbeat(management_url, api_key)
            if hb.get("influx_url") and hb.get("influx_token"):
                influx = {
                    "influx_url":    hb["influx_url"],
                    "influx_token":  hb["influx_token"],
                    "influx_org":    hb.get("influx_org", "beyondbuildings"),
                    "influx_bucket": hb["influx_bucket"],
                }
                log.info("InfluxDB v2 credentials received — bucket=%s", influx["influx_bucket"])
            else:
                log.info("Device not yet assigned to a unit — retrying in 30s")
                time.sleep(30)
        except Exception as e:
            log.error("Heartbeat failed: %s — retrying in 30s", e)
            time.sleep(30)

    # ── Poll loop ─────────────────────────────────────────────────────────────
    log.info("Poll loop running (interval: %ds)", poll_interval)
    heartbeat_counter = 0

    while True:
        try:
            states = get_ha_states()
            points = states_to_points(states, include_domains, exclude_entities,
                                      influx["influx_bucket"], gateway_name)
            if points:
                write_to_influx(points, influx)
            else:
                log.warning("No matching states found")

        except Exception as e:
            log.error("Poll error: %s", e)

        # Heartbeat alle 5 Zyklen
        heartbeat_counter += 1
        if heartbeat_counter >= 5:
            heartbeat_counter = 0
            try:
                hb = send_heartbeat(management_url, api_key)
                log.info("Heartbeat OK — status=%s", hb.get("status"))

                if hb.get("status") == "revoked":
                    log.warning("Device revoked — exiting")
                    return

                # Credentials aktualisieren falls geändert
                if hb.get("influx_token") and hb["influx_token"] != influx["influx_token"]:
                    influx["influx_token"]  = hb["influx_token"]
                    influx["influx_org"]    = hb.get("influx_org", influx["influx_org"])
                    influx["influx_bucket"] = hb.get("influx_bucket", influx["influx_bucket"])
                    log.info("InfluxDB credentials updated")

            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 401:
                    log.error("API key invalid — exiting for re-registration")
                    return
                log.error("Heartbeat error: %s", e)
            except Exception as e:
                log.error("Heartbeat error: %s", e)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
