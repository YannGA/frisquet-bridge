'Tests for the virtual satellite runner.'

from __future__ import annotations

from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.ops import A029_RETRIES, BoilerOps
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.frame import ADDR_SATELLITE_Z1
from frisquet_bridge.model import BoilerData, ZoneMode
from frisquet_bridge.satellite import VirtualSatellite
from frisquet_bridge.transport.base import TransportError
from tests.helpers import FakeTransport

_SATELLITE_INFO = bytes.fromhex('2a050a000026062216274421010111005000100000000004f6000000000000000004f60000000000000000')


def _satellite(data, transport, *, on_update=None):
    state = ProtocolState(network_id=bytes.fromhex("05d97f78"), association_id=0xC0, request_id=0x6C)
    client = FrisquetClient(transport, state, self_addr=ADDR_SATELLITE_Z1)
    return VirtualSatellite(1, BoilerOps(client, boiler_addr=0x80), data, on_update=on_update)


def _ready_zone():
    data = BoilerData()
    zs = data.zones[1]
    zs.mode, zs.auto_comfort, zs.reported_ambient, zs.reduced_temperature = ZoneMode.AUTO, False, 20.0, 19.0
    return data


async def test_send_now_skips_unknown_ambient_sends_consigne_and_reports_failure():
    data = BoilerData()
    data.zones[1].mode = ZoneMode.AUTO  # ambient still unknown
    transport = FakeTransport(init_responses={0x01: _SATELLITE_INFO})
    assert await _satellite(data, transport).send_now() is False
    assert transport.sent == []

    transport = FakeTransport(init_responses={0x01: _SATELLITE_INFO})
    assert await _satellite(_ready_zone(), transport).send_now() is True
    sent = transport.sent[0]
    assert sent.from_addr == ADDR_SATELLITE_Z1
    # read 0xa029/0x15, write 0xa02f/0x04, data len 8.
    assert sent.payload[:9].hex() == "a0290015a02f000408"
    # ambient 20.0 and active setpoint 19.0 (reduced) encoded as temperature16.
    assert sent.payload[9:13].hex() == "00c800be"

    updates = 0

    async def on_update():
        nonlocal updates
        updates += 1

    fail_data = _ready_zone()
    transport = FakeTransport(send_raises=TransportError("serial offline"))
    assert await _satellite(fail_data, transport, on_update=on_update).send_now() is False
    assert updates == 1
    assert fail_data.rf_operations["satellite_z1_consigne"].transport_failures == A029_RETRIES
