"""Sequence-wide profile declaration with strict local projection checks."""

import json
from pathlib import Path

import pytest

from stil.json_artifacts import read_json_artifact
from stil.segmented import verify_segmented_manifest
from stil.segmented_package import build_segment_ranges, build_segmented_packages, SegmentedPackageError
from stil.wait_schedule import propose_wait_schedule, WaitScheduleError

pytestmark = pytest.mark.stpg_host_only
FIXTURE = Path(__file__).parent / "fixtures" / "runner" / "pattern.stil"


def source(tmp_path, layout, *, quantized=False):
    prefix = FIXTURE.read_text(encoding="utf-8").split("Pattern MAIN")[0].replace("400ns", "20000ns")
    if quantized:
        prefix = prefix.replace("100ns", "103ns")
    flat = "V { ALL = 01; }\nV { ALL = 1X; }\n" * 6
    loop = "Loop 6 {\nV { ALL = 01; }\nV { ALL = 1X; }\n}\n"
    body = {"first": loop + flat, "last": flat + loop, "between": flat + loop + flat,
            "flat": flat + flat}[layout]
    path = tmp_path / "public.stil"
    path.write_text(prefix + "Pattern MAIN {\nW NORMAL;\n" + body + "}\n", encoding="utf-8")
    return path, 36 if layout == "between" else 24


def build(tmp_path, layout="last", *, declared="loop-generic", policy="preserve", quantum=4,
          forbidden=(), quantized=False):
    path, total = source(tmp_path, layout, quantized=quantized)
    return build_segmented_packages(input_path=path, output_dir=tmp_path / "segments",
        report_path=tmp_path / "report.json", total_vectors=total,
        ranges=build_segment_ranges(total, segment_vector_count=8), mode="rate-vector",
        loop_policy=policy, allow_approx=quantized, tick_hz=200_000_000,
        chunk_event_capacity=quantum, service_time_us=1,
        service_time_source="public synthetic declared budget, not measured", service_profile_scope=declared,
        max_wait_us=4000, max_total_wait_us=1000000, segment_wait_plan="every-boundary",
        forbidden_stil_rate_units=forbidden)


@pytest.mark.parametrize("layout", ["first", "last", "between"])
@pytest.mark.parametrize("quantum", [4, 2048])
def test_mixed_segments_use_actual_local_scope_without_relaxing_sequence(tmp_path, layout, quantum):
    result = build(tmp_path, layout, quantum=quantum)
    manifest = tmp_path / "segments" / "nominal" / "segmented.manifest.json"
    assert verify_segmented_manifest(manifest)["status"] == "PASS"
    segments = result["variants"][0]["segments"]
    scopes = {segment["service_profile"]["segment_scope"] for segment in segments}
    assert scopes == {"non-loop", "loop-generic"}
    for segment in segments:
        scope = segment["service_profile"]["segment_scope"]
        package = manifest.parent / Path(segment["path"]).parent
        acceptance = read_json_artifact(package / "pattern.wait.acceptance.json")
        assert acceptance["status"] == "ACCEPTED"
        assert acceptance["service_profile_scope"] == scope
        assert acceptance["segmented_service_profile"] == {
            "declared_sequence_scope": "loop-generic", "segment_scope": scope, "segment_id": segment["segment_id"]}
        if quantum == 4:
            assert any(boundary["wait_ticks"] for boundary in read_json_artifact(package / "pattern.wait.json")["boundaries"])
        else:
            assert not any(boundary["wait_ticks"] for boundary in read_json_artifact(package / "pattern.wait.json")["boundaries"])


@pytest.mark.parametrize("layout,declared,policy", [
    ("first", "non-loop", "preserve"), ("last", "non-loop", "preserve"),
    ("flat", "loop-generic", "preserve"), ("last", "loop-generic", "expand"),
])
def test_wrong_sequence_scope_still_rejects(tmp_path, layout, declared, policy):
    with pytest.raises(SegmentedPackageError, match="whole segmented sequence scope"):
        build(tmp_path, layout, declared=declared, policy=policy)


def test_single_package_scope_rule_is_still_strict(tmp_path):
    build(tmp_path)
    package = tmp_path / "segments" / "nominal" / "segment-0000" / "package"
    with pytest.raises(WaitScheduleError, match="artifact scope"):
        propose_wait_schedule(package / "pattern.bin", map_path=package / "pattern.map.json",
            service_time_us=1, service_time_source="public declared budget", service_profile_scope="loop-generic",
            max_wait_us=4000, max_total_wait_us=1000000)


def test_approx_and_mixed_loop_waits_together(tmp_path):
    build(tmp_path, quantized=True)
    assert verify_segmented_manifest(tmp_path / "segments/nominal/segmented.manifest.json")["status"] == "PASS"


def test_scope_binding_tampering_not_just_passed_through(tmp_path):
    build(tmp_path)
    path = tmp_path / "segments/nominal/segmented.manifest.json"
    value = json.loads(path.read_text())
    value["segments"][0]["service_profile"]["segment_scope"] = "loop-generic"
    path.write_text(json.dumps(value))
    verification = verify_segmented_manifest(path)
    assert verification["status"] == "REJECT"
    assert any("scope" in error for error in verification["errors"])


def test_forbidden_rate_not_enabled_by_sequence_scope_fix(tmp_path):
    build(tmp_path, forbidden=("NORMAL",))
    path = tmp_path / "segments/nominal/segment.wait.plan.json"
    assert not any(boundary["wait_ticks"] for boundary in read_json_artifact(path)["boundaries"])
