from __future__ import annotations

import json
from pathlib import Path

import pytest

from stpg.constants import STPG_DRIVE_VECTOR, STPG_RATE_SLOT_DIR_UPDATE
from stpg.pattern import RateDesc, RateSlot, VectorRecord, pack_rate_vector, unpack_rate_vector_full
from stil.cli_wait_schedule import main
from stil.runtime_wait_model import plan_waits, simulate_explicit_waits
from stil.wait_acceptance import evaluate_wait_schedule
from stil.wait_binary import apply_wait_schedule_files, inspect_wait_schedule_encoding
from stil.wait_schedule import (
    WaitScheduleError,
    build_wait_schedule,
    propose_wait_schedule,
    set_wait,
    set_wait_periodic,
    set_wait_range,
    validate_wait_schedule,
)


def _pattern(path: Path, *, count: int = 8, tick_hz: int = 150_000_000) -> None:
    path.write_bytes(
        pack_rate_vector(
            rates=[RateDesc(period_ticks=30, slot_count=2, flags=0, slot_offset=0)],
            slots=[
                RateSlot(time_ticks=0, flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=0xFFFF, dir_mask=0xFFFF, sample_mask=0),
                RateSlot(time_ticks=15, flags=STPG_DRIVE_VECTOR, drive_mask=0xFFFF, dir_mask=0, sample_mask=0),
            ],
            vectors=[VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=1, drive_enable=3, expect_value=0, expect_mask=0)] * count,
            tick_hz=tick_hz,
            active_pins=16,
        )
    )


def _map_for_repeated_pattern(
    count: int, *, waveform_tables: list[str] | None = None
) -> dict:
    waveform_tables = waveform_tables or ["WFT0"] * count
    assert len(waveform_tables) == count
    return {
        "mode_emitted": "rate-vector",
        "vectors": {
            str(index): {
                "top_level_kind": "vector",
                "repeat": 1,
                "involved_bits": [0],
                "marker_events": [],
                "source_map": [{
                    "local_vector_index": index,
                    "global_vector_index": index,
                    "repeat_iteration": 0,
                    "stil_line": 10 + index,
                    "label": None,
                    "pattern_name": "TEST",
                    "waveform_table": waveform_tables[index],
                    "signals": ["D0"],
                }],
            }
            for index in range(count)
        },
    }


def test_create_contains_exact_geometry_and_identity(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=8)
    schedule = build_wait_schedule(pattern, chunk_event_capacity=4, ring_slot_count=6)

    assert schedule["kind"] == "stpg_wait_schedule"
    assert schedule["policy"]["automatic_insertion"] is False
    assert len(schedule["boundaries"]) == 4
    assert [item["event_start"] for item in schedule["boundaries"]] == [0, 4, 8, 12]
    assert schedule["identity"]["pattern"]["size_bytes"] == pattern.stat().st_size
    assert all(item["wait_ticks"] == 0 for item in schedule["boundaries"])


def test_map_identity_is_pinned_and_checked(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    map_path = tmp_path / "pattern.map.json"
    _pattern(pattern, count=4)
    map_path.write_text(json.dumps({"vectors": {}}), encoding="utf-8")
    schedule = build_wait_schedule(pattern, map_path=map_path, chunk_event_capacity=4)
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")

    assert validate_wait_schedule(pattern, schedule_path, map_path=map_path)["status"] == "PASS"
    map_path.write_text(json.dumps({"vectors": {"changed": True}}), encoding="utf-8")
    report = validate_wait_schedule(pattern, schedule_path, map_path=map_path)
    assert report["status"] == "REJECT"
    assert any("map size_bytes mismatch" in error for error in report["errors"])


def test_forbidden_stil_rate_matches_only_the_next_chunks_first_event(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    map_path = tmp_path / "pattern.map.json"
    _pattern(pattern, count=8)
    map_path.write_text(
        json.dumps(
            _map_for_repeated_pattern(
                8,
                waveform_tables=[
                    "WFT_ALLOWED",
                    "WFT_ALLOWED",
                    "WFT_ALLOWED",
                    "WFT_FORBIDDEN",
                    "WFT_FORBIDDEN",
                    "WFT_ALLOWED",
                    "WFT_ALLOWED",
                    "WFT_ALLOWED",
                ],
            )
        ),
        encoding="utf-8",
    )
    schedule = build_wait_schedule(
        pattern,
        map_path=map_path,
        chunk_event_capacity=4,
        forbidden_stil_rate_units=("WFT_FORBIDDEN",),
    )
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")

    assert schedule["policy"]["forbidden_stil_rate_match"] == "next_chunk_first_event"
    # Chunk 1 starts in WFT_ALLOWED, even though its later vectors use the
    # forbidden table.  The wait immediately before it is therefore legal.
    assert schedule["boundaries"][1]["boundary_rate_unit"] == "WFT_ALLOWED"
    assert schedule["boundaries"][1]["rate_units"] == [
        "WFT_ALLOWED",
        "WFT_FORBIDDEN",
    ]
    updated = set_wait(
        pattern,
        schedule_path,
        map_path=map_path,
        before_chunk_index=1,
        wait_ticks=1,
        reason="allowed entry rate despite a later forbidden vector",
        output_path=tmp_path / "edited.wait.json",
    )
    assert updated["boundaries"][1]["wait_ticks"] == 1

    # Chunk 2 starts with the forbidden table, so this boundary is blocked.
    assert schedule["boundaries"][2]["boundary_rate_unit"] == "WFT_FORBIDDEN"
    with pytest.raises(WaitScheduleError, match="starts with forbidden STIL rate"):
        set_wait(
            pattern,
            schedule_path,
            map_path=map_path,
            before_chunk_index=2,
            wait_ticks=1,
            reason="must not precede a forbidden entry rate",
            output_path=tmp_path / "blocked.wait.json",
        )


def test_wait_schedule_validates_boundary_rate_against_pinned_map(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    map_path = tmp_path / "pattern.map.json"
    _pattern(pattern, count=8)
    map_path.write_text(
        json.dumps(_map_for_repeated_pattern(8)),
        encoding="utf-8",
    )
    schedule = build_wait_schedule(pattern, map_path=map_path, chunk_event_capacity=4)
    schedule["boundaries"][1]["boundary_rate_unit"] = "TAMPERED"
    schedule_path = tmp_path / "tampered.wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")

    report = validate_wait_schedule(pattern, schedule_path, map_path=map_path)

    assert report["status"] == "REJECT"
    assert any("boundary_rate_unit" in error for error in report["errors"])


def test_wait_schedule_rejects_unknown_forbidden_rate_policy(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    map_path = tmp_path / "pattern.map.json"
    _pattern(pattern, count=4)
    map_path.write_text(
        json.dumps(_map_for_repeated_pattern(4)),
        encoding="utf-8",
    )
    schedule = build_wait_schedule(pattern, map_path=map_path, chunk_event_capacity=4)
    schedule["policy"]["forbidden_stil_rate_units"] = ["NOT_IN_PATTERN"]
    schedule_path = tmp_path / "tampered-policy.wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")

    report = validate_wait_schedule(pattern, schedule_path, map_path=map_path)

    assert report["status"] == "REJECT"
    assert any("are not used by the pattern" in error for error in report["errors"])


def test_forbidden_stil_rate_fails_closed_when_chunk_entry_mapping_is_missing(
    tmp_path: Path,
) -> None:
    pattern = tmp_path / "pattern.bin"
    map_path = tmp_path / "pattern.map.json"
    _pattern(pattern, count=8)
    map_data = _map_for_repeated_pattern(
        8,
        waveform_tables=[
            "WFT_ALLOWED",
            "WFT_ALLOWED",
            "WFT_ALLOWED",
            "WFT_FORBIDDEN",
            "WFT_FORBIDDEN",
            "WFT_ALLOWED",
            "WFT_ALLOWED",
            "WFT_ALLOWED",
        ],
    )
    del map_data["vectors"]["2"]
    map_path.write_text(json.dumps(map_data), encoding="utf-8")
    schedule = build_wait_schedule(
        pattern,
        map_path=map_path,
        chunk_event_capacity=4,
        forbidden_stil_rate_units=("WFT_FORBIDDEN",),
    )
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")

    with pytest.raises(WaitScheduleError, match="no source STIL rate-unit mapping"):
        set_wait(
            pattern,
            schedule_path,
            map_path=map_path,
            before_chunk_index=1,
            wait_ticks=1,
            reason="unknown entry rate must not be assumed safe",
            output_path=tmp_path / "edited.wait.json",
        )


def test_legacy_wait_schedule_keeps_whole_chunk_forbidden_rate_semantics(
    tmp_path: Path,
) -> None:
    pattern = tmp_path / "pattern.bin"
    map_path = tmp_path / "pattern.map.json"
    _pattern(pattern, count=8)
    map_path.write_text(
        json.dumps(
            _map_for_repeated_pattern(
                8,
                waveform_tables=[
                    "WFT_ALLOWED",
                    "WFT_ALLOWED",
                    "WFT_ALLOWED",
                    "WFT_FORBIDDEN",
                    "WFT_FORBIDDEN",
                    "WFT_ALLOWED",
                    "WFT_ALLOWED",
                    "WFT_ALLOWED",
                ],
            )
        ),
        encoding="utf-8",
    )
    schedule = build_wait_schedule(
        pattern,
        map_path=map_path,
        chunk_event_capacity=4,
        forbidden_stil_rate_units=("WFT_FORBIDDEN",),
    )
    schedule["policy"].pop("forbidden_stil_rate_match")
    for boundary in schedule["boundaries"]:
        boundary.pop("boundary_rate_unit")
    schedule_path = tmp_path / "legacy.wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")

    with pytest.raises(WaitScheduleError, match="forbidden STIL rate unit"):
        set_wait(
            pattern,
            schedule_path,
            map_path=map_path,
            before_chunk_index=1,
            wait_ticks=1,
            reason="legacy schedule keeps its pinned broad policy",
            output_path=tmp_path / "legacy-edited.wait.json",
        )


def test_wait_proposal_excludes_only_a_forbidden_entry_rate(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    map_path = tmp_path / "pattern.map.json"
    _pattern(pattern, count=8)
    map_path.write_text(
        json.dumps(
            _map_for_repeated_pattern(
                8,
                waveform_tables=[
                    "WFT_ALLOWED",
                    "WFT_ALLOWED",
                    "WFT_ALLOWED",
                    "WFT_FORBIDDEN",
                    "WFT_FORBIDDEN",
                    "WFT_ALLOWED",
                    "WFT_ALLOWED",
                    "WFT_ALLOWED",
                ],
            )
        ),
        encoding="utf-8",
    )

    schedule, report = propose_wait_schedule(
        pattern,
        map_path=map_path,
        chunk_event_capacity=4,
        service_time_us=0.1,
        service_time_source="bounded test profile",
        max_wait_us=1.0,
        max_total_wait_us=100.0,
        forbidden_stil_rate_units=("WFT_FORBIDDEN",),
        wait_policy="every-boundary",
    )

    assert report["wait_plan"]["status"] == "planned"
    waited_indices = {
        int(boundary["before_chunk_index"])
        for boundary in schedule["boundaries"]
        if int(boundary["wait_ticks"]) > 0
    }
    # Chunk 1 begins in WFT_ALLOWED but later includes WFT_FORBIDDEN.
    # Chunk 2 begins in WFT_FORBIDDEN and is the only excluded eligible point.
    assert schedule["boundaries"][1]["rate_units"] == [
        "WFT_ALLOWED",
        "WFT_FORBIDDEN",
    ]
    assert schedule["boundaries"][1]["boundary_rate_unit"] == "WFT_ALLOWED"
    assert waited_indices == {1, 3}


def test_set_wait_is_one_boundary_and_preserves_pattern(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=8)
    original = pattern.read_bytes()
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(json.dumps(build_wait_schedule(pattern, chunk_event_capacity=4)), encoding="utf-8")
    output = tmp_path / "edited.wait.json"

    updated = set_wait(
        pattern,
        schedule_path,
        before_chunk_index=1,
        wait_us="2",
        reason="bounded test headroom",
        output_path=output,
    )
    assert pattern.read_bytes() == original
    assert updated["boundaries"][1]["wait_ticks"] == 300
    assert updated["boundaries"][1]["wait_us"] == pytest.approx(2.0)
    assert updated["edit_history"][0]["before_chunk_index"] == 1


def test_set_wait_range_sets_uniform_nonzero_wait_without_mutating_pattern(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=16)
    original = pattern.read_bytes()
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(json.dumps(build_wait_schedule(pattern, chunk_event_capacity=4)), encoding="utf-8")
    output = tmp_path / "range.wait.json"

    updated = set_wait_range(
        pattern,
        schedule_path,
        first_chunk_index=1,
        last_chunk_index=3,
        wait_us="2000",
        reason="fixed diagnostic boundary headroom",
        output_path=output,
    )

    assert pattern.read_bytes() == original
    assert [item["wait_ticks"] for item in updated["boundaries"]] == [
        0,
        300_000,
        300_000,
        300_000,
        0,
        0,
        0,
        0,
    ]
    assert updated["edit_history"][-1]["operation"] == "set_wait_range"
    assert updated["edit_history"][-1]["boundary_count"] == 3


def test_set_wait_range_rejects_nonzero_first_boundary(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=8)
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(json.dumps(build_wait_schedule(pattern, chunk_event_capacity=4)), encoding="utf-8")

    with pytest.raises(ValueError, match="before the first chunk"):
        set_wait_range(
            pattern,
            schedule_path,
            first_chunk_index=0,
            last_chunk_index=1,
            wait_us="2000",
            reason="invalid first boundary",
            output_path=tmp_path / "invalid.wait.json",
        )


def test_set_wait_periodic_selects_absolute_boundary_phase(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=32)
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(json.dumps(build_wait_schedule(pattern, chunk_event_capacity=4)), encoding="utf-8")
    output = tmp_path / "periodic.wait.json"

    updated = set_wait_periodic(
        pattern,
        schedule_path,
        first_chunk_index=1,
        last_chunk_index=7,
        period=3,
        phase=2,
        wait_us="5",
        reason="sparse bounded diagnostic headroom",
        output_path=output,
    )

    assert [item["wait_ticks"] for item in updated["boundaries"]] == [
        0,
        0,
        750,
        0,
        0,
        750,
        0,
        0,
    ] + [0] * 8
    assert updated["edit_history"][-1]["selected_boundary_indices"] == [2, 5]


def test_encoding_check_reports_rate_budget_without_mutating_pattern(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=8)
    original = pattern.read_bytes()
    schedule = build_wait_schedule(pattern, chunk_event_capacity=4)
    schedule["boundaries"][1]["wait_ticks"] = 300_000
    schedule["boundaries"][1]["wait_us"] = 2_000.0

    report = inspect_wait_schedule_encoding(pattern.read_bytes(), schedule=schedule)

    assert report["status"] == "PASS"
    assert report["rate_budget"]["original_rate_count"] == 1
    assert report["rate_budget"]["projected_rate_count"] == 2
    assert report["encoding"]["output_pattern_version"] == 2
    assert pattern.read_bytes() == original


def test_cli_encoding_check_writes_a_host_only_report(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=8)
    schedule = tmp_path / "pattern.wait.json"
    schedule.write_text(json.dumps(build_wait_schedule(pattern, chunk_event_capacity=4)), encoding="utf-8")
    report_path = tmp_path / "encoding.json"

    result = main(
        [
            "encoding-check",
            str(pattern),
            str(schedule),
            "--json-out",
            str(report_path),
        ]
    )

    assert result == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert report["physical_execution_authorized"] is False


def test_validate_rejects_pattern_hash_and_geometry_drift(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=8)
    schedule = tmp_path / "pattern.wait.json"
    schedule.write_text(json.dumps(build_wait_schedule(pattern, chunk_event_capacity=4)), encoding="utf-8")
    pattern.write_bytes(pattern.read_bytes() + b"drift")

    report = validate_wait_schedule(pattern, schedule)
    assert report["status"] == "REJECT"
    assert any("pattern size_bytes mismatch" in error.lower() for error in report["errors"])


def test_validate_rejects_non_integral_microseconds(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=8)
    schedule = build_wait_schedule(pattern, chunk_event_capacity=4)
    schedule["boundaries"][0]["wait_ticks"] = 1
    schedule["boundaries"][0]["wait_us"] = 0.006
    schedule_path = tmp_path / "bad.wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")

    report = validate_wait_schedule(pattern, schedule_path)
    assert report["status"] == "REJECT"
    assert any("wait invalid" in error for error in report["errors"])


def test_cli_validate_returns_reject_and_can_write_report(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=8)
    schedule = tmp_path / "pattern.wait.json"
    schedule.write_text(json.dumps(build_wait_schedule(pattern, chunk_event_capacity=4)), encoding="utf-8")
    data = json.loads(schedule.read_text(encoding="utf-8"))
    data["policy"]["automatic_insertion"] = True
    schedule.write_text(json.dumps(data), encoding="utf-8")
    report_path = tmp_path / "validation.json"

    assert main(["validate", str(pattern), str(schedule), "--json-out", str(report_path)]) == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "REJECT"


def test_validate_malformed_model_is_a_durable_reject(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=4)
    schedule = build_wait_schedule(pattern, chunk_event_capacity=4)
    schedule["model"]["chunk_event_capacity"] = "not-an-integer"
    schedule_path = tmp_path / "bad-model.wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")

    report = validate_wait_schedule(pattern, schedule_path)
    assert report["status"] == "REJECT"
    assert any("model.chunk_event_capacity" in error for error in report["errors"])


def test_acceptance_requires_a_declared_service_profile(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=8)
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(json.dumps(build_wait_schedule(pattern, chunk_event_capacity=4)), encoding="utf-8")

    report = evaluate_wait_schedule(pattern, schedule_path)

    assert report["status"] == "REQUIRES_SERVICE_PROFILE"
    assert report["wait_decision"]["status"] == "WAIT_UNDETERMINED"
    assert report["steady_tfill"]["status"] == "not_emitted"
    assert report["physical_execution_authorized"] is False


def test_acceptance_is_bounded_and_does_not_emit_tfill(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=80)
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(json.dumps(build_wait_schedule(pattern, chunk_event_capacity=4)), encoding="utf-8")

    report = evaluate_wait_schedule(
        pattern,
        schedule_path,
        service_time_us=0.2,
        service_time_source="bounded unit-test profile",
    )

    assert report["status"] == "ACCEPTED"
    assert report["wait_decision"]["status"] == "WAIT_NOT_REQUIRED"
    assert report["comparison"]["candidate_underflow_chunks"] == []
    assert report["steady_tfill"]["status"] == "not_emitted"
    assert report["physical_execution_authorized"] is False


def test_acceptance_rejects_remaining_model_underflow(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=80)
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(json.dumps(build_wait_schedule(pattern, chunk_event_capacity=4)), encoding="utf-8")

    report = evaluate_wait_schedule(
        pattern,
        schedule_path,
        service_time_us=1.0,
        service_time_source="bounded unit-test profile",
    )

    assert report["status"] == "REJECTED"
    assert report["wait_decision"]["status"] == "WAIT_REJECTED"
    assert report["comparison"]["candidate_underflow_chunks"]


def test_acceptance_enforces_declared_wait_limits(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=8)
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(json.dumps(build_wait_schedule(pattern, chunk_event_capacity=4)), encoding="utf-8")
    edited_path = tmp_path / "edited.wait.json"
    set_wait(
        pattern,
        schedule_path,
        before_chunk_index=1,
        wait_us="2",
        reason="bounded unit-test headroom",
        output_path=edited_path,
    )

    report = evaluate_wait_schedule(
        pattern,
        edited_path,
        service_time_us=0.2,
        service_time_source="bounded unit-test profile",
        max_wait_us=1.0,
    )

    assert report["status"] == "REJECTED"
    assert any("max_wait_us exceeded" in error for error in report["errors"])


def _runtime_model_for_boundary_constraint() -> dict:
    chunks = [
        {
            "chunk_index": index,
            "event_start": index * 100,
            "consume_ticks": 100,
        }
        for index in range(8)
    ]
    return {
        "model": {
            "tick_hz": 1_000_000,
            "ring_slot_count": 3,
        },
        "pattern": {
            "total_consume_ticks": 800,
            "chunks": chunks,
        },
    }


def test_runtime_planner_relocates_wait_from_forbidden_boundary() -> None:
    model = _runtime_model_for_boundary_constraint()

    plan = plan_waits(
        model,
        service_time_us=150,
        service_time_source="bounded unit-test profile",
        max_wait_us=1000,
        max_total_wait_us=10_000,
        forbidden_wait_chunks={2},
    )

    assert plan["status"] == "planned"
    assert plan["boundary_constraints"]["forbidden_wait_chunks"] == [2]
    waits = plan["schedule"]["waits"]
    assert waits
    assert all(item["before_chunk_index"] != 2 for item in waits)
    assert waits[0]["requested_before_chunk_index"] == 2
    assert waits[0]["before_chunk_index"] == 1
    assert waits[0]["relocated_forbidden_boundary"] is True
    assert plan["schedule"]["final_underflow_pressure_chunks"] == []

    explicit_waits = {
        int(item["before_chunk_index"]): int(item["wait_ticks"])
        for item in waits
    }
    simulation = simulate_explicit_waits(
        model,
        explicit_waits=explicit_waits,
        service_time_us=150,
        service_time_source="bounded unit-test profile",
    )
    assert simulation["simulation"]["underflow_chunks"] == []


def test_runtime_planner_rejects_when_no_legal_boundary_can_relocate() -> None:
    model = _runtime_model_for_boundary_constraint()

    plan = plan_waits(
        model,
        service_time_us=150,
        service_time_source="bounded unit-test profile",
        max_wait_us=1000,
        max_total_wait_us=10_000,
        forbidden_wait_chunks={1, 2},
    )

    assert plan["status"] == "rejected"
    assert "no legal wait boundary" in plan["reason"]
    assert plan["boundary_constraints"]["forbidden_wait_chunks"] == [1, 2]


def test_runtime_planner_every_boundary_waits_on_all_eligible_boundaries() -> None:
    model = _runtime_model_for_boundary_constraint()

    plan = plan_waits(
        model,
        service_time_us=150,
        service_time_source="bounded unit-test profile",
        max_wait_us=1000,
        max_total_wait_us=10_000,
        forbidden_wait_chunks={2},
        wait_policy="every-boundary",
    )

    assert plan["status"] == "planned"
    assert plan["wait_policy"] == "every-boundary"
    waits = plan["schedule"]["waits"]
    assert {item["before_chunk_index"] for item in waits} == {1, 3, 4, 5, 6, 7}
    assert {item["wait_ticks"] for item in waits} == {1000}
    assert plan["boundary_constraints"]["skipped_forbidden_boundary_indices"] == [2]
    assert plan["schedule"]["final_underflow_pressure_chunks"] == []


def test_runtime_planner_every_boundary_records_natural_gap_skip() -> None:
    model = _runtime_model_for_boundary_constraint()
    model["pattern"]["chunks"][3]["consume_ticks"] = 1000

    plan = plan_waits(
        model,
        service_time_us=150,
        service_time_source="bounded unit-test profile",
        max_wait_us=1000,
        max_total_wait_us=10_000,
        wait_policy="every-boundary",
    )

    assert plan["status"] == "planned"
    assert plan["boundary_constraints"]["skipped_natural_gap_indices"] == [4]
    assert 4 not in {item["before_chunk_index"] for item in plan["schedule"]["waits"]}


def test_runtime_planner_every_boundary_fails_closed_on_total_limit() -> None:
    plan = plan_waits(
        _runtime_model_for_boundary_constraint(),
        service_time_us=150,
        service_time_source="bounded unit-test profile",
        max_wait_us=1000,
        max_total_wait_us=5000,
        wait_policy="every-boundary",
    )

    assert plan["status"] == "rejected"
    assert "every-boundary" in plan["reason"]
    assert "max_total_wait_us" in plan["reason"]


def test_cli_propose_supports_every_boundary_policy(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=80)
    map_path = tmp_path / "pattern.map.json"
    map_path.write_text(json.dumps(_map_for_repeated_pattern(80)), encoding="utf-8")
    schedule_path = tmp_path / "every.wait.json"
    report_path = tmp_path / "every.proposal.json"

    assert main(
        [
            "propose",
            str(pattern),
            "--map",
            str(map_path),
            "--service-us",
            "0.2",
            "--service-source",
            "bounded test profile",
            "--max-wait-us",
            "2",
            "--max-total-wait-us",
            "1000",
            "--wait-policy",
            "every-boundary",
            "--output",
            str(schedule_path),
            "--json-out",
            str(report_path),
            "--acceptance-out",
            str(tmp_path / "every.acceptance.json"),
        ]
    ) == 0

    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    proposal = json.loads(report_path.read_text(encoding="utf-8"))
    assert schedule["policy"]["wait_planning_policy"] == "every-boundary"
    assert proposal["wait_plan"]["wait_policy"] == "every-boundary"
    assert all(int(boundary["wait_ticks"]) > 0 for boundary in schedule["boundaries"][1:])


def test_cli_propose_writes_editable_schedule_and_model_report(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=80)
    map_path = tmp_path / "pattern.map.json"
    map_path.write_text(json.dumps(_map_for_repeated_pattern(80)), encoding="utf-8")
    schedule_path = tmp_path / "proposed.wait.json"
    report_path = tmp_path / "proposal.json"

    result = main(
        [
            "propose",
            str(pattern),
            "--map",
            str(map_path),
            "--service-us",
            "1",
            "--service-source",
            "bounded test profile",
            "--max-wait-us",
            "2",
            "--max-total-wait-us",
            "20",
            "--output",
            str(schedule_path),
            "--json-out",
            str(report_path),
        ]
    )

    assert result in {0, 2}
    schedule = json.loads(schedule_path.read_text())
    proposal = json.loads(report_path.read_text())
    assert schedule["proposal"]["kind"] == "bounded_host_model_wait_proposal"
    assert proposal["wait_plan"]["status"] == schedule["proposal"]["status"]
    assert schedule["policy"]["pattern_mutation"] is False


def test_apply_wait_schedule_requires_acceptance_and_promotes_wide_rate(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=8)
    original = pattern.read_bytes()
    map_path = tmp_path / "pattern.map.json"
    map_path.write_text(json.dumps(_map_for_repeated_pattern(8)), encoding="utf-8")
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(
        json.dumps(build_wait_schedule(pattern, map_path=map_path, chunk_event_capacity=4)),
        encoding="utf-8",
    )
    edited_path = tmp_path / "edited.wait.json"
    set_wait(
        pattern,
        schedule_path,
        map_path=map_path,
        before_chunk_index=1,
        wait_us="2000",
        reason="explicit test wait",
        output_path=edited_path,
    )
    acceptance_path = tmp_path / "acceptance.json"
    acceptance = evaluate_wait_schedule(
        pattern,
        edited_path,
        map_path=map_path,
        service_time_us=0.2,
        service_time_source="bounded test profile",
    )
    assert acceptance["status"] == "ACCEPTED"
    acceptance_path.write_text(json.dumps(acceptance), encoding="utf-8")
    output_dir = tmp_path / "derived"

    result = apply_wait_schedule_files(
        pattern,
        edited_path,
        map_path=map_path,
        acceptance_path=acceptance_path,
        output_dir=output_dir,
    )

    assert result["status"] == "VERIFIED"
    assert pattern.read_bytes() == original
    assert json.loads((output_dir / "pattern.wait.json").read_text())["status"] == "APPLIED"
    assert json.loads((output_dir / "pattern.wait.validation.json").read_text())["status"] == "PASS"
    assert json.loads((output_dir / "pattern.wait.patch.json").read_text())["encoding"]["wide_rate_vector_promoted"] is True
    from stpg.pattern import unpack_rate_vector_full

    assert unpack_rate_vector_full((output_dir / "pattern.bin").read_bytes())[0].version == 2


def test_apply_wait_schedule_propagates_explicit_rate_limit_to_derived_validation(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    slots = [
        RateSlot(
            time_ticks=0,
            flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
            drive_mask=0xFFFF,
            dir_mask=0xFFFF,
            sample_mask=0,
        ),
        RateSlot(time_ticks=15, flags=STPG_DRIVE_VECTOR, drive_mask=0xFFFF, dir_mask=0, sample_mask=0),
    ] * 16
    pattern.write_bytes(
        pack_rate_vector(
            rates=[RateDesc(period_ticks=30 + index, slot_count=2, flags=0, slot_offset=index * 2) for index in range(16)],
            slots=slots,
            vectors=[
                VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=1, drive_enable=3, expect_value=0, expect_mask=0)
                for _ in range(8)
            ],
            tick_hz=150_000_000,
            active_pins=16,
            max_rates=32,
        )
    )
    map_path = tmp_path / "pattern.map.json"
    map_path.write_text(json.dumps(_map_for_repeated_pattern(8)), encoding="utf-8")
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(
        json.dumps(build_wait_schedule(pattern, map_path=map_path, chunk_event_capacity=4, max_rates=32)),
        encoding="utf-8",
    )
    edited_path = tmp_path / "edited.wait.json"
    set_wait_range(
        pattern,
        schedule_path,
        map_path=map_path,
        first_chunk_index=1,
        last_chunk_index=2,
        wait_us="2",
        reason="explicit rate-limit propagation test",
        output_path=edited_path,
    )
    acceptance = evaluate_wait_schedule(
        pattern,
        edited_path,
        map_path=map_path,
        service_time_us=0.2,
        service_time_source="bounded unit-test profile",
    )
    assert acceptance["status"] == "ACCEPTED"
    acceptance_path = tmp_path / "acceptance.json"
    acceptance_path.write_text(json.dumps(acceptance), encoding="utf-8")

    output_dir = tmp_path / "derived"
    result = apply_wait_schedule_files(
        pattern,
        edited_path,
        map_path=map_path,
        acceptance_path=acceptance_path,
        output_dir=output_dir,
        max_rates=32,
    )

    assert result["status"] == "VERIFIED"
    derived_header = unpack_rate_vector_full((output_dir / "pattern.bin").read_bytes())[0]
    assert derived_header.rate_count == 17
    assert json.loads((output_dir / "pattern.wait.validation.json").read_text())["status"] == "PASS"


def test_apply_zero_wait_is_a_successful_noop(tmp_path: Path) -> None:
    pattern = tmp_path / "pattern.bin"
    _pattern(pattern, count=80)
    map_path = tmp_path / "pattern.map.json"
    map_path.write_text(json.dumps(_map_for_repeated_pattern(80)), encoding="utf-8")
    schedule_path = tmp_path / "pattern.wait.json"
    schedule_path.write_text(
        json.dumps(build_wait_schedule(pattern, map_path=map_path, chunk_event_capacity=4)),
        encoding="utf-8",
    )
    acceptance = evaluate_wait_schedule(
        pattern,
        schedule_path,
        map_path=map_path,
        service_time_us=0.2,
        service_time_source="bounded unit-test profile",
    )
    assert acceptance["status"] == "ACCEPTED"
    acceptance_path = tmp_path / "acceptance.json"
    acceptance_path.write_text(json.dumps(acceptance), encoding="utf-8")

    output_dir = tmp_path / "no-derived-artifact"
    result = apply_wait_schedule_files(
        pattern,
        schedule_path,
        map_path=map_path,
        acceptance_path=acceptance_path,
        output_dir=output_dir,
    )

    assert result["status"] == "NOT_REQUIRED"
    assert result["changed"] is False
    assert not output_dir.exists()

    assert main(
        [
            "apply",
            str(pattern),
            str(schedule_path),
            "--map",
            str(map_path),
            "--acceptance",
            str(acceptance_path),
            "--output-dir",
            str(output_dir),
        ]
    ) == 0
    assert not output_dir.exists()
