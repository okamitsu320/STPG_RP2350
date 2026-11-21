"""Public synthetic, host-only regressions for opt-in tick quantization."""

from dataclasses import replace
from decimal import Decimal
import hashlib
from pathlib import Path

import pytest

from stpg.pattern import LoopRefRecord, unpack_rate_vector_full
from stil.json_artifacts import read_json_artifact
from stil.pipeline import CompileOptions, compile_stil
from stil.runtime_capacity import DEFAULT_RUNTIME_PROFILE, RUNTIME_CAPACITY_PROFILES
from stpg_runner import build_project, init_project, load_project, prepare_run, verify_bundle
from stpg_runner.artifacts import RunnerError, read_json, write_json, identity, canonical_json
from stpg_runner.cli import main
from stpg_runner.project import Project, ProjectError

pytestmark = pytest.mark.stpg_host_only
FIXTURE = Path(__file__).parent / "fixtures" / "runner" / "pattern.stil"


def source(tmp_path, *, period="400ns", compare="103ns", body=None):
    text = FIXTURE.read_text(encoding="utf-8").split("Pattern MAIN")[0]
    text = text.replace("400ns", period).replace("100ns", compare)
    path = tmp_path / "public.stil"
    path.write_text(text + "Pattern MAIN {\n W NORMAL;\n" + (body or (
        "V { ALL = 01; }\nLoop 3 {\nV { ALL = 01; }\nV { ALL = 1X; }\n}\nV { ALL = 10; }\n")) + "}\n", encoding="utf-8")
    return path


def packages(result):
    manifest = read_json(result.bundle_dir / "runner.bundle.json")
    return [(result.bundle_dir / item["path"]).parent for item in manifest["patterns"]]


@pytest.mark.parametrize("policy", ["expand", "preserve"])
@pytest.mark.parametrize("cap", [None, 3])
def test_approx_opt_in_reaches_every_encoder_and_is_recorded(tmp_path, policy, cap):
    project = init_project(source(tmp_path), tmp_path / "project", wait_preset="none",
                           completion="hold", compile_mode="rate-vector", loop_policy=policy,
                           max_segment_vectors=cap, allow_approx=True)
    assert load_project(project).to_dict()["compile"]["allow_approx"] is True
    result = build_project(project)
    assert result.segment_count == (1 if cap is None else 3)
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"
    for package in packages(result):
        report = read_json_artifact(package / "pattern.compile_report.json")
        assert report["approximation_reasons"]
        records = [a for a in report["approximations"] if a["original_time"] == "103ns"]
        # Literal independent oracle: 103 ns at 200 MHz rounds to 21 ticks = 105 ns.
        if records:
            assert all(a["emitted_time_ticks"] == 21 and Decimal(a["delta_ns"]) == 2 for a in records)
        _, rates, slots, _, _ = unpack_rate_vector_full((package / "pattern.bin").read_bytes())
        assert all(r.period_ticks == 80 for r in rates)
        assert all(s.time_ticks in (0, 21) for s in slots)
        metadata = read_json_artifact(package / "pattern.metadata.json")
        assert metadata["options"]["allow_approx"] is True
    assert build_project(project).cache_hit
    assert len(prepare_run(result.bundle_dir, manifest_sha256=result.manifest_sha256).segments) == result.segment_count


@pytest.mark.parametrize("policy", ["expand", "preserve"])
@pytest.mark.parametrize("segmentation", ["auto", "single", None])
def test_exact_default_rejects_before_capacity_probes(tmp_path, monkeypatch, policy, segmentation):
    import stpg_runner.build as module
    project = init_project(source(tmp_path), tmp_path / "project", wait_preset="none", completion="hold",
                           loop_policy=policy, segmentation_mode=segmentation,
                           max_segment_vectors=3 if segmentation != "single" else None)
    def forbidden(*args, **kwargs):
        raise AssertionError("exact timing rejection must precede segmented compile")
    monkeypatch.setattr(module, "build_segmented_packages", forbidden)
    with pytest.raises((RunnerError, ValueError), match="ERR_APPROXIMATION_REQUIRED"):
        build_project(project)
    root, = (project.parent / "build").iterdir()
    assert read_json(root / "build.state.json")["status"] == "FAILED"
    assert not (root / "segmented").exists()


@pytest.mark.parametrize("value", [None, "true", "false", "approx", 0, 1, [], {}])
def test_approx_boolean_is_not_truthy_coercion(tmp_path, value):
    project = init_project(source(tmp_path), tmp_path / "project", wait_preset="none", completion="hold")
    data = read_json(project)
    data["compile"]["allow_approx"] = value
    with pytest.raises(ProjectError, match="allow_approx"):
        Project.from_dict(data)


def test_old_project_normalization_does_not_insert_approx_and_new_default_is_false(tmp_path):
    project = init_project(source(tmp_path), tmp_path / "project", wait_preset="none", completion="hold")
    data = read_json(project)
    assert data["compile"]["allow_approx"] is False
    del data["compile"]["allow_approx"]
    assert Project.from_dict(data).to_dict() == data


@pytest.mark.parametrize("period,compare,expected_period,expected_event", [
    ("402.5ns", "102.5ns", 81, 21), ("402ns", "102ns", 80, 20),
])
def test_rounding_half_up_preserve_expand_same_literal_ticks(tmp_path, period, compare, expected_period, expected_event):
    path = source(tmp_path, period=period, compare=compare)
    streams = []
    for policy in ("expand", "preserve"):
        payload, _, report = compile_stil(CompileOptions(input_path=path, mode="rate-vector",
            tick_hz=200_000_000, loop_policy=policy, allow_approx=True))
        _, rates, slots, entries, loops = unpack_rate_vector_full(payload)
        assert {r.period_ticks for r in rates} == {expected_period}
        assert {s.time_ticks for s in slots} == {0, expected_event}
        stream = []
        for entry in entries:
            block = loops[entry.loop_id] if isinstance(entry, LoopRefRecord) else None
            for record in (block.body_records * block.loop_count if block else [entry]):
                stream.extend([(record.rate_id, record.drive_value, record.drive_enable,
                                record.expect_value, record.expect_mask)] * record.repeat)
        assert len(stream) == 8
        assert report["approximations"]
        streams.append(stream)
    assert streams[0] == streams[1]


def test_cli_rounding_is_explicit_and_cache_identity_differs(tmp_path, capsys):
    path = source(tmp_path, compare="100ns")  # exact source still binds the policy
    results = []
    for enabled in (False, True):
        directory = tmp_path / ("approx" if enabled else "exact")
        args = ["init", str(path), "--directory", str(directory), "--wait-preset", "none", "--completion", "hold"]
        if enabled:
            args.append("--allow-approx")
        assert main(args) == 0
        capsys.readouterr()
        project = directory / "project.stpg-project.json"
        assert read_json(project)["compile"]["allow_approx"] is enabled
        results.append(build_project(project))
    assert results[0].bundle_dir.name != results[1].bundle_dir.name


@pytest.mark.parametrize("policy", ["expand", "preserve"])
@pytest.mark.parametrize("cap", [None, 3])
def test_auto_chunk_wait_selects_rate_vector_before_analysis(tmp_path, policy, cap):
    path = source(tmp_path, period="20000ns", compare="100ns")
    project = init_project(path, tmp_path / "project", wait_preset="chunk-4ms", completion="hold",
        service_us=1, service_source="public synthetic, declared not measured", service_scope=(
            "loop-generic" if policy == "preserve" else "non-loop"),
        max_total_wait_us=1000000, loop_policy=policy, max_segment_vectors=cap)
    result = build_project(project)
    for package in packages(result):
        report = read_json_artifact(package / "pattern.compile_report.json")
        assert report["mode_requested"] == report["mode_emitted"] == "rate-vector"
    policy_report = read_json(result.bundle_dir / "compile.policy.json")
    assert policy_report["mode_requested"] == "auto"
    assert policy_report["mode_effective"] == "rate-vector"


@pytest.mark.parametrize("allow", [False, True])
def test_event_at_period_is_not_implicitly_shifted(tmp_path, allow):
    project = init_project(source(tmp_path, compare="400ns"), tmp_path / "project",
                           wait_preset="none", completion="hold", allow_approx=allow)
    with pytest.raises(ValueError, match="ERR_EVENT_TIME_OUT_OF_RANGE"):
        build_project(project)


def test_approx_does_not_bypass_streaming_gate_even_when_bytes_already_exceed_capacity(tmp_path, monkeypatch):
    import stpg.pattern as pattern
    name = "test-quantized-streaming-and-byte-failure"
    monkeypatch.setitem(RUNTIME_CAPACITY_PROFILES, name, replace(
        RUNTIME_CAPACITY_PROFILES[DEFAULT_RUNTIME_PROFILE], name=name,
        full_expansion_event_limit=1, psram_pattern_bytes=100))
    monkeypatch.setattr(pattern, "STPG_PSRAM_PATTERN_BYTES", 100)
    body = "Loop 5000 {\nV { ALL = 01; }\nV { ALL = 1X; }\n}\n"
    project = init_project(source(tmp_path, period="42ns", compare="12ns", body=body), tmp_path / "project",
                           wait_preset="none", completion="hold", allow_approx=True)
    raw = read_json(project)
    raw["target"]["runtime_profile"] = name
    write_json(project, raw)
    with pytest.raises(ValueError, match="ERR_STREAMING_POC_TIMING"):
        build_project(project)
    root, = (project.parent / "build").iterdir()
    assert not (root / "segmented").exists()


def test_compile_policy_tampering_not_hidden_by_updating_inventory(tmp_path):
    result = build_project(init_project(source(tmp_path), tmp_path / "project", wait_preset="none",
                                       completion="hold", allow_approx=True))
    path = result.bundle_dir / "compile.policy.json"
    raw = read_json(path)
    raw["allow_approx"] = False
    write_json(path, raw)
    manifest_path = result.bundle_dir / "runner.bundle.json"
    raw = read_json(manifest_path)
    raw["files"][path.name] = identity(path)
    write_json(manifest_path, raw)
    assert verify_bundle(result.bundle_dir)["status"] == "REJECT"


@pytest.mark.parametrize("rewrite_metadata", [False, True])
def test_rehashed_exact_policy_cannot_claim_rounded_artifact_is_exact(tmp_path, rewrite_metadata):
    result = build_project(init_project(source(tmp_path), tmp_path / "project", wait_preset="none",
                                       completion="hold", allow_approx=True))
    root = result.bundle_dir
    manifest = read_json(root / "runner.bundle.json")
    project = read_json(root / "project.json")
    project["compile"]["allow_approx"] = False
    manifest["binding"]["project"] = project
    write_json(root / "project.json", project)
    manifest["cache_key"] = hashlib.sha256(canonical_json(manifest["binding"])).hexdigest()
    write_json(root / "build.state.json", {"status": "HOST_ONLY_VERIFIED", "cache_key": manifest["cache_key"]})
    policy = read_json(root / "compile.policy.json")
    policy["allow_approx"] = False
    write_json(root / "compile.policy.json", policy)
    for name in ("project.json", "build.state.json", "compile.policy.json"):
        manifest["files"][name] = identity(root / name)
    if rewrite_metadata:
        package = packages(result)[0]
        meta_path = package / "pattern.metadata.json"
        metadata = read_json(meta_path)
        metadata["options"]["allow_approx"] = False
        write_json(meta_path, metadata)
        package_manifest = package / "manifest.json"
        inner = read_json(package_manifest)
        inner["artifacts"][meta_path.name].update(identity(meta_path))
        write_json(package_manifest, inner)
        for path in (meta_path, package_manifest):
            manifest["files"][path.relative_to(root).as_posix()] = identity(path)
    write_json(root / "runner.bundle.json", manifest)
    assert verify_bundle(root)["status"] == "REJECT"


@pytest.mark.parametrize("policy", ["expand", "preserve"])
def test_off_grid_cared_compare_counts_survive_segmented_waits(tmp_path, policy):
    path = source(tmp_path, period="20000ns")
    path.write_text(path.read_text().replace("X: 103ns Compare(X)", "H: 103ns Compare(1)").replace("1X", "1H"))
    project = init_project(path, tmp_path / "project", wait_preset="chunk-4ms", completion="hold",
        service_us=1, service_source="public declared budget", service_scope=(
            "loop-generic" if policy == "preserve" else "non-loop"),
        max_total_wait_us=1000000, loop_policy=policy, max_segment_vectors=3, allow_approx=True)
    result = build_project(project)
    manifest = read_json(result.bundle_dir / "runner.bundle.json")
    assert sum(item["expected"]["events"] for item in manifest["patterns"]) == 11
    assert sum(item["expected"]["samples"] for item in manifest["patterns"]) == 3
    assert sum(item["expected"]["compares"] for item in manifest["patterns"]) == 3
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


def test_capacity_observation_does_not_reject_compatible_quantized_timing(tmp_path, monkeypatch):
    import stpg.pattern as pattern
    monkeypatch.setattr(pattern, "STPG_PSRAM_PATTERN_BYTES", 180)
    body = "\n".join("V { ALL = 01; }" if i % 2 == 0 else "V { ALL = 1X; }" for i in range(16))
    project = init_project(source(tmp_path, body=body), tmp_path / "project", wait_preset="none",
        completion="hold", compile_mode="rate-vector", loop_policy="expand", allow_approx=True)
    result = build_project(project)
    assert result.package_kind == "segmented"
    data = read_json_artifact(result.bundle_dir / "source.analysis.json")
    candidate = data["capability"]["backend_candidates"]["rate-vector"]
    assert candidate["status"] == "unavailable"
    assert candidate["estimated_event_count"] == 24
    assert candidate["streaming_timing_observation"] == "unpacked_ir_after_capacity_rejection"
    assert candidate["streaming_poc_timing_issues"] == []
    assert sum(item["expected"]["samples"] for item in read_json(result.bundle_dir / "runner.bundle.json")["patterns"]) == 8


@pytest.mark.parametrize("policy", ["expand", "preserve"])
def test_quantization_capacity_auto_and_chunk_wait_together(tmp_path, monkeypatch, policy):
    name = "test-approx-auto-samples"
    monkeypatch.setitem(RUNTIME_CAPACITY_PROFILES, name, replace(
        RUNTIME_CAPACITY_PROFILES[DEFAULT_RUNTIME_PROFILE], name=name,
        full_expansion_event_limit=1, streaming_sample_limit=2))
    project = init_project(source(tmp_path, period="20000ns"), tmp_path / "project",
        wait_preset="chunk-4ms", completion="hold", allow_approx=True, loop_policy=policy,
        service_us=1, service_source="public declared budget, not measured",
        service_scope="loop-generic" if policy == "preserve" else "non-loop", max_total_wait_us=1000000)
    data = read_json(project)
    data["target"]["runtime_profile"] = name
    write_json(project, data)
    result = build_project(project)
    bundle = read_json(result.bundle_dir / "runner.bundle.json")
    assert result.package_kind == "segmented"
    assert sum(item["expected"]["events"] for item in bundle["patterns"]) == 11
    assert sum(item["expected"]["samples"] for item in bundle["patterns"]) == 3
    assert all(item["expected"]["samples"] <= 2 for item in bundle["patterns"])
    assert read_json(result.bundle_dir / "segmentation.decision.json")["cause"] == "capacity"
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


@pytest.mark.parametrize("period,compare", [("1ns", "0ns"), ("400ns", "399ns")])
@pytest.mark.parametrize("policy", ["expand", "preserve"])
def test_rounding_cannot_admit_zero_period_or_event_rounded_to_period(tmp_path, period, compare, policy):
    project = init_project(source(tmp_path, period=period, compare=compare), tmp_path / "project",
        wait_preset="none", completion="hold", allow_approx=True, loop_policy=policy)
    with pytest.raises(ValueError, match="REJECT|period"):
        build_project(project)
    root, = (project.parent / "build").iterdir()
    assert read_json(root / "build.state.json")["status"] == "FAILED"


@pytest.mark.parametrize("mutation", ["drop_single_opt_in", "drop_segment_opt_in", "wrong_policy", "no_early_timing"])
def test_positive_and_negative_assertions_detect_mutations(tmp_path, monkeypatch, mutation):
    import stpg_runner.build as module
    if mutation in {"drop_single_opt_in", "drop_segment_opt_in"}:
        target = "build_pattern_package" if mutation == "drop_single_opt_in" else "build_segmented_packages"
        original = getattr(module, target)
        def without_opt_in(*args, **kwargs):
            if args:
                args = (replace(args[0], allow_approx=False), *args[1:])
            else:
                kwargs["allow_approx"] = False
            return original(*args, **kwargs)
        monkeypatch.setattr(module, target, without_opt_in)
        with pytest.raises(ValueError, match="approx|APPROX"):
            test_approx_opt_in_reaches_every_encoder_and_is_recorded(tmp_path, "preserve",
                                                                   None if mutation == "drop_single_opt_in" else 3)
    elif mutation == "wrong_policy":
        original = module._compile_policy
        def corrupted(settings):
            result = original(settings)
            result["allow_approx"] = False
            return result
        # Change only verifier's recomputation after building, not the fixture.
        path = init_project(source(tmp_path), tmp_path / "project", wait_preset="none",
                            completion="hold", allow_approx=True)
        result = build_project(path)
        monkeypatch.setattr(module, "_compile_policy", corrupted)
        with pytest.raises(AssertionError):
            assert verify_bundle(result.bundle_dir)["status"] == "PASS"
    else:
        import stil.source_analysis as analysis
        monkeypatch.setattr(analysis, "streaming_poc_timing_issues", lambda *args: [])
        # Losing the early diagnostic can fall through to the capacity planner;
        # the ordinary negative test then rejects that wrong reason/path.
        with pytest.raises(AssertionError):
            test_approx_does_not_bypass_streaming_gate_even_when_bytes_already_exceed_capacity(tmp_path, monkeypatch)
