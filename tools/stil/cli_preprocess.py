from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .preprocess import build_default_pin_map, preprocess_ategen_stil
from .workspace import add_work_dir_options, resolve_work_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preprocess ATEGen/VCDSTIL project+pattern files into compiler-subset STIL")
    parser.add_argument("project", type=Path, help="ATEGen project/timing STIL file")
    parser.add_argument("patterns", nargs="+", type=Path, help="one or more pattern STIL files")
    add_work_dir_options(parser, scope="preprocess")
    parser.add_argument("--ignore-signal", action="append", default=[], help="signal name to rewrite to T in vectors and exclude from default pin-map")
    parser.add_argument("--pin-map", action="store_true", help="also emit default pin-map JSON for each preprocessed output")
    return parser


def _build_output_name(project: Path, pattern: Path) -> str:
    project_stem = project.stem.removesuffix("_project")
    pattern_stem = pattern.stem
    if pattern_stem.startswith(project_stem):
        return f"{pattern_stem}.pre.stil"
    return f"{project_stem}__{pattern_stem}.pre.stil"


def _has_top_level_block(text: str, keyword: str) -> bool:
    """Return whether text contains an ATEGen-style named block.

    ATEGen project files in the field sometimes keep ``Signals`` and
    ``SignalGroups`` inside comments. The legacy preprocessor intentionally
    extracts those blocks textually, so role detection recognizes the same
    commented form without parsing the whole file here.
    """

    escaped = re.escape(keyword)
    return re.search(rf"(?m)^\s*(?://\s*)?{escaped}(?:\s|\{{)", text) is not None


def _normalize_input_roles(project: Path, patterns: list[Path]) -> tuple[Path, list[Path]]:
    """Accept one unambiguous pattern/project reversal with a warning.

    The documented order remains ``project patterns...``. For the common
    single-pair invocation, however, a file under ``Patterns/`` is easy to
    place first. Swap only when the first file looks like a pattern and the
    second file clearly contains project timing/signals; never guess for a
    multi-pattern invocation.
    """

    if len(patterns) != 1:
        return project, patterns

    candidate_pattern = project
    candidate_project = patterns[0]
    try:
        first_text = candidate_pattern.read_text(encoding="utf-8")
        second_text = candidate_project.read_text(encoding="utf-8")
    except OSError:
        return project, patterns

    first_is_pattern = re.search(r"\bPattern\s+[A-Za-z_][A-Za-z0-9_]*\s*\{", first_text) is not None
    first_has_project_signals = _has_top_level_block(first_text, "Signals")
    second_has_project_blocks = _has_top_level_block(second_text, "Signals") and _has_top_level_block(
        second_text, "Timing"
    )
    if first_is_pattern and not first_has_project_signals and second_has_project_blocks:
        print(
            "warning: detected reversed project/pattern arguments; using the second argument as project",
            file=sys.stderr,
        )
        return candidate_project, [candidate_pattern]
    return project, patterns


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_path, pattern_paths = _normalize_input_roles(args.project, list(args.patterns))
    output_dir = resolve_work_dir(args)
    output_dir.mkdir(parents=True, exist_ok=True)
    ignored = set(args.ignore_signal)

    for pattern_path in pattern_paths:
        result = preprocess_ategen_stil(project_path, pattern_path, ignore_signals=ignored)
        output_path = output_dir / _build_output_name(project_path, pattern_path)
        output_path.write_text(result.output_text, encoding="utf-8")
        print(output_path)
        if args.pin_map:
            pin_map = build_default_pin_map(result, ignored_signals=ignored)
            pin_map_path = output_path.with_suffix(".pinmap.json")
            pin_map_path.write_text(json.dumps(pin_map, indent=2) + "\n", encoding="utf-8")
            print(pin_map_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
