'Tests for MQTT command mapping and the Home Assistant MQTT contract.'

import asyncio, json, time

import pytest

from frisquet_bridge.climate import HVAC_AUTO, PRESET_COMFORT
from frisquet_bridge.config import BridgeConfig, ConnectConfig, DeviceIdentity, HeatingCurve, SondeConfig, ZoneConfig
from frisquet_bridge.model import BoilerData, BoilerStatus, DhwMode, ZoneMode, ZoneSource
from frisquet_bridge.mqtt.adapter import DEVICE_TOPIC, MqttAdapter


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

    def request_send(self): self.sends += 1


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


async def _discover(mqtt):
    "Publish discovery, returning the client and the components of the device config it received"
    client = FakeMqttClient()
    await mqtt.publish_discovery(client)
    configs = [payload for topic, payload, _retain in client.published if topic == DEVICE_TOPIC]
    return client, json.loads(configs[-1])["cmps"]


async def test_discovery_exposes_the_entities_each_configuration_needs(tmp_path, adapter, satellite_adapter):
    mqtt, _data, _ops = adapter
    client, components = await _discover(mqtt)
    device = json.loads(client.published[0][1])
    assert (device["dev"]["identifiers"], device["o"], device["availability"][0]["topic"]) == (
        ["frisquet_bridge"], {"name": "frisquet-bridge"}, "frisquet/frisquet_bridge/availability")
    # unique_id and default_entity_id are what HA's registry keys the entities on; changing them makes new entities.
    temperature = components["sensor_dhw_temperature"]
    assert (temperature["unique_id"], temperature["default_entity_id"], temperature["state_topic"]) == (
        "frisquet_bridge_dhw_temperature", "sensor.frisquet_bridge_dhw_temperature", "frisquet/boiler/dhwTemperature")
    assert components["select_dhw_mode"]["command_topic"] == "frisquet/boiler/dhwMode/set"
    climate = components["climate_zone1_climate"]
    assert climate["mode_command_topic"] == "frisquet/z1/mode/set" and "boost" not in climate["preset_modes"]

    mqtt, _data, _ops = satellite_adapter
    _client, components = await _discover(mqtt)
    assert "select_dhw_mode" not in components and "sensor_dhw_mode" in components
    assert "mode_command_topic" not in components["climate_zone1_climate"]

    mqtt, _data, _satellite, _persisted = _persisting_adapter(tmp_path, "central_boiler", None)
    _client, components = await _discover(mqtt)
    assert {"switch_central_heat_demand", "number_central_setpoint", "sensor_zone1_flow_temperature"} <= set(components)
    assert not {"climate_zone1_climate", "number_zone1_reported_ambient", "sensor_dhw_temperature"} & set(components)


async def test_number_discovery_sends_the_sonde_range(tmp_path):
    # Without min and max, HA limits an MQTT number to 0-100, which rejects any outdoor temperature below 0 °C.
    cfg = _config(tmp_path, "read")
    cfg.sonde = SondeConfig(enabled=True, association_id=0x9C, request_id=0)
    _client, components = await _discover(MqttAdapter(cfg, BoilerData(), None))
    outside = components["number_outside_temperature"]
    assert (outside["min"], outside["max"]) == (-30.0, 80.0)

    cfg.zone1 = ZoneConfig(mode="flow_temp", identity=DeviceIdentity(association_id=0xC0, request_id=0),
        heating_curve=HeatingCurve(slope=1.5, min_flow=20.0, max_flow=65.0))
    _client, components = await _discover(MqttAdapter(cfg, BoilerData(), None))
    assert "number_outside_temperature" not in components
    target = components["number_flow_target"]
    assert (target["min"], target["max"], target["command_topic"]) == (20.0, 65.0, "frisquet/flow/target/set")


async def test_flow_temp_falls_back_once_home_assistant_has_been_offline_for_30_minutes(tmp_path):
    # HA's integration writes only on change, so silence means nothing; HA's own MQTT will message says it is gone.
    writes = []

    async def write_outside_temperature(temperature):
        writes.append(temperature)
        return True

    cfg, data, satellite, client = _config(tmp_path), BoilerData(), FakeSatellite(), FakeMqttClient()
    cfg.zone1 = ZoneConfig(mode="flow_temp", identity=DeviceIdentity(association_id=0xC0, request_id=0),
        heating_curve=HeatingCurve(slope=1.5, min_flow=20.0, max_flow=65.0), fallback_flow=45.0)
    mqtt = MqttAdapter(cfg, data, None, outside_temperature_writer=write_outside_temperature, virtual_satellites={1: satellite})
    await mqtt.handle_message(client, "homeassistant/status", "offline")
    start = time.monotonic()
    await mqtt.check_fallback(start+1799)
    assert (data.zones[1].flow_demand, writes) == (None, [])
    await mqtt.check_fallback(start+1800)
    assert (data.zones[1].flow_demand, data.zones[1].flow_target, satellite.sends) == (True, 45.0, 1)
    assert writes == [pytest.approx(20-(45-19.8)/1.5)]
    await mqtt.check_fallback(start+1900)
    assert satellite.sends == 1
    await mqtt.handle_message(client, "homeassistant/status", "online")
    await mqtt.check_fallback(start+7200)
    assert satellite.sends == 1


async def test_discovery_migrates_old_topics_and_removes_entities_from_earlier_runs(adapter):
    mqtt, _data, ops = adapter
    client, _components = await _discover(mqtt)

    def steps():
        "What the bridge published since the last call, without availability, with each device config shortened to 'config'"
        published = [(topic, "config" if topic == DEVICE_TOPIC else payload) for topic, payload, _retain in client.published if "availability" not in topic]
        client.published.clear()
        return published

    # A per-entity config from before device discovery: HA keeps the entity if it is migrated before the old topic is cleared.
    steps()
    kept = "homeassistant/sensor/frisquet_bridge/dhw_temperature/config"
    await mqtt.handle_message(client, kept, '{"name": "old"}', retain=True)
    assert steps() == [(kept, '{"migrate_discovery": true}'), (DEVICE_TOPIC, "config"), (kept, "")]
    retired = "homeassistant/switch/frisquet_bridge/zone1_boost/config"
    await mqtt.handle_message(client, retired, '{"name": "old"}', retain=True)
    assert steps() == [(retired, "")]

    # The retained device config from the previous run lists an entity this run lacks: HA removes it on a platform-only entry.
    previous = dict(sensor_dhw_temperature=dict(p="sensor", name="DHW"), sensor_retired=dict(p="sensor", name="Retired"))
    await mqtt.handle_message(client, DEVICE_TOPIC, json.dumps(dict(cmps=previous)), retain=True)
    configs = [json.loads(payload)["cmps"] for topic, payload, _retain in client.published if topic == DEVICE_TOPIC]
    assert configs[0]["sensor_retired"] == {"p": "sensor"} and "sensor_retired" not in configs[1]

    # A retained command is an old one: replaying it would undo newer changes.
    await mqtt.handle_message(client, "frisquet/boiler/dhwMode/set", "Stop", retain=True)
    assert ops.calls == []


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


async def test_state_goes_out_only_on_change_until_home_assistant_restarts(adapter):
    mqtt, data, _ops = adapter
    data.boiler.status = BoilerStatus.STANDBY
    client = FakeMqttClient()
    await mqtt.publish_discovery(client)
    await mqtt.publish_state(client)
    client.published.clear()
    await mqtt.publish_discovery(client)
    await mqtt.publish_state(client)
    assert [topic for topic, _payload, _retain in client.published] == ["frisquet/frisquet_bridge/availability"]

    data.boiler.status = BoilerStatus.RUNNING
    await mqtt.publish_state(client)
    assert client.published[-1][0] == "frisquet/boiler/status"

    # HA's birth message means it lost its state, so the bridge sends everything again.
    client.published.clear()
    await mqtt.handle_message(client, "homeassistant/status", "online")
    assert {DEVICE_TOPIC, "frisquet/boiler/status"} <= {topic for topic, _payload, _retain in client.published}

    await mqtt.publish_offline(client)
    assert ("frisquet/frisquet_bridge/availability", "offline", True) in client.published


async def test_rf_diagnostics_publish_operation_health_and_zone_source(tmp_path):
    data = BoilerData()
    data.zones[1].source = ZoneSource.VIRTUAL
    data.rf_health("connect_sensors", freshness_seconds=60.0).record_success(rssi=-54)
    data.rf_health("connect_clock", freshness_seconds=3600.0).record_timeout()
    data.rf_health("satellite_z1_consigne", freshness_seconds=1200.0, zone=1).record_transport_failure()
    mqtt = MqttAdapter(_config(tmp_path), data, None)
    client, components = await _discover(mqtt)
    await mqtt.publish_state(client)

    payloads = {topic: payload for topic, payload, _retain in client.published}
    discovery = components["sensor_rf_connect_sensors"]
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
