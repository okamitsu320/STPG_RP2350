"""Progress of a run in the GUI (host only, demo STPG, public synthetic STIL).

The GUI marks the hand-off to the SDK with its own ``run_verify`` step and the planned segment count; since SDK
e5e97ca0 the SDK then reports its host preparation (port check, one bundle verification, snapshot, port check
again) before it opens the port. Every event carries the job's clock (``job_s``) so the page
can tell how long ago the run last reported progress. The oracle for the order of events is the SDK's own
upload / observe / segment sequence for each segment of a segmented bundle.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time

import pytest
import serial

from stpg import constants as c
from stpg.protocol import StpgCommand
from stpg_runner import build_project, init_project, read_run_result
import stpg_runner.build as sdk_build
import stpg_runner.execution as sdk_execution
from stpgview.runner import jobs as jobs_module
from stpgview.runner import service as service_module
from stpgview.runner.jobs import Job
from stpgview.runner.service import ACKNOWLEDGEMENTS, RunnerService
from stpgview.runner.simulated import SimulatedStpg, simulated_ports
from stpgview.session import Session

from stpg_hardware_guard import forbid_hardware
from test_stpgview_runner_acceptance import counter_en_stil, segmented  # noqa: F401  (module fixture: 3200 vectors, 2 segments)

pytestmark = pytest.mark.stpg_host_only
ACKS = {key: True for key in ACKNOWLEDGEMENTS}


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture
def svc(tmp_path):
    return RunnerService(config_dir=tmp_path / "cfg", device_run=True, demo=True, results_dir=tmp_path / "runs",
                         session=Session(tmp_path / "work"))


def finished(job: Job, timeout: float = 120.0) -> dict:
    end = time.monotonic() + timeout
    while job.status == "running":
        assert time.monotonic() < end, "run did not finish"
        time.sleep(0.02)
    return job.public(0)


def test_a_segmented_run_reports_the_check_then_every_segment_on_the_job_clock(svc, segmented):  # noqa: F811
    prep = svc.prepare_run({"bundle": str(segmented.bundle_dir), "manifest_sha256": segmented.manifest_sha256,
                            "device": "simulated"})
    planned = len(prep["summary"]["segments"])
    assert planned == 2
    done = finished(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))
    assert done["status"] == "done" and done["result"]["run"]["outcome"] == "COMPLETED"
    events = done["events"]
    assert events[0]["stage"] == "run_verify" and events[0]["event"] == "start" and events[0]["segments"] == planned
    assert (events[1]["stage"], events[1]["event"]) == ("run_verify", "done")
    assert (events[2]["stage"], events[2]["event"]) == ("workspace", "reserved")
    first_open = next(i for i, e in enumerate(events) if e["stage"] == "device_preflight")
    assert any(e["stage"] == "bundle_verify" for e in events[2:first_open])
    assert any(e["stage"] == "snapshot_verify" and e["event"] == "done" for e in events[2:first_open])
    assert sum(e["stage"] == "run_verify" for e in events) == 2
    stamps = [e["job_s"] for e in events]
    assert stamps == sorted(stamps) and stamps[0] >= 0 and stamps[-1] <= done["elapsed_s"]
    uploads = [i for i, e in enumerate(events) if (e["stage"], e["event"]) == ("upload", "start")]
    finished_segments = [e for e in events if (e["stage"], e["event"]) == ("segment", "done")]
    assert len(uploads) == planned
    assert [(e["completed"], e["total"]) for e in finished_segments] == [(k, planned) for k in range(1, planned + 1)]
    for k, start in enumerate(uploads):  # each segment: upload, then STATUS polls with its own index, then done
        end = uploads[k + 1] if k + 1 < len(uploads) else len(events)
        stages = [e["stage"] for e in events[start:end]]
        assert stages[0] == "upload" and stages[-1] == "segment"
        observed = [e for e in events[start:end] if e["stage"] == "observe"]
        assert observed and all(e["detail"].startswith(f"segment={k};") for e in observed)
        assert all(0 <= e["completed"] <= e["total"] for e in observed)


def test_a_run_rejected_before_any_sdk_event_ends_the_check_as_failed(svc, segmented, monkeypatch):  # noqa: F811
    prep = svc.prepare_run({"bundle": str(segmented.bundle_dir), "manifest_sha256": segmented.manifest_sha256,
                            "device": "simulated"})

    def refuse(*args, **kwargs):
        raise service_module.RunnerError("execution structural gate rejected: injected")

    monkeypatch.setattr(service_module, "run_with_accessories", refuse)
    done = finished(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))
    assert done["status"] == "failed" and "injected" in done["error"]
    assert [(e["stage"], e["event"]) for e in done["events"]] == [("run_verify", "start")]  # never marked done


def test_one_gui_run_verifies_the_bundle_once_for_the_dialog_and_once_after_start(svc, segmented, monkeypatch):  # noqa: F811
    # Before SDK e5e97ca0 one GUI run verified the bundle six times: two before the dialog, four after Start run.
    calls, original = [], sdk_execution.verify_bundle

    def counted(*args, **kwargs):
        calls.append("verify")
        return original(*args, **kwargs)

    monkeypatch.setattr(sdk_execution, "verify_bundle", counted)
    prep = svc.prepare_run({"bundle": str(segmented.bundle_dir), "manifest_sha256": segmented.manifest_sha256,
                            "device": "simulated"})
    assert len(calls) == 1
    done = finished(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))
    assert done["result"]["run"]["outcome"] == "COMPLETED" and len(calls) == 2
    checks = [(e["event"], e["detail"]) for e in done["events"] if e["stage"] == "port_check"]
    assert checks == [("start", "before_bundle_verify"), ("done", "before_bundle_verify"),
                      ("start", "before_open"), ("done", "before_open")]
    order = [e["stage"] for e in done["events"]]
    assert (order.index("workspace") < order.index("port_check") < order.index("bundle_verify")
            < order.index("snapshot_verify") < order.index("device_preflight"))


def _ports_gone_at(phase: str):
    """The simulated port list, empty only when the SDK's check_port runs for ``phase`` (the GUI's checks pass)."""

    def ports():
        caller = sys._getframe(1)
        if caller.f_code.co_name == "check_port" and caller.f_locals.get("phase") == phase:
            return []
        return simulated_ports()

    return ports


@pytest.mark.parametrize("phase", ["before_bundle_verify", "before_open"])
def test_a_port_that_goes_away_inside_the_sdk_is_refused_before_the_open(svc, segmented, monkeypatch, phase):  # noqa: F811
    opened = []
    monkeypatch.setattr(service_module, "simulated_ports", _ports_gone_at(phase))
    monkeypatch.setattr(service_module, "SimulatedStpg", lambda *args, **kwargs: opened.append(1))
    prep = svc.prepare_run({"bundle": str(segmented.bundle_dir), "manifest_sha256": segmented.manifest_sha256,
                            "device": "simulated"})
    done = finished(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))
    run = done["result"]["run"]
    assert run["outcome"] == "REJECT" and run["stream_open_attempted"] is False and opened == []
    assert any("[PORT_NOT_LISTED]" in error for error in run["errors"])
    assert not any(e["stage"] == "device_preflight" for e in done["events"])
    record = read_run_result(Path(done["result"]["result_file"]))
    checks = record["port_checks"]
    assert [c["phase"] for c in checks] == ["before_bundle_verify", "before_open"][:len(checks)]
    assert checks[-1]["phase"] == phase and checks[-1]["status"] == "REJECT" and checks[-1]["ports"] == []
    assert all(c["status"] == "PASS" and c["ports"] for c in checks[:-1])
    assert (record["plan"] is None) == (phase == "before_bundle_verify")


@pytest.mark.parametrize("check_mode", ["semantic", "receipt"])
def test_a_run_cancelled_during_the_bundle_check_ends_before_the_open(svc, segmented, monkeypatch, check_mode):  # noqa: F811
    opened, gate, entered = [], threading.Event(), threading.Event()
    monkeypatch.setattr(service_module, "SimulatedStpg", lambda *args, **kwargs: opened.append(1))
    prep = svc.prepare_run({"bundle": str(segmented.bundle_dir), "manifest_sha256": segmented.manifest_sha256,
                            "device": "simulated"})
    if check_mode == "semantic":
        sdk_build._clear_verification_receipts()
    target = "verify_segmented_manifest" if check_mode == "semantic" else "_verify_inventory"
    original = getattr(sdk_build, target)

    def held(*args, **kwargs):  # only the SDK's check after Start run: the dialog's check is over
        entered.set()
        gate.wait(30)
        return original(*args, **kwargs)

    monkeypatch.setattr(sdk_build, target, held)
    job = svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS})
    assert entered.wait(30)
    svc.jobs.cancel(job.id)
    gate.set()
    done = finished(job)
    run = done["result"]["run"]
    assert done["status"] == "cancelled" and run["outcome"] == "CANCELLED"
    assert run["stream_open_attempted"] is False and opened == []
    assert not any(e["stage"] == "device_preflight" for e in done["events"])


def test_a_poll_sees_the_latest_heartbeat_and_keeps_up_after_old_events_are_dropped(monkeypatch):
    # The page polls with ``since = next - 1``. A heartbeat replaced in place used to stay invisible to it, and once
    # MAX_EVENTS was passed (one event per 4 KB uploaded and per STATUS poll) it received no event any more.
    monkeypatch.setattr(jobs_module, "MAX_EVENTS", 5)
    job = Job("synthetic", "run", "public test", False, None)
    job.progress({"stage": "bundle_verify", "event": "start"})
    job.progress({"stage": "bundle_verify", "event": "heartbeat", "elapsed_s": 1.0})
    seen = job.public(0)
    assert seen["first"] == 0 and seen["next"] == 2
    job.progress({"stage": "bundle_verify", "event": "heartbeat", "elapsed_s": 2.0})  # replaces the last one
    again = job.public(seen["next"] - 1)
    assert again["first"] == 1 and [e["elapsed_s"] for e in again["events"]] == [2.0] and again["next"] == 2
    for k in range(10):
        job.progress({"stage": "observe", "event": "progress", "completed": k})
    later = job.public(again["next"] - 1)
    assert later["next"] == 12 and len(job.events) == 5
    # position 1 was dropped: the poll gets every retained event, and says where they start
    assert later["first"] == 7 and [e["completed"] for e in later["events"]] == [5, 6, 7, 8, 9]
    last = job.public(later["next"] - 1)
    assert last["first"] == 11 and [e["completed"] for e in last["events"]] == [9]


class _LinkStopsAfterFirstDataBlock(SimulatedStpg):
    """Model a read failure after payload; separately forbid any timeout reconfiguration."""

    failing = False

    def _starts_failing(self, data: bytes) -> bool:
        return self.pending is not None  # the payload of an UPLOAD_DATA

    def write(self, data: bytes) -> int:
        if self._starts_failing(data):
            self.failing = True
        return super().write(data)

    @property
    def timeout(self) -> float:
        return 2.0

    @timeout.setter
    def timeout(self, value: float) -> None:
        raise AssertionError("timeout setting changed after open")

    def read(self, size: int) -> bytes:
        if self.failing:
            raise serial.SerialException("ReadFile failed (synthetic Windows API error 121)")
        return super().read(size)


class _LinkStopsAfterRun(_LinkStopsAfterFirstDataBlock):
    """The same failure right after the RUN command, so the first segment may already drive its outputs."""

    def _starts_failing(self, data: bytes) -> bool:
        return self.pending is None and StpgCommand.unpack(data).cmd == c.CMD_RUN


def _is_command(peer: SimulatedStpg, data: bytes, command: int, arg0: int | None = None) -> bool:
    if peer.pending is not None:  # a payload, not a command
        return False
    parsed = StpgCommand.unpack(data)
    return parsed.cmd == command and (arg0 is None or parsed.arg0 == arg0)


class _RunWriteFails(_LinkStopsAfterFirstDataBlock):
    """The RUN command's write raises (a write timeout): its sent-byte count is unknown."""

    def _starts_failing(self, data: bytes) -> bool:
        return False

    def write(self, data: bytes) -> int:
        if _is_command(self, data, c.CMD_RUN):
            raise serial.SerialTimeoutException("Write timeout")
        return super().write(data)


class _RunWriteShort(_LinkStopsAfterFirstDataBlock):
    """The OS reports only 12 of the RUN command's 32 bytes written."""

    def _starts_failing(self, data: bytes) -> bool:
        return False

    def write(self, data: bytes) -> int:
        return 12 if _is_command(self, data, c.CMD_RUN) else super().write(data)


class _PartialAnswerAfterBlock(_LinkStopsAfterFirstDataBlock):
    """After the first data block, 9 bytes of the answer arrive, then the next read fails."""

    prefix_given = False

    def read(self, size: int) -> bytes:
        if self.failing and not self.prefix_given:
            self.prefix_given = True
            return SimulatedStpg.read(self, min(size, 9))
        if self.failing:
            raise serial.SerialException("ReadFile failed (synthetic)")
        return SimulatedStpg.read(self, size)


class _ReleaseAnswerFails(_LinkStopsAfterFirstDataBlock):
    """The read after the final RESET(1), the release of a completion=release run, fails."""

    def _starts_failing(self, data: bytes) -> bool:
        return _is_command(self, data, c.CMD_RESET, arg0=1)


WINDOWS_OPEN_DENIED = "could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)"  # pyserial 3.5


def _open_fails(message: str):
    """A peer whose open fails as pyserial reports it, before any byte is written."""

    def peer(*args, **kwargs):
        raise serial.SerialException(message)
    return peer


@pytest.fixture(scope="module")
def released(tmp_path_factory):
    """A one-segment public counter bundle with completion=release."""

    root = tmp_path_factory.mktemp("gui-released")
    source = root / "counter.stil"
    source.write_text(counter_en_stil(1600, 800), encoding="utf-8")
    return build_project(init_project(source, root / "project", wait_preset="none", completion="release"))


@pytest.mark.parametrize("peer, bundle, request_name, sent, attempted, completed, last_completed, prefix, stage", [
    (_RunWriteFails, "segmented", "RUN", None, True, False, False, 0, "write"),
    (_RunWriteShort, "segmented", "RUN", 12, True, False, False, 0, "write"),
    (_PartialAnswerAfterBlock, "segmented", "payload", "block", False, False, True, 9, "read"),
    (_ReleaseAnswerFails, "released", "RESET", 32, True, True, True, 0, "read"),
])
def test_each_failure_shape_through_the_sdk_keeps_attempt_completion_prefix_and_stage_apart(
        svc, request, monkeypatch, peer, bundle, request_name, sent, attempted, completed, last_completed, prefix, stage):
    built = request.getfixturevalue(bundle)
    monkeypatch.setattr(service_module, "SimulatedStpg", peer)
    prep = svc.prepare_run({"bundle": str(built.bundle_dir), "manifest_sha256": built.manifest_sha256,
                            "device": "simulated"})
    done = finished(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))
    assert done["result"]["run"]["outcome"] == "REJECT"
    exchanges = done["result"]["exchanges"]
    last = exchanges["rows"][-1]
    record = read_run_result(Path(done["result"]["result_file"]))["transactions"][-1]
    assert exchanges["link_lost"] is True and last["request"] == request_name
    assert last["sent_bytes"] == (record["size"] if sent == "block" else sent)
    assert (exchanges["run_write_attempted"], exchanges["run_write_completed"]) == (attempted, completed)
    assert exchanges["run_sent"] is completed and exchanges["last_write_completed"] is last_completed
    assert last["response_bytes"] == prefix and last["failure_stage"] == stage and last["answer"] is None


def test_a_run_whose_usb_link_stops_after_the_first_data_block_shows_the_last_exchanges(svc, segmented, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service_module, "SimulatedStpg", _LinkStopsAfterFirstDataBlock)
    prep = svc.prepare_run({"bundle": str(segmented.bundle_dir), "manifest_sha256": segmented.manifest_sha256,
                            "device": "simulated"})
    done = finished(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))
    run = done["result"]["run"]
    assert run["outcome"] == "REJECT" and run["stream_opened"] is True and run["close_successes"] == 1
    assert any("ReadFile failed" in error for error in run["errors"])
    exchanges = done["result"]["exchanges"]
    assert exchanges["link_lost"] is True and exchanges["run_sent"] is False and exchanges["total"] == 8
    rows = exchanges["rows"]
    assert [row["request"] for row in rows] == ["GET_STATUS", "RESET", "GET_STATUS", "UPLOAD_BEGIN", "UPLOAD_DATA",
                                                "payload"]
    assert rows[-2]["answer"] == "READY_FOR_PAYLOAD · RECEIVING" and rows[-3]["answer"] == "OK · RECEIVING"
    record = read_run_result(Path(done["result"]["result_file"]))
    block = record["transactions"][-1]
    assert rows[-1]["answer"] is None and rows[-1]["sent_bytes"] == block["written_bytes"] == block["size"]
    assert "ReadFile failed" in rows[-1]["error"] and rows[-1]["took_s"] >= 0
    assert exchanges["close_s"] is not None and exchanges["close_s"] >= 0
    # names, byte counts, times and errors only: no arguments, CRCs or payload hashes reach the page
    assert all(set(row) == {"seq", "request", "sent_bytes", "answer", "took_s", "error",
                            "response_bytes", "failure_stage"} for row in rows)


def test_a_run_whose_port_open_fails_keeps_an_empty_journal_and_no_close(svc, released, monkeypatch):
    # The user's Windows run after the stalls: the port checks passed and the open itself was refused (error 5).
    monkeypatch.setattr(service_module, "SimulatedStpg", _open_fails(WINDOWS_OPEN_DENIED))
    prep = svc.prepare_run({"bundle": str(released.bundle_dir),
                            "manifest_sha256": released.manifest_sha256, "device": "simulated"})
    done = finished(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))
    run = done["result"]["run"]
    assert run["outcome"] == "REJECT" and run["stream_open_attempted"] is True and run["stream_opened"] is False
    assert run["close_attempts"] == 0 and run["errors"] == [f"SerialException: {WINDOWS_OPEN_DENIED}"]
    assert done["result"]["exchanges"] is None
    record = read_run_result(Path(done["result"]["result_file"]))
    assert record["transactions"] == [] and [e["event"] for e in record["lifecycle"]] == ["open.begin"]


def test_a_completed_run_keeps_its_exchanges_without_a_link_loss(svc, segmented):  # noqa: F811
    prep = svc.prepare_run({"bundle": str(segmented.bundle_dir), "manifest_sha256": segmented.manifest_sha256,
                            "device": "simulated"})
    done = finished(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))
    exchanges = done["result"]["exchanges"]
    assert done["result"]["run"]["outcome"] == "COMPLETED"
    assert exchanges["link_lost"] is False and exchanges["run_sent"] is True and exchanges["rows"][-1]["error"] is None


@pytest.mark.parametrize("error, lost", [
    ("SerialException: Cannot configure port, something went wrong.", True),
    ("SerialTimeoutException: Write timeout", True),
    ("RunnerError: response timeout/EOF; partial bytes retained", True),
    ("RunnerError: response deadline exhausted", True),
    ("RunnerError: response deadline exhausted; partial bytes retained", True),  # since SDK 90081059
    ("RunnerError: response read limit exhausted; partial bytes retained", True),
    ("RunnerError: short/invalid write; not resent", True),
    ("RunnerError: response sequence/firmware/capability mismatch", False),  # an answer came back
    (None, False),
])
def test_only_errors_of_the_serial_link_count_as_a_lost_link(error, lost):
    report = {"transactions": [{"kind": "command", "command": 3, "seq": 1, "write_begin_s": 1.0, "read_end_s": 1.5,
                                "written_bytes": 32, "read_error": error}], "lifecycle": []}
    exchanges = service_module.device_exchanges(report)
    assert exchanges["link_lost"] is lost and exchanges["close_s"] is None and exchanges["rows"][0]["took_s"] == 0.5
    assert service_module.device_exchanges({"transactions": []}) is None


@pytest.mark.parametrize("kind, count, size, completed", [
    ("command", 32, None, True), ("command", 12, None, False), ("command", None, None, False),
    ("payload", 4096, 4096, True), ("payload", 4000, 4096, False),
])
def test_a_write_counts_as_complete_only_with_its_full_byte_count(kind, count, size, completed):
    # The SDK also records a write error for a short write; the count alone must already say incomplete.
    record = {"kind": kind, "seq": 1, "write_begin_s": 1.0, "write_end_s": 1.1, "written_bytes": count,
              "read_error": "OSError: synthetic read failure", **({"command": c.CMD_RUN} if kind == "command" else {}),
              **({"size": size} if size else {})}
    exchanges = service_module.device_exchanges({"transactions": [record], "lifecycle": []})
    assert exchanges["last_write_completed"] is completed
    assert exchanges["run_write_completed"] is (completed and kind == "command")
