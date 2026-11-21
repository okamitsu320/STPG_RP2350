from __future__ import annotations

from pathlib import Path

import pytest

from stil.ast import LoopStmt
from stil.errors import StilError
from stil.device_precheck import inspect_pattern_requirements
from stil.parser import parse_stil_text
from stil.pipeline import CompileOptions, compile_stil
from stil.preprocess import _deduplicate_definition_blocks, _extract_patternburst_domains, preprocess_ategen_stil
from stil.resolve import load_pin_map, resolve_document
from stil.source_analysis import analyze_stil_source


_BASE = """\
Signals {
  A Out;
}
Timing {
  WaveformTable W1 {
    Period '100ns';
    Waveforms {
      A { 0: 0ns Drive(0); 1: 0ns Drive(1); }
    }
  }
  WaveformTable W2 {
    Period '200ns';
    Waveforms {
      A { 0: 0ns Drive(0); 1: 0ns Drive(1); }
    }
  }
}
"""


def _resolve(pattern_body: str, *, definitions: str = "", burst_refs: str = "", loop_policy: str = "expand"):
    source = (
        _BASE
        + definitions
        + (
            f"PatternBurst PB {{\n  {burst_refs}\n  PatList {{\n    P;\n  }}\n}}\n"
            if burst_refs
            else ""
        )
        + f"Pattern P {{\n{pattern_body}\n}}\n"
    )
    document = parse_stil_text(source, source_file="test.stil", loop_policy=loop_policy)
    pin_map = load_pin_map(None, document=document)
    resolved = resolve_document(
        document,
        pin_map=pin_map,
        pattern_burst_name="PB" if burst_refs else None,
        pattern_name="P",
        loop_policy=loop_policy,
    )
    return document, resolved


def test_macro_and_procedure_calls_inline_with_distinct_waveform_state_rules() -> None:
    definitions = """\
MacroDefs (MD) {
  M0 {
    W W2;
    V { A = 1; }
  }
}
Procedures (PD) {
  P0 {
    W W1;
    V { A = 0; }
  }
}
"""
    document, resolved = _resolve(
        "Macro M0;\nCall P0;\nV { A = 1; }",
        definitions=definitions,
        burst_refs="MacroDefs MD; Procedures PD;",
    )

    assert [vector.waveform_table_name for vector in resolved.vectors] == ["W2", "W1", "W2"]
    assert [vector.signal_chars["A"] for vector in resolved.vectors] == ["1", "0", "1"]
    macro_call_line = document.patterns[0].statements[0].location.line
    procedure_call_line = document.patterns[0].statements[1].location.line
    assert resolved.vectors[0].location.expansion_trace == (f"Macro M0 called at line {macro_call_line}",)
    assert resolved.vectors[1].location.expansion_trace == (f"Procedure P0 called at line {procedure_call_line}",)
    assert len(document.macros) == 1
    assert len(document.procedures) == 1


def test_forward_goto_and_stop_are_statically_lowered() -> None:
    _document, resolved = _resolve(
        "W W1;\n"
        "start: V { A = 0; }\n"
        "Goto finish;\n"
        "V { A = 1; }\n"
        "finish: V { A = 0; }\n"
        "Stop;\n"
        "V { A = 1; }"
    )
    assert [vector.signal_chars["A"] for vector in resolved.vectors] == ["0", "0"]
    assert [vector.location.label for vector in resolved.vectors] == ["start", "finish"]


def test_procedure_local_goto_and_stop_propagate_to_selected_pattern() -> None:
    definitions = """\
Procedures (PD) {
  P0 {
    first: V { A = 0; }
    Goto last;
    V { A = 1; }
    last: V { A = 0; }
    Stop;
  }
}
"""
    _document, resolved = _resolve(
        "W W1;\nCall P0;\nV { A = 1; }",
        definitions=definitions,
        burst_refs="Procedures PD;",
    )
    assert [vector.signal_chars["A"] for vector in resolved.vectors] == ["0", "0"]


def test_macro_stop_terminates_pattern_after_returning_from_call() -> None:
    definitions = """\
MacroDefs (MD) {
  M0 { V { A = 0; } Stop; V { A = 1; } }
}
"""
    _document, resolved = _resolve(
        "W W1;\nMacro M0;\nV { A = 1; }",
        definitions=definitions,
        burst_refs="MacroDefs MD;",
    )
    assert [vector.signal_chars["A"] for vector in resolved.vectors] == ["0"]


def test_macro_local_forward_goto_is_lowered_within_macro_scope() -> None:
    definitions = """\
MacroDefs (MD) {
  M0 {
    first: V { A = 0; }
    Goto last;
    V { A = 1; }
    last: V { A = 0; }
  }
}
"""
    _document, resolved = _resolve(
        "W W1;\nMacro M0;\nV { A = 1; }",
        definitions=definitions,
        burst_refs="MacroDefs MD;",
    )
    assert [vector.signal_chars["A"] for vector in resolved.vectors] == ["0", "0", "1"]


@pytest.mark.parametrize("mode", ["rate-vector", "raw-event", "macro-stream"])
def test_control_flow_lowering_feeds_existing_compile_backends(tmp_path: Path, mode: str) -> None:
    source = tmp_path / f"control_{mode}.stil"
    source.write_text(
        _BASE
        + "MacroDefs (MD) {\n M0 { W W1; V { A = 0; } V { A = 1; } }\n}\n"
        + "Pattern P {\n Macro M0;\n}\n",
        encoding="utf-8",
    )
    payload, _map_data, report = compile_stil(CompileOptions(input_path=source, mode=mode))
    requirements = inspect_pattern_requirements(payload)
    assert report["vector_count"] == 2
    assert requirements.estimated_event_count > 0
    assert report["mode_emitted"] == mode


def test_source_analysis_reports_lowered_counts_for_macro_pattern(tmp_path: Path) -> None:
    source = tmp_path / "analysis_macro.stil"
    source.write_text(
        _BASE
        + "MacroDefs (MD) {\n M0 { W W1; V { A = 0; } V { A = 1; } }\n}\n"
        + "Pattern P {\n Macro M0;\n}\n",
        encoding="utf-8",
    )
    report = analyze_stil_source(source, mode="rate-vector")
    pattern_summary = report["source_structure"]["patterns"][0]
    assert pattern_summary["control_flow_lowered"] is True
    assert pattern_summary["vector_count"] == 2
    assert pattern_summary["source_vector_count"] == 2


def test_selected_domains_reject_ambiguous_definitions_and_use_burst_binding() -> None:
    definitions = """\
MacroDefs (MD0) {
  M0 { V { A = 0; } }
}
MacroDefs (MD1) {
  M0 { V { A = 1; } }
}
"""
    with pytest.raises(StilError, match="multiple domains"):
        _resolve("W W1;\nMacro M0;", definitions=definitions)

    _document, resolved = _resolve(
        "W W1;\nMacro M0;",
        definitions=definitions,
        burst_refs="MacroDefs MD1;",
    )
    assert resolved.vectors[0].signal_chars["A"] == "1"


def test_selected_patternburst_does_not_expose_unreferenced_domains() -> None:
    definitions = """\
MacroDefs (MD) {
  M0 { V { A = 0; } }
}
"""
    source = (
        _BASE
        + definitions
        + "PatternBurst PB {\n  PatList {\n    P;\n  }\n}\n"
        + "Pattern P {\nW W1;\nMacro M0;\n}\n"
    )
    document = parse_stil_text(source, source_file="test.stil")
    with pytest.raises(StilError) as exc_info:
        resolve_document(
            document,
            pin_map=load_pin_map(None, document=document),
            pattern_burst_name="PB",
            pattern_name="P",
        )
    assert exc_info.value.code == "ERR_MACRO_NOT_FOUND"


def test_single_line_patlist_binds_patternburst_and_blocks_unrelated_macro_domain() -> None:
    source = (
        _BASE
        + "MacroDefs (UNBOUND) {\n M0 { W W2; V { A = 1; } }\n}\n"
        + "MacroDefs (ALLOWED) {\n MA { V { A = 0; } }\n}\n"
        + "PatternBurst PB {\n MacroDefs ALLOWED; PatList { P; }\n}\n"
        + "Pattern P {\n W W1; Macro M0;\n}\n"
    )
    document = parse_stil_text(source, source_file="single_line_patlist.stil")
    assert document.pattern_bursts[0].patterns == ("P",)

    with pytest.raises(StilError) as exc_info:
        resolve_document(
            document,
            pin_map=load_pin_map(None, document=document),
            pattern_burst_name=None,
            pattern_name="P",
        )
    assert exc_info.value.code == "ERR_MACRO_NOT_FOUND"


def test_patternburst_patlist_parses_multiple_inline_names_and_rejects_garbage() -> None:
    source = _BASE + "PatternBurst PB {\n PatList { P0; P1.child; }\n}\n"
    document = parse_stil_text(source, source_file="inline_patlist.stil")
    assert document.pattern_bursts[0].patterns == ("P0", "P1.child")

    malformed = _BASE + "PatternBurst PB {\n PatList { P0; not-an-entry }\n}\n"
    with pytest.raises(StilError) as exc_info:
        parse_stil_text(malformed, source_file="malformed_patlist.stil")
    assert exc_info.value.code == "ERR_BAD_PATTERNBURST"


def test_global_macro_and_procedure_definitions_are_visible_with_burst_domains() -> None:
    definitions = """\
MacroDefs {
  GM { W W2; V { A = 1; } }
}
Procedures {
  GP { W W1; V { A = 0; } }
}
MacroDefs (MD) {
  DM { V { A = 1; } }
}
"""
    document, resolved = _resolve(
        "W W1;\nMacro GM;\nMacro DM;\nCall GP;",
        definitions=definitions,
        burst_refs="MacroDefs MD;",
    )
    assert [(item.domain, item.name) for item in document.macros] == [("", "GM"), ("MD", "DM")]
    assert [(item.domain, item.name) for item in document.procedures] == [("", "GP")]
    assert [vector.waveform_table_name for vector in resolved.vectors] == ["W2", "W2", "W1"]
    assert [vector.signal_chars["A"] for vector in resolved.vectors] == ["1", "1", "0"]


def test_global_definition_is_rejected_when_unlisted_burst_domain_could_be_borrowed() -> None:
    source = (
        _BASE
        + "MacroDefs {\n GM { W W1; V { A = 0; } }\n}\n"
        + "PatternBurst PB {\n MacroDefs MD; PatList { OTHER; }\n}\n"
        + "Pattern P {\n Macro GM;\n}\n"
    )
    document = parse_stil_text(source, source_file="unlisted_pattern.stil")
    with pytest.raises(StilError) as exc_info:
        resolve_document(
            document,
            pin_map=load_pin_map(None, document=document),
            pattern_burst_name=None,
            pattern_name="P",
        )
    assert exc_info.value.code == "ERR_PATTERNBURST_SELECTION_REQUIRED"


def test_preprocessor_preserves_standard_global_definitions_and_legacy_domains() -> None:
    blocks = _deduplicate_definition_blocks(
        [
            "MacroDefs { GM { W W1; V { A = 0; } } }",
            "Procedures { GP { W W2; V { A = 1; } } }",
            "MacroDefs { MD { DM { V { A = 1; } } } }",
        ]
    )
    document = parse_stil_text(_BASE + "\n".join(blocks), source_file="preprocessed_defs.stil")
    assert {(item.domain, item.name) for item in document.macros} == {("", "GM"), ("MD", "DM")}
    assert {(item.domain, item.name) for item in document.procedures} == {("", "GP")}


def test_implicit_patternburst_binding_rejects_multiple_containing_bursts() -> None:
    source = (
        _BASE
        + "PatternBurst PB0 {\nPatList {\nP;\n}\n}\n"
        + "PatternBurst PB1 {\nPatList {\nP;\n}\n}\n"
        + "Pattern P {\nW W1;\nV { A = 0; }\n}\n"
    )
    document = parse_stil_text(source, source_file="ambiguous_bursts.stil")
    with pytest.raises(StilError) as exc_info:
        resolve_document(
            document,
            pin_map=load_pin_map(None, document=document),
            pattern_burst_name=None,
            pattern_name="P",
        )
    assert exc_info.value.code == "ERR_PATTERNBURST_SELECTION_REQUIRED"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("W W1; Goto missing; V { A = 0; }", "ERR_GOTO_NOT_FOUND"),
        ("W W1; again: V { A = 0; } Goto again;", "ERR_GOTO_NOT_FORWARD"),
        ("W W1; Goto nested; Loop 2 { nested: V { A = 0; } }", "ERR_GOTO_CROSS_SCOPE"),
        ("W W1; same: V { A = 0; } Loop 2 { same: V { A = 1; } }", "ERR_DUPLICATE_LABEL"),
        ("W W1; Macro M0 { A = 0; };", "ERR_PARAMETERIZED_CALL_UNSUPPORTED"),
    ],
)
def test_static_control_flow_rejects_unsafe_or_unresolved_forms(body: str, code: str) -> None:
    with pytest.raises(StilError) as exc_info:
        _resolve(body, definitions="MacroDefs (MD) {\n M0 { V { A = 0; } }\n}\n")
    assert exc_info.value.code == code


def test_recursive_calls_are_rejected() -> None:
    definitions = """\
MacroDefs (MD) {
  M0 { Macro M1; }
  M1 { Macro M0; }
}
"""
    with pytest.raises(StilError) as exc_info:
        _resolve("W W1;\nMacro M0;", definitions=definitions)
    assert exc_info.value.code == "ERR_RECURSIVE_CALL"


def test_preserved_loop_can_contain_macro_and_retains_source_map_trace(tmp_path: Path) -> None:
    source = tmp_path / "macro_loop.stil"
    source.write_text(
        _BASE
        + "MacroDefs (MD) {\n M0 { V { A = 1; } }\n}\n"
        + "PatternBurst PB {\n MacroDefs MD;\n PatList {\n P;\n }\n}\n"
        + "Pattern P {\n W W1;\n Loop 100 { Macro M0; }\n}\n",
        encoding="utf-8",
    )
    payload, map_data, report = compile_stil(
        CompileOptions(input_path=source, mode="rate-vector", loop_policy="preserve", pattern_burst="PB")
    )
    assert report["loop_preserved"] is True
    assert report["logical_vector_count"] == 100
    parsed = parse_stil_text(source.read_text(), source_file=str(source), loop_policy="preserve")
    assert isinstance(parsed.patterns[0].statements[1], LoopStmt)
    expected_trace = map_data["vectors"]["0"]["body_source_map"][0]["expansion_trace"]
    assert expected_trace[0].startswith("Loop at line ")
    assert expected_trace[1].startswith("Macro M0 called at line ")
    assert payload


def test_preserved_loop_rejects_a_waveform_state_that_changes_each_iteration() -> None:
    with pytest.raises(StilError) as exc_info:
        _resolve("W W1;\nLoop 3 { V { A = 0; } W W2; }", loop_policy="preserve")
    assert exc_info.value.code == "ERR_NON_STATIONARY_PRESERVED_LOOP"


def test_static_expansion_limit_rejects_before_materializing_excess_loop() -> None:
    with pytest.raises(StilError) as exc_info:
        _resolve("W W1;\nLoop 1100001 { V { A = 0; } }")
    assert exc_info.value.code == "ERR_STATIC_EXPANSION_LIMIT"


def test_preserved_loop_does_not_apply_expanded_vector_limit_to_repeat_count() -> None:
    _document, resolved = _resolve("W W1;\nLoop 1100001 { V { A = 0; } }", loop_policy="preserve")
    assert len(resolved.vectors) == 1
    assert resolved.items[0].loop_count == 1_100_001


def test_block_comments_preserve_line_positions_and_unclosed_comment_rejects() -> None:
    _document, resolved = _resolve("/* opening { */\nW W1; // comment {\nV { A = 0; }")
    assert resolved.vectors[0].location.line == 21
    with pytest.raises(StilError) as exc_info:
        parse_stil_text(_BASE + "/* never closed", source_file="bad.stil")
    assert exc_info.value.code == "ERR_UNCLOSED_COMMENT"


def test_unsupported_pattern_statement_is_not_silently_ignored() -> None:
    with pytest.raises(StilError) as exc_info:
        _resolve("W W1;\nCondition { A = 0; }")
    assert exc_info.value.code == "ERR_UNSUPPORTED_PATTERN_STATEMENT"


def test_split_ategen_preprocess_preserves_macro_domains_and_burst_binding(tmp_path: Path) -> None:
    project = tmp_path / "project.stil"
    pattern = tmp_path / "pattern.stil"
    project.write_text(
        """\
Signals {
  A InOut;
}
SignalGroups {
  ALL = 'A';
}
Timing {
  WaveformTable W1 {
    Period '100ns';
    Waveforms {
      A { 0 { '0ns' D; } }
      A { 1 { '0ns' U; } }
    }
  }
  WaveformTable W2 {
    Period '200ns';
    Waveforms {
      A { 0 { '0ns' D; } }
      A { 1 { '0ns' U; } }
    }
  }
}
MacroDefs {
  MD {
    M0 { W W1; V { ALL = 1; } }
  }
}
PatternBurst PB {
  MacroDefs MD; Procedures PD;
  PatList {
    P;
  }
}
""",
        encoding="utf-8",
    )
    pattern.write_text(
        "Procedures {\n"
        "  PD { P0 { W W2; V { ALL = 0; } } }\n"
        "}\n"
        "Pattern P {\n  Macro M0;\n  Call P0;\n  V { ALL = 1; }\n}\n",
        encoding="utf-8",
    )

    result = preprocess_ategen_stil(project, pattern)
    document = parse_stil_text(result.output_text, source_file="joined.pre.stil")
    assert len(document.macros) == 1
    assert document.pattern_bursts[0].macro_domains == ("MD",)
    assert document.pattern_bursts[0].procedure_domains == ("PD",)
    ignored_result = preprocess_ategen_stil(project, pattern, ignore_signals={"A"})
    ignored_document = parse_stil_text(ignored_result.output_text, source_file="ignored.pre.stil")
    assert ignored_document.macros[0].statements[1].chars == "T"
    assert ignored_document.procedures[0].statements[1].chars == "T"
    resolved = resolve_document(
        document,
        pin_map=load_pin_map(None, document=document),
        pattern_burst_name=None,
        pattern_name=None,
    )
    assert [vector.signal_chars["A"] for vector in resolved.vectors] == ["1", "0", "1"]
    assert [vector.waveform_table_name for vector in resolved.vectors] == ["W1", "W2", "W1"]


def test_preprocessor_rejects_compare_shift_across_macro_expansion(tmp_path: Path) -> None:
    project = tmp_path / "project.stil"
    pattern = tmp_path / "pattern.stil"
    project.write_text(
        "Signals {\nA InOut;\n}\nSignalGroups {\nALL = 'A';\n}\n"
        "Timing {\nWaveformTable W1 {\nPeriod '100ns';\nWaveforms {\n"
        "A { 0 { '0ns' D; } }\nA { 1 { '0ns' U; } }\n}\n}\n}\n"
        "MacroDefs (MD) {\nM0 { V { ALL = H; } }\n}\n"
        "PatternBurst PB {\nMacroDefs MD;\nPatList {\nP;\n}\n}\n",
        encoding="utf-8",
    )
    pattern.write_text("Pattern P {\nMacro M0;\n}\n", encoding="utf-8")
    with pytest.raises(StilError) as exc_info:
        preprocess_ategen_stil(
            project,
            pattern,
            shift_compare_signals={"A"},
            shift_compare_vectors=1,
        )
    assert exc_info.value.code == "ERR_SHIFT_WITH_MACRO_CALL_UNSUPPORTED"


def test_preprocessor_merges_distinct_domains_and_rejects_conflicts() -> None:
    merged = _deduplicate_definition_blocks(
        [
            "MacroDefs { MD0 { M0 { V { A = 0; } } } }",
            "MacroDefs { MD1 { M1 { V { A = 1; } } } }",
        ]
    )
    assert len(merged) == 2
    with pytest.raises(StilError) as exc_info:
        _deduplicate_definition_blocks(
            [
                "MacroDefs (MD) { M0 { V { A = 0; } } }",
                "MacroDefs (MD) { M0 { V { A = 1; } } }",
            ]
        )
    assert exc_info.value.code == "ERR_DUPLICATE_DEFINITION_DOMAIN"


def test_preprocessor_refuses_unrelated_patternburst_domain_binding() -> None:
    with pytest.raises(StilError) as exc_info:
        _extract_patternburst_domains(
            "PatternBurst PB {\nMacroDefs MD;\nPatList {\nOTHER;\n}\n}",
            source_file="project.stil",
            selected_pattern="P",
        )
    assert exc_info.value.code == "ERR_PATTERNBURST_SELECTION_REQUIRED"
