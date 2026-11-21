from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from .markers import parse_marker_specs
from .pipeline import CompileOptions, compile_stil, write_compile_outputs
from .runtime_capacity import DEFAULT_RUNTIME_PROFILE, RUNTIME_CAPACITY_PROFILES
from stpg.constants import STPG_DEFAULT_TICK_HZ


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile a STIL subset into STPG pattern binary")
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("auto", "rate-vector", "raw-event", "macro-stream"), default="auto")
    parser.add_argument("--tick-hz", type=int, default=STPG_DEFAULT_TICK_HZ)
    parser.add_argument("--max-rates", type=int, help="rate-vector descriptor limit for this host-only contract")
    parser.add_argument(
        "--loop-policy",
        choices=("expand", "preserve"),
        default="expand",
        help="expand STIL loops for compatibility or preserve them in the Rate Vector ABI",
    )
    parser.add_argument("--pin-map", type=Path)
    parser.add_argument("--pattern-burst")
    parser.add_argument("--pattern")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--compress-json",
        action="store_true",
        help="gzip large JSON sidecars when this reduces their stored size",
    )
    parser.add_argument("--allow-approx", action="store_true")
    parser.add_argument("--prefer-raw", action="store_true")
    parser.add_argument("--until-vector", type=int)
    parser.add_argument("--vector-range")
    parser.add_argument("--time-scale", type=Decimal, default=Decimal("1.0"))
    parser.add_argument("--marker", action="append", default=[])
    parser.add_argument("--marker-clear", action="append", default=[])
    parser.add_argument("--practical-chunk-shaping", action="store_true")
    parser.add_argument("--shape-boundary-global")
    parser.add_argument("--shape-max-run-vectors", type=int, default=64)
    parser.add_argument("--practical-gate-policy", choices=("error", "report"), default="error")
    parser.add_argument(
        "--runtime-profile",
        choices=tuple(sorted(RUNTIME_CAPACITY_PROFILES)),
        default=DEFAULT_RUNTIME_PROFILE,
        help="firmware capacity contract recorded in the compile report",
    )
    return parser


def _fmt_range(start: object, end: object) -> str:
    if start is None and end is None:
        return "-"
    if start is None:
        return f"?:{end}"
    if end is None:
        return f"{start}:?"
    if start == end:
        return str(start)
    return f"{start}:{end}"


def emit_compile_summary(
    output: Path,
    report_data: dict,
    *,
    map_path: Path | None = None,
    compile_report_path: Path | None = None,
) -> None:
    print(f"WROTE pattern={output}")
    print(f"WROTE map={map_path or output.with_suffix('.map.json')}")
    print(f"WROTE compile_report={compile_report_path or output.with_suffix('.compile_report.json')}")

    runtime_precheck = report_data.get("capability_precheck", {}).get("runtime_precheck", {})
    diagnostics = report_data.get("diagnostics", [])

    print(
        "SUMMARY "
        f"mode_requested={report_data.get('mode_requested')} "
        f"mode_emitted={report_data.get('mode_emitted')} "
        f"compile_strategy={runtime_precheck.get('compile_strategy')} "
        f"tick_hz={report_data.get('capability_precheck', {}).get('tick_hz')} "
        f"estimated_events={runtime_precheck.get('estimated_event_count')}"
    )

    warning_codes = [item.get("code") for item in diagnostics if item.get("severity") == "warning" and item.get("code")]
    if warning_codes:
        print(f"WARNING_CODES {', '.join(warning_codes)}")

    direct_exec_fit = runtime_precheck.get("direct_execution_fit") or {}
    if direct_exec_fit:
        print(
            "DIRECT_EXEC_FIT "
            f"supported={direct_exec_fit.get('supported')} "
            f"events={direct_exec_fit.get('estimated_event_count')}/{direct_exec_fit.get('event_limit')} "
            f"samples={direct_exec_fit.get('estimated_sample_count')}/{direct_exec_fit.get('sample_limit')} "
            f"reason={direct_exec_fit.get('reason')}"
        )

    local_waits = runtime_precheck.get("practical_local_wait_candidates") or []
    if local_waits:
        print("PRACTICAL_LOCAL_WAIT_CANDIDATES")
        for item in local_waits[:3]:
            line = item.get("stil_line_start")
            global_range = _fmt_range(item.get("global_vector_start"), item.get("global_vector_end"))
            print(
                "  "
                f"rate[{item.get('rate_id')}] "
                f"group[{item.get('group_index')}] "
                f"extra>={item.get('minimum_extra_ticks')} tick "
                f"({item.get('minimum_extra_ns')}ns) "
                f"global={global_range} "
                f"line={line if line is not None else '-'}"
            )

    if runtime_precheck.get("practical_chunk_shaping_applied"):
        policy = runtime_precheck.get("practical_chunk_shaping_policy") or {}
        target = policy.get("target_global_range")
        print(
            "PRACTICAL_CHUNK_SHAPING "
            f"target_global={target} "
            f"max_run_vectors={policy.get('max_run_vectors')} "
            f"segments={len(runtime_precheck.get('practical_chunk_shaping_segments') or [])}"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    vector_range = None
    shape_boundary_global = None
    if args.vector_range is not None:
        start_text, end_text = args.vector_range.split(":", 1)
        vector_range = (int(start_text), int(end_text))
    if args.shape_boundary_global is not None:
        start_text, end_text = args.shape_boundary_global.split(":", 1)
        shape_boundary_global = (int(start_text), int(end_text))
    payload, map_data, report_data = compile_stil(
        CompileOptions(
            input_path=args.input,
            mode=args.mode,
            tick_hz=args.tick_hz,
            pin_map_path=args.pin_map,
            pattern_burst=args.pattern_burst,
            pattern=args.pattern,
            strict=args.strict,
            allow_approx=args.allow_approx,
            prefer_raw=args.prefer_raw,
            until_vector=args.until_vector,
            vector_range=vector_range,
            time_scale=args.time_scale,
            markers=parse_marker_specs(args.marker, args.marker_clear),
            practical_chunk_shaping=args.practical_chunk_shaping,
            practical_chunk_shaping_target_range=shape_boundary_global,
            practical_chunk_shaping_max_run_vectors=args.shape_max_run_vectors,
            practical_gate_policy=args.practical_gate_policy,
            runtime_profile=args.runtime_profile,
            max_rates=args.max_rates,
            loop_policy=args.loop_policy,
        )
    )
    map_path, compile_report_path = write_compile_outputs(
        args.output,
        payload,
        map_data,
        report_data,
        json_compression="auto" if args.compress_json else "none",
    )
    emit_compile_summary(
        args.output,
        report_data,
        map_path=map_path,
        compile_report_path=compile_report_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
