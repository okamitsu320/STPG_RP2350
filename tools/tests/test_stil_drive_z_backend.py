from __future__ import annotations

import json
from pathlib import Path

import pytest

from stil.errors import StilError
from stil.pipeline import CompileOptions, compile_stil
from stpg.pattern import unpack_pattern


def _write_drive_then_release_stil(tmp_path: Path) -> tuple[Path, Path]:
    stil_path = tmp_path / "drive_then_release.stil"
    stil_path.write_text(
        "\n".join(
            [
                "Signals {",
                "  IO InOut;",
                "}",
                "SignalGroups {",
                "  ALL = 'IO';",
                "}",
                "Timing {",
                "  WaveformTable WFT0 {",
                "    Period '200ns';",
                "    Waveforms {",
                "      IO { 0: 0ns Drive(0), 100ns Drive(Z); }",
                "    }",
                "  }",
                "}",
                "Pattern P0 {",
                "  W WFT0;",
                "  V { ALL = 0; }",
                "}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    pin_map_path = tmp_path / "pins.json"
    pin_map_path.write_text(
        json.dumps(
            {
                "active_pins": 16,
                "signals": {
                    "IO": {
                        "bit": 0,
                        "logical_pin": "DUT0",
                        "direction": "inout",
                        "role": "signal",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return stil_path, pin_map_path


def test_rate_vector_rejects_within_vector_drive_release_transition(tmp_path) -> None:
    stil_path, pin_map_path = _write_drive_then_release_stil(tmp_path)

    with pytest.raises(StilError) as exc_info:
        compile_stil(
            CompileOptions(
                input_path=stil_path,
                mode="rate-vector",
                tick_hz=150_000_000,
                pin_map_path=pin_map_path,
                strict=True,
            )
        )

    assert exc_info.value.code == "ERR_RATE_VECTOR_DIRECTION_UNSUPPORTED"


def test_raw_event_preserves_drive_release_phase_transition(tmp_path) -> None:
    stil_path, pin_map_path = _write_drive_then_release_stil(tmp_path)
    pattern, _map_data, _report = compile_stil(
        CompileOptions(
            input_path=stil_path,
            mode="raw-event",
            tick_hz=150_000_000,
            pin_map_path=pin_map_path,
            strict=True,
        )
    )

    header, events = unpack_pattern(pattern)
    assert header.event_count == 2
    assert len(events) == 2
    assert events[0].out_enable == 1
    assert events[1].out_enable == 0
