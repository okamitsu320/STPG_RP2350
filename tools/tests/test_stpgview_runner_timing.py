"""Timing choice, rounded times, wait scope and rejection texts of the GUI runner (SDK 6a329215 and later).

Host only, public synthetic STIL. The oracles are the SDK's own artifacts: the reservation's retained
``source.analysis.json`` for the off-grid times, each package's ``pattern.compile_report.json`` for the
rounding records, ``stpg_runner.init_project`` for project files and the viewer's own decoder of the emitted
pattern for the times of a rounded bundle. (Capture planning and alignment on such a bundle are tested with the
separate logic-analyzer tool.)
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from stil.json_artifacts import read_json_artifact, resolve_json_artifact
from stpg_runner.artifacts import read_json, write_json
from stpgview.load_bin import load_bin
from stpgview.runner.service import RunnerService, rejection_text, timing_summary
from stpgview.session import Session

from stpg_hardware_guard import forbid_hardware
from stpg_public_patterns import counter_stil
from test_stpgview_runner_edit import WAIT, _plain_then_loop, edit
from test_stpgview_runner_inputs import RUNNER_STIL, _wait, request

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture
def svc(tmp_path):
    return RunnerService(config_dir=tmp_path / "cfg", device_run=False, demo=False, results_dir=tmp_path / "runs",
                         session=Session(tmp_path / "work"))


def off_grid(tmp_path: Path, vectors: int = 40, name: str = "off.stil") -> Path:
    """The runner fixture with its compare strobe at 103 ns (not a multiple of the 5 ns tick)."""

    prefix = RUNNER_STIL.read_text().split("Pattern MAIN")[0].replace("100ns", "103ns")
    body = "".join(f"  V {{ ALL = {'1X' if i % 2 else '01'}; }}\n" for i in range(vectors))
    source = tmp_path / name
    source.write_text(prefix + "Pattern MAIN {\n  W NORMAL;\n" + body
                      + "  Loop 3 {\n    V { ALL = 01; }\n    V { ALL = 1X; }\n  }\n}\n")
    return source


def compile_reports(bundle: dict) -> list[dict]:
    root = Path(bundle["bundle_dir"])
    return [read_json_artifact(resolve_json_artifact(root / Path(p["path"]).parent / "pattern.compile_report.json"))
            for p in bundle["patterns"]]


def reservation(info: dict, status: str) -> Path:
    (folder,) = [Path(r["path"]) for r in info["reservations"] if r["status"] == status]
    return folder


# ---- exact by default, the off-grid times named, then Approx ------------------------------

def test_an_exact_build_off_the_grid_lists_the_times_and_approx_builds_them_rounded(svc, tmp_path):
    info = svc.init_project(request(tmp_path / "p", stil=str(off_grid(tmp_path)), segmentation="auto"))
    assert read_json(Path(info["project_path"]))["compile"]["allow_approx"] is False
    failed = _wait(svc.start_build(info["project_path"]))
    error = failed["error"]
    assert failed["status"] == "failed" and error.startswith("RunnerError: ERR_APPROXIMATION_REQUIRED: source timing is off")
    opened = svc.open_project(info["project_path"])
    analysis = read_json_artifact(resolve_json_artifact(reservation(opened, "FAILED") / "source.analysis.json"))
    reasons = list(dict.fromkeys(analysis["timing"]["approximation_reasons"]))
    assert reasons and all("103ns quantized to 21 ticks" in reason for reason in reasons)
    assert "Off the target tick grid (5 ns per tick), from " in error
    for reason in reasons:  # every distinct time, with the time it would become
        assert f"{reason} (= 105 ns)" in error
    assert "choose Timing = Approx with Edit settings…" in error

    edited = edit(svc, opened, segmentation="auto", allow_approx="true")
    assert edited["settings"]["compile"]["allow_approx"] is True
    (attempt,) = edited["reservations"]
    assert attempt["settings"]["allow_approx"] is False and attempt["current_settings"] is False
    done = _wait(svc.start_build(info["project_path"]))
    assert done["status"] == "done", done["error"]
    bundle = done["result"]["bundle"]
    timing = bundle["timing"]
    records = [r for report in compile_reports(bundle) for r in report["approximations"]]
    assert timing["policy"]["allow_approx"] is True and timing["policy"]["rounding"] == "nearest_tick_half_up"
    assert timing["allow_approx"] is True and timing["records"] == len(records) > 0
    assert {(g["original_time"], g["emitted_time_ns"], g["delta_ns"]) for g in timing["groups"]} == {("103ns", "105", "2.0")}
    assert sum(g["records"] for g in timing["groups"]) == len(records) and timing["max_abs_delta_ns"] == 2.0
    # Both attempts stay listed, each with the timing it used.
    after = svc.open_project(info["project_path"])
    used = {r["status"]: r["settings"]["allow_approx"] for r in after["reservations"]}
    assert used == {"FAILED": False, "HOST_ONLY_VERIFIED": True}


def test_the_rounding_of_every_segment_is_summarized(svc, tmp_path):
    source = off_grid(tmp_path, vectors=900)
    info = svc.init_project(request(tmp_path / "p", stil=str(source), segmentation="auto", max_segment_vectors="300",
                                    allow_approx=True))
    done = _wait(svc.start_build(info["project_path"]), timeout=300)
    assert done["status"] == "done", done["error"]
    bundle = done["result"]["bundle"]
    assert bundle["package_kind"] == "segmented" and len(bundle["patterns"]) >= 3
    reports = compile_reports(bundle)
    timing = bundle["timing"]
    assert [p["records"] for p in timing["packages"]] == [len(r["approximations"]) for r in reports]
    assert all(p["records"] > 0 for p in timing["packages"])
    (group,) = timing["groups"]
    assert group["packages"] == list(range(len(reports)))
    assert group["records"] == timing["records"] == sum(len(r["approximations"]) for r in reports)


def test_an_exact_bundle_and_one_built_before_the_timing_choice(svc, tmp_path):
    info = svc.init_project(request(tmp_path / "p"))
    done = _wait(svc.start_build(info["project_path"]))
    assert done["status"] == "done", done["error"]
    timing = done["result"]["bundle"]["timing"]
    assert timing["policy"]["allow_approx"] is False and timing["allow_approx"] is False
    assert timing["records"] == 0 and timing["groups"] == []
    # A bundle of an older SDK has no compile.policy.json (it could not round).
    old = tmp_path / "old"
    shutil.copytree(done["result"]["bundle_dir"], old)
    (old / "compile.policy.json").unlink()
    legacy = timing_summary(old, done["result"]["bundle"]["patterns"])
    assert legacy["policy"] is None and legacy["allow_approx"] is False and legacy["records"] == 0


# ---- older project files ------------------------------------------------------------------

def test_an_older_project_without_the_field_stays_without_it_unless_approx_is_chosen(svc, tmp_path):
    info = svc.init_project(request(tmp_path / "p", stil=str(off_grid(tmp_path)), segmentation="auto"))
    path = Path(info["project_path"])
    old = read_json(path)
    del old["compile"]["allow_approx"]
    write_json(path, old)
    opened = svc.open_project(str(path))
    assert "allow_approx" not in opened["settings"]["compile"]  # the SDK does not insert it on load
    # Exact again with another change: the file keeps the old form.
    edited = edit(svc, opened, segmentation="auto", completion="release", allow_approx="false")
    assert "allow_approx" not in read_json(path)["compile"]
    assert edited["settings"]["execution"]["completion"] == "release"
    edited = edit(svc, edited, segmentation="auto", allow_approx="true")
    assert read_json(path)["compile"]["allow_approx"] is True
    copied = svc.init_project(request(tmp_path / "copy", stil=str(off_grid(tmp_path)), segmentation="auto",
                                      copy_of=str(path)))
    assert read_json(Path(copied["project_path"]))["compile"]["allow_approx"] is False


# ---- wait scope: the build names the scope it found ---------------------------------------

def _flat(tmp_path: Path) -> Path:
    prefix = RUNNER_STIL.read_text().split("Pattern MAIN")[0].replace("Period '400ns'", "Period '20000ns'")
    plain = "\n".join(f"  V {{ ALL = {'01' if i % 2 else '10'}; }}" for i in range(9000))
    source = tmp_path / "flat.stil"
    source.write_text(prefix + "Pattern MAIN {\n  W NORMAL;\n" + plain + "\n}\n")
    return source


@pytest.mark.parametrize("stil, declared, found, segmentation", [
    ("loop", "non-loop", "loop-generic", "auto"),
    ("loop", "non-loop", "loop-generic", "single"),
    ("flat", "loop-generic", "non-loop", "auto"),
    ("flat", "loop-generic", "non-loop", "single"),
])
def test_a_wrong_scope_names_the_scope_the_build_found(svc, tmp_path, stil, declared, found, segmentation):
    source = _plain_then_loop(tmp_path) if stil == "loop" else _flat(tmp_path)
    split = {"max_segment_vectors": "3000"} if segmentation == "auto" else {}
    settings = dict(loop_policy="preserve", segmentation=segmentation, **split, **WAIT)
    info = svc.init_project(request(tmp_path / "p", stil=str(source), service_scope=declared, **settings))
    failed = _wait(svc.start_build(info["project_path"]), timeout=300)
    error = failed["error"]
    assert failed["status"] == "failed"
    where = "whole segmented sequence" if segmentation == "auto" else "artifact"
    assert f"service profile scope '{declared}' does not match {where} scope '{found}'" in error
    assert f"so this build needs Scope = {found}; the project declares {declared}." in error
    assert ("including the parts without a Loop" in error) == (found == "loop-generic")
    # The named scope builds the same project.
    edited = edit(svc, svc.open_project(info["project_path"]), service_scope=found, **settings)
    done = _wait(svc.start_build(edited["project_path"]), timeout=300)
    assert done["status"] == "done", done["error"]


# ---- a structure error ---------------------------------------------------------------------

def test_a_structure_error_gives_the_analysis_reasons(svc, tmp_path):
    source = tmp_path / "typo.stil"
    source.write_text(RUNNER_STIL.read_text().replace("  W NORMAL;", "  W NORMAL_TYPO;"))
    info = svc.init_project(request(tmp_path / "p", stil=str(source), segmentation="auto"))
    failed = _wait(svc.start_build(info["project_path"]))
    assert failed["status"] == "failed"
    # Not "AttributeError: 'NoneType' object has no attribute 'get'" (the SDK's renderer, D1).
    assert failed["error"].startswith("RunnerError: source analysis is REJECT; package generation is blocked: "
                                      "ERR_WFT_NOT_FOUND")
    assert "Source analysis: " in failed["error"]


def test_rejection_text_reads_a_report_without_backend_candidates():
    report = {"status": "REJECT", "capability": None,
              "anomalies": [{"severity": "error", "code": "ERR_WFT_NOT_FOUND", "message": "WaveformTable not found: X"},
                            {"severity": "warning", "code": "WARN_X", "message": "not shown"}]}
    assert rejection_text(report) == ("source analysis is REJECT; package generation is blocked: "
                                      "ERR_WFT_NOT_FOUND: WaveformTable not found: X")


# ---- a rounded bundle carries the emitted times ---------------------------------------------

def test_a_rounded_bundle_carries_the_emitted_times(svc, tmp_path):
    # 402.5 ns -> 81 ticks = 405 ns (half up); the CLK fall 301 ns -> 60 ticks = 300 ns.
    text = counter_stil(1600).replace("Period '400ns'", "Period '402.5ns'").replace("300ns Drive(0)", "301ns Drive(0)")
    source = tmp_path / "counter.stil"
    source.write_text(text)
    info = svc.init_project(request(tmp_path / "p", stil=str(source), segmentation="auto", allow_approx=True))
    done = _wait(svc.start_build(info["project_path"]), timeout=300)
    assert done["status"] == "done", done["error"]
    bundle = done["result"]["bundle"]
    groups = {(g["kind"], g["signal"], g["original_time"], g["emitted_time_ns"], g["delta_ns"]): g["records"]
              for g in bundle["timing"]["groups"]}
    assert groups == {("period_quantization", None, "402.5ns", "405", "2.50"): 1600,
                      ("event_quantization", "CLK", "301ns", "300", "-1.0"): 1600}

    trace = load_bin(Path(bundle["bundle_dir"]) / bundle["patterns"][0]["path"])
    assert [trace.starts[k] for k in (0, 1, 1000)] == [0, 405_000, 405_000_000]  # not 402 500 ps per vector
    clk = [i for i, s in enumerate(trace.signals) if s.name == "CLK"][0]
    assert sorted(e.t for e in trace.events(5) if e.signal == clk) == [0, 100_000, 300_000]
