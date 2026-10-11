"""Tests for high-level boiler write operations."""

from __future__ import annotations

import asyncio

import pytest

from frisquet_bridge.climate import HVAC_HEAT, PRESET_BOOST
from frisquet_bridge.connect import ops as ops_module
from frisquet_bridge.connect.client import AddressNotFound, FrisquetClient, RequestTimeout
from frisquet_bridge.connect.ops import BoilerOps
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.frame import ADDR_SATELLITE_Z1, MSG_INIT, MSG_READ, Frame
from frisquet_bridge.model import BoilerData, DhwMode, ZoneMode
from frisquet_bridge.transport.base import TransportError
from tests.helpers import DEFAULT_ZONE_SCHEDULE_BYTES, seed_zone_metadata


@pytest.fixture(autouse=True)
def _fast_zone_writes(monkeypatch):
    # Zone writes are fire-and-forget bursts; drop the inter-send delay so the
    # tests don't sleep in real time.
    monkeypatch.setattr(ops_module, "ZONE_WRITE_INTERVAL", 0.0)

_SATELLITE_INFO = bytes.fromhex(
    "2a050a000026062216274421010111005000100000000004f6000000000000000004f60000000000000000"
)


def _ops(fake_transport, *, self_addr=None):
    state = ProtocolState(network_id=bytes.fromhex("05d97f78"), association_id=0xEA, request_id=0x28)
    kwargs = {} if self_addr is None else {"self_addr": self_addr}
    return BoilerOps(FrisquetClient(fake_transport, state, **kwargs), boiler_addr=0x80)


async def _ack_first_init(fake_transport):
    while not fake_transport.sent:
        await asyncio.sleep(0)
    request = fake_transport.sent[0]
    fake_transport.push_frame(
        Frame(
            to_addr=request.from_addr,
            from_addr=request.to_addr,
            association_id=request.association_id,
            request_id=request.request_id,
            control=request.control | 0x80,
            msg_type=request.msg_type,
            payload=b"",
        )
    )


async def test_tracked_operation_classifies_outcomes_and_counts_retries(fake_transport):
    fake_transport.auto_reply = False
    ops = _ops(fake_transport)
    data = BoilerData()

    async def nack():
        raise AddressNotFound(
            request_msg_type=MSG_READ,
            request_payload=b"",
            response=Frame(0x40, 0x80, 1, 1, 0x81, 0x83, b""),
        )

    async def timeout():
        raise RequestTimeout("no response")

    async def transport():
        raise TransportError("serial offline")

    async def success():
        return None

    for callback, field in ((nack, "nacks"), (timeout, "timeouts"), (transport, "transport_failures")):
        with pytest.raises(TransportError):
            await ops._tracked(data, "test_operation", 60.0, callback)
        assert getattr(data.rf_operations["test_operation"], field) == 1

    await ops._tracked(data, "test_operation", 60.0, success)
    health = data.rf_operations["test_operation"]
    assert (health.attempts, health.successes, health.consecutive_failures) == (4, 1, 0)

    with pytest.raises(RequestTimeout):
        await ops._tracked_read(data, "retrying_read", 60.0, 0x79E0, 1, lambda _p, _d: None, timeout=0.01, retries=3)
    retried = data.rf_operations["retrying_read"]
    assert (retried.attempts, retried.timeouts, retried.consecutive_failures) == (3, 3, 3)


async def test_init_writes_emit_expected_frame_and_update_model(fake_transport):
    ops = _ops(fake_transport)
    data = BoilerData()

    data.boiler.dhw_frame_bits = 0x80
    task = asyncio.create_task(_ack_first_init(fake_transport))
    try:
        await ops.write_dhw_mode(data, DhwMode.ECO)
    finally:
        await task
    assert fake_transport.sent[0].msg_type == MSG_INIT
    assert fake_transport.sent[0].payload == bytes.fromhex("a0fc0001a0fc0001020088")
    assert data.boiler.dhw_mode == DhwMode.ECO

    fake_transport.sent.clear()
    data.boiler.outside_temperature = 8.0
    task = asyncio.create_task(_ack_first_init(fake_transport))
    try:
        await ops.write_outside_temperature(data, 12.34)
    finally:
        await task
    assert fake_transport.sent[0].payload == bytes.fromhex("9c540004a029000102007b")
    assert data.sonde.outside_temperature == pytest.approx(12.3)
    assert data.boiler.outside_temperature == pytest.approx(8.0)


async def test_outside_temperature_write_decodes_the_boiler_clock(fake_transport):
    fake_transport.init_responses = {0x01: bytes.fromhex("082610051400322001")}
    ops = _ops(fake_transport)
    data = BoilerData()

    await ops.write_outside_temperature(data, 12.3)

    assert data.sonde.outside_temperature == pytest.approx(12.3)
    assert data.last_seen_date == "2026-10-05 14:00:32"


async def test_zone_writes_emit_expected_init_frame_and_update_model(fake_transport):
    ops = _ops(fake_transport)
    data = BoilerData()
    seed_zone_metadata(data, 1)

    # Schedule known -> full 0x18 write (official-app style).
    await ops.write_zone_mode(1, data, ZoneMode.COMFORT)
    sent = fake_transport.sent[0]
    assert (sent.control, sent.msg_type) == (0x08, MSG_INIT)
    assert sent.payload == bytes.fromhex("a1540018a154001830967814062500") + DEFAULT_ZONE_SCHEDULE_BYTES
    assert (data.zones[1].mode, data.zones[1].boost) == (ZoneMode.COMFORT, False)

    # Auto uses the schedule options byte 0x10, not a previously-learned frost byte.
    fake_transport.sent.clear()
    data.zones[1].mode_options = 0x20
    await ops.write_zone_mode(1, data, ZoneMode.AUTO)
    assert fake_transport.sent[0].payload[9:15].hex() == "967814051000"

    # No schedule learned -> short 0x03 write, repeated.
    fake_transport.sent.clear()
    data = BoilerData()
    zs = data.zones[1]
    zs.comfort_temperature, zs.reduced_temperature, zs.frost_temperature, zs.mode_options = 20.0, 17.0, 7.0, 0x24
    await ops.write_zone_mode(1, data, ZoneMode.COMFORT)
    assert fake_transport.sent[0].control == 0x08
    assert fake_transport.sent[0].payload == bytes.fromhex("a1540018a154000306967814062500")
    health = data.rf_operations["connect_zone1_write"]
    assert (health.attempts, health.successes) == (ops_module.ZONE_WRITE_REPEATS, ops_module.ZONE_WRITE_REPEATS)

    # Auto + eco derogation: mode byte stays auto, options switch to the fixed-level form.
    fake_transport.sent.clear()
    data = BoilerData()
    seed_zone_metadata(data, 1)
    await ops.write_zone_override(1, data, "reduced")
    assert fake_transport.sent[0].payload == bytes.fromhex("a1540018a154001830967814052400") + DEFAULT_ZONE_SCHEDULE_BYTES
    assert (data.zones[1].mode, data.zones[1].override, data.zones[1].auto_comfort) == (ZoneMode.AUTO, True, False)

    data = BoilerData()
    seed_zone_metadata(data, 1)
    data.zones[1].mode = ZoneMode.COMFORT
    await ops.apply_zone_climate(1, data, hvac_mode=HVAC_HEAT, preset=PRESET_BOOST)
    assert (data.zones[1].mode, data.zones[1].boost) == (ZoneMode.COMFORT, True)

    data = BoilerData()
    seed_zone_metadata(data, 1)
    data.zones[1].mode = ZoneMode.COMFORT
    await ops.apply_zone_climate(1, data, target_temperature=21.0)
    assert data.zones[1].comfort_temperature == pytest.approx(21.0)


async def test_send_zone_consigne_writes_satellite_frame_and_flags_malformed_response(fake_transport):
    fake_transport.init_responses = {0x01: _SATELLITE_INFO}
    ops = _ops(fake_transport, self_addr=ADDR_SATELLITE_Z1)
    data = BoilerData()
    data.zones[1].mode = ZoneMode.AUTO
    data.zones[1].auto_comfort = False

    await ops.send_zone_consigne(1, data, ambient=20.0, setpoint=19.0)

    sent = fake_transport.sent[0]
    assert sent.from_addr == ADDR_SATELLITE_Z1
    assert sent.msg_type == MSG_INIT
    # read 0xa029/0x15, write 0xa02f/0x04, data len 8: ambient, setpoint, 00, mode=0x04, options 0x0000
    assert sent.payload == bytes.fromhex("a0290015a02f00040800c800be00040000")
    # the boiler-state block in the response is decoded back into the model
    assert data.zones[1].ambient_temperature == pytest.approx(27.3)

    fake_transport.init_responses = {0x01: b"bad"}
    data = BoilerData()
    data.zones[1].mode = ZoneMode.AUTO
    with pytest.raises(ValueError):
        await ops.send_zone_consigne(1, data, ambient=20.0, setpoint=19.0)
    health = data.rf_operations["satellite_z1_consigne"]
    assert (health.attempts, health.successes, health.other_failures) == (1, 0, 1)


async def test_send_zone_consigne_bursts_identical_frames(fake_transport):
    # The reference sends the same message id several times, so a reply to any
    # frame of the burst still matches; a new id per frame would discard it.
    fake_transport.auto_reply = False
    ops = _ops(fake_transport, self_addr=ADDR_SATELLITE_Z1)
    data = BoilerData()
    data.zones[1].mode = ZoneMode.AUTO
    data.zones[1].auto_comfort = False

    with pytest.raises(RequestTimeout):
        await ops.send_zone_consigne(1, data, ambient=20.0, setpoint=19.0, timeout=0.02, retries=2, burst=3)

    sent = fake_transport.sent
    assert len(sent) == 6
    assert [f.request_id for f in sent[:3]] == [sent[0].request_id] * 3
    assert [f.request_id for f in sent[3:]] == [sent[3].request_id] * 3
    assert sent[0].request_id != sent[3].request_id
    assert len({f.payload for f in sent}) == 1


async def test_read_satellite_info_decodes_zone_state(fake_transport):
    fake_transport.read_responses = {0xA029: _SATELLITE_INFO}
    ops = _ops(fake_transport)
    data = BoilerData()

    await ops.read_satellite_info(data)

    sent = fake_transport.sent[0]
    assert (sent.msg_type, sent.payload) == (MSG_READ, bytes.fromhex("a0290015"))
    assert data.zones[1].mode == ZoneMode.FROST
    assert data.zones[1].ambient_temperature == pytest.approx(27.3)


async def test_zone_writes_reject_unlearned_metadata_and_boost_outside_comfort(fake_transport):
    ops = _ops(fake_transport)
    data = BoilerData()

    with pytest.raises(ValueError, match="not yet learned"):
        await ops.write_zone_mode(1, data, ZoneMode.COMFORT)
    assert fake_transport.sent == []

    seed_zone_metadata(data, 1)
    data.zones[1].mode = ZoneMode.AUTO
    with pytest.raises(ValueError, match="Comfort"):
        await ops.write_zone_boost(1, data, True)
