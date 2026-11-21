from __future__ import annotations

import argparse
from pathlib import Path

from .compareprep import build_compare_ready_outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build compare-aware preprocessed STIL artifacts from ATEGen/VCDSTIL project+pattern files")
    parser.add_argument("project", type=Path)
    parser.add_argument("patterns", nargs="+", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, required=True)
    parser.add_argument("--keep-compare-signal", action="append", default=[], help="signal name whose H/L/T compare chars are preserved")
    parser.add_argument("--keep-compare-prefix", action="append", default=[], help="signal name prefix whose H/L/T compare chars are preserved")
    parser.add_argument("--ignore-signal", action="append", default=[], help="signal to rewrite to T and exclude from pin-map")
    parser.add_argument("--ignore-out-signals", action="store_true", help="also rewrite Out signals to T and exclude them from pin-map")
    parser.add_argument("--shift-compare-signal", action="append", default=[], help="signal name whose compare chars are shifted later by --shift-compare-vectors")
    parser.add_argument("--shift-compare-prefix", action="append", default=[], help="signal prefix whose compare chars are shifted later by --shift-compare-vectors")
    parser.add_argument("--shift-compare-vectors", type=int, default=0, help="delay compare chars by N vectors for selected signals")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for pattern_path in args.patterns:
        outputs = build_compare_ready_outputs(
            project_path=args.project,
            pattern_path=pattern_path,
            output_dir=args.output_dir,
            keep_compare_signals=set(args.keep_compare_signal),
            keep_compare_prefixes=tuple(args.keep_compare_prefix),
            extra_ignored_signals=set(args.ignore_signal),
            ignore_out_signals=args.ignore_out_signals,
            shift_compare_signals=set(args.shift_compare_signal),
            shift_compare_prefixes=tuple(args.shift_compare_prefix),
            shift_compare_vectors=args.shift_compare_vectors,
        )
        print(outputs.preprocessed_stil_path)
        print(outputs.pin_map_path)
        print(f"ignored={','.join(outputs.ignored_signals)}")
        print(f"compare_kept={','.join(outputs.compare_kept_signals)}")
        print(f"compare_shifted={','.join(outputs.compare_shifted_signals)}")
        print(f"compare_shift_vectors={outputs.compare_shift_vectors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
