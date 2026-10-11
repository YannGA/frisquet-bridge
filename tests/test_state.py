'Tests for protocol state (request id management).'

from __future__ import annotations

from frisquet_bridge.connect.state import ProtocolState


def test_next_request_id_increments_wraps_and_notifies():
    state = ProtocolState(network_id=bytes.fromhex("05d97f78"), association_id=0x9C, request_id=0x18)
    assert state.next_request_id() == 0x1C
    assert state.request_id == 0x1C

    state.request_id = 0xFE
    assert state.next_request_id() == 0x02

    seen = []
    state.on_change = lambda st: seen.append(st.request_id)
    state.request_id = 0x10
    assert state.next_request_id() == 0x14
    assert seen == [0x14]
