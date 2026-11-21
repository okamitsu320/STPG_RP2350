"""Mismatch-only FAIL_LOG wire evidence, no hardware and no recovery."""
from dataclasses import replace
from pathlib import Path
import struct

import pytest

from stpg import constants as c
from stpg.crc32 import crc32
from stpg.protocol import StpgStatusResponse
from stpg_runner import CancellationToken, build_project, init_project, read_run_result, run_bundle
from stpg_runner.execution_gate import check_execution_contract
from test_stpg_runner_execution import Clock, DEVICE, FirmwarePeer, ports

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(scope="module")
def compared(tmp_path_factory):
    root = tmp_path_factory.mktemp("r8-fail-log")
    source = root / "public.stil"
    text = (Path(__file__).parent / "fixtures/runner/pattern.stil").read_text()
    text = text.replace("X: 100ns Compare(X);", "X: 100ns Compare(X); L: 100ns Compare(0);")
    text = text[:text.index("Pattern MAIN")] + "Pattern MAIN { W NORMAL; Loop 500 { V { ALL = 1L; } } }\n"
    source.write_text(text)
    project = init_project(source, root / "project", wait_preset="none", completion="release")
    return build_project(project)


class Peer(FirmwarePeer):
    def __init__(self, fault=None, **options):
        super().__init__(**options)
        self.fault = fault

    def write(self, data):
        size = super().write(data)
        if self.commands[-1].cmd != c.CMD_READ_FAIL_LOG or not self.fault:
            return size
        ready = StpgStatusResponse.unpack(bytes(self.rx[:64]))
        payload = bytes(self.rx[64:-64])
        trailer = StpgStatusResponse.unpack(bytes(self.rx[-64:]))
        if self.fault == "ready_seq":
            ready = replace(ready, seq=ready.seq + 1)
        elif self.fault == "ready_state":
            ready = replace(ready, state=c.STATE_RUNNING)
        elif self.fault == "ready_length":
            ready = replace(ready, pattern_size=4096)
        elif self.fault == "ready_count":
            ready = replace(ready, event_count=99)
        elif self.fault == "ready_offset":
            ready = replace(ready, run_progress=5)
        elif self.fault == "ready_identity":
            ready = replace(ready, capability=0)
        elif self.fault == "trailer_seq":
            trailer = replace(trailer, seq=trailer.seq + 1)
        elif self.fault == "trailer_crc":
            trailer = replace(trailer, event_count=trailer.event_count ^ 1)
        elif self.fault == "trailer_type":
            trailer = replace(trailer, result_flags=0)
        elif self.fault == "payload_partial":
            self.rx = bytearray(ready.pack() + payload[:2])
            return size
        else:
            values = list(struct.unpack("<IIII", payload[:16]))
            if self.fault == "entry_range":
                values[0] = self.requirements.estimated_event_count
            elif self.fault == "entry_bits":
                values[1] = 1 << 25
            elif self.fault == "entry_not_fail":
                values[1] = values[2]
            elif self.fault == "entry_order":
                values[0] = 99  # greater than the second entry's index
            else:
                raise AssertionError(self.fault)
            payload = struct.pack("<IIII", *values) + payload[16:]
            trailer = replace(trailer, event_count=crc32(payload))
        self.rx = bytearray(ready.pack() + payload + trailer.pack())
        return size


def execute(bundle, output, peer, **options):
    clock = Clock()
    return run_bundle(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE,
                      output_dir=output, confirm_run=True, stream_factory=lambda _: peer,
                      port_provider=ports, clock=clock, sleep=clock.sleep, **options)


@pytest.mark.parametrize("fails", [0, 1, 2, 3, 340, 341, 342, 500])
def test_mismatch_only_exact_pages_and_overflow(compared, tmp_path, fails):
    peer = Peer(fails=fails)
    report = execute(compared, tmp_path / "run", peer)
    assert report["outcome"] == "COMPLETED", report["errors"]
    log = report["segments"][0]["fail_log"]
    retained = min(fails, 341)
    assert log["status"] == ("READ" if fails else "NOT_NEEDED")
    assert log["entries_read"] == len(log["entries"]) == retained
    assert log["overflow"] == (fails > 341)
    reads = [cmd for cmd in peer.commands if cmd.cmd == c.CMD_READ_FAIL_LOG]
    assert [(cmd.arg0, cmd.arg1) for cmd in reads] == [(offset, min(2, retained - offset)) for offset in range(0, retained, 2)]
    assert all(cmd.arg2 == cmd.arg3 == 0 for cmd in reads)
    assert c.CMD_GET_FAIL_SUMMARY not in [cmd.cmd for cmd in peer.commands]
    assert read_run_result(tmp_path / "run") == report
    assert peer.close_count == 1


@pytest.mark.parametrize("fault", ["ready_seq", "ready_state", "ready_length", "ready_count", "ready_offset",
                                   "ready_identity", "trailer_seq", "trailer_crc", "trailer_type",
                                   "payload_partial", "entry_range", "entry_bits", "entry_not_fail", "entry_order"])
def test_corrupt_fail_log_seals_without_release_or_retry(compared, tmp_path, fault):
    peer = Peer(fault, fails=3)
    report = execute(compared, tmp_path / "run", peer)
    assert report["outcome"] == "REJECT", report
    assert report["comparison"] == "MISMATCH" and report["segments"][0]["terminal"]["fail_count"] == 3
    assert report["segments"][0]["fail_log"]["status"] == "READ_ERROR"
    assert report["segments"][0]["fail_log"]["entries_read"] == 0
    assert peer.commands[-1].cmd == c.CMD_READ_FAIL_LOG
    assert len([cmd for cmd in peer.commands if cmd.cmd == c.CMD_READ_FAIL_LOG]) == 1
    assert not report["release_command_attempted"] and peer.close_count == 1
    assert report["transactions"][-1].get("raw_response_hex")
    assert read_run_result(tmp_path / "run")["comparison"] == "MISMATCH"


def test_device_error_does_not_read_logs(compared, tmp_path):
    peer = Peer(terminal_error=48, fails=3)
    report = execute(compared, tmp_path / "run", peer)
    assert report["outcome"] == "DEVICE_ERROR"
    assert report["segments"][0]["fail_log"]["status"] == "NOT_COLLECTED_ERROR"
    assert c.CMD_READ_FAIL_LOG not in [cmd.cmd for cmd in peer.commands]


def test_cancel_at_fail_log_closes_once_and_preserves_terminal(compared, tmp_path):
    token = CancellationToken()
    peer = Peer(fails=3)
    original = peer.write
    def write(data):
        count = original(data)
        if peer.commands[-1].cmd == c.CMD_READ_FAIL_LOG:
            token.cancel()
        return count
    peer.write = write
    report = execute(compared, tmp_path / "run", peer, cancel=token)
    assert report["outcome"] == "CANCELLED" and report["comparison"] == "MISMATCH"
    assert peer.commands[-1].cmd == c.CMD_READ_FAIL_LOG and peer.close_count == 1


def test_log_is_read_before_the_next_upload_and_release(compared, tmp_path):
    from stpg_runner.source_storage import bundle_source_paths
    source = bundle_source_paths(compared.bundle_dir)["stil"]
    path = init_project(source, tmp_path / "project", wait_preset="none", completion="release", max_segment_vectors=200)
    built = build_project(path)
    peer = Peer(fails=3)
    report = execute(built, tmp_path / "run", peer)
    assert report["outcome"] == "COMPLETED", report["errors"]
    commands = [command.cmd for command in peer.commands]
    uploads = [index for index, value in enumerate(commands) if value == c.CMD_UPLOAD_BEGIN]
    for segment, start in enumerate(uploads):
        end = uploads[segment + 1] if segment + 1 < len(uploads) else len(commands)
        window = commands[start:end]
        assert window.count(c.CMD_READ_FAIL_LOG) == 2
        assert window.index(c.CMD_RUN) < window.index(c.CMD_READ_FAIL_LOG)
        assert report["segments"][segment]["fail_log"]["entries_read"] == 3
    assert len(uploads) == 3 and report["release_sequence_complete"]


def test_page_checkpoint_failure_seals_later_requests(compared, tmp_path, monkeypatch):
    import stpg_runner.execution as execution
    original = execution.write_run_result
    failed = []
    def checkpoint(folder, report):
        if (report["segments"] and report["segments"][0]["fail_log"]["entries_read"] == 2 and not failed):
            failed.append(True)
            raise OSError("synthetic page persistence failure")
        return original(folder, report)
    monkeypatch.setattr(execution, "write_run_result", checkpoint)
    peer = Peer(fails=3)
    report = execute(compared, tmp_path / "run", peer)
    assert report["outcome"] == "REJECT" and report["comparison"] == "MISMATCH"
    assert report["segments"][0]["fail_log"]["entries_read"] == 2
    assert report["segments"][0]["fail_log"]["status"] == "READ_ERROR"
    assert sum(cmd.cmd == c.CMD_READ_FAIL_LOG for cmd in peer.commands) == 1
    assert peer.close_count == 1 and not report["release_command_attempted"]


@pytest.mark.parametrize("cancel_at", [None, "start", "progress"])
def test_fail_log_events_only_count_validated_entries(compared, tmp_path, cancel_at):
    events, token = [], CancellationToken()
    def progress(event):
        if event.stage == "fail_log":
            events.append(event)
            if event.event == cancel_at:
                token.cancel()
    peer = Peer(fails=3)
    report = execute(compared, tmp_path / "run", peer, cancel=token, on_progress=progress)
    assert all(event.total == 3 and "segment=0" in event.detail for event in events)
    if cancel_at is None:
        assert [(event.event, event.completed) for event in events] == [("start", 0), ("progress", 2), ("progress", 3), ("done", 3)]
        assert report["outcome"] == "COMPLETED"
    else:
        assert report["outcome"] == "CANCELLED" and report["comparison"] == "MISMATCH"
        assert sum(command.cmd == c.CMD_READ_FAIL_LOG for command in peer.commands) == int(cancel_at == "progress")
        assert not report["release_command_attempted"]
    assert peer.close_count == 1


@pytest.mark.parametrize("old,new", [
    ("count = min(terminal.fail_count, c.STPG_FAIL_LOG_MAX)", "count = terminal.fail_count"),
    ("if terminal.fail_count == 0:", "if False:"),
    ("event_count=crc32(payload), **tags", "event_count=0, **tags"),
    ("self._fail_log(segment, terminal, record)", "pass"),
])
def test_gate_rejects_fail_log_guard_mutations(old, new):
    from stpg_runner import execution
    source = Path(execution.__file__).read_text()
    assert old in source
    assert check_execution_contract(execution_source=source.replace(old, new))["status"] == "REJECT"
