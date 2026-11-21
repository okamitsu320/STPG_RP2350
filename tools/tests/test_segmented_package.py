from __future__ import annotations

import json
from pathlib import Path

import stil.segmented_package as segmented_package_module
import pytest

from stil.json_artifacts import read_json_artifact
from stil.cli_segment_package import main
from stil.pattern_package import (
    PatternPackageError,
    PatternPackageOptions,
    build_pattern_package,
    verify_pattern_package,
)
from stil.segmented_package import (
    SegmentedPackageError,
    build_segmented_packages,
    build_segment_ranges,
    derive_segment_wait_schedule,
)
from stil.wait_schedule import WaitScheduleError, build_wait_schedule, set_wait


SOURCE = """\
Signals {
  D0 InOut;
  D1 InOut;
}
SignalGroups {
  ALL = 'D0 + D1';
}
Timing {
  WaveformTable WFT1 {
    Period '40ns';
    Waveforms {
      ALL { 0: 0ns Drive(0); 1: 0ns Drive(1); X: 10ns Compare(X); Z: 0ns DirZ; }
    }
  }
}
Pattern MAIN {
  W WFT1;
  V { ALL = 01; }
  V { ALL = 10; }
  V { ALL = 11; }
  V { ALL = 00; }
  V { ALL = 01; }
  V { ALL = 10; }
}
"""


def test_build_segment_ranges_supports_fixed_count_and_explicit_boundaries() -> None:
    assert build_segment_ranges(10, segment_vector_count=4) == ((0, 3), (4, 7), (8, 9))
    assert build_segment_ranges(10, split_at=(1, 4)) == ((0, 1), (2, 4), (5, 9))
    with pytest.raises(SegmentedPackageError):
        build_segment_ranges(10, segment_vector_count=4, split_at=(3,))
    with pytest.raises(SegmentedPackageError):
        build_segment_ranges(10, split_at=(4, 2))


def test_cli_builds_contiguous_segments_with_global_maps_and_hold_chain(tmp_path: Path) -> None:
    source = tmp_path / "source.stil"
    source.write_text(SOURCE, encoding="utf-8")
    output_dir = tmp_path / "segments"
    report_path = tmp_path / "segments.json"

    assert main(
        [
            str(source),
            "--segment-vector-count",
            "2",
            "--output-dir",
            str(output_dir),
            "--json-out",
            str(report_path),
            "--mode",
            "rate-vector",
            "--allow-approx",
        ]
    ) == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "COMPLETE"
    assert report["policies"]["variant_then_segment_order"] is True
    variant = report["variants"][0]
    assert variant["name"] == "nominal"
    manifest_path = output_dir / "nominal" / "segmented.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["pattern_contract"]["split_ranges"] == [[0, 1], [2, 3], [4, 5]]
    assert manifest["pattern_contract"]["source_vector_count"] == 6
    assert len(manifest["segments"]) == 3
    assert all(segment["pattern"]["rate_count"] == 1 for segment in manifest["segments"])
    assert all(segment["pattern"]["vector_count"] == 2 for segment in manifest["segments"])
    assert [
        (segment["global_event_start"], segment["global_event_end"])
        for segment in manifest["segments"]
    ] == [(0, 1), (2, 3), (4, 5)]
    assert manifest["pattern_contract"]["global_event_end"] == 5
    assert manifest["segments"][0]["entry_hold_state"]["hash"] != ""
    assert manifest["segments"][0]["final_hold_state"] == manifest["segments"][1]["entry_hold_state"]
    assert manifest["segments"][1]["final_hold_state"] == manifest["segments"][2]["entry_hold_state"]
    assert all(segment["physical_execution_authorized"] is False for segment in manifest["segments"])

    for segment in manifest["segments"]:
        package_dir = manifest_path.parent / Path(segment["path"]).parent
        package_manifest = json.loads(
            (package_dir / "manifest.json").read_text(encoding="utf-8")
        )
        assert package_manifest["physical_execution_authorized"] is False
        package_map = read_json_artifact(package_dir / "pattern.map.json")
        assert package_map["vectors"]
        wait_schedule = read_json_artifact(package_dir / "pattern.wait.json")
        assert wait_schedule["kind"] == "stpg_wait_schedule"


def test_cli_segment_package_routes_line_marker_to_its_global_segment(tmp_path: Path) -> None:
    source = tmp_path / "source.stil"
    source.write_text(SOURCE, encoding="utf-8")
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 16,
                "signals": {
                    "D0": {"bit": 0, "role": "signal"},
                    "D1": {"bit": 1, "role": "signal"},
                    "__REF0": {"bit": 2, "role": "marker"},
                },
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "segments"
    report_path = tmp_path / "segments.json"
    assert main(
        [
            str(source),
            "--segment-vector-count",
            "2",
            "--output-dir",
            str(output_dir),
            "--json-out",
            str(report_path),
            "--mode",
            "rate-vector",
            "--pin-map",
            str(pin_map),
            "--allow-approx",
            "--marker",
            "__REF0:line=20:mode=pulse",
        ]
    ) == 0

    manifest_path = output_dir / "nominal" / "segmented.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    maps = [
        read_json_artifact(manifest_path.parent / Path(segment["map_path"]))
        for segment in manifest["segments"]
    ]
    assert maps[0]["markers"] == []
    assert maps[1]["markers"][0]["address_kind"] == "stil_line"
    assert maps[1]["markers"][0]["line"] == 20
    assert maps[1]["markers"][0]["vector"] == 2
    assert maps[1]["vectors"]["0"]["marker_events"][0]["global_vector_index"] == 2
    assert maps[2]["markers"] == []


def test_cli_segment_package_keeps_line_marker_clear_at_segment_start(tmp_path: Path) -> None:
    source = tmp_path / "source.stil"
    source.write_text(SOURCE, encoding="utf-8")
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 16,
                "signals": {
                    "D0": {"bit": 0, "role": "signal"},
                    "D1": {"bit": 1, "role": "signal"},
                    "__REF0": {"bit": 2, "role": "marker"},
                },
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "segments"
    report_path = tmp_path / "segments.json"
    assert main(
        [
            str(source),
            "--segment-vector-count",
            "2",
            "--output-dir",
            str(output_dir),
            "--json-out",
            str(report_path),
            "--mode",
            "rate-vector",
            "--pin-map",
            str(pin_map),
            "--allow-approx",
            "--marker",
            "__REF0:line=18:mode=level:set",
            "--marker-clear",
            "__REF0:line=20",
        ]
    ) == 0

    manifest_path = output_dir / "nominal" / "segmented.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    maps = [
        read_json_artifact(manifest_path.parent / Path(segment["map_path"]))
        for segment in manifest["segments"]
    ]
    assert maps[0]["markers"][0]["clear_line"] == 20
    assert maps[0]["markers"][0]["clear_vector"] == 2
    assert maps[1]["markers"][0]["clear_vector"] == 2
    assert maps[1]["vectors"]["0"]["marker_events"][0]["mode"] == "level:clear"
    assert maps[2]["markers"] == []


def test_stil_rate_unit_exclusion_is_preserved_and_blocks_manual_wait(tmp_path: Path) -> None:
    source = tmp_path / "source.stil"
    source.write_text(SOURCE, encoding="utf-8")
    package_dir = tmp_path / "package"
    report_path = tmp_path / "package.json"
    assert main(
        [
            str(source),
            "--segment-vector-count",
            "2",
            "--output-dir",
            str(package_dir),
            "--json-out",
            str(report_path),
            "--mode",
            "rate-vector",
            "--tick-hz",
            "150000000",
            "--allow-approx",
            "--segment-wait-plan",
            "auto",
            "--chunk-event-capacity",
            "1024",
            "--ring-slot-count",
            "6",
            "--service-us",
            "2000",
            "--service-source",
            "bounded test profile",
            "--max-wait-us",
            "4000",
            "--max-total-wait-us",
            "5000000",
            "--forbid-stil-rate",
            "WFT1",
        ]
    ) == 0
    manifest = json.loads(
        (package_dir / "nominal" / "segmented.manifest.json").read_text(encoding="utf-8")
    )
    plan = json.loads(
        (package_dir / "nominal" / "segment.wait.plan.json").read_text(encoding="utf-8")
    )
    assert plan["policy"]["forbidden_stil_rate_units"] == ["WFT1"]
    assert plan["policy"]["forbidden_stil_rate_match"] == "next_chunk_first_event"
    assert all(
        boundary.get("boundary_rate_unit") != "WFT1"
        for boundary in plan["boundaries"]
        if int(boundary.get("wait_ticks", 0)) > 0
    )
    assert manifest["policies"]["physical_execution_authorized"] is False

    source_package = package_dir / "nominal" / "segment-0000" / "package"
    schedule = build_wait_schedule(
        source_package / "pattern.bin",
        map_path=source_package / "pattern.map.json",
        chunk_event_capacity=1,
        forbidden_stil_rate_units=("WFT1",),
    )
    schedule_path = tmp_path / "forbidden.schedule.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
    with pytest.raises(WaitScheduleError, match="starts with forbidden STIL rate"):
        set_wait(
            source_package / "pattern.bin",
            schedule_path,
            map_path=source_package / "pattern.map.json",
            before_chunk_index=1,
            wait_ticks=1,
            reason="test forbidden source rate",
            output_path=tmp_path / "edited.schedule.json",
        )


def test_segmented_forbidden_rate_uses_next_chunks_first_event(monkeypatch) -> None:
    chunks = [
        {
            "chunk_index": index,
            "event_start": index * 10,
            "event_end_exclusive": (index + 1) * 10,
            "event_count": 10,
            "logical_vector_start": index * 5,
            "logical_vector_end_exclusive": (index + 1) * 5,
            "rate_ids": [0],
            "consume_ticks": 100,
            "consume_us": 100.0,
            "contains_rate_transition": False,
            "rate_units": rate_units,
            "boundary_rate_unit": boundary_rate_unit,
        }
        for index, rate_units, boundary_rate_unit in [
            (0, ["WFT_ALLOWED"], "WFT_ALLOWED"),
            (1, ["WFT_ALLOWED", "WFT_FORBIDDEN"], "WFT_ALLOWED"),
            (2, ["WFT_FORBIDDEN", "WFT_ALLOWED"], "WFT_FORBIDDEN"),
            (3, ["WFT_ALLOWED"], "WFT_ALLOWED"),
        ]
    ]
    model = {
        "model": {
            "tick_hz": 1_000_000,
            "chunk_event_capacity": 10,
            "ring_slot_count": 3,
            "steady_state_lead_slots": 2,
            "steady_state_lead_chunks": 2,
            "service_time_assumption": "test only",
        },
        "pattern": {
            "version": 1,
            "rate_count": 1,
            "slot_count": 1,
            "expanded_event_count": 40,
            "total_consume_ticks": 400,
            "total_consume_us": 400.0,
            "chunk_count": len(chunks),
            "chunks": chunks,
        },
    }
    captured: dict[str, object] = {}

    def fake_plan_waits(_model, **kwargs):
        captured.update(kwargs)
        return {
            "status": "planned",
            "reason": "test plan",
            "schedule": {
                "waits": [
                    {"before_chunk_index": 1, "wait_ticks": 10},
                ]
            },
        }

    monkeypatch.setattr(segmented_package_module, "plan_waits", fake_plan_waits)
    schedule, acceptance = segmented_package_module._build_segmented_wait_schedule(
        model,
        source_identity={"sha256": "test"},
        segment_chunk_ranges=[
            {"segment_id": 0, "first_chunk_index": 0},
            {"segment_id": 1, "first_chunk_index": 3},
        ],
        max_rates=None,
        service_time_us=1.0,
        service_time_source="test only",
        safety_margin_us=0.0,
        max_wait_us=10.0,
        max_total_wait_us=100.0,
        max_wait_count=None,
        pattern_time_limit_us=None,
        wait_policy="every-boundary",
        forbidden_stil_rate_units=("WFT_FORBIDDEN",),
    )

    assert captured["forbidden_wait_chunks"] == {2, 3}
    assert schedule["boundaries"][1]["wait_ticks"] == 10
    assert schedule["boundaries"][1]["rate_units"] == [
        "WFT_ALLOWED",
        "WFT_FORBIDDEN",
    ]
    assert schedule["policy"]["forbidden_stil_rate_match"] == "next_chunk_first_event"
    assert acceptance["status"] == "ACCEPTED"


def test_cli_every_boundary_waits_each_eligible_chunk_once(tmp_path: Path) -> None:
    source = tmp_path / "source.stil"
    source.write_text(SOURCE, encoding="utf-8")
    output_dir = tmp_path / "every-boundary"
    report_path = tmp_path / "every-boundary.json"

    assert main(
        [
            str(source),
            "--segment-vector-count",
            "3",
            "--output-dir",
            str(output_dir),
            "--json-out",
            str(report_path),
            "--mode",
            "rate-vector",
            "--tick-hz",
            "150000000",
            "--allow-approx",
            "--chunk-event-capacity",
            "2",
            "--segment-wait-plan",
            "every-boundary",
            "--service-us",
            "2000",
            "--service-source",
            "bounded test profile",
            "--max-wait-us",
            "4000",
            "--max-total-wait-us",
            "5000000",
        ]
    ) == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "COMPLETE"
    assert report["policies"]["segment_wait_plan"] == "every-boundary"
    variant_dir = output_dir / "nominal"
    plan = json.loads((variant_dir / "segment.wait.plan.json").read_text(encoding="utf-8"))
    assert plan["policy"]["wait_planning_policy"] == "every-boundary"
    forbidden = set(plan["policy"]["forbidden_wait_chunks"])
    wait_indices = {
        int(boundary["before_chunk_index"])
        for boundary in plan["boundaries"]
        if int(boundary["wait_ticks"]) > 0
    }
    eligible = {
        int(boundary["before_chunk_index"])
        for boundary in plan["boundaries"]
        if int(boundary["before_chunk_index"]) > 0
        and int(boundary["before_chunk_index"]) not in forbidden
    }
    assert wait_indices == eligible
    assert all(
        int(boundary["wait_ticks"]) == 600_000
        for boundary in plan["boundaries"]
        if int(boundary["before_chunk_index"]) in eligible
    )

    manifest = json.loads((variant_dir / "segmented.manifest.json").read_text(encoding="utf-8"))
    for segment in manifest["segments"]:
        package_dir = variant_dir / Path(segment["path"]).parent
        assert verify_pattern_package(package_dir)["status"] == "PASS"
        projected = read_json_artifact(package_dir / "pattern.wait.json")
        projected_boundaries = projected["boundaries"]
        assert projected_boundaries[0]["wait_ticks"] == 0
        assert all(
            int(boundary["wait_ticks"]) == 600_000
            for boundary in projected_boundaries[1:]
        )


def test_cli_applies_rate_variant_before_each_segment(tmp_path: Path) -> None:
    source = tmp_path / "source.stil"
    source.write_text(SOURCE, encoding="utf-8")
    output_dir = tmp_path / "matrix"
    report_path = tmp_path / "matrix.json"

    assert main(
        [
            str(source),
            "--segment-vector-count",
            "3",
            "--variant",
            "slow=WFT1:80ns",
            "--output-dir",
            str(output_dir),
            "--json-out",
            str(report_path),
            "--mode",
            "rate-vector",
            "--allow-approx",
        ]
    ) == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert [variant["name"] for variant in report["variants"]] == ["slow"]
    variant_dir = output_dir / "slow"
    timing = json.loads((variant_dir / "rate-variant.json").read_text(encoding="utf-8"))
    assert timing["waveform_tables"][0]["target_period"] == "80ns"
    assert len(report["variants"][0]["segments"]) == 2
    for segment in report["variants"][0]["segments"]:
        assert segment["wait_schedule"]["recalculated_from_segment_binary"] is True


def _schedule(*waits: tuple[int, int]) -> dict[str, object]:
    values = {event_start: wait_ticks for event_start, wait_ticks in waits}
    return {
        "kind": "stpg_wait_schedule",
        "model": {"tick_hz": 150_000_000},
        "boundaries": [
            {
                "before_chunk_index": index,
                "event_start": event_start,
                "event_count": 2,
                "event_end_exclusive": event_start + 2,
                "wait_ticks": values.get(event_start, 0),
                "wait_us": values.get(event_start, 0) / 150,
            }
            for index, event_start in enumerate((0, 2, 4))
        ],
    }


def test_derive_segment_wait_schedule_projects_by_event_boundary(tmp_path: Path) -> None:
    global_path = tmp_path / "global.wait.json"
    base_path = tmp_path / "segment.wait.json"
    global_path.write_text(json.dumps(_schedule((2, 750_000))), encoding="utf-8")
    base_path.write_text(json.dumps(_schedule()), encoding="utf-8")

    projected = derive_segment_wait_schedule(
        global_path,
        base_path,
        global_event_start=0,
        global_event_end=5,
        segment_id=1,
    )

    assert [item["wait_ticks"] for item in projected["boundaries"]] == [0, 750_000, 0]
    assert projected["provenance"]["global_event_range"] == [0, 5]
    assert projected["provenance"]["selected_global_boundary_indices"] == [1]


def test_derive_segment_wait_schedule_rejects_wait_at_upload_boundary(tmp_path: Path) -> None:
    global_path = tmp_path / "global.wait.json"
    base_path = tmp_path / "segment.wait.json"
    global_path.write_text(json.dumps(_schedule((2, 750_000))), encoding="utf-8")
    base_path.write_text(json.dumps(_schedule()), encoding="utf-8")

    with pytest.raises(SegmentedPackageError, match="loses a non-zero first-boundary wait"):
        derive_segment_wait_schedule(
            global_path,
            base_path,
            global_event_start=2,
            global_event_end=5,
            segment_id=1,
        )


def test_cli_projects_and_encodes_global_waits_per_segment(tmp_path: Path) -> None:
    source = tmp_path / "source.stil"
    extra_vectors = """\
  V { ALL = 11; }
  V { ALL = 00; }
  V { ALL = 01; }
  V { ALL = 10; }
  V { ALL = 11; }
  V { ALL = 00; }
"""
    source.write_text(SOURCE.rsplit("}\n", 1)[0] + extra_vectors + "}\n", encoding="utf-8")

    full_dir = tmp_path / "full"
    build_pattern_package(
        PatternPackageOptions(
            input_path=source,
            output_dir=full_dir,
            mode="rate-vector",
            allow_approx=True,
            chunk_event_capacity=2,
            service_time_us=2_000.0,
            service_time_source="test-model",
        )
    )
    global_schedule_path = tmp_path / "global.wait.json"
    global_schedule = read_json_artifact(full_dir / "pattern.wait.json")
    for boundary in global_schedule["boundaries"]:
        if boundary["event_start"] in (2, 6, 10):
            boundary["wait_ticks"] = 750_000
            boundary["wait_us"] = 5_000.0
    global_schedule_path.write_text(json.dumps(global_schedule), encoding="utf-8")

    output_dir = tmp_path / "segments"
    report_path = tmp_path / "segments.json"
    assert main(
        [
            str(source),
            "--segment-vector-count",
            "4",
            "--output-dir",
            str(output_dir),
            "--json-out",
            str(report_path),
            "--mode",
            "rate-vector",
            "--tick-hz",
            "150000000",
            "--allow-approx",
            "--chunk-event-capacity",
            "2",
            "--service-us",
            "2000",
            "--service-source",
            "test-model",
            "--global-wait-schedule",
            str(global_schedule_path),
        ]
    ) == 0

    manifest = json.loads(
        (output_dir / "nominal" / "segmented.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["policies"]["global_wait_schedule_projected"] is True
    for segment in manifest["segments"]:
        package_dir = output_dir / "nominal" / Path(segment["path"]).parent
        schedule = read_json_artifact(package_dir / "pattern.wait.json")
        assert schedule["status"] == "APPLIED"
        assert [item["wait_ticks"] for item in schedule["boundaries"]] == [0, 750_000]


def test_cli_plans_waits_once_across_separate_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.stil"
    source.write_text(SOURCE, encoding="utf-8")
    output_dir = tmp_path / "segments-auto-wait"
    report_path = tmp_path / "segments-auto-wait.json"
    planner_calls: list[dict[str, object]] = []
    planner_constraints: list[object] = []

    def deterministic_plan(model: dict[str, object], **kwargs: object) -> dict[str, object]:
        planner_calls.append(model)
        planner_constraints.append(kwargs["forbidden_wait_chunks"])
        return {
            "status": "planned",
            "schedule": {
                "waits": [
                    {
                        "before_chunk_index": 1,
                        "before_event": 2,
                        "wait_ticks": 300_000,
                        "wait_us": 2_000.0,
                    },
                    {
                        "before_chunk_index": 3,
                        "before_event": 6,
                        "wait_ticks": 300_000,
                        "wait_us": 2_000.0,
                    },
                ]
            },
            "model_chunk_count": model["pattern"]["chunk_count"],
        }

    monkeypatch.setattr(segmented_package_module, "plan_waits", deterministic_plan)

    assert main(
        [
            str(source),
            "--segment-vector-count",
            "3",
            "--output-dir",
            str(output_dir),
            "--json-out",
            str(report_path),
            "--mode",
            "rate-vector",
            "--tick-hz",
            "150000000",
            "--allow-approx",
            "--chunk-event-capacity",
            "2",
            "--ring-slot-count",
            "2",
            "--segment-wait-plan",
            "auto",
            "--service-us",
            "2000",
            "--service-source",
            "test-model",
            "--max-wait-us",
            "4000",
            "--max-total-wait-us",
            "5000000",
        ]
    ) == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "COMPLETE"
    assert report["policies"]["segment_wait_plan"] == "auto"
    assert len(planner_calls) == 1
    assert planner_constraints == [{2}]
    variant_dir = output_dir / "nominal"
    plan = json.loads((variant_dir / "segment.wait.plan.json").read_text(encoding="utf-8"))
    acceptance = json.loads(
        (variant_dir / "segment.wait.plan.acceptance.json").read_text(encoding="utf-8")
    )
    assert plan["scope"] == "logical_segmented_pattern"
    assert acceptance["status"] == "ACCEPTED"
    assert any(item["wait_ticks"] > 0 for item in plan["boundaries"])

    manifest = json.loads((variant_dir / "segmented.manifest.json").read_text(encoding="utf-8"))
    assert manifest["policies"]["segment_wait_plan_auto"] is True
    assert manifest["segment_wait_plan"]["sha256"] == report["variants"][0]["segment_wait_plan"]["sha256"]
    assert not (variant_dir / "pattern.bin").exists()
    for segment in manifest["segments"]:
        package_dir = variant_dir / Path(segment["path"]).parent
        assert verify_pattern_package(package_dir)["status"] == "PASS"
        assert segment["wait_schedule"]["derived_from_segment_wait_plan"] is not None


def test_cli_defensively_rejects_plan_with_segment_entry_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.stil"
    source.write_text(SOURCE, encoding="utf-8")

    def invalid_plan(_model: dict[str, object], **_kwargs: object) -> dict[str, object]:
        return {
            "status": "planned",
            "schedule": {
                "waits": [
                    {
                        "before_chunk_index": 2,
                        "before_event": 4,
                        "wait_ticks": 1,
                        "wait_us": 1.0,
                    }
                ]
            },
        }

    monkeypatch.setattr(segmented_package_module, "plan_waits", invalid_plan)
    with pytest.raises(SegmentedPackageError, match="forbidden.*segment upload boundary"):
        build_segmented_packages(
            input_path=source,
            output_dir=tmp_path / "segments-auto-wait-reject",
            report_path=tmp_path / "segments-auto-wait-reject.json",
            ranges=build_segment_ranges(6, segment_vector_count=2),
            total_vectors=6,
            mode="rate-vector",
            allow_approx=True,
            chunk_event_capacity=2,
            ring_slot_count=2,
            segment_wait_plan="auto",
            service_time_us=2000,
            service_time_source="test-model",
            max_wait_us=4000,
            max_total_wait_us=5000000,
        )


def _large_compare_source(vector_count: int) -> str:
    vectors = "\n".join(
        f"  V {{ ALL = {'0L' if index % 2 == 0 else '1H'}; }}"
        for index in range(vector_count)
    )
    return f"""\
Signals {{
  D0 InOut;
  D1 InOut;
}}
SignalGroups {{
  ALL = 'D0 + D1';
}}
Timing {{
  WaveformTable WFT1 {{
    Period '200ns';
    Waveforms {{
      D0 {{ 0: 0ns Drive(0); 1: 0ns Drive(1); }}
      D1 {{ H: 0ns Compare(1); L: 0ns Compare(0); }}
    }}
  }}
}}
Pattern MAIN {{
  W WFT1;
{vectors}
}}
"""


@pytest.mark.slow
def test_six_megabyte_class_compare_pattern_requires_segmentation(tmp_path: Path) -> None:
    """A >5M2M pattern rejects as one package but fits as bounded segments."""

    vector_count = 270_000
    source = tmp_path / "large.stil"
    source.write_text(_large_compare_source(vector_count), encoding="utf-8")
    profile = "psram-compare-sample-streaming-practical-5m2m"

    with pytest.raises(PatternPackageError, match="source analysis is REJECT"):
        build_pattern_package(
            PatternPackageOptions(
                input_path=source,
                output_dir=tmp_path / "unsplit",
                mode="rate-vector",
                tick_hz=150_000_000,
                allow_approx=True,
                runtime_profile=profile,
            )
        )
    unsplit_analysis = read_json_artifact(tmp_path / "unsplit" / "source.analysis.json")
    assert "ERR_PSRAM_PATTERN_BYTES_LIMIT" in unsplit_analysis["decision"]["blocking_codes"]
    assert unsplit_analysis["segmentation"]["available"] is True
    assert unsplit_analysis["segmentation"]["status"] == "RECOMMENDED"

    output_dir = tmp_path / "segmented"
    report_path = tmp_path / "segmented.json"
    report = build_segmented_packages(
        input_path=source,
        output_dir=output_dir,
        report_path=report_path,
        ranges=build_segment_ranges(vector_count, split_at=(134_999,)),
        total_vectors=vector_count,
        mode="rate-vector",
        tick_hz=150_000_000,
        allow_approx=True,
        runtime_profile=profile,
    )

    assert report["status"] == "COMPLETE"
    manifest = json.loads(
        (output_dir / "nominal" / "segmented.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["pattern_contract"]["global_event_end"] == vector_count - 1
    assert len(manifest["segments"]) == 2

    total_samples = 0
    for segment in manifest["segments"]:
        package_dir = output_dir / "nominal" / Path(segment["path"]).parent
        assert verify_pattern_package(package_dir)["status"] == "PASS"
        assert segment["pattern"]["size_bytes"] <= 0x4F0000
        metadata = read_json_artifact(package_dir / "pattern.metadata.json")
        observation = metadata["pattern"]["runtime_capacity_observation"]
        assert observation["status"] in {"PASS", "REVIEW"}
        sample_metric = observation["metrics"]["sample_count"]
        assert sample_metric["value"] <= 0x200000 // 4
        total_samples += int(sample_metric["value"])
    assert total_samples == vector_count
