"""
BeyondBox Gateway – Home Assistant Add-on

Responsibilities:
  - Read configuration from HA options (/data/options.json)
  - Auto-register with the BeyondBox management platform
  - Send periodic heartbeats → receive InfluxDB credentials
  - Render telegraf.conf and manage Telegraf as a subprocess
  - Reload Telegraf when InfluxDB credentials change
"""

import json
import logging
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import requests

# ── Paths ──────────────────────────────────────────────────────────────────────

OPTIONS_FILE  = Path("/data/options.json")
API_KEY_FILE  = Path("/data/config/bb_api_key")
INFLUX_CACHE  = Path("/data/config/bb_influx_cache.json")
TELEGRAF_CONF = Path("/data/config/telegraf.conf")
TELEGRAF_TPL  = Path("/app/telegraf.conf.tpl")

HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "60"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("beyondbox-gateway")

# ── Options ────────────────────────────────────────────────────────────────────

def load_options() -> dict:
    try:
        return json.loads(OPTIONS_FILE.read_text())
    except PermissionError:
        log.warning("/data/options.json not readable — falling back to Supervisor API")
        token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
        if not token:
            raise RuntimeError("Cannot read options: PermissionError on file and no SUPERVISOR_TOKEN")
        resp = requests.get(
            "http://supervisor/addons/self/options/config",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

# ── Device Identity ────────────────────────────────────────────────────────────

def get_device_id() -> str:
    """
    Returns a stable, unique device identifier.
    Uses /etc/machine-id (present on all Linux systems incl. HA OS).
    Prefixed BB-HA- to distinguish from Pi devices (BB-<serial>) in the platform.
    Falls back to MAC address (same strategy as Pi bb-client).
    """
    machine_id = Path("/etc/machine-id")
    if machine_id.exists():
        mid = machine_id.read_text().strip()[:16].upper()
        return f"BB-HA-{mid}"
    mac = get_mac().replace(":", "")
    return f"BB-HA-{mac.upper()}"


def get_mac() -> str:
    for iface in ("eth0", "wlan0", "end0"):
        path = Path(f"/sys/class/net/{iface}/address")
        if path.exists():
            return path.read_text().strip()
    return "00:00:00:00:00:00"


def get_local_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def get_uptime() -> int | None:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:
        return None

# ── API Key Persistence ────────────────────────────────────────────────────────

def load_api_key() -> str | None:
    if API_KEY_FILE.exists():
        return API_KEY_FILE.read_text().strip() or None
    return None


def save_api_key(key: str) -> None:
    API_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    API_KEY_FILE.write_text(key)
    API_KEY_FILE.chmod(0o600)
    log.info("API key saved: %s", API_KEY_FILE)

# ── Registration ───────────────────────────────────────────────────────────────

def register(management_url: str, gateway_name: str) -> str:
    device_id = get_device_id()
    log.info("Registering device: id=%s name=%s", device_id, gateway_name)
    resp = requests.post(
        f"{management_url}/api/v1/devices/register",
        json={
            "serial_number": device_id,
            "mac_address":   get_mac(),
            "gateway_name":  gateway_name,
            "type":          "ha_addon",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    log.info("Registration successful — device_id=%s", data["id"])
    return data["api_key"]

# ── Heartbeat ──────────────────────────────────────────────────────────────────

def get_ha_states() -> list[dict]:
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN", "")
    resp = requests.get(
        "http://supervisor/core/api/states",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_sensor_states(states: list[dict]) -> list[dict]:
    """Extrahiert Sensor-Konnektivität aus HA States für das Heartbeat-Monitoring."""
    _domains = {"sensor", "binary_sensor"}
    result = []
    for s in states:
        entity_id = s.get("entity_id", "")
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        if domain not in _domains:
            continue

        state_val = s.get("state", "")
        connected = state_val not in ("unavailable", "unknown", "none", "")
        attrs = s.get("attributes", {})

        battery = attrs.get("battery_level") or attrs.get("battery")
        if battery is not None:
            try:
                battery = int(float(battery))
            except (ValueError, TypeError):
                battery = None

        linkquality = attrs.get("linkquality")
        if linkquality is not None:
            try:
                linkquality = int(float(linkquality))
            except (ValueError, TypeError):
                linkquality = None

        result.append({
            "id":          entity_id,
            "name":        attrs.get("friendly_name", entity_id),
            "connected":   connected,
            "battery":     battery,
            "linkquality": linkquality,
            "last_seen":   s.get("last_changed"),
        })
    return result


def send_heartbeat(management_url: str, api_key: str, sensor_states: list | None = None) -> dict:
    payload: dict = {
        "firmware_version": "ha-addon-1.0.0",
        "ip_local":         get_local_ip(),
        "uptime_seconds":   get_uptime(),
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

# ── Telegraf Lifecycle ─────────────────────────────────────────────────────────

_telegraf_proc: subprocess.Popen | None = None


def render_telegraf_conf(options: dict, influx: dict) -> None:
    """Renders telegraf.conf.tpl with current MQTT options and InfluxDB credentials."""
    tpl = TELEGRAF_TPL.read_text()
    replacements = {
        "GATEWAY_NAME":      options["gateway_name"],
        "MQTT_HOST":         options["mqtt_host"],
        "MQTT_PORT":         str(options["mqtt_port"]),
        "MQTT_USERNAME":     options.get("mqtt_username", ""),
        "MQTT_PASSWORD":     options.get("mqtt_password", ""),
        "MQTT_TOPIC_PREFIX": options.get("mqtt_topic_prefix", "zigbee2mqtt"),
        "INFLUX_V2_URL":     influx.get("influx_v2_url", ""),
        "INFLUX_TOKEN":      influx.get("influx_token", ""),
        "INFLUX_ORG":        influx.get("influx_org", "beyond"),
        "INFLUX_BUCKET":     influx.get("influx_bucket", "sensors"),
    }
    conf = tpl
    for key, val in replacements.items():
        conf = conf.replace(f"{{{{{key}}}}}", val)

    TELEGRAF_CONF.parent.mkdir(parents=True, exist_ok=True)
    TELEGRAF_CONF.write_text(conf)
    log.info("telegraf.conf rendered → %s", TELEGRAF_CONF)


def start_telegraf() -> None:
    global _telegraf_proc
    log.info("Starting Telegraf...")
    _telegraf_proc = subprocess.Popen(["telegraf", "--config", str(TELEGRAF_CONF)])
    log.info("Telegraf started (pid=%d)", _telegraf_proc.pid)


def reload_telegraf() -> None:
    global _telegraf_proc
    if _telegraf_proc and _telegraf_proc.poll() is None:
        log.info("Reloading Telegraf (SIGHUP, pid=%d)...", _telegraf_proc.pid)
        _telegraf_proc.send_signal(signal.SIGHUP)
    else:
        log.warning("Telegraf not running — starting fresh")
        start_telegraf()

# ── InfluxDB Cache ─────────────────────────────────────────────────────────────

def load_influx_cache() -> dict:
    if INFLUX_CACHE.exists():
        try:
            return json.loads(INFLUX_CACHE.read_text())
        except Exception:
            pass
    return {}


def save_influx_cache(data: dict) -> None:
    INFLUX_CACHE.parent.mkdir(parents=True, exist_ok=True)
    INFLUX_CACHE.write_text(json.dumps(data))


def influx_changed(new: dict, cached: dict) -> bool:
    return any(
        new.get(k) != cached.get(k)
        for k in ("influx_v2_url", "influx_token", "influx_org", "influx_bucket")
    )


def update_influx_if_changed(heartbeat: dict, options: dict) -> None:
    if not heartbeat.get("influx_token"):
        log.info("No InfluxDB v2 credentials yet — device not assigned to a unit in the platform")
        return

    cached = load_influx_cache()
    if not influx_changed(heartbeat, cached):
        return

    log.info("InfluxDB credentials changed — re-rendering telegraf.conf")
    render_telegraf_conf(options, heartbeat)
    save_influx_cache(heartbeat)
    reload_telegraf()

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("BeyondBox Gateway starting (HA Add-on)")

    options         = load_options()
    management_url  = options.get("management_url", "https://app.beyondbuildings.de").rstrip("/")
    gateway_name    = options["gateway_name"]

    log.info("Gateway: %s | Management: %s", gateway_name, management_url)

    # ── Registration ──────────────────────────────────────────────────────────
    api_key = load_api_key()
    if not api_key:
        while True:
            try:
                api_key = register(management_url, gateway_name)
                save_api_key(api_key)
                break
            except Exception as e:
                log.error("Registration failed: %s — retrying in 30s", e)
                time.sleep(30)

    # ── Initial heartbeat → get InfluxDB credentials ──────────────────────────
    log.info("Fetching initial configuration from platform...")
    initial_heartbeat: dict | None = None
    while initial_heartbeat is None:
        try:
            initial_heartbeat = send_heartbeat(management_url, api_key)
        except Exception as e:
            log.error("Initial heartbeat failed: %s — retrying in 30s", e)
            time.sleep(30)

    # ── Start Telegraf ────────────────────────────────────────────────────────
    # Use cached credentials if the platform hasn't assigned a unit yet
    influx = initial_heartbeat if initial_heartbeat.get("influx_token") else load_influx_cache()

    if not influx.get("influx_token"):
        log.warning(
            "Device not yet assigned to a unit in app.beyondbuildings.de. "
            "Telegraf will start once credentials are received."
        )
    else:
        render_telegraf_conf(options, influx)
        save_influx_cache(influx)
        start_telegraf()

    # ── Heartbeat loop ────────────────────────────────────────────────────────
    log.info("Heartbeat loop running (interval: %ds)", HEARTBEAT_INTERVAL)
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        try:
            sensor_states = None
            try:
                sensor_states = _extract_sensor_states(get_ha_states())
            except Exception as e:
                log.warning("Sensor states konnten nicht gelesen werden: %s", e)
            heartbeat = send_heartbeat(management_url, api_key, sensor_states=sensor_states)
            log.info("Heartbeat OK — status=%s", heartbeat.get("status"))

            if heartbeat.get("status") == "revoked":
                log.warning("Device revoked by platform — exiting")
                return

            # Start telegraf on first credential arrival (deferred start)
            if _telegraf_proc is None and heartbeat.get("influx_token"):
                log.info("InfluxDB credentials received — starting Telegraf")
                render_telegraf_conf(options, heartbeat)
                save_influx_cache(heartbeat)
                start_telegraf()
            else:
                update_influx_if_changed(heartbeat, options)

        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                log.error("API key invalid — clearing for re-registration on next start")
                API_KEY_FILE.unlink(missing_ok=True)
                return
            log.error("Heartbeat HTTP error: %s", e)
        except Exception as e:
            log.error("Heartbeat failed: %s", e)


if __name__ == "__main__":
    main()
