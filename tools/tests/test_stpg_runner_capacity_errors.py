"""Encoder byte overflow through the real SDK defaults, not capacity mocks.

Only the encoder's test limit/profile shrink. Parser, backend choice, actual
packing, source analysis, segment planning, packages and validators all run.
Public synthetic inputs; no USB metadata, serial or physical device contact.
"""
from dataclasses import replace

import pytest

from stpg import constants as c, pattern
from stil import segmented_package as planner
from stil.errors import StilError
from stil.json_artifacts import read_json_artifact
from stil.runtime_capacity import DEFAULT_RUNTIME_PROFILE, RUNTIME_CAPACITY_PROFILES
from stpg_runner import build_project, init_project, prepare_run, verify_bundle
from stpg_runner.artifacts import read_json, write_json

pytestmark = pytest.mark.stpg_host_only
REGION = 256


@pytest.fixture(autouse=True)
def encoder_region(monkeypatch):
    import serial
    from serial.tools import list_ports
    def forbidden(*args, **kwargs):
        pytest.fail("capacity error tests attempted hardware contact")
    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(list_ports, "comports", forbidden)
    monkeypatch.setattr(pattern, "STPG_PSRAM_PATTERN_BYTES", REGION)
    name = "test-real-encoder-small-psram"
    monkeypatch.setitem(RUNTIME_CAPACITY_PROFILES, name,
                        replace(RUNTIME_CAPACITY_PROFILES[DEFAULT_RUNTIME_PROFILE], name=name,
                                psram_pattern_bytes=REGION, full_expansion_event_limit=1))
    return name


def project(tmp_path, profile, *, period="200ns", mode="auto", count=20, cap=None, loop=False,
            segmentation="auto", compare_time="100ns"):
    source = tmp_path / "public.stil"
    body = "V { ALL = 0H; }\nV { ALL = 1L; }\n"
    sequence = f"Loop {count // 2} {{\n{body}}}\n" if loop else body * (count // 2)
    source.write_text(f"""Signals {{
 D InOut;
 Q InOut;
}}
SignalGroups {{
 ALL = 'D + Q';
}}
Timing {{
 WaveformTable NORMAL {{
  Period '{period}';
  Waveforms {{
   D {{ 0: 0ns Drive(0); 1: 0ns Drive(1); }}
   Q {{ H: {compare_time} Compare(1); L: {compare_time} Compare(0); }}
  }}
 }}
}}
Pattern MAIN {{
 W NORMAL;
{sequence}}}
""", encoding="utf-8")
    path = init_project(source, tmp_path / "project", wait_preset="none", completion="hold",
                        compile_mode=mode, max_segment_vectors=cap, segmentation_mode=segmentation)
    raw = read_json(path)
    raw["target"]["runtime_profile"] = profile
    write_json(path, raw)
    return path


@pytest.mark.parametrize("period", ["200ns", "400us"])
@pytest.mark.parametrize("mode", ["auto", "rate-vector"])
def test_real_byte_overflow_builds_from_whole_source_to_verified_segments(tmp_path, encoder_region, period, mode):
    result = build_project(project(tmp_path, encoder_region, period=period, mode=mode))
    assert result.package_kind == "segmented"
    data = read_json(result.bundle_dir / "runner.bundle.json")
    decision = read_json(result.bundle_dir / "segmentation.decision.json")
    assert decision["capacity_codes"] == ["ERR_PSRAM_PATTERN_BYTES_LIMIT"]
    assert decision["planning_strategy"] == "capacity-first" and decision["planning_max_vectors"] is None
    expected = [item["expected"] for item in data["patterns"]]
    assert all(item["mode"] == "rate-vector" and item["size_bytes"] <= REGION for item in expected)
    assert sum(item["events"] for item in expected) == 40
    assert sum(item["samples"] for item in expected) == sum(item["compares"] for item in expected) == 20
    plan = prepare_run(result.bundle_dir, manifest_sha256=result.manifest_sha256)
    assert all(s.expected["size_bytes"] + 20 > REGION for s in plan.segments[:-1])
    assert all(int.from_bytes(s.payload[4:6], "little") == (
        c.STPG_PATTERN_VERSION_WIDE_WAIT if period == "400us" else c.STPG_PATTERN_VERSION) for s in plan.segments)
    segmented = read_json(result.bundle_dir / data["package_manifest"])
    assert segmented["pattern_contract"]["split_ranges"][0][0] == 0
    assert segmented["pattern_contract"]["split_ranges"][-1][1] == 19
    assert all(a["final_hold_state"] == b["entry_hold_state"]
               for a, b in zip(segmented["segments"], segmented["segments"][1:]))
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


@pytest.mark.parametrize("period", ["200ns", "400us"])
def test_actual_preserved_loop_that_fits_is_not_forced_to_split(tmp_path, encoder_region, period):
    result = build_project(project(tmp_path, encoder_region, period=period, loop=True))
    assert result.package_kind == "single"
    manifest = read_json(result.bundle_dir / "runner.bundle.json")
    item, = manifest["patterns"]
    assert (item["expected"]["events"], item["expected"]["samples"], item["expected"]["compares"]) == (40, 20, 20)
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


@pytest.mark.parametrize("period", ["200ns", "400us"])
@pytest.mark.parametrize("cap", [4, 1000])
def test_explicit_cap_remains_an_upper_bound_with_real_encoder_overflow(tmp_path, encoder_region, period, cap):
    result = build_project(project(tmp_path, encoder_region, period=period, cap=cap))
    data = read_json(result.bundle_dir / "runner.bundle.json")
    segmented = read_json(result.bundle_dir / data["package_manifest"])
    assert all(b - a + 1 <= cap for a, b in segmented["pattern_contract"]["split_ranges"])
    assert sum(item["expected"]["samples"] for item in data["patterns"]) == 20
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


@pytest.mark.parametrize("period", ["200ns", "400us"])
def test_single_policy_still_rejects_real_byte_overflow(tmp_path, encoder_region, period):
    path = project(tmp_path, encoder_region, period=period, segmentation="single")
    with pytest.raises(ValueError, match="source analysis is REJECT"):
        build_project(path)
    root, = (path.parent / "build").iterdir()
    assert not (root / "segmented").exists()
    assert read_json(root / "build.state.json")["status"] == "FAILED"


@pytest.mark.parametrize("period", ["200ns", "400us"])
def test_byte_overflow_does_not_hide_a_streaming_timing_error(tmp_path, encoder_region, period):
    # 8000 real events exceed the unchanged 6551-event direct path, so this
    # is a mandatory streaming timing rejection, not a small-run warning.
    path = project(tmp_path, encoder_region, period=period, compare_time="5ns", count=4000)
    with pytest.raises(ValueError, match="source analysis is REJECT"):
        build_project(path)
    root, = (path.parent / "build").iterdir()
    analysis = read_json_artifact(root / "source.analysis.json")
    candidate = analysis["capability"]["backend_candidates"]["rate-vector"]
    assert candidate["status"] == "unavailable"
    assert candidate["error"] == f"{'wide ' if period == '400us' else ''}rate vector exceeds PSRAM pattern region"
    assert candidate["streaming_timing_observation"] == "unpacked_ir_after_capacity_rejection"
    assert candidate["streaming_poc_timing_issues"] and candidate["streaming_poc_compatible"] is False
    assert not (root / "segmented").exists()


CAPACITY_TEXT = "rate vector exceeds PSRAM pattern region"
WRAPPER_TEXT = "preserved loop could not be compiled by the rate-vector backend; refusing raw-event fallback"


def wrapped(cause):
    error = StilError("ERR_LOOP_BACKEND_UNSUPPORTED", WRAPPER_TEXT)
    error.__cause__ = cause
    return error


@pytest.mark.parametrize("text,code", [
    (CAPACITY_TEXT, c.ERR_BAD_SIZE), ("wide " + CAPACITY_TEXT, c.ERR_BAD_SIZE),
    ("event_count out of range", c.ERR_PATTERN_INVALID),
    ("macro-stream exceeds Stage A size", c.ERR_BAD_SIZE),
    ("macro-stream pattern exceeds Stage A size", c.ERR_BAD_SIZE),
])
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_classifier_accepts_only_typed_capacity_leaves_and_known_wrappers(text, code, depth):
    error = pattern.PatternValidationError(text, code)
    for _ in range(depth):
        error = wrapped(error)
    assert planner._capacity_failure(error) is True


def noncapacity_error(case):
    capacity = pattern.PatternValidationError(CAPACITY_TEXT, c.ERR_BAD_SIZE)
    if case == "plain_value_error":
        return ValueError(CAPACITY_TEXT)
    if case == "io_same_text":
        return OSError(CAPACITY_TEXT)
    if case == "wrong_encoder_code":
        return pattern.PatternValidationError(CAPACITY_TEXT, c.ERR_BAD_CRC)
    if case == "substring_only":
        return pattern.PatternValidationError("not " + CAPACITY_TEXT, c.ERR_BAD_SIZE)
    if case == "known_wrapper_timing_cause":
        return wrapped(StilError("ERR_TIME_OUT_OF_RANGE", "public timing rejection"))
    if case == "known_wrapper_io_cause":
        return wrapped(OSError("public I/O rejection"))
    if case == "missing_cause":
        return wrapped(None)
    if case == "implicit_context_only":
        outer = wrapped(None)
        outer.__context__ = capacity
        return outer
    if case == "cycle":
        outer = wrapped(None); outer.__cause__ = outer
        return outer
    if case == "too_many_wrappers":
        for _ in range(8):
            capacity = wrapped(capacity)
        return capacity
    if case == "unknown_outer_wrapper":
        outer = StilError("ERR_PARSER", "public parser rejection")
        outer.__cause__ = wrapped(capacity)
        return outer
    if case == "wrong_wrapper_message":
        outer = StilError("ERR_LOOP_BACKEND_UNSUPPORTED", "some other unsupported loop")
        outer.__cause__ = capacity
        return outer
    if case == "wrong_wrapper_code":
        outer = StilError("ERR_TIME_OUT_OF_RANGE", WRAPPER_TEXT)
        outer.__cause__ = capacity
        return outer
    if case == "capacity_leaf_with_other_cause":
        capacity.__cause__ = OSError("public I/O rejection")
        return capacity
    raise AssertionError(case)


@pytest.mark.parametrize("case", [
    "plain_value_error", "io_same_text", "wrong_encoder_code", "substring_only",
    "known_wrapper_timing_cause", "known_wrapper_io_cause", "missing_cause",
    "implicit_context_only", "cycle", "too_many_wrappers", "unknown_outer_wrapper",
    "wrong_wrapper_message", "wrong_wrapper_code", "capacity_leaf_with_other_cause",
])
def test_unrelated_errors_do_not_turn_into_a_range_search(tmp_path, encoder_region, monkeypatch, case):
    error = noncapacity_error(case)
    path = project(tmp_path, encoder_region)
    calls = []
    def failed_probe(options):
        calls.append(options.vector_range)
        raise error
    monkeypatch.setattr(planner, "_compile_stil_binary_probe", failed_probe)
    with pytest.raises(planner.SegmentedPackageError, match="capacity-first probe rejected"):
        build_project(path)
    assert calls == [(0, 19)]  # no shorter-range retry after an unrelated failure
    root, = (path.parent / "build").iterdir()
    assert not (root / "runner.bundle.json").exists()
    assert read_json(root / "build.state.json")["status"] == "FAILED"
    assert planner._capacity_failure(error) is False


@pytest.mark.parametrize("mutation", ["no_wrapper", "wide_source_not_recognized", "arbitrary_cause_accepted"])
def test_acceptance_checks_detect_reintroduced_regressions(tmp_path, encoder_region, monkeypatch, mutation):
    """Weaken live functions then run the same real-build assertions.

    A passing unmodified control exists above; no production code is edited.
    """
    if mutation == "no_wrapper":
        original = planner._capacity_failure
        monkeypatch.setattr(planner, "_capacity_failure", lambda exc: isinstance(exc, pattern.PatternValidationError)
                            and original(exc))
        with pytest.raises(ValueError, match="capacity-first probe rejected"):
            test_real_byte_overflow_builds_from_whole_source_to_verified_segments(tmp_path, encoder_region, "200ns", "auto")
    elif mutation == "wide_source_not_recognized":
        import stpg_runner.segmentation as selection
        monkeypatch.delitem(selection._CAPACITY_ERRORS["rate-vector"], "wide " + CAPACITY_TEXT)
        with pytest.raises(ValueError, match="source analysis is REJECT"):
            test_real_byte_overflow_builds_from_whole_source_to_verified_segments(tmp_path, encoder_region, "400us", "auto")
    else:
        monkeypatch.setattr(planner, "_capacity_failure", lambda exc: True)
        with pytest.raises(AssertionError):
            test_unrelated_errors_do_not_turn_into_a_range_search(tmp_path, encoder_region, monkeypatch,
                                                                  "unknown_outer_wrapper")
