"""GUI side of R9: the Custom chunk-wait length, refused wait plans, and the controller identification lines.

Public synthetic patterns, the simulated STPG and the simulated GPIO controller only. Nothing here opens a real port
or enumerates USB.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from stpg import constants as c
from stpg_runner import build_project
from stpg_runner.artifacts import RunnerError, identity, read_json, write_json
from stpg_runner.build import BUNDLE_NAME
from stpg_runner.cli import main as runner_main
from stpgview.runner import service as service_module
from stpgview.runner.service import RunnerService, project_options
from stpgview.runner.simulated import SimulatedController

from stpg_hardware_guard import forbid_hardware
from test_stpg_runner_wait_capacity import source_text
from test_stpgview_runner_accessories import (Traffic, counter, gpio_plan, prepare, run, svc,  # noqa: F401
                                              wait)

pytestmark = pytest.mark.stpg_host_only
BASE = {"completion": "hold", "storage": "psram", "compile_mode": "auto", "loop_policy": "preserve",
        "segmentation": "auto"}
SERVICE = {"service_us": "50", "service_source": "public declared budget, not measured", "service_scope": "non-loop",
           "max_total_wait_us": "1000000"}


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture
def public(tmp_path):
    path = tmp_path / "public.stil"
    path.write_text(source_text(6000))
    return path


def options(preset, **extra):
    return {**BASE, "wait_preset": preset, **({} if preset == "none" else SERVICE), **extra}


def create(svc, stil, folder, preset, **extra):
    return svc.init_project({"stil": str(stil), "directory": str(folder), **options(preset, **extra)})


def built_patterns(svc, project_path) -> list[bytes]:
    done = wait(svc.start_build(project_path))
    assert done["status"] == "done", done["error"]
    root = Path(done["result"]["bundle_dir"])
    return [(root / item["path"]).read_bytes() for item in read_json(root / BUNDLE_NAME)["patterns"]]


# ---- the length is read exactly ----------------------------------------------------------------------------

@pytest.mark.parametrize("text,microseconds", [("6.5", 6500), ("0.001", 1), ("4", 4000), ("12.000", 12000),
                                               ("6.5000", 6500), (" 007.25 ", 7250), ("1.001", 1001), ("1.005", 1005),
                                               ("21474.836", 21474836)])
def test_a_custom_length_in_ms_becomes_exact_microseconds(text, microseconds):
    # 1.001 and 1.005 ms are the cases a binary float gets wrong (1000 and 1004 µs after truncation)
    assert project_options(options("chunk-custom", chunk_wait_ms=text))["chunk_wait_us"] == microseconds


@pytest.mark.parametrize("value", ["6.5001", "0.0004", "0", "0.000", "-1", "+6", "nan", "inf", "1e3", "6,5", ".5",
                                   "6.5.1", "１", "", True, 6.5, 6500, None])
def test_a_custom_length_that_needs_rounding_or_is_not_a_plain_number_is_refused(value):
    with pytest.raises(RunnerError):
        project_options(options("chunk-custom", chunk_wait_ms=value))


def test_a_length_is_never_kept_or_dropped_silently_with_another_preset():
    for preset in ("none", "chunk-4ms", "chunk-12ms"):
        assert project_options(options(preset))["chunk_wait_us"] is None
        with pytest.raises(RunnerError, match="only with chunk-custom"):
            project_options(options(preset, chunk_wait_ms="6.5"))
    with pytest.raises(RunnerError, match="chunk_wait_ms"):  # the SDK's µs key is not a form field
        project_options(options("chunk-custom", chunk_wait_ms="6.5", chunk_wait_us=6500))


# ---- New / Edit / reopen / Copy ------------------------------------------------------------------------------

def test_a_custom_project_is_the_cli_project_and_builds_the_same_single_and_segmented_patterns(svc, public, tmp_path):
    for name, extra, cli_extra in (("single", {}, []),
                                   ("segmented", {"max_segment_vectors": "2500"}, ["--max-segment-vectors", "2500"])):
        gui = create(svc, public, tmp_path / f"gui-{name}", "chunk-custom", chunk_wait_ms="6.5", **extra)
        assert runner_main(["init", str(public), "--directory", str(tmp_path / f"cli-{name}"), "--wait-preset",
                            "chunk-custom", "--chunk-wait-us", "6500", "--completion", "hold", "--service-us", "50",
                            "--service-source", "public declared budget, not measured", "--service-scope",
                            "non-loop", "--max-total-wait-us", "1000000", *cli_extra]) == 0
        cli_path = tmp_path / f"cli-{name}" / "project.stpg-project.json"
        assert read_json(Path(gui["project_path"])) == read_json(cli_path)
        assert gui["settings"]["wait"]["chunk_wait_us"] == 6500
        gui_bytes, cli_bytes = built_patterns(svc, gui["project_path"]), built_patterns(svc, str(cli_path))
        assert gui_bytes == cli_bytes
        assert (len(gui_bytes) == 1) if name == "single" else (len(gui_bytes) > 1)


@pytest.mark.parametrize("preset,text", [("chunk-4ms", "4"), ("chunk-12ms", "12.000")])
def test_a_custom_length_equal_to_a_preset_builds_the_same_pattern(svc, public, tmp_path, preset, text):
    named = create(svc, public, tmp_path / "named", preset)
    custom = create(svc, public, tmp_path / "custom", "chunk-custom", chunk_wait_ms=text)
    assert built_patterns(svc, named["project_path"]) == built_patterns(svc, custom["project_path"])


def test_edit_and_reopen_keep_the_exact_length_and_another_preset_removes_it(svc, public, tmp_path):
    info = create(svc, public, tmp_path / "project", "chunk-custom", chunk_wait_ms="6.5")
    path = Path(info["project_path"])

    def edit(preset, **extra):
        nonlocal info
        info = svc.edit_project({"project": str(path), "project_sha256": info["project_sha256"],
                                 **options(preset, **extra)})
        return read_json(path)["wait"]

    assert svc.open_project(str(path))["settings"]["wait"]["chunk_wait_us"] == 6500
    assert edit("chunk-custom", chunk_wait_ms="12.345")["chunk_wait_us"] == 12345
    assert "chunk_wait_us" not in edit("chunk-4ms") and read_json(path)["wait"]["preset"] == "chunk-4ms"
    before = path.read_bytes()
    with pytest.raises(RunnerError, match="only with chunk-custom"):
        edit("chunk-12ms", chunk_wait_ms="6.5")
    assert path.read_bytes() == before  # a refused edit writes nothing
    assert edit("chunk-custom", chunk_wait_ms="0.001")["chunk_wait_us"] == 1
    assert "chunk_wait_us" not in edit("none")
    summary = service_module.settings_summary(read_json(path))
    assert summary["wait_preset"] == "none" and summary["chunk_wait_us"] is None


def test_an_older_preset_project_is_not_given_the_new_key(svc, public, tmp_path):
    info = create(svc, public, tmp_path / "project", "chunk-4ms")
    path = Path(info["project_path"])
    raw = read_json(path)
    assert "chunk_wait_us" not in raw["wait"] and "chunk_wait_us" not in info["settings"]["wait"]
    svc.edit_project({"project": str(path), "project_sha256": info["project_sha256"], **options("chunk-4ms")})
    assert read_json(path) == raw


def test_the_page_is_told_the_modes_and_the_longest_custom_length(svc):
    state = svc.state()
    assert state["wait_presets"] == ["none", "chunk-4ms", "chunk-12ms"]  # older pages keep the numeric presets
    assert state["wait_modes"] == ["none", "chunk-4ms", "chunk-12ms", "chunk-custom"]
    assert state["custom_wait_max_us"] == 21_474_836


# ---- refused wait plans ----------------------------------------------------------------------------------------

def test_a_plan_over_the_total_limit_is_refused_and_nothing_is_adjusted(svc, public, tmp_path):
    info = create(svc, public, tmp_path / "project", "chunk-custom", chunk_wait_ms="6.5", max_total_wait_us="100")
    path = Path(info["project_path"])
    before = path.read_bytes()
    done = wait(svc.start_build(info["project_path"]))
    assert done["status"] == "failed" and "wait plan rejected" in done["error"]
    assert path.read_bytes() == before  # no limit, length or service value was changed
    assert not list((path.parent / "build").glob("*/runner.bundle.json"))
    reopened = svc.open_project(str(path))
    assert [r["status"] for r in reopened["reservations"]] == ["FAILED"]


def test_a_bundle_whose_saved_plan_is_rejected_is_refused_and_not_updated(svc, tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "runner" / "pattern.stil"
    info = create(svc, fixture, tmp_path / "project", "chunk-4ms")
    done = wait(svc.start_build(info["project_path"]))
    root = Path(done["result"]["bundle_dir"])
    # As an older SDK could leave it: a rejected partial proposal, inventoried (only to reach that check).
    proposal_path = root / "wait.proposal.json"
    proposal = read_json(proposal_path)
    proposal["wait_plan"].update(status="rejected", reason="public synthetic rejected partial proposal")
    write_json(proposal_path, proposal)
    manifest_path = root / BUNDLE_NAME
    manifest = read_json(manifest_path)
    manifest["files"][proposal_path.name] = identity(proposal_path)
    write_json(manifest_path, manifest)
    sha = identity(manifest_path)["sha256"]
    opened = svc.open_bundle(str(root), sha)
    assert opened["verification"]["status"] == "REJECT"
    assert any("explicit wait plan rejected" in e for e in opened["verification"]["errors"])
    job = wait(svc.start_rebind(str(root), sha))
    assert job["status"] == "failed" and "wait plan rejected" in job["error"]
    assert sorted(p.name for p in root.parent.iterdir()) == [root.name]  # no new bundle beside it


# ---- the controller's identification lines ---------------------------------------------------------------------

class _OtherSketch(SimulatedController):
    """Answers the identify request with a line that is not the v1 identification (markup included on purpose)."""

    def _command(self, text):
        if text.startswith("i "):
            return "<img src=x onerror=window.__pwned=1> other-sketch v0"
        return super()._command(text)


def test_a_refused_identification_lists_the_lines_and_the_stpg_kept_the_upload(svc, counter, monkeypatch):
    traffic = Traffic(gpio=_OtherSketch()).install(monkeypatch)
    view = run(svc, prepare(svc, counter, gpio=gpio_plan(counter)))["result"]
    assert view["run"]["outcome"] == view["control"]["outcome"] == "REJECT" and traffic.stpg.runs == 0
    lines = view["control"]["identity_lines"]
    assert lines[-1] == {"line": "<img src=x onerror=window.__pwned=1> other-sketch v0", "disposition": "REJECT"}
    assert view["control"]["identity_lines_total"] == len(lines) and view["control"]["open_attempted"]
    assert traffic.gpio.applied == []  # no GPIO level was set
    assert view["left_loaded"] == {"segment": 0, "planned_segments": 1, "state": "READY"}
    assert view["start_check"] is None


def test_a_matched_identification_is_listed_too_and_a_completed_run_left_nothing_loaded(svc, counter, monkeypatch):
    Traffic().install(monkeypatch)
    view = run(svc, prepare(svc, counter, gpio=gpio_plan(counter)))["result"]
    assert view["control"]["outcome"] == "COMPLETED" and view["left_loaded"] is None
    assert view["control"]["identity_lines"][-1]["disposition"] == "MATCH"


def test_an_old_record_has_no_lines_and_long_or_many_lines_are_bounded(tmp_path):
    view = RunnerService._gpio_view({"outcome": "REJECT", "transport": {"journal": [{"event": "open.begin"}]}},
                                    tmp_path / "control.json", run_attached=True)
    assert view["identity_lines"] == [] and view["identity_lines_total"] == 0
    journal = [{"event": "identity.line", "line": f"boot {i} " + "x" * 300, "disposition": "STARTUP"} for i in range(11)]
    view = RunnerService._gpio_view({"outcome": "REJECT", "transport": {"journal": journal}}, tmp_path / "c.json",
                                    run_attached=False)
    assert view["identity_lines_total"] == 11 and len(view["identity_lines"]) == service_module.IDENTITY_LINES_SHOWN
    assert view["identity_lines"][0]["line"].startswith("boot 3 ")
    assert len(view["identity_lines"][0]["line"]) == service_module.LINE_SHOWN + 1  # the cut is marked with …


def _command(seq, command, state, **extra):
    return {"kind": "command", "seq": seq, "command": command, "written_bytes": c.STPG_CMD_SIZE,
            "decoded": {"seq": seq, "rsp": c.RSP_OK, "state": state, "error_code": 0}, **extra}


def test_an_accepted_upload_with_nothing_after_it_is_recognised_only_as_the_last_record():
    upload = _command(5, c.CMD_UPLOAD_END, c.STATE_READY)
    report = {"outcome": "REJECT", "completed_segments": 0, "planned_segments": 2, "transactions": [upload]}
    assert service_module.upload_without_run(report) == {"segment": 0, "planned_segments": 2, "state": "READY"}
    later = {**report, "completed_segments": 1,
             "transactions": [_command(9, c.CMD_UPLOAD_END, c.STATE_HOLD_AWAITING_NEXT)]}
    assert service_module.upload_without_run(later)["state"] == "HOLD_AWAITING_NEXT"
    for changed in ({"transactions": [upload, _command(6, c.CMD_RUN, c.STATE_RUNNING)]},
                    {"transactions": [upload, _command(6, c.CMD_RESET, c.STATE_IDLE)]},
                    {"outcome": "COMPLETED"},
                    {"transactions": [{**upload, "read_error": "deadline"}]},
                    {"transactions": [_command(5, c.CMD_UPLOAD_END, c.STATE_IDLE)]},
                    {"transactions": [{**upload, "decoded": {**upload["decoded"], "error_code": 3}}]},
                    {"transactions": []}):
        assert service_module.upload_without_run({**report, **changed}) is None, changed


def test_a_start_check_refusal_never_lists_controller_lines(svc, counter, monkeypatch):
    from dataclasses import replace
    from stpgview.runner.simulated import SimulatedStpg

    class Ready(SimulatedStpg):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.last = replace(self.last, state=c.STATE_READY)

    traffic = Traffic().install(monkeypatch)
    monkeypatch.setattr(traffic, "stpg_factory", lambda _config: Ready())
    view = run(svc, prepare(svc, counter, gpio=gpio_plan(counter)))["result"]
    assert view["start_check"]["state"] == "READY" and view["left_loaded"] is None
    assert view["control"]["open_attempted"] is False and view["control"]["identity_lines"] == []
    assert traffic.gpio.writes == []
