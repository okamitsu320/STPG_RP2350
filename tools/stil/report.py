from __future__ import annotations

from bisect import bisect_right
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any
import argparse

from stpg.client import FailLogEntry
from stpg.constants import STPG_HEADER_SIZE, STPG_PATTERN_MAGIC
from stpg.pattern import LoopRefRecord, PatternHeader, VectorRecord, unpack_rate_vector_full, validate_pattern

from .source_analysis import build_source_analysis_report
from .map_lookup import source_map_entry_for_occurrence
from .runtime_wait_model import RuntimeEvent, iter_runtime_events
from stpg.constants import STPG_DEFAULT_TICK_HZ
from .json_artifacts import read_json_artifact


def build_compile_report(
    ir,
    *,
    mode_requested: str,
    mode_emitted: str,
    rate_count: int = 0,
    slot_count: int = 0,
    vector_count: int | None = None,
    fallback_reasons: list[str] | None = None,
    requested_time_scale: str = "1.0",
    tick_hz: int = STPG_DEFAULT_TICK_HZ,
    markers: list[dict[str, Any]] | None = None,
    terminal_drive_value: int = 0,
    terminal_drive_enable: int = 0,
    micro_op_family_analysis: dict[str, Any] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    runtime_precheck: dict[str, Any] | None = None,
    pin_map_path: Path | None = None,
    structure_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    source_vector_count = len(ir.vectors)
    logical_vector_count = int(getattr(ir, "logical_vector_count", 0) or source_vector_count)
    return {
        "input_file": ir.resolved_pattern.document.source_file,
        "mode_requested": mode_requested,
        "mode_emitted": mode_emitted,
        "requested_time_scale": requested_time_scale,
        "exact_count": logical_vector_count if not ir.approximate_reasons else 0,
        "approximate_count": logical_vector_count if ir.approximate_reasons else 0,
        "fallback_count": 1 if mode_requested == "auto" and mode_emitted == "raw-event" else 0,
        "reject_count": 0,
        "rate_count": rate_count,
        "slot_count": slot_count,
        "vector_count": logical_vector_count if vector_count is None else vector_count,
        "source_vector_count": source_vector_count,
        "logical_vector_count": logical_vector_count,
        "loop_preserved": bool(getattr(ir, "loop_preserved", False)),
        "structure_metrics": structure_metrics or {},
        "warnings": list(ir.warnings),
        "fallback_reasons": fallback_reasons or [],
        "approximation_reasons": list(ir.approximate_reasons),
        "approximations": [
            {
                "kind": item.kind,
                "original_time": item.original_time,
                "emitted_time_ticks": item.emitted_time_ticks,
                "emitted_time_ns": item.emitted_time_ns,
                "delta_ticks": item.delta_ticks,
                "delta_ns": item.delta_ns,
                "affected_signal": item.affected_signal,
                "affected_vector_index": item.affected_vector_index,
                "stil_line": item.stil_line,
                "waveform_table_name": item.waveform_table_name,
            }
            for item in ir.approximations
        ],
        "markers": markers or [],
        "terminal_drive_value": terminal_drive_value,
        "terminal_drive_enable": terminal_drive_enable,
        "micro_op_family_analysis": micro_op_family_analysis or {},
        "normalized_transition_count": ir.resolved_pattern.normalized_transition_count,
        "normalized_transition_summary": list(ir.resolved_pattern.normalized_transition_summary),
        "source_analysis": build_source_analysis_report(
            ir,
            mode_requested=mode_requested,
            mode_emitted=mode_emitted,
            tick_hz=tick_hz,
            time_scale=Decimal(requested_time_scale),
            pin_map_path=pin_map_path,
            include_backend_candidates=False,
            _micro_op_family_analysis=micro_op_family_analysis,
        ),
        "capability_precheck": {
            "input_signal_count": len(ir.resolved_pattern.document.signals),
            "mapped_signal_count": len(ir.resolved_pattern.pin_map.signals),
            "active_pins": ir.resolved_pattern.pin_map.active_pins,
            "max_assigned_bit": max((assignment.bit for assignment in ir.resolved_pattern.pin_map.signals.values()), default=-1),
            "resolved_vectors": source_vector_count,
            "logical_vectors": logical_vector_count,
            "emitted_vectors": logical_vector_count if vector_count is None else vector_count,
            "mode_emitted": mode_emitted,
            "tick_hz": tick_hz,
            "selected_pattern_burst": ir.resolved_pattern.selected_pattern_burst,
            "selected_pattern_name": ir.resolved_pattern.selected_pattern_name,
            "normalized_transition_count": ir.resolved_pattern.normalized_transition_count,
            "approximate": bool(ir.approximate_reasons),
            "runtime_precheck": runtime_precheck or {},
        },
        "diagnostics": diagnostics or [],
    }


def render_fail_report(
    fail_json_path: str | Path,
    map_json_path: str | Path,
    *,
    pattern_path: str | Path | None = None,
) -> str:
    fail_data = read_json_artifact(Path(fail_json_path))
    map_data = read_json_artifact(Path(map_json_path))
    entries = []
    for item in fail_data.get("entries", []):
        normalized = {
            "vector_index": item.get("vector_index", item.get("event_index", 0)),
            "repeat_iteration": item.get("repeat_iteration", 0),
            "slot_index": item.get("slot_index", 0),
            "sample_value": item.get("sample_value", item.get("actual", 0)),
            "expect_value": item.get("expect_value", item.get("expected", 0)),
            "effective_mask": item.get("effective_mask", item.get("mask", 0)),
            "fail_bits": item.get(
                "fail_bits",
                (
                    item.get("sample_value", item.get("actual", 0))
                    ^ item.get("expect_value", item.get("expected", 0))
                )
                & item.get("effective_mask", item.get("mask", 0)),
            ),
        }
        # A transport/segmented result uses event_index.  Keep vector_index
        # as a legacy source-map index only when the file has no event index.
        if "event_index" in item:
            normalized["runtime_event_index"] = int(item["event_index"])
        if "resolved_source_map" in item:
            normalized["resolved_source_map"] = item["resolved_source_map"]
            normalized["source_record_index"] = item["source_record_index"]
            normalized["group_index"] = item["group_index"]
            normalized["segment_id"] = item["segment_id"]
        entries.append(normalized)
    pattern_bytes = Path(pattern_path).read_bytes() if pattern_path is not None else None
    return render_fail_entries(entries, map_data, pattern_bytes=pattern_bytes)


def render_fail_report_text(
    entries: list[FailLogEntry],
    map_data: dict[str, Any],
    *,
    pattern_bytes: bytes | None = None,
) -> str:
    normalized = []
    for entry in entries:
        normalized.append(
            {
                # Keep the runtime event separate from the legacy
                # source-record ``vector_index``.  The two are not equal for
                # rate groups, folded repeats, or preserved loops.
                "vector_index": entry.event_index,
                "runtime_event_index": entry.event_index,
                "repeat_iteration": 0,
                "slot_index": 0,
                "sample_value": entry.actual,
                "expect_value": entry.expected,
                "effective_mask": entry.mask,
                "fail_bits": (entry.actual ^ entry.expected) & entry.mask,
            }
        )
    return render_fail_entries(normalized, map_data, pattern_bytes=pattern_bytes)


def describe_fail_sources(entries: list[FailLogEntry], map_data: dict[str, Any], *,
                          pattern_bytes: bytes) -> list[dict[str, Any]]:
    """Structured compiler mapping for consumers; no input_file/path metadata is returned.

    Event numbers are runtime event numbers, vectors are zero-based global STIL
    vector occurrences, and STIL lines are one-based. Resolve repeats, loop
    iterations and rate groups with the same implementation as text reports.
    """
    indices = {entry.event_index for entry in entries}
    raw = map_data.get("mode_emitted") == "raw-event"
    events = _raw_event_lookup(pattern_bytes, map_data, indices) if raw else _runtime_event_lookup(pattern_bytes, indices)
    signals = map_data.get("signal_map", {})
    result = []
    for entry in entries:
        event = events[entry.event_index]
        if raw:
            source, record = event["source_map"], event["source_record_index"]
        else:
            record, source = _source_map_for_runtime_event(event, map_data)
        fail_bits = (entry.actual ^ entry.expected) & entry.mask
        mapped = [{"name": name, "bit": value["bit"],
                   "expected": (entry.expected >> value["bit"]) & 1,
                   "actual": (entry.actual >> value["bit"]) & 1}
                  for name, value in signals.items() if fail_bits & (1 << value["bit"])]
        result.append({"event_index": entry.event_index, "source_record_index": record,
                       "actual": entry.actual, "expected": entry.expected, "mask": entry.mask,
                       "fail_bits": fail_bits, "source": _public_source(source), "signals": mapped,
                       "unmapped_fail_bits": fail_bits & ~sum({1 << item["bit"] for item in mapped})})
    return result


def _public_source(source: dict[str, Any]) -> dict[str, Any]:
    result = {key: source.get(key) for key in (
        "global_vector_index", "stil_line", "pattern_name", "label", "waveform_table",
        "loop_id", "loop_iteration", "loop_iteration_index", "repeat_iteration",
    )}
    if (type(result["global_vector_index"]) is not int or result["global_vector_index"] < 0
            or type(result["stil_line"]) is not int or result["stil_line"] < 1
            or any(value is not None and type(value) not in (int, str) for value in result.values())):
        raise ValueError("invalid source occurrence fields")
    return result


def describe_source_span(map_data: dict[str, Any], *, pattern_bytes: bytes) -> dict[str, Any]:
    """First/last compiled source occurrences without expanding whole preserved loops."""
    if map_data.get("mode_emitted") == "raw-event":
        count = map_data["raw_event_count"]
        edges = _raw_event_lookup(pattern_bytes, map_data, {0, count - 1})
        return {"first": _public_source(edges[0]["source_map"]),
                "last": _public_source(edges[count - 1]["source_map"])}
    _, _, _, vectors, loops = unpack_rate_vector_full(pattern_bytes)
    logical = 0
    edges = []
    for index, vector in enumerate(vectors):
        if isinstance(vector, VectorRecord):
            size = vector.repeat
            first = RuntimeEvent(0, logical, index, 0, vector.rate_id, 0, 0)
            last = RuntimeEvent(0, logical + size - 1, index, size - 1, vector.rate_id, 0, 0)
        elif isinstance(vector, LoopRefRecord):
            loop = loops[vector.loop_id]
            size = loop.loop_count * sum(record.repeat for record in loop.body_records)
            end = loop.body_records[-1]
            first = RuntimeEvent(0, logical, index, 0, loop.body_records[0].rate_id, 0, 0,
                                 vector.loop_id, 0, 0, 0)
            last = RuntimeEvent(0, logical + size - 1, index, end.repeat - 1, end.rate_id, 0, 0,
                                vector.loop_id, loop.loop_count - 1, len(loop.body_records) - 1, end.repeat - 1)
        else:
            raise ValueError("unsupported source span record")
        if index == 0:
            edges.append(first)
        if index == len(vectors) - 1:
            edges.append(last)
        logical += size
    if not edges:
        raise ValueError("empty source span")
    return {"first": _public_source(_source_map_for_runtime_event(edges[0], map_data)[1]),
            "last": _public_source(_source_map_for_runtime_event(edges[-1], map_data)[1])}


def _hex_width(*values: int) -> int:
    return 6 if any(value > 0xFFFF for value in values) else 4


def _runtime_event_lookup(
    pattern_bytes: bytes,
    event_indices: set[int],
) -> dict[int, RuntimeEvent]:
    """Resolve transport event indices using the executed rate-vector binary.

    The firmware FAIL_LOG contains runtime event indices, not source-record
    indices.  Iterate only through the largest requested event and stop as
    soon as all requested entries have been found.  This preserves the exact
    repeat/loop/rate-group ordering without materialising a full map.
    """

    if not event_indices:
        return {}
    if any(index < 0 for index in event_indices):
        raise ValueError("FAIL_LOG event_index must be non-negative")
    resolved: dict[int, RuntimeEvent] = {}
    max_event_index = max(event_indices)
    try:
        for event in iter_runtime_events(pattern_bytes, max_events=max_event_index + 1):
            if event.event_index in event_indices:
                resolved[event.event_index] = event
                if len(resolved) == len(event_indices):
                    break
    except ValueError as exc:
        raise ValueError(
            "cannot reconstruct runtime event order from the executed pattern: "
            f"{exc}"
        ) from exc
    missing = sorted(event_indices - resolved.keys())
    if missing:
        raise ValueError(
            "FAIL_LOG event_index is outside the executed pattern event range: "
            + ", ".join(str(index) for index in missing)
        )
    return resolved


def _source_map_for_runtime_event(
    event: RuntimeEvent,
    map_data: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    vectors = map_data.get("vectors")
    if not isinstance(vectors, dict):
        raise ValueError("pattern map has no vectors object")
    source_record_index = int(event.source_record_index)
    vector_map = vectors.get(str(source_record_index))
    if not isinstance(vector_map, dict):
        raise ValueError(
            "pattern map has no source record for runtime event "
            f"{event.event_index}: source_record_index={source_record_index}"
        )
    source_map = source_map_entry_for_occurrence(
        vector_map,
        repeat_index=int(event.repeat_index),
        loop_iteration=event.loop_iteration,
        body_record_index=event.body_record_index,
        body_repeat_index=event.body_repeat_index,
        logical_vector_index=event.logical_vector_index,
    )
    if not source_map:
        raise ValueError(
            "pattern map cannot resolve runtime event "
            f"{event.event_index}: source_record_index={source_record_index}"
        )
    return source_record_index, source_map


def _raw_event_lookup(
    pattern_bytes: bytes | None,
    map_data: dict[str, Any],
    event_indices: set[int],
) -> dict[int, dict[str, Any]]:
    """Resolve raw runtime event indexes through compiler-emitted source ranges."""

    if not event_indices:
        return {}
    if pattern_bytes is None:
        raise ValueError("raw-event FAIL_LOG mapping requires the exact executed pattern.bin")
    if any(index < 0 for index in event_indices):
        raise ValueError("FAIL_LOG event_index must be non-negative")
    ranges = map_data.get("raw_event_ranges")
    if not isinstance(ranges, list):
        raise ValueError("raw-event map has no raw_event_ranges table")
    declared_count = map_data.get("raw_event_count")
    if isinstance(declared_count, bool) or not isinstance(declared_count, int) or declared_count < 0:
        raise ValueError("raw-event map has no valid raw_event_count")

    starts: list[int] = []
    validated_ranges: list[dict[str, Any]] = []
    cursor = 0
    vectors = map_data.get("vectors")
    if not isinstance(vectors, dict):
        raise ValueError("raw-event map has no vectors object")
    for item in ranges:
        if not isinstance(item, dict):
            raise ValueError("raw-event map contains a malformed event range")
        start = item.get("event_start")
        end = item.get("event_end_exclusive")
        phase_times = item.get("phase_times_ticks")
        source_record_index = item.get("source_record_index")
        logical_vector_index = item.get("logical_vector_index")
        phase_times_valid = (
            isinstance(phase_times, list)
            and all(
                not isinstance(time_ticks, bool)
                and isinstance(time_ticks, int)
                and time_ticks >= 0
                and (index == 0 or time_ticks > phase_times[index - 1])
                for index, time_ticks in enumerate(phase_times)
            )
        )
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or isinstance(source_record_index, bool)
            or not isinstance(source_record_index, int)
            or isinstance(logical_vector_index, bool)
            or not isinstance(logical_vector_index, int)
            or not phase_times_valid
            or start != cursor
            or end <= start
            or end - start != len(phase_times)
            or str(source_record_index) not in vectors
        ):
            raise ValueError("raw-event map contains a gap, overlap, or malformed source range")
        starts.append(start)
        validated_ranges.append(item)
        cursor = end
    if cursor != declared_count:
        raise ValueError("raw-event map range total does not match raw_event_count")
    if len(pattern_bytes) < STPG_HEADER_SIZE:
        raise ValueError("raw-event pattern is shorter than its header")
    try:
        validate_pattern(pattern_bytes)
        header = PatternHeader.unpack(pattern_bytes[:STPG_HEADER_SIZE])
    except ValueError as exc:
        raise ValueError(f"cannot validate raw-event pattern: {exc}") from exc
    if header.magic != STPG_PATTERN_MAGIC:
        raise ValueError("supplied pattern is not a Raw Event pattern")
    if header.event_count != declared_count:
        raise ValueError(
            "raw-event map/pattern event count mismatch: "
            f"map={declared_count} pattern={header.event_count}"
        )
    expected_pattern_sha256 = map_data.get("pattern_sha256")
    actual_pattern_sha256 = hashlib.sha256(pattern_bytes).hexdigest()
    if expected_pattern_sha256 != actual_pattern_sha256:
        raise ValueError("raw-event map is not bound to the supplied exact pattern.bin")

    resolved: dict[int, dict[str, Any]] = {}
    for event_index in event_indices:
        range_index = bisect_right(starts, event_index) - 1
        if range_index < 0:
            raise ValueError(f"FAIL_LOG event_index is outside the raw-event map: {event_index}")
        item = validated_ranges[range_index]
        if event_index >= int(item["event_end_exclusive"]):
            raise ValueError(f"FAIL_LOG event_index is outside the raw-event map: {event_index}")
        source_record_index = int(item["source_record_index"])
        vector_map = vectors[str(source_record_index)]
        source_map = source_map_entry_for_occurrence(vector_map, repeat_index=0)
        if not source_map:
            raise ValueError(f"raw-event map has no source provenance for event {event_index}")
        if int(source_map.get("global_vector_index", -1)) != int(item["logical_vector_index"]):
            raise ValueError(f"raw-event map source/logical vector mismatch for event {event_index}")
        phase_index = event_index - int(item["event_start"])
        resolved[event_index] = {
            "source_record_index": source_record_index,
            "source_map": source_map,
            "phase_index": phase_index,
            "phase_time_ticks": int(item["phase_times_ticks"][phase_index]),
            "logical_vector_index": int(item["logical_vector_index"]),
        }
    return resolved


def render_fail_entries(
    entries: list[dict[str, Any]],
    map_data: dict[str, Any],
    *,
    pattern_bytes: bytes | None = None,
) -> str:
    lines = []
    mode_emitted = map_data.get("mode_emitted")
    if mode_emitted == "raw-event":
        for entry_index, entry in enumerate(entries):
            event_index = entry.get("runtime_event_index")
            if (
                isinstance(event_index, bool)
                or not isinstance(event_index, int)
                or event_index < 0
            ):
                raise ValueError(
                    "raw-event FAIL_LOG entry is missing a valid runtime_event_index "
                    f"at entry {entry_index}"
                )
    signal_map = map_data.get("signal_map", {})
    bit_to_signal = {payload["bit"]: name for name, payload in signal_map.items()}
    runtime_indices = {
        int(entry["runtime_event_index"])
        for entry in entries
        if entry.get("runtime_event_index") is not None
    }
    runtime_events = (
        _runtime_event_lookup(pattern_bytes, runtime_indices)
        if pattern_bytes is not None
        and runtime_indices
        and mode_emitted == "rate-vector"
        and not map_data.get("segmented")
        else {}
    )
    raw_events = (
        _raw_event_lookup(pattern_bytes, map_data, runtime_indices)
        if runtime_indices and mode_emitted == "raw-event"
        else {}
    )
    for entry in entries:
        runtime_event = runtime_events.get(entry.get("runtime_event_index"))
        raw_event = raw_events.get(entry.get("runtime_event_index"))
        loop_context = ""
        if map_data.get("segmented") and entry.get("runtime_event_index") is not None:
            source_map = entry.get("resolved_source_map")
            if not isinstance(source_map, dict) or "stil_line" not in source_map:
                raise ValueError("segmented runtime events need resolved segment-local provenance; regenerate results or use stpg_manual_tool segmented")
            vector_index = int(entry["source_record_index"])
            repeat_iteration = 0
            loop_context = f" event={entry['runtime_event_index']} segment={entry['segment_id']} group={entry['group_index']}"
        elif runtime_event is not None:
            vector_index, source_map = _source_map_for_runtime_event(runtime_event, map_data)
            repeat_iteration = int(runtime_event.repeat_index)
            loop_context = (
                f" event={runtime_event.event_index} group={runtime_event.group_index}"
            )
            if runtime_event.loop_id is not None:
                loop_context += (
                    f" loop={runtime_event.loop_id}"
                    f" loop_iteration={runtime_event.loop_iteration}"
                    f" body_record={runtime_event.body_record_index}"
                    f" body_repeat={runtime_event.body_repeat_index}"
                )
        elif raw_event is not None:
            vector_index = int(raw_event["source_record_index"])
            source_map = raw_event["source_map"]
            repeat_iteration = 0
            loop_context = (
                f" event={entry['runtime_event_index']}"
                f" raw_phase={raw_event['phase_index']}"
                f" phase_tick={raw_event['phase_time_ticks']}"
            )
        else:
            vector_index = int(entry["vector_index"])
            repeat_iteration = int(entry.get("repeat_iteration", 0))
            vector_map = map_data["vectors"][str(vector_index)]
            source_map = source_map_entry_for_occurrence(
                vector_map,
                repeat_index=repeat_iteration,
                loop_iteration=entry.get("loop_iteration"),
                body_record_index=entry.get("body_record_index"),
                body_repeat_index=entry.get("body_repeat_index"),
            )
        if not source_map:
            source_map = {
                "global_vector_index": vector_index,
                "stil_line": "?",
                "pattern_name": "?",
                "waveform_table": "?",
            }
        fail_bits = entry.get("fail_bits", 0)
        signals = [name for bit, name in sorted(bit_to_signal.items()) if fail_bits & (1 << bit)]
        global_vector_index = source_map.get("global_vector_index", vector_index)
        vector_label = f"vector={global_vector_index}"
        if global_vector_index != vector_index:
            vector_label += f" local_vector={vector_index}"
        sample_value = entry.get("sample_value", 0)
        expect_value = entry.get("expect_value", 0)
        effective_mask = entry.get("effective_mask", 0)
        width = _hex_width(sample_value, expect_value, effective_mask, fail_bits)
        stil_line = source_map.get("stil_line", "?")
        pattern_name = source_map.get("pattern_name", "?")
        waveform_table = source_map.get("waveform_table", "?")
        lines.append(
            f"{vector_label}{loop_context} repeat={repeat_iteration} line={stil_line} "
            f"label={source_map.get('label')} pattern={pattern_name} "
            f"wft={waveform_table} signals={signals} "
            f"sample=0x{sample_value:0{width}X} expect=0x{expect_value:0{width}X} "
            f"mask=0x{effective_mask:0{width}X} fail_bits=0x{fail_bits:0{width}X}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a fail report using STIL map metadata")
    parser.add_argument("fail_json")
    parser.add_argument("--map", required=True, dest="map_json")
    parser.add_argument(
        "--pattern",
        type=Path,
        help="exact executed pattern.bin; required for Raw Event FAIL_LOG mapping and resolves Rate Vector runtime order",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(render_fail_report(args.fail_json, args.map_json, pattern_path=args.pattern))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
