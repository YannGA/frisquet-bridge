'Tests for Frisquet RF frame encode/decode.'

from __future__ import annotations

import pytest

from frisquet_bridge.frame import MSG_ASSOCIATION, Frame, FrameError

# Association broadcast from the frisquet-connect pair.rs test.
ASSOCIATION_HEX = "0b008012d402410412345678"


def test_decode_association_frame_from_wire():
    frame = Frame.decode(bytes.fromhex(ASSOCIATION_HEX))
    assert (frame.to_addr, frame.from_addr, frame.association_id, frame.request_id, frame.control, frame.msg_type) == (
        0x00,
        0x80,
        0x12,
        0xD4,
        0x02,
        MSG_ASSOCIATION,)
    assert not frame.is_ack
    assert frame.payload == bytes.fromhex("0412345678")


def test_encode_read_request_matches_wire_layout():
    frame = Frame(to_addr=128, from_addr=126, association_id=156, request_id=24, control=1, msg_type=3, payload=bytes.fromhex('79e0001c'))
    raw = frame.encode()
    assert raw.hex() == "0a807e9c18010379e0001c"
    assert Frame.decode(raw) == frame


def test_frame_errors():
    with pytest.raises(FrameError, match="too short"): Frame.decode(b"\x01\x02")
    # length=0x0b implies 12 bytes; only the 7-byte header is present.
    with pytest.raises(FrameError, match="truncated"): Frame.decode(bytes.fromhex("0b008012d40241"))
    frame = Frame(to_addr=128, from_addr=126, association_id=1, request_id=2, control=1, msg_type=3, payload=b'\x00' * 250)
    with pytest.raises(FrameError, match="too long"): frame.encode()


def test_matches_and_ack_flag():
    request = Frame(to_addr=128, from_addr=126, association_id=156, request_id=24, control=1, msg_type=3)
    response = Frame(to_addr=126, from_addr=128, association_id=156, request_id=24, control=129, msg_type=3, payload=b'\xaa')
    assert response.matches(from_addr=0x80, to_addr=0x7E, association_id=0x9C, request_id=0x18)
    assert not request.matches(from_addr=0x80, to_addr=0x7E, association_id=0x9C, request_id=0x18)
    assert response.is_ack and not request.is_ack
