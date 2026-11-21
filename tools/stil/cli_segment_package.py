"""Create host-only segmented STIL packages and manifests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .markers import parse_marker_specs
from .parser import parse_stil_file
from .prepared_source import PreparedStilSource
from .rate_sweep import RateSweepError, parse_variant_spec
from .resolve import load_pin_map, resolve_document
from .segmented_package import (
    SegmentedPackageError,
    build_segment_ranges,
    build_segmented_packages,
)
from .runtime_capacity import DEFAULT_RUNTIME_PROFILE, RUNTIME_CAPACITY_PROFILES
from .wait_schedule import DEFAULT_CHUNK_EVENT_CAPACITY, DEFAULT_RING_SLOT_COUNT
from stpg.constants import STPG_DEFAULT_TICK_HZ


def _integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="source or preprocessed STIL")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    split_group = parser.add_mutually_exclusive_group(required=True)
    split_group.add_argument("--segment-vector-count", type=_integer)
    split_group.add_argument("--split-at", action="append", type=_integer, metavar="GLOBAL_END")
    parser.add_argument("--variant", action="append", metavar="NAME=TABLE:PERIOD[,TABLE:PERIOD...]")
    parser.add_argument("--mode", choices=("auto", "rate-vector", "raw-event", "macro-stream"), default="auto")
    parser.add_argument(
        "--loop-policy",
        choices=("expand", "preserve"),
        default="expand",
        help="expand STIL loops or retain them for the Rate Vector LoopBlock backend",
    )
    parser.add_argument("--tick-hz", type=int, default=STPG_DEFAULT_TICK_HZ)
    parser.add_argument("--max-rates", type=int)
    parser.add_argument("--chunk-event-capacity", type=int, default=DEFAULT_CHUNK_EVENT_CAPACITY)
    parser.add_argument("--ring-slot-count", type=int, default=DEFAULT_RING_SLOT_COUNT)
    parser.add_argument("--pin-map", type=Path)
    parser.add_argument("--pattern-burst")
    parser.add_argument("--pattern")
    parser.add_argument(
        "--marker",
        action="append",
        default=[],
        help="add a source marker: NAME:vector=N or NAME:line=N with mode=pulse/level:set",
    )
    parser.add_argument(
        "--marker-clear",
        action="append",
        default=[],
        help="clear a level marker at vector=N or line=N",
    )
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--allow-approx", action="store_true")
    parser.add_argument("--prefer-raw", action="store_true")
    parser.add_argument(
        "--runtime-profile",
        choices=tuple(sorted(RUNTIME_CAPACITY_PROFILES)),
        default=DEFAULT_RUNTIME_PROFILE,
    )
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
    parser.add_argument(
        "--forbid-stil-rate",
        action="append",
        default=[],
        metavar="WAVEFORM_TABLE",
        help=(
            "forbid a non-zero wait only when the next chunk's first event "
            "uses this STIL WaveformTable; repeat for multiple rate units"
        ),
    )
    parser.add_argument(
        "--global-wait-schedule",
        type=Path,
        help="project an existing full-pattern wait schedule onto each segment",
    )
    parser.add_argument(
        "--segment-wait-plan",
        choices=("auto", "every-boundary"),
        help=(
            "plan waits once across the logical segmented pattern, then apply and verify "
            "the plan to every segment; auto=pressure-based, "
            "every-boundary=wait at each eligible chunk boundary"
        ),
    )
    return parser


def _source_vector_count(args: argparse.Namespace, prepared_source: PreparedStilSource | None = None) -> int:
    if prepared_source is not None:
        prepared_source.validate_binding(args.input, pin_map_path=args.pin_map,
                                          pattern_burst=args.pattern_burst, pattern=args.pattern,
                                          loop_policy=args.loop_policy)
        return prepared_source.logical_vector_count()
    document = parse_stil_file(args.input, loop_policy=args.loop_policy)
    pin_map = load_pin_map(args.pin_map, document=document)
    resolved = resolve_document(document, pin_map=pin_map, pattern_burst_name=args.pattern_burst,
                                pattern_name=args.pattern, loop_policy=args.loop_policy)
    if args.loop_policy == "preserve":
        from .resolve import _resolved_logical_vector_count

        count = _resolved_logical_vector_count(resolved)
    else:
        count = len(resolved.vectors)
    return count


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        prepared_source = PreparedStilSource(args.input, pin_map_path=args.pin_map,
                                              pattern_burst=args.pattern_burst, pattern=args.pattern,
                                              loop_policy=args.loop_policy)
        total_vectors = _source_vector_count(args, prepared_source)
        seed_cap = args.segment_vector_count
        if seed_cap is not None and total_vectors >= 2:
            # A short source can exceed e.g.128 Loop blocks while remaining
            # below the requested vector cap. Let the real capacity planner
            # shorten it; keep build_segment_ranges' strict multi-range API.
            seed_cap = min(seed_cap, total_vectors - 1)
        ranges = build_segment_ranges(
            total_vectors,
            segment_vector_count=seed_cap,
            split_at=tuple(args.split_at or ()),
        )
        variants = tuple(parse_variant_spec(value) for value in (args.variant or ()))
        markers = parse_marker_specs(args.marker, args.marker_clear)
        report = build_segmented_packages(
            input_path=args.input,
            output_dir=args.output_dir,
            report_path=args.json_out,
            ranges=ranges,
            total_vectors=total_vectors,
            variants=variants,
            mode=args.mode,
            loop_policy=args.loop_policy,
            tick_hz=args.tick_hz,
            pin_map_path=args.pin_map,
            pattern_burst=args.pattern_burst,
            pattern=args.pattern,
            strict=args.strict,
            allow_approx=args.allow_approx,
            prefer_raw=args.prefer_raw,
            runtime_profile=args.runtime_profile,
            max_rates=args.max_rates,
            chunk_event_capacity=args.chunk_event_capacity,
            ring_slot_count=args.ring_slot_count,
            service_time_us=args.service_us,
            service_time_source=args.service_source,
            service_profile_scope=args.service_profile_scope,
            safety_margin_us=args.safety_margin_us,
            max_wait_us=args.max_wait_us,
            max_total_wait_us=args.max_total_wait_us,
            max_wait_count=args.max_wait_count,
            pattern_time_limit_us=args.pattern_time_limit_us,
            global_wait_schedule_path=args.global_wait_schedule,
            segment_wait_plan=args.segment_wait_plan,
            forbidden_stil_rate_units=tuple(args.forbid_stil_rate),
            markers=markers,
            capacity_aware=args.segment_vector_count is not None,
            prepared_source=prepared_source,
        )
    except (OSError, RateSweepError, SegmentedPackageError, ValueError) as exc:
        print(json.dumps({"status": "REJECT", "error": str(exc)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "variants": len(report["variants"]),
                "segments_per_variant": max(
                    (len(variant.get("segments", [])) for variant in report["variants"]),
                    default=0,
                ),
                "report": str(args.json_out),
                "device_io_performed": False,
            },
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
