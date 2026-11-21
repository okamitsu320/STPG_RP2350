"""Offline consistency audit for the two short, single-segment SDK observations.

Deliberately not a general execution gate: requires completion=hold, one chunk,
one terminal observation, and no streaming/HOLD continuation. Pattern bytes,
firmware image, approval, waveform evidence and physical effects are NOT checked.
Only aggregates are printed; raw/local device evidence is never exported.
"""

from dataclasses import asdict
import argparse
import json
from pathlib import Path
import struct
import zlib

from stpg import constants as c
from stpg.protocol import StpgCommand, StpgStatusResponse
from stpg_runner import read_run_result


def audit_short_completed_report(report: dict) -> dict:
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    require(report["kind"] == "stpg_runner_run_result" and report["schema_version"] == 1,
            "wrong report schema")
    require(report["outcome"] == "COMPLETED" and not report["errors"], "report is not cleanly completed")
    require(report["planned_segments"] == report["completed_segments"] == 1, "not one completed segment")
    require(report["plan"]["completion"] == "hold", "audit only covers short hold invocations")
    require(report["stream_open_attempted"] is report["stream_opened"] is True, "missing open evidence")
    require(report["close_attempts"] == report["close_successes"] == 1 and report["result_persisted"] is True,
            "close/persistence incomplete")
    require(all(report[key] is False for key in ("release_command_attempted", "release_acknowledged",
            "release_sequence_complete", "electrical_release_verified", "firmware_validation_pass",
            "sample_payload_collection")), "unexpected release/validation claim")
    segment, = report["plan"]["segments"]
    retained, = report["segments"]
    expected = segment["expected"]
    require(segment["index"] == retained["index"] == 0 and retained["outcome"] == "COMPLETED",
            "segment identity differs")
    require(retained["expected"] == expected and 0 < expected["size_bytes"] <= 4096,
            "expected counts differ or not a short upload")
    require(segment["run_args"] == [0, 0, 0, 0] and segment["terminal_state"] == c.STATE_DONE,
            "audit does not cover streaming or segmented RUN")
    records = report["transactions"]
    grammar = [c.CMD_INFO, c.CMD_GET_CLOCK_INFO, c.CMD_GET_STATUS, c.CMD_RESET,
               c.CMD_GET_STATUS, c.CMD_UPLOAD_BEGIN, c.CMD_UPLOAD_DATA, None,
               c.CMD_UPLOAD_END, c.CMD_RUN, c.CMD_GET_STATUS]
    require(len(records) == len(grammar), "unexpected transaction count")
    responses, commands = [], []
    sequence = 0
    previous_end = float("-inf")
    for record, command_id in zip(records, grammar):
        if command_id is not None:
            sequence += 1
            require(record["kind"] == "command" and record["command"] == command_id, "command grammar differs")
            raw_command = bytes.fromhex(record["planned_write_hex"])
            require(len(raw_command) == 32 and zlib.crc32(raw_command[:-4]) == struct.unpack("<I", raw_command[-4:])[0],
                    "bad saved command CRC")
            command = StpgCommand.unpack(raw_command)
            require(command.cmd == command_id and command.seq == record["seq"] == sequence,
                    "command/sequence linkage differs")
            require([command.arg0, command.arg1, command.arg2, command.arg3] == record["args"],
                    "planned command args differ")
            commands.append(command)
            size = len(raw_command)
        else:
            require(record["kind"] == "payload" and record["seq"] == sequence and record["offset"] == 0,
                    "payload grammar differs")
            require(record["size"] == expected["size_bytes"] and record["planned_write_sha256"] == segment["sha256"],
                    "payload hash/size linkage differs")
            size = record["size"]
        require(not any(key.endswith("_error") for key in record), "transaction contains an error")
        require(record["written_bytes"] == size, "write not complete")
        require(previous_end <= record["write_begin_s"] <= record["write_end_s"] <= record["read_begin_s"]
                <= record["read_end_s"], "non-monotonic transaction timestamps")
        previous_end = record["read_end_s"]
        raw = bytes.fromhex(record["raw_response_hex"])
        require(len(raw) == 64 and zlib.crc32(raw[:-4]) == struct.unpack("<I", raw[-4:])[0], "bad saved response CRC")
        response = StpgStatusResponse.unpack(raw)
        require(asdict(response) == record["decoded"], "raw/decoded response differs")
        require(response.seq == sequence and response.firmware_version == report["device"]["firmware_version"]
                and response.capability == report["device"]["capability"] and response.error_code == 0,
                "response identity/seq/error differs")
        parts = record["read_parts"]
        part_end = record["read_begin_s"]
        for part in parts:
            require(part_end <= part["begin_s"] <= part["end_s"] <= record["read_end_s"] and part["size"] > 0,
                    "invalid read-part timestamps/size")
            part_end = part["end_s"]
        require(sum(part["size"] for part in parts) == 64, "read parts do not account for frame")
        responses.append(response)
    require([r.rsp for r in responses] == [c.RSP_INFO, c.RSP_CLOCK_INFO, c.RSP_STATUS, c.RSP_OK,
            c.RSP_STATUS, c.RSP_OK, c.RSP_READY_FOR_PAYLOAD, c.RSP_DATA_ACK, c.RSP_OK, c.RSP_OK, c.RSP_STATUS],
            "response code grammar differs")
    require(responses[0].state in (c.STATE_IDLE, c.STATE_DONE) and
            responses[1].state == responses[2].state == responses[0].state, "initial state differs")
    require(responses[1].pattern_size == expected["tick_hz"], "clock differs")
    require([r.state for r in responses[3:]] == [c.STATE_IDLE, c.STATE_IDLE,
            c.STATE_RECEIVING, c.STATE_RECEIVING, c.STATE_RECEIVING, c.STATE_READY, c.STATE_RUNNING, c.STATE_DONE],
            "state grammar differs")
    for response in responses[3:7]:
        require(all(getattr(response, key) == 0 for key in ("active_pins", "pattern_size", "event_count",
                "run_progress", "sample_count", "fail_count", "result_flags")), "reset/upload fields differ")
    require(responses[7].run_progress == expected["size_bytes"], "payload ACK progress differs")
    require(all(getattr(responses[7], key) == 0 for key in ("active_pins", "pattern_size", "event_count",
            "sample_count", "fail_count", "result_flags")), "payload ACK fields differ")
    for response in responses[8:]:
        require((response.active_pins, response.pattern_size, response.event_count) ==
                (expected["active_pins"], expected["size_bytes"], expected["events"]), "admission/terminal tags differ")
    for response in responses[8:10]:
        require(response.run_progress == response.sample_count == response.fail_count == response.result_flags == 0,
                "admission counters differ")
    terminal = responses[-1]
    require(asdict(terminal) == retained["terminal"] and terminal.run_progress == expected["events"]
            and terminal.sample_count == expected["samples"] and 0 <= terminal.fail_count <= expected["compares"],
            "terminal evidence/counts differ")
    flags = (4 if terminal.fail_count else 0) | (2 if terminal.fail_count > 341 else 0)
    require(terminal.result_flags == flags, "terminal flags differ")
    comparison = "MISMATCH" if terminal.fail_count else "MATCH" if expected["compares"] else "NOT_APPLICABLE"
    require(report["comparison"] == comparison, "comparison claim differs")
    require([item["event"] for item in report["lifecycle"]] == ["open.begin", "open.end", "close.begin", "close.end"],
            "open/close lifecycle differs")
    require(all(a["time_s"] <= b["time_s"] for a, b in zip(report["lifecycle"], report["lifecycle"][1:])),
            "lifecycle times differ")
    require(report["lifecycle"][1]["time_s"] <= records[0]["write_begin_s"] and
            records[-1]["read_end_s"] <= report["lifecycle"][2]["time_s"], "I/O outside lifecycle")
    # Bind actual upload/RUN command arguments independently of cached decoded responses.
    require(all((cmd.arg0, cmd.arg1, cmd.arg2, cmd.arg3) == (0, 0, 0, 0)
                for cmd in [*commands[:5], commands[-1]]), "unexpected preflight/reset/status args")
    begin, data, end, run = commands[5:9]
    require((begin.arg0, begin.arg1, begin.arg3) == (report["plan"]["storage"], expected["size_bytes"], 0)
            and (data.arg0, data.arg1, data.arg2, data.arg3) == (0, expected["size_bytes"], begin.arg2, 0)
            and (end.arg0, end.arg1, end.arg2, end.arg3) == (expected["size_bytes"], begin.arg2, 0, 0)
            and (run.arg0, run.arg1, run.arg2, run.arg3) == (0, 0, 0, 0), "upload/RUN args differ")
    return {"status": "CONSISTENT_SHORT_REPORT", "response_boundaries": len(records),
            "events": expected["events"], "samples": expected["samples"], "compares": expected["compares"],
            "comparison": comparison, "release_sequence_complete": False,
            "device_io_performed": False, "firmware_image_verified": False,
            "pattern_bytes_verified": False, "physical_authorization_verified": False,
            "waveform_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    try:
        result = audit_short_completed_report(read_run_result(args.report))
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, OSError, struct.error) as exc:
        result = {"status": "REJECT", "error": str(exc), "device_io_performed": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "CONSISTENT_SHORT_REPORT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
