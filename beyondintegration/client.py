"""
Beyond Integration – Home Assistant Add-on

Connects any apartment in the Beyond Buildings platform to InfluxDB 2.
Differences from beyondbox-gateway (v1):
  - Heartbeat returns InfluxDB v2 credentials: influx_token, influx_org, influx_bucket
  - telegraf.conf uses [[outputs.influxdb_v2]] instead of [[outputs.influxdb]]
  - No influx_user / influx_password
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
API_KEY_FILE  = Path("/data/bb_api_key")
INFLUX_CACHE  = Path("/data/bb_influx_cache.json")
TELEGRAF_CONF = Path("/data/telegraf.conf")
TELEGRAF_TPL  = Path("/app/telegraf.conf.tpl")

HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "60"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("beyondintegration")

# ── Options ────────────────────────────────────────────────────────────────────

def load_options() -> dict:
    """Liest Add-on-Optionen via Supervisor API (bevorzugt) oder Datei-Fallback."""
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if supervisor_token:
        resp = requests.get(
            "http://supervisor/addons/self/options/config",
            headers={"Authorization": f"Bearer {supervisor_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["data"]
    return json.loads(OPTIONS_FILE.read_text())

# ── Device Identity ────────────────────────────────────────────────────────────

def get_device_id() -> str:
    machine_id = Path("/etc/machine-id")
    if machine_id.exists():
        mid = machine_id.read_text().strip()[:16].upper()
        return f"BB-HA-{mid}"
    return f"BB-HA-{socket.gethostname().upper()}"


def get_local_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None

# ── API Key Persistence ────────────────────────────────────────────────────────

def load_api_key() -> str | None:
    if API_KEY_FILE.exists():
        return API_KEY_FILE.read_text().strip() or None
    return None


def save_api_key(key: str) -> None:
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
            "mac_address":   "00:00:00:00:00:00",
            "gateway_name":  gateway_name,
            "type":          "ha_addon_v2",
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
            "firmware_version": "beyondintegration-1.0.0",
            "ip_local":         get_local_ip(),
            "uptime_seconds":   None,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

# ── Telegraf Lifecycle ─────────────────────────────────────────────────────────

_telegraf_proc: subprocess.Popen | None = None


def render_telegraf_conf(options: dict, influx: dict) -> None:
    """Renders telegraf.conf.tpl with current MQTT options and InfluxDB v2 credentials."""
    tpl = TELEGRAF_TPL.read_text()
    replacements = {
        "GATEWAY_NAME":      options["gateway_name"],
        "MQTT_HOST":         options["mqtt_host"],
        "MQTT_PORT":         str(options["mqtt_port"]),
        "MQTT_USERNAME":     options.get("mqtt_username", ""),
        "MQTT_PASSWORD":     options.get("mqtt_password", ""),
        "MQTT_TOPIC_PREFIX": options.get("mqtt_topic_prefix", "zigbee2mqtt"),
        "INFLUX_URL":        influx.get("influx_url", ""),
        "INFLUX_TOKEN":      influx.get("influx_token", ""),
        "INFLUX_ORG":        influx.get("influx_org", "beyondbuildings"),
        "INFLUX_BUCKET":     influx.get("influx_bucket", ""),
    }
    conf = tpl
    for key, val in replacements.items():
        conf = conf.replace(f"{{{{{key}}}}}", val)

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
    INFLUX_CACHE.write_text(json.dumps(data))


def influx_changed(new: dict, cached: dict) -> bool:
    return any(
        new.get(k) != cached.get(k)
        for k in ("influx_url", "influx_token", "influx_org", "influx_bucket")
    )


def update_influx_if_changed(heartbeat: dict, options: dict) -> None:
    if not heartbeat.get("influx_url"):
        log.info("No InfluxDB credentials yet — device not assigned to a unit in the platform")
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
    log.info("Beyond Integration starting (HA Add-on)")

    options        = load_options()
    management_url = options.get("management_url", "https://app.beyondbuildings.de").rstrip("/")
    gateway_name   = options["gateway_name"]

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

    # ── Initial heartbeat → get InfluxDB v2 credentials ──────────────────────
    log.info("Fetching initial configuration from platform...")
    initial_heartbeat: dict | None = None
    while initial_heartbeat is None:
        try:
            initial_heartbeat = send_heartbeat(management_url, api_key)
        except Exception as e:
            log.error("Initial heartbeat failed: %s — retrying in 30s", e)
            time.sleep(30)

    # ── Start Telegraf ────────────────────────────────────────────────────────
    influx = initial_heartbeat if initial_heartbeat.get("influx_url") else load_influx_cache()

    if not influx.get("influx_url"):
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
            heartbeat = send_heartbeat(management_url, api_key)
            log.info("Heartbeat OK — status=%s", heartbeat.get("status"))

            if heartbeat.get("status") == "revoked":
                log.warning("Device revoked by platform — exiting")
                return

            if _telegraf_proc is None and heartbeat.get("influx_url"):
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
