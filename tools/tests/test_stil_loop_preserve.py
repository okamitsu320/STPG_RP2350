from __future__ import annotations

import json
from pathlib import Path

import pytest
from stpg.client import FailLogEntry
from stpg.constants import STPG_ACTIVE_PINS_V11, STPG_RATE_SLOT_SAMPLE
from stpg.pattern import LoopRefRecord, VectorRecord, unpack_rate_vector_full
from stil.ast import LoopDecl
from stil.markers import parse_marker_specs
from stil.map_lookup import source_map_entry_for_occurrence
from stil.parser import parse_stil_file
from stil.pipeline import CompileOptions, compile_stil
from stil.report import render_fail_report_text
from stil.runtime_wait_model import iter_runtime_events
from stil.segmented_package import (
    SegmentedPackageError,
    build_segment_ranges,
    build_segmented_packages,
    plan_capacity_aware_segment_ranges,
)
from stil.wait_binary import apply_wait_schedule_to_pattern
from stil.wait_acceptance import evaluate_wait_schedule
from stil.wait_schedule import build_wait_schedule, propose_wait_schedule


LOOP_STIL = """Header { Title \"loop-preserve\"; }
Signals {
  D0 InOut;
}
SignalGroups {
  ALL = 'D0';
}
Timing {
  WaveformTable WFT1 {
    Period '100ns';
    Waveforms {
      D0 { P: 0ns Drive(0); N: 0ns Drive(1); }
    }
  }
}
Pattern MAIN {
  W WFT1;
  V { ALL = P; }
  Loop 4 {
    V { ALL = P; }
    V { ALL = N; }
  }
}
"""


LOOP_ONE_VECTOR_STIL = LOOP_STIL.replace(
    "Loop 4 {\n    V { ALL = P; }\n    V { ALL = N; }\n  }",
    "Loop 70000 {\n    V { ALL = P; }\n  }",
)


LOOP_COMPARE_MASK_STIL = """Header { Title "preserved-loop-compare-mask"; }
Signals {
  clk InOut;
  dout_0 InOut;
  dout_1 InOut;
  dout_2 InOut;
}
SignalGroups {
  ALL = 'clk + dout_0 + dout_1 + dout_2';
}
Timing {
  WaveformTable WFT1 {
    Period '200ns';
    Waveforms {
      clk { 0: 0ns Drive(0); 1: 100ns Drive(1); }
      dout_0 { 0: 0ns Drive(0); 1: 0ns Drive(1); L: 0ns Compare(L); H: 0ns Compare(H); T: 0ns Compare(X); }
      dout_1 { 0: 0ns Drive(0); 1: 0ns Drive(1); L: 0ns Compare(L); H: 0ns Compare(H); T: 0ns Compare(X); }
      dout_2 { 0: 0ns Drive(0); 1: 0ns Drive(1); L: 0ns Compare(L); H: 0ns Compare(H); T: 0ns Compare(X); }
    }
  }
}
Pattern MAIN {
  W WFT1;
  V { ALL = 0TTT; }
  Loop 2 {
    V { ALL = 0TTT; }
    V { ALL = 1LHL; }
  }
}
"""


def test_parser_preserve_retains_loop_without_expanding(tmp_path: Path) -> None:
    source = tmp_path / "loop.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")

    expanded = parse_stil_file(source)
    preserved = parse_stil_file(source, loop_policy="preserve")

    assert len(expanded.patterns[0].vectors) == 9
    assert preserved.patterns[0].vectors == ()
    assert len(preserved.patterns[0].items) == 2
    assert isinstance(preserved.patterns[0].items[1], LoopDecl)
    assert preserved.patterns[0].items[1].loop_count == 4
    assert len(preserved.patterns[0].items[1].items) == 2


def test_rate_vector_preserve_emits_loop_ref_and_logical_count(tmp_path: Path) -> None:
    source = tmp_path / "loop.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")

    payload, map_data, report = compile_stil(
        CompileOptions(input_path=source, mode="rate-vector", loop_policy="preserve")
    )
    header, rates, slots, entries, loop_blocks = unpack_rate_vector_full(payload)

    assert header.vector_count == 2
    assert len(rates) == 1
    assert len(slots) == 1
    assert len(entries) == 2
    assert isinstance(entries[1], LoopRefRecord)
    assert len(loop_blocks) == 1
    assert loop_blocks[0].loop_count == 4
    assert report["loop_preserved"] is True
    assert report["logical_vector_count"] == 9
    assert map_data["vectors"]["1"]["top_level_kind"] == "loop"
    assert map_data["vectors"]["1"]["loop_count"] == 4
    assert map_data["vectors"]["1"]["body_source_map"][0]["body_record_index"] == 0

    events = list(iter_runtime_events(payload))
    assert events[0].loop_id is None
    assert events[0].loop_ref_occurrence_index is None
    assert events[1].loop_id == 0
    assert events[1].loop_ref_occurrence_index == 1


def test_preserved_loop_compare_mask_covers_low_and_mutated_high_bits(tmp_path: Path) -> None:
    """H/L are both cared bits; changing an in-loop expectation changes value only."""
    pin_map = Path(__file__).parent / "fixtures" / "vcd" / "vf_c10lp_pinmap_24.json"
    variants = []
    for name, final_row in (("clean", "1LHL"), ("injected", "1LHH")):
        source = tmp_path / f"{name}.stil"
        source.write_text(LOOP_COMPARE_MASK_STIL.replace("1LHL", final_row), encoding="utf-8")
        payload, _map_data, _report = compile_stil(
            CompileOptions(
                input_path=source,
                mode="rate-vector",
                loop_policy="preserve",
                pin_map_path=pin_map,
            )
        )
        header, rates, slots, entries, loops = unpack_rate_vector_full(payload)
        assert header.active_pins == STPG_ACTIVE_PINS_V11
        assert len(loops) == 1 and loops[0].loop_count == 2
        assert len(loops[0].body_records) == 2
        compare_record = loops[0].body_records[-1]
        rate = rates[compare_record.rate_id]
        rate_slots = slots[rate.slot_offset : rate.slot_offset + rate.slot_count]
        variants.append((payload, slots, entries, loops, compare_record, rate_slots))

    clean, injected = variants
    clean_record, injected_record = clean[4], injected[4]
    expected_mask = (1 << 17) | (1 << 18) | (1 << 19)
    injected_bit = 1 << 19
    assert clean_record.expect_value == 1 << 18
    assert injected_record.expect_value == (1 << 18) | injected_bit
    assert clean_record.expect_mask == injected_record.expect_mask == expected_mask
    assert (clean_record.expect_mask & injected_bit) != 0
    for rate_slots in (clean[5], injected[5]):
        assert any(
            (slot.flags & STPG_RATE_SLOT_SAMPLE) != 0 and slot.sample_mask == expected_mask
            for slot in rate_slots
        )
    assert clean[1] == injected[1]
    assert clean[2] == injected[2]
    assert clean[3][0].body_records[:-1] == injected[3][0].body_records[:-1]


def test_fail_report_maps_runtime_event_to_preserved_loop_source(tmp_path: Path) -> None:
    source = tmp_path / "loop-report.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")
    payload, map_data, _report = compile_stil(
        CompileOptions(input_path=source, mode="rate-vector", loop_policy="preserve")
    )

    # Runtime event 2 is the second body record of the first loop iteration.
    # It must not be looked up as map key "2": the only top-level map keys are
    # source records "0" and "1".
    text = render_fail_report_text(
        [FailLogEntry(event_index=2, actual=0, expected=1, mask=1)],
        map_data,
        pattern_bytes=payload,
    )

    assert "event=2" in text
    assert "vector=2 local_vector=1" in text
    assert "loop=0 loop_iteration=0 body_record=1 body_repeat=0" in text
    assert "line=21" in text


def test_preserved_loop_auto_mode_does_not_fallback_to_flat_raw_events(tmp_path: Path) -> None:
    source = tmp_path / "nested-loop.stil"
    source.write_text(
        LOOP_STIL.replace(
            "Loop 4 {\n    V { ALL = P; }\n    V { ALL = N; }\n  }",
            "Loop 2 {\n    V { ALL = P; }\n    Loop 2 {\n      V { ALL = N; }\n    }\n  }",
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception) as error:
        compile_stil(
            CompileOptions(input_path=source, mode="auto", loop_policy="preserve")
        )
    assert getattr(error.value, "code", None) == "ERR_NESTED_LOOP_UNSUPPORTED"


def test_one_vector_loop_splits_repeat_records_and_uses_compressed_map(tmp_path: Path) -> None:
    source = tmp_path / "loop-one-vector.stil"
    source.write_text(LOOP_ONE_VECTOR_STIL, encoding="utf-8")

    payload, map_data, report = compile_stil(
        CompileOptions(input_path=source, mode="rate-vector", loop_policy="preserve")
    )
    header, _rates, _slots, entries, loop_blocks = unpack_rate_vector_full(payload)

    assert header.vector_count == 3
    assert loop_blocks == []
    assert [entry.repeat for entry in entries if isinstance(entry, VectorRecord)] == [1, 0xFFFF, 4465]
    assert report["logical_vector_count"] == 70001
    first_chunk = map_data["vectors"]["1"]
    second_chunk = map_data["vectors"]["2"]
    assert first_chunk["source_map_compression"]["kind"] == "loop_vector_repeat_v1"
    assert source_map_entry_for_occurrence(first_chunk, repeat_index=12)["global_vector_index"] == 13
    assert source_map_entry_for_occurrence(second_chunk, repeat_index=0)["global_vector_index"] == 65536
    assert source_map_entry_for_occurrence(second_chunk, repeat_index=4464)["global_vector_index"] == 70000


def test_long_preserved_loop_package_verifies_with_logical_map_reader(tmp_path: Path) -> None:
    source = tmp_path / "loop-one-vector.stil"
    source.write_text(LOOP_ONE_VECTOR_STIL, encoding="utf-8")
    output = tmp_path / "package"

    from stil.pattern_package import PatternPackageOptions, build_pattern_package, verify_pattern_package

    built = build_pattern_package(
        PatternPackageOptions(
            input_path=source,
            output_dir=output,
            mode="rate-vector",
            loop_policy="preserve",
        )
    )

    assert built["status"] == "VERIFIED"
    from stil.json_artifacts import read_json_artifact

    assert isinstance(read_json_artifact(output / "pattern.map.json")["vectors"], dict)
    verified = verify_pattern_package(output)
    assert verified["status"] == "PASS", verified["errors"]


def test_package_preserve_records_loop_policy(tmp_path: Path) -> None:
    source = tmp_path / "loop.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")
    output = tmp_path / "package"

    from stil.pattern_package import PatternPackageOptions, build_pattern_package

    result = build_pattern_package(
        PatternPackageOptions(
            input_path=source,
            output_dir=output,
            mode="rate-vector",
            loop_policy="preserve",
        )
    )

    assert result["status"] == "VERIFIED"
    from stil.json_artifacts import read_json_artifact

    metadata = read_json_artifact(output / "pattern.metadata.json")
    assert metadata["options"]["loop_policy"] == "preserve"


def test_wait_application_splits_only_target_loop_iteration(tmp_path: Path) -> None:
    source = tmp_path / "loop.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")
    pattern_path = tmp_path / "pattern.bin"
    map_path = tmp_path / "pattern.map.json"
    payload, map_data, _report = compile_stil(
        CompileOptions(input_path=source, mode="rate-vector", loop_policy="preserve")
    )
    pattern_path.write_bytes(payload)
    map_path.write_text(json.dumps(map_data), encoding="utf-8")
    schedule = build_wait_schedule(
        pattern_path,
        map_path=map_path,
        chunk_event_capacity=1,
        ring_slot_count=2,
    )
    # The boundary before event 2 targets loop iteration 0, body record 1.
    schedule["boundaries"][2]["wait_ticks"] = 100
    schedule["boundaries"][2]["wait_us"] = 100 * 1_000_000.0 / 150_000_000

    patched, patched_map, patch_manifest = apply_wait_schedule_to_pattern(
        payload,
        schedule=schedule,
        map_data=map_data,
        max_rates=64,
    )
    _header, _rates, _slots, entries, loops = unpack_rate_vector_full(patched)

    assert len(list(iter_runtime_events(patched))) == 9
    assert patch_manifest["applied_boundaries"][0]["target_loop_iteration"] == 0
    assert any(isinstance(entry, LoopRefRecord) for entry in entries)
    assert any(isinstance(entry, VectorRecord) for entry in entries)
    # The original block remains in the binary as immutable context; the
    # suffix gets one split block and the target iteration is plain records.
    assert len(loops) == 2
    assert any(item["top_level_kind"] == "loop_iteration" for item in patched_map["vectors"].values())
    suffix = next(
        item
        for item in patched_map["vectors"].values()
        if item.get("top_level_kind") == "loop"
        and item.get("loop_iteration_start") == 1
    )
    suffix_source = source_map_entry_for_occurrence(
        suffix,
        loop_iteration=0,
        body_record_index=0,
        body_repeat_index=0,
    )
    assert suffix_source["global_vector_index"] == 3


def test_non_loop_service_profile_is_rejected_for_loop_artifact(tmp_path: Path) -> None:
    source = tmp_path / "loop.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")
    pattern_path = tmp_path / "pattern.bin"
    map_path = tmp_path / "pattern.map.json"
    payload, map_data, _report = compile_stil(
        CompileOptions(input_path=source, mode="rate-vector", loop_policy="preserve")
    )
    pattern_path.write_bytes(payload)
    map_path.write_text(json.dumps(map_data), encoding="utf-8")
    schedule_path = tmp_path / "wait.json"
    schedule_path.write_text(
        json.dumps(build_wait_schedule(pattern_path, map_path=map_path, chunk_event_capacity=1)),
        encoding="utf-8",
    )

    rejected = evaluate_wait_schedule(
        pattern_path,
        schedule_path,
        map_path=map_path,
        service_time_us=2000,
        service_time_source="non-loop bounded profile",
    )
    assert rejected["status"] == "REJECTED"
    assert "loop-generic" in rejected["errors"][0]


def test_wait_proposal_detects_loop_provenance_from_compressed_repeat_map(tmp_path: Path) -> None:
    source = tmp_path / "loop-one-vector.stil"
    source.write_text(LOOP_ONE_VECTOR_STIL, encoding="utf-8")
    pattern_path = tmp_path / "pattern.bin"
    map_path = tmp_path / "pattern.map.json"
    payload, map_data, _report = compile_stil(
        CompileOptions(input_path=source, mode="rate-vector", loop_policy="preserve")
    )
    pattern_path.write_bytes(payload)
    map_path.write_text(json.dumps(map_data), encoding="utf-8")
    schedule_path = tmp_path / "wait.json"
    proposal, model = propose_wait_schedule(
        pattern_path,
        map_path=map_path,
        service_time_us=2000,
        service_time_source="bounded loop-generic profile",
        service_profile_scope="loop-generic",
        max_wait_us=4000,
        max_total_wait_us=500000,
    )
    schedule_path.write_text(json.dumps(proposal), encoding="utf-8")

    assert proposal["policy"]["service_profile_scope"] == "loop-generic"
    assert model["wait_plan"]["service_profile_scope"] == "loop-generic"


def test_segmented_loop_wait_plan_requires_explicit_loop_profile(tmp_path: Path) -> None:
    source = tmp_path / "loop.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")
    ranges = build_segment_ranges(9, segment_vector_count=4)
    common = {
        "input_path": source,
        "ranges": ranges,
        "total_vectors": 9,
        "mode": "rate-vector",
        "loop_policy": "preserve",
        "chunk_event_capacity": 1024,
        "ring_slot_count": 6,
        "segment_wait_plan": "every-boundary",
        "service_time_us": 2000,
        "service_time_source": "bounded loop profile",
        "max_wait_us": 4000,
        "max_total_wait_us": 500000,
    }

    with pytest.raises(SegmentedPackageError, match="loop-generic"):
        build_segmented_packages(
            **common,
            output_dir=tmp_path / "segments-non-loop",
            report_path=tmp_path / "segments-non-loop.json",
        )

    report = build_segmented_packages(
        **common,
        output_dir=tmp_path / "segments-loop",
        report_path=tmp_path / "segments-loop.json",
        service_profile_scope="loop-generic",
    )
    assert report["status"] == "COMPLETE"
    manifest = json.loads(
        (
            tmp_path
            / "segments-loop"
            / "nominal"
            / "segmented.manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["policies"]["service_profile_scope"] == "loop-generic"


def test_preserved_loop_marker_requires_and_uses_line_occurrence(tmp_path: Path) -> None:
    source = tmp_path / "loop.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 16,
                "signals": {
                    "D0": {"bit": 0, "role": "signal"},
                    "__MARK0": {"bit": 2, "role": "marker"},
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception) as ambiguous:
        compile_stil(
            CompileOptions(
                input_path=source,
                mode="rate-vector",
                pin_map_path=pin_map,
                loop_policy="preserve",
                markers=parse_marker_specs(["__MARK0:line=20:mode=pulse"], []),
            )
        )
    assert getattr(ambiguous.value, "code", None) == "ERR_MARKER_LINE_AMBIGUOUS"

    _payload, map_data, report = compile_stil(
        CompileOptions(
            input_path=source,
            mode="rate-vector",
            pin_map_path=pin_map,
            loop_policy="preserve",
            markers=parse_marker_specs(["__MARK0:line=20:occurrence=2:mode=pulse"], []),
        )
    )
    assert report["markers"][0]["occurrence"] == 2
    assert report["markers"][0]["vector"] == 5
    assert map_data["markers"][0]["address_kind"] == "stil_line"
    assert any(
        event.get("global_vector_index") == 5
        for payload in map_data["vectors"].values()
        for event in payload.get("marker_events", [])
    )


def test_preserved_loop_segmentation_uses_logical_ranges(tmp_path: Path) -> None:
    source = tmp_path / "loop.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")
    output = tmp_path / "segments"
    ranges = build_segment_ranges(9, segment_vector_count=4)
    report = build_segmented_packages(
        input_path=source,
        output_dir=output,
        report_path=tmp_path / "segmented.json",
        ranges=ranges,
        total_vectors=9,
        mode="rate-vector",
        loop_policy="preserve",
    )
    assert report["status"] == "COMPLETE"
    assert report["source_vector_count"] == 9
    assert report["segment_ranges"] == [[0, 3], [4, 7], [8, 8]]
    segment_records = report["variants"][0]["segments"]
    assert [record["global_vector_start"] for record in segment_records] == [0, 4, 8]
    assert all(record["physical_execution_authorized"] is False for record in segment_records)


def test_sliced_preserved_loop_map_keeps_global_logical_base(tmp_path: Path) -> None:
    source = tmp_path / "loop.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")

    _payload, map_data, _report = compile_stil(
        CompileOptions(
            input_path=source,
            mode="rate-vector",
            loop_policy="preserve",
            vector_range=(3, 6),
        )
    )
    loop_payload = map_data["vectors"]["0"]
    assert loop_payload["top_level_kind"] == "loop"
    assert loop_payload["logical_vector_base"] == 3
    first = source_map_entry_for_occurrence(
        loop_payload,
        loop_iteration=0,
        body_record_index=0,
        body_repeat_index=0,
    )
    second = source_map_entry_for_occurrence(
        loop_payload,
        loop_iteration=1,
        body_record_index=0,
        body_repeat_index=0,
    )
    assert first["global_vector_index"] == 3
    assert second["global_vector_index"] == 5
    assert first["loop_iteration"] == 1
    assert second["loop_iteration"] == 2


def test_capacity_aware_segmentation_rechecks_compiled_ranges(tmp_path: Path) -> None:
    source = tmp_path / "loop.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")

    ranges, plan = plan_capacity_aware_segment_ranges(
        input_path=source,
        total_vectors=9,
        max_segment_vector_count=4,
        mode="rate-vector",
        tick_hz=150_000_000,
        pin_map_path=None,
        pattern_burst=None,
        pattern=None,
        strict=False,
        allow_approx=False,
        prefer_raw=False,
        runtime_profile="current",
        max_rates=None,
        loop_policy="preserve",
    )
    assert ranges == ((0, 3), (4, 7), (8, 8))
    assert plan["status"] == "PLANNED"
    assert plan["probe_count"] >= 3
    assert all(item["metrics"]["fit"] for item in plan["decisions"])


def test_segmented_preserved_marker_uses_global_vector_address(tmp_path: Path) -> None:
    source = tmp_path / "loop.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 16,
                "signals": {
                    "D0": {"bit": 0, "role": "signal"},
                    "__MARK0": {"bit": 2, "role": "marker"},
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "segments"
    report = build_segmented_packages(
        input_path=source,
        output_dir=output,
        report_path=tmp_path / "segmented.json",
        ranges=build_segment_ranges(9, segment_vector_count=4),
        total_vectors=9,
        mode="rate-vector",
        pin_map_path=pin_map,
        loop_policy="preserve",
        markers=parse_marker_specs(["__MARK0:line=20:occurrence=2:mode=pulse"], []),
    )
    assert report["status"] == "COMPLETE"
    marked_segment = output / "nominal" / "segment-0001" / "package" / "pattern.map.json"
    from stil.json_artifacts import read_json_artifact

    marked_map = read_json_artifact(marked_segment)
    assert any(
        event.get("global_vector_index") == 5
        for payload in marked_map["vectors"].values()
        for event in payload.get("marker_events", [])
    )
