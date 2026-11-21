"""Frozen record reuse with complete keys; public synthetic IR and STIL only."""
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from stil.ast import TimedSignalEvent
import stil.backend_rate as backend
from stil.pipeline import CompileOptions, compile_stil
from stpg.pattern import VectorRecord
from helpers.benchmark_planning_speed import synthetic_source

pytestmark = pytest.mark.stpg_host_only


def vector(**changes):
    args = dict(signal_bit_assignments={"D": 0, "Q": 1},
                drive_chars={"D": "1"}, compare_chars={"Q": "H"},
                signal_events={"D": (TimedSignalEvent(0, "Drive", "1"),),
                               "Q": (TimedSignalEvent(10, "Compare", "1"),)})
    return SimpleNamespace(**(args | changes))


def test_record_cache_reuses_only_complete_semantics_and_keeps_values_frozen(monkeypatch):
    cache = backend._VectorRecordCache()
    calls = []
    cold = backend._build_vector_record
    def counted(v, rate):
        calls.append(1)
        return cold(v, rate)
    monkeypatch.setattr(backend, "_build_vector_record", counted)
    first = vector()
    expected = VectorRecord(7, 0, 1, 1, 1, 2, 2)
    assert cache.record(first, 7) == expected
    assert cache.record(vector(), 7) == expected
    assert len(calls) == 1
    with pytest.raises(FrozenInstanceError):
        cache.record(first, 7).drive_value = 0
    assert cache.record(first, 8) == replace(expected, rate_id=8)
    assert cache.record(vector(compare_chars={"Q": "L"}), 7) == replace(expected, expect_value=0)
    assert cache.record(vector(signal_bit_assignments={"D": 3, "Q": 5}), 7) == VectorRecord(7, 0, 1, 8, 8, 32, 32)
    changed = vector(signal_events={**first.signal_events, "D": (TimedSignalEvent(0, "Drive", "0"),)})
    assert cache.record(changed, 7) == replace(expected, drive_value=0)
    assert len(calls) == 5


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_record_cache_has_bounded_storage_and_an_exact_cold_fallback(budget):
    cache = backend._VectorRecordCache(budget)
    for rate in range(20):
        for char in ("H", "L", "X", "T"):
            item = vector(compare_chars={"Q": char})
            assert cache.record(item, rate) == backend._build_vector_record(item, rate)
    assert len(cache._records) <= budget


def test_invalid_manual_ir_keeps_cold_error_order_and_never_caches_errors():
    from stil.errors import StilError
    cache = backend._VectorRecordCache()
    bad = vector(signal_bit_assignments={"D": 0},  # missing Q is later than bad Drive
                 signal_events={"D": (TimedSignalEvent(0, "Drive", "bad"),)})
    errors = []
    for action in (lambda: backend._build_vector_record(bad, 0), lambda: cache.record(bad, 0)):
        with pytest.raises(StilError) as caught:
            action()
        errors.append((caught.value.code, str(caught.value)))
    assert errors[0] == errors[1] and cache._records == {}


@pytest.mark.parametrize("loops", [False, True])
@pytest.mark.parametrize("policy", ["expand", "preserve"])
def test_whole_compile_binary_map_and_report_are_unchanged_with_cache_disabled(tmp_path, monkeypatch, loops, policy):
    source = tmp_path / "public.stil"
    source.write_text(synthetic_source(80, loops=loops), encoding="utf-8")
    options = CompileOptions(input_path=source, mode="rate-vector", loop_policy=policy)
    warm = compile_stil(options)
    original = backend._VectorRecordCache.__init__
    monkeypatch.setattr(backend._VectorRecordCache, "__init__", lambda self: original(self, max_entries=0))
    assert compile_stil(options) == warm


def test_vector_action_mutant_is_detected_by_the_independent_expected_record():
    # Omitting timed actions from a key would conflate Drive(1) and Drive(0)
    # even though both vector characters and rate_id stay identical.
    first, changed = vector(), vector(signal_events={"D": (TimedSignalEvent(0, "Drive", "0"),),
                                                    "Q": (TimedSignalEvent(10, "Compare", "1"),)})
    wrong = {}
    def mutant(item):
        key = tuple(item.drive_chars.items()), tuple(item.compare_chars.items())
        return wrong.setdefault(key, backend._build_vector_record(item, 7))
    assert mutant(first) == VectorRecord(7, 0, 1, 1, 1, 2, 2)
    assert mutant(changed) != VectorRecord(7, 0, 1, 0, 1, 2, 2)
    assert backend._VectorRecordCache().record(changed, 7) == VectorRecord(7, 0, 1, 0, 1, 2, 2)
