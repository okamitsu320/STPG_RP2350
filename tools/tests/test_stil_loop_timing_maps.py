"""Public synthetic provenance oracles; no user patterns or hardware."""

from copy import deepcopy
import json

import pytest

from stpg.pattern import VectorRecord, unpack_rate_vector_full
from stil.map_lookup import SourceMapOccurrenceResolver, source_map_entry_for_occurrence
from stil.pipeline import CompileOptions, compile_stil
from stil.runtime_wait_model import iter_runtime_events
from stil.wait_binary import apply_wait_schedule_to_pattern
from stil.wait_schedule import build_wait_schedule

pytestmark = pytest.mark.stpg_host_only


def make(tmp_path, *, body_count=2, count=6, flat=False, body_values=None):
    body = "\n".join(f"V {{ ALL = {value}; }}" for value in (body_values or ["01"] * body_count))
    text = """Header { Title "public collapsed-loop provenance oracle"; }
Signals {
 D0 InOut;
 D1 InOut;
}
SignalGroups {
 ALL = 'D0 + D1';
}
Timing {
 WaveformTable NORMAL {
  Period '20000ns';
  Waveforms {
   ALL { 0: 0ns Drive(0); 1: 0ns Drive(1); }
  }
 }
}
Pattern MAIN {
 W NORMAL;
 V { ALL = 11; }
""" + (body if flat else f"Loop {count} {{\n{body}\n}}") + "\nV { ALL = 10; }\n}\n"
    path = tmp_path / "public.stil"
    path.write_text(text, encoding="utf-8")
    lines = [i for i, line in enumerate(text.splitlines(), 1) if "V { ALL" in line]
    return path, lines


def compile_(path, **kwargs):
    return compile_stil(CompileOptions(input_path=path, mode="rate-vector", tick_hz=200_000_000,
                                      loop_policy="preserve", **kwargs))


@pytest.mark.parametrize("body_count,count", [(1, 70000), (2, 40000), (3, 7), (4, 20000)])
def test_collapsed_body_literal_provenance_across_repeat_overflow(tmp_path, body_count, count):
    path, lines = make(tmp_path, body_count=body_count, count=count)
    payload, mapping, _ = compile_(path)
    _, _, _, entries, loops = unpack_rate_vector_full(payload)
    assert loops == [] and all(isinstance(entry, VectorRecord) for entry in entries)
    assert sum(entry.repeat for entry in entries) == body_count * count + 2
    assert all(entry.drive_value == 2 for entry in entries[1:-1])
    resolver = SourceMapOccurrenceResolver(mapping)
    offset = 0
    for index, entry in enumerate(entries[1:-1], 1):
        row = mapping["vectors"][str(index)]
        assert row["source_map_compression"]["kind"] == f"loop_vector_repeat_v{1 if body_count == 1 else 2}"
        assert len(row["source_map"]) == 1 and len(row["body_source_map"]) == body_count
        if body_count > 1:
            assert row["source_map"][0]["global_vector_index"] == 1 + offset
        for repeat in sorted({0, 1, entry.repeat // 2, entry.repeat - 1}):
            if repeat >= entry.repeat:
                continue
            global_index = 1 + offset + repeat
            iteration, body_index = divmod(offset + repeat, body_count)
            direct = source_map_entry_for_occurrence(row, repeat_index=repeat)
            cached = resolver.resolve(index, repeat_index=repeat)
            readonly = resolver.resolve_readonly(index, repeat_index=repeat)
            for found in (direct, cached, readonly):
                assert found["global_vector_index"] == global_index
                assert found["loop_iteration_index"] == iteration
                assert found["stil_line"] == lines[1 + body_index]
        offset += entry.repeat


@pytest.mark.parametrize("policy", ["expand", "preserve"])
@pytest.mark.parametrize("flat", [False, True])
@pytest.mark.parametrize("boundary", [1, 4, 8])
def test_wait_split_rebases_repeat_and_keeps_literal_source_lines(tmp_path, policy, flat, boundary):
    path, lines = make(tmp_path, body_count=12 if flat else 2, flat=flat)
    payload, mapping, _ = compile_stil(CompileOptions(input_path=path, mode="rate-vector",
                                      tick_hz=200_000_000, loop_policy=policy))
    before = deepcopy(mapping)
    binary, map_path = tmp_path / "pattern.bin", tmp_path / "map.json"
    binary.write_bytes(payload)
    map_path.write_text(json.dumps(mapping), encoding="utf-8")
    schedule = build_wait_schedule(binary, map_path=map_path, chunk_event_capacity=1, ring_slot_count=2)
    schedule["boundaries"][boundary].update(wait_ticks=100, wait_us=0.5)
    patched, patched_map, report = apply_wait_schedule_to_pattern(payload, schedule=schedule, map_data=mapping, max_rates=64)
    assert report["applied_boundaries"]
    assert mapping == before  # slicing must not mutate the source/cache
    _, _, _, entries, _ = unpack_rate_vector_full(patched)
    expected_lines = [lines[0]] + (lines[1:-1] if flat else lines[1:-1] * 6) + [lines[-1]]
    offset = 0
    for index, entry in enumerate(entries):
        row = patched_map["vectors"][str(index)]
        assert row["repeat"] == entry.repeat
        for repeat in range(entry.repeat):
            found = source_map_entry_for_occurrence(row, repeat_index=repeat)
            assert (found["global_vector_index"], found["stil_line"]) == (offset + repeat, expected_lines[offset + repeat])
            if "source_map_compression" not in row:
                assert found["repeat_iteration"] == repeat
        offset += entry.repeat
    assert offset == len(expected_lines) == 14


@pytest.mark.parametrize("mutation", ["kind", "kind_type", "offset", "count", "stride", "body", "boolean", "repeat"])
def test_new_compression_malformed_not_silently_misreported(tmp_path, mutation):
    path, _ = make(tmp_path)
    payload, mapping, _ = compile_(path)
    row = mapping["vectors"]["1"]
    compression = row["source_map_compression"]
    if mutation == "kind":
        compression["kind"] = "loop_vector_repeat_v999"
    elif mutation == "kind_type":
        compression["kind"] = []
    elif mutation == "offset":
        compression["loop_body_vector_offset"] = 2
    elif mutation == "count":
        compression["loop_body_vector_count"] = 0
    elif mutation == "stride":
        compression["vector_stride"] = 2
    elif mutation == "boolean":
        compression["loop_iteration_base"] = True
    elif mutation == "repeat":
        row["repeat"] = 0
    else:
        row["body_source_map"].pop()
    with pytest.raises(ValueError):
        source_map_entry_for_occurrence(row)
    with pytest.raises(ValueError):
        SourceMapOccurrenceResolver(mapping).resolve(1)


@pytest.mark.parametrize("vector_range", [None, (7, 18)])
@pytest.mark.parametrize("boundary", [2, 4, 8])
def test_wait_split_inside_folded_loopref_body_preserves_local_and_global_occurrences(tmp_path, vector_range, boundary):
    path, lines = make(tmp_path, count=3, body_values=["01"] * 4 + ["10"] * 2)
    payload, mapping, _ = compile_(path, vector_range=vector_range)
    original = deepcopy(mapping)
    binary, map_path = tmp_path / "pattern.bin", tmp_path / "map.json"
    binary.write_bytes(payload)
    map_path.write_text(json.dumps(mapping), encoding="utf-8")
    schedule = build_wait_schedule(binary, map_path=map_path, chunk_event_capacity=1, ring_slot_count=2)
    schedule["boundaries"][boundary].update(wait_ticks=100, wait_us=0.5)
    patched, patched_map, _ = apply_wait_schedule_to_pattern(payload, schedule=schedule, map_data=mapping, max_rates=64)
    assert mapping == original
    resolver = SourceMapOccurrenceResolver(patched_map)
    full_lines = [lines[0]] + lines[1:-1] * 3 + [lines[-1]]
    start, end = vector_range if vector_range is not None else (0, len(full_lines) - 1)
    events = list(iter_runtime_events(patched))
    assert len(events) == end - start + 1
    for offset, event in enumerate(events):
        found = resolver.resolve(event.source_record_index, repeat_index=event.repeat_index,
            loop_iteration=event.loop_iteration, body_record_index=event.body_record_index,
            body_repeat_index=event.body_repeat_index)
        global_index = start + offset
        assert (found["global_vector_index"], found["stil_line"]) == (global_index, full_lines[global_index])
        if 1 <= global_index <= 18:
            assert found["loop_iteration"] == found["loop_iteration_index"] == (global_index - 1) // 6
