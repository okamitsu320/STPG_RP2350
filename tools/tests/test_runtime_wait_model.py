from __future__ import annotations

from pathlib import Path

from stpg.constants import STPG_DRIVE_VECTOR, STPG_RATE_SLOT_DIR_UPDATE
from stpg.pattern import RateDesc, RateSlot, VectorRecord, pack_rate_vector
import stil.map_lookup as map_lookup
from stil.runtime_wait_model import (
    analyze_pattern_with_wait_policy,
    build_runtime_model,
    build_runtime_chunks,
    runtime_chunk_rate_units,
    runtime_chunk_source_contexts,
)
from stil.wait_schedule import _chunk_source_context


def _mixed_period_pattern() -> bytes:
    """Public mathematical replacement, not copied historical artifact bytes.

    Five 4096-vector phases, two events at ticks 0/15, period 750/600/450/300/30
    at 150 MHz. This retains the original literal window/wait assertions.
    """
    return pack_rate_vector(
        rates=[RateDesc(period_ticks=period, slot_count=2, flags=0, slot_offset=2 * i)
               for i, period in enumerate((750, 600, 450, 300, 30))],
        slots=[slot for _ in range(5) for slot in (
            RateSlot(time_ticks=0, flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
                     drive_mask=0xFFFF, dir_mask=0xFFFF, sample_mask=0),
            RateSlot(time_ticks=15, flags=STPG_DRIVE_VECTOR,
                     drive_mask=0xFFFF, dir_mask=0, sample_mask=0))],
        vectors=[VectorRecord(rate_id=i, flags=0, repeat=4096, drive_value=1,
                              drive_enable=3, expect_value=0, expect_mask=0) for i in range(5)],
        tick_hz=150_000_000, active_pins=16)


def _pattern(
    *, period_ticks: int = 30, repeat: int = 8, vector_count: int = 1
) -> bytes:
    return pack_rate_vector(
        rates=[RateDesc(period_ticks=period_ticks, slot_count=2, flags=0, slot_offset=0)],
        slots=[
            RateSlot(
                time_ticks=0,
                flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
                drive_mask=0xFFFF,
                dir_mask=0xFFFF,
                sample_mask=0,
            ),
            RateSlot(
                time_ticks=15,
                flags=STPG_DRIVE_VECTOR,
                drive_mask=0xFFFF,
                dir_mask=0,
                sample_mask=0,
            ),
        ],
        vectors=[
            VectorRecord(
                rate_id=0,
                flags=0,
                repeat=repeat,
                drive_value=1,
                drive_enable=3,
                expect_value=0,
                expect_mask=0,
            )
            for _ in range(vector_count)
        ],
        tick_hz=150_000_000,
        active_pins=16,
    )


def test_chunk_consumption_uses_emitted_group_deltas() -> None:
    total_events, chunks = build_runtime_chunks(_pattern(repeat=4), chunk_event_capacity=4)

    assert total_events == 8
    assert len(chunks) == 2
    # First event is at logical time zero; the remaining three deltas are 15.
    assert chunks[0].consume_ticks == 45
    # The next chunk starts at the next cycle and therefore includes 15, 15, 15, 15.
    assert chunks[1].consume_ticks == 60
    assert chunks[0].event_start == 0
    assert chunks[0].event_end_exclusive == 4
    assert chunks[1].event_start == 4
    assert chunks[1].event_end_exclusive == 8


def test_runtime_chunk_rate_units_keeps_source_mapping_across_chunk_cursor() -> None:
    total_events, chunks = build_runtime_chunks(_pattern(repeat=6), chunk_event_capacity=2)
    assert total_events == 12

    map_data = {
        "vectors": {
            "0": {"source_map": [{"waveform_table": "WFT_RATE_0"}]},
        }
    }
    assert runtime_chunk_rate_units(_pattern(repeat=6), chunks, map_data) == [
        ["WFT_RATE_0"],
        ["WFT_RATE_0"],
        ["WFT_RATE_0"],
        ["WFT_RATE_0"],
        ["WFT_RATE_0"],
        ["WFT_RATE_0"],
    ]


def test_runtime_chunk_source_context_separates_entry_rate_from_later_rates() -> None:
    pattern = _pattern(repeat=1, vector_count=4)
    total_events, chunks = build_runtime_chunks(pattern, chunk_event_capacity=4)
    assert total_events == 8
    map_data = {
        "vectors": {
            str(index): {
                "source_map": [{"waveform_table": waveform_table}]
            }
            for index, waveform_table in enumerate(
                ["WFT_ENTRY", "WFT_LATER", "WFT_ENTRY", "WFT_ENTRY"]
            )
        }
    }

    contexts = runtime_chunk_source_contexts(pattern, chunks, map_data)

    assert contexts == [
        {
            "rate_units": ["WFT_ENTRY", "WFT_LATER"],
            "boundary_rate_unit": "WFT_ENTRY",
        },
        {
            "rate_units": ["WFT_ENTRY"],
            "boundary_rate_unit": "WFT_ENTRY",
        },
    ]


def test_source_map_occurrence_is_resolved_once_for_each_multigroup_vector(monkeypatch) -> None:
    pattern = _pattern(repeat=1)
    total_events, chunks = build_runtime_chunks(pattern, chunk_event_capacity=2)
    assert total_events == 2
    map_data = {
        "vectors": {
            "0": {"source_map": [{"waveform_table": "WFT_RATE_0", "global_vector_index": 0}]}
        }
    }
    original = map_lookup._source_map_entry_for_occurrence
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(map_lookup, "_source_map_entry_for_occurrence", counted)

    contexts = _chunk_source_context(pattern, chunks, map_data)
    assert calls == 1
    assert contexts[0]["first_event"]["source"]["waveform_table"] == "WFT_RATE_0"
    assert contexts[0]["last_event"]["source"]["waveform_table"] == "WFT_RATE_0"

    calls = 0
    rate_contexts = runtime_chunk_source_contexts(pattern, chunks, map_data)
    assert calls == 1
    assert rate_contexts == [
        {"rate_units": ["WFT_RATE_0"], "boundary_rate_unit": "WFT_RATE_0"}
    ]


def test_f19_model_reports_exact_steady_chunk_window() -> None:
    report = build_runtime_model(
        _mixed_period_pattern(), chunk_event_capacity=1024, ring_slot_count=6
    )
    pattern = report["pattern"]

    assert pattern["expanded_event_count"] == 40_960
    assert pattern["chunk_count"] == 40
    assert pattern["chunks"][0]["consume_us"] == 2_555.1
    assert all(chunk["consume_us"] == 2_560.0 for chunk in pattern["chunks"][1:8])
    assert pattern["chunks"][32]["contains_rate_transition"] is False
    assert pattern["chunks"][32]["consume_us"] == 104.2
    assert all(chunk["consume_us"] == 102.4 for chunk in pattern["chunks"][33:40])


def test_wait_planner_is_fail_closed_without_limits() -> None:
    report = analyze_pattern_with_wait_policy(
        _pattern(period_ticks=30, repeat=100),
        service_time_us=500.0,
        service_time_source="single-run diagnostic input",
    )

    assert report["wait_plan"]["status"] == "requires_limits"
    assert report["wait_plan"]["bounded"] is False
    assert report["wait_plan"]["schedule"]["waits"] == []


def test_wait_planner_needs_no_wait_when_service_fits_consumer_window() -> None:
    report = analyze_pattern_with_wait_policy(
        _mixed_period_pattern(),
        service_time_us=65.0,
        service_time_source="R092 steady-state maximum; bounded diagnostic only",
        safety_margin_us=10.0,
        max_wait_us=3_200.0,
        max_total_wait_us=20_000.0,
    )

    plan = report["wait_plan"]
    assert plan["status"] == "no_wait_needed"
    assert plan["bounded"] is True
    assert plan["objective"]["wait_count"] == 0


def test_wait_planner_batches_deficit_under_declared_limits() -> None:
    report = analyze_pattern_with_wait_policy(
        _pattern(period_ticks=30, repeat=5_000),
        service_time_us=896.5,
        service_time_source="R084 steady-state average; not a universal worst case",
        safety_margin_us=0.0,
        max_wait_us=3_200.0,
        max_total_wait_us=100_000.0,
    )

    plan = report["wait_plan"]
    assert plan["status"] == "planned"
    assert plan["objective"]["wait_count"] >= 1
    assert plan["objective"]["max_wait_us"] <= 3_200.0
    assert plan["objective"]["total_wait_us"] <= 100_000.0
    assert all(item["before_event"] % 1024 == 0 for item in plan["schedule"]["waits"])
    assert all(item["reason"] == "frequency_first_prepaid_deficit" for item in plan["schedule"]["waits"])


def test_wait_planner_rejects_when_total_wait_limit_is_too_small() -> None:
    report = analyze_pattern_with_wait_policy(
        _pattern(period_ticks=30, repeat=5_000),
        service_time_us=896.5,
        service_time_source="bounded test input",
        max_wait_us=3_200.0,
        max_total_wait_us=0.001,
    )

    assert report["wait_plan"]["status"] == "rejected"
    assert "max_total_wait_us" in report["wait_plan"]["reason"]


def test_ring_depth7_preserves_chunks_and_reduces_bounded_wait() -> None:
    baseline = analyze_pattern_with_wait_policy(
        _mixed_period_pattern(),
        chunk_event_capacity=1024,
        ring_slot_count=6,
        service_time_us=896.40625,
        service_time_source="bounded sensitivity profile",
        max_wait_us=3_200.0,
        max_total_wait_us=100_000.0,
        pattern_time_limit_us=70_000.0,
    )
    candidate = analyze_pattern_with_wait_policy(
        _mixed_period_pattern(),
        chunk_event_capacity=1024,
        ring_slot_count=7,
        service_time_us=896.40625,
        service_time_source="bounded sensitivity profile",
        max_wait_us=3_200.0,
        max_total_wait_us=100_000.0,
        pattern_time_limit_us=70_000.0,
    )
    assert baseline["pattern"]["chunk_count"] == candidate["pattern"]["chunk_count"] == 40
    assert baseline["pattern"]["expanded_event_count"] == candidate["pattern"]["expanded_event_count"] == 40_960
    assert baseline["wait_plan"]["objective"]["wait_count"] == 1
    assert candidate["wait_plan"]["objective"]["wait_count"] == 1
    assert baseline["wait_plan"]["objective"]["max_wait_us"] == 2867.0266666666666
    assert candidate["wait_plan"]["objective"]["max_wait_us"] == 1970.62
    assert candidate["wait_plan"]["schedule"]["waits"][0]["before_event"] == 37888
