from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from copy import deepcopy
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from stpg import constants as c
from stpg.crc32 import crc32
from stpg.protocol import StpgCommand, StpgStatusResponse
from stil.device_precheck import inspect_pattern_requirements
from stil.hold_state import derive_terminal_hold
from stpg_runner import (CancellationToken, CaptureMetadata, DeviceConfig, RunHooks,
                         SegmentRunContext, build_project, init_project,
                         physical_identity_key, prepare_run, public_summary, run_bundle)
from stpg_runner.artifacts import RunnerError, read_json, write_json
from stpg_runner.device import DeviceLease, open_serial, validate_port
from stpg_runner.execution import judge_status
from stpg_runner.execution_gate import check_execution_contract

pytestmark = pytest.mark.stpg_host_only
FIXTURES = Path(__file__).parent / "fixtures" / "runner"
DEVICE = DeviceConfig("MOCK-NO-DEVICE", 0x2E8A, 0xA, "PUBLIC-SYNTHETIC-ONLY", 0x40010204, 0x001F7BFF)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FirmwarePeer:
    """Packed-byte protocol model, not a target/scheduling/electrical simulator.

    Independently decodes the actual uploaded bytes. Models the existing
    command_dispatch/pattern_buffer contracts, including HOLD upload reset.
    No SDK high-level judge or plan-supplied expected response is used.
    """

    timeout = 2.0

    def __init__(self, *, mutate=None, corrupt_at=0, partial_at=0, prefix=0,
                 fragment=64, terminal_error=0, fails=0, nonterminal=0,
                 write_fault=0, short_write=False, close_error=False, read_delay=None):
        self.mutate, self.corrupt_at = mutate, corrupt_at
        self.partial_at, self.prefix, self.fragment = partial_at, prefix, fragment
        self.terminal_error, self.fails, self.nonterminal = terminal_error, fails, nonterminal
        self.write_fault, self.short_write, self.close_error = write_fault, short_write, close_error
        self.read_delay = read_delay
        self.response_count = self.write_count = self.close_count = 0
        self.commands, self.payloads = [], []
        self.pending = None
        self.buffer = bytearray()
        self.rx = bytearray()
        self.holding = False
        self.hold = (0, 0)
        self.requirements = None
        self.run_flags = 0
        self.last = StpgStatusResponse(c.RSP_STATUS, 0, c.STATE_IDLE, DEVICE.capability, 0, 0,
                                       firmware_version=DEVICE.firmware_version)

    def _queue(self, response):
        self.response_count += 1
        if self.mutate:
            response = self.mutate(self.response_count, response)
        self.last = response
        raw = response.pack()
        if self.response_count == self.corrupt_at:
            raw = raw[:-1] + bytes([raw[-1] ^ 1])
        if self.response_count == self.partial_at:
            raw = raw[:self.prefix]
        self.rx.extend(raw)

    def write(self, data):
        assert not self.rx, "new request before consuming previous response"
        self.write_count += 1
        if self.write_count == self.write_fault:
            if self.short_write:
                return len(data) - 1
            raise OSError("unknown sent-byte prefix")
        if self.pending is not None:
            command = self.pending
            self.pending = None
            assert (command.arg0, command.arg1, command.arg2) == (len(self.buffer), len(data), crc32(data))
            self.buffer.extend(data)
            self.payloads.append(data)
            self._queue(replace(self.last, rsp=c.RSP_DATA_ACK, run_progress=len(self.buffer)))
            return len(data)
        command = StpgCommand.unpack(data)
        assert command.seq == len(self.commands) + 1
        self.commands.append(command)
        response = replace(self.last, seq=command.seq, rsp=c.RSP_OK)
        if command.cmd == c.CMD_INFO:
            assert command.arg0 == command.arg1 == command.arg2 == command.arg3 == 0
            response = replace(response, rsp=c.RSP_INFO)
        elif command.cmd == c.CMD_GET_CLOCK_INFO:
            response = replace(response, rsp=c.RSP_CLOCK_INFO, pattern_size=200_000_000,
                               event_count=6, run_progress=65535, result_flags=200_000_000,
                               fail_count=200_000_000)
        elif command.cmd == c.CMD_RESET:
            assert command.arg0 in (0, 1) and command.arg1 == command.arg2 == command.arg3 == 0
            self.holding = False
            self.requirements = None
            self.buffer.clear()
            response = replace(response, state=c.STATE_IDLE, active_pins=0, pattern_size=0,
                               event_count=0, run_progress=0, result_flags=0, fail_count=0, sample_count=0)
        elif command.cmd == c.CMD_UPLOAD_BEGIN:
            assert command.arg0 in (c.STORAGE_SRAM, c.STORAGE_PSRAM) and command.arg3 == 0
            assert response.state in (c.STATE_IDLE, c.STATE_HOLD_AWAITING_NEXT, c.STATE_DONE)
            self.buffer.clear()
            self.total_size, self.total_crc = command.arg1, command.arg2
            response = replace(response, state=c.STATE_RECEIVING, active_pins=0, pattern_size=0,
                               event_count=0, run_progress=0, result_flags=0, fail_count=0, sample_count=0)
        elif command.cmd == c.CMD_UPLOAD_DATA:
            assert command.arg0 == len(self.buffer) and 1 <= command.arg1 <= 4096 and command.arg3 == 0
            self.pending = command
            response = replace(response, rsp=c.RSP_READY_FOR_PAYLOAD, run_progress=len(self.buffer))
        elif command.cmd == c.CMD_UPLOAD_END:
            assert (command.arg0, command.arg1) == (self.total_size, self.total_crc)
            assert len(self.buffer) == self.total_size and crc32(bytes(self.buffer)) == self.total_crc
            self.requirements = inspect_pattern_requirements(bytes(self.buffer))
            req = self.requirements
            response = replace(response, state=c.STATE_HOLD_AWAITING_NEXT if self.holding else c.STATE_READY,
                               active_pins=req.active_pins, pattern_size=len(self.buffer),
                               event_count=req.estimated_event_count, run_progress=0)
        elif command.cmd == c.CMD_RUN:
            self.run_flags = command.arg0
            assert command.arg3 == 0
            if self.holding:
                assert command.arg0 & c.RUN_FLAG_PRESERVE_GPIO_STATE
                assert (command.arg1, command.arg2) == self.hold
            else:
                assert command.arg1 == command.arg2 == 0
                assert not command.arg0 & c.RUN_FLAG_PRESERVE_GPIO_STATE
            self.hold = derive_terminal_hold(bytes(self.buffer), *self.hold if self.holding else (0, 0))
            response = replace(response, state=c.STATE_RUNNING, run_progress=0)
        elif command.cmd == c.CMD_GET_STATUS:
            response = replace(response, rsp=c.RSP_STATUS)
            if self.requirements and response.state == c.STATE_RUNNING:
                req = self.requirements
                if self.nonterminal:
                    self.nonterminal -= 1
                    response = replace(response, run_progress=min(1, req.estimated_event_count))
                elif self.terminal_error:
                    response = replace(response, state=c.STATE_ERROR, error_code=self.terminal_error,
                                       run_progress=1, sample_count=0, result_flags=0, fail_count=0)
                else:
                    self.holding = bool(self.run_flags & c.RUN_FLAG_SEGMENT_END_HOLD)
                    flags = (4 if self.fails else 0) | (2 if self.fails > 341 else 0)
                    response = replace(response, state=c.STATE_HOLD_AWAITING_NEXT if self.holding else c.STATE_DONE,
                                       run_progress=req.estimated_event_count, sample_count=req.estimated_sample_count,
                                       fail_count=self.fails, result_flags=flags)
            elif self.requirements is None:
                # GET_CLOCK_INFO overloads fields; STATUS restores ordinary state.
                response = replace(response, pattern_size=0, event_count=0, run_progress=0,
                                   result_flags=0, fail_count=0, sample_count=0)
        elif command.cmd == c.CMD_READ_FAIL_LOG:
            assert response.state in (c.STATE_DONE, c.STATE_HOLD_AWAITING_NEXT)
            offset, count = command.arg0, command.arg1
            assert command.arg2 == command.arg3 == 0 and 1 <= count <= 2
            assert offset + count <= min(self.fails, 341)
            # Deliberately synthetic compare data; not sampled DUT values.
            payload = b"".join(struct.pack("<IIII", i, 1, 0, 1) for i in range(offset, offset + count))
            terminal = self.last
            self._queue(replace(response, rsp=c.RSP_READY_FOR_READ, pattern_size=len(payload),
                                event_count=count, run_progress=offset, result_flags=1))
            self.rx.extend(payload)
            self._queue(replace(response, rsp=c.RSP_DATA_END, pattern_size=len(payload),
                                event_count=crc32(payload), run_progress=offset, result_flags=1))
            self.last = terminal
            return len(data)
        else:
            raise AssertionError(f"unauthorized model command {command.cmd:#x}")
        self._queue(response)
        return len(data)

    def read(self, size):
        if self.read_delay:
            self.read_delay(self.response_count)
        size = min(size, self.fragment)
        part = bytes(self.rx[:size])
        del self.rx[:size]
        return part

    def close(self):
        self.close_count += 1
        if self.close_error:
            raise OSError("injected close error")


def ports():
    return [SimpleNamespace(device=DEVICE.port, vid=DEVICE.vid, pid=DEVICE.pid,
                            serial_number=DEVICE.serial_number)]


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("runner-wire")
    project = init_project(FIXTURES / "pattern.stil", root / "project", wait_preset="none", completion="release")
    return build_project(project)


def execute(bundle, tmp_path, peer=None, *, clock=None, **kwargs):
    peer = peer or FirmwarePeer()
    clock = clock or Clock()
    report = run_bundle(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE,
                        output_dir=tmp_path / "result", confirm_run=True, stream_factory=lambda _: peer,
                        port_provider=ports, clock=clock, sleep=clock.sleep, **kwargs)
    return report, peer


@pytest.fixture(autouse=True)
def forbid_real_device(monkeypatch):
    import stpg_runner.execution as execution

    def forbidden(*args, **kwargs):
        raise AssertionError("host test attempted real device access")

    monkeypatch.setattr(execution, "open_serial", forbidden)
    monkeypatch.setattr(execution, "enumerate_ports", forbidden)


def test_successful_exact_wire_and_release(bundle, tmp_path):
    report, peer = execute(bundle, tmp_path)
    assert report["outcome"] == "COMPLETED", report["errors"]
    assert report["comparison"] == "NOT_APPLICABLE"
    assert report["release_sequence_complete"] is True
    assert report["electrical_release_verified"] is False
    assert report["firmware_validation_pass"] is False
    assert report["result_persisted"] is True
    assert peer.close_count == report["close_attempts"] == report["close_successes"] == 1
    assert [cmd.cmd for cmd in peer.commands] == [c.CMD_INFO, c.CMD_GET_CLOCK_INFO, c.CMD_GET_STATUS,
        c.CMD_RESET, c.CMD_GET_STATUS, c.CMD_UPLOAD_BEGIN, c.CMD_UPLOAD_DATA, c.CMD_UPLOAD_END,
        c.CMD_RUN, c.CMD_GET_STATUS, c.CMD_RESET, c.CMD_GET_STATUS, c.CMD_INFO]
    assert peer.response_count == len(report["transactions"]) == 14
    assert read_json(tmp_path / "result" / "run.json.gz") == report
    for record in report["transactions"]:
        assert record["write_begin_s"] <= record["write_end_s"] <= record["read_begin_s"] <= record["read_end_s"]
        assert len(bytes.fromhex(record["raw_response_hex"])) == 64
    summary = public_summary(report)
    assert not {"transactions", "device", "plan", "port_metadata"} & summary.keys()
    summary["errors"].append("not shared")
    assert report["errors"] == []


@pytest.mark.parametrize("boundary", range(1, 15))
def test_crc_rejects_every_wire_boundary_without_later_request(bundle, tmp_path, boundary):
    report, peer = execute(bundle, tmp_path, FirmwarePeer(corrupt_at=boundary))
    assert report["outcome"] == "REJECT"
    assert peer.response_count == boundary
    assert peer.close_count == 1
    assert len(report["transactions"][-1]["raw_response_hex"]) == 128
    assert "bad response crc" in report["errors"][-1]


@pytest.mark.parametrize("boundary", range(1, 15))
@pytest.mark.parametrize("field,value", [("seq", 999), ("firmware_version", 0), ("capability", 0)])
def test_exact_identity_seq_at_every_boundary(bundle, tmp_path, boundary, field, value):
    peer = FirmwarePeer(mutate=lambda index, response: replace(response, **{field: value}) if index == boundary else response)
    report, peer = execute(bundle, tmp_path, peer)
    assert report["outcome"] == "REJECT"
    assert peer.response_count == boundary
    assert peer.close_count == 1


@pytest.mark.parametrize("prefix", range(64))
def test_every_partial_response_prefix_preserved_no_retry(bundle, tmp_path, prefix):
    report, peer = execute(bundle, tmp_path, FirmwarePeer(partial_at=10, prefix=prefix, fragment=7))
    assert report["outcome"] == "REJECT"
    assert peer.response_count == 10
    assert len(bytes.fromhex(report["transactions"][-1]["raw_response_hex"])) == prefix
    assert peer.commands[-1].cmd == c.CMD_RUN
    assert peer.close_count == 1


@pytest.mark.parametrize("fragment", [1, 3, 7, 63])
def test_fragmented_success(bundle, tmp_path, fragment):
    report, _ = execute(bundle, tmp_path, FirmwarePeer(fragment=fragment))
    assert report["outcome"] == "COMPLETED", report["errors"]


@pytest.mark.parametrize("boundary", range(1, 15))
@pytest.mark.parametrize("field", ["rsp", "state", "error_code"])
def test_code_state_error_guard_at_every_wire_boundary(bundle, tmp_path, boundary, field):
    def mutate(index, response):
        if index != boundary:
            return response
        value = response.rsp ^ 1 if field == "rsp" else c.STATE_BOOT if field == "state" else 6
        return replace(response, **{field: value})

    report, peer = execute(bundle, tmp_path, FirmwarePeer(mutate=mutate))
    assert report["outcome"] == "REJECT"
    assert peer.response_count == boundary and peer.close_count == 1


@pytest.mark.parametrize("field,value", [
    ("rsp", c.RSP_OK), ("pattern_size", 0), ("event_count", 0), ("active_pins", 24),
    ("run_progress", 0), ("run_progress", 5), ("run_progress", 7), ("sample_count", 1),
    ("sample_count", 3), ("fail_count", 1), ("result_flags", 1), ("result_flags", 4),
    ("state", c.STATE_IDLE), ("state", c.STATE_HOLD_AWAITING_NEXT), ("error_code", 9),
])
def test_incomplete_or_wrong_nonzero_done_rejected_at_actual_callsite(bundle, tmp_path, field, value):
    peer = FirmwarePeer(mutate=lambda index, response: replace(response, **{field: value}) if index == 11 else response)
    report, peer = execute(bundle, tmp_path, peer)
    assert report["outcome"] == "REJECT"
    assert peer.commands[-1].cmd == c.CMD_GET_STATUS
    assert not report["release_command_attempted"]
    assert peer.response_count == 11


@pytest.mark.parametrize("error", [6, 9, 48, 50, 255])
def test_terminal_error_seals_without_diagnostic_or_release(bundle, tmp_path, error):
    report, peer = execute(bundle, tmp_path, FirmwarePeer(terminal_error=error))
    assert report["outcome"] == "DEVICE_ERROR"
    assert report["segments"][0]["terminal"]["error_code"] == error
    assert report["comparison"] == "UNOBSERVED"
    assert peer.response_count == 11 and peer.close_count == 1
    assert not report["release_command_attempted"]


@pytest.mark.parametrize("write_fault", range(1, 15))
@pytest.mark.parametrize("short", [False, True])
def test_failed_or_short_write_never_resent(bundle, tmp_path, write_fault, short):
    report, peer = execute(bundle, tmp_path, FirmwarePeer(write_fault=write_fault, short_write=short))
    assert report["outcome"] == "REJECT"
    assert peer.write_count == write_fault and peer.close_count == 1
    if short:
        assert report["transactions"][-1]["written_bytes"] is not None
    else:
        assert report["transactions"][-1]["written_bytes"] is None


def test_close_failure_rejects_and_preserves_completed_prefix(bundle, tmp_path):
    report, peer = execute(bundle, tmp_path, FirmwarePeer(close_error=True))
    assert report["outcome"] == "REJECT" and report["completed_segments"] == 1
    assert report["comparison"] == "UNOBSERVED"
    assert peer.close_count == 1 and report["close_successes"] == 0


def test_rejects_same_reservation_before_second_open(bundle, tmp_path):
    first, _ = execute(bundle, tmp_path)
    assert first["outcome"] == "COMPLETED"
    with pytest.raises(FileExistsError):
        execute(bundle, tmp_path)


def test_cancellation_after_payload_is_not_stop_or_release(bundle, tmp_path):
    token = CancellationToken()

    def progress(event):
        if event.stage == "upload" and event.event == "progress":
            token.cancel()

    report, peer = execute(bundle, tmp_path, cancel=token, on_progress=progress)
    assert report["outcome"] == "CANCELLED"
    assert peer.response_count == 8 and peer.close_count == 1
    assert peer.commands[-1].cmd == c.CMD_UPLOAD_DATA
    assert not report["release_command_attempted"]


def test_callback_exception_after_payload_seals(bundle, tmp_path):
    def callback(event):
        if event.stage == "upload" and event.event == "progress":
            raise RuntimeError("injected callback failure")

    report, peer = execute(bundle, tmp_path, on_progress=callback)
    assert report["outcome"] == "REJECT"
    assert peer.response_count == 8 and peer.close_count == 1


def test_checkpoint_failure_stops_before_run(bundle, tmp_path, monkeypatch):
    import stpg_runner.execution as execution
    real = execution.write_run_result

    def checkpoint(path, report):
        if report["transactions"] and report["transactions"][-1].get("command") == c.CMD_UPLOAD_END:
            raise OSError("injected disk full")
        real(path, report)

    monkeypatch.setattr(execution, "write_run_result", checkpoint)
    report, peer = execute(bundle, tmp_path)
    assert report["outcome"] == "REJECT"
    assert peer.commands[-1].cmd == c.CMD_UPLOAD_END and peer.close_count == 1
    assert "final checkpoint" in report["errors"][-1]
    assert report["result_persisted"] is False


def test_final_checkpoint_failure_cannot_publish_stale_completed(bundle, tmp_path, monkeypatch):
    import stpg_runner.execution as execution
    real = execution.write_run_result

    def checkpoint(path, report):
        if report["outcome"] == "COMPLETED":
            raise OSError("injected final checkpoint failure")
        real(path, report)

    monkeypatch.setattr(execution, "write_run_result", checkpoint)
    report, peer = execute(bundle, tmp_path)
    assert report["outcome"] == "REJECT" and report["result_persisted"] is False
    assert peer.close_count == 1
    assert read_json(tmp_path / "result" / "run.json.gz")["outcome"] == "PENDING"


def test_final_checkpoint_interrupt_preserves_truth_about_device_contact(bundle, tmp_path, monkeypatch):
    import stpg_runner.execution as execution
    real = execution.write_run_result

    def checkpoint(path, report):
        if report["outcome"] == "COMPLETED":
            raise KeyboardInterrupt()
        real(path, report)

    monkeypatch.setattr(execution, "write_run_result", checkpoint)
    report, peer = execute(bundle, tmp_path)
    assert report["outcome"] == "REJECT" and report["stream_opened"] is True
    assert report["result_persisted"] is False and peer.close_count == 1
    assert "KeyboardInterrupt" in report["errors"][-1]


def test_deadline_after_sleep_prevents_status(bundle, tmp_path):
    class ExpiredClock(Clock):
        def sleep(self, seconds):
            self.now += 60

    report, peer = execute(bundle, tmp_path, clock=ExpiredClock())
    assert report["outcome"] == "REJECT"
    assert peer.commands[-1].cmd == c.CMD_RUN
    assert "after sleep" in report["errors"][-1]


def test_poll_limit_is_not_a_permanent_stall_claim(bundle, tmp_path):
    clock = Clock()
    clock.sleep = lambda _: None
    report, peer = execute(bundle, tmp_path, FirmwarePeer(nonterminal=1000), clock=clock)
    assert report["outcome"] == "REJECT"
    assert peer.response_count == 10 + 300
    assert "poll limit" in report["errors"][-1]


def test_late_started_response_retains_raw_and_rejects(bundle, tmp_path):
    from stpg_runner.execution import STATUS_RESPONSE_TIMEOUT_S
    clock = Clock()

    def delay(boundary):
        if boundary == 11:
            clock.now += STATUS_RESPONSE_TIMEOUT_S + 0.1

    report, peer = execute(bundle, tmp_path, FirmwarePeer(read_delay=delay), clock=clock)
    assert report["outcome"] == "REJECT" and peer.response_count == 11
    assert len(report["transactions"][-1]["raw_response_hex"]) == 128


def test_segmented_hold_arguments_and_summary_checkpoint(tmp_path):
    project = init_project(FIXTURES / "pattern.stil", tmp_path / "project", wait_preset="none",
                           completion="hold", max_segment_vectors=2)
    bundle = build_project(project)
    events = []

    def progress(event):
        events.append(event)
        if event.stage == "segment":
            disk = read_json(tmp_path / "result" / "run.json.gz")
            assert len(disk["segments"]) == event.completed

    report, peer = execute(bundle, tmp_path, on_progress=progress)
    assert report["outcome"] == "COMPLETED", report["errors"]
    assert [item["terminal"]["state"] for item in report["segments"]] == [c.STATE_HOLD_AWAITING_NEXT, c.STATE_DONE]
    runs = [cmd for cmd in peer.commands if cmd.cmd == c.CMD_RUN]
    assert runs[0].arg0 == c.RUN_FLAG_STREAMING_POC | c.RUN_FLAG_SEGMENT_END_HOLD
    assert runs[1].arg0 == c.RUN_FLAG_STREAMING_POC | c.RUN_FLAG_PRESERVE_GPIO_STATE
    assert (runs[1].arg1, runs[1].arg2) == derive_terminal_hold(peer.payloads[0])
    assert sum(cmd.cmd == c.CMD_RESET for cmd in peer.commands) == 1
    assert not report["release_command_attempted"] and peer.close_count == 1


def test_later_segment_error_preserves_prefix_but_never_releases(tmp_path):
    project = init_project(FIXTURES / "pattern.stil", tmp_path / "project", wait_preset="none",
                           completion="release", max_segment_vectors=2)
    bundle = build_project(project)

    def mutate(index, response):
        if response.state == c.STATE_DONE and response.rsp == c.RSP_STATUS:
            return replace(response, state=c.STATE_ERROR, error_code=9, sample_count=0)
        return response

    report, peer = execute(bundle, tmp_path, FirmwarePeer(mutate=mutate))
    assert report["outcome"] == "DEVICE_ERROR" and report["completed_segments"] == 1
    assert [item["outcome"] for item in report["segments"]] == ["COMPLETED", "DEVICE_ERROR"]
    assert sum(cmd.cmd == c.CMD_RESET for cmd in peer.commands) == 1
    assert not report["release_command_attempted"]


def test_three_segment_multi_chunk_uploads(tmp_path):
    source = tmp_path / "multi.stil"
    text = (FIXTURES / "pattern.stil").read_text(encoding="utf-8")
    text = text[:text.index("Pattern MAIN")] + "Pattern MAIN { W NORMAL;\n" + "V { ALL = 1X; } V { ALL = 0X; }\n" * 600 + "}\n"
    source.write_text(text, encoding="utf-8")
    project = init_project(source, tmp_path / "project", wait_preset="none", completion="release", max_segment_vectors=550)
    bundle = build_project(project)
    plan = prepare_run(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256)
    assert len(plan.segments) == 3
    report, peer = execute(bundle, tmp_path)
    assert report["outcome"] == "COMPLETED", report["errors"]
    expected_acks = sum((len(segment.payload) + 4095) // 4096 for segment in plan.segments)
    assert len(peer.payloads) == expected_acks > 3
    assert all(len(payload) <= 4096 for payload in peer.payloads)
    assert report["completed_segments"] == 3


@pytest.fixture(scope="module")
def compare_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("runner-compares")
    source = root / "public.stil"
    text = (FIXTURES / "pattern.stil").read_text(encoding="utf-8")
    text = text.replace("X: 100ns Compare(X);", "X: 100ns Compare(X); L: 100ns Compare(0);")
    text = text[:text.index("Pattern MAIN")] + "Pattern MAIN { W NORMAL; Loop 500 { V { ALL = 1L; } } }\n"
    source.write_text(text, encoding="utf-8")
    project = init_project(source, root / "project", wait_preset="none", completion="hold")
    return build_project(project)


@pytest.mark.parametrize("fails", [0, 1, 341, 342, 500])
def test_compare_outcome_at_actual_run_callsite(compare_bundle, tmp_path, fails):
    report, peer = execute(compare_bundle, tmp_path, FirmwarePeer(fails=fails))
    assert report["outcome"] == "COMPLETED", report["errors"]
    assert report["comparison"] == ("MISMATCH" if fails else "MATCH")
    assert report["firmware_validation_pass"] is False
    assert not report["sample_payload_collection"]
    assert report["segments"][0]["expected"]["compares"] == 500


def test_sram_raw_execution_and_segmented_sample_rejection(tmp_path):
    project = init_project(FIXTURES / "pattern.stil", tmp_path / "project", wait_preset="none",
                           completion="hold", storage="sram", compile_mode="raw-event", loop_policy="expand")
    bundle = build_project(project)
    report, peer = execute(bundle, tmp_path)
    assert report["outcome"] == "COMPLETED", report["errors"]
    assert next(cmd for cmd in peer.commands if cmd.cmd == c.CMD_UPLOAD_BEGIN).arg0 == c.STORAGE_SRAM
    assert next(cmd for cmd in peer.commands if cmd.cmd == c.CMD_RUN).arg0 == 0

    project = init_project(FIXTURES / "pattern.stil", tmp_path / "raw-segments", wait_preset="none",
                           completion="hold", compile_mode="raw-event", loop_policy="expand", max_segment_vectors=2)
    bundle = build_project(project)
    with pytest.raises(RunnerError, match="raw drive-only"):
        prepare_run(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256)


@pytest.mark.parametrize("sample_count", [6551, 6552])
def test_decoded_sample_boundary_survives_streaming_plan_and_judge(tmp_path, sample_count):
    source = tmp_path / "public.stil"
    template = (FIXTURES / "pattern.stil").read_text(encoding="utf-8")
    template = template[:template.index("Pattern MAIN")] + f"Pattern MAIN {{ W NORMAL; Loop {sample_count} {{ V {{ ALL = 1X; }} }} }}"
    source.write_text(template, encoding="utf-8")
    project = init_project(source, tmp_path / "project", wait_preset="none", completion="hold")
    result = build_project(project)
    plan = prepare_run(result.bundle_dir, manifest_sha256=result.manifest_sha256)
    assert plan.segments[0].expected["samples"] == sample_count
    assert plan.segments[0].run_args[0] == c.RUN_FLAG_STREAMING_POC
    report, _ = execute(result, tmp_path)
    assert report["outcome"] == "COMPLETED", report["errors"]


@pytest.mark.parametrize("fails,flags", [(0, 0), (1, 4), (341, 4), (342, 6), (1000, 6)])
def test_compare_flags_boundaries(fails, flags):
    expected = {"size_bytes": 400, "active_pins": 24, "events": 4000, "samples": 1000, "compares": 1000}
    response = StpgStatusResponse(c.RSP_STATUS, 1, c.STATE_DONE, DEVICE.capability, 0, 24, 400, 4000,
                                  4000, flags, fails, 1000, 0, DEVICE.firmware_version)
    assert judge_status(response, expected, terminal_state=c.STATE_DONE) == "TERMINAL"
    with pytest.raises(RunnerError):
        judge_status(replace(response, result_flags=flags ^ 2), expected, terminal_state=c.STATE_DONE)


@pytest.mark.parametrize("changes", [{"schema_version": True}, {"port": ""}, {"port": " bad "},
                                     {"serial_number": "x\n"}, {"vid": True}, {"pid": 0},
                                     {"capability": -1}, {"firmware_version": 2 ** 32}, {"extra": 1}])
def test_device_config_rejects_invalid_or_unknown(changes):
    with pytest.raises(RunnerError):
        DeviceConfig.from_dict({**DEVICE.to_dict(), **changes})


def test_binding_mismatch_and_missing_ports_never_open(bundle, tmp_path):
    called = []
    report = run_bundle(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE,
                        output_dir=tmp_path / "result", confirm_run=True, port_provider=lambda: [],
                        stream_factory=lambda _: called.append(True))
    assert report["outcome"] == "REJECT"
    assert not called and not report["stream_open_attempted"]


def test_identity_lease_rejects_other_config_alias():
    with DeviceLease(DEVICE):
        with pytest.raises(RunnerError):
            with DeviceLease(replace(DEVICE, port="another-port")):
                pytest.fail("conflicting identity lease admitted")
    with DeviceLease(DEVICE):
        pass


def test_port_metadata_rejects_wrong_or_ambiguous():
    assert validate_port(DEVICE, ports())["port"] == DEVICE.port
    with pytest.raises(RunnerError):
        validate_port(DEVICE, ports() + ports())
    wrong = ports()
    wrong[0].serial_number = "WRONG"
    with pytest.raises(RunnerError):
        validate_port(DEVICE, wrong)


def test_explicit_nested_port_alias_is_not_an_alternate_endpoint(tmp_path):
    endpoint = tmp_path / "tty-fixture"
    endpoint.touch()
    alias = tmp_path / "by-id" / "public-synthetic"
    alias.parent.mkdir()
    alias.symlink_to(endpoint)
    config = replace(DEVICE, port=str(alias))
    metadata = [SimpleNamespace(device=str(endpoint), vid=config.vid, pid=config.pid,
                                serial_number=config.serial_number)]
    assert validate_port(config, metadata)["port"] == str(alias)
    metadata.append(SimpleNamespace(device=str(alias), vid=config.vid, pid=config.pid,
                                    serial_number=config.serial_number))
    assert validate_port(config, metadata)["metadata_port"] == str(endpoint)
    other = tmp_path / "other-endpoint"
    other.touch()
    metadata.append(SimpleNamespace(device=str(other), vid=config.vid, pid=config.pid,
                                    serial_number=config.serial_number))
    with pytest.raises(RunnerError, match="ambiguous"):
        validate_port(config, metadata)


@pytest.mark.parametrize("state", [c.STATE_RUNNING, c.STATE_RUN_DRAINING, c.STATE_ERROR,
                                   c.STATE_HOLD_AWAITING_NEXT, c.STATE_RECEIVING, c.STATE_READY])
def test_nonquiescent_initial_state_rejects_before_reset(bundle, tmp_path, state):
    peer = FirmwarePeer(mutate=lambda index, response: replace(response, state=state) if index == 1 else response)
    report, peer = execute(bundle, tmp_path, peer)
    assert report["outcome"] == "REJECT" and peer.response_count == 1
    assert peer.commands[-1].cmd == c.CMD_INFO


def test_clock_mismatch_rejects_before_reset(bundle, tmp_path):
    peer = FirmwarePeer(mutate=lambda index, response: replace(response, pattern_size=100_000_000) if index == 2 else response)
    report, peer = execute(bundle, tmp_path, peer)
    assert report["outcome"] == "REJECT" and peer.response_count == 3
    assert not any(cmd.cmd == c.CMD_RESET for cmd in peer.commands)


def test_cancelled_before_open_does_not_enumerate(bundle, tmp_path):
    token = CancellationToken()
    token.cancel()
    report, peer = execute(bundle, tmp_path, cancel=token)
    assert report["outcome"] == "CANCELLED" and report["close_attempts"] == 0
    assert not report["stream_open_attempted"] and peer.write_count == 0


def test_cli_plan_and_injected_run_parity(bundle, tmp_path, monkeypatch, capsys):
    import stpg_runner.cli as cli
    device_path = tmp_path / "device.json"
    write_json(device_path, DEVICE.to_dict())
    assert cli.main(["plan", str(bundle.bundle_dir), "--manifest-sha256", bundle.manifest_sha256]) == 0
    assert json.loads(capsys.readouterr().out)["segments"][0]["expected"]["events"] == 6
    peer, clock = FirmwarePeer(), Clock()

    def injected(*args, **kwargs):
        return run_bundle(*args, **kwargs, stream_factory=lambda _: peer, port_provider=ports,
                          clock=clock, sleep=clock.sleep)

    monkeypatch.setattr(cli, "run_bundle", injected)
    assert cli.main(["run", str(bundle.bundle_dir), "--manifest-sha256", bundle.manifest_sha256,
                     "--device-config", str(device_path), "--output-dir", str(tmp_path / "cli-result"),
                     "--confirm-run", "--progress", "jsonl"]) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["outcome"] == "COMPLETED" and summary["result_persisted"] is True
    assert "transactions" not in summary
    assert all(json.loads(line)["kind"] == "stpg_runner_progress" for line in captured.err.splitlines())


def test_serial_settings_only_while_closed(monkeypatch):
    import serial
    trace = []

    class SerialStub:
        def __init__(self, **kwargs):
            trace.append(("construct", kwargs))
            self.opened = False

        def __setattr__(self, key, value):
            if key in {"rts", "dtr", "port"}:
                assert not self.opened
                trace.append((key, value))
            object.__setattr__(self, key, value)

        def open(self):
            trace.append(("open",))
            self.opened = True

    monkeypatch.setattr(serial, "Serial", SerialStub)
    stream = open_serial(DEVICE)
    assert stream.opened
    assert [item[0] for item in trace] == ["construct", "rts", "port", "open"]
    import sys
    assert trace[0][1] == dict(port=None, baudrate=115200, timeout=0.05, write_timeout=2.0,
                             dsrdtr=sys.platform != "win32", rtscts=False, xonxoff=False)


@pytest.mark.parametrize("before,after", [
    ("MAX_POLLS = 300", "MAX_POLLS = 301"),
    ("self.sleep(POLL_INTERVAL_S)", "self.sleep(0.0)"),
    ("self.clock() >= deadline", "self.clock() > deadline"),
    ("self._reset(False)", "self._reset(True)"),
    ("self._start(segment)", "self._start(segment); self._start(segment)"),
    ("self._upload(segment)", "\n            for extra in range(2):\n                self._upload(segment)"),
    ("self._upload(segment)", "self._reset(False); self._upload(segment)"),
    ("self.plan.completion == \"release\"", "True"),
    ("outcome = judge_status(response, segment.expected, terminal_state=segment.terminal_state)", "outcome = 'TERMINAL'"),
    ("c.CMD_UPLOAD_BEGIN,", "c.CMD_STOP,"),
])
def test_execution_gate_detects_structural_mutations(before, after):
    import stpg_runner.execution as execution
    source = Path(execution.__file__).read_text(encoding="utf-8")
    assert before in source
    assert check_execution_contract(execution_source=source.replace(before, after))["status"] == "REJECT"


@pytest.mark.parametrize("before,after", [
    ('dsrdtr=sys.platform != "win32"', 'dsrdtr=True'),
    ('dsrdtr=sys.platform != "win32"', 'dsrdtr=False'),
    ("stream.rts = True", "stream.rts = True; stream.dtr = False"),
    ("stream.open()", "stream.open(); stream.open()"),
    ("stream.open()", "stream.open(); stream.reset_input_buffer()"),
])
def test_device_gate_rejects_line_control_or_extra_operations(before, after):
    import stpg_runner.device as device
    source = Path(device.__file__).read_text(encoding="utf-8")
    assert before in source
    assert check_execution_contract(device_source=source.replace(before, after))["status"] == "REJECT"


def test_plan_and_gate_are_noncontact_and_require_pin(bundle):
    gate = check_execution_contract()
    assert gate["status"] == "PASS", gate["errors"]
    assert gate["device_io_performed"] is False
    plan = prepare_run(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256).to_dict()
    assert plan["status"] == "HOST_ONLY_PLANNED"
    assert plan["physical_execution_authorized"] is False
    assert plan["segments"][0]["expected"]["events"] == 6
    with pytest.raises(RunnerError):
        prepare_run(bundle.bundle_dir, manifest_sha256="0" * 64)


def test_explicit_intent_required_before_any_device_access(bundle, tmp_path):
    with pytest.raises(RunnerError, match="confirm_run"):
        run_bundle(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE,
                   output_dir=tmp_path / "result")
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("segmented", [False, True])
def test_hook_order_immutable_context_and_persisted_terminal(tmp_path, monkeypatch, segmented):
    import stpg_runner.execution as execution
    project = init_project(FIXTURES / "pattern.stil", tmp_path / "project", wait_preset="none",
                           completion="release", max_segment_vectors=2 if segmented else None)
    built = build_project(project)
    plan = prepare_run(built.bundle_dir, manifest_sha256=built.manifest_sha256)
    peer, clock, trace = FirmwarePeer(), Clock(), []
    original_write = execution.write_run_result

    def checkpoint(path, report):
        trace.append(("checkpoint", peer.commands[-1].cmd if peer.commands else None))
        original_write(path, report)

    monkeypatch.setattr(execution, "write_run_result", checkpoint)

    def before(context):
        assert isinstance(context, SegmentRunContext)
        segment = plan.segments[context.segment_index]
        assert context.manifest_sha256 == built.manifest_sha256
        assert context.pattern_sha256 == segment.to_dict()["sha256"]
        assert context.run_args == segment.run_args
        assert (context.events, context.samples, context.compares) == tuple(
            segment.expected[key] for key in ("events", "samples", "compares"))
        assert peer.commands[-1].cmd == c.CMD_UPLOAD_END
        assert trace[-1] == ("checkpoint", c.CMD_UPLOAD_END)
        with pytest.raises(FrozenInstanceError):
            context.run_args = (0, 0, 0, 0)
        trace.append(("before", context.segment_index))

    def after(context, terminal):
        saved = read_json(tmp_path / "result" / "run.json.gz")
        assert saved["segments"][-1]["terminal"] == terminal
        assert terminal["state"] == context.terminal_state
        assert peer.commands[-1].cmd == c.CMD_GET_STATUS
        terminal["sample_count"] = -100  # must not change retained terminal evidence
        trace.append(("after", context.segment_index))

    report, _ = execute(built, tmp_path, peer, clock=clock, capture_hook=RunHooks(before, after),
                        capture_metadata=CaptureMetadata("a" * 64, "local/capture"))
    assert report["outcome"] == "COMPLETED", report["errors"]
    assert report["release_sequence_complete"]
    assert [event["phase"] for event in report["capture"]["hook_events"]] == [
        phase for _ in plan.segments for phase in ("before_run", "after_terminal")]
    assert all(event["outcome"] == "COMPLETED" and event["begin_s"] <= event["end_s"]
               for event in report["capture"]["hook_events"])
    assert report["capture"]["metadata"]["plan_sha256"] == "a" * 64
    assert "capture" not in public_summary(report)  # local path is not GUI/public evidence
    for terminal, segment in zip(report["segments"], plan.segments):
        assert terminal["terminal"]["sample_count"] == segment.expected["samples"]
    assert [tuple((cmd.arg0, cmd.arg1, cmd.arg2, cmd.arg3)) for cmd in peer.commands if cmd.cmd == c.CMD_RUN
            ] == [segment.run_args for segment in plan.segments]
    assert clock.sleeps == [0.2] * len(plan.segments)  # no hook sleep inserted
    assert read_json(tmp_path / "result" / "run.json.gz") == report


@pytest.mark.parametrize("phase", ["before_run", "after_terminal"])
@pytest.mark.parametrize("failure", ["exception", "return", "cancel", "interrupt"])
def test_hook_failure_seals_without_next_upload_or_release(tmp_path, phase, failure):
    project = init_project(FIXTURES / "pattern.stil", tmp_path / "project", wait_preset="none",
                           completion="release", max_segment_vectors=2)
    built = build_project(project)
    token = CancellationToken()

    def fail(*args):
        if failure == "exception":
            raise RuntimeError("synthetic hook failure")
        if failure == "return":
            return {"pretend": "accepted"}
        if failure == "interrupt":
            raise KeyboardInterrupt()
        token.cancel()

    report, peer = execute(built, tmp_path, capture_hook=RunHooks(**{phase: fail}), cancel=token)
    assert report["outcome"] == ("CANCELLED" if failure in {"cancel", "interrupt"} else "REJECT")
    assert sum(cmd.cmd == c.CMD_UPLOAD_BEGIN for cmd in peer.commands) == 1
    assert sum(cmd.cmd == c.CMD_RUN for cmd in peer.commands) == int(phase == "after_terminal")
    assert peer.commands[-1].cmd == (c.CMD_UPLOAD_END if phase == "before_run" else c.CMD_GET_STATUS)
    assert report["completed_segments"] == int(phase == "after_terminal")
    assert not report["release_command_attempted"]
    assert peer.close_count == report["close_attempts"] == report["close_successes"] == 1
    assert report["capture"]["hook_events"][-1]["outcome"] == "REJECT"
    assert read_json(tmp_path / "result" / "run.json.gz") == report


def test_terminal_error_does_not_invoke_terminal_hook(bundle, tmp_path):
    invoked = []
    report, peer = execute(bundle, tmp_path, FirmwarePeer(terminal_error=6),
                           capture_hook=RunHooks(lambda ctx: invoked.append("before"),
                                                 lambda ctx, terminal: invoked.append("after")))
    assert report["outcome"] == "DEVICE_ERROR"
    assert invoked == ["before"] and peer.commands[-1].cmd == c.CMD_GET_STATUS
    assert not report["release_command_attempted"]


@pytest.mark.parametrize("bad_boundary", [8, 9, 10, 11])
def test_invalid_upload_run_or_terminal_never_invokes_later_hook(bundle, tmp_path, bad_boundary):
    invoked = []
    report, peer = execute(bundle, tmp_path, FirmwarePeer(corrupt_at=bad_boundary),
                           capture_hook=RunHooks(lambda ctx: invoked.append("before"),
                                                 lambda ctx, term: invoked.append("after")))
    assert report["outcome"] == "REJECT"
    assert invoked == (["before"] if bad_boundary >= 10 else [])
    assert peer.response_count == bad_boundary and peer.close_count == 1


@pytest.mark.parametrize("phase", ["before_run", "after_terminal"])
def test_checkpoint_failure_blocks_hook_or_next_command(bundle, tmp_path, monkeypatch, phase):
    import stpg_runner.execution as execution
    original_write, invoked = execution.write_run_result, []

    def persist(path, report):
        records = report["transactions"]
        if phase == "before_run" and records and records[-1].get("command") == c.CMD_UPLOAD_END:
            raise OSError("upload checkpoint failure")
        if phase == "after_terminal" and report.get("capture", {}).get("hook_events"):
            if report["capture"]["hook_events"][-1]["phase"] == "after_terminal":
                raise OSError("hook checkpoint failure")
        original_write(path, report)

    monkeypatch.setattr(execution, "write_run_result", persist)
    report, peer = execute(bundle, tmp_path, capture_hook=RunHooks(
        lambda ctx: invoked.append("before"), lambda ctx, term: invoked.append("after")))
    assert report["outcome"] == "REJECT"
    assert invoked == ([] if phase == "before_run" else ["before", "after"])
    assert report["completed_segments"] == int(phase == "after_terminal")
    assert peer.close_count == 1 and not report["release_command_attempted"]


@pytest.mark.parametrize("hooks,metadata", [
    ({"before_run": "command"}, None), (RunHooks(), None), (RunHooks(before_run=3), None),
    (None, CaptureMetadata("a" * 64, "capture")),
    (RunHooks(before_run=lambda ctx: None), {"plan_sha256": "a" * 64}),
    (RunHooks(before_run=lambda ctx: None), CaptureMetadata("A" * 64, "capture")),
    (RunHooks(before_run=lambda ctx: None), CaptureMetadata("0" * 63, "capture")),
    (RunHooks(before_run=lambda ctx: None), CaptureMetadata("0" * 64, " capture")),
    (RunHooks(before_run=lambda ctx: None), CaptureMetadata("0" * 64, "capture\n")),
])
def test_invalid_hook_configuration_rejects_before_result_or_metadata_io(bundle, tmp_path, hooks, metadata):
    def forbidden():
        pytest.fail("invalid hook config reached port enumeration")

    with pytest.raises(RunnerError):
        run_bundle(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE,
                   output_dir=tmp_path / "result", confirm_run=True, capture_hook=hooks,
                   capture_metadata=metadata, port_provider=forbidden)
    assert not (tmp_path / "result").exists()


def test_cross_role_lease_ignores_untrusted_role_specific_key():
    analyzer = SimpleNamespace(vid=DEVICE.vid, pid=DEVICE.pid, serial_number=DEVICE.serial_number,
                               port="other-role-alias", lease_key="analyzer-namespace-bypass")
    assert physical_identity_key(analyzer) == DEVICE.lease_key
    with DeviceLease(DEVICE):
        with pytest.raises(RunnerError, match="leased"):
            with DeviceLease(analyzer):
                pytest.fail("cross-role duplicate ownership admitted")
    with DeviceLease(analyzer):
        with pytest.raises(RunnerError, match="leased"):
            with DeviceLease(DEVICE):
                pytest.fail("reverse cross-role duplicate ownership admitted")


@pytest.mark.parametrize("field,value", [("vid", True), ("pid", 0), ("serial_number", " "),
                                        ("serial_number", "bad\n")])
def test_lease_identity_validation(field, value):
    config = SimpleNamespace(vid=DEVICE.vid, pid=DEVICE.pid, serial_number=DEVICE.serial_number)
    setattr(config, field, value)
    with pytest.raises(RunnerError, match="identity"):
        DeviceLease(config)


@pytest.mark.parametrize("before,after", [
    ('self._invoke_hook("before_run", segment)', 'self._invoke_hook("after_terminal", segment)'),
    ('self._invoke_hook("before_run", segment)', 'self._invoke_hook("before_run", segment); self.checkpoint()'),
    ('self._invoke_hook("before_run", segment)', 'self._invoke_hook("before_run", segment); self.emit("x", "y")'),
    ('self._invoke_hook("before_run", segment)', 'self._invoke_hook("before_run", segment); self.sleep(0.1)'),
    ('self._invoke_hook("after_terminal", segment, terminal)', 'self._invoke_hook("before_run", segment)'),
    ('if terminal.state == c.STATE_ERROR:', 'if False:'),
    ('self._invoke_hook("before_run", segment)', 'None'),
    ('self._invoke_hook("before_run", segment)', 'self._invoke_hook("before_run", segment); self._invoke_hook("before_run", segment)'),
    ('result = callback(context) if terminal is None else callback(context, asdict(terminal))',
     'result = callback(context) if terminal is None else callback(context, asdict(terminal)); callback(context)'),
])
def test_gate_rejects_hook_boundary_mutations(before, after):
    import stpg_runner.execution as execution
    source = Path(execution.__file__).read_text(encoding="utf-8")
    assert before in source
    assert check_execution_contract(execution_source=source.replace(before, after))["status"] == "REJECT"


def test_gate_rejects_role_prefixed_lease_key():
    import stpg_runner.device as device
    source = Path(device.__file__).read_text(encoding="utf-8")
    assert check_execution_contract(device_source=source.replace("physical_identity_key(config)", "config.lease_key"))[
        "status"] == "REJECT"


@pytest.mark.parametrize("phase", ["before_run", "after_terminal"])
def test_later_hook_failure_preserves_completed_prefix(tmp_path, phase):
    project = init_project(FIXTURES / "pattern.stil", tmp_path / "project", wait_preset="none",
                           completion="release", max_segment_vectors=2)
    built = build_project(project)

    def fail_on_second(context, *args):
        if context.segment_index == 1:
            raise RuntimeError("later hook failed")

    report, peer = execute(built, tmp_path, capture_hook=RunHooks(**{phase: fail_on_second}))
    assert report["outcome"] == "REJECT" and report["comparison"] == "UNOBSERVED"
    assert report["completed_segments"] == (1 if phase == "before_run" else 2)
    assert report["segments"][0]["terminal"]["state"] == c.STATE_HOLD_AWAITING_NEXT
    assert peer.close_count == 1 and not report["release_command_attempted"]
    assert sum(cmd.cmd == c.CMD_UPLOAD_BEGIN for cmd in peer.commands) == 2
    assert sum(cmd.cmd == c.CMD_RUN for cmd in peer.commands) == (1 if phase == "before_run" else 2)


@pytest.fixture(scope="module")
def short_hold_report(tmp_path_factory):
    root = tmp_path_factory.mktemp("report-audit")
    project = init_project(FIXTURES / "pattern.stil", root / "project", wait_preset="none", completion="hold")
    built = build_project(project)
    report, _ = execute(built, root)
    assert report["outcome"] == "COMPLETED"
    return report


def test_offline_short_report_audit_limits(short_hold_report):
    from helpers.audit_runner_result import audit_short_completed_report
    result = audit_short_completed_report(short_hold_report)
    assert result["status"] == "CONSISTENT_SHORT_REPORT" and result["response_boundaries"] == 11
    assert result["events"] == 6 and result["samples"] == 2
    assert all(result[key] is False for key in ("device_io_performed", "firmware_image_verified",
               "pattern_bytes_verified", "physical_authorization_verified", "waveform_verified"))


@pytest.mark.parametrize("mutation", ["response_crc", "command_crc", "raw_decoded", "partial", "write_count",
                                     "timestamps", "payload_sha", "grammar", "terminal", "release", "comparison"])
def test_offline_short_report_audit_rejects_inconsistent_evidence(short_hold_report, mutation):
    from helpers.audit_runner_result import audit_short_completed_report
    report = deepcopy(short_hold_report)
    if mutation in {"response_crc", "command_crc"}:
        key = "raw_response_hex" if mutation == "response_crc" else "planned_write_hex"
        value = report["transactions"][0][key]
        report["transactions"][0][key] = value[:-2] + ("00" if value[-2:] != "00" else "ff")
    elif mutation == "raw_decoded":
        report["transactions"][-1]["decoded"]["sample_count"] = 0
    elif mutation == "partial":
        report["transactions"][-1]["raw_response_hex"] = report["transactions"][-1]["raw_response_hex"][:-2]
    elif mutation == "write_count":
        report["transactions"][7]["written_bytes"] -= 1
    elif mutation == "timestamps":
        report["transactions"][-1]["read_end_s"] = -1
    elif mutation == "payload_sha":
        report["transactions"][7]["planned_write_sha256"] = "f" * 64
    elif mutation == "grammar":
        report["transactions"].pop(4)
    elif mutation == "terminal":
        report["segments"][0]["terminal"]["run_progress"] -= 1
    elif mutation == "release":
        record = report["transactions"][3]
        command = replace(StpgCommand.unpack(bytes.fromhex(record["planned_write_hex"])), arg0=1)
        record["planned_write_hex"], record["args"] = command.pack().hex(), [1, 0, 0, 0]
    else:
        report["comparison"] = "MATCH"  # the fixture has zero comparisons
    with pytest.raises(ValueError):
        audit_short_completed_report(report)
