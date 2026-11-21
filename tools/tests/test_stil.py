from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from synthetic_fixtures import write_public_ate

import pytest

from stil.cli_compile import main as compile_main
from stil.cli_compareprep import main as compareprep_main
from stil.cli_stimulus import main as stimulus_main
from stil.cli_preprocess import main as preprocess_main
from compat_cli.runtime import _build_state_update_pattern, _emit_fail_indication
from compat_cli.selfloop import build_selfloop_pin_map
from stil.cli_inspect import main as inspect_main
from compat_cli.regression import build_cases as build_regression_cases, main as regression_main
from compat_cli.runtime import _wait_for_terminal_status, _write_outputs
from stil.device_precheck import (
    PatternRequirements,
    estimate_direct_execution_fit,
    inspect_pattern_requirements,
    validate_device_compatibility,
)
from stil.errors import StilError
from stil.parser import parse_stil_file, parse_stil_text
from stil.preprocess import preprocess_ategen_stil
from stil.report import render_fail_report, render_fail_report_text
from stpg.constants import (
    CAP_EXPECT_COMPARE,
    CAP_PSRAM_STREAMING,
    CAP_RATE_VECTOR_MODE,
    CAP_RUN_16PIN,
    CAP_RUN_24PIN,
    CAP_SAMPLE_CAPTURE,
    CAP_UPLOAD_FLASH,
    CAP_UPLOAD_PSRAM,
    CAP_UPLOAD_SRAM,
    STPG_DRIVE_ONE,
    STPG_DRIVE_VECTOR,
    STPG_DRIVE_ZERO,
    STPG_RATE_SLOT_DIR_UPDATE,
    STPG_RATE_SLOT_SAMPLE,
    RUN_FLAG_PRESERVE_GPIO_STATE,
    RUN_FLAG_STREAMING_POC,
    STPG_EVF_COMPARE_POINT,
    STPG_EVF_SAMPLE_POINT,
    STORAGE_PSRAM,
    STORAGE_SRAM,
)
from stil.resolve import build_ir, load_pin_map, resolve_document
from stpg.pattern import (
    MacroVectorRecordP2,
    MacroVectorRecord,
    LoopBlock,
    LoopRefRecord,
    PatternValidationError,
    RateDesc,
    RateSlot,
    VectorRecord,
    pack_macro_vector,
    pack_macro_p2_vector,
    pack_rate_vector,
    unpack_pattern,
    unpack_rate_vector,
    unpack_rate_vector_full,
    validate_macro_vector,
)


FIXTURES = Path(__file__).parent / "fixtures" / "stil"
FPGA_FIXTURES = None


@pytest.fixture(scope="module", autouse=True)
def _synthetic_ate_inputs(tmp_path_factory):
    global FPGA_FIXTURES
    FPGA_FIXTURES = write_public_ate(tmp_path_factory.mktemp("public-ate"))


def test_inspect_single_rate(capsys: pytest.CaptureFixture[str]) -> None:
    assert inspect_main([str(FIXTURES / "single_rate_nrz.stil")]) == 0
    output = capsys.readouterr().out
    assert "signals=2" in output
    assert "patterns=1" in output


def test_parse_multi_wft_pattern_vectors_keep_waveform_table() -> None:
    document = parse_stil_file(FIXTURES / "multi_wft_rate_switch.stil")
    pattern = document.patterns[0]
    assert pattern.vectors[0].waveform_table_name == "WFT_SLOW"
    assert pattern.vectors[1].waveform_table_name == "WFT_FAST"


def test_parser_ignores_stil_line_comments_inside_pattern_and_trailing_declarations() -> None:
    document = parse_stil_text(
        '\n'.join(
            [
                'Header { Title "comment // is quoted"; }',
                "Signals {",
                "  D0 InOut; // signal comment",
                "}",
                "SignalGroups {",
                "  ALL = 'D0'; // group comment",
                "}",
                "Timing {",
                "  WaveformTable WFT1 {",
                "    Period '100ns'; // period comment",
                "    Waveforms {",
                "      D0 { 0: 0ns Drive(0); 1: 0ns Drive(1); } // waveform comment",
                "    }",
                "  }",
                "}",
                "Pattern MAIN {",
                "  // comment-only line inside Pattern with braces { }",
                '  W "WFT1"; // W comment',
                "  entry_0: V { ALL = 0; } // vector comment",
                "  Loop 2 {",
                "    V { ALL = 1; } // loop vector comment",
                "  }",
                "}",
            ]
        ),
        source_file="comments.stil",
    )
    assert len(document.signals) == 1
    assert len(document.signal_groups) == 1
    assert len(document.waveform_tables) == 1
    assert len(document.patterns) == 1
    assert len(document.patterns[0].vectors) == 3
    assert all(vector.waveform_table_name == "WFT1" for vector in document.patterns[0].vectors)


def test_preprocess_ategen_preserves_all_waveform_tables(tmp_path: Path) -> None:
    project = tmp_path / "project.stil"
    project.write_text(
        "\n".join(
            [
                "Signals {",
                "  D0 In;",
                "}",
                "SignalGroups {",
                "  ALL = 'D0';",
                "}",
                "Timing Timing_ATE {",
                "  WaveformTable WFT_SLOW {",
                "    Period '100ns';",
                "    Waveforms {",
                "      D0 { 0 { '0s' D; } }",
                "      D0 { 1 { '0s' U; } }",
                "      D0 { P { '10ns' U; '20ns' D; } }",
                "      D0 { X { '0ns' X; } }",
                "      D0 { T { '10ns' T; } }",
                "    }",
                "  }",
                "  WaveformTable \"WFT_FAST\" {",
                "    Period '20ns';",
                "    Waveforms {",
                "      D0 { 1 { '0s' U; } }",
                "    }",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    pattern = tmp_path / "pattern.stil"
    pattern.write_text(
        "\n".join(
            [
                "Pattern MAIN {",
                "  // comment-only line with braces { }",
                '  W "WFT_FAST";',
                "  V { ALL = 1; }",
                "}",
            ]
        ),
        encoding="utf-8",
    )

    result = preprocess_ategen_stil(project, pattern)
    assert "WaveformTable WFT_SLOW {" in result.output_text
    assert "WaveformTable WFT_FAST {" in result.output_text
    assert "comment-only line with braces" not in result.output_text
    document = parse_stil_text(result.output_text, source_file=str(tmp_path / "pre.stil"))
    assert [table.name for table in document.waveform_tables] == ["WFT_SLOW", "WFT_FAST"]
    assert len(document.waveform_tables[0].waveforms) == 1
    assert {item.char for item in document.waveform_tables[0].waveforms[0].chars} == {"0", "1", "P", "X", "T"}
    events_by_char = {
        item.char: item.events for item in document.waveform_tables[0].waveforms[0].chars
    }
    assert [event.action_name for event in events_by_char["P"]] == ["Drive", "Drive"]
    assert events_by_char["X"][0].action_name == "Compare"
    assert events_by_char["T"][0].action_name == "Compare"
    assert document.patterns[0].vectors[0].waveform_table_name == "WFT_FAST"


def test_preprocess_cli_accepts_unambiguous_reversed_project_pattern_pair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project.stil"
    project.write_text(
        "\n".join(
            [
                "Signals {",
                "  D0 In;",
                "}",
                "SignalGroups {",
                "  ALL = 'D0';",
                "}",
                "Timing Timing_ATE {",
                "  WaveformTable WFT1 {",
                "    Period '100ns';",
                "    Waveforms {",
                "      D0 { 0 { '0s' D; } }",
                "      D0 { 1 { '0s' U; } }",
                "    }",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    pattern = tmp_path / "Patterns" / "pattern.stil"
    pattern.parent.mkdir()
    pattern.write_text(
        "\n".join(
            [
                "Pattern MAIN {",
                '  W "WFT1";',
                "  V { ALL = 1; }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "pre"

    assert preprocess_main([str(pattern), str(project), "--work-dir", str(output_dir), "--pin-map"]) == 0
    captured = capsys.readouterr()
    assert "detected reversed project/pattern arguments" in captured.err
    assert (output_dir / "project__pattern.pre.stil").is_file()
    assert (output_dir / "project__pattern.pre.pinmap.json").is_file()


def test_compile_single_rate_rate_vector(tmp_path: Path) -> None:
    output = tmp_path / "single_rate.bin"
    assert compile_main([str(FIXTURES / "single_rate_nrz.stil"), "-o", str(output), "--mode", "rate-vector"]) == 0
    header, rates, slots, vectors = unpack_rate_vector(output.read_bytes())
    assert header.rate_count == 1
    assert len(rates) == 1
    assert len(vectors) == 2
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    assert report["mode_emitted"] == "rate-vector"


def test_raw_event_rejects_period_below_event_overhead(tmp_path: Path) -> None:
    output = tmp_path / "single_rate_raw.bin"
    with pytest.raises(StilError) as excinfo:
        compile_main([str(FIXTURES / "single_rate_nrz.stil"), "-o", str(output), "--mode", "raw-event"])
    assert excinfo.value.code == "ERR_BAD_RAW_TIMING"


def test_raw_event_uses_vector_period_as_wait_budget(tmp_path: Path) -> None:
    stil_path = tmp_path / "raw_100ns.stil"
    stil_path.write_text(
        "\n".join(
            [
                'Header { Title "raw-100ns"; }',
                "Signals {",
                "  D0 InOut;",
                "  D1 InOut;",
                "}",
                "SignalGroups {",
                "  ALL = 'D0 + D1';",
                "}",
                "Timing {",
                "  WaveformTable WFT1 {",
                "    Period '100ns';",
                "    Waveforms {",
                "      ALL { 0: 0ns Drive(0); 1: 0ns Drive(1); X: 50ns Compare(X); Z: 0ns DirZ; }",
                "    }",
                "  }",
                "}",
                "PatternBurst PB1 {",
                "  PatList {",
                "    MAIN;",
                "  }",
                "}",
                "Pattern MAIN {",
                "  W WFT1;",
                "  V { ALL = 01; }",
                "  V { ALL = 10; }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "raw_100ns.bin"
    assert compile_main(
        [str(stil_path), "-o", str(output), "--mode", "raw-event", "--tick-hz", "150000000"]
    ) == 0
    header, events = unpack_pattern(output.read_bytes())
    assert header.event_count == 2
    assert [event.wait_ticks for event in events] == [9, 9]


def test_rate_vector_splits_mid_vector_compare_timing_and_allows_24pin(tmp_path: Path) -> None:
    pin_map = tmp_path / "pinmap.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 24,
                "signals": {
                    "D0": {"bit": 0, "gpio": 2, "direction": "inout"},
                    "D1": {"bit": 1, "gpio": 3, "direction": "inout"},
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "compare24.bin"
    assert compile_main(
        [
            str(FIXTURES / "compare_hlt.stil"),
            "-o",
            str(output),
            "--mode",
            "rate-vector",
            "--tick-hz",
            "150000000",
            "--pin-map",
            str(pin_map),
        ]
    ) == 0
    header, rates, slots, vectors = unpack_rate_vector(output.read_bytes())
    assert header.active_pins == 24
    assert len(rates) == 1
    assert rates[0].slot_count == 2
    assert [slots[0].time_ticks, slots[1].time_ticks] == [0, 3]
    assert slots[0].drive_mask == 0b10
    assert slots[1].sample_mask == 0b01
    assert vectors[0].expect_mask == 0b01
    assert vectors[0].expect_value == 0b01
    assert vectors[1].expect_mask == 0b01
    assert vectors[1].expect_value == 0


def test_compile_rejects_compare24_practical_two_group_deficit(tmp_path: Path) -> None:
    stil_path = tmp_path / "compare24_deficit.stil"
    vector_lines = "\n".join("  V { ALL = 10; }" for _ in range(2500))
    stil_path.write_text(
        "\n".join(
            [
                'Header { Title "compare24-deficit"; }',
                "Signals {",
                "  D0 InOut;",
                "  D1 InOut;",
                "}",
                "SignalGroups {",
                "  ALL = 'D0 + D1';",
                "}",
                "Timing {",
                "  WaveformTable WFT1 {",
                "    Period '40ns';",
                "    Waveforms {",
                "      D0 { H: 20ns Compare(1); L: 20ns Compare(0); T: 20ns Compare(X); }",
                "      D1 { 0: 0ns Drive(0); 1: 0ns Drive(1); }",
                "    }",
                "  }",
                "}",
                "Pattern MAIN {",
                "  W WFT1;",
                vector_lines.replace("10", "H1"),
                "}",
            ]
        ),
        encoding="utf-8",
    )
    pin_map = tmp_path / "pinmap.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 24,
                "signals": {
                    "D0": {"bit": 0, "gpio": 2, "direction": "input"},
                    "D1": {"bit": 1, "gpio": 3, "direction": "output"},
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "compare24_deficit.bin"
    with pytest.raises(StilError) as excinfo:
        compile_main(
            [
                str(stil_path),
                "-o",
                str(output),
                "--mode",
                "rate-vector",
                "--pin-map",
                str(pin_map),
            ]
        )
    assert excinfo.value.code == "ERR_PRACTICAL_COMPARE24_TIMING"
    assert "add at least" in str(excinfo.value)


def test_compile_multi_wft_creates_multiple_rates(tmp_path: Path) -> None:
    output = tmp_path / "multi_rate.bin"
    assert compile_main([str(FIXTURES / "multi_wft_rate_switch.stil"), "-o", str(output), "--mode", "rate-vector"]) == 0
    header, rates, _slots, vectors = unpack_rate_vector(output.read_bytes())
    assert header.rate_count == 2
    assert len(rates) == 2
    assert {vector.rate_id for vector in vectors} == {0, 1}


def test_auto_fallback_to_raw_event(tmp_path: Path) -> None:
    output = tmp_path / "fallback.bin"
    assert compile_main([str(FIXTURES / "rate_vector_reject_raw_fallback.stil"), "-o", str(output), "--mode", "auto"]) == 0
    header, rates, slots, vectors = unpack_rate_vector(output.read_bytes())
    assert header.rate_count == 2
    assert len(rates) == 2
    assert len(slots) >= 2
    assert len(vectors) == 2
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    assert report["mode_emitted"] == "rate-vector"
    assert report["fallback_count"] == 0
    assert not report["fallback_reasons"]


def test_strict_rejects_rate_fallback(tmp_path: Path) -> None:
    output = tmp_path / "strict.bin"
    assert compile_main([str(FIXTURES / "rate_vector_reject_raw_fallback.stil"), "-o", str(output), "--mode", "auto", "--strict"]) == 0
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    assert report["mode_emitted"] == "rate-vector"


def test_prefer_raw_forces_raw_backend(tmp_path: Path) -> None:
    output = tmp_path / "prefer_raw.bin"
    with pytest.raises(StilError) as excinfo:
        compile_main([str(FIXTURES / "single_rate_nrz.stil"), "-o", str(output), "--mode", "auto", "--prefer-raw"])
    assert excinfo.value.code == "ERR_BAD_RAW_TIMING"


def test_patternburst_requires_pattern_when_multiple(tmp_path: Path) -> None:
    with pytest.raises(StilError) as excinfo:
        compile_main(
            [
                str(FIXTURES / "patternburst_multi_pattern_select.stil"),
                "-o",
                str(tmp_path / "unused.bin"),
                "--pattern-burst",
                "PB1",
            ]
        )
    assert excinfo.value.code == "ERR_MULTIPLE_PATTERNS_UNSUPPORTED"


def test_patternburst_selects_requested_pattern(tmp_path: Path) -> None:
    output = tmp_path / "pat1.bin"
    assert compile_main(
        [
            str(FIXTURES / "patternburst_multi_pattern_select.stil"),
            "-o",
            str(output),
            "--pattern-burst",
            "PB1",
            "--pattern",
            "PAT1",
            "--mode",
            "rate-vector",
        ]
    ) == 0
    _header, _rates, _slots, vectors = unpack_rate_vector(output.read_bytes())
    assert vectors[0].drive_value == 1


def test_pn_normalization_recorded_in_compile_report(tmp_path: Path) -> None:
    output = tmp_path / "pn.bin"
    assert compile_main(
        [str(FIXTURES / "pn_norm.stil"), "-o", str(output), "--mode", "rate-vector", "--tick-hz", "150000000"]
    ) == 0
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    assert report["normalized_transition_count"] == 2
    assert len(report["normalized_transition_summary"]) == 2
    assert report["warnings"]


def test_rate_vector_preserves_pn_drive_actions_and_final_levels(tmp_path: Path) -> None:
    output = tmp_path / "pn-actions.bin"
    assert compile_main(
        [str(FIXTURES / "pn_norm.stil"), "-o", str(output), "--mode", "rate-vector", "--tick-hz", "150000000"]
    ) == 0

    header, rates, slots, _vectors, _loop_blocks = unpack_rate_vector_full(output.read_bytes())
    assert header.rate_count == 2
    assert [rate.slot_count for rate in rates] == [2, 2]
    assert [slot.time_ticks for slot in slots] == [0, 3, 0, 3]

    first_slots = slots[rates[0].slot_offset : rates[0].slot_offset + rates[0].slot_count]
    second_slots = slots[rates[1].slot_offset : rates[1].slot_offset + rates[1].slot_count]
    assert [slot.flags & 0x7 for slot in first_slots] == [STPG_DRIVE_ZERO, STPG_DRIVE_ONE]
    assert [slot.flags & 0x7 for slot in second_slots] == [STPG_DRIVE_ONE, STPG_DRIVE_ZERO]
    assert all(slot.drive_mask == 1 for slot in [*first_slots, *second_slots])

    assert _vectors[0].drive_value == 1
    assert _vectors[1].drive_value == 0


def test_raw_event_preserves_multiple_drive_actions_in_pulse(tmp_path: Path) -> None:
    stil_path = tmp_path / "multi-drive-pulse.stil"
    stil_path.write_text(
        "\n".join(
            [
                'Header { Title "multi-drive-pulse"; }',
                "Signals {",
                "  D0 InOut;",
                "}",
                "Timing {",
                "  WaveformTable WFT1 {",
                "    Period '200ns';",
                "    Waveforms {",
                "      D0 { P: 0ns Drive(0), 40ns Drive(1), 80ns Drive(0); }",
                "    }",
                "  }",
                "}",
                "Pattern MAIN {",
                "  W WFT1;",
                "  V { D0 = P; }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "multi-drive-pulse.bin"
    assert compile_main([str(stil_path), "-o", str(output), "--mode", "raw-event"]) == 0
    header, events = unpack_pattern(output.read_bytes())
    assert header.event_count == 3
    assert [event.out_value & 1 for event in events] == [0, 1, 0]
    assert [event.out_enable & 1 for event in events] == [1, 1, 1]


def test_approximation_requires_allow_approx(tmp_path: Path) -> None:
    output = tmp_path / "approx.bin"
    with pytest.raises(Exception):
        compile_main([str(FIXTURES / "approx_period.stil"), "-o", str(output), "--mode", "rate-vector"])


def test_allow_approx_records_report(tmp_path: Path) -> None:
    output = tmp_path / "approx.bin"
    assert compile_main([str(FIXTURES / "approx_period.stil"), "-o", str(output), "--mode", "rate-vector", "--allow-approx"]) == 0
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    assert report["approximate_count"] == 2
    assert report["approximation_reasons"]
    assert report["approximations"]
    first = report["approximations"][0]
    assert first["kind"] in {"period_quantization", "event_quantization"}
    assert "original_time" in first
    assert "emitted_time_ticks" in first
    assert "emitted_time_ns" in first
    assert "delta_ticks" in first
    assert "delta_ns" in first
    assert "stil_line" in first
    assert "waveform_table_name" in first
    precheck = report["capability_precheck"]
    assert precheck["input_signal_count"] >= 1
    assert precheck["mapped_signal_count"] >= 1
    assert precheck["emitted_vectors"] == report["vector_count"]
    assert precheck["selected_pattern_name"] is not None
    assert "approximate" in precheck


def test_preprocess_ategen_emits_subset_stil_and_pinmap(tmp_path: Path) -> None:
    output_dir = tmp_path / "pre"
    assert preprocess_main(
        [
            str(FPGA_FIXTURES / "FPGASTIL_RATE100_project.stil"),
            str(FPGA_FIXTURES / "Patterns" / "FPGASTIL_RATE100.stil"),
            "-o",
            str(output_dir),
            "--ignore-signal",
            "dout_0",
            "--ignore-signal",
            "dout_1",
            "--ignore-signal",
            "dout_2",
            "--ignore-signal",
            "dout_3",
            "--ignore-signal",
            "dout_4",
            "--ignore-signal",
            "dout_5",
            "--ignore-signal",
            "dout_6",
            "--ignore-signal",
            "rst_n",
            "--pin-map",
        ]
    ) == 0
    stil_path = output_dir / "FPGASTIL_RATE100.pre.stil"
    pin_map_path = output_dir / "FPGASTIL_RATE100.pre.pinmap.json"
    assert stil_path.exists()
    assert pin_map_path.exists()
    text = stil_path.read_text(encoding="utf-8")
    assert "Header {" in text
    assert "Signals {" in text
    assert "SignalGroups {" in text
    assert "Timing {" in text
    assert "Pattern FPGASTIL_RATE100_1 {" in text
    assert "//" not in text
    pin_map = json.loads(pin_map_path.read_text(encoding="utf-8"))
    assert pin_map["active_pins"] == 16
    assert pin_map["signals"]["clk"]["bit"] == 0
    assert "dout_0" not in pin_map["signals"]
    assert "rst_n" not in pin_map["signals"]


def test_stimulus_only_helper_rounds_active_pins_to_firmware_supported_width(tmp_path: Path) -> None:
    output_dir = tmp_path / "stim"
    assert stimulus_main(
        [
            str(FPGA_FIXTURES / "FPGASTIL_RATE100_project.stil"),
            str(FPGA_FIXTURES / "Patterns" / "FPGASTIL_RATE100.stil"),
            "-o",
            str(output_dir),
            "--drop-compare-prefix",
            "io_",
        ]
    ) == 0
    pin_map = json.loads((output_dir / "FPGASTIL_RATE100.pre.pinmap.json").read_text(encoding="utf-8"))
    assert pin_map["active_pins"] == 24
    report = json.loads((output_dir / "FPGASTIL_RATE100.compile_report.json").read_text(encoding="utf-8"))
    assert report["mode_emitted"] == "raw-event"
    assert report["capability_precheck"]["active_pins"] == 24


def test_compile_preprocessed_ategen_stimulus_only(tmp_path: Path) -> None:
    pre_dir = tmp_path / "pre"
    assert preprocess_main(
        [
            str(FPGA_FIXTURES / "FPGASTIL_RATE100_project.stil"),
            str(FPGA_FIXTURES / "Patterns" / "FPGASTIL_RATE100.stil"),
            "-o",
            str(pre_dir),
            "--ignore-signal",
            "dout_0",
            "--ignore-signal",
            "dout_1",
            "--ignore-signal",
            "dout_2",
            "--ignore-signal",
            "dout_3",
            "--ignore-signal",
            "dout_4",
            "--ignore-signal",
            "dout_5",
            "--ignore-signal",
            "dout_6",
            "--ignore-signal",
            "rst_n",
            "--pin-map",
        ]
    ) == 0
    stil_path = pre_dir / "FPGASTIL_RATE100.pre.stil"
    pin_map_path = pre_dir / "FPGASTIL_RATE100.pre.pinmap.json"
    output = tmp_path / "fpgastil.bin"
    assert compile_main(
        [
            str(stil_path),
            "-o",
            str(output),
            "--mode",
            "auto",
            "--pin-map",
            str(pin_map_path),
        ]
    ) == 0
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    assert report["mode_emitted"] == "raw-event"
    assert report["vector_count"] > 1000


def test_compile_preprocessed_ategen_project_mod_emits_rate_vector(tmp_path: Path) -> None:
    pre_dir = tmp_path / "pre_mod"
    assert preprocess_main(
        [
            str(FPGA_FIXTURES / "FPGASTIL_RATE100_project_mod.stil"),
            str(FPGA_FIXTURES / "Patterns" / "FPGASTIL_RATE100.stil"),
            "-o",
            str(pre_dir),
            "--ignore-signal",
            "io_oe",
            "--pin-map",
        ]
    ) == 0
    output = tmp_path / "project_mod.bin"
    assert compile_main(
        [
            str(pre_dir / "FPGASTIL_RATE100_project_mod__FPGASTIL_RATE100.pre.stil"),
            "-o",
            str(output),
            "--mode",
            "auto",
            "--pin-map",
            str(pre_dir / "FPGASTIL_RATE100_project_mod__FPGASTIL_RATE100.pre.pinmap.json"),
            "--allow-approx",
            "--practical-gate-policy",
            "report",
        ]
    ) == 0
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    assert report["mode_emitted"] == "rate-vector"
    header, rates, slots, _vectors = unpack_rate_vector(output.read_bytes())
    assert header.active_pins == 24
    assert len(rates) >= 1
    assert any(slot.time_ticks > 0 and slot.sample_mask != 0 for slot in slots)


def test_stimulus_only_helper_builds_all_outputs(tmp_path: Path) -> None:
    project = tmp_path / "demo_project.stil"
    pattern = tmp_path / "demo.stil"
    project.write_text(
        "\n".join(
            [
                "Signals {",
                "  CLK In;",
                "  DIN InOut;",
                "  DOUT Out;",
                "}",
                "SignalGroups {",
                "  ALL = 'CLK + DIN + DOUT';",
                "}",
                "Timing {",
                "  WaveformTable WFT1 {",
                "    Period '100ns';",
                "    Waveforms {",
                "      CLK { 0 { '0ns' D; } }",
                "      CLK { 1 { '0ns' U; } }",
                "      DIN { 0 { '0ns' D; } }",
                "      DIN { 1 { '0ns' U; } }",
                "      DIN { T { '50ns' H; } }",
                "      DOUT { H { '50ns' H; } }",
                "      DOUT { L { '50ns' L; } }",
                "      DOUT { T { '50ns' H; } }",
                "    }",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    pattern.write_text(
        "\n".join(
            [
                "Pattern demo {",
                "  W WFT1;",
                "  V { ALL = 10H; }",
                "  V { ALL = 01L; }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "stim"
    assert stimulus_main(
        [
            str(project),
            str(pattern),
            "-o",
            str(output_dir),
        ]
    ) == 0
    pre = output_dir / "demo.pre.stil"
    pin_map = output_dir / "demo.pre.pinmap.json"
    pattern = output_dir / "demo.bin"
    report = output_dir / "demo.compile_report.json"
    assert pre.exists()
    assert pin_map.exists()
    assert pattern.exists()
    assert report.exists()
    pin_map_data = json.loads(pin_map.read_text(encoding="utf-8"))
    assert pin_map_data["active_pins"] == 16
    assert "DOUT" not in pin_map_data["signals"]
    assert set(pin_map_data["signals"]) == {"CLK", "DIN"}


def test_stimulus_only_helper_can_drop_compare_prefix_and_compile(tmp_path: Path) -> None:
    output_dir = tmp_path / "stim"
    assert stimulus_main(
        [
            str(FPGA_FIXTURES / "FPGASTIL_RATE100_project.stil"),
            str(FPGA_FIXTURES / "Patterns" / "FPGASTIL_RATE100.stil"),
            "-o",
            str(output_dir),
            "--drop-compare-prefix",
            "io_",
        ]
    ) == 0
    report = json.loads((output_dir / "FPGASTIL_RATE100.compile_report.json").read_text(encoding="utf-8"))
    pin_map_data = json.loads((output_dir / "FPGASTIL_RATE100.pre.pinmap.json").read_text(encoding="utf-8"))
    assert report["mode_emitted"] == "raw-event"
    assert report["vector_count"] > 1000
    assert pin_map_data["active_pins"] == 24


def test_compareprep_rewrites_nonkept_compare_chars(tmp_path: Path) -> None:
    project = tmp_path / "demo_project.stil"
    pattern = tmp_path / "demo.stil"
    project.write_text(
        "\n".join(
            [
                "Signals {",
                "  A InOut;",
                "  B InOut;",
                "  C InOut;",
                "}",
                "SignalGroups {",
                "  ALL = 'A + B + C';",
                "}",
                "Timing {",
                "  WaveformTable WFT1 {",
                "    Period '100ns';",
                "    Waveforms {",
                "      A { H { '50ns' H; } }",
                "      A { 1 { '0ns' U; } }",
                "      A { T { '50ns' H; } }",
                "      B { H { '50ns' H; } }",
                "      B { 0 { '0ns' D; } }",
                "      B { T { '50ns' H; } }",
                "      C { L { '50ns' L; } }",
                "      C { T { '50ns' H; } }",
                "    }",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    pattern.write_text(
        "\n".join(
            [
                "Pattern demo {",
                "  W WFT1;",
                "  V { ALL = HHL; }",
                "  V { ALL = 10T; }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    assert compareprep_main(
        [
            str(project),
            str(pattern),
            "-o",
            str(output_dir),
            "--keep-compare-signal",
            "C",
        ]
    ) == 0
    pre = output_dir / "demo.compare.pre.stil"
    text = pre.read_text(encoding="utf-8")
    assert "V { ALL = TTL; }" in text
    assert "V { ALL = 10T; }" in text
    pin_map = json.loads((output_dir / "demo.compare.pre.pinmap.json").read_text(encoding="utf-8"))
    assert pin_map["active_pins"] == 16
    assert set(pin_map["signals"]) == {"A", "B", "C"}


def test_compareprep_practical_fixture_keeps_dout_compare(tmp_path: Path) -> None:
    output_dir = tmp_path / "compareprep"
    assert compareprep_main(
        [
            str(FPGA_FIXTURES / "FPGASTIL_RATE100_project.stil"),
            str(FPGA_FIXTURES / "Patterns" / "FPGASTIL_RATE100.stil"),
            "-o",
            str(output_dir),
            "--keep-compare-prefix",
            "dout_",
            "--ignore-out-signals",
        ]
    ) == 0
    pre = output_dir / "FPGASTIL_RATE100.compare.pre.stil"
    pin_map = output_dir / "FPGASTIL_RATE100.compare.pre.pinmap.json"
    assert pre.exists()
    assert pin_map.exists()
    pin_map_data = json.loads(pin_map.read_text(encoding="utf-8"))
    assert pin_map_data["active_pins"] == 24
    for signal_name in ("dout_0", "dout_1", "dout_2", "dout_3", "dout_4", "dout_5", "dout_6"):
        assert signal_name in pin_map_data["signals"]
        assert pin_map_data["signals"][signal_name]["direction"] == "input"
    text = pre.read_text(encoding="utf-8")
    assert "Pattern FPGASTIL_RATE100" in text


def test_compareprep_can_shift_compare_vectors(tmp_path: Path) -> None:
    project = tmp_path / "shift_project.stil"
    pattern = tmp_path / "shift.stil"
    project.write_text(
        "\n".join(
            [
                "Signals {",
                "  IN0 InOut;",
                "  D0 Out;",
                "  D1 Out;",
                "}",
                "SignalGroups {",
                "  ALL = 'IN0 + D0 + D1';",
                "}",
                "Timing {",
                "  WaveformTable WFT1 {",
                "    Period '100ns';",
                "    Waveforms {",
                "      IN0 { 0 { '0ns' D; } }",
                "      IN0 { 1 { '0ns' U; } }",
                "      D0 { H { '50ns' H; } }",
                "      D0 { L { '50ns' L; } }",
                "      D0 { T { '50ns' H; } }",
                "      D1 { H { '50ns' H; } }",
                "      D1 { L { '50ns' L; } }",
                "      D1 { T { '50ns' H; } }",
                "    }",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    pattern.write_text(
        "\n".join(
            [
                "Pattern shift {",
                "  W WFT1;",
                "  V { ALL = 0HL; }",
                "  V { ALL = 1LH; }",
                "  V { ALL = 0TT; }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    assert compareprep_main(
        [
            str(project),
            str(pattern),
            "-o",
            str(output_dir),
            "--keep-compare-prefix",
            "D",
            "--ignore-out-signals",
            "--shift-compare-prefix",
            "D",
            "--shift-compare-vectors",
            "1",
        ]
    ) == 0
    text = (output_dir / "shift.compare.pre.stil").read_text(encoding="utf-8")
    assert "V { ALL = 0TT; }" in text
    assert "V { ALL = 1HL; }" in text
    assert "V { ALL = 0LH; }" in text


def test_t_char_is_not_treated_as_compare(tmp_path: Path) -> None:
    stil = tmp_path / "t_char.stil"
    stil.write_text(
        "\n".join(
            [
                "Signals {",
                "  A InOut;",
                "}",
                "Timing {",
                "  WaveformTable W {",
                "    Period '100ns';",
                "    Waveforms {",
                "      A { 0: 0ns Drive(0); }",
                "      A { T: 0ns Compare(X); }",
                "    }",
                "  }",
                "}",
                "Pattern P {",
                "  W W;",
                "  V { A=T; }",
                "  V { A=0; }",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "t_char.bin"
    assert compile_main([str(stil), "-o", str(output), "--mode", "raw-event"]) == 0
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    assert report["mode_emitted"] == "raw-event"


def test_compile_window_range_preserves_global_index(tmp_path: Path) -> None:
    output = tmp_path / "window.bin"
    assert compile_main(
        [
            str(FIXTURES / "single_rate_nrz.stil"),
            "-o",
            str(output),
            "--mode",
            "rate-vector",
            "--vector-range",
            "1:1",
        ]
    ) == 0
    map_data = json.loads(output.with_suffix(".map.json").read_text(encoding="utf-8"))
    assert set(map_data["vectors"].keys()) == {"0"}
    source_map = map_data["vectors"]["0"]["source_map"][0]
    assert source_map["local_vector_index"] == 0
    assert source_map["global_vector_index"] == 1


def test_compile_until_vector_sets_requested_scale(tmp_path: Path) -> None:
    output = tmp_path / "scaled.bin"
    assert compile_main(
        [
            str(FIXTURES / "single_rate_nrz.stil"),
            "-o",
            str(output),
            "--mode",
            "rate-vector",
            "--until-vector",
            "0",
            "--time-scale",
            "1.05",
            "--allow-approx",
        ]
    ) == 0
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    assert report["requested_time_scale"] == "1.05"
    assert report["vector_count"] == 1


def test_compile_marker_injection_updates_map_and_report(tmp_path: Path) -> None:
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(
        json.dumps(
                {
                    "active_pins": 16,
                    "signals": {
                        "D0": {"bit": 0, "role": "signal"},
                        "D1": {"bit": 1, "role": "signal"},
                    "__MARK0": {"bit": 2, "role": "marker"},
                    "__FAIL0": {"bit": 3, "role": "fail"},
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "marked.bin"
    assert compile_main(
        [
            str(FIXTURES / "single_rate_nrz.stil"),
            "-o",
            str(output),
            "--mode",
            "rate-vector",
            "--pin-map",
            str(pin_map),
            "--marker",
            "__MARK0:vector=0:mode=pulse",
        ]
    ) == 0
    map_data = json.loads(output.with_suffix(".map.json").read_text(encoding="utf-8"))
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    assert map_data["markers"][0]["name"] == "__MARK0"
    assert map_data["vectors"]["0"]["marker_events"][0]["name"] == "__MARK0"
    assert report["markers"][0]["name"] == "__MARK0"
    assert report["terminal_drive_enable"] & (1 << 2)


def test_compile_marker_level_mode_and_clear_vector(tmp_path: Path) -> None:
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(
        json.dumps(
                {
                    "active_pins": 16,
                    "signals": {
                        "D0": {"bit": 0, "role": "signal"},
                        "D1": {"bit": 1, "role": "signal"},
                    "__MARK0": {"bit": 2, "role": "marker"},
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "marked.bin"
    assert compile_main(
        [
            str(FIXTURES / "single_rate_nrz.stil"),
            "-o",
            str(output),
            "--mode",
            "raw-event",
            "--tick-hz",
            "300000000",
            "--pin-map",
            str(pin_map),
            "--marker",
            "__MARK0:vector=0:mode=level:set",
            "--marker-clear",
            "__MARK0:vector=1",
        ]
    ) == 0
    map_data = json.loads(output.with_suffix(".map.json").read_text(encoding="utf-8"))
    assert map_data["markers"][0]["mode"] == "level:set"
    assert map_data["markers"][0]["clear_vector"] == 1


def test_compile_marker_pulse_width_updates_map(tmp_path: Path) -> None:
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(
        json.dumps(
                {
                "active_pins": 16,
                "signals": {
                    "D0": {"bit": 0, "role": "signal"},
                    "D1": {"bit": 1, "role": "signal"},
                    "__MARK0": {"bit": 2, "role": "marker"},
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "marked.bin"
    assert compile_main(
        [
            str(FIXTURES / "single_rate_nrz.stil"),
            "-o",
            str(output),
            "--mode",
            "raw-event",
            "--tick-hz",
            "300000000",
            "--pin-map",
            str(pin_map),
            "--marker",
            "__MARK0:vector=0:mode=pulse:width=2",
        ]
    ) == 0
    map_data = json.loads(output.with_suffix(".map.json").read_text(encoding="utf-8"))
    assert map_data["markers"][0]["width"] == 2
    assert map_data["vectors"]["0"]["marker_events"][0]["width"] == 2
    assert map_data["vectors"]["1"]["marker_events"][0]["width"] == 2


def test_compile_marker_can_address_stil_vector_line_and_emits_marker_bit(tmp_path: Path) -> None:
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 16,
                "signals": {
                    "D0": {"bit": 0, "role": "signal"},
                    "D1": {"bit": 1, "role": "signal"},
                    "__REF0": {"bit": 2, "role": "marker"},
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "line-marked.bin"
    assert compile_main(
        [
            str(FIXTURES / "single_rate_nrz.stil"),
            "-o",
            str(output),
            "--mode",
            "rate-vector",
            "--pin-map",
            str(pin_map),
            "--marker",
            "__REF0:line=24:mode=pulse",
        ]
    ) == 0
    map_data = json.loads(output.with_suffix(".map.json").read_text(encoding="utf-8"))
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    marker = map_data["markers"][0]
    assert marker["address_kind"] == "stil_line"
    assert marker["line"] == 24
    assert marker["vector"] == 0
    assert map_data["vectors"]["0"]["marker_events"][0]["global_vector_index"] == 0
    assert report["markers"][0]["address_kind"] == "stil_line"

    _header, _rates, _slots, vectors, _loops = unpack_rate_vector_full(output.read_bytes())
    assert vectors[0].drive_value & (1 << 2)
    assert not (vectors[1].drive_value & (1 << 2))


def test_compile_marker_line_must_identify_one_vector(tmp_path: Path) -> None:
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 16,
                "signals": {
                    "D0": {"bit": 0, "role": "signal"},
                    "D1": {"bit": 1, "role": "signal"},
                    "__REF0": {"bit": 2, "role": "marker"},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StilError) as excinfo:
        compile_main(
            [
                str(FIXTURES / "single_rate_nrz.stil"),
                "-o",
                str(tmp_path / "bad.bin"),
                "--mode",
                "raw-event",
                "--pin-map",
                str(pin_map),
                "--marker",
                "__REF0:line=23:mode=pulse",
            ]
        )
    assert excinfo.value.code == "ERR_MARKER_LINE_NOT_FOUND"


def test_marker_on_logical_bit20_rejected_for_current_24pin_board(tmp_path: Path) -> None:
    pin_map = tmp_path / "pins24.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 24,
                "signals": {
                    "D0": {"bit": 0, "role": "signal"},
                    "D1": {"bit": 1, "role": "signal"},
                    "__MARK0": {"bit": 20, "role": "marker"},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StilError) as excinfo:
        compile_main(
            [
                str(FIXTURES / "single_rate_nrz.stil"),
                "-o",
                str(tmp_path / "bad.bin"),
                "--mode",
                "rate-vector",
                "--pin-map",
                str(pin_map),
                "--marker",
                "__MARK0:vector=0:mode=pulse",
            ]
        )
    assert excinfo.value.code == "ERR_MARKER_UNOBSERVABLE_BIT"


def test_pin_map_rejects_invalid_role(tmp_path: Path) -> None:
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(json.dumps({"active_pins": 16, "signals": {"D0": {"bit": 0, "role": "weird"}}}), encoding="utf-8")
    with pytest.raises(StilError) as excinfo:
        compile_main([str(FIXTURES / "single_rate_nrz.stil"), "-o", str(tmp_path / "out.bin"), "--mode", "raw-event", "--pin-map", str(pin_map)])
    assert excinfo.value.code == "ERR_BAD_PIN_MAP"


def test_pin_map_rejects_marker_fail_share_without_allow_shared(tmp_path: Path) -> None:
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 16,
                "signals": {
                    "D0": {"bit": 0, "role": "signal"},
                    "__MARK0": {"bit": 0, "role": "marker"},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StilError) as excinfo:
        compile_main(
            [
                str(FIXTURES / "single_rate_nrz.stil"),
                "-o",
                str(tmp_path / "out.bin"),
                "--mode",
                "raw-event",
                "--pin-map",
                str(pin_map),
                "--marker",
                "__MARK0:vector=0:mode=pulse",
            ]
        )
    assert excinfo.value.code == "ERR_BAD_PIN_MAP"


def test_unmapped_active_signal_rejected(tmp_path: Path) -> None:
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(json.dumps({"active_pins": 16, "signals": {"D0": {"bit": 0}}}), encoding="utf-8")
    with pytest.raises(StilError) as excinfo:
        compile_main([str(FIXTURES / "single_rate_nrz.stil"), "-o", str(tmp_path / "out.bin"), "--mode", "rate-vector", "--pin-map", str(pin_map)])
    assert excinfo.value.code == "ERR_UNMAPPED_ACTIVE_SIGNAL"


def test_unmapped_xt_signal_is_ignored(tmp_path: Path) -> None:
    fixture = tmp_path / "ignored_xt.stil"
    fixture.write_text(
        """Header { Title \"ignored-xt\"; }\nSignals {\n  D0 InOut;\n  D1 InOut;\n}\nSignalGroups {\n  ALL = 'D0 + D1';\n}\nTiming {\n  WaveformTable WFT1 {\n    Period '40ns';\n    Waveforms {\n      ALL { 0: 0ns Drive(0); X: 20ns Compare(X); T: 20ns Compare(X); }\n    }\n  }\n}\nPattern MAIN {\n  W WFT1;\n  V { ALL = XT; }\n}\n""",
        encoding="utf-8",
    )
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(json.dumps({"active_pins": 16, "signals": {"D0": {"bit": 0}}}), encoding="utf-8")
    output = tmp_path / "xt.bin"
    assert compile_main(
        [str(fixture), "-o", str(output), "--mode", "raw-event", "--tick-hz", "300000000", "--pin-map", str(pin_map)]
    ) == 0


def test_map_and_fail_report_roundtrip(tmp_path: Path) -> None:
    output = tmp_path / "compile.bin"
    assert compile_main([str(FIXTURES / "single_rate_nrz.stil"), "-o", str(output), "--mode", "rate-vector"]) == 0
    fail_json = tmp_path / "fail.json"
    fail_json.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "vector_index": 0,
                        "repeat_iteration": 0,
                        "slot_index": 0,
                        "sample_value": 0,
                        "expect_value": 1,
                        "effective_mask": 1,
                        "fail_bits": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    text = render_fail_report(fail_json, output.with_suffix(".map.json"))
    assert "line=" in text
    assert "signals=['D0']" in text
    assert "sample=0x0000" in text


def test_fail_report_uses_global_vector_index_for_window(tmp_path: Path) -> None:
    output = tmp_path / "window.bin"
    assert compile_main(
        [
            str(FIXTURES / "single_rate_nrz.stil"),
            "-o",
            str(output),
            "--mode",
            "rate-vector",
            "--vector-range",
            "1:1",
        ]
    ) == 0
    fail_json = tmp_path / "fail.json"
    fail_json.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "vector_index": 0,
                        "repeat_iteration": 0,
                        "slot_index": 0,
                        "sample_value": 0,
                        "expect_value": 1,
                        "effective_mask": 1,
                        "fail_bits": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    text = render_fail_report(fail_json, output.with_suffix(".map.json"))
    assert "vector=1 local_vector=0" in text


def test_rate_vector_map_tracks_repeat_folding_iterations(tmp_path: Path) -> None:
    fixture = tmp_path / "fold_repeat.stil"
    fixture.write_text(
        "\n".join(
            [
                'Header { Title "fold-repeat"; }',
                "Signals {",
                "  D0 InOut;",
                "}",
                "Timing {",
                "  WaveformTable WFT1 {",
                "    Period '100ns';",
                "    Waveforms {",
                "      D0 { 1: 0ns Drive(1); }",
                "    }",
                "  }",
                "}",
                "Pattern MAIN {",
                "  W WFT1;",
                "  V { D0 = 1; }",
                "  V { D0 = 1; }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "fold_repeat.bin"
    assert compile_main([str(fixture), "-o", str(output), "--mode", "rate-vector"]) == 0
    map_data = json.loads(output.with_suffix(".map.json").read_text(encoding="utf-8"))
    vector0 = map_data["vectors"]["0"]
    assert vector0["repeat"] == 2
    assert [item["repeat_iteration"] for item in vector0["source_map"]] == [0, 1]
    assert [item["stil_line"] for item in vector0["source_map"]] == [15, 16]


def test_fail_report_uses_repeat_iteration_source_entry(tmp_path: Path) -> None:
    fixture = tmp_path / "fold_repeat.stil"
    fixture.write_text(
        "\n".join(
            [
                'Header { Title "fold-repeat"; }',
                "Signals {",
                "  D0 InOut;",
                "}",
                "Timing {",
                "  WaveformTable WFT1 {",
                "    Period '100ns';",
                "    Waveforms {",
                "      D0 { 1: 0ns Drive(1); }",
                "    }",
                "  }",
                "}",
                "Pattern MAIN {",
                "  W WFT1;",
                "  V { D0 = 1; }",
                "  V { D0 = 1; }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "fold_repeat.bin"
    assert compile_main([str(fixture), "-o", str(output), "--mode", "rate-vector"]) == 0
    fail_json = tmp_path / "fail.json"
    fail_json.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "vector_index": 0,
                        "repeat_iteration": 1,
                        "slot_index": 0,
                        "sample_value": 0,
                        "expect_value": 1,
                        "effective_mask": 1,
                        "fail_bits": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    text = render_fail_report(fail_json, output.with_suffix(".map.json"))
    assert "line=16" in text


def test_build_state_update_pattern_preserves_terminal_state() -> None:
    report_data = {
        "capability_precheck": {
            "active_pins": 16,
            "tick_hz": 150_000_000,
        },
        "terminal_drive_value": 0x0003,
        "terminal_drive_enable": 0x0003,
    }
    map_data = {
        "signal_map": {
            "__FAIL0": {"bit": 4, "logical_pin": "DUT4", "role": "fail", "allow_shared": False},
        }
    }
    payload = _build_state_update_pattern(report_data, map_data, "__FAIL0", asserted=True)
    _header, events = unpack_pattern(payload)
    assert events[0].out_value == 0x0013
    assert events[0].out_enable == 0x0013


def test_emit_fail_indication_uses_preserve_gpio_state_flag() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.uploaded = None
            self.run_flags = None
            self.status_called = False

        def upload(self, payload, storage_target, chunk_size):
            self.uploaded = (payload, storage_target, chunk_size)

        def run(self, run_flags=0, start_event_index=0, max_events=0):
            self.run_flags = run_flags
            return None

        def run_preserve_gpio_state(self):
            self.run_flags = RUN_FLAG_PRESERVE_GPIO_STATE
            return None

        def status(self):
            self.status_called = True
            return None

    report_data = {
        "capability_precheck": {
            "active_pins": 16,
            "tick_hz": 150_000_000,
        },
        "terminal_drive_value": 0x0003,
        "terminal_drive_enable": 0x0003,
    }
    map_data = {
        "signal_map": {
            "__FAIL0": {"bit": 4, "logical_pin": "DUT4", "role": "fail", "allow_shared": False},
        }
    }
    client = FakeClient()
    _emit_fail_indication(client, report_data, map_data, "__FAIL0", asserted=True, chunk_size=4096)
    assert client.uploaded is not None
    assert client.run_flags == RUN_FLAG_PRESERVE_GPIO_STATE
    assert client.status_called is True


def test_render_fail_report_text_from_fail_entries(tmp_path: Path) -> None:
    output = tmp_path / "compile.bin"
    assert compile_main([str(FIXTURES / "single_rate_nrz.stil"), "-o", str(output), "--mode", "rate-vector"]) == 0
    map_data = json.loads(output.with_suffix(".map.json").read_text(encoding="utf-8"))
    from stpg.client import FailLogEntry

    text = render_fail_report_text([FailLogEntry(event_index=0, actual=0, expected=1, mask=1)], map_data)
    assert "fail_bits=0x0001" in text


def test_compare_hlt_emits_raw_event(tmp_path: Path) -> None:
    output = tmp_path / "compare.bin"
    assert compile_main([str(FIXTURES / "compare_hlt.stil"), "-o", str(output), "--mode", "raw-event", "--tick-hz", "300000000"]) == 0
    _header, events = unpack_pattern(output.read_bytes())
    assert any(event.sample_mask != 0 for event in events)


def test_parser_rejects_unsupported_block(tmp_path: Path) -> None:
    fixture = tmp_path / "procedure.stil"
    fixture.write_text("Procedure P0 { }\n", encoding="utf-8")
    with pytest.raises(StilError) as excinfo:
        parse_stil_file(fixture)
    assert excinfo.value.code == "ERR_UNSUPPORTED_BLOCK"


def test_public_regression_cases_have_expected_modes() -> None:
    cases = build_regression_cases()
    assert cases["single-rate-nrz"].expect_mode_emitted == "rate-vector"
    assert cases["raw-fallback"].expect_mode_emitted == "rate-vector"
    assert cases["approx-period"].allow_approx is True


def test_public_regression_runner(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert regression_main(["--fixture-dir", str(FIXTURES), "--output-dir", str(tmp_path), "--cases", "single-rate-nrz", "raw-fallback"]) == 0
    output = capsys.readouterr().out
    assert "single-rate-nrz: PASS" in output
    assert "raw-fallback: PASS" in output


def test_runtime_summary_writer(tmp_path: Path) -> None:
    from compat_cli.runtime import RunRecord

    summary = tmp_path / "summary.json"
    fail_report = tmp_path / "fail.txt"
    _write_outputs(
        [RunRecord(scale="1.0", repeat_index=0, state=6, error_code=0, sample_count=0, fail_count=0, result_flags=0, mode_emitted="rate-vector", run_path="streaming-poc", output_file=str(tmp_path / "a.bin"))],
        ["example-fail-report"],
        summary,
        fail_report,
    )
    saved = json.loads(summary.read_text(encoding="utf-8"))
    assert saved[0]["mode_emitted"] == "rate-vector"
    assert fail_report.read_text(encoding="utf-8") == "example-fail-report"


def test_runtime_wait_for_terminal_status_done() -> None:
    class FakeStatus:
        def __init__(self, state: int) -> None:
            self.state = state

        def __repr__(self) -> str:
            return f"FakeStatus(state={self.state})"

    class FakeClient:
        def __init__(self) -> None:
            self.values = [FakeStatus(4), FakeStatus(5), FakeStatus(6)]

        def status(self):
            return self.values.pop(0)

    status = _wait_for_terminal_status(FakeClient(), timeout_s=0.5)
    assert status.state == 6


def test_runtime_wait_for_terminal_status_error() -> None:
    class FakeStatus:
        def __init__(self, state: int) -> None:
            self.state = state

        def __repr__(self) -> str:
            return f"FakeStatus(state={self.state})"

    class FakeClient:
        def __init__(self) -> None:
            self.values = [FakeStatus(4), FakeStatus(7)]

        def status(self):
            return self.values.pop(0)

    status = _wait_for_terminal_status(FakeClient(), timeout_s=0.5)
    assert status.state == 7


def test_raw_event_splits_mid_vector_compare_timing(tmp_path: Path) -> None:
    stil_path = tmp_path / "mid_compare.stil"
    stil_path.write_text(
        "\n".join(
            [
                "Signals {",
                "  CLK Out;",
                "  DOUT In;",
                "}",
                "SignalGroups {",
                "  G = 'CLK + DOUT';",
                "}",
                "Timing {",
                "  WaveformTable WFT0 {",
                "    Period '100ns';",
                "    Waveforms {",
                "      CLK { 1: 0s Drive(1); }",
                "      CLK { 0: 0s Drive(0); }",
                "      DOUT { H: 50ns Compare(1); }",
                "      DOUT { L: 50ns Compare(0); }",
                "    }",
                "  }",
                "}",
                "Pattern P0 {",
                "  W WFT0;",
                "  V { G=0L; }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    pin_map = tmp_path / "pins.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 16,
                "signals": {
                    "CLK": {"bit": 0, "logical_pin": "DUT0", "direction": "output", "role": "signal"},
                    "DOUT": {"bit": 1, "logical_pin": "DUT1", "direction": "input", "role": "signal"},
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "mid_compare.bin"
    assert compile_main([str(stil_path), "-o", str(output), "--mode", "raw-event", "--allow-approx", "--pin-map", str(pin_map)]) == 0
    _header, events = unpack_pattern(output.read_bytes())
    assert len(events) == 2
    assert events[0].sample_mask == 0
    assert events[1].sample_mask == 0x0002


def test_build_selfloop_pin_map_basic() -> None:
    pin_map = build_selfloop_pin_map(active_pins=16, drive_bit=1, observe_bit=2, marker_bit=3, fail_bit=4)
    assert pin_map["signals"]["DRV"]["bit"] == 1
    assert pin_map["signals"]["OBS"]["bit"] == 2
    assert pin_map["signals"]["__MARK0"]["role"] == "marker"
    assert pin_map["signals"]["__FAIL0"]["role"] == "fail"


def test_build_selfloop_pin_map_rejects_bit20_for_marker_fail() -> None:
    with pytest.raises(ValueError):
        build_selfloop_pin_map(active_pins=24, drive_bit=1, observe_bit=2, marker_bit=20, fail_bit=None)
    with pytest.raises(ValueError):
        build_selfloop_pin_map(active_pins=24, drive_bit=1, observe_bit=2, marker_bit=None, fail_bit=20)


def test_selfloop_fixture_compiles_pass_and_fail(tmp_path: Path) -> None:
    pin_map = tmp_path / "selfloop_pins.json"
    pin_map.write_text(
        json.dumps(build_selfloop_pin_map(active_pins=16, drive_bit=1, observe_bit=2, marker_bit=3, fail_bit=4)),
        encoding="utf-8",
    )
    output_pass = tmp_path / "selfloop_pass.bin"
    output_fail = tmp_path / "selfloop_fail.bin"
    assert compile_main(
        [
            str(FIXTURES / "selfloop_compare_pass.stil"),
            "-o",
            str(output_pass),
            "--mode",
            "auto",
            "--pin-map",
            str(pin_map),
            "--marker",
            "__MARK0:vector=1:mode=pulse",
        ]
    ) == 0
    assert compile_main(
        [
            str(FIXTURES / "selfloop_compare_fail.stil"),
            "-o",
            str(output_fail),
            "--mode",
            "auto",
            "--pin-map",
            str(pin_map),
        ]
    ) == 0
    report = json.loads(output_pass.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    assert report["markers"][0]["name"] == "__MARK0"


def test_device_precheck_detects_rate_vector_sample_compare_requirements(tmp_path: Path) -> None:
    output = tmp_path / "compare.bin"
    assert compile_main([str(FIXTURES / "compare_hlt.stil"), "-o", str(output), "--mode", "rate-vector"]) == 0
    req = inspect_pattern_requirements(output.read_bytes())
    assert req.mode == "rate-vector"
    assert req.requires_sample is True
    assert req.requires_compare is True
    assert req.estimated_event_count > 0
    assert req.streaming_poc_incompatibility is not None


def test_device_precheck_rejects_missing_rate_vector_capability(tmp_path: Path) -> None:
    output = tmp_path / "compare.bin"
    assert compile_main([str(FIXTURES / "compare_hlt.stil"), "-o", str(output), "--mode", "rate-vector"]) == 0
    req = inspect_pattern_requirements(output.read_bytes())
    with pytest.raises(RuntimeError, match="rate-vector capability"):
        validate_device_compatibility(
            req,
            device_clock_tick_hz=req.tick_hz,
            device_active_pins=req.active_pins,
            device_capability=CAP_UPLOAD_PSRAM | CAP_RUN_16PIN | CAP_SAMPLE_CAPTURE | CAP_EXPECT_COMPARE,
            storage_target=STORAGE_PSRAM,
        )


def test_device_precheck_rejects_missing_compare_capability(tmp_path: Path) -> None:
    output = tmp_path / "compare.bin"
    assert compile_main([str(FIXTURES / "compare_hlt.stil"), "-o", str(output), "--mode", "rate-vector"]) == 0
    req = inspect_pattern_requirements(output.read_bytes())
    with pytest.raises(RuntimeError, match="requires compare"):
        validate_device_compatibility(
            req,
            device_clock_tick_hz=req.tick_hz,
            device_active_pins=req.active_pins,
            device_capability=CAP_UPLOAD_PSRAM | CAP_RUN_16PIN | CAP_RATE_VECTOR_MODE | CAP_SAMPLE_CAPTURE,
            storage_target=STORAGE_PSRAM,
        )


def test_device_precheck_rejects_missing_streaming_capability(tmp_path: Path) -> None:
    output = tmp_path / "single_rate.bin"
    assert compile_main([str(FIXTURES / "single_rate_nrz.stil"), "-o", str(output), "--mode", "rate-vector"]) == 0
    req = inspect_pattern_requirements(output.read_bytes())
    with pytest.raises(RuntimeError, match="streaming-poc"):
        validate_device_compatibility(
            req,
            device_clock_tick_hz=req.tick_hz,
            device_active_pins=req.active_pins,
            device_capability=CAP_UPLOAD_PSRAM | CAP_RUN_16PIN | CAP_RATE_VECTOR_MODE,
            storage_target=STORAGE_PSRAM,
            run_flags=RUN_FLAG_STREAMING_POC,
        )


def test_device_precheck_rejects_rate_vector_sram_target(tmp_path: Path) -> None:
    output = tmp_path / "single_rate.bin"
    assert compile_main([str(FIXTURES / "single_rate_nrz.stil"), "-o", str(output), "--mode", "rate-vector"]) == 0
    req = inspect_pattern_requirements(output.read_bytes())
    with pytest.raises(RuntimeError, match="PSRAM or Flash target"):
        validate_device_compatibility(
            req,
            device_clock_tick_hz=req.tick_hz,
            device_active_pins=req.active_pins,
            device_capability=CAP_UPLOAD_SRAM | CAP_RUN_16PIN | CAP_RATE_VECTOR_MODE,
            storage_target=STORAGE_SRAM,
        )


def test_device_precheck_accepts_24pin_rate_vector_device(tmp_path: Path) -> None:
    pin_map = tmp_path / "pinmap.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 24,
                "signals": {
                    "D0": {"bit": 0, "gpio": 2, "direction": "inout"},
                    "D1": {"bit": 1, "gpio": 3, "direction": "inout"},
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "compare24.bin"
    assert compile_main(
        [str(FIXTURES / "compare_hlt.stil"), "-o", str(output), "--mode", "rate-vector", "--pin-map", str(pin_map)]
    ) == 0
    req = inspect_pattern_requirements(output.read_bytes())
    validate_device_compatibility(
        req,
        device_clock_tick_hz=req.tick_hz,
        device_active_pins=req.active_pins,
        device_capability=(
            CAP_UPLOAD_FLASH
            | CAP_UPLOAD_PSRAM
            | CAP_UPLOAD_SRAM
            | CAP_RUN_24PIN
            | CAP_RATE_VECTOR_MODE
            | CAP_SAMPLE_CAPTURE
            | CAP_EXPECT_COMPARE
            | CAP_PSRAM_STREAMING
        ),
        storage_target=STORAGE_PSRAM,
    )


def test_device_precheck_accepts_unknown_active_pins_for_24pin_device(tmp_path: Path) -> None:
    pin_map = tmp_path / "pinmap.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 24,
                "signals": {
                    "D0": {"bit": 0, "gpio": 2, "direction": "inout"},
                    "D1": {"bit": 1, "gpio": 3, "direction": "inout"},
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "compare24_unknown.bin"
    assert compile_main(
        [str(FIXTURES / "compare_hlt.stil"), "-o", str(output), "--mode", "rate-vector", "--pin-map", str(pin_map)]
    ) == 0
    req = inspect_pattern_requirements(output.read_bytes())
    validate_device_compatibility(
        req,
        device_clock_tick_hz=req.tick_hz,
        device_active_pins=0,
        device_capability=(
            CAP_UPLOAD_FLASH
            | CAP_UPLOAD_PSRAM
            | CAP_UPLOAD_SRAM
            | CAP_RUN_24PIN
            | CAP_RATE_VECTOR_MODE
            | CAP_SAMPLE_CAPTURE
            | CAP_EXPECT_COMPARE
            | CAP_PSRAM_STREAMING
        ),
        storage_target=STORAGE_PSRAM,
    )


def test_device_precheck_rejects_non_streaming_psram_overflow(tmp_path: Path) -> None:
    pin_map = tmp_path / "pinmap.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 24,
                "signals": {f"D{i}": {"bit": i, "gpio": i + 2, "direction": "inout"} for i in range(24)},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "practical_like.bin"
    assert compile_main(
        [
            str(FIXTURES / "compare_hlt.stil"),
            "-o",
            str(output),
            "--mode",
            "rate-vector",
            "--pin-map",
            str(pin_map),
        ]
    ) == 0
    req = inspect_pattern_requirements(output.read_bytes())
    overflow_req = type(req)(
        tick_hz=req.tick_hz,
        active_pins=req.active_pins,
        mode=req.mode,
        requires_sample=req.requires_sample,
        requires_compare=req.requires_compare,
        estimated_event_count=6552,
        streaming_poc_incompatibility=None,
    )
    with pytest.raises(RuntimeError, match="rerun with --streaming-poc"):
        validate_device_compatibility(
            overflow_req,
            device_clock_tick_hz=req.tick_hz,
            device_active_pins=0,
            device_capability=(
                CAP_UPLOAD_PSRAM
                | CAP_RUN_24PIN
                | CAP_RATE_VECTOR_MODE
                | CAP_SAMPLE_CAPTURE
                | CAP_EXPECT_COMPARE
                | CAP_PSRAM_STREAMING
            ),
            storage_target=STORAGE_PSRAM,
        )


def test_device_precheck_allows_upload_only_for_oversized_pattern(tmp_path: Path) -> None:
    pin_map = tmp_path / "pinmap.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 16,
                "signals": {f"D{i}": {"bit": i, "gpio": i + 2, "direction": "inout"} for i in range(16)},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "upload_only_large.bin"
    assert compile_main(
        [
            str(FIXTURES / "compare_hlt.stil"),
            "-o",
            str(output),
            "--mode",
            "rate-vector",
            "--pin-map",
            str(pin_map),
        ]
    ) == 0
    req = inspect_pattern_requirements(output.read_bytes())
    capabilities = (
        CAP_UPLOAD_PSRAM
        | CAP_RUN_16PIN
        | CAP_RATE_VECTOR_MODE
        | CAP_SAMPLE_CAPTURE
        | CAP_EXPECT_COMPARE
    )
    validate_device_compatibility(
        req,
        device_clock_tick_hz=req.tick_hz,
        device_active_pins=16,
        device_capability=capabilities,
        storage_target=STORAGE_PSRAM,
        run_requested=False,
    )


def test_device_precheck_rejects_upload_only_without_practical_capability(tmp_path: Path) -> None:
    pin_map = tmp_path / "pinmap.json"
    pin_map.write_text(
        json.dumps(
            {
                "active_pins": 16,
                "signals": {f"D{i}": {"bit": i, "gpio": i + 2, "direction": "inout"} for i in range(16)},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "upload_only_large.bin"
    assert compile_main(
        [
            str(FIXTURES / "compare_hlt.stil"),
            "-o",
            str(output),
            "--mode",
            "rate-vector",
            "--pin-map",
            str(pin_map),
        ]
    ) == 0
    req = inspect_pattern_requirements(output.read_bytes())
    capabilities = (
        CAP_UPLOAD_PSRAM
        | CAP_RUN_16PIN
        | CAP_RATE_VECTOR_MODE
        | CAP_SAMPLE_CAPTURE
        | CAP_EXPECT_COMPARE
    )
    oversized_req = replace(req, estimated_event_count=7000)
    with pytest.raises(RuntimeError, match="before upload/RUN"):
        validate_device_compatibility(
            oversized_req,
            device_clock_tick_hz=req.tick_hz,
            device_active_pins=16,
            device_capability=capabilities,
            storage_target=STORAGE_PSRAM,
            run_requested=False,
        )


def test_compile_report_records_streaming_runtime_precheck(tmp_path: Path) -> None:
    output = tmp_path / "compare.bin"
    assert compile_main([str(FIXTURES / "compare_hlt.stil"), "-o", str(output), "--mode", "rate-vector"]) == 0
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    runtime_precheck = report["capability_precheck"]["runtime_precheck"]
    assert runtime_precheck["estimated_event_count"] > 0
    assert runtime_precheck["non_streaming_event_limit"] == 6551
    assert runtime_precheck["streaming_poc_compatible"] is False
    assert "streaming-poc" in runtime_precheck["streaming_poc_incompatibility"]
    assert runtime_precheck["streaming_poc_timing_issues"]
    assert all("deficit_ticks" in issue for issue in runtime_precheck["streaming_poc_timing_issues"])
    assert isinstance(runtime_precheck["reduced_density_candidate"], bool)
    assert isinstance(runtime_precheck["reduced_density_incompatibilities"], list)
    assert isinstance(runtime_precheck["exact_density_opportunities"], list)
    assert "micro_op_family_summary" in runtime_precheck
    assert "micro_op_unsupported_examples" in runtime_precheck
    assert "micro_op_family_examples" in runtime_precheck
    assert isinstance(runtime_precheck["practical_chunk_windows"], list)
    assert isinstance(runtime_precheck["practical_generator_avoidance_candidates"], list)
    assert isinstance(runtime_precheck["practical_local_wait_candidates"], list)
    assert "micro_op_family_analysis" in report


def _write_family_fixture(
    path: Path,
    *,
    observe_char: str,
    observe_time: str,
    drive_char: str | None,
    drive_time: str | None,
) -> None:
    drive_waveform = "      DDRV { 0: 0ns Drive(0); 1: 0ns Drive(1); }\n"
    if drive_time is not None:
        drive_waveform = f"      DDRV {{ 0: 0ns Drive(0); 1: {drive_time} Drive(1); }}\n"
    vector_chars = observe_char if drive_char is None else f"{observe_char}{drive_char}"
    signals = ["  DOBS InOut;"]
    group = ""
    waveforms = [f"      DOBS {{ H: {observe_time} Compare(1); L: {observe_time} Compare(0); X: {observe_time} Compare(X); }}"]
    if drive_char is not None:
        signals.append("  DDRV InOut;")
        group = "SignalGroups {\n  ALL = 'DOBS + DDRV';\n}\n"
        waveforms.append(drive_waveform.rstrip())
        vector_line = f"  V {{ ALL = {vector_chars}; }}"
    else:
        vector_line = f"  V {{ DOBS = {vector_chars}; }}"
    path.write_text(
        "".join(
            [
                'Header { Title "family"; }\n',
                "Signals {\n",
                *[line + "\n" for line in signals],
                "}\n",
                group,
                "Timing {\n",
                "  WaveformTable WFT1 {\n",
                "    Period '100ns';\n",
                "    Waveforms {\n",
                *[line + "\n" for line in waveforms],
                "    }\n",
                "  }\n",
                "}\n",
                "Pattern MAIN {\n",
                "  W WFT1;\n",
                f"{vector_line}\n",
                "}\n",
            ]
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("name", "observe_char", "observe_time", "drive_char", "drive_time", "expected_family", "expected_codes"),
    [
        ("m1", "H", "20ns", "1", "0ns", "M1_DRIVE_THEN_SAMPLE", set()),
        ("m2", "H", "0ns", "1", "20ns", "M2_SAMPLE_THEN_DRIVE", set()),
        ("m3", "T", "0ns", "1", "0ns", "M3_DRIVE_ONLY", set()),
        ("m4", "H", "20ns", None, None, "M4_SAMPLE_ONLY", set()),
        (
            "unsupported",
            "H",
            "0ns",
            "1",
            "0ns",
            "M5_SIMULTANEOUS_SAMPLE_DRIVE",
            set(),
        ),
    ],
)
def test_compile_report_records_micro_op_family_analysis(
    tmp_path: Path,
    name: str,
    observe_char: str,
    observe_time: str,
    drive_char: str | None,
    drive_time: str | None,
    expected_family: str,
    expected_codes: set[str],
) -> None:
    fixture = tmp_path / f"{name}.stil"
    _write_family_fixture(
        fixture,
        observe_char=observe_char,
        observe_time=observe_time,
        drive_char=drive_char,
        drive_time=drive_time,
    )
    output = tmp_path / f"{name}.bin"
    assert compile_main([str(fixture), "-o", str(output), "--mode", "rate-vector"]) == 0
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    summary = report["micro_op_family_analysis"]["summary"]
    assert summary["family_histogram"][expected_family] == 1
    assert expected_family in summary["supported_families"]
    runtime_precheck = report["capability_precheck"]["runtime_precheck"]
    assert runtime_precheck["micro_op_family_summary"]["family_histogram"][expected_family] == 1
    codes = {item["code"] for item in report["diagnostics"]}
    for code in expected_codes:
        assert code in codes


def test_practical_compile_report_emits_chunk_boundary_warnings(tmp_path: Path) -> None:
    pre_dir = tmp_path / "pre_warn"
    assert preprocess_main(
        [
            str(FPGA_FIXTURES / "FPGASTIL_RATE100_project_mod.stil"),
            str(FPGA_FIXTURES / "Patterns" / "FPGASTIL_RATE100.stil"),
            "-o",
            str(pre_dir),
            "--ignore-signal",
            "io_oe",
            "--pin-map",
        ]
    ) == 0
    output = tmp_path / "practical_warn.bin"
    assert compile_main(
        [
            str(pre_dir / "FPGASTIL_RATE100_project_mod__FPGASTIL_RATE100.pre.stil"),
            "-o",
            str(output),
            "--mode",
            "auto",
            "--pin-map",
            str(pre_dir / "FPGASTIL_RATE100_project_mod__FPGASTIL_RATE100.pre.pinmap.json"),
            "--allow-approx",
            "--practical-gate-policy",
            "report",
        ]
    ) == 0
    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    codes = {item["code"] for item in report["diagnostics"]}
    assert "WARN_PRACTICAL_PREDICTED_CHUNK_BOUNDARY_CROSSING" in codes
    assert "WARN_PRACTICAL_LONG_HOMOGENEOUS_COMPARE_RUN" in codes
    runtime_precheck = report["capability_precheck"]["runtime_precheck"]
    assert runtime_precheck["practical_chunk_windows"]
    assert runtime_precheck["practical_generator_avoidance_candidates"]
    assert runtime_precheck["practical_local_wait_candidates"]


def test_cli_compile_prints_practical_local_wait_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pre_dir = tmp_path / "pre_cli"
    assert preprocess_main(
        [
            str(FPGA_FIXTURES / "FPGASTIL_RATE100_project_mod.stil"),
            str(FPGA_FIXTURES / "Patterns" / "FPGASTIL_RATE100.stil"),
            "-o",
            str(pre_dir),
            "--ignore-signal",
            "io_oe",
            "--pin-map",
        ]
    ) == 0
    output = tmp_path / "practical_cli.bin"
    assert compile_main(
        [
            str(pre_dir / "FPGASTIL_RATE100_project_mod__FPGASTIL_RATE100.pre.stil"),
            "-o",
            str(output),
            "--mode",
            "auto",
            "--pin-map",
            str(pre_dir / "FPGASTIL_RATE100_project_mod__FPGASTIL_RATE100.pre.pinmap.json"),
            "--allow-approx",
            "--practical-gate-policy",
            "report",
        ]
    ) == 0
    stdout = capsys.readouterr().out
    assert "PRACTICAL_LOCAL_WAIT_CANDIDATES" in stdout
    assert "extra>=" in stdout
    assert "global=" in stdout
    assert "WARNING_CODES" in stdout


def test_practical_chunk_shaping_splits_long_run(tmp_path: Path) -> None:
    pre_dir = tmp_path / "pre_shape"
    assert preprocess_main(
        [
            str(FPGA_FIXTURES / "FPGASTIL_RATE100_project_mod.stil"),
            str(FPGA_FIXTURES / "Patterns" / "FPGASTIL_RATE100.stil"),
            "-o",
            str(pre_dir),
            "--ignore-signal",
            "io_oe",
            "--pin-map",
        ]
    ) == 0

    baseline = tmp_path / "practical_base.bin"
    shaped = tmp_path / "practical_shaped.bin"
    compile_args = [
        str(pre_dir / "FPGASTIL_RATE100_project_mod__FPGASTIL_RATE100.pre.stil"),
        "--mode",
        "auto",
        "--pin-map",
        str(pre_dir / "FPGASTIL_RATE100_project_mod__FPGASTIL_RATE100.pre.pinmap.json"),
        "--allow-approx",
        "--practical-gate-policy",
        "report",
    ]
    assert compile_main([*compile_args, "-o", str(baseline)]) == 0
    assert compile_main(
        [
            *compile_args,
            "-o",
            str(shaped),
            "--practical-chunk-shaping",
            "--shape-boundary-global",
            "2048:2560",
            "--shape-max-run-vectors",
            "64",
            "--practical-gate-policy",
            "report",
        ]
    ) == 0

    _, baseline_rates, _, baseline_vectors, _ = unpack_rate_vector_full(baseline.read_bytes())
    _, shaped_rates, _, shaped_vectors, _ = unpack_rate_vector_full(shaped.read_bytes())
    assert len(shaped_vectors) == len(baseline_vectors)
    assert len(shaped_rates) > len(baseline_rates)

    report = json.loads(shaped.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    runtime_precheck = report["capability_precheck"]["runtime_precheck"]
    assert runtime_precheck["practical_chunk_shaping_applied"] is True
    assert runtime_precheck["practical_chunk_shaping_policy"]["target_global_range"] == [2048, 2560]
    assert runtime_precheck["practical_chunk_shaping_policy"]["max_run_vectors"] == 64
    assert runtime_precheck["practical_chunk_shaping_segments"]
    shaped_candidates = runtime_precheck["practical_generator_avoidance_candidates"]
    assert shaped_candidates[0]["global_vector_end"] == 2047
    codes = {item["code"] for item in report["diagnostics"]}
    assert "NOTE_PRACTICAL_CHUNK_SHAPING_APPLIED" in codes


def test_cli_compile_prints_practical_chunk_shaping_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pre_dir = tmp_path / "pre_shape_cli"
    assert preprocess_main(
        [
            str(FPGA_FIXTURES / "FPGASTIL_RATE100_project_mod.stil"),
            str(FPGA_FIXTURES / "Patterns" / "FPGASTIL_RATE100.stil"),
            "-o",
            str(pre_dir),
            "--ignore-signal",
            "io_oe",
            "--pin-map",
        ]
    ) == 0
    output = tmp_path / "practical_shape_cli.bin"
    assert compile_main(
        [
            str(pre_dir / "FPGASTIL_RATE100_project_mod__FPGASTIL_RATE100.pre.stil"),
            "-o",
            str(output),
            "--mode",
            "auto",
            "--pin-map",
            str(pre_dir / "FPGASTIL_RATE100_project_mod__FPGASTIL_RATE100.pre.pinmap.json"),
            "--allow-approx",
            "--practical-chunk-shaping",
            "--shape-boundary-global",
            "2048:2560",
            "--shape-max-run-vectors",
            "64",
            "--practical-gate-policy",
            "report",
        ]
    ) == 0
    stdout = capsys.readouterr().out
    assert "PRACTICAL_CHUNK_SHAPING" in stdout
    assert "segments=" in stdout


def test_device_precheck_rejects_streaming_poc_timing_incompatible_rate_vector(tmp_path: Path) -> None:
    pattern = pack_rate_vector(
        active_pins=16,
        tick_hz=150_000_000,
        rates=[
            RateDesc(period_ticks=15, slot_count=2, flags=0, slot_offset=0),
        ],
        slots=[
            RateSlot(
                time_ticks=0,
                flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
                drive_mask=0x1,
                dir_mask=0x1,
                sample_mask=0,
            ),
            RateSlot(
                time_ticks=8,
                flags=STPG_RATE_SLOT_SAMPLE,
                drive_mask=0,
                dir_mask=0,
                sample_mask=0x1,
            ),
        ],
        vectors=[
            VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=0x1, drive_enable=0x1, expect_value=0x1, expect_mask=0x1),
        ],
    )
    req = inspect_pattern_requirements(pattern)
    assert req.streaming_poc_incompatibility is not None
    with pytest.raises(RuntimeError, match="streaming-poc unsupported for current runtime"):
        validate_device_compatibility(
            req,
            device_clock_tick_hz=req.tick_hz,
            device_active_pins=req.active_pins,
            device_capability=(
                CAP_UPLOAD_PSRAM
                | CAP_RUN_16PIN
                | CAP_RATE_VECTOR_MODE
                | CAP_SAMPLE_CAPTURE
                | CAP_EXPECT_COMPARE
                | CAP_PSRAM_STREAMING
            ),
            storage_target=STORAGE_PSRAM,
            run_flags=RUN_FLAG_STREAMING_POC,
        )


def test_device_precheck_accepts_streaming_poc_timing_compatible_rate_vector(tmp_path: Path) -> None:
    pattern = pack_rate_vector(
        active_pins=16,
        tick_hz=150_000_000,
        rates=[
            RateDesc(period_ticks=30, slot_count=2, flags=0, slot_offset=0),
        ],
        slots=[
            RateSlot(
                time_ticks=0,
                flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
                drive_mask=0x1,
                dir_mask=0x1,
                sample_mask=0,
            ),
            RateSlot(
                time_ticks=15,
                flags=STPG_RATE_SLOT_SAMPLE,
                drive_mask=0,
                dir_mask=0,
                sample_mask=0x1,
            ),
        ],
        vectors=[
            VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=0x1, drive_enable=0x1, expect_value=0x1, expect_mask=0x1),
        ],
    )
    req = inspect_pattern_requirements(pattern)
    assert req.streaming_poc_incompatibility is None
    validate_device_compatibility(
        req,
        device_clock_tick_hz=req.tick_hz,
        device_active_pins=req.active_pins,
        device_capability=(
            CAP_UPLOAD_PSRAM
            | CAP_RUN_16PIN
            | CAP_RATE_VECTOR_MODE
            | CAP_SAMPLE_CAPTURE
            | CAP_EXPECT_COMPARE
            | CAP_PSRAM_STREAMING
        ),
        storage_target=STORAGE_PSRAM,
        run_flags=RUN_FLAG_STREAMING_POC,
    )


def test_device_precheck_reports_reduced_density_candidate_for_two_group_compare() -> None:
    pattern = pack_rate_vector(
        active_pins=16,
        tick_hz=150_000_000,
        rates=[
            RateDesc(period_ticks=30, slot_count=2, flags=0, slot_offset=0),
        ],
        slots=[
            RateSlot(
                time_ticks=0,
                flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
                drive_mask=0x1,
                dir_mask=0x1,
                sample_mask=0,
            ),
            RateSlot(
                time_ticks=15,
                flags=STPG_RATE_SLOT_SAMPLE,
                drive_mask=0,
                dir_mask=0,
                sample_mask=0x1,
            ),
        ],
        vectors=[
            VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=0x1, drive_enable=0x1, expect_value=0x1, expect_mask=0x1),
        ],
    )
    req = inspect_pattern_requirements(pattern)
    assert req.reduced_density_candidate is True
    assert req.reduced_density_incompatibilities == ()
    assert "reduced-density-compare" in req.exact_density_opportunities


def test_device_precheck_requires_sample_flag_not_only_sample_mask() -> None:
    pattern = pack_rate_vector(
        active_pins=16,
        tick_hz=150_000_000,
        rates=[
            RateDesc(period_ticks=30, slot_count=2, flags=0, slot_offset=0),
        ],
        slots=[
            RateSlot(
                time_ticks=0,
                flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
                drive_mask=0x1,
                dir_mask=0x1,
                sample_mask=0,
            ),
            RateSlot(
                time_ticks=15,
                flags=0,
                drive_mask=0,
                dir_mask=0,
                sample_mask=0x1,
            ),
        ],
        vectors=[
            VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=0x1, drive_enable=0x1, expect_value=0x1, expect_mask=0x1),
        ],
    )
    req = inspect_pattern_requirements(pattern)
    assert req.reduced_density_candidate is False
    assert "rate[0] sample_group_missing_sample" in req.reduced_density_incompatibilities


def test_device_precheck_reports_reduced_density_incompatibility_for_loop_pattern() -> None:
    pattern = pack_rate_vector(
        active_pins=16,
        tick_hz=150_000_000,
        rates=[
            RateDesc(period_ticks=30, slot_count=2, flags=0, slot_offset=0),
        ],
        slots=[
            RateSlot(
                time_ticks=0,
                flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
                drive_mask=0x1,
                dir_mask=0x1,
                sample_mask=0,
            ),
            RateSlot(
                time_ticks=15,
                flags=STPG_RATE_SLOT_SAMPLE,
                drive_mask=0,
                dir_mask=0,
                sample_mask=0x1,
            ),
        ],
        vectors=[LoopRefRecord(loop_id=0)],
        loop_blocks=[
            LoopBlock(
                loop_count=2,
                body_records=(
                    VectorRecord(
                        rate_id=0,
                        flags=0,
                        repeat=1,
                        drive_value=0x1,
                        drive_enable=0x1,
                        expect_value=0x1,
                        expect_mask=0x1,
                    ),
                ),
            )
        ],
    )
    req = inspect_pattern_requirements(pattern)
    assert req.reduced_density_candidate is False
    assert "loop blocks present" in req.reduced_density_incompatibilities


def test_validate_macro_vector_allows_streaming_sized_macro_payload() -> None:
    pattern = pack_macro_vector(
        active_pins=16,
        tick_hz=150_000_000,
        records=[
            MacroVectorRecord(
                family=1,
                flags=STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT,
                repeat=4000,
                period_ticks=30,
                phase_ticks=15,
                drive_value=0x1,
                drive_enable=0x1,
                sample_mask=0x1,
                expect_value=0x1,
            )
        ],
    )
    validate_macro_vector(pattern, expected_tick_hz=150_000_000)


def test_inspect_pattern_requirements_reports_macro_direct_exec_overflow() -> None:
    pattern = pack_macro_vector(
        active_pins=16,
        tick_hz=150_000_000,
        records=[
            MacroVectorRecord(
                family=1,
                flags=STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT,
                repeat=4000,
                period_ticks=30,
                phase_ticks=15,
                drive_value=0x1,
                drive_enable=0x1,
                sample_mask=0x1,
                expect_value=0x1,
            )
        ],
    )
    req = inspect_pattern_requirements(pattern)
    assert req.mode == "macro-stream"
    assert req.streaming_poc_incompatibility is None
    assert req.estimated_event_count == 8000
    fit = estimate_direct_execution_fit(req)
    assert fit.supported is False
    assert fit.reason == f"estimated_event_count>{fit.event_limit}"


def test_inspect_pattern_requirements_reports_macro_p2_direct_exec_overflow() -> None:
    pattern = pack_macro_p2_vector(
        active_pins=16,
        tick_hz=150_000_000,
        records=[
            MacroVectorRecordP2(
                period_ticks=30,
                repeat=4000,
                wait_first_ticks=15,
                source_family=1,
                exec_family=1,
                flags=0,
                wait_second_ticks=15,
                drive0_value=0x1,
                drive0_enable=0x1,
                drive1_value=0x1,
                drive1_enable=0x1,
                sample0_mask=0,
                sample1_mask=0x1,
                expect0_value=0,
                expect1_value=0x1,
            )
        ],
    )
    req = inspect_pattern_requirements(pattern)
    assert req.mode == "macro-p2"
    assert req.streaming_poc_incompatibility is None
    assert req.estimated_event_count == 8000
    fit = estimate_direct_execution_fit(req)
    assert fit.supported is False
    assert fit.reason == f"estimated_event_count>{fit.event_limit}"


def test_direct_execution_admission_includes_exact_6551_event_boundary() -> None:
    base = PatternRequirements(
        tick_hz=150_000_000,
        active_pins=16,
        mode="rate-vector",
        requires_sample=False,
        requires_compare=False,
        estimated_event_count=6551,
        streaming_poc_incompatibility=None,
    )
    exact = estimate_direct_execution_fit(base)
    assert exact.event_limit == 6551
    assert exact.supported is True

    over = estimate_direct_execution_fit(
        replace(base, estimated_event_count=6552)
    )
    assert over.supported is False
    assert over.reason == "estimated_event_count>6551"


def test_device_precheck_rejects_macro_streaming_poc_for_overflow_pattern() -> None:
    pattern = pack_macro_vector(
        active_pins=24,
        tick_hz=150_000_000,
        records=[
            MacroVectorRecord(
                family=5,
                flags=STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT,
                repeat=5000,
                period_ticks=30,
                phase_ticks=0,
                drive_value=0x1,
                drive_enable=0x1,
                sample_mask=0x1,
                expect_value=0x1,
            )
        ],
    )
    req = inspect_pattern_requirements(pattern)
    with pytest.raises(RuntimeError, match="streaming-poc is not supported for macro-stream"):
        validate_device_compatibility(
            req,
            device_clock_tick_hz=req.tick_hz,
            device_active_pins=req.active_pins,
            device_capability=(
                CAP_UPLOAD_PSRAM
                | CAP_RUN_24PIN
                | CAP_SAMPLE_CAPTURE
                | CAP_EXPECT_COMPARE
                | CAP_PSRAM_STREAMING
            ),
            storage_target=STORAGE_PSRAM,
            run_flags=RUN_FLAG_STREAMING_POC,
        )


def test_device_precheck_allows_24pin_pattern_when_capability_advertises_24pin() -> None:
    pattern = pack_macro_vector(
        active_pins=24,
        tick_hz=150_000_000,
        records=[
            MacroVectorRecord(
                family=1,
                flags=STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT,
                repeat=1,
                period_ticks=30,
                phase_ticks=15,
                drive_value=0x1,
                drive_enable=0x1,
                sample_mask=0x1,
                expect_value=0x1,
            )
        ],
    )
    req = inspect_pattern_requirements(pattern)
    validate_device_compatibility(
        req,
        device_clock_tick_hz=req.tick_hz,
        device_active_pins=16,
        device_capability=CAP_UPLOAD_PSRAM | CAP_RUN_16PIN | CAP_RUN_24PIN | CAP_SAMPLE_CAPTURE | CAP_EXPECT_COMPARE,
        storage_target=STORAGE_PSRAM,
        run_flags=0,
    )
