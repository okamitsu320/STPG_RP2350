from __future__ import annotations

import argparse
from pathlib import Path

from .stimulus import build_stimulus_only_outputs
from stpg.constants import STPG_DEFAULT_TICK_HZ


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build stimulus-only artifacts from ATEGen/VCDSTIL project+pattern files")
    parser.add_argument("project", type=Path)
    parser.add_argument("patterns", nargs="+", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("auto", "rate-vector", "raw-event"), default="auto")
    parser.add_argument("--tick-hz", type=int, default=STPG_DEFAULT_TICK_HZ)
    parser.add_argument("--ignore-signal", action="append", default=[], help="extra signal to rewrite to T and exclude from pin-map")
    parser.add_argument("--drop-compare-signal", action="append", default=[], help="signal name whose compare chars H/L/T are rewritten to T while keeping drive chars")
    parser.add_argument("--drop-compare-prefix", action="append", default=[], help="signal name prefix whose compare chars H/L/T are rewritten to T while keeping drive chars")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for pattern_path in args.patterns:
        outputs = build_stimulus_only_outputs(
            project_path=args.project,
            pattern_path=pattern_path,
            output_dir=args.output_dir,
            mode=args.mode,
            tick_hz=args.tick_hz,
            extra_ignored_signals=set(args.ignore_signal),
            drop_compare_signals=set(args.drop_compare_signal),
            drop_compare_prefixes=tuple(args.drop_compare_prefix),
        )
        print(outputs.preprocessed_stil_path)
        print(outputs.pin_map_path)
        print(outputs.pattern_path)
        print(f"ignored={','.join(outputs.ignored_signals)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
