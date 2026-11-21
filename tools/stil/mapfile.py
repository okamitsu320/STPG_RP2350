from __future__ import annotations

import hashlib
from typing import Any

from stpg.pattern import LoopRefRecord, VectorRecord
from stpg.constants import STPG_DEFAULT_TICK_HZ

from .backend_rate import (
    RateBuildResult,
    ShapingOptions,
    _SlotSignatureCache,
    _build_rate_tables,
    _build_vector_record,
    _same_shape,
    _vector_rate_key,
    build_folded_rate_vector_records,
)
from .backend_raw import raw_event_phase_times
from .ast import IRVector
from .markers import summarize_marker_specs


def _marker_events_for_vector(ir, vector) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    return [
        event
        for marker in ir.markers
        for event in marker.get("marker_events_by_vector", {}).get(vector.vector_index, [])
    ]


def build_map(
    ir,
    *,
    mode_emitted: str,
    shaping: ShapingOptions | None = None,
    pattern_bytes: bytes | None = None,
    _rate_build_result: RateBuildResult | None = None,
) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    signal_map = {}
    for signal_name, assignment in ir.resolved_pattern.pin_map.signals.items():
        payload = {
            "bit": assignment.bit,
            "logical_pin": assignment.logical_pin,
            "direction": assignment.direction,
            "role": assignment.role,
            "allow_shared": assignment.allow_shared,
        }
        if assignment.gpio is not None:
            payload["gpio"] = assignment.gpio
        signal_map[signal_name] = payload
    groups = {group.name: list(group.signals) for group in ir.resolved_pattern.document.signal_groups}
    vectors = _build_vector_map(
        ir, mode_emitted=mode_emitted, shaping=shaping, _rate_build_result=_rate_build_result,
    )
    result = {
        "input_file": ir.resolved_pattern.document.source_file,
        "mode_emitted": mode_emitted,
        "signal_map": signal_map,
        "groups": groups,
        "markers": summarize_marker_specs(ir.markers),
        "terminal_drive_value": ir.terminal_drive_value,
        "terminal_drive_enable": ir.terminal_drive_enable,
        "vectors": vectors,
    }
    if mode_emitted == "raw-event":
        if pattern_bytes is None:
            raise ValueError("raw-event source maps must be bound to the emitted pattern bytes")
        raw_event_ranges = []
        event_cursor = 0
        for vector in ir.vectors:
            phase_times = raw_event_phase_times(vector)
            source_record_index = int(vector.vector_index)
            vector_map = vectors.get(str(source_record_index))
            if not isinstance(vector_map, dict):
                raise ValueError(
                    "raw-event map cannot find source vector "
                    f"{source_record_index}"
                )
            event_end = event_cursor + len(phase_times)
            raw_event_ranges.append(
                {
                    "event_start": event_cursor,
                    "event_end_exclusive": event_end,
                    "source_record_index": source_record_index,
                    "logical_vector_index": int(vector.top_level_index),
                    "phase_times_ticks": list(phase_times),
                }
            )
            event_cursor = event_end
        result["raw_event_ranges"] = raw_event_ranges
        result["raw_event_count"] = event_cursor
        result["pattern_sha256"] = hashlib.sha256(pattern_bytes).hexdigest()
    return result


def _build_vector_map(
    ir, *, mode_emitted: str, shaping: ShapingOptions | None = None,
    _rate_build_result: RateBuildResult | None = None,
) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    if mode_emitted != "rate-vector":
        return {
            str(vector.vector_index): {
                "top_level_kind": "vector",
                "repeat": 1,
                "involved_bits": sorted(vector.signal_bit_assignments.values()),
                "marker_events": [
                    event
                    for marker in ir.markers
                    for event in marker.get("marker_events_by_vector", {}).get(vector.vector_index, [])
                ],
                "source_map": [
                    {
                        "local_vector_index": vector.vector_index,
                        "global_vector_index": vector.top_level_index,
                        "repeat_iteration": 0,
                        "stil_line": vector.stil_line,
                        "label": vector.label,
                        "pattern_name": vector.pattern_name,
                        "waveform_table": vector.waveform_table_name,
                        "signals": sorted(vector.signal_chars.keys()),
                        "expansion_trace": list(vector.expansion_trace),
                    }
                ],
            }
            for vector in ir.vectors
        }

    if ir.loop_preserved or ir.sequence_items:
        return _build_preserved_loop_map(ir, _rate_build_result=_rate_build_result)

    if _rate_build_result is None:
        folded_records, folded_source_vectors, _ = build_folded_rate_vector_records(ir, shaping=shaping)
    else:
        # Private handoff from the same compile invocation, not a cross-input
        # cache. The backend already assigned rate IDs and folded these exact
        # source occurrences with the requested shaping.
        folded_records = _rate_build_result.folded_vectors
        folded_source_vectors = _rate_build_result.folded_source_vectors
    vectors: dict[str, Any] = {}
    for emitted_index, (record, source_vectors) in enumerate(zip(folded_records, folded_source_vectors)):
        if not isinstance(record, VectorRecord):
            continue
        source_map = []
        marker_events = []
        involved_bits: set[int] = set()
        for repeat_iteration, source_vector in enumerate(source_vectors):
            source_map.append(
                {
                    "local_vector_index": emitted_index,
                    "global_vector_index": source_vector.top_level_index,
                    "repeat_iteration": repeat_iteration,
                    "stil_line": source_vector.stil_line,
                    "label": source_vector.label,
                    "pattern_name": source_vector.pattern_name,
                    "waveform_table": source_vector.waveform_table_name,
                    "signals": sorted(source_vector.signal_chars.keys()),
                    "expansion_trace": list(source_vector.expansion_trace),
                }
            )
            involved_bits.update(source_vector.signal_bit_assignments.values())
            marker_events.extend(
                event
                for marker in ir.markers
                for event in marker.get("marker_events_by_vector", {}).get(source_vector.vector_index, [])
            )
        vectors[str(emitted_index)] = {
            "top_level_kind": "vector",
            "repeat": record.repeat,
            "involved_bits": sorted(involved_bits),
            "marker_events": marker_events,
            "source_map": source_map,
        }
    return vectors


def _source_map_entry(vector, *, body_record_index: int | None = None) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    entry = {
        "local_vector_index": 0,
        "global_vector_index": int(vector.top_level_index),
        "repeat_iteration": 0,
        "stil_line": int(vector.stil_line),
        "label": vector.label,
        "pattern_name": vector.pattern_name,
        "waveform_table": vector.waveform_table_name,
        "signals": sorted(vector.signal_chars.keys()),
        "source_path": list(vector.source_path),
    }
    if vector.expansion_trace:
        entry["expansion_trace"] = list(vector.expansion_trace)
    if vector.loop_source_path is not None:
        entry.update(
            {
                "source_loop_path": list(vector.loop_source_path),
                "loop_iteration_index": vector.loop_iteration_index,
                "loop_body_index": vector.loop_body_index,
            }
        )
    if body_record_index is not None:
        entry["body_record_index"] = body_record_index
    return entry


def _add_provenance(
    entry: dict[str, Any],
    *,
    top_level_entry_index: int,
    entry_kind: str,
    top_level_repeat_index: int | None = None,
    loop_id: int | None = None,
    loop_ref_occurrence_index: int | None = None,
    loop_iteration_index: int | None = None,
    expanded_logical_vector_index: int | None = None,
) -> dict[str, Any]:
    """Attach the occurrence fields shared by map and runtime consumers."""

    enriched = dict(entry)
    enriched.update(
        {
            "top_level_entry_index": int(top_level_entry_index),
            "entry_kind": entry_kind,
            "top_level_repeat_index": top_level_repeat_index,
            "loop_id": loop_id,
            "loop_ref_occurrence_index": loop_ref_occurrence_index,
            "loop_iteration_index": loop_iteration_index,
            "expanded_logical_vector_index": expanded_logical_vector_index,
        }
    )
    return enriched


def _build_preserved_loop_map(
    ir, *, _rate_build_result: RateBuildResult | None = None,
) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Build a bounded map for top-level LoopRefRecord entries.

    Loop occurrences are intentionally represented by their body map and a
    logical offset, rather than by enumerating every repetition.
    """

    from .backend_rate import build_preserved_loop_rate_vector

    built = (
        _rate_build_result if _rate_build_result is not None
        else build_preserved_loop_rate_vector(ir, tick_hz=ir.tick_hz or STPG_DEFAULT_TICK_HZ)
    )
    slot_signatures = _SlotSignatureCache()
    _rates, _slots, rate_ids = _build_rate_tables(ir.vectors, _slot_signatures=slot_signatures)
    vectors: dict[str, Any] = {}
    emitted_index = 0
    built_index = 0
    for sequence_item in ir.sequence_items:
        if built_index >= len(built.sequence_entries):
            raise ValueError("preserved loop map cannot align top-level sequence entries")
        entry = built.sequence_entries[built_index]
        if isinstance(sequence_item, IRVector):
            is_loop_iteration = sequence_item.loop_source_path is not None
            source_entry = _add_provenance(
                _source_map_entry(sequence_item),
                top_level_entry_index=emitted_index,
                entry_kind="loop_iteration" if is_loop_iteration else "vector",
                top_level_repeat_index=0,
                loop_iteration_index=(
                    sequence_item.loop_iteration_index if is_loop_iteration else None
                ),
                expanded_logical_vector_index=sequence_item.top_level_index,
            )
            vectors[str(emitted_index)] = {
                "top_level_kind": "loop_iteration" if is_loop_iteration else "vector",
                "repeat": int(entry.repeat),
                "involved_bits": sorted(sequence_item.signal_bit_assignments.values()),
                "marker_events": _marker_events_for_vector(ir, sequence_item),
                "source_map": [source_entry],
            }
            if is_loop_iteration:
                vectors[str(emitted_index)].update(
                    {
                        "source_loop_path": list(sequence_item.loop_source_path),
                        "loop_iteration_index": sequence_item.loop_iteration_index,
                        "loop_body_index": sequence_item.loop_body_index,
                    }
                )
            emitted_index += 1
            built_index += 1
            continue
        if not isinstance(entry, LoopRefRecord):
            # One effective record (possibly several identical source
            # vectors) is lowered to bounded VectorRecord repeats.
            body_vectors = tuple(item for item in sequence_item.items if isinstance(item, IRVector))
            if not body_vectors or len(body_vectors) != len(sequence_item.items):
                raise ValueError("preserved loop map cannot align lowered loop entries")
            records = [_build_vector_record(vector, rate_ids[
                _vector_rate_key(vector, _slot_signatures=slot_signatures)]) for vector in body_vectors]
            if any(not _same_shape(records[0], record) for record in records[1:]):
                raise ValueError("preserved loop map lowered body is not identical")
            body_count = len(body_vectors)
            remaining_vectors = int(sequence_item.loop_count) * body_count
            vector_offset = 0
            while remaining_vectors:
                if built_index >= len(built.sequence_entries):
                    raise ValueError("preserved loop map cannot align lowered loop repeat chunks")
                current_entry = built.sequence_entries[built_index]
                if (not isinstance(current_entry, VectorRecord) or not 0 < current_entry.repeat <= remaining_vectors
                        or not _same_shape(records[0], current_entry)):
                    raise ValueError("preserved loop map cannot align lowered loop repeat chunks")
                iteration_base, body_offset = divmod(vector_offset, body_count)
                base_source = _add_provenance(
                    _source_map_entry(body_vectors[0], body_record_index=0),
                    top_level_entry_index=emitted_index,
                    entry_kind="vector",
                    top_level_repeat_index=0,
                    loop_id=sequence_item.loop_id,
                    loop_ref_occurrence_index=emitted_index,
                    loop_iteration_index=sequence_item.iteration_base + iteration_base,
                    expanded_logical_vector_index=sequence_item.logical_vector_base + vector_offset,
                )
                vectors[str(emitted_index)] = {
                    "top_level_kind": "loop_vector_repeat",
                    "repeat": int(current_entry.repeat),
                    "loop_count": int(sequence_item.loop_count),
                    "logical_vector_base": int(sequence_item.logical_vector_base),
                    "iteration_base": int(sequence_item.iteration_base),
                    "involved_bits": sorted(body_vectors[0].signal_bit_assignments.values()),
                    "marker_events": _marker_events_for_vector(ir, body_vectors[0]),
                    "source_map": [base_source],
                    "body_source_map": [
                        {
                            **base_source,
                            "body_repeat_count": 1,
                            "marker_events": _marker_events_for_vector(ir, body_vectors[0]),
                        }
                    ],
                    "source_map_compression": {
                        "kind": "loop_vector_repeat_v1",
                        "global_vector_base": int(sequence_item.logical_vector_base) + vector_offset,
                        "loop_id": int(sequence_item.loop_id),
                        "loop_iteration_base": int(sequence_item.iteration_base) + iteration_base,
                        "vector_stride": 1,
                    },
                }
                if body_count > 1:
                    # Do not extend v1's meaning silently: its iteration
                    # stride is one per record repeat, not one per body.
                    payload = vectors[str(emitted_index)]
                    payload["source_map_compression"].update({
                        "kind": "loop_vector_repeat_v2",
                        "loop_body_vector_count": body_count,
                        "loop_body_vector_offset": body_offset,
                    })
                    payload["body_source_map"] = [
                        {**_add_provenance(
                            _source_map_entry(vector, body_record_index=0),
                            top_level_entry_index=emitted_index, entry_kind="vector",
                            top_level_repeat_index=0, loop_id=sequence_item.loop_id,
                            loop_ref_occurrence_index=emitted_index,
                            loop_iteration_index=sequence_item.iteration_base + iteration_base,
                            expanded_logical_vector_index=sequence_item.logical_vector_base + body_index,
                        ), "body_repeat_count": 1,
                         "loop_body_index": body_index,
                         "marker_events": _marker_events_for_vector(ir, vector)}
                        for body_index, vector in enumerate(body_vectors)
                    ]
                    payload["source_map"] = [dict(payload["body_source_map"][body_offset])]
                    payload["source_map"][0].update({
                        "global_vector_index": sequence_item.logical_vector_base + vector_offset,
                        "expanded_logical_vector_index": sequence_item.logical_vector_base + vector_offset,
                    })
                    payload["marker_events"] = [event for vector in body_vectors
                                                 for event in _marker_events_for_vector(ir, vector)]
                emitted_index += 1
                built_index += 1
                vector_offset += int(current_entry.repeat)
                remaining_vectors -= int(current_entry.repeat)
            continue
        body_vectors = tuple(item for item in sequence_item.items if isinstance(item, IRVector))
        if len(body_vectors) != len(sequence_item.items):
            raise ValueError("preserved loop map cannot represent nested loops")
        # The backend folds adjacent equal body vectors before constructing a
        # LoopBlock. Recreate that bounded grouping here so body_record_index
        # remains the emitted ABI index even when one body record covers more
        # than one source vector.
        body_groups: list[tuple[IRVector, ...]] = []
        body_records: list[VectorRecord] = []
        for vector in body_vectors:
            record = _build_vector_record(
                vector, rate_ids[_vector_rate_key(vector, _slot_signatures=slot_signatures)],
            )
            if body_records and _same_shape(body_records[-1], record) and body_records[-1].repeat < 0xFFFF:
                previous = body_records[-1]
                body_records[-1] = VectorRecord(
                    rate_id=previous.rate_id,
                    flags=previous.flags,
                    repeat=previous.repeat + 1,
                    drive_value=previous.drive_value,
                    drive_enable=previous.drive_enable,
                    expect_value=previous.expect_value,
                    expect_mask=previous.expect_mask,
                )
                body_groups[-1] = (*body_groups[-1], vector)
            else:
                body_records.append(record)
                body_groups.append((vector,))
        if len(body_records) != len(built.loop_blocks[entry.loop_id].body_records):
            raise ValueError("preserved loop map body folding differs from compiled LoopBlock")
        body_logical_vector_count = sum(len(group) for group in body_groups)
        body_logical_offset = 0
        body_source_map: list[dict[str, Any]] = []
        for index, group in enumerate(body_groups):
            body_global_base = sequence_item.logical_vector_base + body_logical_offset
            body_entry = _source_map_entry(group[0], body_record_index=index)
            body_entry["global_vector_index"] = body_global_base
            body_source_map.append(
                {
                    **body_entry,
                    "body_repeat_count": len(group),
                    "body_logical_offset": body_logical_offset,
                    "body_source_paths": [list(vector.source_path) for vector in group],
                    "body_source_entries": [
                        {
                            **_add_provenance(
                                _source_map_entry(vector, body_record_index=index),
                                top_level_entry_index=emitted_index,
                                entry_kind="loop_ref",
                                loop_id=entry.loop_id,
                                loop_ref_occurrence_index=emitted_index,
                                expanded_logical_vector_index=(
                                    body_global_base + repeat_index
                                ),
                            ),
                            "global_vector_index": body_global_base + repeat_index,
                            "body_repeat_index": repeat_index,
                            "loop_body_record_index": index,
                            "loop_body_repeat_index": repeat_index,
                        }
                        for repeat_index, vector in enumerate(group)
                    ],
                    "marker_events": [
                        event
                        for vector in group
                        for event in _marker_events_for_vector(ir, vector)
                    ],
                }
            )
            body_logical_offset += len(group)
        for body_index, body_entry in enumerate(body_source_map):
            body_entry["top_level_entry_index"] = emitted_index
            body_entry["entry_kind"] = "loop_ref"
            body_entry["loop_id"] = int(entry.loop_id)
            body_entry["loop_ref_occurrence_index"] = emitted_index
            body_entry["loop_body_record_index"] = body_index
            body_entry["loop_iteration_index"] = None
            body_entry["expanded_logical_vector_base"] = (
                sequence_item.logical_vector_base
                + int(body_entry.get("body_logical_offset", 0))
            )
        vectors[str(emitted_index)] = {
            "top_level_kind": "loop",
            "repeat": int(sequence_item.loop_count),
            "loop_id": int(entry.loop_id),
            "loop_count": int(sequence_item.loop_count),
            "body_logical_vector_count": body_logical_vector_count,
            "logical_vector_base": int(sequence_item.logical_vector_base),
            "iteration_base": int(sequence_item.iteration_base),
            "involved_bits": sorted({bit for vector in body_vectors for bit in vector.signal_bit_assignments.values()}),
            "marker_events": [
                event
                for body_vector in body_vectors
                for event in _marker_events_for_vector(ir, body_vector)
            ],
            "source_map": body_source_map,
            "body_source_map": body_source_map,
        }
        emitted_index += 1
        built_index += 1
    if built_index != len(built.sequence_entries):
        raise ValueError("preserved loop map has unconsumed compiled sequence entries")
    return vectors
