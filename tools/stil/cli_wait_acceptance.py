"""CLI for bounded host-only wait schedule acceptance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .wait_acceptance import evaluate_wait_schedule


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pattern", type=Path)
    parser.add_argument("schedule", type=Path)
    parser.add_argument("--map", dest="map_path", type=Path)
    parser.add_argument("--service-us", type=float)
    parser.add_argument("--service-source", default="")
    parser.add_argument(
        "--service-profile-scope",
        choices=("non-loop", "loop-generic"),
        default="non-loop",
        help="bind the declared service budget to a non-loop or preserved-Loop artifact",
    )
    parser.add_argument("--safety-margin-us", type=float, default=0.0)
    parser.add_argument("--max-wait-us", type=float)
    parser.add_argument("--max-total-wait-us", type=float)
    parser.add_argument("--max-wait-count", type=int)
    parser.add_argument("--pattern-time-limit-us", type=float)
    parser.add_argument("--max-rates", type=int, help="rate-vector descriptor limit for this host-only contract")
    parser.add_argument("--json-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = evaluate_wait_schedule(
        args.pattern,
        args.schedule,
        map_path=args.map_path,
        service_time_us=args.service_us,
        service_time_source=args.service_source,
        safety_margin_us=args.safety_margin_us,
        max_wait_us=args.max_wait_us,
        max_total_wait_us=args.max_total_wait_us,
        max_wait_count=args.max_wait_count,
        pattern_time_limit_us=args.pattern_time_limit_us,
        max_rates=args.max_rates,
        service_profile_scope=args.service_profile_scope,
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "status": report["status"],
        "errors": len(report["errors"]),
        "warnings": len(report["warnings"]),
    }
    print(json.dumps(summary, sort_keys=True))
    for error in report["errors"]:
        print(f"ERROR {error}")
    for warning in report["warnings"]:
        print(f"WARN {warning}")
    return 0 if report["status"] == "ACCEPTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
