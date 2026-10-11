'Tests for the passive RF mirror.'

from frisquet_bridge.emulation import PassiveMirror
from frisquet_bridge.frame import MSG_INIT, MSG_READ, Frame
from frisquet_bridge.model import BoilerData, ZoneMode
from frisquet_bridge.transport.base import ReceivedFrame


def _received(frame, *, rssi=-43): return ReceivedFrame(rssi=rssi, frame=frame, raw=frame.encode())


async def test_passive_mirror_learns_zone_config():
    data = BoilerData()
    persisted = []
    mirror = PassiveMirror(data, boiler_addr=0x80, on_zone_config=lambda: persisted.append(True))

    # Satellite z1 reporting its 0xa154 config to the boiler, as captured.
    broadcast = Frame(
        to_addr=0x80,
        from_addr=0x08,
        association_id=0xC0,
        request_id=0x70,
        control=0x7E,
        msg_type=MSG_INIT,
        payload=bytes.fromhex(
            "a1540015a154001830"
            "91821e05250000e0ffffffff00e0ffffff7f00e0ffffff7f"
            "00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffffff"
        ))
    await mirror.handle(_received(broadcast))
    zone = data.zones[1]
    assert zone.mode == ZoneMode.AUTO
    assert zone.comfort_temperature is not None and zone.reduced_temperature is not None and zone.frost_temperature is not None
    assert zone.schedule is not None and zone.schedule.days["sunday"].hex() == "00e0ffffffff"
    assert persisted == [True]

    # Boiler relaying the satellite's ACK of a Connect zone write (control=0x88).
    data = BoilerData()
    persisted = []
    mirror = PassiveMirror(data, boiler_addr=0x80, on_zone_config=lambda: persisted.append(True))
    relayed = Frame(to_addr=126, from_addr=128, association_id=234, request_id=84, control=136, msg_type=MSG_INIT,
        payload=bytes.fromhex('3091821e05100000e0ffffffff00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffffff'))
    await mirror.handle(_received(relayed, rssi=-82))
    zone = data.zones[1]
    assert zone.mode == ZoneMode.AUTO
    assert zone.comfort_temperature is not None and zone.schedule is not None
    assert persisted == [True]

    # A satellite polling for consolidated state (0xa029) is not a zone config.
    data = BoilerData()
    mirror = PassiveMirror(data, boiler_addr=0x80)
    poll = Frame(to_addr=128, from_addr=8, association_id=192, request_id=108, control=1, msg_type=MSG_INIT,
        payload=bytes.fromhex('a0290015a02f000408010d00c300250000'))
    await mirror.handle(_received(poll))
    assert data.zones[1].schedule is None


async def test_passive_mirror_decodes_boiler_responses():
    data = BoilerData()
    mirror = PassiveMirror(data, boiler_addr=0x80)

    # Connect reads the sensor block; the boiler's matching response is decoded.
    request = Frame(to_addr=128, from_addr=126, association_id=156, request_id=28, control=1, msg_type=MSG_READ, payload=bytes.fromhex('79e0001c'))
    await mirror.handle(_received(request))
    sensors = b"\x00" + (500).to_bytes(2, "big") + b"\x00" * 54
    response = Frame(
        to_addr=0x7E, from_addr=0x80, association_id=0x9C, request_id=0x1C, control=0x81, msg_type=MSG_READ, payload=sensors)
    await mirror.handle(_received(response))
    assert data.boiler.dhw_temperature == 50.0

    # Physical satellite z1 reporting ambient/setpoint/mode (write 0xa02f).
    report = Frame(to_addr=128, from_addr=8, association_id=192, request_id=108, control=1, msg_type=MSG_INIT,
        payload=bytes.fromhex('a0290015a02f00040800c800be00040000'))
    await mirror.handle(_received(report))
    zone = data.zones[1]
    assert (zone.ambient_temperature, zone.setpoint_temperature) == (20.0, 19.0)
    assert zone.mode == ZoneMode.AUTO
    assert zone.auto_comfort is False
