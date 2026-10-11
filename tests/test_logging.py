'Tests for production logging helpers.'

import json

from frisquet_bridge.frame import ADDR_BOILER, ADDR_CONNECT, MSG_READ, Frame
from frisquet_bridge.logging import RawMessageRecorder


def test_raw_message_recorder_writes_frame_metadata_and_opt_in_lines(tmp_path):
    path = tmp_path / "raw.jsonl"
    recorder = RawMessageRecorder(str(path), max_bytes=10_000, backup_count=1)
    frame = Frame(to_addr=ADDR_BOILER, from_addr=ADDR_CONNECT, association_id=234, request_id=40, control=1, msg_type=MSG_READ,
        payload=bytes.fromhex('79e0001c'))

    try:
        recorder.record_frame("tx", frame, frame.encode(), outcome="attempt")
        recorder.record_line("rx", "HB")
    finally: recorder.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "rf_frame" and entry["direction"] == "tx"
    assert (entry["to_addr"], entry["from_addr"]) == ("0x80", "0x7e")
    assert (entry["association_id"], entry["request_id"]) == ("0xea", "0x28")
    assert entry["msg_name"] == "READ" and entry["payload_hex"] == "79e0001c"
    assert entry["outcome"] == "attempt"
