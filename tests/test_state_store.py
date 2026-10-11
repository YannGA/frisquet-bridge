"""Tests for learned zone-state persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from frisquet_bridge.climate import zone_target_temperature
from frisquet_bridge.connect.decode import encode_satellite_mode
from frisquet_bridge.model import SCHEDULE_DAYS, BoilerData, ZoneMode, ZoneSchedule
from frisquet_bridge.state_store import load_protocol_request_ids, load_zone_state, save_protocol_request_id, save_zone_state

SCHEDULE_HEX = "00e0ffffffff00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffffff"


def test_zone_state_round_trip_full_metadata(tmp_path: Path):
    path = tmp_path / "config.state.json"
    src = BoilerData()
    z = src.zones[1]
    z.mode = ZoneMode.COMFORT
    z.mode_options = 0x24
    z.comfort_temperature = 20.0
    z.reduced_temperature = 16.5
    z.frost_temperature = 7.0
    z.schedule = ZoneSchedule.decode(bytes.fromhex(SCHEDULE_HEX))
    z.central_setpoint = 20.0
    z.central_demand = True
    z.central_demand_on_delta = 4.0
    z.central_demand_off_margin = 1.0

    save_zone_state(path, src)
    dst = BoilerData()
    load_zone_state(path, dst)

    loaded = dst.zones[1]
    assert loaded.mode == ZoneMode.COMFORT
    assert loaded.mode_options == 0x24
    assert loaded.comfort_temperature == 20.0
    assert loaded.reduced_temperature == 16.5
    assert loaded.frost_temperature == 7.0
    assert b"".join(loaded.schedule.days[d] for d in SCHEDULE_DAYS).hex() == SCHEDULE_HEX
    assert loaded.central_setpoint == 20.0
    assert loaded.central_demand is True
    assert loaded.central_demand_on_delta == 4.0
    assert loaded.central_demand_off_margin == 1.0
    # zones without learned config are not persisted
    assert dst.zones[2].schedule is None


def test_auto_and_boost_flags_round_trip(tmp_path: Path):
    path = tmp_path / "config.state.json"
    for auto_comfort, override, wire_mode in [(True, False, 0x05), (False, True, 0x02), (True, True, 0x03)]:
        src = BoilerData()
        zone = src.zones[1]
        zone.mode = ZoneMode.AUTO
        zone.auto_comfort = auto_comfort
        zone.override = override
        save_zone_state(path, src)

        dst = BoilerData()
        load_zone_state(path, dst)
        loaded = dst.zones[1]
        assert loaded.mode == ZoneMode.AUTO
        assert loaded.auto_comfort is auto_comfort
        assert loaded.override is override
        assert encode_satellite_mode(loaded) == wire_mode

    boost = BoilerData()
    boost.zones[1].mode = ZoneMode.COMFORT
    boost.zones[1].boost = True
    boost.zones[1].comfort_temperature = 20.0
    save_zone_state(path, boost)

    dst = BoilerData()
    load_zone_state(path, dst)
    loaded = dst.zones[1]
    assert loaded.mode == ZoneMode.COMFORT
    assert loaded.boost is True
    assert encode_satellite_mode(loaded) == 0x01
    assert zone_target_temperature(loaded) == pytest.approx(22.0)


def test_load_handles_absent_corrupt_and_legacy_payloads(tmp_path: Path):
    absent = BoilerData()
    load_zone_state(tmp_path / "absent.state.json", absent)
    assert absent.zones[1].schedule is None

    corrupt_path = tmp_path / "corrupt.state.json"
    corrupt_path.write_text("{not valid json", encoding="utf-8")
    corrupt = BoilerData()
    load_zone_state(corrupt_path, corrupt)
    assert corrupt.zones[1].schedule is None

    legacy_path = tmp_path / "legacy.state.json"
    legacy_path.write_text(
        '{"1": {"mode": "Auto", "mode_options": 16, "comfort_temperature": 20.0}}',
        encoding="utf-8",
    )
    legacy = BoilerData()
    load_zone_state(legacy_path, legacy)
    zone = legacy.zones[1]
    assert zone.mode == ZoneMode.AUTO
    assert zone.mode_options == 16
    assert zone.comfort_temperature == 20.0
    assert zone.auto_comfort is None
    assert zone.override is False
    assert zone.boost is False


def test_protocol_request_ids_survive_zone_state_save(tmp_path: Path):
    path = tmp_path / "config.state.json"
    save_protocol_request_id(path, "satellite_z1", 0x44)
    assert load_protocol_request_ids(path) == {"satellite_z1": 0x44}

    data = BoilerData()
    data.zones[1].comfort_temperature = 20.0
    save_zone_state(path, data)

    assert load_protocol_request_ids(path) == {"satellite_z1": 0x44}
