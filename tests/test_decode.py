'Tests for sensor/consumption decode.'

import pytest

from frisquet_bridge.connect.decode import (BOILER_EVENT_KIND_CLEAR, BOILER_EVENT_KIND_SET, decode_clock, decode_consumption,
    decode_daily_consumption, decode_dhw_mode, decode_satellite_info, decode_sensors, decode_zone_consigne, decode_zone_init,
    describe_boiler_event, encode_satellite_mode, parse_boiler_event_push, parse_memory_push)
from frisquet_bridge.connect.passive import PassiveReadTracker
from frisquet_bridge.frame import MSG_INIT, MSG_READ, Frame
from frisquet_bridge.model import BoilerData, DhwMode, ZoneMode, ZoneSource, ZoneState, daily_consumption_reset

# Real captures (logs/re).
SENSORS_CONNECT = bytes.fromhex('38028802b1012004f604f6020500000000000000001a000005015900000000000000000000011104f604f600c600c600c6005000000000050a')
SATELLITE_INFO_PUSH = bytes.fromhex('2a050a000026062216274421010111005000100000000004f6000000000000000004f60000000000000000')
ZONE_INIT_PUSH = bytes.fromhex('a1540015a15400183091821e08100000e0ffffffff00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffffff')
CONSUMPTION_DAILY_CAPTURE = '3810014b19143c4b0f14504b0f1450070200640005000000000000000000000000803100000000000000000000000000000184138001101080'
SENSORS_APP_CAPTURE = '3802720296011a04f604f601f800000000000000001a0000050159000000000000000000000000000000000000000000000000000062846a39'
# max-temp experiment: clear then set, all-zero tail (no boost bit).
BOILER_EVENT_CLEAR = bytes.fromhex("020a8031ac553a6a00000000000000000000000000")
BOILER_EVENT_SET = bytes.fromhex("010a8031ad553a6a00000000000000000000000000")


def _sensors_data():
    data = BoilerData()
    for zone in data.zones.values(): zone.source = ZoneSource.CONNECT
    return data


def test_decode_sensors():
    data = _sensors_data()
    decode_sensors(SENSORS_CONNECT, data)
    b = data.boiler
    assert (b.dhw_temperature, b.cdc_temperature, b.cdc_safety_temperature) == pytest.approx((64.8, 68.9, 51.7))
    assert (b.flue_temperature, b.dhw_power, b.heating_power, b.dhw_instant_temperature) == pytest.approx(
        (0.0, 0.0, 0.0, 34.5))
    assert b.pressure == pytest.approx(1.3, rel=1e-3)
    assert b.outside_temperature is None
    assert data.zones[1].ambient_temperature == pytest.approx(27.3)
    assert (data.zones[1].flow_setpoint_temperature, data.zones[1].setpoint_temperature) == pytest.approx((19.8, 8.0))
    assert data.zones[2].ambient_temperature is None

    # absent sentinels (127.0, 129.0, 258.1) and out-of-range values are dropped.
    payload = bytearray(57)
    payload[1:3] = (582).to_bytes(2, "big", signed=True)
    payload[3:5] = (1654).to_bytes(2, "big", signed=True)
    payload[7:9] = (1270).to_bytes(2, "big", signed=True)
    payload[37:39] = (544).to_bytes(2, "big", signed=True)
    payload[55:57] = (2581).to_bytes(2, "big", signed=True)
    data = _sensors_data()
    decode_sensors(bytes(payload), data)
    assert data.boiler.dhw_temperature == pytest.approx(58.2)
    assert data.boiler.cdc_temperature is None
    assert data.zones[2].flow_temperature is None
    assert data.zones[1].ambient_temperature is None
    assert data.boiler.outside_temperature is None


def test_decode_sensors_respects_source_and_sonde():
    payload = bytearray(57)
    for offset, value in ((5, 451), (7, 452), (37, 201), (39, 202), (43, 501), (45, 502), (49, 191), (51, 192), (55, 172)):
        payload[offset : offset + 2] = value.to_bytes(2, "big", signed=True)
    data = BoilerData()
    data.sonde.outside_temperature = 18.5
    data.zones[1].source = ZoneSource.CONNECT
    data.zones[2].source = ZoneSource.SATELLITE
    data.zones[3].source = ZoneSource.VIRTUAL
    for zone in data.zones.values(): (zone.ambient_temperature, zone.setpoint_temperature) = (18.0, 19.0)

    decode_sensors(bytes(payload), data)

    assert (data.zones[1].ambient_temperature, data.zones[1].setpoint_temperature) == pytest.approx((20.1, 19.1))
    assert (data.zones[2].ambient_temperature, data.zones[2].setpoint_temperature) == pytest.approx((18.0, 19.0))
    assert (data.zones[3].ambient_temperature, data.zones[3].setpoint_temperature) == pytest.approx((18.0, 19.0))
    assert (data.zones[1].flow_temperature, data.zones[2].flow_temperature) == pytest.approx((45.1, 45.2))
    assert (data.zones[1].flow_setpoint_temperature, data.zones[2].flow_setpoint_temperature) == pytest.approx((50.1, 50.2))
    assert data.boiler.outside_temperature == pytest.approx(17.2)
    assert data.sonde.outside_temperature == pytest.approx(18.5)


def test_decode_consumption_blocks():
    data = BoilerData()
    payload = bytearray(57)
    payload[1:3] = (100).to_bytes(2, "big", signed=True)
    payload[3:5] = (200).to_bytes(2, "big", signed=True)
    payload[19:21] = (5).to_bytes(2, "big", signed=True)
    payload[21:23] = (0).to_bytes(2, "big", signed=True)

    decode_consumption(bytes(payload), data)
    assert (data.boiler.dhw_consumption, data.boiler.heating_consumption) == (100, 200)
    assert (data.boiler.daily_dhw_consumption, data.boiler.daily_heating_consumption) == (None, None)

    data.last_seen_date = "2026-10-27T00:54:00"
    decode_daily_consumption(bytes(payload), data)
    assert (data.boiler.daily_dhw_consumption, data.boiler.daily_heating_consumption) == (5, 0)
    assert (data.boiler.dhw_consumption, data.boiler.heating_consumption) == (100, 200)
    # Read before 03:00, the counters may still hold the day before yesterday, so they claim no day yet.
    assert daily_consumption_reset(data.boiler.daily_consumption_read_at) is None
    data.last_seen_date = "2026-10-27T03:10:00"
    decode_daily_consumption(bytes(payload), data)
    assert daily_consumption_reset(data.boiler.daily_consumption_read_at).startswith("2026-10-26T00:00:00")


def test_passive_read_tracker_matches_read_responses():
    tracker = PassiveReadTracker()
    consumption = bytearray(57)
    consumption[1:3] = (123).to_bytes(2, "big", signed=True)
    consumption[3:5] = (456).to_bytes(2, "big", signed=True)
    cases = [(0x9C, 0x34, "7a34001c", bytes(consumption), "consumption", ("dhw=123", "heating=456")),
        (0xEA, 0x80, "7a18001c", bytes.fromhex(CONSUMPTION_DAILY_CAPTURE), "daily_consumption", ("dhw=5", "heating=0")),
        (0x9C, 0x38, "79e0001c", bytes(57), "sensors", ("dhw=0.0°C", "cdc=0.0°C", "pressure=0.0bar")),
        (0xEA, 0x74, "79fc001c", bytes.fromhex(SENSORS_APP_CAPTURE), "sensors_app", ("dhw=62.6°C", "cdc=66.2°C", "pressure=1.3bar"))]
    for assoc, req_id, req_payload, resp_payload, name, fields in cases:
        request = Frame(to_addr=128, from_addr=126, association_id=assoc, request_id=req_id, control=1, msg_type=MSG_READ,
            payload=bytes.fromhex(req_payload))
        response = Frame(
            to_addr=0x7E, from_addr=0x80, association_id=assoc, request_id=req_id, control=0x81, msg_type=MSG_READ, payload=resp_payload)
        assert name in (tracker.describe(request) or "")
        description = tracker.describe(response) or ""
        for field in fields: assert field in description


def test_decode_dhw_mode_and_clock():
    data = BoilerData()
    decode_dhw_mode(bytes((0x01, 0x00, 0x88)), data)
    assert data.boiler.dhw_mode == DhwMode.ECO
    assert data.boiler.dhw_frame_bits == 0x80

    decode_clock(bytes.fromhex("082606221544492101"), data)
    assert data.last_seen_date == "2026-06-22 15:44:49"


def test_decode_satellite_info():
    data = BoilerData()
    for zone in data.zones.values(): zone.source = ZoneSource.SATELLITE
    decode_satellite_info(SATELLITE_INFO_PUSH, data)
    assert data.last_seen_date == "2026-06-22 16:27:44"
    assert data.boiler.outside_temperature is None
    assert (data.zones[1].ambient_temperature, data.zones[1].setpoint_temperature) == pytest.approx((27.3, 8.0))
    assert data.zones[1].mode == ZoneMode.FROST
    assert data.zones[2].mode is None

    data = BoilerData()
    data.sonde.outside_temperature = 18.5
    payload = bytearray(SATELLITE_INFO_PUSH)
    payload[1:3] = (172).to_bytes(2, "big", signed=True)
    decode_satellite_info(bytes(payload), data)
    assert data.boiler.outside_temperature == pytest.approx(17.2)
    assert data.sonde.outside_temperature == pytest.approx(18.5)

    for source in (ZoneSource.CONNECT, ZoneSource.VIRTUAL):
        data = BoilerData()
        zone = data.zones[1]
        zone.source = source
        (zone.ambient_temperature, zone.setpoint_temperature, zone.mode) = (21.0, 22.0, ZoneMode.COMFORT)
        decode_satellite_info(SATELLITE_INFO_PUSH, data)
        assert (zone.ambient_temperature, zone.setpoint_temperature) == pytest.approx((21.0, 22.0))
        assert zone.mode == ZoneMode.COMFORT


def test_decode_zone_init():
    data = BoilerData()
    decode_zone_init(ZONE_INIT_PUSH, 1, data)
    zone = data.zones[1]
    assert (zone.comfort_temperature, zone.reduced_temperature, zone.frost_temperature) == pytest.approx((19.5, 18.0, 8.0))
    assert zone.mode == ZoneMode.FROST
    assert zone.mode_options == 0x10
    assert zone.boost is False and zone.override is False
    assert zone.schedule is not None
    assert zone.schedule.days["sunday"].hex() == "00e0ffffffff"

    data = BoilerData()
    zone = data.zones[1]
    zone.source = ZoneSource.VIRTUAL
    (zone.mode, zone.comfort_temperature) = (ZoneMode.COMFORT, 21.0)
    decode_zone_init(bytes.fromhex("a1540015a154000306967814082000"), 1, data)
    assert zone.mode == ZoneMode.COMFORT
    assert zone.comfort_temperature == pytest.approx(21.0)


def test_decode_zone_consigne():
    payload = bytes.fromhex("a0290015a02f00040800c800be00040000")
    data = BoilerData()
    data.zones[1].source = ZoneSource.SATELLITE
    decode_zone_consigne(payload, 1, data)
    zone = data.zones[1]
    assert (zone.ambient_temperature, zone.setpoint_temperature) == pytest.approx((20.0, 19.0))
    assert zone.mode == ZoneMode.AUTO
    assert zone.auto_comfort is False and zone.override is False

    for source in (ZoneSource.CONNECT, ZoneSource.VIRTUAL):
        data = BoilerData()
        zone = data.zones[1]
        zone.source = source
        (zone.mode, zone.ambient_temperature, zone.setpoint_temperature) = (ZoneMode.COMFORT, 20.0, 21.0)
        decode_zone_consigne(payload, 1, data)
        assert zone.mode == ZoneMode.COMFORT
        assert (zone.ambient_temperature, zone.setpoint_temperature) == pytest.approx((20.0, 21.0))


def test_encode_satellite_mode():
    zone = ZoneState(zone=1)
    for mode, expected in ((ZoneMode.FROST, 0x10), (ZoneMode.COMFORT, 0x01), (ZoneMode.REDUCED, 0x00)):
        zone.mode = mode
        assert encode_satellite_mode(zone) == expected

    zone.mode = ZoneMode.AUTO
    for auto_comfort, override, expected in ((True, False, 0x05), (False, False, 0x04), (True, True, 0x03), (False, True, 0x02)):
        (zone.auto_comfort, zone.override) = (auto_comfort, override)
        assert encode_satellite_mode(zone) == expected

    zone.mode = ZoneMode.COMFORT
    assert encode_satellite_mode(zone, connect_compat=True) == 0x21

    zone.mode = None
    with pytest.raises(ValueError, match="mode unknown"): encode_satellite_mode(zone)


def test_parse_memory_push_framing():
    push = parse_memory_push(bytes.fromhex('a0f0000d1a0000000000000000000000000000000000000000000000000088'))
    assert (push.address, push.length, len(push.body)) == (0xA0F0, 0x000D, 13)
    with pytest.raises(ValueError, match="truncated"): parse_memory_push(bytes.fromhex("79e00015aabb"))


def test_boiler_event_push():
    clear = parse_boiler_event_push(BOILER_EVENT_CLEAR)
    assert (clear.kind, clear.kind_label, clear.memory_tag.hex(), clear.stamp.hex()) == (
        BOILER_EVENT_KIND_CLEAR,
        "clear",
        "0a8031",
        "ac553a6a",)
    settle = parse_boiler_event_push(BOILER_EVENT_SET)
    assert (settle.kind, settle.kind_label) == (BOILER_EVENT_KIND_SET, "set")
    assert "kind=clear" in describe_boiler_event(BOILER_EVENT_CLEAR)
    assert "kind=set" in describe_boiler_event(BOILER_EVENT_SET)
    assert "device=boiler param=0x31" in describe_boiler_event(BOILER_EVENT_CLEAR)


def test_passive_read_tracker_init_frames():
    tracker = PassiveReadTracker()
    zone_init = Frame(
        to_addr=0x80, from_addr=0x08, association_id=0xC0, request_id=0xF4, control=0x7E, msg_type=MSG_INIT, payload=ZONE_INIT_PUSH)
    description = tracker.describe(zone_init)
    assert description is not None
    assert description.startswith("ZONE1 mode=Frost protection boost=off comfort=19.5°C reduced=18.0°C frost=8.0°C")
    assert "schedule_raw=sun=00e0ffffffff" in description

    request = Frame(to_addr=128, from_addr=8, association_id=192, request_id=12, control=1, msg_type=MSG_INIT,
        payload=bytes.fromhex('a0290015a02f0004080111005000100000'))
    response = Frame(
        to_addr=0x08, from_addr=0x80, association_id=0xC0, request_id=0x0C, control=0x81, msg_type=MSG_INIT, payload=SATELLITE_INFO_PUSH)
    assert "satellite_info" in (tracker.describe(request) or "")
    description = tracker.describe(response)
    assert description is not None
    assert description.startswith("SATELLITE_INFO clock=2026-06-22 16:27:44")
    assert "z1=mode:Frost protection amb:27.3°C setpoint:8.0°C" in description
    assert "ZONE1 mode=unknown" not in description
