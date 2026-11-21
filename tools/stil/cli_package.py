"""CLI for host-only STIL pattern package generation and verification."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path

from .markers import parse_marker_specs
from .pattern_package import (
    PatternPackageError,
    PatternPackageOptions,
    build_pattern_package,
    derive_pattern_package_with_wait,
    verify_pattern_package,
)
from .runtime_capacity import DEFAULT_RUNTIME_PROFILE, RUNTIME_CAPACITY_PROFILES
from .wait_schedule import DEFAULT_CHUNK_EVENT_CAPACITY, DEFAULT_RING_SLOT_COUNT
from .workspace import add_work_dir_options, resolve_work_dir
from stpg.constants import STPG_DEFAULT_TICK_HZ


def _range(value: str) -> tuple[int, int]:
    start, end = value.split(":", 1)
    return int(start), int(end)


def _add_compile_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", choices=("auto", "rate-vector", "raw-event", "macro-stream"), default="auto")
    parser.add_argument("--tick-hz", type=int, default=STPG_DEFAULT_TICK_HZ)
    parser.add_argument("--max-rates", type=int, help="rate-vector descriptor limit for this host-only contract")
    parser.add_argument(
        "--loop-policy",
        choices=("expand", "preserve"),
        default="expand",
        help="expand STIL loops for compatibility or preserve them for rate-vector compilation",
    )
    parser.add_argument("--chunk-event-capacity", type=int, default=DEFAULT_CHUNK_EVENT_CAPACITY)
    parser.add_argument("--ring-slot-count", type=int, default=DEFAULT_RING_SLOT_COUNT)
    parser.add_argument("--pin-map", type=Path)
    parser.add_argument("--pattern-burst")
    parser.add_argument("--pattern")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--allow-approx", action="store_true")
    parser.add_argument("--prefer-raw", action="store_true")
    parser.add_argument("--until-vector", type=int)
    parser.add_argument("--vector-range", type=_range)
    parser.add_argument("--time-scale", type=Decimal, default=Decimal("1.0"))
    parser.add_argument("--marker", action="append", default=[])
    parser.add_argument("--marker-clear", action="append", default=[])
    parser.add_argument("--practical-chunk-shaping", action="store_true")
    parser.add_argument("--shape-boundary-global", type=_range)
    parser.add_argument("--shape-max-run-vectors", type=int, default=64)
    parser.add_argument("--practical-gate-policy", choices=("error", "report"), default="error")
    parser.add_argument(
        "--runtime-profile",
        choices=tuple(sorted(RUNTIME_CAPACITY_PROFILES)),
        default=DEFAULT_RUNTIME_PROFILE,
        help="firmware capacity contract used for early execution admission",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="compile and verify one host-only pattern package")
    build.add_argument("input", type=Path)
    add_work_dir_options(build, scope="package")
    _add_compile_options(build)
    build.add_argument("--wait-schedule", type=Path)
    build.add_argument("--service-us", type=float)
    build.add_argument("--service-source", default="")
    build.add_argument(
        "--service-profile-scope",
        choices=("non-loop", "loop-generic"),
        default="non-loop",
        help="bind the declared service budget to a non-loop or preserved-Loop artifact",
    )
    build.add_argument("--safety-margin-us", type=float, default=0.0)
    build.add_argument("--max-wait-us", type=float)
    build.add_argument("--max-total-wait-us", type=float)
    build.add_argument("--max-wait-count", type=int)
    build.add_argument("--pattern-time-limit-us", type=float)
    build.add_argument(
        "--forbid-stil-rate",
        action="append",
        default=[],
        metavar="WAVEFORM_TABLE",
        help=(
            "forbid a non-zero wait only when the next chunk's first event "
            "uses this STIL WaveformTable"
        ),
    )
    build.add_argument("--json-out", type=Path)

    verify = subparsers.add_parser("verify", help="verify an existing host-only pattern package")
    verify.add_argument("package_dir", type=Path)
    verify.add_argument("--json-out", type=Path)

    derive_wait = subparsers.add_parser(
        "derive-wait", help="create a new package with an ACCEPTED schedule encoded in its binary"
    )
    derive_wait.add_argument("source_package", type=Path)
    derive_wait.add_argument("--schedule", type=Path, required=True)
    derive_wait.add_argument("--acceptance", type=Path, required=True)
    add_work_dir_options(derive_wait, scope="package-derive-wait")
    derive_wait.add_argument("--json-out", type=Path)
    return parser


def _write_report(path: Path | None, report: dict) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "verify":
        report = verify_pattern_package(args.package_dir)
        _write_report(args.json_out, report)
        print(json.dumps({"status": report["status"], "errors": len(report["errors"])}, sort_keys=True))
        return 0 if report["status"] == "PASS" else 2

    if args.command == "derive-wait":
        try:
            report = derive_pattern_package_with_wait(
                args.source_package,
                schedule_path=args.schedule,
                acceptance_path=args.acceptance,
                output_dir=resolve_work_dir(args),
            )
        except (PatternPackageError, OSError, ValueError) as exc:
            print(json.dumps({"status": "REJECT", "error": str(exc)}, sort_keys=True))
            return 2
        _write_report(args.json_out, report)
        print(json.dumps({"status": report["status"], "package_dir": report["package_dir"]}, sort_keys=True))
        return 0

    try:
        report = build_pattern_package(
            PatternPackageOptions(
                input_path=args.input,
                output_dir=resolve_work_dir(args),
                mode=args.mode,
                tick_hz=args.tick_hz,
                pin_map_path=args.pin_map,
                pattern_burst=args.pattern_burst,
                pattern=args.pattern,
                strict=args.strict,
                allow_approx=args.allow_approx,
                prefer_raw=args.prefer_raw,
                until_vector=args.until_vector,
                vector_range=args.vector_range,
                time_scale=args.time_scale,
                markers=parse_marker_specs(args.marker, args.marker_clear),
                practical_chunk_shaping=args.practical_chunk_shaping,
                practical_chunk_shaping_target_range=args.shape_boundary_global,
                practical_chunk_shaping_max_run_vectors=args.shape_max_run_vectors,
                practical_gate_policy=args.practical_gate_policy,
                runtime_profile=args.runtime_profile,
                max_rates=args.max_rates,
                loop_policy=args.loop_policy,
                chunk_event_capacity=args.chunk_event_capacity,
                ring_slot_count=args.ring_slot_count,
                forbidden_stil_rate_units=tuple(args.forbid_stil_rate),
                wait_schedule_path=args.wait_schedule,
                service_time_us=args.service_us,
                service_time_source=args.service_source,
                service_profile_scope=args.service_profile_scope,
                safety_margin_us=args.safety_margin_us,
                max_wait_us=args.max_wait_us,
                max_total_wait_us=args.max_total_wait_us,
                max_wait_count=args.max_wait_count,
                pattern_time_limit_us=args.pattern_time_limit_us,
            )
        )
    except (PatternPackageError, OSError, ValueError) as exc:
        print(json.dumps({"status": "REJECT", "error": str(exc)}, sort_keys=True))
        return 2
    _write_report(args.json_out, report)
    print(json.dumps({"status": report["status"], "package_dir": report["package_dir"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
