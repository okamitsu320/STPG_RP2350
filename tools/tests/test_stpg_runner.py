from __future__ import annotations

import json
from pathlib import Path
import shutil
import threading

import pytest

from stpg_runner import (
    BuildCancelled, CancellationToken, Project, ProjectError, build_project,
    init_project, load_project, verify_bundle,
)
from stpg_runner.artifacts import RunnerError, identity, read_json, write_json
from stpg_runner.cli import main
from stpg_runner.progress import Reporter

FIXTURES = Path(__file__).parent / "fixtures" / "runner"
pytestmark = pytest.mark.stpg_host_only


@pytest.fixture
def project_path(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(FIXTURES, root)
    return root / "project.stpg-project.json"


def settings() -> dict:
    return json.loads((FIXTURES / "project.stpg-project.json").read_text(encoding="utf-8"))


def test_project_normalization_and_copy_isolation() -> None:
    raw = settings()
    project = Project.from_dict(raw)
    raw["target"]["tick_hz"] = 1
    normalized = project.to_dict()
    assert normalized["target"]["tick_hz"] == 200_000_000
    assert normalized["segmentation"] == {"max_vectors": None}
    normalized["wait"]["preset"] = "chunk-4ms"
    assert project.to_dict()["wait"]["preset"] == "none"


@pytest.mark.parametrize("section,field,value", [
    (None, "schema_version", True), (None, "schema_version", 2),
    (None, "extra", "ignored"), ("target", "tick_hz", True),
    ("target", "tick_hz", 0), ("target", "tick_hz", 1 << 32),
    ("target", "max_rates", 65), ("target", "max_rates", 1.0),
    ("target", "runtime_profile", "unknown"), ("target", "runtime_profile", []),
    ("input", "stil", "../source.stil"), ("input", "stil", "/source.stil"),
    ("input", "stil", "C:/source.stil"), ("input", "stil", "a\\b.stil"),
    ("input", "stil", "a//b.stil"), ("input", "stil", "./b.stil"),
    ("input", "stil", "a:b.stil"), ("input", "pattern", ""),
    ("input", "stil", "CON.stil"), ("input", "stil", "trailing./file.stil"),
    ("input", "stil", "wild*.stil"), ("input", "stil", "control\n.stil"),
    ("compile", "mode", "unrecognized"), ("compile", "loop_policy", "auto"),
    ("execution", "completion", "auto"), ("execution", "storage", "flash"),
    ("wait", "preset", "auto"), ("wait", "service_us", 4000),
    ("wait", "forbidden_stil_rates", ["NORMAL"]),
    ("capture", "enabled", True), ("segmentation", "max_vectors", True),
    ("segmentation", "max_vectors", 0),
])
def test_project_rejects_ignored_or_invalid_fields(section, field, value) -> None:
    raw = settings()
    if section is None:
        raw[field] = value
    else:
        raw.setdefault(section, {})[field] = value
    with pytest.raises(ProjectError):
        Project.from_dict(raw)


def test_wait_budget_is_separate_from_wait_duration() -> None:
    raw = settings()
    raw["wait"] = {"preset": "chunk-12ms", "service_us": 4000,
                   "service_scope": "non-loop",
                   "service_source": "declared, unmeasured", "max_total_wait_us": 100000,
                   "forbidden_stil_rates": ["NORMAL"]}
    project = Project.from_dict(raw)
    assert project.to_dict()["wait"]["service_us"] == 4000
    assert project.to_dict()["wait"]["preset"] == "chunk-12ms"


@pytest.mark.parametrize("field,value", [
    ("service_us", None), ("service_us", True), ("service_us", float("nan")),
    ("service_us", float("inf")), ("service_us", -1),
    ("service_us", 10 ** 500), ("service_scope", "auto"), ("service_scope", None),
    ("service_source", " "), ("service_source", None),
    ("max_total_wait_us", 0), ("forbidden_stil_rates", ["NORMAL", "NORMAL"]),
    ("forbidden_stil_rates", [""]), ("forbidden_stil_rates", "NORMAL"),
])
def test_wait_invalid_inputs(field, value) -> None:
    raw = settings()
    raw["wait"] = {"preset": "chunk-4ms", "service_us": 4000,
                   "service_scope": "non-loop",
                   "service_source": "model only", "max_total_wait_us": 40000}
    raw["wait"][field] = value
    with pytest.raises(ProjectError):
        Project.from_dict(raw)


@pytest.mark.parametrize("payload", [
    '{"kind":1,"kind":2}', '{"n":NaN}', '{"n":Infinity}', '[]',
])
def test_json_rejects_duplicates_and_nonfinite(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(RunnerError):
        read_json(path)


def test_input_symlink_escape_rejected(project_path: Path, tmp_path: Path) -> None:
    path = project_path.parent / "pattern.stil"
    path.unlink()
    path.symlink_to(tmp_path / "outside.stil")
    with pytest.raises(RunnerError, match="escapes"):
        load_project(project_path).input_paths(project_path.parent)


def test_init_is_portable_and_exclusive(tmp_path: Path) -> None:
    source = FIXTURES / "pattern.stil"
    destination = tmp_path / "日本語 space"
    project_path = init_project(source, destination, wait_preset="none", completion="release")
    project = load_project(project_path)
    assert project.to_dict()["execution"]["completion"] == "release"
    assert project.input_paths(destination)["stil"].read_bytes() == source.read_bytes()
    with pytest.raises(FileExistsError):
        init_project(source, destination, wait_preset="none", completion="hold")


def test_explicit_24pin_map_is_copied_and_bound(tmp_path: Path) -> None:
    pins = tmp_path / "source-pins.json"
    write_json(pins, {"active_pins": 24, "signals": {
        "D0": {"bit": 0, "gpio": 2}, "D1": {"bit": 1, "gpio": 3},
    }})
    project_path = init_project(FIXTURES / "pattern.stil", tmp_path / "project",
                                wait_preset="none", completion="hold", pin_map=pins)
    first = build_project(project_path)
    manifest = read_json(first.bundle_dir / "runner.bundle.json")
    assert manifest["patterns"][0]["expected"]["active_pins"] == 24
    assert "pin_map" in manifest["binding"]["inputs"]
    write_json(project_path.parent / "inputs" / "pins.json", {"active_pins": 24, "signals": {
        "D0": {"bit": 2, "gpio": 4}, "D1": {"bit": 1, "gpio": 3},
    }})
    second = build_project(project_path)
    assert second.bundle_dir != first.bundle_dir


def test_invalid_init_does_not_create_directory(tmp_path: Path) -> None:
    destination = tmp_path / "not-created"
    with pytest.raises(ProjectError):
        init_project(FIXTURES / "pattern.stil", destination, wait_preset="chunk-4ms", completion="hold")
    assert not destination.exists()


def test_build_expected_counts_and_host_only_progress(project_path: Path, monkeypatch) -> None:
    import serial
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("offline operation attempted physical/process access")

    events = []
    with monkeypatch.context() as guard:
        guard.setattr(serial, "Serial", forbidden)
        guard.setattr(subprocess, "Popen", forbidden)
        result = build_project(project_path, on_progress=events.append)
        verification = verify_bundle(result.bundle_dir, manifest_sha256=result.manifest_sha256)
    assert result.cache_hit is False
    assert result.to_dict()["execution_available"] is True
    assert result.to_dict()["physical_execution_authorized"] is False
    assert result.to_dict()["device_io_performed"] is False
    assert verification["status"] == "PASS"
    manifest = read_json(result.bundle_dir / "runner.bundle.json")
    assert manifest["patterns"][0]["expected"] == {
        "size_bytes": manifest["patterns"][0]["expected"]["size_bytes"],
        "mode": "rate-vector", "tick_hz": 200_000_000, "active_pins": 16,
        "events": 6, "samples": 2, "compares": 0,
    }
    assert [event.stage for event in events if event.event == "start"] == [
        "preflight", "snapshot_inputs", "source_analysis", "compile_package", "bundle_verify",
    ]
    assert [event.elapsed_s for event in events] == sorted(event.elapsed_s for event in events)
    assert events[-1].stage == "bundle_verify" and events[-1].event == "done"
    assert all(event.total is None for event in events if event.stage == "compile_package")
    assert (result.bundle_dir / "verify.py").is_file()
    assert (result.bundle_dir / "run.py").is_file()


def test_verified_cache_does_not_compile_again(project_path: Path, monkeypatch) -> None:
    import stpg_runner.build as builder

    first = build_project(project_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("cache hit attempted compilation")
    monkeypatch.setattr(builder, "build_pattern_package", forbidden)
    second = build_project(project_path)
    assert second.cache_hit is True
    assert second.bundle_dir == first.bundle_dir
    assert second.manifest_sha256 == first.manifest_sha256


def test_changed_input_uses_new_bundle(project_path: Path) -> None:
    first = build_project(project_path)
    source = project_path.parent / "pattern.stil"
    source.write_text(source.read_text(encoding="utf-8").replace("400ns", "500ns"), encoding="utf-8")
    second = build_project(project_path)
    assert second.bundle_dir != first.bundle_dir
    assert second.cache_hit is False
    assert verify_bundle(first.bundle_dir)["status"] == "PASS"


def test_changed_tool_invalidates_cache(project_path: Path, monkeypatch) -> None:
    import stpg_runner.build as builder
    first = build_project(project_path)
    monkeypatch.setattr(builder, "tool_fingerprint", lambda: "f" * 64)
    second = build_project(project_path)
    assert second.bundle_dir != first.bundle_dir


def test_relocation_needs_no_original_source(project_path: Path, tmp_path: Path) -> None:
    result = build_project(project_path)
    relocated = tmp_path / "relocated 日本語"
    shutil.copytree(result.bundle_dir, relocated)
    source = project_path.parent / "pattern.stil"
    source.rename(source.with_suffix(".not-stil"))
    assert verify_bundle(relocated, manifest_sha256=result.manifest_sha256)["status"] == "PASS"


@pytest.mark.parametrize("mutation", ["pattern", "extra", "missing", "state", "binding", "counts", "float-counts", "path", "symlink", "unknown", "admission"])
def test_corrupt_bundle_is_rejected_and_not_rebuilt(project_path: Path, tmp_path: Path, mutation: str) -> None:
    result = build_project(project_path)
    root = result.bundle_dir
    manifest = read_json(root / "runner.bundle.json")
    if mutation == "pattern":
        (root / manifest["patterns"][0]["path"]).write_bytes(b"bad")
    elif mutation == "extra":
        (root / "unexpected.txt").write_text("not inventoried", encoding="utf-8")
    elif mutation == "missing":
        (root / "verify.py").unlink()
    elif mutation == "state":
        write_json(root / "build.state.json", {"status": "BUILDING"})
    elif mutation == "binding":
        manifest["binding"]["project"]["target"]["tick_hz"] = 1
    elif mutation == "counts":
        manifest["patterns"][0]["expected"]["events"] = 5
    elif mutation == "float-counts":
        manifest["patterns"][0]["expected"]["events"] = 6.0
    elif mutation == "path":
        manifest["package_manifest"] = "../outside.json"
    elif mutation == "unknown":
        manifest["shell_hook"] = "NEVER_EXECUTE"
    elif mutation == "admission":
        manifest["physical_execution_authorized"] = True
    else:
        path = root / "verify.py"
        external = tmp_path / "outside.py"
        shutil.copyfile(path, external)
        path.unlink()
        path.symlink_to(external)
    write_json(root / "runner.bundle.json", manifest)
    before = (root / "runner.bundle.json").read_bytes()
    assert verify_bundle(root)["status"] == "REJECT"
    with pytest.raises(RunnerError, match="not overwritten"):
        build_project(project_path)
    assert (root / "runner.bundle.json").read_bytes() == before


def test_wrong_manifest_pin_rejected(project_path: Path) -> None:
    result = build_project(project_path)
    assert verify_bundle(result.bundle_dir, manifest_sha256="0" * 64)["status"] == "REJECT"


def test_failure_reservation_is_retained(project_path: Path, monkeypatch) -> None:
    import stpg_runner.build as builder
    def broken(*args, **kwargs):
        raise ValueError("synthetic compile rejection")
    monkeypatch.setattr(builder, "build_pattern_package", broken)
    with pytest.raises(ValueError, match="synthetic"):
        build_project(project_path)
    roots = list((project_path.parent / "build").iterdir())
    assert len(roots) == 1
    assert read_json(roots[0] / "build.state.json")["status"] == "FAILED"
    assert not (roots[0] / "runner.bundle.json").exists()
    with pytest.raises(RunnerError, match="not overwritten"):
        build_project(project_path)


def test_cancel_before_build_has_no_outputs(project_path: Path) -> None:
    cancel = CancellationToken()
    cancel.cancel()
    with pytest.raises(BuildCancelled):
        build_project(project_path, cancel=cancel)
    assert not (project_path.parent / "build").exists()


def test_cancel_after_snapshot_retains_prefix(project_path: Path) -> None:
    cancel = CancellationToken()
    def callback(event):
        if event.stage == "snapshot_inputs" and event.event == "done":
            cancel.cancel()
    with pytest.raises(BuildCancelled):
        build_project(project_path, on_progress=callback, cancel=cancel)
    root = next((project_path.parent / "build").iterdir())
    assert read_json(root / "build.state.json")["status"] == "FAILED"
    assert verify_bundle(root)["status"] == "REJECT"


def test_progress_heartbeat_is_elapsed_time_not_fabricated_percentage() -> None:
    arrived = threading.Event()
    events = []
    def callback(event):
        events.append(event)
        if event.event == "heartbeat":
            arrived.set()
    reporter = Reporter(callback, heartbeat_interval=0.01)
    assert reporter.perform("slow_host_stage", lambda: arrived.wait(1.0)) is True
    assert [event.event for event in events][0] == "start"
    assert events[-1].event == "done"
    assert all(event.completed is None and event.total is None for event in events)
    assert any(event.event == "heartbeat" for event in events)
    assert [event.elapsed_s for event in events] == sorted(event.elapsed_s for event in events)


def test_progress_heartbeat_callback_failure_propagates() -> None:
    arrived = threading.Event()
    def callback(event):
        if event.event == "heartbeat":
            arrived.set()
            raise RuntimeError("synthetic progress sink failure")
    reporter = Reporter(callback, heartbeat_interval=0.01)
    with pytest.raises(RuntimeError, match="sink failure"):
        reporter.perform("slow_host_stage", lambda: arrived.wait(1.0))


def test_progress_callback_failure_retains_failed_build(project_path: Path) -> None:
    def callback(event):
        if event.stage == "compile_package" and event.event == "start":
            raise RuntimeError("synthetic progress failure")
    with pytest.raises(RuntimeError, match="progress failure"):
        build_project(project_path, on_progress=callback)
    root = next((project_path.parent / "build").iterdir())
    assert read_json(root / "build.state.json")["status"] == "FAILED"
    assert verify_bundle(root)["status"] == "REJECT"


def test_disk_full_preflight_leaves_no_reservation(project_path: Path, monkeypatch) -> None:
    import stpg_runner.build as builder
    usage = shutil.disk_usage(project_path.parent)
    monkeypatch.setattr(builder.shutil, "disk_usage", lambda path: usage._replace(free=0))
    with pytest.raises(RunnerError, match="insufficient"):
        build_project(project_path)
    assert not list((project_path.parent / "build").iterdir())


def test_build_rejects_input_changed_during_compile(project_path: Path) -> None:
    def callback(event):
        if event.stage == "compile_package" and event.event == "done":
            source = project_path.parent / "pattern.stil"
            source.write_text("changed", encoding="utf-8")
    with pytest.raises(RunnerError, match="changed during build"):
        build_project(project_path, on_progress=callback)


def test_segmented_build_and_relocation(project_path: Path, tmp_path: Path) -> None:
    raw = read_json(project_path)
    raw["segmentation"] = {"max_vectors": 2}
    write_json(project_path, raw)
    result = build_project(project_path)
    manifest = read_json(result.bundle_dir / "runner.bundle.json")
    assert manifest["package_kind"] == "segmented"
    assert len(manifest["patterns"]) == 2
    assert [item["expected"]["events"] for item in manifest["patterns"]] == [3, 3]
    assert [item["expected"]["samples"] for item in manifest["patterns"]] == [1, 1]
    relocated = tmp_path / "segmented relocated"
    shutil.copytree(result.bundle_dir, relocated)
    assert verify_bundle(relocated)["status"] == "PASS"


def test_segmented_external_reference_rejected_before_legacy_verifier(project_path: Path, monkeypatch) -> None:
    import stpg_runner.build as builder
    raw = read_json(project_path)
    raw["segmentation"] = {"max_vectors": 2}
    write_json(project_path, raw)
    result = build_project(project_path)
    bundle = read_json(result.bundle_dir / "runner.bundle.json")
    relative = bundle["package_manifest"]
    path = result.bundle_dir / relative
    manifest = read_json(path)
    manifest["segments"][0]["path"] = "../../../../outside.bin"
    write_json(path, manifest)
    bundle["files"][relative] = identity(path)
    write_json(result.bundle_dir / "runner.bundle.json", bundle)
    def forbidden(*args, **kwargs):
        raise AssertionError("unsafe reference reached the legacy reader")
    monkeypatch.setattr(builder, "verify_segmented_manifest", forbidden)
    assert verify_bundle(result.bundle_dir)["status"] == "REJECT"


@pytest.mark.parametrize("preset", ["chunk-4ms", "chunk-12ms"])
def test_explicit_wait_build_retains_base_and_verifies(project_path: Path, preset: str) -> None:
    raw = read_json(project_path)
    raw["wait"] = {"preset": preset, "service_us": 4000,
                   "service_scope": "non-loop",
                   "service_source": "unmeasured synthetic model budget", "max_total_wait_us": 100000}
    write_json(project_path, raw)
    result = build_project(project_path)
    assert (result.bundle_dir / "base-package" / "pattern.bin").is_file()
    assert (result.bundle_dir / "wait.proposal.json").is_file()
    assert read_json(result.bundle_dir / "wait.acceptance.json")["status"] == "ACCEPTED"
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


def _long_source(project_path: Path, *, count: int, loop: bool) -> None:
    source = project_path.parent / "pattern.stil"
    prefix = source.read_text(encoding="utf-8").split("Pattern MAIN")[0]
    if loop:
        body = f"Loop {count} {{\n  V {{ ALL = 1X; }}\n}}"
    else:
        body = "\n".join("V { ALL = 1X; }" for _ in range(count))
    source.write_text(prefix + "Pattern MAIN {\nW NORMAL;\n" + body + "\n}\n", encoding="utf-8")


@pytest.mark.parametrize("loop", [False, True])
def test_nonzero_wait_application_and_scope(project_path: Path, loop: bool) -> None:
    _long_source(project_path, count=1600, loop=loop)
    raw = read_json(project_path)
    raw["wait"] = {"preset": "chunk-4ms", "service_us": 4000,
                   "service_scope": "loop-generic" if loop else "non-loop",
                   "service_source": "synthetic, not measured", "max_total_wait_us": 40000}
    write_json(project_path, raw)
    result = build_project(project_path)
    schedule = read_json(result.bundle_dir / "wait.proposed.json")
    assert any(boundary["wait_ticks"] > 0 for boundary in schedule["boundaries"])
    assert (result.bundle_dir / "package" / "pattern.bin").is_file()
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


def test_wrong_service_scope_rejected(project_path: Path) -> None:
    raw = read_json(project_path)
    raw["wait"] = {"preset": "chunk-4ms", "service_us": 4000,
                   "service_scope": "loop-generic", "service_source": "declared for wrong scope",
                   "max_total_wait_us": 40000}
    write_json(project_path, raw)
    with pytest.raises(ValueError, match="scope"):
        build_project(project_path)


def test_forbidden_wait_plan_rejects_without_hidden_adjustment(project_path: Path) -> None:
    _long_source(project_path, count=1600, loop=False)
    raw = read_json(project_path)
    raw["wait"] = {"preset": "chunk-4ms", "service_us": 4000, "service_scope": "non-loop",
                   "service_source": "synthetic", "max_total_wait_us": 40000,
                   "forbidden_stil_rates": ["MISSING_RATE"]}
    write_json(project_path, raw)
    with pytest.raises(ValueError):
        build_project(project_path)
    root = next((project_path.parent / "build").iterdir())
    assert read_json(root / "build.state.json")["status"] == "FAILED"
    assert verify_bundle(root)["status"] == "REJECT"


def test_segmented_explicit_waits_and_relocation(project_path: Path, tmp_path: Path) -> None:
    _long_source(project_path, count=3200, loop=False)
    raw = read_json(project_path)
    raw["segmentation"] = {"max_vectors": 1600}
    raw["wait"] = {"preset": "chunk-4ms", "service_us": 4000, "service_scope": "non-loop",
                   "service_source": "synthetic, not measured", "max_total_wait_us": 80000}
    write_json(project_path, raw)
    result = build_project(project_path)
    manifest = read_json(result.bundle_dir / "runner.bundle.json")
    assert manifest["package_kind"] == "segmented"
    assert sum(item["expected"]["samples"] for item in manifest["patterns"]) == 3200
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"
    relocated = tmp_path / "waited segmented relocated"
    shutil.copytree(result.bundle_dir, relocated)
    assert verify_bundle(relocated, manifest_sha256=result.manifest_sha256)["status"] == "PASS"


def test_cli_json_output_and_progress(project_path: Path, capsys) -> None:
    assert main(["build", str(project_path), "--progress", "jsonl"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["status"] == "HOST_ONLY_VERIFIED"
    assert all(json.loads(line)["kind"] == "stpg_runner_progress" for line in captured.err.splitlines())
    assert main(["verify", report["bundle_dir"], "--manifest-sha256", report["manifest_sha256"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def test_cli_rejection_and_no_device_entrypoint(tmp_path: Path, capsys) -> None:
    assert main(["verify", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out)["device_io_performed"] is False
    with pytest.raises(SystemExit) as exc:
        main(["run", str(tmp_path), "--port", "NEVER_OPEN"])
    assert exc.value.code == 2
