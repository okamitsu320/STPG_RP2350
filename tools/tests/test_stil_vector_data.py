"""Public, synthetic coverage of WaveformChars and character repetition."""

from __future__ import annotations

from pathlib import Path

import pytest

from stil.backend_rate import _SlotSignatureCache, _build_slot_signature, compile_rate_vector
from stil.backend_raw import compile_raw_event
from stil.errors import StilError
from stil.parser import parse_stil_text
from stil.pipeline import CompileOptions, compile_stil
from stil.preprocess import preprocess_ategen_stil
from stil.resolve import build_ir, load_pin_map, resolve_document
from stil.vector_data import MAX_VECTOR_CHARACTERS, decode_vector_data
from stpg.constants import STPG_EVF_COMPARE_POINT, STPG_EVF_SAMPLE_POINT, STPG_RATE_SLOT_SAMPLE, STPG_T_MIN_TICKS
from stpg.pattern import expand_macro_pattern, unpack_pattern, unpack_rate_vector


def source(data: str, definitions: str, *, pins: int = 2, statements: str | None = None) -> str:
    return "\n".join([
        "Signals {", *(f"  S{i} InOut;" for i in range(pins)), "}",
        "SignalGroups {", "  ALL = '" + " + ".join(f"S{i}" for i in range(pins)) + "';", "}",
        "Timing {", "  WaveformTable W {", "    Period '400ns';", "    Waveforms {",
        f"      ALL {{ {definitions} }}", "    }", "  }", "}",
        "Pattern PUBLIC {", "  W W;", statements or f"  V {{ ALL = {data}; }}", "}", "",
    ])


def make_ir(text: str, *, loop_policy: str = "expand"):
    document = parse_stil_text(text, source_file="public.stil", loop_policy=loop_policy)
    return build_ir(resolve_document(document, pin_map=load_pin_map(None, document=document),
                                     pattern_burst_name=None, pattern_name=None,
                                     loop_policy=loop_policy), tick_hz=200_000_000)


@pytest.mark.parametrize("pins", [16, 24])
@pytest.mark.parametrize("mode", ["raw-event", "auto", "macro-stream"])
@pytest.mark.parametrize("data,mask,expected", [("HXXL", 0b1001, 1), ("LXXH", 0b1001, 8)])
def test_mixed_compare_and_x_never_compares_x(tmp_path, pins, mode, data, mask, expected):
    path = tmp_path / "public.stil"
    path.write_text(source(data + "T" * (pins - len(data)),
                           "H: 0ns Compare(1); L: 0ns Compare(0); X: 0ns Compare(X); T: 0ns Compare(X);",
                           pins=pins), encoding="utf-8")
    payload, _map, report = compile_stil(CompileOptions(input_path=path, mode=mode))
    if mode == "macro-stream":
        _header, events = expand_macro_pattern(payload)
    else:
        assert report["mode_emitted"] == "raw-event"
        _header, events = unpack_pattern(payload)
    sampled = [event for event in events if event.flags & STPG_EVF_SAMPLE_POINT]
    assert len(sampled) == 1
    event = sampled[0]
    assert event.flags & STPG_EVF_COMPARE_POINT
    assert event.sample_mask == mask
    assert event.expect_value == expected
    for x_values in (0, 0b0010, 0b0100, 0b0110):
        assert ((expected | x_values) ^ event.expect_value) & event.sample_mask == 0
    # The fix must not suppress a real mismatch on either constrained pin.
    assert ((expected ^ 1) ^ event.expect_value) & event.sample_mask == 1
    assert ((expected ^ 8) ^ event.expect_value) & event.sample_mask == 8


def test_pure_x_still_captures_without_comparison():
    ir = make_ir(source("XX", "X: 0ns Compare(X);"))
    _header, events = unpack_pattern(compile_raw_event(ir, tick_hz=200_000_000).pattern_bytes)
    assert events[0].flags == STPG_EVF_SAMPLE_POINT
    assert events[0].sample_mask == 3  # preserve existing sample-only encoding


def test_x_is_excluded_per_phase_not_per_vector():
    ir = make_ir(source("HX", "H: 0ns Compare(1); X: 100ns Compare(X);"))
    _header, events = unpack_pattern(compile_raw_event(ir, tick_hz=200_000_000).pattern_bytes)
    assert [(event.flags, event.sample_mask) for event in events] == [
        (STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT, 1), (STPG_EVF_SAMPLE_POINT, 2)]


@pytest.mark.parametrize("char,definition,other", [
    ("U", "100ns Drive(1)", "u: 0ns Drive(0);"),
    ("u", "0ns Drive(1)", "U: 100ns Drive(0);"),
    ("D", "0ns Drive(0), 100ns Drive(1)", "d: 0ns Drive(1);"),
    ("d", "100ns Drive(0)", "D: 0ns Drive(1);"),
    ("p", "0ns Drive(1), 100ns Drive(0)", "P: 0ns Drive(0);"),
    ("n", "0ns Drive(0), 100ns Drive(1)", "N: 0ns Drive(1);"),
])
def test_added_drive_chars_preserve_exact_declared_edges(char, definition, other):
    ir = make_ir(source(char, f"{char}: {definition}; {other}", pins=1))
    events = ir.vectors[0].signal_events["S0"]
    assert ir.vectors[0].signal_chars["S0"] == char
    _header, raw = unpack_pattern(compile_raw_event(ir, tick_hz=200_000_000).pattern_bytes)
    # Raw events act before waiting: include the implicit prefix hold when
    # the selected selector's first edge is delayed.
    timeline = []
    tick = 0
    for event in raw:
        if event.out_enable:
            timeline.append((tick, event.out_value))
        tick += event.wait_ticks + STPG_T_MIN_TICKS
    assert timeline == [(event.time_ticks, int(event.action_arg)) for event in events]
    assert tick == 80
    result = compile_rate_vector(ir, tick_hz=200_000_000)
    _header, _rates, slots, records = unpack_rate_vector(result.pattern_bytes)
    assert [slot.time_ticks for slot in slots] == [event.time_ticks for event in events]
    assert records[0].drive_value == int(events[-1].action_arg)
    assert records[0].drive_enable == 1


@pytest.mark.parametrize("char,arg", [("h", "1"), ("l", "0"), ("h", "0"), ("l", "1")])
def test_added_compare_chars_use_selected_timing_and_action(char, arg):
    ir = make_ir(source(char + "X", f"{char}: 100ns Compare({arg}); H: 0ns Compare(1); L: 0ns Compare(0); X: 100ns Compare(X);"))
    _header, raw = unpack_pattern(compile_raw_event(ir, tick_hz=200_000_000).pattern_bytes)
    assert len(raw) == 2
    assert raw[0].flags == 0
    assert raw[0].wait_ticks + STPG_T_MIN_TICKS == 20
    assert raw[1].sample_mask == 1
    assert raw[1].expect_value == int(arg)
    _header, _rates, _slots, records = unpack_rate_vector(compile_rate_vector(ir, tick_hz=200_000_000).pattern_bytes)
    assert records[0].expect_value == int(arg)
    assert records[0].expect_mask == 1


def test_added_selector_compare_unknown_phase_remains_sample_only_and_cache_safe():
    ir = make_ir(source("h", "h: 0ns Compare(X), 100ns Compare(1);", pins=1))
    signature = _build_slot_signature(ir.vectors[0])
    cache = _SlotSignatureCache()
    assert cache.signature(ir.vectors[0]) == signature
    assert signature[0][1] & STPG_RATE_SLOT_SAMPLE
    assert signature[0][4] == 0
    assert signature[1][4] == 1


def test_new_selector_with_different_expected_levels_requires_raw():
    ir = make_ir(source("h", "h: 0ns Compare(0), 100ns Compare(1);", pins=1))
    _header, raw = unpack_pattern(compile_raw_event(ir, tick_hz=200_000_000).pattern_bytes)
    assert [event.expect_value for event in raw] == [0, 1]
    with pytest.raises(StilError, match="different expected levels"):
        compile_rate_vector(ir, tick_hz=200_000_000)
    simultaneous = make_ir(source("h", "h: 0ns Compare(0), 0ns Compare(1);", pins=1))
    with pytest.raises(StilError) as caught:
        compile_raw_event(simultaneous, tick_hz=200_000_000)
    assert caught.value.code == "ERR_CONFLICTING_COMPARE_ACTIONS"


def test_undefined_lowercase_selector_does_not_borrow_uppercase():
    with pytest.raises(StilError) as caught:
        make_ir(source("u", "U: 0ns Drive(1);", pins=1))
    assert caught.value.code == "ERR_WAVEFORM_CHAR_NOT_FOUND"


@pytest.mark.parametrize("action", ["DirDrive", "DirOutput"])
def test_compare_selector_direction_only_output_is_not_misread_as_sampling(action):
    ir = make_ir(source("h", f"h: 0ns {action}, 100ns Compare(1);", pins=1))
    with pytest.raises(StilError) as caught:
        compile_raw_event(ir, tick_hz=200_000_000)
    assert caught.value.code == "ERR_UNSUPPORTED_DIRECTION_ACTION"


@pytest.mark.parametrize("data,expected", [
    (r"\r3 01", "010101"),
    (r"\r2 01HL X", "01HL01HLX"),
    (r"1 \r2 hl 0 \r3 p", "1hlhl0ppp"),
    ("\\r2 01\n\\r1 UD", "0101UD"),
    (r"\w01 \r2 hL", "01hLhL"),
    (r"\w \r2 ud", "udud"),
    (r"\r0002 pn", "pnpn"),
])
def test_character_group_repetition(data, expected):
    assert decode_vector_data(data) == expected


@pytest.mark.parametrize("data", [r"\r0 01", r"\r-1 01", r"\r2", r"\r2 \r2 01", r"\r2Q 01",
                                      r"\r 2 01", r"\h ABC", r"\d 10", r"\l2 01", r"\r2 ?", ""])
def test_invalid_or_unimplemented_vector_encodings_are_rejected(data):
    with pytest.raises(StilError) as caught:
        decode_vector_data(data)
    assert caught.value.code == "ERR_UNSUPPORTED_PATTERN_DATA"


@pytest.mark.parametrize("data,max_width", [
    (r"\r99999999999999999999999999999999 01", None),
    ("\\r" + "9" * 5000 + " 0", None),
    (rf"\r{MAX_VECTOR_CHARACTERS} 01", None),
    (r"\r13 01", 24),
    (r"\r12 01 0", 24),
])
def test_expansion_is_bounded_before_allocating(data, max_width):
    with pytest.raises(StilError) as caught:
        decode_vector_data(data, max_width=max_width)
    assert caught.value.code == "ERR_VECTOR_DATA_LIMIT"


@pytest.mark.parametrize("mode", ["raw-event", "rate-vector", "auto"])
def test_repeat_is_binary_equivalent_to_explicit_chars(tmp_path, mode):
    path = tmp_path / "public.stil"
    definitions = "0: 0ns Drive(0); 1: 0ns Drive(1); H: 100ns Compare(1); L: 100ns Compare(0);"
    path.write_text(source(r"\r3 01HL", definitions, pins=12), encoding="utf-8")
    repeated, repeated_map, _report = compile_stil(CompileOptions(input_path=path, mode=mode))
    path.write_text(source("01HL" * 3, definitions, pins=12), encoding="utf-8")
    literal, literal_map, _report = compile_stil(CompileOptions(input_path=path, mode=mode))
    assert repeated == literal
    assert repeated_map == literal_map


@pytest.mark.parametrize("loop_policy", ["expand", "preserve"])
def test_repetition_inside_static_control_flow_and_loops(loop_policy):
    text = source("", "0: 0ns Drive(0); 1: 0ns Drive(1);", pins=4,
                  statements="Loop 3 { V { ALL = \\r2 01; } }")
    ir = make_ir(text, loop_policy=loop_policy)
    assert all(vector.signal_chars == {"S0": "0", "S1": "1", "S2": "0", "S3": "1"} for vector in ir.vectors)
    assert ir.logical_vector_count == 3


def test_repetition_in_macro_definition_and_source_provenance():
    text = source("", "0: 0ns Drive(0); 1: 0ns Drive(1);", pins=4, statements="Macro M;")
    text = text.replace("Pattern PUBLIC {", "MacroDefs { M { V { ALL = \\r2 01; } } }\nPattern PUBLIC {")
    ir = make_ir(text)
    assert len(ir.vectors) == 1
    assert ir.vectors[0].expansion_trace


def test_known_target_width_applies_in_parser_and_macro_definition():
    for statement in ["V { ALL = \\r999999 01; }", "Macro M;"]:
        text = source("", "0: 0ns Drive(0);", statements=statement)
        if statement.startswith("Macro"):
            text = text.replace("Pattern PUBLIC {", "MacroDefs { M { V { ALL = \\r999999 01; } } }\nPattern PUBLIC {")
        with pytest.raises(StilError) as caught:
            parse_stil_text(text, source_file="public.stil")
        assert caught.value.code == "ERR_VECTOR_DATA_LIMIT"
        assert "public.stil:" in str(caught.value)


def test_preprocessor_keeps_case_sensitive_waveform_selectors_and_repeat(tmp_path):
    project = tmp_path / "project.stil"
    pattern = tmp_path / "pattern.stil"
    project.write_text("\n".join([
        "Signals {", "  A InOut;", "  B InOut;", "}",
        "SignalGroups {", "  ALL = 'A + B';", "}",
        "Timing {", "  WaveformTable W {", "    Period '400ns';", "    Waveforms {",
        "      ALL { U { '100ns' U; } }", "      ALL { u { '0ns' U; } }",
        "      ALL { D { '0ns' D; '100ns' U; } }", "      ALL { d { '0ns' D; } }",
        "      ALL { h { '100ns' H; } }", "      ALL { l { '100ns' L; } }",
        "      ALL { p { '0ns' U; '100ns' D; } }", "      ALL { n { '0ns' D; '100ns' U; } }",
        "    }", "  }", "}",
    ]), encoding="utf-8")
    pattern.write_text("Pattern PUBLIC {\nW W;\nV { ALL = \\r2 u; }\nV { ALL = UD; }\n}\n", encoding="utf-8")
    result = preprocess_ategen_stil(project, pattern)
    ir = make_ir(result.output_text)
    assert ir.vectors[0].signal_events["A"][0].time_ticks == 0
    assert ir.vectors[1].signal_events["A"][0].time_ticks == 20
    assert [event.action_arg for event in ir.vectors[1].signal_events["B"]] == ["0", "1"]
