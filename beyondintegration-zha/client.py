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

def _get_entity_device_info() -> dict[str, dict]:
    """Gibt dict zurück: entity_id → {manufacturer, model} aus HA Device Registry."""
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    try:
        entity_resp = requests.get(
            f"{SUPERVISOR_URL}/core/api/config/entity_registry/list",
            headers=headers, timeout=10,
        )
        entity_resp.raise_for_status()
        device_resp = requests.get(
            f"{SUPERVISOR_URL}/core/api/config/device_registry/list",
            headers=headers, timeout=10,
        )
        device_resp.raise_for_status()
        devices = {d["id"]: d for d in device_resp.json()}
        result = {}
        for e in entity_resp.json():
            dev = devices.get(e.get("device_id", ""))
            if dev:
                serial_number = None
                for conn_type, conn_id in (dev.get("connections") or []):
                    if conn_type in ("zigbee", "zha"):
                        serial_number = conn_id
                        break
                result[e["entity_id"]] = {
                    "manufacturer":  dev.get("manufacturer"),
                    "model":         dev.get("model"),
                    "serial_number": serial_number,
                }
        return result
    except Exception as ex:
        log.debug("Device Registry nicht verfügbar: %s", ex)
        return {}


_BATT_MARKERS = ("_batterie", "_battery")
_LQI_MARKERS  = ("_lqi",)


def _is_batt_entity(eid: str) -> bool:
    eid_l = eid.lower()
    return any(eid_l.rfind(m) != -1 for m in _BATT_MARKERS)


def _is_lqi_entity(eid: str) -> bool:
    eid_l = eid.lower()
    return any(eid_l.rfind(m) != -1 for m in _LQI_MARKERS)


def _base_of(eid: str, markers: tuple) -> str | None:
    """Gibt den Basis-entity_id zurück, wenn eid eine Batterie/LQI-Entity ist."""
    eid_l = eid.lower()
    for m in markers:
        idx = eid_l.rfind(m)
        if idx != -1:
            return eid[:idx]
    return None


def _extract_sensor_states(
    states: list[dict],
    include_domains: list[str],
    include_entities: list[str],
) -> list[dict]:
    """Extrahiert Sensor-Konnektivität aus HA States für das Heartbeat-Monitoring.

    ZHA erstellt separate Entities für Batterie (_batterie) und LQI (_lqi).
    Pass 1 sammelt diese Werte nach Basis-entity_id.
    Pass 2 baut die Sensor-Einträge, injiziert die Werte und überspringt
    standalone Batterie/LQI-Rows.
    """
    device_info = _get_entity_device_info()

    # Pass 1 — Batterie- und LQI-Werte nach Basis-entity_id einsammeln
    battery_by_base: dict[str, int] = {}
    lqi_by_base: dict[str, int] = {}
    for s in states:
        eid = s.get("entity_id", "")
        val = s.get("state", "")
        base = _base_of(eid, _BATT_MARKERS)
        if base is not None:
            try:
                battery_by_base[base] = int(float(val))
            except (ValueError, TypeError):
                pass
            continue
        base = _base_of(eid, _LQI_MARKERS)
        if base is not None:
            try:
                lqi_by_base[base] = int(float(val))
            except (ValueError, TypeError):
                pass

    def _lookup(entity_id: str, table: dict) -> int | None:
        """Findet Wert für entity_id in table (Basis-Prefix-Match)."""
        for base, val in table.items():
            if entity_id == base or entity_id.startswith(base + "_"):
                return val
        return None

    # Pass 2 — Sensor-Einträge bauen
    result = []
    for s in states:
        entity_id = s.get("entity_id", "")
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        if domain not in include_domains:
            continue
        if include_entities and entity_id not in include_entities:
            continue

        # Standalone Batterie/LQI-Entities überspringen — Werte wurden in Pass 1 gesammelt
        if _is_batt_entity(entity_id) or _is_lqi_entity(entity_id):
            continue

        state_val = s.get("state", "")
        connected = state_val not in ("unavailable", "unknown", "none", "")
        attrs = s.get("attributes", {})

        # Batterie: erst aus Attributen, dann aus dedizierter _batterie-Entity
        battery = attrs.get("battery_level") or attrs.get("battery")
        if battery is not None:
            try:
                battery = int(float(battery))
            except (ValueError, TypeError):
                battery = None
        if battery is None:
            battery = _lookup(entity_id, battery_by_base)

        # LQI: erst aus Attributen, dann aus dedizierter _lqi-Entity
        linkquality = attrs.get("linkquality")
        if linkquality is not None:
            try:
                linkquality = int(float(linkquality))
            except (ValueError, TypeError):
                linkquality = None
        if linkquality is None:
            linkquality = _lookup(entity_id, lqi_by_base)

        dev = device_info.get(entity_id, {})
        manufacturer = dev.get("manufacturer")
        model = dev.get("model")
        serial_number = dev.get("serial_number")

        # Whitelisted entities immer senden; sonst nur physische Hardware-Sensoren
        on_whitelist = bool(include_entities and entity_id in include_entities)
        has_hardware = any(v is not None for v in (manufacturer, model, serial_number, battery, linkquality))
        if not on_whitelist and not has_hardware:
            continue

        result.append({
            "id":            entity_id,
            "name":          attrs.get("friendly_name") or entity_id,
            "connected":     connected,
            "battery":       battery,
            "linkquality":   linkquality,
            "last_seen":     s.get("last_changed"),
            "manufacturer":  manufacturer,
            "model":         model,
            "serial_number": serial_number,
        })
    return result


def send_heartbeat(management_url: str, api_key: str, sensor_states: list | None = None) -> dict:
    payload: dict = {
        "firmware_version": "beyondintegration-zha-1.0.0",
        "ip_local":         get_local_ip(),
        "uptime_seconds":   0,
    }
    if sensor_states is not None:
        payload["sensor_states"] = sensor_states
    resp = requests.post(
        f"{management_url}/api/v1/devices/heartbeat",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
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
                     exclude_entities: list[str], include_entities: list[str],
                     influx_bucket: str, gateway_name: str) -> list:
    points = []
    for s in states:
        entity_id = s.get("entity_id", "")
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        if domain not in include_domains:
            continue
        if entity_id in exclude_entities:
            continue
        # Wenn include_entities gesetzt: nur diese erlauben (Whitelist)
        if include_entities and entity_id not in include_entities:
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
            .time(ts, WritePrecision.S)
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
    include_domains  = [d.strip() for d in options.get("include_domains", "sensor,binary_sensor").split(",") if d.strip()]
    exclude_entities = [e.strip() for e in options.get("exclude_entities", "").split(",") if e.strip()]
    include_entities = [e.strip() for e in options.get("include_entities", "").split(",") if e.strip()]

    log.info("Gateway: %s | Management: %s | Domains: %s | Whitelist: %d entities",
             gateway_name, management_url, include_domains, len(include_entities))

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
            if hb.get("influx_v2_url") and hb.get("influx_token"):
                influx = {
                    "influx_url":    hb["influx_v2_url"],
                    "influx_token":  hb["influx_token"],
                    "influx_org":    hb.get("influx_org", "beyondbuildings"),
                    "influx_bucket": hb["influx_bucket"],
                }
                # Entitäten aus Raumkonfiguration übernehmen (Whitelist vom Backend)
                server_entities = hb.get("configured_entity_ids", [])
                if server_entities:
                    include_entities = server_entities
                    log.info("Entity whitelist from backend: %d entities", len(include_entities))
                log.info("InfluxDB v2 credentials received — url=%s bucket=%s",
                         influx["influx_url"], influx["influx_bucket"])
            else:
                log.info("Device not yet assigned to a unit — retrying in 30s")
                time.sleep(30)
        except Exception as e:
            log.error("Heartbeat failed: %s — retrying in 30s", e)
            time.sleep(30)

    # ── Poll loop ─────────────────────────────────────────────────────────────
    log.info("Poll loop running (interval: %ds)", poll_interval)
    heartbeat_counter = 0
    cached_sensor_states: list | None = None
    _battery_lqi_logged = False

    while True:
        try:
            states = get_ha_states()

            # Einmalig alle Batterie/LQI-Entities loggen (Discovery)
            if not _battery_lqi_logged:
                _kw = ("batter", "lqi", "akkustand", "signal")
                found = sorted(
                    s["entity_id"] for s in states
                    if s.get("entity_id", "").split(".")[0] == "sensor"
                    and any(k in s["entity_id"].lower() for k in _kw)
                )
                log.info("DISCOVERY — Batterie/LQI-Entities in HA (%d):\n%s",
                         len(found), "\n".join(found) if found else "(keine gefunden)")
                _battery_lqi_logged = True

            points = states_to_points(states, include_domains, exclude_entities,
                                      include_entities, influx["influx_bucket"], gateway_name)
            if points:
                write_to_influx(points, influx)
            else:
                log.warning("No matching states found")
            # States für nächsten Heartbeat cachen
            cached_sensor_states = _extract_sensor_states(
                states, include_domains, include_entities
            )
        except Exception as e:
            log.error("Poll error: %s", e)

        # Heartbeat alle 5 Zyklen
        heartbeat_counter += 1
        if heartbeat_counter >= 5:
            heartbeat_counter = 0
            try:
                hb = send_heartbeat(management_url, api_key, sensor_states=cached_sensor_states)
                log.info("Heartbeat OK — status=%s", hb.get("status"))

                if hb.get("status") == "revoked":
                    log.warning("Device revoked — exiting")
                    return

                # Credentials aktualisieren falls geändert
                if hb.get("influx_token") and hb["influx_token"] != influx["influx_token"]:
                    influx["influx_token"]  = hb["influx_token"]
                    influx["influx_org"]    = hb.get("influx_org", influx["influx_org"])
                    influx["influx_bucket"] = hb.get("influx_bucket", influx["influx_bucket"])
                    if hb.get("influx_v2_url"):
                        influx["influx_url"] = hb["influx_v2_url"]
                    log.info("InfluxDB credentials updated")

                # Entity-Whitelist aktualisieren falls Backend neue Liste sendet
                server_entities = hb.get("configured_entity_ids", [])
                if server_entities and server_entities != include_entities:
                    include_entities = server_entities
                    log.info("Entity whitelist updated: %d entities", len(include_entities))

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
