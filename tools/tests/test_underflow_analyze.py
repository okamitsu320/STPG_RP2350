from __future__ import annotations

import json
from pathlib import Path

from helpers.rate_vector_patterns import build_cases
from stil.cli_underflow_analyze import main as underflow_main
from stil.pipeline import CompileOptions, compile_stil
from stil.underflow_analyze import analyze_rate_vector_underflow, build_practical_runtime_hints
from stpg.pattern import LoopBlock, LoopRefRecord, RateDesc, RateSlot, VectorRecord, pack_rate_vector
from stpg.constants import STPG_DRIVE_VECTOR, STPG_RATE_SLOT_DIR_UPDATE, STPG_RATE_SLOT_SAMPLE


def _build_deficit_pattern() -> bytes:
    return pack_rate_vector(
        rates=[RateDesc(period_ticks=12, slot_count=2, flags=0, slot_offset=0)],
        slots=[
            RateSlot(time_ticks=0, flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=0x3, dir_mask=0x3, sample_mask=0),
            RateSlot(time_ticks=6, flags=STPG_RATE_SLOT_SAMPLE, drive_mask=0, dir_mask=0, sample_mask=0x3),
        ],
        vectors=[VectorRecord(rate_id=0, flags=0, repeat=5000, drive_value=0x1, drive_enable=0x3, expect_value=0x1, expect_mask=0x3)],
        tick_hz=150_000_000,
        active_pins=16,
    )


def test_underflow_analyzer_reports_wait_candidates() -> None:
    report = analyze_rate_vector_underflow(_build_deficit_pattern(), chunk_event_budget=4096)
    assert report["summary"]["streaming_poc_compatibility"] is False
    assert report["summary"]["timing_model_variant"] == "full"
    assert report["summary"]["model_confidence"] == "measured-safe"
    assert report["summary"]["timing_model_notes"] == {}
    assert report["rates"][0]["groups"][0]["deficit_ticks"] > 0
    assert report["local_wait_candidates"]
    assert report["generator_avoidance_candidates"]
    assert report["chunk_windows"][0]["score"] > 0


def test_underflow_analyzer_prioritizes_first_hard_error_candidate() -> None:
    payload = pack_rate_vector(
        rates=[RateDesc(period_ticks=15, slot_count=2, flags=0, slot_offset=0)],
        slots=[
            RateSlot(
                time_ticks=0,
                flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
                drive_mask=0x3,
                dir_mask=0x3,
                sample_mask=0,
            ),
            RateSlot(
                time_ticks=8,
                flags=STPG_RATE_SLOT_SAMPLE,
                drive_mask=0,
                dir_mask=0,
                sample_mask=0x3,
            ),
        ],
        vectors=[VectorRecord(rate_id=0, flags=0, repeat=5000, drive_value=0x1, drive_enable=0x3, expect_value=0x1, expect_mask=0x3)],
        tick_hz=150_000_000,
        active_pins=24,
    )
    report = analyze_rate_vector_underflow(payload, chunk_event_budget=4096)
    assert report["local_wait_candidates"]
    assert report["local_wait_candidates"][0]["group_index"] == 0
    assert report["local_wait_candidates"][0]["minimum_extra_ticks"] == 3


def test_underflow_analyzer_ignores_sample_mask_without_sample_flag() -> None:
    payload = pack_rate_vector(
        rates=[RateDesc(period_ticks=15, slot_count=2, flags=0, slot_offset=0)],
        slots=[
            RateSlot(
                time_ticks=0,
                flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
                drive_mask=0x3,
                dir_mask=0x3,
                sample_mask=0,
            ),
            RateSlot(
                time_ticks=8,
                flags=0,
                drive_mask=0,
                dir_mask=0,
                sample_mask=0x3,
            ),
        ],
        vectors=[VectorRecord(rate_id=0, flags=0, repeat=10, drive_value=0x1, drive_enable=0x3, expect_value=0x1, expect_mask=0x3)],
        tick_hz=150_000_000,
        active_pins=24,
    )
    report = analyze_rate_vector_underflow(payload, chunk_event_budget=4096)
    groups = report["rates"][0]["groups"]
    assert groups[0]["sample_trigger"] is False
    assert groups[0]["required_ticks"] == 10
    assert groups[1]["sample_trigger"] is False
    assert groups[1]["required_ticks"] == 10


def test_underflow_analyzer_marks_sv06_as_low_risk() -> None:
    case = build_cases()["SV-06-300NS"]
    payload = case.build_pattern(tick_hz=150_000_000)
    report = analyze_rate_vector_underflow(payload, chunk_event_budget=4096)
    assert report["summary"]["streaming_poc_compatibility"] is True
    assert report["summary"]["nonzero_deficit_rate_count"] == 0
    assert report["local_wait_candidates"] == []


def test_underflow_analyzer_loop_is_reduced_confidence() -> None:
    payload = pack_rate_vector(
        rates=[RateDesc(period_ticks=16, slot_count=1, flags=0, slot_offset=0)],
        slots=[RateSlot(time_ticks=0, flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=0x3, dir_mask=0x3, sample_mask=0)],
        vectors=[LoopRefRecord(loop_id=0)],
        loop_blocks=[LoopBlock(loop_count=4, body_records=(VectorRecord(rate_id=0, flags=0, repeat=2, drive_value=1, drive_enable=3, expect_value=0, expect_mask=0),))],
        tick_hz=150_000_000,
        active_pins=16,
    )
    report = analyze_rate_vector_underflow(payload, chunk_event_budget=4096)
    assert report["summary"]["confidence"] == "reduced"


def test_underflow_analyzer_marks_reduced_timing_model_as_heuristic() -> None:
    report = analyze_rate_vector_underflow(
        _build_deficit_pattern(),
        compile_report={
            "capability_precheck": {
                "runtime_precheck": {
                    "reduced_density_candidate": True,
                }
            }
        },
        chunk_event_budget=4096,
    )
    assert report["summary"]["timing_model_variant"] == "reduced"
    assert report["summary"]["model_confidence"] == "measured-spacing-heuristic-phase"
    assert report["summary"]["timing_model_notes"]["measured_output_half_cycle_ticks"] == 33
    assert report["summary"]["timing_model_notes"]["measured_output_period_ticks"] == 66


def test_underflow_analyzer_marks_compiled_reduced_candidate_as_heuristic() -> None:
    payload, _map_data, report_data = compile_stil(
        CompileOptions(
            input_path=Path("tools/tests/fixtures/stil/c1_macro_m1_sampled_200ns.stil"),
            mode="rate-vector",
            tick_hz=150_000_000,
        )
    )
    runtime_precheck = report_data["capability_precheck"]["runtime_precheck"]
    assert runtime_precheck["reduced_density_candidate"] is True
    report = analyze_rate_vector_underflow(payload, compile_report=report_data, chunk_event_budget=4096)
    assert report["summary"]["timing_model_variant"] == "reduced"
    assert report["summary"]["model_confidence"] == "measured-spacing-heuristic-phase"


def test_underflow_cli_writes_json_and_uses_sidecars(tmp_path: Path, capsys) -> None:
    payload_path = tmp_path / "sample.bin"
    payload_path.write_bytes(_build_deficit_pattern())
    map_path = tmp_path / "sample.map.json"
    map_path.write_text(
        json.dumps(
            {
                "vectors": {
                    "0": {
                        "source_map": [
                            {
                                "global_vector_index": 100,
                                "stil_line": 42,
                                "label": "L0",
                                "pattern_name": "PAT0",
                                "waveform_table": "WFT0",
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "sample.compile_report.json"
    report_path.write_text(json.dumps({"capability_precheck": {"runtime_precheck": {"estimated_event_count": 10000}}}), encoding="utf-8")
    json_out = tmp_path / "report.json"
    assert underflow_main([str(payload_path), "--json-out", str(json_out)]) == 0
    stdout = capsys.readouterr().out
    assert "Top Risky Rates" in stdout
    assert "timing_model_variant=full model_confidence=measured-safe" in stdout
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["vector_spans"][0]["global_vector_start"] == 100
    assert data["compile_report_runtime_precheck"]["estimated_event_count"] == 10000


def test_underflow_analyzer_prepends_runtime_page30_candidate() -> None:
    pattern = pack_rate_vector(
        rates=[RateDesc(period_ticks=10, slot_count=1, flags=0, slot_offset=0)],
        slots=[RateSlot(time_ticks=0, flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=0x3, dir_mask=0x3, sample_mask=0)],
        vectors=[VectorRecord(rate_id=0, flags=0, repeat=32, drive_value=0x1, drive_enable=0x3, expect_value=0, expect_mask=0)],
        tick_hz=150_000_000,
        active_pins=24,
    )
    map_data = {
        "vectors": {
            "0": {
                "source_map": [
                    {
                        "global_vector_index": index,
                        "stil_line": 100 + index,
                        "pattern_name": "PAT0",
                        "waveform_table": "WFT0",
                    }
                    for index in range(32)
                ]
            }
        }
    }
    runtime_report = {
        "post_run": {
            "page30": {
                "pattern_size": 13,
                "event_count": 0,
                "run_progress": (10 << 16) | 0,
                "result_flags": 7,
                "fail_count": 10,
                "sample_count": 0,
                "ok": True,
            }
        }
    }

    report = analyze_rate_vector_underflow(pattern, map_data=map_data, runtime_report=runtime_report)
    candidate = report["page30_local_wait_candidate"]
    assert candidate is not None
    assert candidate["candidate_source"] == "runtime_page30"
    assert candidate["rate_id"] == 0
    assert candidate["group_index"] == 0
    assert candidate["minimum_extra_ticks"] == 3
    assert report["local_wait_candidates"][0] == candidate


def test_underflow_analyzer_emits_compare24_practical_hard_error() -> None:
    payload = pack_rate_vector(
        rates=[RateDesc(period_ticks=15, slot_count=2, flags=0, slot_offset=0)],
        slots=[
            RateSlot(
                time_ticks=0,
                flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
                drive_mask=0x3,
                dir_mask=0x3,
                sample_mask=0,
            ),
            RateSlot(
                time_ticks=8,
                flags=STPG_RATE_SLOT_SAMPLE,
                drive_mask=0,
                dir_mask=0,
                sample_mask=0x3,
            ),
        ],
        vectors=[VectorRecord(rate_id=0, flags=0, repeat=5000, drive_value=0x1, drive_enable=0x3, expect_value=0x1, expect_mask=0x3)],
        tick_hz=150_000_000,
        active_pins=24,
    )
    report = analyze_rate_vector_underflow(payload, chunk_event_budget=4096)
    hints = build_practical_runtime_hints(report)
    assert hints["practical_hard_errors"]
    assert hints["practical_hard_errors"][0]["code"] == "ERR_PRACTICAL_COMPARE24_TIMING"
    assert "add at least 3 tick(s)" in hints["practical_hard_errors"][0]["message"]


def test_underflow_analyzer_hard_gate_does_not_require_drive_ops_shape() -> None:
    payload = pack_rate_vector(
        rates=[RateDesc(period_ticks=15, slot_count=2, flags=0, slot_offset=0)],
        slots=[
            RateSlot(
                time_ticks=0,
                flags=STPG_RATE_SLOT_DIR_UPDATE,
                drive_mask=0,
                dir_mask=0x3,
                sample_mask=0,
            ),
            RateSlot(
                time_ticks=8,
                flags=STPG_RATE_SLOT_SAMPLE | STPG_DRIVE_VECTOR,
                drive_mask=0x3,
                dir_mask=0,
                sample_mask=0x3,
            ),
        ],
        vectors=[VectorRecord(rate_id=0, flags=0, repeat=5000, drive_value=0x1, drive_enable=0x3, expect_value=0x1, expect_mask=0x3)],
        tick_hz=150_000_000,
        active_pins=24,
    )
    report = analyze_rate_vector_underflow(payload, chunk_event_budget=4096)
    hints = build_practical_runtime_hints(report)
    assert hints["practical_hard_errors"]
    assert hints["practical_hard_errors"][0]["context"]["rate_id"] == 0


def test_underflow_analyzer_hard_gate_is_not_limited_by_top_n_display() -> None:
    rates = []
    slots = []
    vectors = []
    for rate_id in range(12):
        slot_offset = len(slots)
        rates.append(RateDesc(period_ticks=15, slot_count=2, flags=0, slot_offset=slot_offset))
        slots.extend(
            [
                RateSlot(
                    time_ticks=0,
                    flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
                    drive_mask=0x3,
                    dir_mask=0x3,
                    sample_mask=0,
                ),
                RateSlot(
                    time_ticks=8,
                    flags=STPG_RATE_SLOT_SAMPLE,
                    drive_mask=0,
                    dir_mask=0,
                    sample_mask=0x3,
                ),
            ]
        )
        vectors.append(
            VectorRecord(
                rate_id=rate_id,
                flags=0,
                repeat=5000,
                drive_value=0x1,
                drive_enable=0x3,
                expect_value=0x1,
                expect_mask=0x3,
            )
        )
    payload = pack_rate_vector(
        rates=rates,
        slots=slots,
        vectors=vectors,
        tick_hz=150_000_000,
        active_pins=24,
    )
    report = analyze_rate_vector_underflow(payload, chunk_event_budget=4096, top_n=10)
    hints = build_practical_runtime_hints(report)
    flagged_rates = {item["context"]["rate_id"] for item in hints["practical_hard_errors"]}
    assert report["rates"][-1]["rate_id"] == 9
    assert flagged_rates == set(range(12))
