'Tests for TOML config loading.'

from __future__ import annotations

from pathlib import Path

import pytest

from frisquet_bridge.config import BridgeConfig, ConfigError, DeviceIdentity, HeatingCurve, ZoneConfig, load

BASE = r"""[frisquet]
network_id = "05d97f78"
boiler_addr = "80"
"""
FLOW_ZONE1 = '\n[frisquet.zone1]\nmode = "flow_temp"\nassociation_id = "c0"\n\n[frisquet.zone1.heating_curve]\n'


def test_load_full_config(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        r"""
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"
sensor_poll_interval_seconds = 12.5

[frisquet.connect]
mode = "full"
association_id = "ea"

[frisquet.sonde]
enabled = false
association_id = "9c"

[frisquet.zone1]
mode = "satellite"

[frisquet.zone2]
mode = "virtual_satellite"
association_id = "c0"

[frisquet.zone3]
mode = "disabled"

[mqtt]
language = "fr"

[logging]
level = "DEBUG"
format = "json"
file = "logs/bridge.log"
file_max_bytes = 12345
file_backup_count = 2
raw_file = "logs/raw.jsonl"
raw_max_bytes = 54321
raw_backup_count = 3
raw_lines = true
""",
        encoding="utf-8")

    cfg = load(path)

    assert (cfg.network_id, cfg.boiler_addr, cfg.sensor_poll_interval_seconds) == (bytes.fromhex("05d97f78"), 0x80, 12.5)
    assert (cfg.sonde.association_id, cfg.identity("satellite_z2").association_id, cfg.zone1.identity) == (0x9C, 0xC0, None)
    assert (cfg.logging.level, cfg.logging.raw_lines) == ("DEBUG", True)


def test_load_minimal_config_defaults(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(BASE, encoding="utf-8")

    cfg = load(path)

    assert cfg.sensor_poll_interval_seconds == 30.0
    assert cfg.connect is None
    assert cfg.connect_reads_enabled is False
    assert cfg.connect_writes_enabled is False
    assert cfg.boiler_entities_enabled is False
    assert cfg.sonde is None
    assert cfg.zone1 is None
    assert cfg.mqtt.language == "en"


def test_connect_modes_gate_reads_and_writes(tmp_path: Path):
    cases = [('read', 'association_id = "ea"', True, False), ('full', 'association_id = "ea"', True, True), ('passive', '', False, False)]
    for mode, identity, reads, writes in cases:
        path = tmp_path / f"{mode}.toml"
        path.write_text(
            f"""[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.connect]
mode = "{mode}"
{identity}""",
            encoding="utf-8")

        cfg = load(path)

        assert (cfg.connect.identity is None) == (mode == "passive")
        assert cfg.connect_reads_enabled is reads
        assert cfg.connect_writes_enabled is writes
        assert cfg.connect_physical_zone_control_enabled is writes
        assert cfg.boiler_entities_enabled is True


def test_load_rejects_invalid_config(tmp_path: Path):
    cases = [
        ('\n[mqtt]\nlanguage = "de"\n', "mqtt.language"),
        ("\nsensor_poll_interval_seconds = 0\n", "sensor_poll_interval_seconds"),
        ("\nread_boiler = true\n", "unknown field 'read_boiler'"),
        ('\n[frisquet.connect]\nmode = "read"\n', "connect.association_id"),
        ('\n[frisquet.connect]\nmode = "full"\nenabled = true\nassociation_id = "ea"\n', "unknown field 'enabled'"),
        ('\n[frisquet.connect]\nmode = "full"\npassive = false\nassociation_id = "ea"\n', "unknown field 'passive'"),
        ('\n[frisquet.zone1]\nmode = "connect"\n', "frisquet.zone1.mode"),
        ('\n[frisquet.zone1]\nmode = "physical_satellite"\n', "frisquet.zone1.mode"),
        ('\n[frisquet.zone1]\nmode = "virtual_satellite"\n', "zone1.association_id"),
        ('\n[frisquet.zone1]\nmode = "simple_satellite"\n', "zone1.association_id"),
        (
            '\n[frisquet.zone1]\nmode = "simple_satellite"\nassociation_id = "c0"\nrequest_id = "00"\n',
            "unknown field 'request_id'",
        ),
        (
            '\n[frisquet.zone1]\nmode = "central_boiler"\nassociation_id = "c0"\ncentral_setpoint = 20.0\n',
            "unknown field 'central_setpoint'",
        ),
        ('\n[frisquet.zone2]\nmode = "central_boiler"\nassociation_id = "c0"\n', "central_boiler is only valid"),
        (
            '\n[frisquet.zone1]\nmode = "central_boiler"\nassociation_id = "c0"\n\n[frisquet.zone2]\nmode = "satellite"\n',
            "requires zone2 to be disabled",
        ),
        ('\n[frisquet.zone1]\nmode = "flow_temp"\nassociation_id = "c0"\n', "missing required table: frisquet.zone1.heating_curve"),
        (FLOW_ZONE1 + 'slope = 0\nmin_flow = 20\nmax_flow = 65\n', "expected positive number"),
        (FLOW_ZONE1 + 'slope = 1.5\nmin_flow = 20\n', "heating_curve.max_flow"),
        (FLOW_ZONE1 + 'slope = 1.5\nmin_flow = 65\nmax_flow = 20\n', "min_flow must be below max_flow"),
        (FLOW_ZONE1.replace("zone1", "zone2") + 'slope = 1.5\nmin_flow = 20\nmax_flow = 65\n', "flow_temp is only valid"),
        (FLOW_ZONE1 + 'slope = 1.5\nmin_flow = 20\nmax_flow = 65\n', "flow_temp requires frisquet.sonde"),
        (FLOW_ZONE1.replace('association_id = "c0"\n', 'association_id = "c0"\nfallback_flow = 70\n') + 'slope = 1.5\nmin_flow = 20\nmax_flow = 65\n',
            "fallback_flow: expected a flow between min_flow and max_flow"),
        ('\n[frisquet.zone1]\nmode = "simple_satellite"\nassociation_id = "c0"\nfallback_flow = 45\n', 'fallback_flow: only valid with mode = "flow_temp"'),
        ("\n[home_assistant]\nenabled = true\n", "unknown field 'home_assistant'"),
        ('\n[logging]\nformat = "pretty"\n', "logging.format")]
    for i, (extra, match) in enumerate(cases):
        path = tmp_path / f"invalid{i}.toml"
        path.write_text(BASE + extra, encoding="utf-8")
        with pytest.raises(ConfigError, match=match): load(path)


def test_load_applies_protocol_request_ids_from_sidecar(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE
        + r"""
[frisquet.zone1]
mode = "simple_satellite"
association_id = "c0"
""",
        encoding="utf-8")
    path.with_name("config.state.json").write_text(
        r"""
{
  "protocol": {
    "satellite_z1": {"request_id": 68}
  }
}
""",
        encoding="utf-8")

    cfg = load(path)

    assert cfg.zone1.identity.association_id == 0xC0
    assert cfg.zone1.identity.request_id == 0x44


def test_save_load_round_trip(tmp_path: Path):
    path = tmp_path / "config.toml"
    cfg = BridgeConfig(path=path, network_id=bytes.fromhex("05d97f78"), boiler_addr=0x80)
    cfg.set_identity("connect", association_id=0xEA, request_id=0x48)
    cfg.set_identity("sonde", association_id=0x9C, request_id=0x18)
    # Every mode keeps the boiler's heating curve, not only flow_temp, which needs it.
    cfg.zone1 = ZoneConfig(mode="simple_satellite", identity=DeviceIdentity(association_id=0xC0, request_id=0),
        heating_curve=HeatingCurve(slope=1.5, min_flow=20.0, max_flow=65.0))

    cfg.save()
    loaded = load(path)

    assert loaded.network_id == bytes.fromhex("05d97f78")
    assert loaded.boiler_addr == 0x80
    assert loaded.connect.mode == "full"
    assert loaded.connect.identity.association_id == 0xEA
    assert loaded.connect.identity.request_id == 0x48
    assert loaded.sonde.enabled is True
    assert loaded.sonde.association_id == 0x9C
    assert loaded.sonde.request_id == 0x18
    assert loaded.zone1.heating_curve == HeatingCurve(slope=1.5, min_flow=20.0, max_flow=65.0)
