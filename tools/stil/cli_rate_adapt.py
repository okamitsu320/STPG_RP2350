"""Create an explicit STPG-safe timing view from preprocessed STIL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .rate_adapt import RateAdaptError, RateAdaptOptions, SUPPORTED_PROFILE, adapt_stil
from stpg.constants import STPG_DEFAULT_TICK_HZ


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="preprocessed STIL input")
    parser.add_argument("--output", type=Path, required=True, help="derived STIL output")
    parser.add_argument("--report", type=Path, required=True, help="adaptation report JSON")
    parser.add_argument("--waveform-table", action="append", required=True)
    parser.add_argument("--tick-hz", type=int, default=STPG_DEFAULT_TICK_HZ)
    parser.add_argument(
        "--min-gap-ticks",
        type=int,
        help="use this explicit uniform gap between declared event groups",
    )
    parser.add_argument("--profile", choices=(SUPPORTED_PROFILE,), default=SUPPORTED_PROFILE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = adapt_stil(
            RateAdaptOptions(
                input_path=args.input,
                output_path=args.output,
                report_path=args.report,
                waveform_tables=tuple(args.waveform_table),
                tick_hz=args.tick_hz,
                profile=args.profile,
                min_gap_ticks=args.min_gap_ticks,
            )
        )
    except (OSError, RateAdaptError, ValueError) as exc:
        print(json.dumps({"status": "REJECT", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"status": report["status"], "output": report["output"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
