'Tests for MQTT command mapping and the Home Assistant MQTT contract.'

import asyncio, json

import pytest

from frisquet_bridge.climate import HVAC_AUTO, PRESET_COMFORT
from frisquet_bridge.config import BridgeConfig, ConnectConfig, DeviceIdentity, ZoneConfig
from frisquet_bridge.model import BoilerData, BoilerStatus, DhwMode, ZoneMode, ZoneSource
from frisquet_bridge.mqtt.adapter import MqttAdapter


class FakeOps:
    def __init__(self): self.calls = []

    async def write_dhw_mode(self, data, mode):
        self.calls.append(("dhw", mode))
        data.boiler.dhw_mode = mode

    async def apply_zone_climate(self, zone, data, *, hvac_mode=None, preset=None, target_temperature=None):
        self.calls.append(("climate", zone, hvac_mode, preset, target_temperature))
        if hvac_mode == "auto": data.zones[zone].mode = ZoneMode.AUTO
        if preset == "comfort":
            data.zones[zone].override = True
            data.zones[zone].auto_comfort = True


class SlowDhwOps(FakeOps):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def write_dhw_mode(self, data, mode):
        self.started.set()
        await asyncio.wait_for(self.release.wait(), timeout=1.0)
        await super().write_dhw_mode(data, mode)


class FakeSatellite:
    def __init__(self): self.sends = 0

    async def send_now(self):
        self.sends += 1
        return True


class FakeMqttClient:
    def __init__(self): self.published = []

    async def publish(self, topic, payload, retain=False): self.published.append((topic, payload, retain))


def _config(tmp_path, connect_mode=None, zone_mode=None):
    cfg = BridgeConfig(path=tmp_path / "config.toml", network_id=bytes.fromhex("05d97f78"), boiler_addr=0x80)
    if connect_mode is not None:
        cfg.connect = ConnectConfig(mode=connect_mode, identity=DeviceIdentity(association_id=0xEA, request_id=0x48))
    if zone_mode is not None: cfg.zone1 = ZoneConfig(mode=zone_mode)
    return cfg


def _persisting_adapter(tmp_path, zone_mode, connect_mode="read"):
    data, satellite, persisted = BoilerData(), FakeSatellite(), []
    adapter = MqttAdapter(_config(tmp_path, connect_mode, zone_mode), data, None, virtual_satellites={1: satellite},
        on_persist_state=lambda: persisted.append('saved'))
    return adapter, data, satellite, persisted


@pytest.fixture
def adapter(tmp_path):
    data, ops = BoilerData(), FakeOps()
    return MqttAdapter(_config(tmp_path, "full", "satellite"), data, ops), data, ops


@pytest.fixture
def virtual_adapter(tmp_path):
    data, satellite = BoilerData(), FakeSatellite()
    return MqttAdapter(_config(tmp_path, "full", "virtual_satellite"), data, None, virtual_satellites={1: satellite}), data, satellite


@pytest.fixture
def satellite_adapter(tmp_path):
    data, ops = BoilerData(), FakeOps()
    return MqttAdapter(_config(tmp_path, "read", "satellite"), data, ops), data, ops


async def test_handle_command_maps_topics_to_writes(adapter, tmp_path):
    mqtt, _data, ops = adapter
    await mqtt.handle_command("frisquet/boiler/dhwMode/set", "Eco Horaires")
    await mqtt.handle_command("frisquet/z1/mode/set", HVAC_AUTO)
    await mqtt.handle_command("frisquet/z1/preset/set", PRESET_COMFORT)
    await mqtt.handle_command("frisquet/z1/targetTemperature/set", "21.0")
    assert ops.calls == [
        ("dhw", DhwMode.ECO_SCHEDULE),
        ("climate", 1, HVAC_AUTO, None, None),
        ("climate", 1, None, PRESET_COMFORT, None),
        ("climate", 1, None, None, 21.0)]

    writes = []

    async def write_outside_temperature(temperature):
        writes.append(temperature)
        return True

    sonde = MqttAdapter(_config(tmp_path), BoilerData(), None, outside_temperature_writer=write_outside_temperature)
    await sonde.handle_command("frisquet/outsideSensor/outsideTemperature/set", "12.34")
    assert writes == [12.34]


async def test_dhw_pending_state_behaviour(adapter, tmp_path):
    mqtt, data, ops = adapter
    client = FakeMqttClient()

    await mqtt.handle_command("frisquet/boiler/dhwMode/set", "Eco")
    data.boiler.dhw_mode = DhwMode.STOP
    await mqtt.publish_state(client)
    await mqtt.handle_command("frisquet/boiler/dhwMode/set", "Eco")
    assert ("frisquet/boiler/dhwMode", "Eco", True) in client.published
    assert ops.calls == [("dhw", DhwMode.ECO)]

    data, ops, client = BoilerData(), SlowDhwOps(), FakeMqttClient()
    data.boiler.dhw_mode = DhwMode.STOP
    slow = MqttAdapter(_config(tmp_path, "full", "satellite"), data, ops, on_state_change=lambda: slow.publish_state(client))
    task = asyncio.create_task(slow.handle_command("frisquet/boiler/dhwMode/set", "Eco"))
    try:
        await asyncio.wait_for(ops.started.wait(), timeout=1.0)
        assert ("frisquet/boiler/dhwMode", "Eco", True) in client.published
        assert ops.calls == []
        ops.release.set()
        await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert ops.calls == [("dhw", DhwMode.ECO)]


async def test_zone_commands_route_by_profile(tmp_path, virtual_adapter, satellite_adapter):
    mqtt, data, satellite = virtual_adapter
    await mqtt.handle_command("frisquet/z1/reportedAmbient/set", "20.5")
    await mqtt.handle_command("frisquet/z1/mode/set", HVAC_AUTO)
    assert (data.zones[1].reported_ambient, data.zones[1].mode, satellite.sends) == (20.5, ZoneMode.AUTO, 2)

    mqtt, _data, ops = satellite_adapter
    await mqtt.handle_command("frisquet/z1/mode/set", HVAC_AUTO)
    assert ops.calls == []

    mqtt, data, satellite, persisted = _persisting_adapter(tmp_path, "simple_satellite")
    await mqtt.handle_command("frisquet/z1/mode/set", "heat")
    await mqtt.handle_command("frisquet/z1/preset/set", "eco")
    await mqtt.handle_command("frisquet/z1/targetTemperature/set", "16.5")
    assert (data.zones[1].mode, data.zones[1].reduced_temperature) == (ZoneMode.REDUCED, 16.5)
    assert satellite.sends == 3
    assert persisted == ["saved", "saved", "saved"]

    mqtt, data, satellite, persisted = _persisting_adapter(tmp_path, "central_boiler", None)
    await mqtt.handle_command("frisquet/central/setpoint/set", "20.0")
    await mqtt.handle_command("frisquet/central/demandOnDelta/set", "4.0")
    await mqtt.handle_command("frisquet/central/demandOffMargin/set", "1.0")
    await mqtt.handle_command("frisquet/central/demand/set", "ON")
    zone = data.zones[1]
    assert (zone.central_setpoint, zone.central_demand_on_delta, zone.central_demand_off_margin, zone.central_demand) == (
        20.0,
        4.0,
        1.0,
        True,)
    assert satellite.sends == 4
    assert persisted == ["saved"] * 4


async def test_discovery_dhw_and_climate_depend_on_write_capability(tmp_path, adapter, satellite_adapter):
    mqtt, _data, _ops = adapter
    client = FakeMqttClient()
    await mqtt.publish_discovery(client)
    payloads = {topic: payload for topic, payload, _retain in client.published}
    dhw_mode = json.loads(payloads["homeassistant/select/frisquet_bridge/dhw_mode/config"])
    assert dhw_mode["state_topic"] == "frisquet/boiler/dhwMode"
    assert dhw_mode["command_topic"] == "frisquet/boiler/dhwMode/set"
    assert payloads["homeassistant/sensor/frisquet_bridge/dhw_mode/config"] == ""

    mqtt, _data, _ops = satellite_adapter
    client = FakeMqttClient()
    await mqtt.publish_discovery(client)
    payloads = {topic: payload for topic, payload, _retain in client.published}
    climate = json.loads(payloads["homeassistant/climate/frisquet_bridge/zone1_climate/config"])
    assert climate["mode_state_topic"] == "frisquet/z1/mode"
    assert "mode_command_topic" not in climate
    assert "preset_mode_command_topic" not in climate
    assert "temperature_command_topic" not in climate

    mqtt, _data, _satellite, _persisted = _persisting_adapter(tmp_path, "simple_satellite")
    client = FakeMqttClient()
    await mqtt.publish_discovery(client)
    payloads = {topic: payload for topic, payload, _retain in client.published}
    assert payloads["homeassistant/sensor/frisquet_bridge/dhw_temperature/config"]
    dhw_mode = json.loads(payloads["homeassistant/sensor/frisquet_bridge/dhw_mode/config"])
    assert dhw_mode["state_topic"] == "frisquet/boiler/dhwMode"
    assert "command_topic" not in dhw_mode
    assert payloads["homeassistant/select/frisquet_bridge/dhw_mode/config"] == ""
    assert payloads["homeassistant/sensor/frisquet_bridge/zone1_schedule_raw/config"] == ""
    assert payloads["homeassistant/switch/frisquet_bridge/zone1_boost/config"] == ""
    assert "homeassistant/climate/frisquet_bridge/zone1_climate/config" in payloads


async def test_discovery_topics_and_entity_metadata(adapter, tmp_path):
    mqtt, _data, _ops = adapter
    client = FakeMqttClient()
    await mqtt.publish_discovery(client)
    payloads = {topic: json.loads(payload) for topic, payload, _retain in client.published if topic.startswith('homeassistant/') and payload}
    dhw_temperature = payloads["homeassistant/sensor/frisquet_bridge/dhw_temperature/config"]
    assert dhw_temperature["availability_topic"] == "frisquet/frisquet_bridge/availability"
    assert (dhw_temperature["payload_available"], dhw_temperature["payload_not_available"]) == ("online", "offline")
    assert (dhw_temperature["state_class"], dhw_temperature["suggested_display_precision"]) == ("measurement", 1)
    assert dhw_temperature["name"] == "Boiler DHW temperature"
    assert payloads["homeassistant/sensor/frisquet_bridge/cdc_safety_temperature/config"]["name"] == (
        "Boiler heating body safety probe temperature")
    assert payloads["homeassistant/sensor/frisquet_bridge/dhw_consumption/config"]["state_class"] == "total_increasing"
    assert payloads["homeassistant/sensor/frisquet_bridge/daily_dhw_consumption/config"]["name"] == "Boiler DHW previous day consumption"
    assert payloads["homeassistant/sensor/frisquet_bridge/boiler_status/config"]["state_topic"] == "frisquet/boiler/status"
    climate = payloads["homeassistant/climate/frisquet_bridge/zone1_climate/config"]
    assert climate["availability_topic"] == "frisquet/frisquet_bridge/availability"
    assert climate["name"] == "Zone 1 heating"
    assert "boost" not in climate["preset_modes"]
    flow = payloads["homeassistant/sensor/frisquet_bridge/zone1_flow_temperature/config"]
    assert (flow["name"], flow["state_topic"], flow["unique_id"]) == (
        "Zone 1 flow temperature",
        "frisquet/z1/flowTemperature",
        "frisquet_bridge_zone1_flow_temperature",)
    for payload in payloads.values():
        assert payload["device"]["identifiers"] == ["frisquet_bridge"]
        assert payload["origin"] == {"name": "frisquet-bridge"}
    assert "homeassistant/sensor/frisquet_bridge/zone1_schedule_raw/config" not in payloads

    mqtt, _data, _satellite, _persisted = _persisting_adapter(tmp_path, "central_boiler", None)
    client = FakeMqttClient()
    await mqtt.publish_discovery(client)
    payloads = {topic: payload for topic, payload, _retain in client.published}
    for topic in (
        "homeassistant/switch/frisquet_bridge/central_heat_demand/config",
        "homeassistant/number/frisquet_bridge/central_setpoint/config",
        "homeassistant/number/frisquet_bridge/central_demand_on_delta/config",
        "homeassistant/number/frisquet_bridge/central_demand_off_margin/config",
        "homeassistant/sensor/frisquet_bridge/zone1_flow_temperature/config",
        "homeassistant/sensor/frisquet_bridge/zone1_flow_setpoint_temperature/config",
    ):
        assert payloads[topic]
    for topic in (
        "homeassistant/climate/frisquet_bridge/zone1_climate/config",
        "homeassistant/sensor/frisquet_bridge/central_flow_temperature/config",
        "homeassistant/sensor/frisquet_bridge/central_flow_setpoint_temperature/config",
    ):
        assert payloads[topic] == ""


async def test_discovery_localizes_french_names_and_dhw_options(tmp_path):
    cfg = _config(tmp_path, "full", "virtual_satellite")
    cfg.mqtt.language = "fr"
    mqtt = MqttAdapter(cfg, BoilerData(), FakeOps(), virtual_satellites={1: FakeSatellite()})
    client = FakeMqttClient()
    await mqtt.publish_discovery(client)
    payloads = {topic: json.loads(payload) for topic, payload, _retain in client.published if topic.startswith('homeassistant/') and payload}
    assert payloads["homeassistant/select/frisquet_bridge/dhw_mode/config"]["name"] == "Chaudière mode ECS"
    assert payloads["homeassistant/select/frisquet_bridge/dhw_mode/config"]["options"] == ["Max", "Eco", "Eco Horaires", "Stop"]
    safety = payloads["homeassistant/sensor/frisquet_bridge/cdc_safety_temperature/config"]
    assert safety["name"] == "Chaudière température sonde sécurité CDC"
    assert payloads["homeassistant/climate/frisquet_bridge/zone1_climate/config"]["name"] == "Zone 1 chauffage"
    assert payloads["homeassistant/number/frisquet_bridge/zone1_reported_ambient/config"]["name"] == "Zone 1 température ambiante déclarée"
    assert payloads["homeassistant/sensor/frisquet_bridge/zone1_flow_temperature/config"]["name"] == "Zone 1 température départ"


async def test_publish_state_values_flow_topics_and_localization(adapter, tmp_path):
    mqtt, data, _ops = adapter
    data.boiler.status = BoilerStatus.STANDBY
    data.zones[1].flow_temperature = 42.3
    data.zones[1].flow_setpoint_temperature = 45.6
    client = FakeMqttClient()
    await mqtt.publish_state(client)
    assert ("frisquet/boiler/status", "Standby", True) in client.published
    assert ("frisquet/z1/flowTemperature", "42.3", True) in client.published
    assert ("frisquet/z1/flowSetpointTemperature", "45.6", True) in client.published

    data = BoilerData()
    data.zones[1].flow_temperature = 41.0
    data.zones[1].flow_setpoint_temperature = 43.0
    mqtt = MqttAdapter(_config(tmp_path, "read", "central_boiler"), data, None)
    client = FakeMqttClient()
    await mqtt.publish_state(client)
    assert ("frisquet/z1/flowTemperature", "41.0", True) in client.published
    assert ("frisquet/z1/flowSetpointTemperature", "43.0", True) in client.published
    assert not any(topic.startswith("frisquet/central/flow") for topic, _payload, _retain in client.published)

    cfg = _config(tmp_path, "read")
    cfg.mqtt.language = "fr"
    data = BoilerData()
    data.boiler.dhw_mode = DhwMode.ECO_SCHEDULE
    data.boiler.status = BoilerStatus.HEATING_OFF
    mqtt = MqttAdapter(cfg, data, FakeOps())
    client = FakeMqttClient()
    await mqtt.publish_state(client)
    assert ("frisquet/boiler/dhwMode", "Eco Horaires", True) in client.published
    assert ("frisquet/boiler/status", "Chauffage arrêté", True) in client.published


async def test_homeassistant_birth_republishes_discovery_and_availability(adapter):
    mqtt, data, _ops = adapter
    data.boiler.status = BoilerStatus.STANDBY
    client = FakeMqttClient()
    await mqtt.publish_discovery(client)
    client.published.clear()

    await mqtt.handle_message(client, "homeassistant/status", "online")
    assert any(topic == "homeassistant/sensor/frisquet_bridge/dhw_temperature/config" for topic, _payload, _retain in client.published)
    assert ("frisquet/boiler/status", "Standby", True) in client.published

    await mqtt.publish_offline(client)
    assert ("frisquet/frisquet_bridge/availability", "offline", True) in client.published


async def test_rf_diagnostics_publish_operation_health_and_zone_source(tmp_path):
    data = BoilerData()
    data.zones[1].source = ZoneSource.VIRTUAL
    data.rf_health("connect_sensors", freshness_seconds=60.0).record_success(rssi=-54)
    data.rf_health("connect_clock", freshness_seconds=3600.0).record_timeout()
    data.rf_health("satellite_z1_consigne", freshness_seconds=1200.0, zone=1).record_transport_failure()
    mqtt = MqttAdapter(_config(tmp_path), data, None)
    client = FakeMqttClient()
    await mqtt.publish_discovery(client)
    await mqtt.publish_state(client)

    payloads = {topic: payload for topic, payload, _retain in client.published}
    discovery = json.loads(payloads["homeassistant/sensor/frisquet_bridge/rf_connect_sensors/config"])
    assert discovery["entity_category"] == "diagnostic"
    assert discovery["json_attributes_topic"] == "frisquet/diagnostics/rf/connect_sensors"

    sensors_state = json.loads(payloads["frisquet/diagnostics/rf/connect_sensors"])
    assert (sensors_state["state"], sensors_state["fresh"], sensors_state["last_rssi"]) == ("healthy", True, -54)
    assert (sensors_state["attempts"], sensors_state["successes"]) == (1, 1)
    assert sensors_state["last_success"].endswith("+00:00")
    clock_state = json.loads(payloads["frisquet/diagnostics/rf/connect_clock"])
    assert (clock_state["state"], clock_state["timeouts"]) == ("unavailable", 1)
    zone_state = json.loads(payloads["frisquet/diagnostics/rf/satellite_z1_consigne"])
    assert (zone_state["zone"], zone_state["source"]) == (1, "virtual")
