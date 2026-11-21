"""Actual offline SDK/CLI builds, plus conservative fallback mutations.

Small registered test profiles exercise the real capacity assessor, planner,
compiler and verifier without a 600k-vector benchmark or any device contact.
Production profiles and firmware are not altered.
"""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil

import pytest

from stil.json_artifacts import read_json_artifact
from stil.runtime_capacity import DEFAULT_RUNTIME_PROFILE, RUNTIME_CAPACITY_PROFILES
from stpg_runner import (CancellationToken, build_project, init_project,
                         load_project, prepare_run, verify_bundle)
from stpg_runner.artifacts import RunnerError, identity, read_json, write_json
from stpg_runner.cli import main
from stpg_runner.project import Project, ProjectError
from stpg_runner.segmentation import capacity_reasons, segmentation_decision

pytestmark = pytest.mark.stpg_host_only
FIXTURE = Path(__file__).parent / "fixtures" / "runner" / "pattern.stil"


@pytest.fixture(autouse=True)
def no_contact(monkeypatch):
    import serial
    from serial.tools import list_ports

    def forbidden(*args, **kwargs):
        raise AssertionError("offline build must not enumerate/open a device")
    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(list_ports, "comports", forbidden)


def create(tmp_path, *, count=16, loop=False, **kwargs):
    source = tmp_path / "source.stil"
    prefix = FIXTURE.read_text(encoding="utf-8").split("Pattern MAIN")[0].replace("X: 100ns Compare(X)", "H: 100ns Compare(1)")
    # Every vector produces 2 events and one sample/compare; unlike a
    # drive-only constant sequence the capacity oracle has no zero-work case.
    body = f"Loop {count} {{\nV {{ ALL = 1H; }}\n}}" if loop else "V { ALL = 1H; }\n" * count
    source.write_text(prefix + "Pattern MAIN {\nW NORMAL;\n" + body + "\n}\n", encoding="utf-8")
    return init_project(source, tmp_path / "project", wait_preset="none", completion="hold", **kwargs)


def profile(project, monkeypatch, **limits):
    name = "test-runner-auto-capacity-only"
    value = replace(RUNTIME_CAPACITY_PROFILES[DEFAULT_RUNTIME_PROFILE], name=name,
                    full_expansion_event_limit=1, **limits)
    monkeypatch.setitem(RUNTIME_CAPACITY_PROFILES, name, value)
    raw = read_json(project)
    raw["target"]["runtime_profile"] = name
    write_json(project, raw)


def manifest(result):
    return read_json(result.bundle_dir / "runner.bundle.json")


def test_new_default_and_legacy_normalization(tmp_path):
    project = create(tmp_path)
    raw = read_json(project)
    assert raw["segmentation"] == {"mode": "auto", "max_vectors": None}
    del raw["segmentation"]["mode"]
    assert Project.from_dict(raw).to_dict()["segmentation"] == {"max_vectors": None}
    del raw["segmentation"]
    assert Project.from_dict(raw).to_dict()["segmentation"] == {"max_vectors": None}


@pytest.mark.parametrize("value", [True, None, "", "guess", 0, []])
def test_invalid_policy_is_not_silently_auto(tmp_path, value):
    raw = read_json(create(tmp_path))
    raw["segmentation"]["mode"] = value
    with pytest.raises(ProjectError):
        Project.from_dict(raw)


def test_single_and_cap_are_mutually_exclusive_before_directory(tmp_path):
    with pytest.raises(ProjectError, match="single"):
        create(tmp_path, segmentation_mode="single", max_segment_vectors=2)
    assert not (tmp_path / "project").exists()


@pytest.mark.parametrize("cap", [None, 16, 100000])
def test_fitting_source_is_single_cache_and_plan(tmp_path, cap):
    project = create(tmp_path, max_segment_vectors=cap)
    events = []
    result = build_project(project, on_progress=events.append)
    assert result.to_dict()["segment_count"] == 1
    assert result.package_kind == "single"
    expected = manifest(result)["patterns"][0]["expected"]
    assert (expected["events"], expected["samples"], expected["compares"]) == (32, 16, 16)
    decision = read_json(result.bundle_dir / "segmentation.decision.json")
    assert decision["cause"] == "none" and decision["planning_max_vectors"] is None
    assert any(e.stage == "source_analysis" for e in events)
    assert any(e.stage == "segmentation" and "single" in e.detail for e in events)
    hit = build_project(project)
    assert hit.cache_hit and hit.to_dict()["segment_count"] == 1
    assert len(prepare_run(result.bundle_dir, manifest_sha256=result.manifest_sha256).segments) == 1


@pytest.mark.parametrize("mode", ["auto", "rate-vector"])
@pytest.mark.parametrize("cap", [None, 16, 100000])
def test_sample_capacity_splits_even_below_vector_cap(tmp_path, monkeypatch, mode, cap):
    project = create(tmp_path, compile_mode=mode, max_segment_vectors=cap)
    profile(project, monkeypatch, streaming_sample_limit=7)
    result = build_project(project)
    data = manifest(result)
    assert result.segment_count == 3 and data["package_kind"] == "segmented"
    assert [p["expected"]["samples"] for p in data["patterns"]] == [7, 7, 2]
    assert sum(p["expected"]["events"] for p in data["patterns"]) == 32
    assert sum(p["expected"]["compares"] for p in data["patterns"]) == 16
    decision = read_json(result.bundle_dir / "segmentation.decision.json")
    assert decision["cause"] == "capacity"
    assert "ERR_RUNTIME_STREAM_SAMPLE_LIMIT" in decision["capacity_codes"]
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"
    assert build_project(project).to_dict()["segment_count"] == 3
    plan = prepare_run(result.bundle_dir, manifest_sha256=result.manifest_sha256)
    assert len(plan.segments) == 3
    from stpg import constants as c
    assert [p.terminal_state for p in plan.segments] == [c.STATE_HOLD_AWAITING_NEXT] * 2 + [c.STATE_DONE]


def test_psram_encoded_byte_capacity_uses_real_planner(tmp_path, monkeypatch):
    project = create(tmp_path, count=20)
    profile(project, monkeypatch, psram_pattern_bytes=256)
    result = build_project(project)
    data = manifest(result)
    assert result.segment_count >= 2
    assert all(item["expected"]["size_bytes"] <= 256 for item in data["patterns"])
    assert sum(item["expected"]["samples"] for item in data["patterns"]) == 20
    assert read_json(result.bundle_dir / "segmentation.decision.json")["capacity_codes"] == ["ERR_PSRAM_PATTERN_BYTES_LIMIT"]


@pytest.mark.parametrize("count", [7, 8])
def test_capacity_exact_boundary(tmp_path, monkeypatch, count):
    project = create(tmp_path, count=count)
    profile(project, monkeypatch, streaming_sample_limit=7)
    result = build_project(project)
    assert result.package_kind == ("single" if count == 7 else "segmented")
    assert sum(p["expected"]["samples"] for p in manifest(result)["patterns"]) == count


def test_explicit_vector_cap_not_number_of_segments(tmp_path):
    result = build_project(create(tmp_path, count=16, max_segment_vectors=6))
    assert result.segment_count == 3
    assert [p["expected"]["samples"] for p in manifest(result)["patterns"]] == [6, 6, 4]
    assert read_json(result.bundle_dir / "segmentation.decision.json")["cause"] == "vector_limit"


@pytest.mark.parametrize("policy", [None, "single"])
def test_legacy_and_explicit_single_do_not_fallback(tmp_path, monkeypatch, policy):
    project = create(tmp_path, segmentation_mode=policy, compile_mode="rate-vector")
    profile(project, monkeypatch, streaming_sample_limit=7)
    with pytest.raises(RunnerError, match="source analysis is REJECT.*ERR_RUNTIME_STREAM_SAMPLE_LIMIT"):
        build_project(project)
    root, = (project.parent / "build").iterdir()
    assert not (root / "segmented").exists()
    assert read_json(root / "build.state.json")["status"] == "FAILED"


@pytest.mark.parametrize("cap", [None, 2])
def test_invalid_timing_cannot_be_hidden_by_segmentation(tmp_path, monkeypatch, cap):
    project = create(tmp_path, max_segment_vectors=cap)
    profile(project, monkeypatch, streaming_sample_limit=7)
    source = project.parent / "inputs" / "pattern.stil"
    source.write_text(source.read_text().replace("100ns Compare", "500ns Compare"), encoding="utf-8")
    with pytest.raises(RunnerError, match="source analysis is REJECT"):
        build_project(project)
    root, = (project.parent / "build").iterdir()
    assert not (root / "segmented").exists()


def test_unknown_compile_failure_is_not_a_fallback(tmp_path, monkeypatch):
    import stpg_runner.build as module
    def broken(*args, **kwargs):
        raise ValueError("backend bug; exceeds something out of range")
    monkeypatch.setattr(module, "build_pattern_package", broken)
    project = create(tmp_path)
    with pytest.raises(ValueError, match="backend bug"):
        build_project(project)
    root, = (project.parent / "build").iterdir()
    assert not (root / "segmented").exists()


def test_one_vector_that_cannot_fit_is_rejected(tmp_path, monkeypatch):
    project = create(tmp_path, count=1)
    profile(project, monkeypatch, psram_pattern_bytes=1)
    with pytest.raises(RunnerError, match="cannot split one logical vector"):
        build_project(project)


def test_auto_cancel_after_analysis_never_starts_compile(tmp_path, monkeypatch):
    import stpg_runner.build as module
    from stpg_runner import BuildCancelled
    token = CancellationToken()
    def callback(event):
        if event.stage == "source_analysis" and event.event == "done":
            token.cancel()
    def forbidden(*args, **kwargs):
        raise AssertionError("compile after cancellation")
    monkeypatch.setattr(module, "build_pattern_package", forbidden)
    monkeypatch.setattr(module, "build_segmented_packages", forbidden)
    project = create(tmp_path)
    with pytest.raises(BuildCancelled):
        build_project(project, cancel=token, on_progress=callback)
    root, = (project.parent / "build").iterdir()
    assert read_json(root / "build.state.json")["status"] == "FAILED"


def test_automatic_bundle_relocation_and_decision_tampering(tmp_path):
    result = build_project(create(tmp_path, max_segment_vectors=6))
    relocated = tmp_path / "copied bundle"
    shutil.copytree(result.bundle_dir, relocated)
    assert verify_bundle(relocated, manifest_sha256=result.manifest_sha256)["status"] == "PASS"
    path = relocated / "segmentation.decision.json"
    data = read_json(path)
    data["cause"] = "none"
    write_json(path, data)
    value = read_json(relocated / "runner.bundle.json")
    value["files"][path.name] = identity(path)  # defeat just the outer hash check
    write_json(relocated / "runner.bundle.json", value)
    assert verify_bundle(relocated)["status"] == "REJECT"


def test_cli_defaults_and_actionable_single_error(tmp_path, monkeypatch, capsys):
    source = create(tmp_path).parent / "inputs" / "pattern.stil"
    target = tmp_path / "cli-project"
    assert main(["init", str(source), "--directory", str(target), "--wait-preset", "none", "--completion", "hold"]) == 0
    assert read_json(target / "project.stpg-project.json")["segmentation"]["mode"] == "auto"
    capsys.readouterr()
    project = target / "project.stpg-project.json"
    profile(project, monkeypatch, streaming_sample_limit=7)
    assert main(["build", str(project), "--progress", "jsonl"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["segment_count"] == 3
    assert '"stage": "segmentation"' in captured.err
    raw = read_json(project)
    raw["segmentation"]["mode"] = "single"
    write_json(project, raw)
    assert main(["build", str(project), "--progress", "none"]) == 2
    rejected = json.loads(capsys.readouterr().out)
    assert "ERR_RUNTIME_STREAM_SAMPLE_LIMIT" in rejected["errors"][0]
    assert rejected["device_io_performed"] is False


@pytest.mark.parametrize("change", ["unknown_global", "timing", "unknown_backend", "unknown_code", "mixed_capacity", "no_errors"])
def test_fail_closed_capacity_classifier(change):
    report = {"status": "REJECT", "anomalies": [{"severity": "error", "code": "ERR_REQUESTED_BACKEND_UNAVAILABLE"}],
              "vectors": {"logical_vector_count": 20},
              "capability": {"backend_candidates": {"rate-vector": {
                  "status": "unavailable", "error_code": "ERR_BACKEND_VALIDATION",
                  "error": "rate vector exceeds PSRAM pattern region"}}}}
    assert capacity_reasons(report, "auto") == ["ERR_PSRAM_PATTERN_BYTES_LIMIT"]  # positive control
    bad = deepcopy(report)
    candidate = bad["capability"]["backend_candidates"]["rate-vector"]
    if change in {"unknown_global", "timing"}:
        bad["anomalies"].append({"severity": "error", "code": "ERR_TIME_OUT_OF_RANGE" if change == "timing" else "FUTURE_ERROR"})
    elif change == "unknown_backend":
        candidate["error"] = "invalid slot time out of range"
    elif change == "unknown_code":
        candidate["error_code"] = "ERR_FUTURE_BACKEND"
    elif change == "mixed_capacity":
        candidate.update(status="available", runtime_capacity_assessment={
            "blocking_codes": ["ERR_PSRAM_PATTERN_BYTES_LIMIT", "ERR_UNSUPPORTED_MODE"]})
    else:
        bad["anomalies"] = []
    assert capacity_reasons(bad, "auto") == []
    settings = {"segmentation": {"mode": "auto", "max_vectors": None}, "compile": {"mode": "auto"}}
    with pytest.raises(RunnerError):
        segmentation_decision(settings, bad)


def test_real_raw_capacity_below_default_cap(tmp_path):
    # Exact public raw encoder limit (6551 events), not a mocked exception.
    project = create(tmp_path, count=4000, compile_mode="raw-event", loop_policy="expand")
    result = build_project(project)
    data = manifest(result)
    assert result.segment_count == 2
    assert sum(p["expected"]["events"] for p in data["patterns"]) == 8000
    assert sum(p["expected"]["samples"] for p in data["patterns"]) == 4000
    assert all(p["expected"]["events"] <= 6551 for p in data["patterns"])
    # Raw compare/sample segmented execution is still outside the preview;
    # automatic build must not relax that execution gate.
    with pytest.raises(RunnerError, match="raw drive-only"):
        prepare_run(result.bundle_dir, manifest_sha256=result.manifest_sha256)


@pytest.mark.parametrize("count", [6551, 6552, 524288, 524289])
def test_real_preserved_loop_sample_boundaries(tmp_path, count):
    # 6551/6552 is a firmware staging-route boundary, not a package capacity
    # boundary. This host test makes no claim about runtime staging behaviour.
    project = create(tmp_path, count=count, loop=True)
    result = build_project(project)
    data = manifest(result)
    assert sum(p["expected"]["events"] for p in data["patterns"]) == 2 * count
    assert sum(p["expected"]["samples"] for p in data["patterns"]) == count
    assert sum(p["expected"]["compares"] for p in data["patterns"]) == count
    assert result.package_kind == ("segmented" if count > 524288 else "single")
    if count > 524288:
        assert result.segment_count == 2  # no cap: fill the sample region, then the tail
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


@pytest.mark.parametrize("forbidden", [[], ["NORMAL"]])
def test_auto_segments_keep_wait_policy_and_forbidden_rate(tmp_path, forbidden):
    project = create(tmp_path, count=3200, max_segment_vectors=1600)
    raw = read_json(project)
    raw["wait"] = {"preset": "chunk-4ms", "service_us": 4000,
                   "service_source": "declared, not measured", "service_scope": "non-loop",
                   "max_total_wait_us": 80000, "forbidden_stil_rates": forbidden}
    write_json(project, raw)
    if forbidden:
        with pytest.raises(ValueError):
            build_project(project)
        root, = (project.parent / "build").iterdir()
        assert read_json(root / "build.state.json")["status"] == "FAILED"
        assert not (root / "base-package").exists()  # no retry as a single package
    else:
        result = build_project(project)
        data = manifest(result)
        assert result.segment_count == 2
        assert sum(p["expected"]["samples"] for p in data["patterns"]) == 3200
        assert verify_bundle(result.bundle_dir)["status"] == "PASS"
        segment_manifest = read_json(result.bundle_dir / data["package_manifest"])
        base = (result.bundle_dir / data["package_manifest"]).parent
        schedules = [read_json_artifact(base / s["wait_schedule"]["path"]) for s in segment_manifest["segments"]]
        assert all(any(b["wait_ticks"] > 0 for b in schedule["boundaries"]) for schedule in schedules)


@pytest.mark.parametrize("mutation", ["no_fallback", "unknown_error_allowed", "ignore_requested_cap"])
def test_acceptance_assertions_detect_disabled_guards(tmp_path, monkeypatch, mutation):
    """Deliberately weaken live functions, then execute the ordinary assertions.

    No catch-all accepts a successful broken run; a positive control exists
    in the unmutated cases above. The mutated implementation is never saved.
    """
    import stpg_runner.build as build_module
    import stpg_runner.segmentation as selection
    if mutation == "no_fallback":
        monkeypatch.setattr(selection, "capacity_reasons", lambda *args: [])
        with pytest.raises((RunnerError, AssertionError)):
            test_sample_capacity_splits_even_below_vector_cap(tmp_path, monkeypatch, "rate-vector", None)
    elif mutation == "unknown_error_allowed":
        monkeypatch.setattr(selection, "CAPACITY_CODES", selection.CAPACITY_CODES | {"FUTURE_ERROR"})
        with pytest.raises(AssertionError):
            test_fail_closed_capacity_classifier("unknown_global")
    else:
        original = build_module.segmentation_decision
        def ignore_cap(settings, analysis, **kwargs):
            settings = deepcopy(settings)
            settings["segmentation"]["max_vectors"] = None
            return original(settings, analysis, **kwargs)
        monkeypatch.setattr(build_module, "segmentation_decision", ignore_cap)
        with pytest.raises(AssertionError):
            test_explicit_vector_cap_not_number_of_segments(tmp_path)
