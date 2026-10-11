'Tests for Frisquet field encode/decode helpers.'

from __future__ import annotations

import pytest

from frisquet_bridge.connect.codec import (BoilerDate, decode_pressure16, decode_temp8, decode_temp16, encode_pressure16, encode_temp8,
    encode_temp16)
def test_temperature_and_pressure_codecs_round_trip():
    # Sonde test vector: 92 tenths -> 9.2 C.
    assert decode_temp16(bytes.fromhex("005c")) == pytest.approx(9.2)
    assert encode_temp16(9.2) == bytes.fromhex("005c")
    assert encode_temp8(18.0) == 130
    assert decode_temp8(130) == pytest.approx(18.0)
    assert decode_pressure16(encode_pressure16(1.5)) == pytest.approx(1.5, rel=1e-3)


def test_temp8_clamps_out_of_range():
    assert encode_temp8(-10.0) == 0
    assert encode_temp8(100.0) == 255


def test_boiler_date_bcd_decode():
    # From the frisquet-connect sonde reply test.
    d = BoilerDate.decode(bytes.fromhex("2304051131172803"))
    assert (d.year, d.month, d.day, d.hour, d.minute, d.second, d.weekday) == (2023, 4, 5, 11, 31, 17, 3)
    assert d.isoformat() == "2023-04-05 11:31:17"
    assert d.slot == 11 * 2 + 31 // 30
