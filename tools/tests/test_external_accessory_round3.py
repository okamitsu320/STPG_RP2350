"""Cancellation at each receive boundary, independent stdlib child.

The real peer writes its own protocol/request journal. Only STPG/controller I/O
is replaced with models; physical open and metadata enumeration are forbidden.
Cancellation is injected after bytes have been consumed but before admission,
without timing sleeps or substituting an SDK outcome as the peer's oracle.
"""
import json

import pytest

from stpg import constants as c
from stpg_runner import CancellationToken, accessory_exit
from stpg_runner import controller, device, execution, external_accessory
from test_external_accessory_process import bundle, execute

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    import serial
    from serial.tools import list_ports

    def forbidden(*args, **kwargs):
        pytest.fail("round3 test attempted physical I/O")

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(list_ports, "comports", forbidden)
    for module in (controller, device, execution, external_accessory):
        monkeypatch.setattr(module, "enumerate_ports", forbidden)
    monkeypatch.setattr(controller, "open_controller_serial", forbidden)
    monkeypatch.setattr(device, "open_serial", forbidden)
    monkeypatch.setattr(execution, "open_serial", forbidden)


@pytest.mark.parametrize("phase,methods,runs", [
    ("open", ["open"], 0),
    ("before_run", ["open", "before_run"], 0),
    ("after_terminal", ["open", "before_run", "after_terminal"], 1),
])
def test_received_reply_is_unadmitted_at_every_cancel_boundary(
    tmp_path, bundle, monkeypatch, phase, methods, runs
):
    token = CancellationToken()
    original = external_accessory._ProcessSession._consume
    cancelled_prefixes = []

    def consume(session, item, **options):
        original(session, item, **options)
        if (item[0] == "stdout" and b"\n" in session.pending
                and session.report["requests"][-1]["method"] == phase):
            cancelled_prefixes.append(bytes(session.pending))
            token.cancel()

    monkeypatch.setattr(external_accessory._ProcessSession, "_consume", consume)
    result, stpg = execute(tmp_path, bundle, cancel=token)
    external = result["external"]

    assert len(cancelled_prefixes) == 1
    crossing = json.loads(cancelled_prefixes[0])
    assert crossing["method"] == phase
    assert crossing["status"] == {"open": "READY", "before_run": "ARMED", "after_terminal": "COMPLETE"}[phase]
    assert external["outcome"] == "CANCELLED" and external["cancel_requested"] is True
    assert external["adopted"] is False and accessory_exit(result) == 130
    assert [request["method"] for request in external["requests"]] == methods
    assert "response" not in external["requests"][-1]
    assert external["unadmitted_stdout_bytes"] == len(cancelled_prefixes[0])
    assert cancelled_prefixes[0] in bytes.fromhex(external["stdout_hex"])
    assert bytes.fromhex(external["pending_stdout_hex"]) == cancelled_prefixes[0]

    # The child's own journal establishes the request prefix independently of
    # the SDK report, including the absence of a cleanup or later request.
    root = tmp_path / "result" / "accessory"
    requests = [json.loads(line) for line in (root / "peer-requests.jsonl").read_bytes().splitlines()]
    assert [request["method"] for request in requests] == methods
    assert sum(command.cmd == c.CMD_RUN for command in stpg.commands) == runs
    if phase == "open":
        assert not stpg.commands and result["run"] is None
    else:
        assert result["run"]["outcome"] == "CANCELLED"
    assert external["spawn_attempts"] == external["stdin_close_attempts"] == 1
    assert external["terminate_attempts"] == external["kill_attempts"] == 0
    assert external["returncode"] == 0 and external["output_drain_complete"] is True
    assert external["pipe_eof_seen"] == ["stderr", "stdout"]
    assert external["result_persisted"] is True
    assert json.loads((root / "session.json").read_text()) == external
