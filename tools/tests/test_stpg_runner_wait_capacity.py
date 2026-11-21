"""Public synthetic wait-aware capacity tests; no private data or hardware."""
from dataclasses import replace
import random

import pytest

from stpg import pattern as binary
from stil import segmented_package as segments, wait_binary
from stil.json_artifacts import read_json_artifact
from stil.pipeline import CompileOptions, compile_stil, _compile_stil_wait_probe
from stil.device_precheck import inspect_pattern_requirements
from stil.runtime_capacity import DEFAULT_RUNTIME_PROFILE, RUNTIME_CAPACITY_PROFILES
from stil.runtime_wait_model import build_runtime_chunks, iter_runtime_events
from stil.segmented import verify_segmented_manifest
from stil.wait_binary import apply_wait_schedule_to_pattern, inspect_wait_schedule_encoding
from stpg_runner import build_project, init_project, prepare_run, verify_bundle
from stpg_runner.artifacts import read_json, write_json

pytestmark = pytest.mark.stpg_host_only


def source_text(count=6000, *, shape="flat", period="400ns"):
    body = "V { ALL = 0; }\nV { ALL = 1; }\n"
    if shape == "repeat":
        body = "V { ALL = 0; }\nV { ALL = 0; }\n"
    sequence = body * (count // 2) if shape == "flat" else f"Loop {count // 2} {{\n{body}}}\n"
    return """Signals {
 D InOut;
}
SignalGroups {
 ALL = 'D';
}
Timing {
 WaveformTable NORMAL {
  Period 'PERIOD';
  Waveforms {
   D { 0: 0ns Drive(0); 1: 0ns Drive(1); }
  }
 }
}
Pattern MAIN {
 W NORMAL;
SEQUENCE}
""".replace("PERIOD", period).replace("SEQUENCE", sequence)


def region(monkeypatch, size, **limits):
    monkeypatch.setattr(binary, "STPG_PSRAM_PATTERN_BYTES", size)
    name = "public-wait-capacity-test"
    monkeypatch.setitem(RUNTIME_CAPACITY_PROFILES, name, replace(
        RUNTIME_CAPACITY_PROFILES[DEFAULT_RUNTIME_PROFILE], name=name,
        psram_pattern_bytes=size, **limits))
    return name


@pytest.mark.parametrize("period", ["400ns", "400us"])
@pytest.mark.parametrize("cap", [None, 100000])
def test_sdk_packs_the_encoded_waits_with_default_compile_settings(tmp_path, monkeypatch, cap, period):
    profile = region(monkeypatch, 50000)
    source = tmp_path / "public.stil"
    source.write_text(source_text(period=period), encoding="utf-8")
    project = init_project(source, tmp_path / "p", wait_preset="chunk-4ms", completion="hold",
                           service_us=50, service_source="public declared assumption, not measured",
                           service_scope="non-loop", max_total_wait_us=1e11, max_segment_vectors=cap)
    settings = read_json(project)
    settings["target"]["runtime_profile"] = profile
    write_json(project, settings)
    result = build_project(project)
    assert result.package_kind == "segmented"
    report = read_json_artifact(result.bundle_dir / "segmented.report.json")
    plan = report["capacity_plan"]
    assert plan["capacity_basis"] == "every-boundary-encoded-waits"
    witness = plan["decisions"]
    run_plan = prepare_run(result.bundle_dir, manifest_sha256=result.manifest_sha256)
    assert len(witness) == len(run_plan.segments) == 3
    assert sum(s.expected["events"] for s in run_plan.segments) == 6000
    assert sum(s.expected["samples"] for s in run_plan.segments) == 0
    for segment, decision in zip(run_plan.segments, witness):
        metrics = decision["metrics"]
        expected = metrics["wait_encoding"]
        assert len(segment.payload) == metrics["pattern_bytes"] == expected["pattern_bytes_after_waits"] <= 50000
        assert expected["pattern_bytes_before_waits"] <= expected["pattern_bytes_after_waits"]
    # Full segments retain encoded waits and fill the actual post-wait limit.
    for segment in run_plan.segments[:-1]:
        assert 49980 < len(segment.payload) <= 50000
        assert int.from_bytes(segment.payload[4:6], "little") == 2
    for directory in sorted((result.bundle_dir / "segmented/nominal").glob("segment-*")):
        schedule = read_json_artifact(directory / "projected.wait.json")
        assert schedule["boundaries"][0]["wait_ticks"] == 0
        local_count = sum(bool(b["wait_ticks"]) for b in schedule["boundaries"])
        segment_id = int(directory.name.split("-")[-1])
        assert local_count == witness[segment_id]["metrics"]["wait_encoding"]["nonzero_wait_count"]
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


@pytest.mark.parametrize("shape", ["flat", "repeat", "loop"])
@pytest.mark.parametrize("period", ["400ns", "400us"])
def test_projection_matches_actual_encoding_for_many_sparse_schedules(tmp_path, shape, period):
    source = tmp_path / "public.stil"
    source.write_text(source_text(24, shape=shape, period=period), encoding="utf-8")
    data, mapping, _ = compile_stil(CompileOptions(input_path=source, mode="rate-vector", loop_policy="preserve"))
    rng = random.Random(719)
    total, chunks = build_runtime_chunks(data, chunk_event_capacity=1)
    for _case in range(15):
        schedule = {"model": {"chunk_event_capacity": 1}, "boundaries": [
            {"event_start": c.event_start, "before_chunk_index": c.chunk_index,
             "wait_ticks": rng.choice([0, 1, 10, 800000]) if c.chunk_index else 0} for c in chunks]}
        assert any(b["wait_ticks"] for b in schedule["boundaries"])
        projection = inspect_wait_schedule_encoding(data, schedule=schedule, max_rates=64)
        assert projection["status"] == "PASS"
        actual, new_map, patch = apply_wait_schedule_to_pattern(data, schedule=schedule, map_data=mapping, max_rates=64)
        header, rates, slots, entries, loops = binary.unpack_rate_vector_full(actual)
        capacity = projection["capacity"]
        assert capacity["pattern_bytes_before_waits"] == len(data)
        assert capacity["pattern_bytes_after_waits"] == len(actual)
        assert capacity["vector_record_count"] == len(entries) == len(new_map["vectors"])
        assert capacity["loop_block_count"] == len(loops)
        assert capacity["slot_count"] == len(slots)
        assert projection["rate_budget"]["projected_rate_count"] == len(rates)
        assert projection["encoding"]["output_pattern_version"] == header.version
        assert len(list(iter_runtime_events(actual))) == total
        assert patch["encoding"]["capacity"] == capacity


@pytest.mark.parametrize("changes", [{}, {0: 1}, {1: 1, 2: 1, 4: 2}, {0: 2, 5: 2}, {1: 0, 3: 0}])
def test_shared_repeat_run_iterator_matches_independent_expansion(changes):
    record = binary.VectorRecord(0, 0, 6, 0, 1, 0, 0)
    runs = list(wait_binary._repeat_rate_runs(record, changes))
    assert [rate for a, b, rate in runs for _ in range(a, b)] == [changes.get(i, 0) for i in range(6)]
    assert runs[0][0] == 0 and runs[-1][1] == 6
    assert all(left[1] == right[0] and left[2] != right[2] for left, right in zip(runs, runs[1:]))


@pytest.mark.parametrize("shape", ["loop", "repeat"])
def test_waits_that_split_preserved_loops_or_repeats_drive_range_planning(tmp_path, monkeypatch, shape):
    profile = region(monkeypatch, 480)
    source = tmp_path / "public.stil"
    source.write_text(source_text(80, shape=shape), encoding="utf-8")
    result = segments.build_segmented_packages(
        input_path=source, output_dir=tmp_path / "segments", report_path=tmp_path / "report.json",
        ranges=((0, 39), (40, 79)), total_vectors=80,
        mode="rate-vector", loop_policy="preserve", runtime_profile=profile,
        tick_hz=200000000, chunk_event_capacity=4, service_time_us=0.5,
        service_time_source="public declared assumption", service_profile_scope="loop-generic",
        max_wait_us=4000, max_total_wait_us=1e11, segment_wait_plan="every-boundary",
        capacity_aware=True, capacity_first=True)
    assert len(result["segment_ranges"]) > 1
    for item, decision in zip(result["variants"][0]["segments"], result["capacity_plan"]["decisions"]):
        payload = (tmp_path / "segments/nominal" / item["path"]).read_bytes()
        expected = decision["metrics"]["wait_encoding"]
        assert len(payload) == expected["pattern_bytes_after_waits"] <= 480
        assert expected["pattern_bytes_after_waits"] > expected["pattern_bytes_before_waits"]
    assert verify_segmented_manifest(tmp_path / "segments/nominal/segmented.manifest.json")["status"] == "PASS"


@pytest.mark.parametrize("forbidden,period,expected_waits", [((), "400ns", 3), (("NORMAL",), "400ns", 0),
                                                            ((), "4ms", 0)])
def test_probe_uses_exact_boundary_exclusions_and_natural_gaps(tmp_path, forbidden, period, expected_waits):
    source = tmp_path / "public.stil"
    source.write_text(source_text(16, period=period), encoding="utf-8")
    probe = _compile_stil_wait_probe(CompileOptions(input_path=source, mode="rate-vector", practical_gate_policy="report"))
    report = segments._probe_every_boundary_encoding(
        probe.payload, rate_waveform_tables=probe.rate_waveform_tables, chunk_event_capacity=4,
        max_wait_us=4000, forbidden_stil_rate_units=forbidden, max_rates=64)
    assert report["capacity"]["nonzero_wait_count"] == expected_waits
    if not expected_waits:
        assert report["capacity"]["pattern_bytes_after_waits"] == len(probe.payload)


def test_post_wait_loop_block_limit_is_a_planning_resource_not_a_late_pack_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(wait_binary, "STPG_MAX_LOOP_BLOCKS", 3)
    monkeypatch.setattr(binary, "STPG_MAX_LOOP_BLOCKS", 3)
    profile = region(monkeypatch, 100000)
    source = tmp_path / "public.stil"
    source.write_text(source_text(80, shape="loop"), encoding="utf-8")
    options = dict(input_path=source, total_vectors=80, max_segment_vector_count=None, mode="rate-vector",
                   tick_hz=200000000, pin_map_path=None, pattern_burst=None, pattern=None,
                   strict=True, allow_approx=False, prefer_raw=False, runtime_profile=profile,
                   max_rates=64, loop_policy="preserve", segment_wait_plan="every-boundary",
                   max_wait_us=4000, chunk_event_capacity=8)
    ranges, report = segments.plan_capacity_aware_segment_ranges(**options)
    assert len(ranges) > 1
    assert all(d["metrics"]["wait_encoding"]["loop_block_count"] <= 3 for d in report["decisions"])
    result = segments.build_segmented_packages(
        input_path=source, output_dir=tmp_path / "segments", report_path=tmp_path / "report.json",
        ranges=((0, 39), (40, 79)), total_vectors=80, mode="rate-vector", loop_policy="preserve",
        runtime_profile=profile, tick_hz=200000000, chunk_event_capacity=8,
        service_time_us=0.5, service_time_source="public declared assumption", service_profile_scope="loop-generic",
        max_wait_us=4000, max_total_wait_us=1e11, segment_wait_plan="every-boundary",
        capacity_aware=True, capacity_first=True)
    assert tuple(map(tuple, result["segment_ranges"])) == ranges
    for path in (tmp_path / "segments/nominal").glob("segment-*/package/pattern.bin"):
        assert len(binary.unpack_rate_vector_full(path.read_bytes())[-1]) <= 3
    assert verify_segmented_manifest(tmp_path / "segments/nominal/segmented.manifest.json")["status"] == "PASS"


@pytest.mark.parametrize("error", [ValueError("public timing rejection"), OSError("public read failure")])
def test_wait_probe_never_searches_after_an_unrelated_failure(tmp_path, monkeypatch, error):
    source = tmp_path / "public.stil"
    source.write_text(source_text(20), encoding="utf-8")
    seen = []
    def rejected(options):
        seen.append(options.vector_range)
        raise error
    monkeypatch.setattr(segments, "_compile_stil_wait_probe", rejected)
    with pytest.raises(segments.SegmentedPackageError, match="probe rejected"):
        segments.plan_capacity_aware_segment_ranges(
            input_path=source, total_vectors=20, max_segment_vector_count=16, mode="rate-vector",
            tick_hz=200000000, pin_map_path=None, pattern_burst=None, pattern=None,
            strict=True, allow_approx=False, prefer_raw=False, runtime_profile=DEFAULT_RUNTIME_PROFILE,
            max_rates=64, loop_policy="preserve", segment_wait_plan="every-boundary", max_wait_us=4000)
    assert seen == [(0, 15)]


def test_forbidden_names_follow_actual_emitted_ids_even_for_identical_tables(tmp_path):
    prefix = source_text(2).split("Pattern MAIN")[0]
    table = prefix.split("Timing {\n", 1)[1].rsplit("}\n", 1)[0]
    prefix = prefix.replace(table, table + table.replace("NORMAL", "BLOCKED"))
    body = "V { ALL = 0; }\nV { ALL = 1; }\n"
    source = tmp_path / "public.stil"
    source.write_text(prefix + "Pattern MAIN {\n W NORMAL;\n" + body * 3
                      + " W BLOCKED;\n" + body * 3 + "}\n", encoding="utf-8")
    options = CompileOptions(input_path=source, mode="rate-vector", practical_gate_policy="report", loop_policy="preserve")
    probe = _compile_stil_wait_probe(options)
    data, mapping, _ = compile_stil(options)
    assert data == probe.payload and probe.rate_waveform_tables == ("NORMAL", "BLOCKED")
    report = segments._probe_every_boundary_encoding(
        data, rate_waveform_tables=probe.rate_waveform_tables, chunk_event_capacity=4,
        max_wait_us=4000, forbidden_stil_rate_units=("BLOCKED",), max_rates=64)
    assert report["schedule"]["nonzero_wait_count"] == 1
    assert [b["before_event"] for b in report["schedule"]["boundaries"]] == [4]
    # Independent source-map provenance, not just an SDK-generated flag.
    assert mapping["vectors"]["4"]["source_map"][0]["waveform_table"] == "NORMAL"
    assert mapping["vectors"]["8"]["source_map"][0]["waveform_table"] == "BLOCKED"


@pytest.mark.parametrize("failure", ["service", "total", "count", "scope", "unknown_rate"])
def test_capacity_fit_does_not_bypass_global_wait_admission(tmp_path, failure):
    source = tmp_path / "public.stil"
    source.write_text(source_text(40), encoding="utf-8")
    settings = dict(service_time_us=0.5, max_total_wait_us=1e11, max_wait_count=None,
                    service_profile_scope="non-loop", forbidden_stil_rate_units=())
    if failure == "service":
        settings["service_time_us"] = 10000
    elif failure == "total":
        settings["max_total_wait_us"] = 1000
    elif failure == "count":
        settings["max_wait_count"] = 0
    elif failure == "scope":
        settings["service_profile_scope"] = "loop-generic"
    else:
        settings["forbidden_stil_rate_units"] = ("UNUSED",)
    expected = "whole segmented sequence scope" if failure == "scope" else (
        "not used by this pattern" if failure == "unknown_rate" else "wait plan was not accepted")
    with pytest.raises(segments.SegmentedPackageError, match=expected):
        segments.build_segmented_packages(
            input_path=source, output_dir=tmp_path / "segments", report_path=tmp_path / "report.json",
            ranges=((0, 19), (20, 39)), total_vectors=40, mode="rate-vector", loop_policy="preserve",
            tick_hz=200000000, chunk_event_capacity=4, service_time_source="public declared assumption",
            max_wait_us=4000, segment_wait_plan="every-boundary", capacity_aware=True, **settings)


def test_probe_rate_budget_shortens_ranges_without_changing_the_wait_policy(tmp_path):
    source = tmp_path / "public.stil"
    source.write_text(source_text(20), encoding="utf-8")
    ranges, report = segments.plan_capacity_aware_segment_ranges(
        input_path=source, total_vectors=20, max_segment_vector_count=None, mode="rate-vector",
        tick_hz=200000000, pin_map_path=None, pattern_burst=None, pattern=None, strict=True,
        allow_approx=False, prefer_raw=False, runtime_profile=DEFAULT_RUNTIME_PROFILE, max_rates=1,
        loop_policy="preserve", segment_wait_plan="every-boundary", max_wait_us=4000, chunk_event_capacity=4)
    assert ranges == ((0, 3), (4, 7), (8, 11), (12, 15), (16, 19))
    assert all(d["metrics"]["wait_encoding"]["rate_budget"]["fits"] for d in report["decisions"])


def test_bad_source_rate_binding_is_rejected_not_treated_as_no_exclusions(tmp_path):
    source = tmp_path / "public.stil"
    source.write_text(source_text(16), encoding="utf-8")
    probe = _compile_stil_wait_probe(CompileOptions(input_path=source, mode="rate-vector", practical_gate_policy="report"))
    with pytest.raises(segments.SegmentedPackageError, match="source-rate binding"):
        segments._probe_every_boundary_encoding(
            probe.payload, rate_waveform_tables=(), chunk_event_capacity=4, max_wait_us=4000,
            forbidden_stil_rate_units=("NORMAL",), max_rates=64)


def test_uint32_wait_encoding_failure_is_not_a_capacity_pressure_hint(tmp_path):
    source = tmp_path / "public.stil"
    source.write_text(source_text(16), encoding="utf-8")
    probe = _compile_stil_wait_probe(CompileOptions(input_path=source, mode="rate-vector", practical_gate_policy="report"))
    with pytest.raises(segments.SegmentedPackageError, match="wide rate period exceeds uint32"):
        segments._probe_every_boundary_encoding(
            probe.payload, rate_waveform_tables=probe.rate_waveform_tables, chunk_event_capacity=4,
            max_wait_us=21474836.4, forbidden_stil_rate_units=(), max_rates=64)


@pytest.mark.parametrize("start,end", [(0, 0), (1, 1), (2, 2), (0, 2), (1, 2), (2, 4), (7, 7)])
def test_loop_first_partial_iteration_keeps_every_selected_vector_and_source(tmp_path, start, end):
    source = tmp_path / "public.stil"
    source.write_text(source_text(8, shape="loop"), encoding="utf-8")
    data, mapping, _ = compile_stil(CompileOptions(
        input_path=source, mode="rate-vector", loop_policy="preserve", vector_range=(start, end)))
    assert inspect_pattern_requirements(data).estimated_event_count == end - start + 1
    _, _, _, entries, loops = binary.unpack_rate_vector_full(data)
    levels = []
    for entry in entries:
        if isinstance(entry, binary.LoopRefRecord):
            for _ in range(loops[entry.loop_id].loop_count):
                for body in loops[entry.loop_id].body_records:
                    levels.extend([body.drive_value] * body.repeat)
        else:
            levels.extend([entry.drive_value] * entry.repeat)
    assert levels == [index % 2 for index in range(start, end + 1)]
    if start == end:
        item, = mapping["vectors"]["0"]["source_map"]
        assert item["global_vector_index"] == start and item["loop_iteration_index"] == start // 2


@pytest.mark.parametrize("mutation", ["ignore_wait_bytes", "ignore_loop_cost", "ignore_forbidden"])
def test_live_mutations_are_detected_by_positive_acceptance(tmp_path, monkeypatch, mutation):
    if mutation == "ignore_forbidden":
        original = segments._probe_every_boundary_encoding
        def mutant(*args, **kwargs):
            kwargs["forbidden_stil_rate_units"] = ()
            return original(*args, **kwargs)
        monkeypatch.setattr(segments, "_probe_every_boundary_encoding", mutant)
        with pytest.raises(AssertionError):
            test_forbidden_names_follow_actual_emitted_ids_even_for_identical_tables(tmp_path)
    else:
        if mutation == "ignore_wait_bytes":
            original = segments._probe_every_boundary_encoding
            def mutant(*args, **kwargs):
                result = original(*args, **kwargs)
                result["capacity"]["pattern_bytes_after_waits"] = result["capacity"]["pattern_bytes_before_waits"]
                return result
            monkeypatch.setattr(segments, "_probe_every_boundary_encoding", mutant)
        else:
            original = wait_binary._wait_layout
            def mutant(*args, **kwargs):
                result = original(*args, **kwargs)
                result["pattern_bytes_after_waits"] -= 20
                return result
            monkeypatch.setattr(wait_binary, "_wait_layout", mutant)
        with pytest.raises((AssertionError, ValueError)):
            test_waits_that_split_preserved_loops_or_repeats_drive_range_planning(tmp_path, monkeypatch, "loop")
