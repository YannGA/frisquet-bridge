'Unit tests for SerialTransport line handling (no hardware).'

from __future__ import annotations

import asyncio

import pytest, serial

from frisquet_bridge.frame import Frame
from frisquet_bridge.protocol import crc8, format_cmd
from frisquet_bridge.transport.base import TransportError
from frisquet_bridge.transport.serial import SerialTransport


def _rx_line(rssi: int, frame_hex: str) -> str:
    body = f"RX {rssi} {frame_hex}"
    return f"{body} {crc8(body.encode('ascii')):02x}"


@pytest.fixture
def transport() -> SerialTransport: return SerialTransport("/dev/null")


def test_handle_line_dispatches_frames_and_ignores_noise(transport: SerialTransport) -> None:
    frame = Frame(to_addr=128, from_addr=126, association_id=156, request_id=24, control=1, msg_type=3, payload=bytes.fromhex('79e0001c'))
    queue = asyncio.Queue()
    transport._subscribers.add(queue)
    # A bad CRC, a payload that is not hex, and the modem's heartbeat are all dropped.
    for line in ("RX -55 deadbeef ff", _rx_line(-55, "nothex"), "HB", _rx_line(-55, frame.encode().hex())): transport._handle_line(line)
    received = queue.get_nowait()
    assert (received.rssi, received.frame) == (-55, frame)
    assert queue.empty()


async def test_replies_resolve_only_the_pending_command_they_answer(transport: SerialTransport) -> None:
    loop = asyncio.get_running_loop()
    ok = loop.create_future()
    transport._pending = (42, "OK", ok)
    # A late reply to an earlier command, and a reply of the wrong kind, leave the pending command waiting.
    for line in ("OK 41", "ERR 41 delayed", "PONG 42"): transport._handle_line(line)
    assert not ok.done()
    transport._handle_line("OK 42")
    assert ok.result() is None

    err = loop.create_future()
    transport._pending = (43, "OK", err)
    transport._handle_line("ERR 43 bad_crc")
    with pytest.raises(TransportError, match="bad_crc"): err.result()

    pong = loop.create_future()
    transport._pending = (99, "PONG", pong)
    transport._handle_line("OK 99")
    assert not pong.done()
    transport._handle_line("PONG 99")
    assert pong.result() is None

    # A timed-out command's clean-up leaves a newer command's pending reply alone.
    old, newer = loop.create_future(), loop.create_future()
    transport._pending = (8, "OK", newer)
    transport._clear_pending(7, "OK", old)
    assert transport._pending == (8, "OK", newer)


async def test_commands_allocate_uint32_sequences_and_wrap(transport: SerialTransport) -> None:
    loop = asyncio.get_running_loop()

    class RecordingSerial:
        def __init__(self) -> None: self.writes = []

        def write(self, data: bytes) -> None:
            text = data.decode("ascii")
            self.writes.append(text)
            seq = int(text.split()[1].removeprefix("@"))
            loop.call_soon_threadsafe(transport._handle_line, f"OK {seq}")

        def flush(self) -> None: pass

    serial_port = RecordingSerial()
    transport._serial = serial_port  # type: ignore[assignment]
    transport._next_seq = 0xFFFFFFFF

    await transport.listen()
    await transport.sleep()

    assert serial_port.writes == [format_cmd("LISTEN @4294967295"), format_cmd("SLEEP @0")]


async def test_reader_failure_fails_the_transport_unless_it_is_closing() -> None:
    class FailingSerial:
        def readline(self) -> bytes: raise serial.SerialException("device disconnected")

    transport = SerialTransport("/dev/null")
    transport._serial = FailingSerial()  # type: ignore[assignment]
    transport._loop = asyncio.get_running_loop()
    pending = asyncio.get_running_loop().create_future()
    transport._pending = (7, "OK", pending)
    transport._read_loop()
    with pytest.raises(TransportError, match="serial reader failed"): await pending
    with pytest.raises(TransportError, match="serial reader failed"): await transport.wait_failed()

    closing = SerialTransport("/dev/null")
    closing._serial = FailingSerial()  # type: ignore[assignment]
    closing._loop = asyncio.get_running_loop()
    closing._closing = True
    closing._read_loop()
    with pytest.raises(TimeoutError): await asyncio.wait_for(closing.wait_failed(), timeout=0.01)
