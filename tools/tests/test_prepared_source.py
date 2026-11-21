"""Portable host tests for hash-bound, one-conversion preparation reuse."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import weakref
import gc

import pytest

from stil.errors import StilError
from stil.markers import parse_marker_specs
from stil.pipeline import CompileOptions, compile_stil
from stil.prepared_source import PreparedStilSource
import stil.prepared_source as prepared_module
import stil.pipeline as pipeline_module
import stil.source_analysis as analysis_module
from stil.resolve import build_ir, load_pin_map, resolve_document, slice_resolved_pattern
from stil.source_analysis import analyze_stil_source


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


def source_file(tmp_path, *, loop=False):
    source = tmp_path / "source.stil"
    sequence = "  Loop 8 {\n" + BODY + "  }\n" if loop else BODY * 8
    source.write_text(HEADER + "Pattern MAIN {\n  W T;\n" + sequence + "}\n", encoding="utf-8")
    return source


@pytest.mark.parametrize("loop_policy", ["expand", "preserve"])
@pytest.mark.parametrize("selection", [None, (0, 0), (2, 11), (23, 23)])
def test_range_resolution_matches_full_resolution_then_slice(tmp_path, loop_policy, selection):
    source = source_file(tmp_path, loop=True)
    ctx = PreparedStilSource(source, loop_policy=loop_policy)
    document = ctx.document()
    pin_map = load_pin_map(None, document=document)
    full = resolve_document(document, pin_map=pin_map, pattern_burst_name=None,
                            pattern_name=None, loop_policy=loop_policy)
    expected = slice_resolved_pattern(full, start=selection[0], end=selection[1]) if selection else full
    assert ctx.resolve(selection) == expected


@pytest.mark.parametrize("mode", ["rate-vector", "raw-event", "auto"])
@pytest.mark.parametrize("tick_hz", [150_000_000, 200_000_000])
def test_prepared_compile_and_analysis_have_identical_binary_map_report(tmp_path, mode, tick_hz):
    source = source_file(tmp_path)
    ctx = PreparedStilSource(source)
    options = CompileOptions(input_path=source, mode=mode, tick_hz=tick_hz,
                              vector_range=(2, 10), time_scale=Decimal("1.1"))
    analysis_options = dict(mode=mode, tick_hz=tick_hz, vector_range=(2, 10), time_scale=Decimal("1.1"))
    assert analyze_stil_source(source, **analysis_options, prepared_source=ctx) == analyze_stil_source(source, **analysis_options)
    assert compile_stil(replace(options, prepared_source=ctx)) == compile_stil(options)


def test_preparation_parses_once_reuses_only_latest_range_and_keeps_rate_keys_shared(tmp_path, monkeypatch):
    source = source_file(tmp_path)
    counts = {"parse": 0, "resolve": 0}
    parse_original, resolve_original = prepared_module.parse_stil_file, prepared_module.resolve_document

    def parse(*args, **kwargs):
        counts["parse"] += 1
        return parse_original(*args, **kwargs)

    def resolve(*args, **kwargs):
        counts["resolve"] += 1
        return resolve_original(*args, **kwargs)

    monkeypatch.setattr(prepared_module, "parse_stil_file", parse)
    monkeypatch.setattr(prepared_module, "resolve_document", resolve)
    ctx = PreparedStilSource(source)
    analyze_stil_source(source, mode="rate-vector", vector_range=(0, 7), prepared_source=ctx)
    compile_stil(CompileOptions(input_path=source, mode="rate-vector", vector_range=(0, 7), prepared_source=ctx))
    assert counts == {"parse": 1, "resolve": 1}
    previous = weakref.ref(ctx.resolve((0, 7)))
    current = ctx.resolve((8, 15))
    assert previous() is None
    ir = build_ir(current, tick_hz=200_000_000)
    assert all(vector.rate_key is ir.vectors[0].rate_key for vector in ir.vectors)
    assert counts == {"parse": 1, "resolve": 2}
    ctx.clear_resolution()
    assert ctx._resolved is None


@pytest.mark.parametrize("change_pinmap", [False, True])
def test_same_size_content_changes_reject_cache_hits_and_poison_context(tmp_path, change_pinmap):
    source = source_file(tmp_path)
    pinmap = tmp_path / "pins.json"
    pinmap.write_text(json.dumps({"active_pins": 16, "signals": {"CLK": {"bit": 0}, "Q": {"bit": 1}}}), encoding="utf-8")
    ctx = PreparedStilSource(source, pin_map_path=pinmap)
    ctx.resolve((0, 3))
    target = pinmap if change_pinmap else source
    original = target.read_text()
    changed = original.replace('"bit": 1', '"bit": 2') if change_pinmap else original.replace("PH", "PL", 1)
    assert len(changed) == len(original) and changed != original
    target.write_text(changed, encoding="utf-8")
    with pytest.raises(StilError) as caught:
        ctx.resolve((0, 3))
    assert caught.value.code == "ERR_PREPARED_SOURCE_CHANGED"
    target.write_text(original, encoding="utf-8")
    with pytest.raises(StilError, match="cannot be reused"):
        ctx.resolve((0, 3))


@pytest.mark.parametrize("changed", ["input_path", "pattern", "pattern_burst", "loop_policy", "pin_map_path"])
def test_prepared_binding_rejects_different_paths_and_selection_options(tmp_path, changed):
    source = source_file(tmp_path)
    ctx = PreparedStilSource(source)
    options = CompileOptions(input_path=source, mode="rate-vector", prepared_source=ctx)
    values = {"input_path": tmp_path / "other.stil", "pattern": "OTHER", "pattern_burst": "PB",
              "loop_policy": "preserve", "pin_map_path": tmp_path / "other.json"}
    with pytest.raises(StilError) as caught:
        compile_stil(replace(options, **{changed: values[changed]}))
    assert caught.value.code == "ERR_PREPARED_SOURCE_BINDING"


def test_invalid_signal_outside_selected_range_is_not_hidden(tmp_path):
    source = source_file(tmp_path)
    source.write_text(source.read_text().replace(BODY * 8, BODY * 7 + "  V { UNKNOWN = 1; }\n"), encoding="utf-8")
    ctx = PreparedStilSource(source)
    for prepared in (None, ctx):
        with pytest.raises(StilError) as caught:
            compile_stil(CompileOptions(input_path=source, mode="rate-vector", vector_range=(0, 2), prepared_source=prepared))
        assert caught.value.code == "ERR_SIGNAL_NOT_FOUND"


def test_out_of_range_analysis_error_report_keeps_full_lowered_counts(tmp_path):
    source = source_file(tmp_path)
    expected = analyze_stil_source(source, mode="rate-vector", vector_range=(0, 999))
    actual = analyze_stil_source(source, mode="rate-vector", vector_range=(0, 999), prepared_source=PreparedStilSource(source))
    assert actual == expected


def test_timing_and_markers_are_recomputed_without_mutating_cached_resolution(tmp_path):
    source = source_file(tmp_path)
    pinmap = tmp_path / "pins.json"
    pinmap.write_text(json.dumps({"active_pins": 16, "signals": {
        "CLK": {"bit": 0}, "Q": {"bit": 1}, "MARK": {"bit": 2, "role": "marker"}}}), encoding="utf-8")
    ctx = PreparedStilSource(source, pin_map_path=pinmap)
    base = CompileOptions(input_path=source, pin_map_path=pinmap, mode="rate-vector", vector_range=(0, 7))
    for options in (replace(base, tick_hz=150_000_000),
                    replace(base, tick_hz=200_000_000, time_scale=Decimal("1.2"),
                            markers=parse_marker_specs(["MARK:vector=0:mode=pulse"], [])), base):
        assert compile_stil(replace(options, prepared_source=ctx)) == compile_stil(options)
    assert all("MARK" not in item.signal_chars for item in ctx.resolve((0, 7)).vectors)


def test_input_change_during_parse_is_rejected(tmp_path, monkeypatch):
    source = source_file(tmp_path)
    original_text = source.read_text()
    original = prepared_module.parse_stil_file

    def parse_and_change(*args, **kwargs):
        document = original(*args, **kwargs)
        source.write_text(source.read_text().replace("PH", "PL", 1), encoding="utf-8")
        return document

    monkeypatch.setattr(prepared_module, "parse_stil_file", parse_and_change)
    ctx = PreparedStilSource(source)
    with pytest.raises(StilError) as caught:
        ctx.resolve((0, 2))
    assert caught.value.code == "ERR_PREPARED_SOURCE_CHANGED"
    source.write_text(original_text, encoding="utf-8")
    with pytest.raises(StilError, match="cannot be reused"):
        ctx.document()


def test_selected_range_ir_error_keeps_legacy_full_lowered_counts(tmp_path):
    source = source_file(tmp_path)
    source.write_text(source.read_text().replace("PH", "0H", 1), encoding="utf-8")
    expected = analyze_stil_source(source, mode="rate-vector", vector_range=(0, 2))
    assert expected["status"] == "REJECT"
    assert analyze_stil_source(source, mode="rate-vector", vector_range=(0, 2),
                               prepared_source=PreparedStilSource(source)) == expected


@pytest.mark.parametrize("loop_policy", ["expand", "preserve"])
def test_lightweight_count_and_selected_resolution_follow_macro_goto_and_stop(tmp_path, loop_policy):
    source = tmp_path / "control.stil"
    source.write_text(HEADER + """MacroDefs {
  M0 { V { ALL = PH; } V { ALL = NL; } }
}
Procedures {
  P0 { V { ALL = PX; } Stop; V { ALL = PH; } }
}
Pattern MAIN {
  W T;
  Macro M0;
  Goto tail;
  V { ALL = PH; }
  tail: Loop 3 { V { ALL = NL; } }
  Call P0;
  V { ALL = PH; }
}
""", encoding="utf-8")
    ctx = PreparedStilSource(source, loop_policy=loop_policy)
    assert ctx.logical_vector_count() == 6
    options = CompileOptions(input_path=source, mode="rate-vector", loop_policy=loop_policy, vector_range=(1, 4))
    assert compile_stil(replace(options, prepared_source=ctx)) == compile_stil(options)
    assert analyze_stil_source(source, mode="rate-vector", loop_policy=loop_policy, vector_range=(1, 4),
                               prepared_source=ctx) == analyze_stil_source(
        source, mode="rate-vector", loop_policy=loop_policy, vector_range=(1, 4))


@pytest.mark.parametrize("loop_policy", ["expand", "preserve"])
def test_count_validates_full_source_without_caching_expanded_resolution(tmp_path, monkeypatch, loop_policy):
    source = source_file(tmp_path, loop=True)
    ctx = PreparedStilSource(source, loop_policy=loop_policy)
    assert ctx.logical_vector_count() == 24
    assert ctx._resolved is None
    assert ctx.logical_vector_count() == 24
    ctx.release()
    assert ctx._document is None
    assert ctx._pin_map is None
    source.write_text(source.read_text().replace("PH", "PL", 1), encoding="utf-8")
    with pytest.raises(StilError) as caught:
        ctx.resolve((0, 1))
    assert caught.value.code == "ERR_PREPARED_SOURCE_CHANGED"


@pytest.mark.parametrize("loop_policy", ["expand", "preserve"])
def test_prepared_analysis_compile_build_ir_and_rate_encoding_once(tmp_path, monkeypatch, loop_policy):
    source = source_file(tmp_path, loop=True)
    counts = {"ir": 0, "rate": 0}
    original_ir, original_rate = prepared_module.build_ir_with_scale, analysis_module.compile_rate_vector

    def build_ir(*args, **kwargs):
        counts["ir"] += 1
        return original_ir(*args, **kwargs)

    def encode(*args, **kwargs):
        counts["rate"] += 1
        return original_rate(*args, **kwargs)

    monkeypatch.setattr(prepared_module, "build_ir_with_scale", build_ir)
    monkeypatch.setattr(analysis_module, "compile_rate_vector", encode)
    monkeypatch.setattr(pipeline_module, "compile_rate_vector", encode)
    ctx = PreparedStilSource(source, loop_policy=loop_policy)
    report = analyze_stil_source(source, mode="rate-vector", loop_policy=loop_policy, prepared_source=ctx)
    assert report["status"] != "REJECT"
    ir_reference, encoded_reference = weakref.ref(ctx._ir), weakref.ref(ctx._rate_result)
    actual = compile_stil(CompileOptions(input_path=source, mode="rate-vector", loop_policy=loop_policy, prepared_source=ctx))
    assert counts == {"ir": 1, "rate": 1}
    assert ctx._rate_result is None and ctx._rate_options is None
    gc.collect()
    assert encoded_reference() is None
    ctx.clear_resolution()
    gc.collect()
    assert ir_reference() is None and encoded_reference() is None
    assert compile_stil(CompileOptions(input_path=source, mode="rate-vector", loop_policy=loop_policy)) == actual


@pytest.mark.parametrize("change", ["tick", "scale", "range", "rates", "shaping", "markers"])
def test_analysis_encoding_not_reused_for_changed_compile_inputs(tmp_path, monkeypatch, change):
    source = source_file(tmp_path)
    pinmap = tmp_path / "pins.json"
    pinmap.write_text(json.dumps({"active_pins": 16, "signals": {
        "CLK": {"bit": 0}, "Q": {"bit": 1}, "MARK": {"bit": 2, "role": "marker"}}}), encoding="utf-8")
    ctx = PreparedStilSource(source, pin_map_path=pinmap)
    original = analysis_module.compile_rate_vector
    calls = []

    def encode(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(analysis_module, "compile_rate_vector", encode)
    monkeypatch.setattr(pipeline_module, "compile_rate_vector", encode)
    analyze_stil_source(source, mode="rate-vector", pin_map_path=pinmap, prepared_source=ctx)
    base = CompileOptions(input_path=source, mode="rate-vector", pin_map_path=pinmap)
    changed = {
        "tick": {"tick_hz": 150_000_000}, "scale": {"time_scale": Decimal("1.1")},
        "range": {"vector_range": (0, 7)}, "rates": {"max_rates": 32},
        "shaping": {"practical_chunk_shaping": True, "practical_chunk_shaping_target_range": (0, 0),
                    "practical_chunk_shaping_max_run_vectors": 2},
        "markers": {"markers": parse_marker_specs(["MARK:vector=0:mode=pulse"], [])},
    }
    options = replace(base, **changed[change])
    actual = compile_stil(replace(options, prepared_source=ctx))
    assert len(calls) == 2
    assert compile_stil(options) == actual


def test_analysis_approximation_permission_cannot_override_strict_compile(tmp_path):
    source = source_file(tmp_path)
    source.write_text(source.read_text().replace("5us", "5.001us"), encoding="utf-8")
    ctx = PreparedStilSource(source)
    assert analyze_stil_source(source, mode="rate-vector", prepared_source=ctx)["status"] != "REJECT"
    assert ctx._ir.approximate_reasons
    assert ctx._rate_result is not None  # the permissive analysis probe succeeded
    for prepared in (None, ctx):
        with pytest.raises(StilError) as caught:
            compile_stil(CompileOptions(input_path=source, mode="rate-vector", prepared_source=prepared))
        assert caught.value.code == "ERR_APPROXIMATION_REQUIRED"


def test_input_change_rejects_and_releases_warm_ir_and_encoding(tmp_path):
    source = source_file(tmp_path)
    ctx = PreparedStilSource(source)
    analyze_stil_source(source, mode="rate-vector", prepared_source=ctx)
    assert ctx._ir is not None and ctx._rate_result is not None
    source.write_text(source.read_text().replace("PH", "PL", 1), encoding="utf-8")
    with pytest.raises(StilError) as caught:
        compile_stil(CompileOptions(input_path=source, mode="rate-vector", prepared_source=ctx))
    assert caught.value.code == "ERR_PREPARED_SOURCE_CHANGED"
    assert ctx._ir is None and ctx._rate_result is None
