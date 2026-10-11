'Tests for FrisquetClient request/response and pairing.'

from __future__ import annotations

import asyncio

import pytest

from frisquet_bridge.connect.client import AddressNotFound, FrisquetClient, RequestTimeout
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.frame import (
    ADDR_BOILER,
    ADDR_BROADCAST,
    ADDR_CONNECT,
    MSG_ADDRESS_NOT_FOUND,
    MSG_ASSOCIATION,
    MSG_INIT,
    MSG_READ,
    Frame,)
from frisquet_bridge.transport.base import TransportError
from tests.helpers import FakeTransport


def _state(): return ProtocolState(network_id=bytes.fromhex("05d97f78"), association_id=0x9C, request_id=0x18)


def _reply(request, *, control, msg_type, payload=b"", request_id=None):
    return Frame(to_addr=request.from_addr, from_addr=request.to_addr, association_id=request.association_id,
        request_id=request.request_id if request_id is None else request_id, control=control, msg_type=msg_type, payload=payload)


def _broadcast():
    return Frame(to_addr=ADDR_BROADCAST, from_addr=ADDR_BOILER, association_id=18, request_id=212, control=2, msg_type=MSG_ASSOCIATION,
        payload=bytes.fromhex('0412345678'))


async def _wait_for_sent(transport, count):
    while len(transport.sent) < count: await asyncio.sleep(0)


async def test_read_memory_request_response_and_state(fake_transport):
    state = _state()
    client = FrisquetClient(fake_transport, state)
    payload = await client.read_memory(0x79E0, 0x1C)
    assert payload == bytes.fromhex("38024b0269")
    sent = fake_transport.sent[0]
    assert sent.msg_type == MSG_READ
    assert sent.payload == bytes.fromhex("79e0001c")
    assert sent.request_id == 0x1C
    assert state.request_id == 0x1C
    assert fake_transport.network_ids[-1] == state.network_id


async def test_request_filters_noise_and_reports_nack():
    transport = FakeTransport(auto_reply=False)
    client = FrisquetClient(transport, _state())

    async def noise_then_ack():
        await _wait_for_sent(transport, 1)
        request = transport.sent[0]
        for request_id, control in (
            (request.request_id + 1, 0x81),
            (request.request_id, 0x01),
            (request.request_id, 0x82),
        ):
            transport.push_frame(
                _reply(request, request_id=request_id, control=control, msg_type=MSG_ADDRESS_NOT_FOUND))
        transport.push_frame(_reply(request, control=0x81, msg_type=MSG_READ, payload=b"success"))

    task = asyncio.create_task(noise_then_ack())
    try: result = await client.read_memory(0xA029, 1, retries=1)
    finally: await task
    assert result == b"success"
    assert len(transport.sent) == 1

    nack_transport = FakeTransport(auto_reply=False)
    nack_client = FrisquetClient(nack_transport, _state())

    async def push_nack():
        await _wait_for_sent(nack_transport, 1)
        request = nack_transport.sent[0]
        nack_transport.push_frame(
            _reply(request, control=0x81, msg_type=MSG_ADDRESS_NOT_FOUND, payload=request.payload))

    task = asyncio.create_task(push_nack())
    try:
        with pytest.raises(AddressNotFound) as caught: await nack_client.read_memory(0xA029, 1, retries=3)
    finally: await task
    assert len(nack_transport.sent) == 1
    assert caught.value.request_msg_type == MSG_READ
    assert caught.value.request_payload == bytes.fromhex("a0290001")


async def test_request_retries_then_raises():
    transport = FakeTransport(auto_reply=False)
    client = FrisquetClient(transport, _state())
    with pytest.raises(RequestTimeout, match="failed after"):
        await client.request(
            control=0x01, msg_type=MSG_READ, payload=bytes.fromhex("79e0001c"), timeout=0.01, retries=2)
    assert len(transport.sent) == 2

    failing = FakeTransport(send_raises=TransportError("modem offline"))
    client = FrisquetClient(failing, _state())
    with pytest.raises(TransportError, match="failed after"):
        await client.request(control=0x01, msg_type=MSG_READ, payload=bytes.fromhex("79e0001c"), retries=2)
    assert failing.sent == []


async def test_request_burst_repeats_identical_frame_and_accepts_mid_burst_reply():
    # The reference implementation repeats the identical frame (same message id)
    # on the lossy 0xa029 exchange instead of retrying with a fresh id.
    transport = FakeTransport(auto_reply=False)
    client = FrisquetClient(transport, _state())
    payload = bytes.fromhex("a0290015")

    task = asyncio.create_task(client.request(control=1, msg_type=MSG_INIT, payload=payload, burst=3, burst_interval=0.05, timeout=1.0))
    try:
        await _wait_for_sent(transport, 2)
        first = transport.sent[0]
        # A reply to the first frame of the burst must match: the request id does
        # not change inside a burst.
        transport.push_frame(_reply(first, control=first.control | 0x80, msg_type=MSG_INIT, payload=b"ok"))
        response = await task
    finally:
        if not task.done(): task.cancel()

    sent = transport.sent
    assert len(sent) == 3
    assert {f.request_id for f in sent} == {first.request_id}
    assert {f.payload for f in sent} == {payload}
    assert response.payload == b"ok"


async def test_pair_and_recover_network_id():
    transport = FakeTransport(auto_reply=False)
    client = FrisquetClient(transport, _state())

    async def push_broadcast():
        await asyncio.sleep(0.05)
        transport.push_frame(_broadcast())

    task = asyncio.create_task(push_broadcast())
    try: assoc = await client.pair(ADDR_CONNECT, listen_window=0.2)
    finally: await task
    assert assoc.network_id == bytes.fromhex("12345678")
    assert (assoc.association_id, assoc.request_id) == (0x12, 0xD4)
    assert transport.network_ids[0] == b"\xff\xff\xff\xff"
    reply = transport.sent[0]
    assert (reply.to_addr, reply.from_addr, reply.control, reply.msg_type) == (
        ADDR_BOILER,
        ADDR_CONNECT,
        0x82,
        MSG_ASSOCIATION,)

    with pytest.raises(TransportError, match="pairing timed out"):
        await FrisquetClient(FakeTransport(auto_reply=False), _state()).pair(ADDR_CONNECT, listen_window=0.05)

    rec = FakeTransport(auto_reply=False)

    async def push_broadcast_again():
        await asyncio.sleep(0.02)
        rec.push_frame(_broadcast())

    task = asyncio.create_task(push_broadcast_again())
    try: network_id = await FrisquetClient(rec, _state()).recover_network_id(timeout=0.3)
    finally: await task
    assert network_id == bytes.fromhex("12345678")
    assert rec.sent == []

    with pytest.raises(TransportError, match="network id recovery timed out"):
        await FrisquetClient(FakeTransport(auto_reply=False), _state()).recover_network_id(timeout=0.05)
