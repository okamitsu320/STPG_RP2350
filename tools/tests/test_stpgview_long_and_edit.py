"""Long looped STIL in the viewer, and the signal/character edits (host only, public data).

The viewer used to resolve a STIL with every Loop expanded. The compiler bounds static expansion at 1,100,000 vector
occurrences (``static Pattern expansion exceeds ...``), so a generated pattern whose Loops expand past that could be
built by the runner (Loops preserved) but not opened in the viewer. The viewer now resolves with Loops kept, as the
compiler's default does, interprets each distinct row once and repeats Loop bodies as row ids. These tests check that
the result is the same trace as full expansion where both work, that a pattern past the bound opens, and the new edit
operations: a character change (also drive <-> compare on a bidirectional pin), signal rename, add and remove, with
pin maps that keep every other signal on its bit, and STIL export.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stil.pipeline import CompileOptions, compile_stil
from stpgview import load_stil as load_stil_module
from stpgview.edit import EditError, PreviewMap, StilEditor
from stpgview.load_bin import load_bin
from stpgview.load_stil import load_stil
from stpgview.model import KIND_COMPARE, KIND_DRIVE
from stpgview.physical.patterns import counter, counter_pins
from stpgview.session import Session

from test_stpgview import ATE_PATTERN, ATE_PROJECT, timelines

pytestmark = pytest.mark.stpg_host_only
FIXTURES = Path(__file__).parent / "fixtures" / "stil"
F8 = FIXTURES / "vf_c10lp_f8_bidir_turnaround_200ns.stil"  # 24 InOut/Out signals, H/L/T/Z on the io pins
SELECTORS = Path(__file__).parent / "fixtures" / "stpgview" / "new_selectors.stil"  # 4 signals, \rN data
LOOPED = ["loop_boundary_exit_direct_regression.stil", "vf_c10lp_f5_enable_hold_resume_200ns.stil",
          "vf_c10lp_f13_medium_continuous_lfsr_200ns.stil"]


def looped(blocks: int, count: int) -> str:
    """The public counter's header; each block is one vector and a Loop of two vectors."""

    head = counter(1).split("Pattern MAIN {")[0]
    body = []
    for i in range(blocks):
        a, b = format(i % 128, "07b"), format(i * 7 % 128, "07b")
        body.append(f"  V {{ ALL = {a}0; }}\n  Loop {count} {{\n    V {{ ALL = {a}1; }}\n    V {{ ALL = {b}0; }}\n  }}\n")
    return head + "Pattern MAIN {\n  W NORMAL;\n" + "".join(body) + "}\n"


def same_trace(a, b) -> None:
    assert a.vector_count == b.vector_count and a.total_ps == b.total_ps
    assert [(r.name, r.period_ps) for r in a.rates] == [(r.name, r.period_ps) for r in b.rates]
    for i in range(a.vector_count):
        assert (a.starts[i], a.periods[i], a.rate_ids[i], a.lines[i], a.chars[i], a.start_values[i],
                a.start_enables[i], a.extra_ps[i], a.labels.get(i)) == \
               (b.starts[i], b.periods[i], b.rate_ids[i], b.lines[i], b.chars[i], b.start_values[i],
                b.start_enables[i], b.extra_ps[i], b.labels.get(i)), i
        assert a.events(i) == b.events(i)


@pytest.fixture
def expanded(monkeypatch):
    """load_stil as before: every Loop expanded."""

    original = load_stil_module._parse_and_resolve

    def expand_only(path, pin_map, burst, pattern):
        from stil.parser import parse_stil_file
        from stil.resolve import load_pin_map, resolve_document

        document = parse_stil_file(path, loop_policy="expand")
        pins = load_pin_map(pin_map, document=document)
        return document, pins, resolve_document(document, pin_map=pins, pattern_burst_name=burst,
                                                pattern_name=pattern, loop_policy="expand")

    def load(path):
        monkeypatch.setattr(load_stil_module, "_parse_and_resolve", expand_only)
        try:
            return load_stil(path)
        finally:
            monkeypatch.setattr(load_stil_module, "_parse_and_resolve", original)

    return load


@pytest.mark.parametrize("name", LOOPED + ["vf_c10lp_f8_bidir_turnaround_200ns.stil", "pn_norm.stil"])
def test_loops_kept_give_the_same_trace_as_full_expansion(expanded, name):
    same_trace(load_stil(FIXTURES / name), expanded(FIXTURES / name))


def test_a_public_looped_pattern_matches_expansion(tmp_path, expanded):
    source = tmp_path / "looped.stil"
    source.write_text(looped(300, 7))
    same_trace(load_stil(source), expanded(source))


def test_a_pattern_past_the_static_expansion_bound_opens(tmp_path, expanded):
    source = tmp_path / "long.stil"
    source.write_text(looped(40, 15_000))  # 40 * (1 + 2 * 15,000) = 1,200,040 vectors from 120 statements
    with pytest.raises(Exception, match="static Pattern expansion exceeds"):
        expanded(source)
    trace = load_stil(source)
    assert trace.vector_count == 1_200_040 and trace.total_ps == 1_200_040 * 400_000
    assert len(trace.chars.rows) <= 120  # one string per distinct row
    # the last vector: block 39's Loop, second body vector; its line is the source line of that statement
    last = trace.vector_count - 1
    assert trace.chars[last] == format(39 * 7 % 128, "07b") + "0"
    assert trace.detail(last)["source_text"] == f"V {{ ALL = {format(39 * 7 % 128, '07b')}0; }}"
    session = Session(tmp_path / "work")
    (trace_id,) = session.load([source])
    summary = session.wave(session.get(trace_id), 0, trace.total_ps, 500)
    assert summary["mode"] == "summary" and len(summary["rates"]) == 500
    window = session.wave(session.get(trace_id), trace.starts[last - 3], trace.total_ps, 500)
    assert window["mode"] == "events" and window["vectors"][-1][0] == last


def test_too_long_patterns_are_refused_with_the_count(tmp_path, monkeypatch):
    monkeypatch.setattr(load_stil_module, "MAX_VIEW_VECTORS", 1000)
    source = tmp_path / "long.stil"
    source.write_text(looped(2, 1000))
    with pytest.raises(Exception, match="expands to 4,002 vectors; the viewer shows at most 1,000"):
        load_stil(source)


# ---------------------------------------------------------------------------
# character changes
# ---------------------------------------------------------------------------
def _sig(trace, name):
    return [s.name for s in trace.signals].index(name)


def _edited(tmp_path, source, edits, name="edited"):
    editor = StilEditor(source)
    result = editor.apply(edits, load_stil(source))
    path = tmp_path / f"{name}.stil"
    path.write_text(result.text)
    pins = None
    if result.pin_map is not None:
        pins = tmp_path / f"{name}.pinmap.json"
        pins.write_text(json.dumps(result.pin_map))
    return result, load_stil(path, pin_map=pins), path, pins


def test_a_character_change_turns_a_bidirectional_drive_into_a_compare(tmp_path):
    source = load_stil(F8)
    io0 = _sig(source, "io_0")
    assert [source.chars[i][io0] for i in (2, 3, 4)] == ["0", "0", "0"]
    choices = {c["char"]: c["kind"] for c in StilEditor(F8).char_choices(3, "io_0")}
    assert choices == {"0": "drive", "1": "drive", "Z": "release", "H": "compare", "L": "compare", "T": "compare"}
    result, edited, path, _ = _edited(tmp_path, F8, [{"op": "set_char", "signal": "io_0", "char": "H", "first": 2, "last": 4}])
    assert [edited.chars[i][io0] for i in range(1, 6)] == ["0", "H", "H", "H", "0"]
    assert edited.vector_count == source.vector_count and edited.total_ps == source.total_ps
    for i in (2, 3, 4):
        kinds = {e.kind for e in edited.events(i) if e.signal == io0}
        assert kinds == {KIND_COMPARE}  # the tester no longer drives io_0 there: it compares
    for i in range(source.vector_count):  # every other signal and vector unchanged
        if i not in (2, 3, 4):
            assert edited.chars[i] == source.chars[i]
        assert [c for k, c in enumerate(edited.chars[i]) if k != io0] == [c for k, c in enumerate(source.chars[i]) if k != io0]
    assert "3 character(s) changed in 3 vector(s)" in result.notes
    # and it compiles to a binary that decodes to the same timeline
    payload, map_data, _report = compile_stil(CompileOptions(input_path=path, mode="rate-vector"))
    binary = tmp_path / "edited.bin"
    binary.write_bytes(payload)
    binary.with_name("edited.map.json").write_text(json.dumps(map_data))
    assert timelines(load_bin(binary)) == timelines(edited)


def test_a_character_change_keeps_labels_and_repetition_targets(tmp_path):
    _, edited, path, _ = _edited(tmp_path, SELECTORS, [{"op": "set_char", "signal": "C", "char": "L", "first": 3}])
    assert edited.chars[3] == "11Ll"  # was '\\r2 1 Hl'
    assert "V { ALL = 11Ll; }" in path.read_text()


@pytest.mark.parametrize("edit, message", [
    ({"op": "set_char", "signal": "dout_0", "char": "1", "first": 3}, "has no character '1' in WaveformTable WFT1"),
    ({"op": "set_char", "signal": "io_0", "char": "Q", "first": 3}, "not a WaveformChar"),
    ({"op": "set_char", "signal": "nope", "char": "1", "first": 3}, "unknown signal"),
    ({"op": "rename_signal", "signal": "clk", "to": "io_1"}, "already used"),
    ({"op": "rename_signal", "signal": "clk", "to": "WFT1"}, "already used"),
    ({"op": "rename_signal", "signal": "clk", "to": "9x"}, "not a STIL name"),
    ({"op": "add_signal", "signal": "tp", "direction": "In"}, "no free pin bit"),  # F8 uses all 24 bits
    ({"op": "add_signal", "signal": "tp", "direction": "Sideways"}, "direction must be"),
])
def test_invalid_signal_edits_are_rejected(edit, message):
    with pytest.raises(EditError, match=message):
        StilEditor(F8).apply([edit], load_stil(F8))


def test_a_vector_inside_a_loop_is_not_edited_one_by_one():
    name = FIXTURES / "loop_boundary_exit_direct_regression.stil"
    trace = load_stil(name)
    with pytest.raises(EditError, match="Loop"):
        StilEditor(name).apply([{"op": "set_char", "signal": trace.signals[0].name, "char": "1",
                                 "first": trace.vector_count - 1}], trace)


# ---------------------------------------------------------------------------
# signals: rename, add, remove
# ---------------------------------------------------------------------------
def test_rename_changes_every_reference_and_the_pin_map(tmp_path):
    stil = tmp_path / "counter.stil"
    stil.write_text(counter(40))
    pins = tmp_path / "counter.pins.json"
    pins.write_text(json.dumps({"active_pins": 16, "signals": {
        **{f"A{k}": {"bit": k, "logical_pin": f"DUT{k}", "direction": "input"} for k in range(7)},
        "IOOE": {"bit": 8, "logical_pin": "DUT8", "direction": "input"}}}))
    source = load_stil(stil, pin_map=pins)
    result = StilEditor(stil, pin_map=pins).apply([{"op": "rename_signal", "signal": "A3", "to": "ADDR3"}], source)
    assert result.pin_map["signals"]["ADDR3"]["bit"] == 3 and "A3" not in result.pin_map["signals"]
    text = result.text
    assert "ADDR3 In;" in text and "A2 + ADDR3 + A4" in text and "ADDR3 { 0: 0ns Drive(0)" in text
    assert "A3" not in text.split("\n", 1)[1]  # (the header comment names the source file only)
    (tmp_path / "e.stil").write_text(text)
    (tmp_path / "e.pins.json").write_text(json.dumps(result.pin_map))
    edited = load_stil(tmp_path / "e.stil", pin_map=tmp_path / "e.pins.json")
    assert [s.name for s in edited.signals] == [s.name if s.name != "A3" else "ADDR3" for s in source.signals]
    assert [(s.bit, s.pin) for s in edited.signals] == [(s.bit, s.pin) for s in source.signals]
    assert list(edited.chars) == list(source.chars)


def test_add_and_remove_keep_every_other_signal_on_its_bit_and_compile(tmp_path):
    source = load_stil(SELECTORS)
    result, edited, path, pins = _edited(tmp_path, SELECTORS, [
        {"op": "remove_signal", "signal": "B"},
        {"op": "add_signal", "signal": "TP", "direction": "Out", "fill": "X"},
        {"op": "set_char", "signal": "TP", "char": "H", "first": 2},
    ])
    assert [s.name for s in edited.signals] == ["A", "C", "D", "TP"]
    bits = {name: v["bit"] for name, v in result.pin_map["signals"].items()}
    assert bits == {"A": 0, "C": 2, "D": 3, "TP": 1}  # B's bit is free again and taken by TP; A/C/D stay
    for i in range(source.vector_count):
        kept = "".join(c for k, c in enumerate(source.chars[i]) if source.signals[k].name != "B")
        assert edited.chars[i] == kept + ("H" if i == 2 else "X")
    tp = _sig(edited, "TP")
    assert [e.kind for e in edited.events(2) if e.signal == tp] == [KIND_COMPARE]
    payload, map_data, _report = compile_stil(CompileOptions(input_path=path, pin_map_path=pins, mode="rate-vector"))
    binary = tmp_path / "edited.bin"
    binary.write_bytes(payload)
    binary.with_name("edited.map.json").write_text(json.dumps(map_data))
    assert timelines(load_bin(binary)) == timelines(edited)


def test_an_added_input_drives_its_fill_and_a_wait_holds_it(tmp_path):
    _, edited, _path, _ = _edited(tmp_path, SELECTORS, [
        {"op": "add_signal", "signal": "EN", "direction": "In", "fill": "1"},
        {"op": "insert_wait", "after": 1, "duration": "2us"},
    ])
    en = _sig(edited, "EN")
    assert edited.vector_count == 8 and edited.extra_ps[2] == 2_000_000
    assert {edited.chars[i][en] for i in range(edited.vector_count) if i != 2} == {"1"}
    assert all((edited.start_values[i] >> en) & 1 and (edited.start_enables[i] >> en) & 1 for i in range(1, 8))
    assert [e.value for e in edited.events(0) if e.signal == en and e.kind == KIND_DRIVE] == ["1"]


def test_a_vector_that_assigns_only_the_removed_signal_is_refused(tmp_path):
    stil = tmp_path / "lone.stil"
    stil.write_text(SELECTORS.read_text().replace("  V { ALL = p0XH; }", "  V { ALL = p0XH; }\n  V { B = 1; }"))
    line = stil.read_text().split("\n").index("  V { B = 1; }") + 1
    with pytest.raises(EditError, match=rf"^line {line}: the vector assigns only B"):
        StilEditor(stil).apply([{"op": "remove_signal", "signal": "B"}], load_stil(stil))


class _CountingText(str):
    """The source text, counting the characters that ``count`` scans (line numbers are counted with it)."""

    scanned = 0

    def count(self, sub, start=None, end=None):  # type: ignore[no-untyped-def, override]
        lo, hi, _ = slice(start, end).indices(len(self))
        self.scanned += max(0, hi - lo)
        return super().count(sub, start, end)


def test_adding_a_signal_to_a_flat_pattern_does_not_rescan_the_file_per_vector(tmp_path):
    """Each rewritten vector used to count the newlines from the file start for an error message's line number,
    so adding a signal to a long flat pattern took time growing with the square of its vector count."""

    stil, pins = tmp_path / "counter.stil", tmp_path / "counter.pins.json"
    stil.write_text(counter(600))
    pins.write_text(counter_pins())
    edits = [{"op": "add_signal", "signal": "TP_DIAG", "direction": "In", "fill": "0"},
             {"op": "set_char", "signal": "A0", "char": "1", "first": 5, "last": 9}]
    source = load_stil(stil, pin_map=pins)
    expected = StilEditor(stil, pin_map=pins).apply(edits, source)
    editor = StilEditor(stil, pin_map=pins)
    editor.text = _CountingText(editor.text)
    result = editor.apply(edits, source)
    assert (result.text, result.pin_map) == (expected.text, expected.pin_map)
    assert result.text.count("; }") >= 600 and "TP_DIAG In;" in result.text
    assert editor.text.scanned <= 2 * len(editor.text)  # once per vector would be about 300 times the file


def test_names_follow_the_edit_list(tmp_path):
    # after a rename the new name is the signal's name; an added signal can be renamed and removed again
    _, edited, _path, _ = _edited(tmp_path, SELECTORS, [
        {"op": "rename_signal", "signal": "A", "to": "RENAMED_A"},
        {"op": "set_char", "signal": "RENAMED_A", "char": "1", "first": 0},
        {"op": "add_signal", "signal": "X1", "direction": "InOut"},
        {"op": "rename_signal", "signal": "X1", "to": "X2"},
        {"op": "remove_signal", "signal": "X2"},
    ])
    assert [s.name for s in edited.signals] == ["RENAMED_A", "B", "C", "D"]
    assert edited.chars[0] == "11HL"


def test_the_preview_map_skips_inserted_vectors():
    mapping = PreviewMap({1: 1, 4: 2}, 10)
    assert [mapping.to_source(i) for i in range(10)] == [0, 1, None, 2, 3, 4, None, None, 5, 6]
    assert [mapping.to_preview(s) for s in range(7)] == [0, 1, 3, 4, 5, 8, 9]
    assert 2 not in mapping and mapping[3] == 2


def test_session_flow_with_signal_edits_on_the_preview_save_and_export(tmp_path):
    stil = tmp_path / "sel.stil"
    stil.write_text(SELECTORS.read_text())
    session = Session(work_dir=tmp_path / "work")
    (source_id,) = session.load([stil])
    assert session.edits.info(source_id)["edits"] == [] and not session.edits.states  # no second parse yet
    info = session.edits.add(source_id, {"op": "insert_wait", "after": 0, "duration": "1us"})
    preview_id = info["preview_id"]
    info = session.edits.add(preview_id, {"op": "rename_signal", "signal": "B", "to": "DATA"})
    choices = session.edits.char_choices(preview_id, 2, "DATA")  # preview #2 is source #1
    assert choices["vector"] == 1 and {c["char"] for c in choices["choices"]} >= {"0", "1", "Z"}
    session.edits.add(preview_id, {"op": "set_char", "signal": "DATA", "char": "Z", "first": 2})
    assert session.edits.info(source_id)["edits"][-1] == {"op": "set_char", "signal": "DATA", "char": "Z", "first": 1}
    preview = session.get(preview_id)
    assert [s.name for s in preview.signals] == ["A", "DATA", "C", "D"] and preview.chars[2][1] == "Z"
    saved = session.edits.save(source_id)["saved"]
    assert Path(saved["stil"]).name == "sel.edited.stil" and Path(saved["pin_map"]).name == "sel.edited.pinmap.json"
    assert json.loads(Path(saved["pin_map"]).read_text())["signals"]["DATA"]["bit"] == 1
    exported = session.edits.export(preview_id, str(tmp_path / "out" / "mine"))
    assert exported["saved"]["stil"].endswith("mine.stil") and Path(exported["saved"]["stil"]).read_text() == \
        Path(saved["stil"]).read_text()
    with pytest.raises(EditError, match="overwrite"):
        session.edits.export(source_id, str(stil))


def test_export_without_edits_writes_the_stil_as_read(tmp_path):
    # an ATE-generator pair is shown preprocessed; exporting it writes the converted (subset) STIL
    (tmp_path / "proj.stil").write_text(ATE_PROJECT)
    (tmp_path / "pat.stil").write_text(ATE_PATTERN.replace("\\r2 1", "1H"))
    session = Session(work_dir=tmp_path / "work")
    (trace_id,) = session.load([tmp_path / "pat.stil", tmp_path / "proj.stil"])
    out = session.edits.export(trace_id, str(tmp_path / "export"))["exported"]
    shown = next(s["path"] for s in session.get(trace_id).sources if s["kind"] == "stil")
    assert out["stil"].endswith("export.stil") and Path(out["stil"]).read_bytes() == Path(shown).read_bytes()
    assert load_stil(out["stil"]).chars == ["0L", "1H", "1H"]
    with pytest.raises(EditError, match="overwrite"):
        session.edits.export(trace_id, str(tmp_path / "pat.stil"))  # the user's original pattern file
