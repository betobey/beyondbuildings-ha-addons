# telegraf.conf – rendered by BeyondBox Gateway v2 add-on (client.py)
# Placeholders {{KEY}} are replaced at runtime. Do not edit directly.

[global_tags]
  gateway   = "{{GATEWAY_NAME}}"
  apartment = "{{INFLUX_BUCKET}}"

[agent]
  interval = "10s"
  round_interval = true
  metric_batch_size = 1000
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
  topics = ["{{MQTT_TOPIC_PREFIX}}/+"]
  username = "{{MQTT_USERNAME}}"
  password = "{{MQTT_PASSWORD}}"
  qos = 0
  topic_tag = "device"
  data_format = "json_v2"

  [[inputs.mqtt_consumer.json_v2]]
    measurement_name = "sensor_data"

    [[inputs.mqtt_consumer.json_v2.field]]
      path = "temperature"
      type = "float"
      optional = true

    [[inputs.mqtt_consumer.json_v2.field]]
      path = "local_temperature"
      type = "float"
      optional = true

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

    [[inputs.mqtt_consumer.json_v2.field]]
      path = "system_mode"
      type = "string"
      optional = true

    [[inputs.mqtt_consumer.json_v2.field]]
      path = "running_state"
      type = "string"
      optional = true

# ─── Processor: Convert thermostat strings to numeric fields ─────────────────
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

# ─── Output: BeyondBox remote InfluxDB v2 ────────────────────────────────────
[[outputs.influxdb_v2]]
  urls         = ["{{INFLUX_URL}}"]
  token        = "{{INFLUX_TOKEN}}"
  organization = "{{INFLUX_ORG}}"
  bucket       = "{{INFLUX_BUCKET}}"
  timeout      = "15s"
  metric_buffer_limit = 10000
