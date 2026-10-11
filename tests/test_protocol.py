'Tests for host <-> modem serial line protocol helpers.'

from __future__ import annotations

from frisquet_bridge.protocol import crc8, format_cmd, parse_rx, verify_crc


def test_crc8_and_format_cmd_round_trip():
    assert crc8(b"") == 0
    assert crc8(b"abc") == ord("a") ^ ord("b") ^ ord("c")
    line = format_cmd("PING 1")
    assert line.endswith("\n")
    assert verify_crc(line.strip())
    # The CRC covers the sequence number, so commands differing only by it differ.
    assert format_cmd("TX @17 deadbeef") != format_cmd("TX @18 deadbeef")
    assert verify_crc(format_cmd("TX @17 deadbeef").strip())


def test_verify_crc_rejects_malformed_lines():
    for line in ("PING 1 ff", "PING", "", "PING 1 zz"): assert verify_crc(line) is False


def test_parse_rx():
    rx = parse_rx("RX -78 deadbeef 00")
    assert rx is not None and rx.rssi == -78 and rx.frame_hex == "deadbeef"
    assert parse_rx("OK 1") is None
    assert parse_rx("HB") is None
