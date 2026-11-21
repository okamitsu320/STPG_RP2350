from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path

from .source_analysis import analyze_stil_source, render_source_analysis_text, write_source_analysis_report
from .runtime_capacity import DEFAULT_RUNTIME_PROFILE, RUNTIME_CAPACITY_PROFILES
from stpg.constants import STPG_DEFAULT_TICK_HZ


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze a STIL source for structural, timing, pin-map, and STPG backend risks"
    )
    parser.add_argument("input", type=Path, help="STIL source input")
    parser.add_argument("-o", "--output", type=Path, help="analysis JSON output (default: <input>.analysis.json)")
    parser.add_argument("--mode", choices=("auto", "rate-vector", "raw-event", "macro-stream"), default="auto")
    parser.add_argument("--tick-hz", type=int, default=STPG_DEFAULT_TICK_HZ)
    parser.add_argument("--max-rates", type=int, help="rate-vector descriptor limit for this host-only contract")
    parser.add_argument(
        "--loop-policy",
        choices=("expand", "preserve"),
        default="expand",
        help="expand STIL loops for compatibility or preserve them for backend admission",
    )
    parser.add_argument("--pin-map", type=Path)
    parser.add_argument("--pattern-burst")
    parser.add_argument("--pattern")
    parser.add_argument("--time-scale", type=Decimal, default=Decimal("1.0"))
    parser.add_argument(
        "--runtime-profile",
        choices=tuple(sorted(RUNTIME_CAPACITY_PROFILES)),
        default=DEFAULT_RUNTIME_PROFILE,
        help="firmware capacity contract used for early execution admission",
    )
    parser.add_argument(
        "--no-backend-candidates",
        action="store_true",
        help="skip in-memory raw/rate/macro backend probes",
    )
    parser.add_argument(
        "--segment-on-capacity",
        action="store_true",
        help=(
            "when the requested rate-vector exceeds a segmentable byte/event/sample/compare/Loop-block limit, "
            "build verified host-only segmented packages"
        ),
    )
    parser.add_argument(
        "--segment-vector-count",
        type=int,
        default=100_000,
        help="vectors per segment for --segment-on-capacity (default: 100000)",
    )
    parser.add_argument(
        "--segment-output-dir",
        type=Path,
        help="segmented package directory (default: <analysis-stem>.segments)",
    )
    parser.add_argument(
        "--segment-json-out",
        type=Path,
        help="segmented package report (default: <analysis-stem>.segmented.json)",
    )
    parser.add_argument(
        "--allow-approx",
        action="store_true",
        help="allow device-tick timing approximation while building segments",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return exit code 2 for REVIEW as well as REJECT",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def _segmented_output_paths(args: argparse.Namespace, analysis_output: Path) -> tuple[Path, Path]:
    output_dir = args.segment_output_dir or analysis_output.parent / f"{analysis_output.stem}.segments"
    report_path = args.segment_json_out or analysis_output.parent / f"{analysis_output.stem}.segmented.json"
    return output_dir, report_path


def _run_segment_on_capacity(
    args: argparse.Namespace,
    *,
    analysis_output: Path,
    report: dict,
) -> tuple[dict | None, str | None]:
    if not args.segment_on_capacity:
        return None, None
    advice = report.get("segmentation")
    if not isinstance(advice, dict) or advice.get("available") is not True:
        return None, "segmentation fallback is not applicable to this analysis result"
    if args.segment_vector_count < 1:
        return None, "--segment-vector-count must be positive"

    from .segmented_package import SegmentedPackageError, build_segment_ranges, build_segmented_packages

    output_dir, report_path = _segmented_output_paths(args, analysis_output)
    try:
        total_vectors = int(report["vectors"]["vector_count"])
        ranges = build_segment_ranges(
            total_vectors,
            segment_vector_count=min(args.segment_vector_count, total_vectors - 1),
        )
        segmented_report = build_segmented_packages(
            input_path=args.input,
            output_dir=output_dir,
            report_path=report_path,
            ranges=ranges,
            total_vectors=total_vectors,
            mode="rate-vector",
            tick_hz=args.tick_hz,
            pin_map_path=args.pin_map,
            pattern_burst=args.pattern_burst,
            pattern=args.pattern,
            strict=args.strict,
            allow_approx=args.allow_approx,
            runtime_profile=args.runtime_profile,
            max_rates=args.max_rates,
            loop_policy=args.loop_policy,
            capacity_aware=True,
        )
    except (OSError, SegmentedPackageError, ValueError) as exc:
        report["segmentation"] = {
            **advice,
            "requested": True,
            "status": "REJECT",
            "output_dir": str(output_dir),
            "report_path": str(report_path),
            "error": str(exc),
            "physical_execution_authorized": False,
        }
        return None, str(exc)

    report["segmentation"] = {
        **advice,
        "requested": True,
        "status": "COMPLETE",
        "output_dir": str(output_dir),
        "report_path": str(report_path),
        "segment_count": len(segmented_report.get("segment_ranges", ranges)),
        "segment_ranges": segmented_report.get("segment_ranges", [list(item) for item in ranges]),
        "physical_execution_authorized": False,
    }
    return segmented_report, None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output or args.input.with_suffix(".analysis.json")
    report = analyze_stil_source(
        args.input,
        mode=args.mode,
        tick_hz=args.tick_hz,
        pin_map_path=args.pin_map,
        pattern_burst=args.pattern_burst,
        pattern=args.pattern,
        time_scale=args.time_scale,
        include_backend_candidates=not args.no_backend_candidates,
        runtime_profile=args.runtime_profile,
        max_rates=args.max_rates,
        loop_policy=args.loop_policy,
    )
    segmented_report, segmentation_error = _run_segment_on_capacity(
        args,
        analysis_output=output,
        report=report,
    )
    write_source_analysis_report(output, report)
    print(f"WROTE analysis={output}")
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_source_analysis_text(report))
    if args.segment_on_capacity:
        if segmented_report is not None:
            print(
                "SEGMENTATION COMPLETE "
                f"report={report['segmentation']['report_path']} "
                f"segments={report['segmentation']['segment_count']} "
                "device_io_performed=False"
            )
            return 0
        print(f"SEGMENTATION NOT_PERFORMED reason={segmentation_error}")
    status = str(report.get("status"))
    if status == "REJECT" or (args.strict and status == "REVIEW"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
