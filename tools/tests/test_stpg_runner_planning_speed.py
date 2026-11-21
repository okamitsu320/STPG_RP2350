"""Public real-encoder cost/equality checks, never a timing-only PASS oracle."""
from dataclasses import replace
from decimal import Decimal
import gc
import weakref

import pytest

import stil.resolve as resolver
import stil.segmented_package as planner
import stpg.pattern as binary
from stil.errors import StilError
from stil.json_artifacts import read_json_artifact
from stil.pipeline import CompileOptions, compile_stil
from stil.prepared_source import PreparedStilSource
from stpg_runner import build_project, init_project, verify_bundle
from helpers.benchmark_planning_speed import synthetic_source

pytestmark = pytest.mark.stpg_host_only


@pytest.mark.parametrize("loops", [False, True])
def test_retained_resolution_lowers_once_and_matches_uncached_full_and_ranges(tmp_path, monkeypatch, loops):
    source = tmp_path / "public.stil"
    source.write_text(synthetic_source(80, loops=loops), encoding="utf-8")
    ctx = PreparedStilSource(source, loop_policy="preserve", retain_full_resolution=True)
    original = resolver.lower_pattern_control_flow
    lowerings = []
    def lower(*args, **kw):
        lowerings.append(1)
        return original(*args, **kw)
    monkeypatch.setattr(resolver, "lower_pattern_control_flow", lower)
    selected = [(0, 0), (1, 6), (3, 25), (34, 55), (79, 79)]
    for span in selected:
        assert ctx.resolve(span) == PreparedStilSource(source, loop_policy="preserve").resolve(span)
        ctx.clear_resolution()
    # Five cold reference resolutions above; the retained source lowered once.
    assert len(lowerings) == len(selected) + 1
    assert ctx.logical_vector_count() == 80 and ctx._resolved is None
    assert ctx._flat_preserved_source() is not loops
    for span in selected:
        options = CompileOptions(input_path=source, mode="rate-vector", loop_policy="preserve",
                                 vector_range=span, time_scale=Decimal("1.1"), allow_approx=True)
        assert compile_stil(replace(options, prepared_source=ctx)) == compile_stil(options)


def test_reuse_keeps_only_one_range_and_release_drops_the_whole_source(tmp_path):
    source = tmp_path / "public.stil"
    source.write_text(synthetic_source(32), encoding="utf-8")
    ctx = PreparedStilSource(source, loop_policy="preserve", retain_full_resolution=True)
    old_range = weakref.ref(ctx.resolve((0, 2)))
    full = weakref.ref(ctx._full_resolved)
    ctx.resolve((10, 12))
    gc.collect()
    assert old_range() is None and full() is not None
    ctx.clear_resolution()
    assert ctx._resolved is None and ctx._ir is None and ctx._rate_result is None
    ctx.release()
    gc.collect()
    assert full() is None and ctx._document is None and ctx._pin_map is None


@pytest.mark.parametrize("loop_policy,retain", [("expand", False), ("expand", True), ("preserve", False)])
def test_default_or_expanded_sources_do_not_gain_a_full_resolution_cache(tmp_path, loop_policy, retain):
    source = tmp_path / "public.stil"
    source.write_text(synthetic_source(80, loops=True), encoding="utf-8")
    ctx = PreparedStilSource(source, loop_policy=loop_policy, retain_full_resolution=retain)
    ctx.resolve((0, 0))
    assert ctx._full_resolved is None and not ctx._flat_preserved_source()


@pytest.mark.parametrize("release", [False, True])
def test_same_size_source_changes_poison_the_retained_source_even_after_release(tmp_path, release):
    source = tmp_path / "public.stil"
    original = synthetic_source(32)
    source.write_text(original, encoding="utf-8")
    ctx = PreparedStilSource(source, loop_policy="preserve", retain_full_resolution=True)
    ctx.resolve((0, 0))
    if release:
        ctx.release()
    source.write_text(original.replace("00000000;", "00000001;", 1), encoding="utf-8")
    assert len(source.read_text()) == len(original) and source.read_text() != original
    with pytest.raises(StilError) as caught:
        ctx.resolve((10, 15))
    assert caught.value.code == "ERR_PREPARED_SOURCE_CHANGED"
    assert ctx._full_resolved is None
    source.write_text(original, encoding="utf-8")
    with pytest.raises(StilError):
        ctx.resolve((0, 0))


def test_invalid_signal_outside_the_range_is_not_hidden_by_reuse(tmp_path):
    source = tmp_path / "public.stil"
    text = synthetic_source(16).removesuffix("}\n") + "V { UNKNOWN = 1; }\n}\n"
    source.write_text(text, encoding="utf-8")
    ctx = PreparedStilSource(source, loop_policy="preserve", retain_full_resolution=True)
    with pytest.raises(StilError) as caught:
        ctx.resolve((0, 0))
    assert caught.value.code == "ERR_SIGNAL_NOT_FOUND"


def test_retained_source_is_not_reused_after_a_same_size_pin_map_edit(tmp_path):
    from stpg_runner.artifacts import write_json
    source, pins = tmp_path / "public.stil", tmp_path / "pins.json"
    source.write_text(synthetic_source(16), encoding="utf-8")
    write_json(pins, {"active_pins": 16, "signals": {f"A{i}": {"bit": i} for i in range(8)}})
    ctx = PreparedStilSource(source, pin_map_path=pins, loop_policy="preserve", retain_full_resolution=True)
    ctx.resolve((0, 0))
    old = pins.read_bytes()
    pins.write_bytes(old.replace(b'"bit":7', b'"bit":8'))
    assert len(old) == pins.stat().st_size and pins.read_bytes() != old
    with pytest.raises(StilError) as caught:
        ctx.resolve((1, 1))
    assert caught.value.code == "ERR_PREPARED_SOURCE_CHANGED" and ctx._full_resolved is None


def test_retained_source_is_not_mutated_by_time_or_marker_variants(tmp_path):
    from test_prepared_source import source_file
    from stil.markers import parse_marker_specs
    from stpg_runner.artifacts import write_json
    source = source_file(tmp_path)
    pins = tmp_path / "pins.json"
    write_json(pins, {"active_pins": 16, "signals": {
        "CLK": {"bit": 0}, "Q": {"bit": 1}, "MARK": {"bit": 2, "role": "marker"}}})
    ctx = PreparedStilSource(source, pin_map_path=pins, loop_policy="preserve", retain_full_resolution=True)
    base = CompileOptions(input_path=source, pin_map_path=pins, mode="rate-vector", loop_policy="preserve",
                          vector_range=(0, 7))
    for options in (replace(base, time_scale=Decimal("1.2"), markers=parse_marker_specs(["MARK:vector=0:mode=pulse"], [])),
                    replace(base, tick_hz=150000000), base):
        assert compile_stil(replace(options, prepared_source=ctx)) == compile_stil(options)
    assert all("MARK" not in item.signal_chars for item in ctx._full_resolved.vectors)


def test_reuse_does_not_override_the_wait_service_pressure_guard(tmp_path, monkeypatch):
    source = tmp_path / "public.stil"
    source.write_text(synthetic_source(80, loops=True), encoding="utf-8")
    monkeypatch.setattr(binary, "STPG_PSRAM_PATTERN_BYTES", 400)
    project = init_project(source, tmp_path / "p", wait_preset="chunk-4ms", completion="hold",
                           service_us=50, service_source="deliberately excessive public test assumption",
                           service_scope="loop-generic", max_total_wait_us=1e11)
    with pytest.raises(ValueError, match="underflow pressure"):
        build_project(project)


@pytest.mark.parametrize("wait,wide,loops,cap", [(False, False, False, None), (True, False, False, None),
                                                (True, True, False, None), (False, False, False, 300),
                                                (True, False, True, None)])
def test_faster_search_and_bisection_emit_identical_real_segment_binaries(tmp_path, monkeypatch, wait, wide, loops, cap):
    source = tmp_path / "public.stil"
    source.write_text(synthetic_source(80 if loops else (6000 if wait else 800), loops=loops, wide=wide), encoding="utf-8")
    monkeypatch.setattr(binary, "STPG_PSRAM_PATTERN_BYTES", 400 if loops else (50000 if wait else 10000))
    options = dict(wait_preset="chunk-4ms" if wait else "none", completion="hold", max_segment_vectors=cap)
    if wait:
        options.update(service_us=0.5 if loops else 50, service_source="public declared test assumption, not measured",
                       service_scope="loop-generic" if loops else "non-loop", max_total_wait_us=1e11)
    outputs, reports = [], []
    for name in ("bisection", "optimized"):
        with monkeypatch.context() as scope:
            if name == "bisection":
                scope.setattr(PreparedStilSource, "_flat_preserved_source", lambda self: False)
            project = init_project(source, tmp_path / name, **options)
            built = build_project(project)
        reports.append(read_json_artifact(built.bundle_dir / "segmented/nominal/segmented.manifest.json")["capacity_plan"])
        outputs.append([p.read_bytes() for p in sorted(built.bundle_dir.glob("segmented/nominal/segment-*/package/pattern.bin"))])
        assert verify_bundle(built.bundle_dir)["status"] == "PASS"
    assert reports[0]["ranges"] == reports[1]["ranges"] and outputs[0] == outputs[1]
    if not loops and cap is None:
        assert reports[1]["probe_count"] < reports[0]["probe_count"]
    if loops:
        assert reports[1]["probe_count"] == reports[0]["probe_count"]  # conservative no-interpolation path


def test_interpolation_witness_is_really_probed_through_the_first_failing_next_vector(tmp_path, monkeypatch):
    source = tmp_path / "public.stil"
    source.write_text(synthetic_source(1400), encoding="utf-8")
    monkeypatch.setattr(binary, "STPG_PSRAM_PATTERN_BYTES", 10000)
    original = planner._compile_stil_binary_probe
    seen = []
    def probe(options):
        seen.append(options.vector_range)
        return original(options)
    monkeypatch.setattr(planner, "_compile_stil_binary_probe", probe)
    project = init_project(source, tmp_path / "p", wait_preset="none", completion="hold")
    built = build_project(project)
    plan = read_json_artifact(built.bundle_dir / "segmented/nominal/segmented.manifest.json")["capacity_plan"]
    for start, end in plan["ranges"][:-1]:
        assert (start, end) in seen and (start, end + 1) in seen
    assert plan["probe_count"] == len(seen) <= 5 * len(plan["ranges"])


def test_synchronous_build_decode_reuse_cannot_hide_disk_corruption(tmp_path, monkeypatch):
    source = tmp_path / "public.stil"
    source.write_text(synthetic_source(32), encoding="utf-8")
    calls = []
    original = binary._decode_rate_vector_full
    def decode(payload):
        calls.append(bytes(payload))
        return original(payload)
    monkeypatch.setattr(binary, "_decode_rate_vector_full", decode)
    from stil.pattern_package import PatternPackageOptions, build_pattern_package, verify_pattern_package
    output = tmp_path / "package"
    assert build_pattern_package(PatternPackageOptions(input_path=source, output_dir=output,
                                                       mode="rate-vector", loop_policy="preserve"))["status"] == "VERIFIED"
    payload = (output / "pattern.bin").read_bytes()
    assert calls.count(payload) == 1
    assert binary._decode_scope.get() is None
    bad = bytearray(payload)
    bad[-1] ^= 1
    (output / "pattern.bin").write_bytes(bad)
    assert verify_pattern_package(output)["status"] == "REJECT"
