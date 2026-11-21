from __future__ import annotations

import json
from pathlib import Path

import pytest

from stil.rate_adapt import RateAdaptOptions, adapt_stil
from stil.cli_rate_adapt import main


SOURCE = """\
Signals {
  CLK In;
  SYNTH_OBSERVED Out;
}
SignalGroups {
  ALL = 'CLK + SYNTH_OBSERVED';
}
Timing {
  WaveformTable CAPTURE {
    Period '20ns';
    Waveforms {
      CLK { P: 5ns Drive(1), 15ns Drive(0); } // comment 280ns Drive(0)
      SYNTH_OBSERVED { X: 0ns Compare(X); H: 3ns Compare(1); }
    }
  }
}
Pattern MAIN {
  W CAPTURE;
  V { ALL = PX; }
}
"""


def test_streaming_fastest_adapter_preserves_order_and_remaps_only_selected_table(tmp_path: Path) -> None:
    source = tmp_path / "input.pre.stil"
    output = tmp_path / "adapted.stil"
    report_path = tmp_path / "adaptation.json"
    source.write_text(SOURCE, encoding="utf-8")

    report = adapt_stil(
        RateAdaptOptions(
            input_path=source,
            output_path=output,
            report_path=report_path,
            waveform_tables=("CAPTURE",),
            tick_hz=150_000_000,
        )
    )

    assert report["status"] == "DERIVED"
    plan = report["waveform_tables"][0]
    assert plan["original_period_ticks"] == 3
    assert plan["adapted_period_ticks"] == 42
    assert [(item["original_time_ticks"], item["adapted_time_ticks"]) for item in plan["event_group_mapping"]] == [
        (0, 0),
        (0, 11),
        (1, 21),
        (2, 31),
    ]
    adapted = output.read_text(encoding="utf-8")
    assert "Period '280ns';" in adapted
    assert "0ns Compare(X)" in adapted
    assert "73.33333333333333333333333333ns Compare(1)" in adapted
    assert "140ns Drive(1)" in adapted
    assert "206.6666666666666666666666667ns Drive(0)" in adapted
    assert "// comment 280ns Drive(0)" in adapted
    assert json.loads(report_path.read_text(encoding="utf-8"))["output"]["sha256"] == report["output"]["sha256"]


def test_cli_rejects_without_explicit_waveform_table(tmp_path: Path) -> None:
    source = tmp_path / "input.pre.stil"
    source.write_text(SOURCE, encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        main([str(source), "--output", str(tmp_path / "out.stil"), "--report", str(tmp_path / "report.json")])
    assert exc_info.value.code == 2


def test_adapter_rejects_unknown_timing_actions(tmp_path: Path) -> None:
    source = tmp_path / "input.pre.stil"
    source.write_text(SOURCE.replace("Drive(1)", "Pulse(1)", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported timing action"):
        adapt_stil(
            RateAdaptOptions(
                input_path=source,
                output_path=tmp_path / "adapted.stil",
                report_path=tmp_path / "adaptation.json",
                waveform_tables=("CAPTURE",),
            )
        )


def test_adapter_supports_explicit_uniform_rounded_gap(tmp_path: Path) -> None:
    source = tmp_path / "input.pre.stil"
    output = tmp_path / "adapted.stil"
    report_path = tmp_path / "adaptation.json"
    source.write_text(SOURCE, encoding="utf-8")

    report = adapt_stil(
        RateAdaptOptions(
            input_path=source,
            output_path=output,
            report_path=report_path,
            waveform_tables=("CAPTURE",),
            tick_hz=150_000_000,
            min_gap_ticks=12,
        )
    )

    plan = report["waveform_tables"][0]
    assert plan["gap_policy"] == "explicit_uniform_min_gap"
    assert plan["explicit_min_gap_ticks"] == 12
    assert plan["adapted_period_ticks"] == 48
    assert [item["adapted_time_ticks"] for item in plan["event_group_mapping"]] == [0, 12, 24, 36]
    adapted = output.read_text(encoding="utf-8")
    assert "Period '320ns';" in adapted
    assert "160ns Drive(1)" in adapted
    assert "80ns Compare(1)" in adapted
    assert "240ns Drive(0)" in adapted
