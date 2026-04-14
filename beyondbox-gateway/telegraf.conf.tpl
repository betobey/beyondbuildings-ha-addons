# telegraf.conf – rendered by BeyondBox Gateway add-on (client.py)
# Placeholders {{KEY}} are replaced at runtime. Do not edit directly.

[global_tags]
  gateway = "{{GATEWAY_NAME}}"

[agent]
  interval = "10s"
  round_interval = true
  metric_batch_size = 1000
  # Buffer: ~100,000 data points = several hours of offline buffering
  metric_buffer_limit = 100000
  collection_jitter = "2s"
  flush_interval = "10s"
  flush_jitter = "5s"
  precision = "1s"
  hostname = "{{GATEWAY_NAME}}"
  omit_hostname = false

# ─── Input: Zigbee sensor data via MQTT ──────────────────────────────────────
[[inputs.mqtt_consumer]]
  servers = ["tcp://{{MQTT_HOST}}:{{MQTT_PORT}}"]
  # All Zigbee2MQTT device topics, bridge status excluded
  topics = ["{{MQTT_TOPIC_PREFIX}}/+"]
  username = "{{MQTT_USERNAME}}"
  password = "{{MQTT_PASSWORD}}"
  qos = 0
  # Extract device name from topic path (last segment)
  topic_tag = "device"
  data_format = "json_v2"

  [[inputs.mqtt_consumer.json_v2]]
    measurement_name = "zigbee_sensor"

    [[inputs.mqtt_consumer.json_v2.field]]
      path = "temperature"
      type = "float"
      optional = true

    # Radiator thermostats (e.g. SONOFF TRVZB) send local_temperature instead of temperature
    [[inputs.mqtt_consumer.json_v2.field]]
      path = "local_temperature"
      type = "float"
      optional = true

    # Thermostat target temperature
    [[inputs.mqtt_consumer.json_v2.field]]
      path = "occupied_heating_setpoint"
      type = "float"
      optional = true

    [[inputs.mqtt_consumer.json_v2.field]]
      path = "humidity"
      type = "float"
      optional = true

    [[inputs.mqtt_consumer.json_v2.field]]
      path = "pressure"
      type = "float"
      optional = true

    [[inputs.mqtt_consumer.json_v2.field]]
      path = "battery"
      type = "float"
      optional = true

    [[inputs.mqtt_consumer.json_v2.field]]
      path = "linkquality"
      type = "int"
      optional = true

    [[inputs.mqtt_consumer.json_v2.field]]
      path = "occupancy"
      type = "boolean"
      optional = true

    [[inputs.mqtt_consumer.json_v2.field]]
      path = "contact"
      type = "boolean"
      optional = true

    # Thermostat mode as string – converted to 0/1 by starlark processor
    [[inputs.mqtt_consumer.json_v2.field]]
      path = "system_mode"
      type = "string"
      optional = true

    # Heating state as string – converted to 0/1 by starlark processor
    [[inputs.mqtt_consumer.json_v2.field]]
      path = "running_state"
      type = "string"
      optional = true

# ─── Processor: Convert thermostat strings to numeric fields ─────────────────
# system_mode ("heat"/"off") → thermostat_active (1/0)
# running_state ("heating"/"idle") → heating_active (1/0)
[[processors.starlark]]
  source = '''
def apply(metric):
    mode = metric.fields.pop("system_mode", None)
    if mode != None:
        metric.fields["thermostat_active"] = 1 if mode in ("heat", "cool", "auto") else 0
    state = metric.fields.pop("running_state", None)
    if state != None:
        metric.fields["heating_active"] = 1 if state == "heating" else 0
    return metric
'''

# ─── Output: BeyondBox remote InfluxDB ───────────────────────────────────────
[[outputs.influxdb]]
  urls = ["{{INFLUX_URL}}"]
  database = "{{INFLUX_DATABASE}}"
  username = "{{INFLUX_USERNAME}}"
  password = "{{INFLUX_PASSWORD}}"
  timeout = "15s"
  metric_buffer_limit = 10000
