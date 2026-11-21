"""Conversion-local runtime reuse cannot bypass artifact or model checks."""

from dataclasses import FrozenInstanceError
import gc
import json
import weakref

import pytest

from stpg.constants import STPG_DRIVE_VECTOR, STPG_RATE_SLOT_DIR_UPDATE
from stpg.pattern import LoopBlock, LoopRefRecord, RateDesc, RateSlot, VectorRecord, pack_rate_vector
from stil.json_artifacts import resolve_json_artifact, write_json_artifact
import stil.runtime_wait_model as runtime
import stil.wait_schedule as schedules
import stil.segmented_package as segmented
import stil.wait_acceptance as acceptance
from stil.wait_acceptance import evaluate_wait_schedule
from stil.wait_binary import apply_wait_schedule_to_pattern, inspect_wait_schedule_encoding


def pattern_bytes(*, period=40, loop=False):
    records = [VectorRecord(0, 0, 1, value, 3, 0, 0) for value in (0, 1)]
    return pack_rate_vector(
        rates=[RateDesc(period, 2, 0, 0)],
        slots=[RateSlot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, 3, 3, 0),
               RateSlot(20, STPG_DRIVE_VECTOR, 3, 0, 0)],
        vectors=[LoopRefRecord(0)] if loop else records * 16,
        loop_blocks=[LoopBlock(16, tuple(records))] if loop else [],
        tick_hz=200_000_000, active_pins=16)


def source_map():
    return {"mode_emitted": "rate-vector", "vectors": {
        str(index): {"repeat": 1, "source_map": [{"waveform_table": "WFT0", "stil_line": index + 10,
                                                    "global_vector_index": index, "pattern_name": "MAIN"}]}
        for index in range(32)}}


def artifacts(tmp_path):
    pattern = tmp_path / "pattern.bin"
    pattern.write_bytes(pattern_bytes())
    mapping = tmp_path / "pattern.map.json"
    write_json_artifact(mapping, source_map(), compression="gzip", compact_vector_map=True)
    return pattern, mapping


@pytest.mark.parametrize("loop", [False, True])
def test_decode_and_geometry_reused_once_without_retaining_event_arrays(monkeypatch, loop):
    payload = pattern_bytes(loop=loop)
    cache = runtime._RuntimeModelCache()
    original = runtime.unpack_rate_vector_full
    calls = []

    def unpack(data):
        calls.append(data)
        return original(data)

    monkeypatch.setattr(runtime, "unpack_rate_vector_full", unpack)
    expected = runtime.build_runtime_model(payload, chunk_event_capacity=4)
    calls.clear()
    first = runtime.build_runtime_model(payload, chunk_event_capacity=4, _model_cache=cache)
    second = runtime.build_runtime_model(bytes(bytearray(payload)), chunk_event_capacity=4, _model_cache=cache)
    assert first == second == expected
    assert len(calls) == 1
    assert cache._pattern_digest is not None
    assert all(isinstance(chunk, runtime.RuntimeChunk) for chunk in cache._chunks)
    assert not any(isinstance(value, runtime.RuntimeEvent) for value in cache.__dict__.values())
    _, chunks = runtime.build_runtime_chunks(payload, chunk_event_capacity=4, _model_cache=cache)
    chunks.clear()
    assert runtime.build_runtime_model(payload, chunk_event_capacity=4, _model_cache=cache) == expected
    with pytest.raises(FrozenInstanceError):
        cache._decoded[1][0].period_ticks = 999


def test_capacity_limit_ring_and_binary_changes_never_reuse_wrong_model():
    cache = runtime._RuntimeModelCache()
    for payload, capacity, ring in ((pattern_bytes(), 4, 3), (pattern_bytes(), 6, 6),
                                    (pattern_bytes(period=50), 6, 3), (pattern_bytes(loop=True), 4, 3)):
        assert runtime.build_runtime_model(payload, chunk_event_capacity=capacity, ring_slot_count=ring,
                                            _model_cache=cache) == runtime.build_runtime_model(
            payload, chunk_event_capacity=capacity, ring_slot_count=ring)
    with pytest.raises(runtime.RuntimeWaitModelError, match="max_events=3"):
        runtime.build_runtime_chunks(pattern_bytes(loop=True), chunk_event_capacity=4, max_events=3,
                                     _model_cache=cache)
    assert runtime.build_runtime_model(pattern_bytes(loop=True), chunk_event_capacity=4, _model_cache=cache) == (
        runtime.build_runtime_model(pattern_bytes(loop=True), chunk_event_capacity=4))
    assert cache._pattern_bytes == pattern_bytes(loop=True)  # only the latest binary


def test_cached_source_context_is_owned_and_file_validation_still_runs(tmp_path, monkeypatch):
    pattern, mapping = artifacts(tmp_path)
    cache = runtime._RuntimeModelCache()
    original = schedules._compute_chunk_source_context
    calls = []

    def compute(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(schedules, "_compute_chunk_source_context", compute)
    schedule = schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)
    schedule_path = tmp_path / "wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
    schedule["boundaries"][0]["source_context"]["first_event"]["source"]["waveform_table"] = "NOT_OWNED"
    again = schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)
    assert again["boundaries"][0]["boundary_rate_unit"] == "WFT0"
    assert again["boundaries"][0]["source_context"]["first_event"]["source"]["waveform_table"] == "WFT0"
    assert len(calls) == 1
    assert schedules.validate_wait_schedule(pattern, schedule_path, map_path=mapping, _model_cache=cache)["status"] == "PASS"
    accepted = evaluate_wait_schedule(pattern, schedule_path, map_path=mapping, service_time_us=0.01,
                                      service_time_source="unit test only", _model_cache=cache)
    assert accepted["status"] == "ACCEPTED"
    assert len(calls) == 1
    # Do not retain the large input map in the runtime context.
    assert not hasattr(cache, "_map_data")


def test_changed_map_and_corrupt_crc_are_rejected_even_with_warm_context(tmp_path):
    pattern, mapping = artifacts(tmp_path)
    cache = runtime._RuntimeModelCache()
    schedule = schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)
    schedule_path = tmp_path / "wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
    changed = source_map()
    changed["vectors"]["0"]["source_map"][0]["waveform_table"] = "WFT1"
    write_json_artifact(mapping, changed, compression="gzip", compact_vector_map=True)
    report = schedules.validate_wait_schedule(pattern, schedule_path, map_path=mapping, _model_cache=cache)
    assert report["status"] == "REJECT"
    assert any("map sha256 mismatch" in error for error in report["errors"])
    updated = schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)
    assert updated["boundaries"][0]["boundary_rate_unit"] == "WFT1"
    raw = bytearray(pattern.read_bytes())
    raw[-1] ^= 1
    pattern.write_bytes(raw)
    report = schedules.validate_wait_schedule(pattern, schedule_path, map_path=mapping, _model_cache=cache)
    assert report["status"] == "REJECT"
    assert any("crc32" in error for error in report["errors"])


def test_source_map_change_during_read_rejects_before_caching_context(tmp_path, monkeypatch):
    pattern, mapping = artifacts(tmp_path)
    original = schedules._load_json_object

    def read_then_change(path):
        value = original(path)
        if path == mapping:
            write_json_artifact(mapping, {"vectors": {}}, compression="gzip")
        return value

    monkeypatch.setattr(schedules, "_load_json_object", read_then_change)
    cache = runtime._RuntimeModelCache()
    with pytest.raises(schedules.WaitScheduleError, match="source map changed"):
        schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)
    assert cache._contexts is None


def test_rate_cloning_does_not_mutate_cached_tables_or_original_source_map(tmp_path):
    pattern, mapping = artifacts(tmp_path)
    cache = runtime._RuntimeModelCache()
    schedule = schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)
    schedule["boundaries"][1]["wait_ticks"] = 1000
    schedule["boundaries"][1]["wait_us"] = 5.0
    original_map = source_map()
    original_decoded = cache.decode(pattern.read_bytes())
    expected = apply_wait_schedule_to_pattern(pattern.read_bytes(), schedule=schedule, map_data=source_map())
    actual = apply_wait_schedule_to_pattern(pattern.read_bytes(), schedule=schedule, map_data=original_map,
                                            _model_cache=cache)
    assert actual == expected
    assert cache.decode(pattern.read_bytes()) is original_decoded
    assert len(original_decoded[1]) == 1
    assert original_map == source_map()
    assert inspect_wait_schedule_encoding(pattern.read_bytes(), schedule=schedule, _model_cache=cache) == (
        inspect_wait_schedule_encoding(pattern.read_bytes(), schedule=schedule))


def test_segment_aggregation_releases_each_map_before_reading_the_next(tmp_path, monkeypatch):
    class TrackedMap(dict):
        pass

    references = []

    def read_map(path):
        gc.collect()
        assert all(reference() is None for reference in references)
        mapping = TrackedMap(source_map())
        references.append(weakref.ref(mapping))
        return mapping

    monkeypatch.setattr(segmented, "read_json_artifact", read_map)
    inputs = []
    for index in range(2):
        directory = tmp_path / f"segment-{index}"
        directory.mkdir()
        (directory / "pattern.bin").write_bytes(pattern_bytes())
        inputs.append({"source_package_dir": str(directory), "segment_id": index,
                       "global_vector_start": index * 32, "global_vector_end": index * 32 + 31})
    model, ranges = segmented._aggregate_segment_runtime_model(inputs, chunk_event_capacity=4, ring_slot_count=3)
    assert model["pattern"]["expanded_event_count"] == 128
    assert len(ranges) == 2 and len(references) == 2
    assert all(chunk["boundary_rate_unit"] == "WFT0" for chunk in model["pattern"]["chunks"])
    gc.collect()
    assert all(reference() is None for reference in references)


@pytest.mark.parametrize("compression", ["none", "gzip"])
def test_warm_context_skips_map_decoding_but_keeps_forbidden_policy_and_acceptance(tmp_path, monkeypatch, compression):
    pattern = tmp_path / "pattern.bin"
    pattern.write_bytes(pattern_bytes())
    mapping = tmp_path / "map.json"
    write_json_artifact(mapping, source_map(), compression=compression)
    options = dict(map_path=mapping, chunk_event_capacity=4, forbidden_stil_rate_units=["WFT0"])
    expected = schedules.build_wait_schedule(pattern, **options)
    cache = runtime._RuntimeModelCache()
    reads = []
    original = schedules._load_json_object

    def read(path):
        if path == mapping:
            reads.append(path)
        return original(path)

    def no_extra_map_read(path):
        assert path != mapping, "Loop detection must use the verified map summary"
        return read_json(path)

    read_json = acceptance.read_json_artifact
    monkeypatch.setattr(schedules, "_load_json_object", read)
    monkeypatch.setattr(acceptance, "read_json_artifact", no_extra_map_read)
    assert schedules.build_wait_schedule(pattern, **options, _model_cache=cache) == expected
    schedule_path = tmp_path / "wait.json"
    schedule_path.write_text(json.dumps(expected), encoding="utf-8")
    assert schedules.build_wait_schedule(pattern, **options, _model_cache=cache) == expected
    assert schedules.validate_wait_schedule(pattern, schedule_path, map_path=mapping,
                                            _model_cache=cache)["status"] == "PASS"
    result = evaluate_wait_schedule(pattern, schedule_path, map_path=mapping,
                                    service_time_us=0.01, service_time_source="test only", _model_cache=cache)
    assert result["status"] == "ACCEPTED"
    assert reads == [mapping]
    # Cached scalar contexts still enforce the forbidden wait boundary.
    expected["boundaries"][1].update(wait_ticks=100, wait_us=0.5)
    schedule_path.write_text(json.dumps(expected), encoding="utf-8")
    warm = schedules.validate_wait_schedule(pattern, schedule_path, map_path=mapping, _model_cache=cache)
    monkeypatch.setattr(acceptance, "read_json_artifact", read_json)
    cold = schedules.validate_wait_schedule(pattern, schedule_path, map_path=mapping)
    assert warm == cold and warm["status"] == "REJECT"
    assert any("forbidden" in message for message in warm["errors"])


@pytest.mark.parametrize("loop_marker", ["loop_iteration", "loop_vector_repeat", "loop", "loop_id", "source_loop_id"])
def test_split_plain_loop_provenance_remains_bound_to_current_map(tmp_path, monkeypatch, loop_marker):
    pattern, mapping = artifacts(tmp_path)  # no LoopRef in this binary
    loop_map = source_map()
    if loop_marker in {"loop_id", "source_loop_id"}:
        loop_map["vectors"]["0"][loop_marker] = 0
    else:
        loop_map["vectors"]["0"]["top_level_kind"] = loop_marker
    write_json_artifact(mapping, loop_map, compression="gzip")
    cache = runtime._RuntimeModelCache()
    schedule = schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)
    schedule_path = tmp_path / "wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
    options = dict(map_path=mapping, service_time_us=0.01, service_time_source="test only")
    expected = evaluate_wait_schedule(pattern, schedule_path, **options, service_profile_scope="loop-generic")
    result = evaluate_wait_schedule(pattern, schedule_path, **options, _model_cache=cache)
    assert result["status"] == "REJECTED" and "loop-generic" in result["errors"][0]
    assert evaluate_wait_schedule(pattern, schedule_path, **options, service_profile_scope="loop-generic",
                                   _model_cache=cache) == expected
    assert expected["status"] == "ACCEPTED"
    assert cache._map_has_loop is True
    write_json_artifact(mapping, source_map(), compression="gzip")
    schedule = schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
    assert cache._map_has_loop is False
    assert evaluate_wait_schedule(pattern, schedule_path, **options, _model_cache=cache)["status"] == "ACCEPTED"


@pytest.mark.parametrize("fault", ["missing", "malformed", "same-size"])
def test_warm_map_reuse_cannot_hide_missing_or_modified_artifacts(tmp_path, fault):
    pattern, mapping = artifacts(tmp_path)
    cache = runtime._RuntimeModelCache()
    schedule = schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)
    schedule_path = tmp_path / "wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
    stored = resolve_json_artifact(mapping)
    if fault == "missing":
        stored.unlink()
    elif fault == "malformed":
        stored.write_bytes(b"not a gzip document")
    else:
        # A same-sized uncompressed document is a fresh cache identity too.
        changed = source_map()
        changed["vectors"]["0"]["source_map"][0]["waveform_table"] = "WFT1"
        stored = write_json_artifact(mapping, source_map(), compression="none")
        plain_schedule = schedules.build_wait_schedule(pattern, map_path=stored, chunk_event_capacity=4, _model_cache=cache)
        schedule_path.write_text(json.dumps(plain_schedule), encoding="utf-8")
        previous_size = stored.stat().st_size
        write_json_artifact(mapping, changed, compression="none")
        assert stored.stat().st_size == previous_size
    warm = schedules.validate_wait_schedule(pattern, schedule_path, map_path=mapping, _model_cache=cache)
    cold = schedules.validate_wait_schedule(pattern, schedule_path, map_path=mapping)
    assert warm == cold and warm["status"] == "REJECT"


def test_map_change_during_warm_digest_check_rejects(tmp_path, monkeypatch):
    pattern, mapping = artifacts(tmp_path)
    cache = runtime._RuntimeModelCache()
    schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)
    original = schedules._stored_map_digest
    calls = []

    def digest_then_change(path):
        result = original(path)
        calls.append(path)
        if len(calls) == 1:
            write_json_artifact(mapping, {"vectors": {}}, compression="gzip")
        return result

    monkeypatch.setattr(schedules, "_stored_map_digest", digest_then_change)
    with pytest.raises(schedules.WaitScheduleError, match="source map changed"):
        schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)


def test_geometry_change_reparses_map_and_summary_cache_has_bounded_retention(tmp_path, monkeypatch):
    pattern, mapping = artifacts(tmp_path)
    cache = runtime._RuntimeModelCache()
    original = schedules._load_json_object
    reads = []

    def read(path):
        if path == mapping:
            reads.append(path)
        return original(path)

    monkeypatch.setattr(schedules, "_load_json_object", read)
    for capacity in (4, 4, 6, 6):
        warm = schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=capacity, _model_cache=cache)
        assert warm["model"]["chunk_event_capacity"] == capacity
    assert reads == [mapping, mapping]
    assert isinstance(cache._map_has_loop, bool)
    assert not hasattr(cache, "_map_data")
    cache.decode(pattern_bytes(period=50))
    assert cache._map_summary_key is None and cache._map_has_loop is None


def test_preserved_binary_loop_is_detected_without_reading_source_map(tmp_path, monkeypatch):
    pattern = tmp_path / "loop.bin"
    pattern.write_bytes(pattern_bytes(loop=True))

    def forbidden(*args, **kwargs):
        pytest.fail("binary LoopRef is already sufficient evidence")

    monkeypatch.setattr(acceptance, "read_json_artifact", forbidden)
    assert acceptance._has_loop_provenance(pattern, tmp_path / "missing.json", _model_cache=runtime._RuntimeModelCache())


def test_loop_summary_change_during_acceptance_rejects(tmp_path, monkeypatch):
    pattern, mapping = artifacts(tmp_path)
    cache = runtime._RuntimeModelCache()
    schedule = schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)
    schedule_path = tmp_path / "wait.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
    original = acceptance._stored_map_digest
    calls = []

    def digest_then_change(path):
        result = original(path)
        calls.append(path)
        if len(calls) == 1:
            mapping_with_loop = source_map()
            mapping_with_loop["vectors"]["0"]["source_loop_id"] = 0
            write_json_artifact(mapping, mapping_with_loop, compression="gzip")
        return result

    monkeypatch.setattr(acceptance, "_stored_map_digest", digest_then_change)
    result = evaluate_wait_schedule(pattern, schedule_path, map_path=mapping,
                                    service_time_us=0.01, service_time_source="test only", _model_cache=cache)
    assert result["status"] == "REJECTED"
    assert "source map changed" in result["errors"][0]


def test_verified_map_summary_and_contexts_do_not_retain_the_input_map(tmp_path, monkeypatch):
    class TrackedMap(dict):
        pass

    pattern, mapping = artifacts(tmp_path)
    original = schedules._load_json_object
    references = []

    def read(path):
        value = TrackedMap(original(path))
        references.append(weakref.ref(value))
        return value

    monkeypatch.setattr(schedules, "_load_json_object", read)
    cache = runtime._RuntimeModelCache()
    first = schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache)
    gc.collect()
    assert all(reference() is None for reference in references)
    assert schedules.build_wait_schedule(pattern, map_path=mapping, chunk_event_capacity=4, _model_cache=cache) == first
    assert len(references) == 1
