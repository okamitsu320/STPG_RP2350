"""Content-bound and bounded reuse without changing rate-vector output."""

from copy import deepcopy
from dataclasses import replace
import gc
import random
import weakref

import pytest

from stil import backend_rate
from stil.ast import TimedSignalEvent
from stil.backend_rate import ShapingOptions, _SlotSignatureCache, _build_slot_signature
from stil.errors import StilError
from stil.mapfile import build_map
from stil.markers import inject_markers
from stil.parser import parse_stil_file
from stil.pipeline import CompileOptions, compile_stil
from stil.resolve import build_ir, load_pin_map, resolve_document
from stpg.pattern import VectorRecord, unpack_rate_vector_full


HEADER = """Signals {
  CLK InOut;
  Q InOut;
}
SignalGroups {
  ALL = 'CLK + Q';
}
Timing {
  WaveformTable T {
    Period '5us';
    Waveforms {
      CLK { P: 0ns Drive(0), 2us Drive(1); N: 0ns Drive(1), 2us Drive(0); }
      Q { H: 3us Compare(1); L: 3us Compare(0); X: 3us Compare(X); }
    }
  }
}
"""
BODY = "  V { ALL = PH; }\n  V { ALL = NL; }\n  V { ALL = PX; }\n"


def _source_and_ir(tmp_path, *, loop_policy="expand", tick_hz=200_000_000, body=BODY):
    source = tmp_path / "source.stil"
    source.write_text(HEADER + "Pattern MAIN { W T;\n Loop 8 {\n" + body + " }\n}\n", encoding="utf-8")
    document = parse_stil_file(source, loop_policy=loop_policy)
    resolved = resolve_document(document, pin_map=load_pin_map(None, document=document),
                                pattern_burst_name=None, pattern_name=None, loop_policy=loop_policy)
    return source, inject_markers(build_ir(resolved, tick_hz=tick_hz), ())


@pytest.mark.parametrize("field", ["bit", "char", "time", "action", "final_drive"])
def test_slot_cache_keys_include_bits_characters_and_complete_timed_actions(tmp_path, field):
    _, ir = _source_and_ir(tmp_path)
    vector = ir.vectors[0]
    if field == "bit":
        changed = replace(vector, signal_bit_assignments={"CLK": 2, "Q": 1})
    elif field == "char":
        changed = replace(vector, signal_chars={**vector.signal_chars, "Q": "T"})
    else:
        events = list(vector.signal_events["CLK"])
        if field == "time":
            events[1] = replace(events[1], time_ticks=events[1].time_ticks + 1)
        elif field == "action":
            events[1] = replace(events[1], action_name="Compare", action_arg="X")
        else:
            events[1] = replace(events[1], action_arg="0")
        changed = replace(vector, signal_events={**vector.signal_events, "CLK": tuple(events)})
    original = deepcopy(vector)
    cache = _SlotSignatureCache()
    assert cache.signature(vector) == _build_slot_signature(vector)
    assert cache.signature(changed) == _build_slot_signature(changed)
    assert cache.signature(changed) != cache.signature(vector)
    assert vector == original


def test_mutating_a_nested_ir_dictionary_cannot_reuse_a_stale_signature(tmp_path):
    _, ir = _source_and_ir(tmp_path)
    vector = ir.vectors[0]
    cache = _SlotSignatureCache()
    before = cache.signature(vector)
    vector.signal_bit_assignments["CLK"] = 3
    assert cache.signature(vector) == _build_slot_signature(vector)
    assert cache.signature(vector) != before


def test_invalid_changed_actions_are_not_hidden_by_a_cache_hit(tmp_path):
    _, ir = _source_and_ir(tmp_path)
    vector = ir.vectors[0]
    cache = _SlotSignatureCache()
    cache.signature(vector)
    invalid = replace(vector, signal_events={**vector.signal_events, "CLK":
                      (TimedSignalEvent(0, "Drive", "Z"), TimedSignalEvent(10, "Drive", "1"))})
    for build in (cache.signature, _build_slot_signature):
        with pytest.raises(StilError) as caught:
            build(invalid)
        assert caught.value.code == "ERR_RATE_VECTOR_DIRECTION_UNSUPPORTED"


def test_signature_retention_is_bounded_and_does_not_keep_ir_vectors(tmp_path, monkeypatch):
    _, ir = _source_and_ir(tmp_path)
    cache = _SlotSignatureCache(max_entries=2)
    calls = 0
    original = backend_rate._build_slot_signature

    def build(vector):
        nonlocal calls
        calls += 1
        return original(vector)

    monkeypatch.setattr(backend_rate, "_build_slot_signature", build)
    for vector in ir.vectors:
        assert cache.signature(vector) == original(vector)
    assert len(cache._signatures) == 2
    calls_before = calls
    cache.signature(ir.vectors[0])
    assert calls == calls_before  # overflow retained earlier useful shapes
    transient = replace(ir.vectors[0], vector_index=999)
    reference = weakref.ref(transient)
    cache.signature(transient)
    del transient
    gc.collect()  # Do not require CPython's immediate reference-count cleanup.
    assert reference() is None


@pytest.mark.parametrize("loop_policy", ["expand", "preserve"])
@pytest.mark.parametrize("tick_hz", [150_000_000, 200_000_000])
def test_cached_compile_equals_uncached_and_standalone_map(tmp_path, monkeypatch, loop_policy, tick_hz):
    source, ir = _source_and_ir(tmp_path, loop_policy=loop_policy, tick_hz=tick_hz)
    options = CompileOptions(input_path=source, mode="rate-vector", loop_policy=loop_policy, tick_hz=tick_hz)
    actual = compile_stil(options)
    assert actual[1] == build_map(ir, mode_emitted="rate-vector", pattern_bytes=actual[0])
    monkeypatch.setattr(backend_rate, "_SlotSignatureCache", lambda: _SlotSignatureCache(max_entries=0))
    assert compile_stil(options) == actual


def test_shaped_map_uses_exact_folded_records_from_its_compile(tmp_path, monkeypatch):
    source, ir = _source_and_ir(tmp_path, body="  V { ALL = PH; }\n")
    options = CompileOptions(input_path=source, mode="rate-vector", practical_chunk_shaping=True,
                              practical_chunk_shaping_target_range=(0, 7),
                              practical_chunk_shaping_max_run_vectors=2)
    original = backend_rate.build_rate_vector_records
    calls = 0

    def build(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(backend_rate, "build_rate_vector_records", build)
    binary, map_data, _ = compile_stil(options)
    assert calls == 1  # table, folding and map must not rebuild these records
    assert map_data == build_map(ir, mode_emitted="rate-vector", pattern_bytes=binary,
                                 shaping=ShapingOptions(True, (0, 7), 2))


def test_rate_ids_remain_first_seen_and_compare_masks_remain_level_independent(tmp_path):
    _, ir = _source_and_ir(tmp_path)
    built = backend_rate.compile_rate_vector(ir, tick_hz=200_000_000)
    _, rates, _, entries, _ = unpack_rate_vector_full(built.pattern_bytes)
    assert len(rates) == 2
    assert [entry.rate_id for entry in entries[:3]] == [0, 1, 0]
    assert [entry.expect_mask for entry in entries[:3]] == [2, 2, 0]
    assert [entry.expect_value for entry in entries[:3]] == [2, 0, 0]
    changed = replace(ir.vectors[0], rate_key=replace(ir.vectors[0].rate_key, period_ticks=2000))
    records = backend_rate.build_rate_vector_records(replace(ir, vectors=(ir.vectors[0], changed, ir.vectors[0])))
    assert [record.rate_id for record in records] == [0, 1, 0]


def test_long_repeat_folding_preserves_source_order_and_uint16_split():
    record = VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=1, drive_enable=1,
                          expect_value=0, expect_mask=0)
    source_indices = list(range(65537))
    records, sources = backend_rate._fold_with_sources([record] * len(source_indices), source_indices)
    assert [entry.repeat for entry in records] == [65535, 2]
    assert sources == [tuple(range(65535)), (65535, 65536)]
    assert source_indices == list(range(65537))


def test_semantic_cache_shares_data_levels_but_preserves_vector_record_data(tmp_path, monkeypatch):
    _, ir = _source_and_ir(tmp_path)
    base = ir.vectors[0]
    cache = _SlotSignatureCache()
    vectors = [replace(base, signal_chars={"CLK": drive, "Q": compare},
                       drive_chars={"CLK": drive}, compare_chars={"Q": compare},
                       signal_events={"CLK": (TimedSignalEvent(0, "Drive", drive),),
                                      "Q": (TimedSignalEvent(600, "Compare", compare),)})
               for drive, compare in (("0", "L"), ("1", "H"), ("0", "X"))]
    original = backend_rate._build_slot_signature
    calls = []

    def build(vector):
        calls.append(vector)
        return original(vector)

    monkeypatch.setattr(backend_rate, "_build_slot_signature", build)
    for vector in vectors:
        assert cache.signature(vector) == original(vector)
    assert len(calls) == len(cache._signatures) == 1
    assert len(cache._signal_actions) == 5
    records = [backend_rate._build_vector_record(vector, 0) for vector in vectors]
    assert [record.drive_value for record in records] == [0, 1, 0]
    assert [record.expect_value for record in records] == [0, 2, 0]
    assert [record.expect_mask for record in records] == [2, 2, 0]


@pytest.mark.parametrize("char,events", [
    ("0", ((0, "Drive", "0"),)), ("1", ((0, "Drive", "1"),)),
    ("0", ((5, "Drive", "0"),)), ("1", ((5, "Drive", "1"),)),
    ("P", ((0, "Drive", "0"), (5, "Drive", "1"))),
    ("N", ((0, "Drive", "1"), (5, "Drive", "0"))),
    ("0", ((0, "Compare", "X"),)), ("1", ((5, "Compare", "X"),)),
    ("H", ((0, "Compare", "H"),)), ("L", ((0, "Compare", "L"),)),
    ("X", ((5, "Compare", "X"),)), ("T", ((5, "Compare", "X"),)),
    ("Z", ((0, "Drive", "Z"),)), ("Z", ((0, "Compare", "X"),)),
    ("H", ((0, "Drive", None),)), ("0", ((0, "Drive", None),)),
    ("Q", ((0, "Compare", "X"),)), ("Q", ((0, "Drive", "1"),)),
    ("1", ((0, "Drive", "Z"), (5, "Drive", "1"))),
    ("1", ((0, "Drive", "bad"),)), ("T", ()),
])
def test_resolved_cache_matches_direct_slots_or_exact_error(tmp_path, char, events):
    _, ir = _source_and_ir(tmp_path)
    vector = replace(ir.vectors[0], signal_bit_assignments={"CLK": 0}, signal_chars={"CLK": char},
                     signal_events={"CLK": tuple(TimedSignalEvent(*event) for event in events)})
    cache = _SlotSignatureCache()
    try:
        expected = _build_slot_signature(vector)
    except StilError as expected_error:
        for _ in range(2):
            with pytest.raises(StilError) as caught:
                cache.signature(vector)
            assert (caught.value.code, str(caught.value)) == (expected_error.code, str(expected_error))
    else:
        assert cache.signature(vector) == cache.signature(vector) == expected


def test_semantic_normalization_preserves_error_order_and_alias_bit_conflicts(tmp_path):
    _, ir = _source_and_ir(tmp_path)
    base = ir.vectors[0]
    cache = _SlotSignatureCache()
    cases = [
        # Unsupported Compare char comes earlier, but the gather pass must
        # report the later invalid Drive first.
        replace(base, signal_chars={"CLK": "?", "Q": "H"}, signal_events={
            "CLK": (TimedSignalEvent(0, "Compare", "X"),),
            "Q": (TimedSignalEvent(0, "Drive", "bad"),)}),
        # Identical bit mapping is a supported shared-pin representation;
        # conflicting fixed levels must not collapse to a data-only shape.
        replace(base, signal_bit_assignments={"CLK": 0, "Q": 0}, signal_chars={"CLK": "0", "Q": "1"},
                signal_events={"CLK": (TimedSignalEvent(5, "Drive", "0"),),
                               "Q": (TimedSignalEvent(5, "Drive", "1"),)}),
        replace(base, signal_bit_assignments={"CLK": 0, "Q": 0}, signal_chars={"CLK": "Z", "Q": "1"},
                signal_events={"CLK": (TimedSignalEvent(0, "Compare", "X"),),
                               "Q": (TimedSignalEvent(0, "Drive", "1"),)}),
    ]
    for vector in cases:
        with pytest.raises(StilError) as expected:
            _build_slot_signature(vector)
        with pytest.raises(StilError) as actual:
            cache.signature(vector)
        assert (actual.value.code, str(actual.value)) == (expected.value.code, str(expected.value))


def test_seeded_random_data_uses_two_signatures_with_bounded_signal_keys(tmp_path):
    _, ir = _source_and_ir(tmp_path)
    base = ir.vectors[0]
    names = tuple(f"D{bit}" for bit in range(16)) + ("CLK", "Q")
    bits = dict(zip(names, range(len(names))))
    rng = random.Random(730041)
    cache = _SlotSignatureCache()
    for index in range(2048):
        chars = {name: rng.choice("01") for name in names[:16]}
        chars.update(CLK=str(index % 2), Q=rng.choice("HL"))
        events = {name: (TimedSignalEvent(0, "Drive", chars[name]),) for name in names[:16]}
        events.update(CLK=(TimedSignalEvent(0 if index % 2 == 0 else 5, "Drive", chars["CLK"]),),
                      Q=(TimedSignalEvent(0, "Compare", chars["Q"]),))
        vector = replace(base, signal_bit_assignments=bits, signal_chars=chars, signal_events=events)
        assert cache.signature(vector) == _build_slot_signature(vector)
    assert len(cache._signatures) == 2
    assert len(cache._signal_actions) == 36
    tiny_cache = _SlotSignatureCache(max_entries=1)
    for vector in ir.vectors:
        assert tiny_cache.signature(vector) == _build_slot_signature(vector)
    assert len(tiny_cache._signal_actions) == len(tiny_cache._signatures) == 1
