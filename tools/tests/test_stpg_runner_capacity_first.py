"""Capacity search bounds plus real compiler/capacity/HOLD tests; public data."""
import pytest

from stil import segmented_package as planner
from stil.json_artifacts import read_json_artifact
from stil.runtime_capacity import DEFAULT_RUNTIME_PROFILE
from stpg_runner import build_project, prepare_run, verify_bundle
from stpg_runner.artifacts import identity, read_json, write_json
from stpg_runner.segmentation import segmentation_decision
from test_stpg_runner_auto_segmentation import create, profile, manifest, no_contact

pytestmark = pytest.mark.stpg_host_only


@pytest.mark.parametrize("cap,expected", [(None, [700, 700, 150]), (500, [500, 500, 500, 50])])
def test_growth_is_bounded_by_capacity_not_the_probe_or_explicit_cap(tmp_path, monkeypatch, cap, expected):
    source = tmp_path / "public.stil"
    source.write_text("// mock compile probes, not device input")
    seen = []
    def compile(options):
        start, end = options.vector_range
        size = end - start + 1
        seen.append((start, size))
        return size.to_bytes(4, "little"), None
    class Requirements:
        def __init__(self, count):
            self.estimated_event_count = count
            self.estimated_sample_count = count
            self.estimated_compare_count = count
    monkeypatch.setattr(planner, "_compile_stil_binary_probe", compile)
    monkeypatch.setattr(planner, "inspect_pattern_requirements", lambda raw: Requirements(int.from_bytes(raw, "little")))
    monkeypatch.setattr(planner, "assess_runtime_capacity", lambda req, **kw: [
        {"severity": "error", "code": "ERR_RUNTIME_STREAM_SAMPLE_LIMIT"}] if req.estimated_sample_count > 700 else [])
    class Prepared:
        def clear_resolution(self):
            pass
    ranges, report = planner.plan_capacity_aware_segment_ranges(
        input_path=source, total_vectors=1550, max_segment_vector_count=cap,
        initial_probe_vector_count=100, mode="rate-vector", tick_hz=200_000_000,
        pin_map_path=None, pattern_burst=None, pattern=None, strict=True, allow_approx=False,
        prefer_raw=False, runtime_profile=DEFAULT_RUNTIME_PROFILE, max_rates=64,
        loop_policy="preserve", prepared_source=Prepared())
    assert [end - start + 1 for start, end in ranges] == expected
    assert ranges[0][0] == 0 and ranges[-1][1] == 1549
    assert all(ranges[i][1] + 1 == ranges[i + 1][0] for i in range(len(ranges) - 1))
    if cap is None:
        assert seen[:4] == [(0, 100), (0, 200), (0, 400), (0, 800)]
        assert report["schema_version"] == 2 and report["max_segment_vector_count"] is None
        assert report["planning_strategy"] == "capacity-first"
    else:
        assert max(size for _, size in seen) <= cap and report["schema_version"] == 1
    assert report["probe_count"] == len(set(seen)) < 35


@pytest.mark.parametrize("limit", ["streaming_sample_limit", "streaming_compare_limit", "streaming_event_limit"])
def test_real_auto_search_fills_resource_limit_and_retains_hold(tmp_path, monkeypatch, limit):
    project = create(tmp_path, count=23, loop=True)
    profile(project, monkeypatch, **{limit: 14 if limit == "streaming_event_limit" else 7})
    result = build_project(project)
    data = manifest(result)
    assert [item["expected"]["samples"] for item in data["patterns"]] == [7, 7, 7, 2]
    decision = read_json(result.bundle_dir / "segmentation.decision.json")
    assert decision["schema_version"] == 2 and decision["planning_strategy"] == "capacity-first"
    assert decision["planning_max_vectors"] is None and decision["initial_probe_vectors"] == 100000
    seg = read_json(result.bundle_dir / data["package_manifest"])
    assert seg["pattern_contract"]["split_ranges"] == [[0, 6], [7, 13], [14, 20], [21, 22]]
    assert all(a["final_hold_state"] == b["entry_hold_state"] for a, b in zip(seg["segments"], seg["segments"][1:]))
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"
    assert len(prepare_run(result.bundle_dir, manifest_sha256=result.manifest_sha256).segments) == 4


def test_legacy_recorded_decision_is_still_inspectable(tmp_path, monkeypatch):
    project = create(tmp_path, count=16)
    profile(project, monkeypatch, streaming_sample_limit=7)
    result = build_project(project)
    root = result.bundle_dir
    settings = read_json(root / "project.json")
    analysis = read_json_artifact(root / "source.analysis.json")
    legacy = segmentation_decision(settings, analysis, legacy=True)
    assert legacy["schema_version"] == 1 and legacy["planning_max_vectors"] == 15
    write_json(root / "segmentation.decision.json", legacy)
    data = manifest(result)
    data["files"]["segmentation.decision.json"] = identity(root / "segmentation.decision.json")
    write_json(root / "runner.bundle.json", data)
    assert verify_bundle(root)["status"] == "PASS"


def test_capacity_first_does_not_turn_unknown_compile_error_into_a_split(tmp_path, monkeypatch):
    project = create(tmp_path)
    profile(project, monkeypatch, streaming_sample_limit=7)
    def broken(*args, **kwargs):
        raise ValueError("public timing or parser rejection, not capacity")
    monkeypatch.setattr(planner, "_compile_stil_binary_probe", broken)
    with pytest.raises(ValueError, match="capacity-first probe rejected.*not capacity"):
        build_project(project)
