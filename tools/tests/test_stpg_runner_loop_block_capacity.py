"""Real 128-block boundary, safe segmentation and independent rejection guards.

All sources are public synthetic STIL. No firmware, serial metadata or device I/O.
"""
from copy import deepcopy

import pytest

from stpg import pattern as binary
from stpg.constants import STPG_MAX_LOOP_BLOCKS
from stil.errors import StilError
from stil.json_artifacts import read_json_artifact
from stil.pipeline import CompileOptions, compile_stil
from stil.segmented import verify_segmented_manifest
from stil import segmented_package as planner
from stil.source_analysis import analyze_stil_source
from stpg_runner import build_project, init_project, prepare_run, verify_bundle
from stpg_runner.artifacts import read_json
from stpg_runner.segmentation import capacity_reasons

pytestmark = pytest.mark.stpg_host_only
LOOP_ERROR = "preserved loop package exceeds STPG_MAX_LOOP_BLOCKS=128"


def public_source(blocks, *, repeat=3, one_record=False, period="200ns", sample="100ns"):
    second = "0H" if one_record else "1L"
    header = f"""Signals {{
 D InOut;
 Q InOut;
}}
SignalGroups {{
 ALL = 'D + Q';
}}
Timing {{
 WaveformTable NORMAL {{
  Period '{period}';
  Waveforms {{
   D {{ 0: 0ns Drive(0); 1: 0ns Drive(1); }}
   Q {{ H: {sample} Compare(1); L: {sample} Compare(0); }}
  }}
 }}
}}
Pattern MAIN {{
 W NORMAL;
"""
    body = f" V {{ ALL = 0H; }}\n Loop {repeat} {{\n  V {{ ALL = 0H; }}\n  V {{ ALL = {second}; }}\n }}\n"
    return header + body * blocks + "}\n"


def create(tmp_path, *, blocks=129, mode="auto", cap=None, policy="auto", loop_policy="preserve", wait=False, **source):
    path = tmp_path / "public.stil"
    path.write_text(public_source(blocks, **source), encoding="utf-8")
    wait_options = (dict(service_us=0.5, service_source="public declared test assumption, not measured",
                         service_scope="loop-generic", max_total_wait_us=1e10) if wait else {})
    project = init_project(path, tmp_path / "project", wait_preset="chunk-4ms" if wait else "none",
                           completion="hold", compile_mode=mode, max_segment_vectors=cap,
                           segmentation_mode=policy, loop_policy=loop_policy, **wait_options)
    return path, project


@pytest.mark.parametrize("mode", ["auto", "rate-vector"])
@pytest.mark.parametrize("blocks,cap", [(129, None), (200, 400), (200, 100000)])
def test_loop_block_capacity_selects_real_verified_preserved_segments(tmp_path, blocks, cap, mode):
    source, project = create(tmp_path, blocks=blocks, mode=mode, cap=cap)
    result = build_project(project)
    assert result.package_kind == "segmented"
    decision = read_json(result.bundle_dir / "segmentation.decision.json")
    assert decision["cause"] == "capacity" and decision["capacity_codes"] == ["ERR_TOO_MANY_LOOP_BLOCKS"]
    manifest = read_json(result.bundle_dir / "runner.bundle.json")
    segmented = read_json(result.bundle_dir / manifest["package_manifest"])
    ranges = segmented["pattern_contract"]["split_ranges"]
    assert ranges[0][0] == 0 and ranges[-1][1] == 7 * blocks - 1
    assert all(a[1] + 1 == b[0] for a, b in zip(ranges, ranges[1:]))
    if cap:
        assert all(end - start + 1 <= cap for start, end in ranges)
    plan = prepare_run(result.bundle_dir, manifest_sha256=result.manifest_sha256)
    counts = []
    for (start, end), segment in zip(ranges, plan.segments, strict=True):
        loops = binary.unpack_rate_vector_full(segment.payload)[-1]
        counts.append(len(loops))
        assert len(loops) <= STPG_MAX_LOOP_BLOCKS == 128
        cold, _, _ = compile_stil(CompileOptions(input_path=source, mode=mode, loop_policy="preserve",
                                                strict=True, vector_range=(start, end)))
        assert segment.payload == cold
    assert any(count > 0 for count in counts)  # not a hidden expand fallback
    assert sum(s.expected["events"] for s in plan.segments) == 14 * blocks
    assert sum(s.expected["samples"] for s in plan.segments) == 7 * blocks
    assert sum(s.expected["compares"] for s in plan.segments) == 7 * blocks
    assert all(a["final_hold_state"] == b["entry_hold_state"] for a, b in zip(segmented["segments"], segmented["segments"][1:]))
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


@pytest.mark.parametrize("blocks,one_record,loop_policy", [(128, False, "preserve"),
                                                          (200, True, "preserve"), (200, False, "expand")])
def test_fitting_boundary_repeat_only_and_expand_controls_remain_single(tmp_path, blocks, one_record, loop_policy):
    source, project = create(tmp_path, blocks=blocks, one_record=one_record, loop_policy=loop_policy)
    result = build_project(project)
    assert result.package_kind == "single"
    manifest = read_json(result.bundle_dir / "runner.bundle.json")
    payload = (result.bundle_dir / manifest["patterns"][0]["path"]).read_bytes()
    if loop_policy == "preserve":
        assert len(binary.unpack_rate_vector_full(payload)[-1]) == (128 if not one_record else 0)
    expected, _, _ = compile_stil(CompileOptions(input_path=source, loop_policy=loop_policy, mode="auto", strict=True))
    assert payload == expected and verify_bundle(result.bundle_dir)["status"] == "PASS"


def test_source_analysis_remains_reject_but_has_precise_capacity_advice(tmp_path):
    source, project = create(tmp_path, mode="rate-vector")
    report = analyze_stil_source(source, mode="rate-vector", loop_policy="preserve")
    assert report["status"] == "REJECT" and report["decision"]["generation_ready"] is False
    assert report["segmentation"]["available"] is True
    assert report["segmentation"]["reason_code"] == "ERR_TOO_MANY_LOOP_BLOCKS"
    assert capacity_reasons(report, "rate-vector") == ["ERR_TOO_MANY_LOOP_BLOCKS"]
    candidate = report["capability"]["backend_candidates"]["rate-vector"]
    assert candidate["status"] == "unavailable" and candidate["error_code"] == "ERR_TOO_MANY_LOOP_BLOCKS"
    assert candidate["error"] == LOOP_ERROR


def test_single_policy_still_rejects_without_segment_output(tmp_path):
    _, project = create(tmp_path, policy="single")
    with pytest.raises(ValueError, match="source analysis is REJECT"):
        build_project(project)
    root, = (project.parent / "build").iterdir()
    assert read_json(root / "build.state.json")["status"] == "FAILED"
    assert not (root / "segmented").exists() and not (root / "runner.bundle.json").exists()


@pytest.mark.parametrize("mode", ["auto", "rate-vector"])
def test_loop_limit_does_not_hide_independent_streaming_timing_rejection(tmp_path, mode):
    source, project = create(tmp_path, mode=mode, repeat=30, sample="5ns")
    report = analyze_stil_source(source, mode=mode, loop_policy="preserve")
    candidate = report["capability"]["backend_candidates"]["rate-vector"]
    assert candidate["error_code"] == "ERR_TOO_MANY_LOOP_BLOCKS"
    assert candidate["streaming_timing_observation"] == "unpacked_ir_after_capacity_rejection"
    assert candidate["estimated_event_count"] > 6551
    assert "ERR_STREAMING_POC_TIMING" in report["decision"]["blocking_codes"]
    assert capacity_reasons(report, mode) == []
    with pytest.raises(ValueError, match="source analysis is REJECT"):
        build_project(project)
    root, = (project.parent / "build").iterdir()
    assert not (root / "segmented").exists()


@pytest.mark.parametrize("depth", [0, 1, 2])
def test_typed_loop_capacity_leaf_and_known_auto_wrappers_are_recognized(depth):
    error = StilError("ERR_TOO_MANY_LOOP_BLOCKS", LOOP_ERROR)
    for _ in range(depth):
        wrapper = StilError("ERR_LOOP_BACKEND_UNSUPPORTED", planner._PRESERVED_RATE_FAILURE)
        wrapper.__cause__ = error
        error = wrapper
    assert planner._capacity_failure(error) is True


@pytest.mark.parametrize("case", ["wrong_type", "wrong_code", "substring", "cause", "unknown_wrapper", "cycle"])
def test_related_words_or_unrelated_causes_do_not_authorize_splitting(case):
    error = StilError("ERR_TOO_MANY_LOOP_BLOCKS", LOOP_ERROR)
    if case == "wrong_type":
        error = ValueError(LOOP_ERROR)
    elif case == "wrong_code":
        error.code = "ERR_NESTED_LOOP_UNSUPPORTED"
    elif case == "substring":
        error = StilError("ERR_TOO_MANY_LOOP_BLOCKS", LOOP_ERROR + "; public unrelated failure")
    elif case == "cause":
        error.__cause__ = OSError("public I/O error")
    elif case == "unknown_wrapper":
        outer = StilError("ERR_PARSER", "public unrelated outer error")
        outer.__cause__ = error
        error = outer
    else:
        error.__cause__ = error
    assert planner._capacity_failure(error) is False


@pytest.mark.parametrize("cap", [400, 100000])
def test_cli_analyze_short_source_can_split_for_loop_capacity_and_reports_actual_count(tmp_path, cap, capsys):
    from stil.cli_analyze import main
    source, _ = create(tmp_path, blocks=300)
    output, segments = tmp_path / "analysis.json", tmp_path / "segments"
    assert main([str(source), "--mode", "rate-vector", "--loop-policy", "preserve",
                 "--segment-on-capacity", "--segment-vector-count", str(cap), "--output", str(output),
                 "--segment-output-dir", str(segments), "--segment-json-out", str(tmp_path / "segments.json")]) == 0
    report = read_json_artifact(output)
    actual = read_json_artifact(tmp_path / "segments.json")["segment_ranges"]
    assert report["status"] == "REJECT" and report["segmentation"]["status"] == "COMPLETE"
    assert report["segmentation"]["segment_count"] == len(actual) > 1
    assert verify_segmented_manifest(segments / "nominal/segmented.manifest.json")["status"] == "PASS"


def test_cli_segment_package_accepts_nonbinding_vector_cap_for_a_short_capacity_failure(tmp_path, capsys):
    from stil.cli_segment_package import main
    source, _ = create(tmp_path)
    output = tmp_path / "segments"
    assert main([str(source), "--mode", "rate-vector", "--loop-policy", "preserve",
                 "--segment-vector-count", "100000", "--output-dir", str(output),
                 "--json-out", str(tmp_path / "segments.json")]) == 0
    assert verify_segmented_manifest(output / "nominal/segmented.manifest.json")["status"] == "PASS"


@pytest.mark.parametrize("period", ["200ns", "400us"])
def test_loop_limit_and_encoded_wait_blocks_fit_together_in_narrow_and_wide_sources(tmp_path, period):
    from stil.wait_binary import apply_wait_schedule_to_pattern
    source, project = create(tmp_path, repeat=10, wait=True, period=period)
    result = build_project(project)
    assert result.package_kind == "segmented"
    plan = prepare_run(result.bundle_dir, manifest_sha256=result.manifest_sha256)
    report = read_json_artifact(result.bundle_dir / "segmented.report.json")
    ranges = report["segment_ranges"]
    saw_wait = False
    for span, segment in zip(ranges, plan.segments, strict=True):
        directory = (result.bundle_dir / segment.path).parent.parent
        schedule = read_json_artifact(directory / "projected.wait.json")
        assert schedule["boundaries"][0]["wait_ticks"] == 0
        cold, mapping, _ = compile_stil(CompileOptions(input_path=source, mode="rate-vector", loop_policy="preserve",
                                                      vector_range=tuple(span), strict=True))
        if any(b["wait_ticks"] for b in schedule["boundaries"]):
            saw_wait = True
            expected, _, _ = apply_wait_schedule_to_pattern(cold, schedule=schedule, map_data=mapping, max_rates=64)
        else:
            expected = cold
        assert segment.payload == expected
        assert len(binary.unpack_rate_vector_full(segment.payload)[-1]) <= 128
    if period == "200ns":
        assert saw_wait  # must actually exercise encoded wait/Loop capacity
    assert sum(s.expected["samples"] for s in plan.segments) == 129 * 21
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


@pytest.mark.parametrize("case", ["missing_proof", "numeric_proof", "wrong_code", "wrong_text", "extra_error", "available"])
def test_source_report_does_not_generalize_loop_capacity_to_unrelated_errors(tmp_path, case):
    source, _ = create(tmp_path)
    report = analyze_stil_source(source, mode="auto", loop_policy="preserve")
    assert capacity_reasons(report, "auto") == ["ERR_TOO_MANY_LOOP_BLOCKS"]
    report = deepcopy(report)
    candidate = report["capability"]["backend_candidates"]["rate-vector"]
    if case == "missing_proof":
        candidate.pop("loop_block_capacity")
    elif case == "numeric_proof":
        candidate["loop_block_capacity"] = 1
    elif case == "wrong_code":
        candidate["error_code"] = "ERR_NESTED_LOOP_UNSUPPORTED"
    elif case == "wrong_text":
        candidate["error"] += "; not the encoder capacity leaf"
    elif case == "extra_error":
        report["anomalies"].append({"severity": "error", "code": "ERR_PARSER", "message": "public independent error"})
    else:
        candidate["status"] = "available"
        candidate["runtime_capacity_assessment"] = {"blocking_codes": ["ERR_TOO_MANY_LOOP_BLOCKS"]}
    assert capacity_reasons(report, "auto") == []


@pytest.mark.parametrize("mutation", ["source", "selection", "probe", "broad_message"])
def test_real_build_and_negative_controls_detect_live_capacity_gate_mutations(tmp_path, monkeypatch, mutation):
    if mutation == "broad_message":
        monkeypatch.setattr(planner, "_is_loop_block_capacity_error", lambda code, text: code == "ERR_TOO_MANY_LOOP_BLOCKS")
        with pytest.raises(AssertionError):
            test_related_words_or_unrelated_causes_do_not_authorize_splitting("substring")
        return
    if mutation == "source":
        import stil.source_analysis as target
    elif mutation == "selection":
        import stpg_runner.segmentation as target
    else:
        target = planner
    monkeypatch.setattr(target, "_is_loop_block_capacity_error", lambda code, text: False)
    with pytest.raises(ValueError, match="source analysis is REJECT|capacity-first probe rejected"):
        test_loop_block_capacity_selects_real_verified_preserved_segments(tmp_path, 129, None, "auto")


def test_source_analysis_does_not_mint_capacity_proof_for_a_leaf_with_an_unrelated_cause(tmp_path, monkeypatch):
    import stil.source_analysis as analysis
    source, _ = create(tmp_path)
    error = StilError("ERR_TOO_MANY_LOOP_BLOCKS", LOOP_ERROR)
    error.__cause__ = OSError("public unrelated I/O cause")
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(analysis, "compile_rate_vector", fail)
    report = analysis.analyze_stil_source(source, mode="rate-vector", loop_policy="preserve")
    candidate = report["capability"]["backend_candidates"]["rate-vector"]
    assert candidate.get("loop_block_capacity") is not True
    assert not report["segmentation"]["available"] and capacity_reasons(report, "rate-vector") == []


@pytest.mark.parametrize("kind", ["unknown_loop", "wrong_type", "extra_cause"])
def test_unknown_probe_failure_stops_before_trying_a_shorter_range(tmp_path, monkeypatch, kind):
    _, project = create(tmp_path)
    if kind == "unknown_loop":
        error = StilError("ERR_NESTED_LOOP_UNSUPPORTED", LOOP_ERROR)
    elif kind == "wrong_type":
        error = ValueError(LOOP_ERROR)
    else:
        error = StilError("ERR_TOO_MANY_LOOP_BLOCKS", LOOP_ERROR)
        error.__cause__ = OSError("public unrelated cause")
    seen = []
    def fail(options):
        seen.append(options.vector_range)
        raise error
    monkeypatch.setattr(planner, "_compile_stil_binary_probe", fail)
    with pytest.raises(planner.SegmentedPackageError, match="capacity-first probe rejected"):
        build_project(project)
    assert seen == [(0, 902)]


def test_manually_requested_split_points_are_not_silently_moved_to_fit(tmp_path, capsys):
    from stil.cli_segment_package import main
    source, _ = create(tmp_path)
    output = tmp_path / "segments"
    assert main([str(source), "--mode", "rate-vector", "--loop-policy", "preserve",
                 "--split-at", "0", "--output-dir", str(output),
                 "--json-out", str(tmp_path / "segments.json")]) == 2
    assert not (output / "nominal/segmented.manifest.json").exists()
