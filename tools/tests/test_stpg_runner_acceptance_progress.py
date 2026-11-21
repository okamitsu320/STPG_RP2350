"""Acceptance events must follow validated ACKs, never imply acquisition DONE."""

from dataclasses import replace
from pathlib import Path

import pytest

from stpg import constants as c
from stpg_runner import CancellationToken, RunHooks, build_project, init_project
import stpg_runner.execution as execution
from stpg_runner.execution_gate import check_execution_contract
from test_stpg_runner_execution import FIXTURES, FirmwarePeer, bundle, execute

pytestmark = pytest.mark.stpg_host_only


def test_acceptance_events_follow_their_actual_validated_response(bundle, tmp_path):
    peer, events = FirmwarePeer(nonterminal=1), []

    def progress(event):
        if (event.stage, event.event) in (("upload", "done"), ("run", "accepted")):
            events.append((event, peer.commands[-1].cmd, peer.response_count, bytes(peer.rx)))

    report, _ = execute(bundle, tmp_path, peer, on_progress=progress)
    assert report["outcome"] == "COMPLETED"
    assert [(event.stage, event.event, command, boundary, unread) for event, command, boundary, unread in events] == [
        ("upload", "done", c.CMD_UPLOAD_END, 9, b""), ("run", "accepted", c.CMD_RUN, 10, b""),
    ]
    upload, run = (item[0] for item in events)
    assert upload.completed == upload.total == report["plan"]["segments"][0]["expected"]["size_bytes"]
    assert run.completed is run.total is None
    assert run.detail == "segment=0; RUN accepted; waiting for STATUS; no waveform or terminal completion observed"


@pytest.mark.parametrize("boundary", [9, 10])
@pytest.mark.parametrize("field,value", [("rsp", c.RSP_ERROR), ("state", c.STATE_IDLE),
                                         ("event_count", 999), ("run_progress", 1),
                                         ("seq", 999), ("firmware_version", 0)])
def test_invalid_acceptance_emits_no_corresponding_event(bundle, tmp_path, boundary, field, value):
    events = []
    peer = FirmwarePeer(mutate=lambda i, r: replace(r, **{field: value}) if i == boundary else r)
    report, _ = execute(bundle, tmp_path, peer, on_progress=events.append)
    assert report["outcome"] == "REJECT" and peer.close_count == 1
    expected = ("upload", "done") if boundary == 9 else ("run", "accepted")
    assert expected not in [(event.stage, event.event) for event in events]
    assert peer.response_count == boundary and not report["release_command_attempted"]


@pytest.mark.parametrize("stage,event,command", [("upload", "done", c.CMD_UPLOAD_END),
                                               ("run", "accepted", c.CMD_RUN)])
@pytest.mark.parametrize("cancelled", [False, True])
def test_acceptance_callback_failure_or_cancel_sends_no_later_command(bundle, tmp_path, stage, event, command, cancelled):
    token = CancellationToken()

    def callback(progress):
        if (progress.stage, progress.event) == (stage, event):
            if cancelled:
                token.cancel()
            else:
                raise ValueError("public acceptance callback rejection")

    report, peer = execute(bundle, tmp_path, cancel=token, on_progress=callback)
    assert report["outcome"] == ("CANCELLED" if cancelled else "REJECT")
    assert peer.commands[-1].cmd == command and peer.close_count == 1
    assert not report["release_command_attempted"]


def test_every_segment_emits_acceptance_before_observation_not_between_arm_and_run(tmp_path):
    project = init_project(FIXTURES / "pattern.stil", tmp_path / "project", wait_preset="none",
                           completion="hold", max_segment_vectors=2)
    built = build_project(project)
    peer, trace = FirmwarePeer(nonterminal=2), []

    def progress(event):
        if (event.stage, event.event) in (("upload", "done"), ("run", "accepted"), ("segment", "done")):
            trace.append((event.stage, event.event, event.detail, peer.commands[-1].cmd))

    def before_run(context):
        trace.append(("hook", "before_run", str(context.segment_index), peer.commands[-1].cmd))

    report, _ = execute(built, tmp_path, peer, on_progress=progress, capture_hook=RunHooks(before_run=before_run))
    assert report["outcome"] == "COMPLETED"
    assert [(stage, event) for stage, event, _, _ in trace] == [
        ("upload", "done"), ("hook", "before_run"), ("run", "accepted"), ("segment", "done"),
    ] * 2
    for index in (0, 1):
        offset = index * 4
        assert trace[offset][2].startswith(f"segment={index};")
        assert trace[offset + 1][3] == c.CMD_UPLOAD_END
        assert trace[offset + 2][2].startswith(f"segment={index};")
        assert trace[offset + 2][3] == c.CMD_RUN


def test_progress_callback_adds_no_wire_commands_or_payloads(bundle, tmp_path):
    plain, first = execute(bundle, tmp_path / "without-callback")
    events = []
    observed, second = execute(bundle, tmp_path / "with-callback", on_progress=events.append)
    assert plain["outcome"] == observed["outcome"] == "COMPLETED"
    assert [command.pack() for command in first.commands] == [command.pack() for command in second.commands]
    assert first.payloads == second.payloads and first.close_count == second.close_count == 1


@pytest.mark.parametrize("before,after", [
    ('self.emit("upload", "done",', 'self.emit("upload", "progress",'),
    ('self.emit("run", "accepted",', 'self.emit("run", "done",'),
    ('detail=f"segment={segment.index}; RUN accepted; waiting for STATUS; "',
     'detail=f"segment={segment.index}; waveform completed; "'),
    ('self._start(segment)\n            terminal', 'self.emit("run", "accepted")\n            self._start(segment)\n            terminal'),
    ('        self.emit("upload", "done", completed=len(payload), total=len(payload),\n'
     '                  detail=f"segment={segment.index}; UPLOAD_END accepted")\n', ''),
    ('        self.emit("run", "accepted", detail=f"segment={segment.index}; RUN accepted; waiting for STATUS; "\n'
     '                  "no waveform or terminal completion observed")\n', ''),
])
def test_gate_rejects_missing_extra_or_misleading_acceptance_notifications(before, after):
    source = Path(execution.__file__).read_text(encoding="utf-8")
    assert source.count(before) == 1
    assert check_execution_contract(execution_source=source.replace(before, after))["status"] == "REJECT"
