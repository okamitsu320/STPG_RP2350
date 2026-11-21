"""Historical upload replies do not establish later commands or current pin state."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from stpg import constants as c
import stpg_runner.execution as execution
import stpgview
from stpgview.runner.service import upload_without_run
from test_stpg_runner_execution import bundle, execute
from test_stpgview_external_result_render import STUBS

pytestmark = pytest.mark.stpg_host_only


def record(outcome="REJECT", state=c.STATE_READY):
    return {"outcome": outcome, "completed_segments": 0, "planned_segments": 1,
            "transactions": [{"kind": "command", "command": c.CMD_UPLOAD_END,
                              "decoded": {"rsp": c.RSP_OK, "state": state, "error_code": 0}}]}


def test_a_real_pending_upload_checkpoint_can_precede_a_later_successful_run(bundle, tmp_path, monkeypatch):
    checkpoints = []
    writer = execution.write_run_result

    def capture(folder, report):
        rows = report.get("transactions") or []
        if report["outcome"] == "PENDING" and rows and rows[-1].get("command") == c.CMD_UPLOAD_END:
            checkpoints.append(deepcopy(report))
        return writer(folder, report)

    monkeypatch.setattr(execution, "write_run_result", capture)
    completed, peer = execute(bundle, tmp_path)
    assert completed["outcome"] == "COMPLETED" and any(cmd.cmd == c.CMD_RUN for cmd in peer.commands)
    assert checkpoints and checkpoints[0]["outcome"] == "PENDING"
    assert upload_without_run(checkpoints[0]) is None


@pytest.mark.parametrize("outcome", ["PENDING", "INCOMPLETE", "DEVICE_ERROR", "COMPLETED", None])
def test_nonfinal_or_inapplicable_records_do_not_claim_a_before_run_stop(outcome):
    assert upload_without_run(record(outcome)) is None


@pytest.mark.parametrize("outcome", ["REJECT", "CANCELLED"])
@pytest.mark.parametrize("state", [c.STATE_READY, c.STATE_HOLD_AWAITING_NEXT])
def test_finished_before_run_records_keep_their_recorded_upload_reply(outcome, state):
    report = record(outcome, state)
    assert upload_without_run(report) is not None


def test_an_unrecognised_later_record_is_not_silently_removed():
    report = record()
    report["transactions"].append(None)
    assert upload_without_run(report) is None


@pytest.mark.parametrize("state", ["READY", "HOLD_AWAITING_NEXT"])
def test_the_rendered_notice_is_about_the_record_not_current_hardware(state):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for production-JS rendering")
    source = (Path(stpgview.__file__).parent / "static/runner.js").read_text()
    start = source.index("function leftLoadedNotice(")
    function = source[start:source.index("\nfunction identityMismatchNotice(", start)]
    script = STUBS + function + "\nconsole.log(JSON.stringify(String(leftLoadedNotice(input, null))));"
    result = subprocess.run([node, "-e", script], input=json.dumps({"segment": 1, "planned_segments": 2,
        "state": state}), text=True, capture_output=True, check=True, timeout=10)
    text = json.loads(result.stdout)
    assert "accepted the upload of segment 2 of 2" in text and state in text
    assert "no subsequent STPG command is recorded" in text
    assert "not a live device check" in text and "current state or pin levels" in text
    assert "If the STPG is still" in text
    assert "no RUN was sent" not in text and "When this run ended the STPG was" not in text
