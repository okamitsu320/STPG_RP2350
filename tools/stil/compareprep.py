from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .preprocess import (
    build_default_pin_map,
    preprocess_ategen_stil,
    read_ategen_signals,
)


@dataclass(frozen=True)
class ComparePrepOutputs:
    preprocessed_stil_path: Path
    pin_map_path: Path
    ignored_signals: tuple[str, ...]
    compare_kept_signals: tuple[str, ...]
    compare_shifted_signals: tuple[str, ...]
    compare_shift_vectors: int


def build_compare_ready_outputs(
    *,
    project_path: str | Path,
    pattern_path: str | Path,
    output_dir: str | Path,
    keep_compare_signals: set[str] | None = None,
    keep_compare_prefixes: tuple[str, ...] = (),
    extra_ignored_signals: set[str] | None = None,
    ignore_out_signals: bool = False,
    shift_compare_signals: set[str] | None = None,
    shift_compare_prefixes: tuple[str, ...] = (),
    shift_compare_vectors: int = 0,
) -> ComparePrepOutputs:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    signals = read_ategen_signals(project_path)

    keep_compare = set(keep_compare_signals or set())
    for signal in signals:
        if any(signal.name.startswith(prefix) for prefix in keep_compare_prefixes):
            keep_compare.add(signal.name)

    ignored_signals = set(extra_ignored_signals or set())
    shift_compare = set(shift_compare_signals or set())
    for signal in signals:
        if any(signal.name.startswith(prefix) for prefix in shift_compare_prefixes):
            shift_compare.add(signal.name)

    if ignore_out_signals:
        ignored_signals |= {
            signal.name
            for signal in signals
            if signal.direction == "Out" and signal.name not in keep_compare
        }

    all_signal_names = {signal.name for signal in signals}
    compare_drop = all_signal_names - keep_compare - ignored_signals

    result = preprocess_ategen_stil(
        project_path,
        pattern_path,
        ignore_signals=ignored_signals,
        drop_compare_signals=compare_drop,
        shift_compare_signals=shift_compare,
        shift_compare_vectors=shift_compare_vectors,
    )

    output_stem = _build_output_stem(Path(project_path), Path(pattern_path))
    preprocessed_stil_path = output_dir / f"{output_stem}.compare.pre.stil"
    pin_map_path = output_dir / f"{output_stem}.compare.pre.pinmap.json"

    preprocessed_stil_path.write_text(result.output_text, encoding="utf-8")
    pin_map = build_default_pin_map(result, ignored_signals=ignored_signals)
    signal_dir = {signal.name: signal.direction for signal in signals}
    for signal_name in keep_compare:
        if signal_name in pin_map["signals"] and signal_dir.get(signal_name) == "Out":
            pin_map["signals"][signal_name]["direction"] = "input"
    pin_map_path.write_text(json.dumps(pin_map, indent=2) + "\n", encoding="utf-8")

    return ComparePrepOutputs(
        preprocessed_stil_path=preprocessed_stil_path,
        pin_map_path=pin_map_path,
        ignored_signals=tuple(sorted(ignored_signals)),
        compare_kept_signals=tuple(sorted(keep_compare)),
        compare_shifted_signals=tuple(sorted(shift_compare)),
        compare_shift_vectors=shift_compare_vectors,
    )


def _build_output_stem(project_path: Path, pattern_path: Path) -> str:
    project_stem = project_path.stem.removesuffix("_project")
    pattern_stem = pattern_path.stem
    if pattern_stem.startswith(project_stem):
        return pattern_stem
    return f"{project_stem}__{pattern_stem}"
