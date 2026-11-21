from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from stpg.constants import STPG_DEFAULT_TICK_HZ

from .pipeline import CompileOptions, compile_stil, write_compile_outputs
from .preprocess import (
    build_default_pin_map,
    preprocess_ategen_stil,
    read_ategen_signals,
)


@dataclass(frozen=True)
class StimulusOnlyOutputs:
    preprocessed_stil_path: Path
    pin_map_path: Path
    pattern_path: Path
    map_path: Path
    compile_report_path: Path
    ignored_signals: tuple[str, ...]


def build_stimulus_only_outputs(
    *,
    project_path: str | Path,
    pattern_path: str | Path,
    output_dir: str | Path,
    mode: str = "auto",
    tick_hz: int = STPG_DEFAULT_TICK_HZ,
    extra_ignored_signals: set[str] | None = None,
    drop_compare_signals: set[str] | None = None,
    drop_compare_prefixes: tuple[str, ...] = (),
) -> StimulusOnlyOutputs:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    signals = read_ategen_signals(project_path)
    ignored_signals = {signal.name for signal in signals if signal.direction == "Out"}
    if extra_ignored_signals:
        ignored_signals |= set(extra_ignored_signals)
    compare_drop = set(drop_compare_signals or set())
    if drop_compare_prefixes:
        for signal in signals:
            if any(signal.name.startswith(prefix) for prefix in drop_compare_prefixes):
                compare_drop.add(signal.name)

    result = preprocess_ategen_stil(
        project_path,
        pattern_path,
        ignore_signals=ignored_signals,
        drop_compare_signals=compare_drop,
    )
    output_stem = _build_output_stem(Path(project_path), Path(pattern_path))
    preprocessed_stil_path = output_dir / f"{output_stem}.pre.stil"
    pin_map_path = output_dir / f"{output_stem}.pre.pinmap.json"
    pattern_out = output_dir / f"{output_stem}.bin"

    preprocessed_stil_path.write_text(result.output_text, encoding="utf-8")
    pin_map = build_default_pin_map(result, ignored_signals=ignored_signals)
    pin_map_path.write_text(json.dumps(pin_map, indent=2) + "\n", encoding="utf-8")

    # The compiler reads the saved STIL; release its in-memory duplicate
    # before allocating the resolved pattern and IR.
    del result
    payload, map_data, report_data = compile_stil(
        CompileOptions(
            input_path=preprocessed_stil_path,
            mode=mode,
            tick_hz=tick_hz,
            pin_map_path=pin_map_path,
        )
    )
    write_compile_outputs(pattern_out, payload, map_data, report_data)
    return StimulusOnlyOutputs(
        preprocessed_stil_path=preprocessed_stil_path,
        pin_map_path=pin_map_path,
        pattern_path=pattern_out,
        map_path=pattern_out.with_suffix(".map.json"),
        compile_report_path=pattern_out.with_suffix(".compile_report.json"),
        ignored_signals=tuple(sorted(ignored_signals)),
    )


def _build_output_stem(project_path: Path, pattern_path: Path) -> str:
    project_stem = project_path.stem.removesuffix("_project")
    pattern_stem = pattern_path.stem
    if pattern_stem.startswith(project_stem):
        return pattern_stem
    return f"{project_stem}__{pattern_stem}"
