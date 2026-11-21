from __future__ import annotations

import argparse
import json
from pathlib import Path

from .underflow_analyze import (
    DEFAULT_CHUNK_EVENT_BUDGET,
    analyze_rate_vector_underflow,
    load_optional_json,
    render_underflow_analysis_text,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze local underflow concentration risk for STPG rate-vector patterns")
    parser.add_argument("input", type=Path, help="compiled .bin rate-vector pattern")
    parser.add_argument("--map", dest="map_path", type=Path, help="optional .map.json")
    parser.add_argument("--compile-report", dest="compile_report_path", type=Path, help="optional .compile_report.json")
    parser.add_argument("--runtime-report", dest="runtime_report_path", type=Path, help="optional practical_baseline_probe JSON")
    parser.add_argument("--chunk-event-budget", type=int, default=DEFAULT_CHUNK_EVENT_BUDGET)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def _default_sidecar(path: Path, suffix: str) -> Path | None:
    candidate = path.with_suffix(suffix)
    return candidate if candidate.exists() else None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pattern_bytes = args.input.read_bytes()
    map_path = args.map_path or _default_sidecar(args.input, ".map.json")
    compile_report_path = args.compile_report_path or _default_sidecar(args.input, ".compile_report.json")
    map_data = load_optional_json(map_path)
    compile_report = load_optional_json(compile_report_path)
    runtime_report = load_optional_json(args.runtime_report_path)
    report = analyze_rate_vector_underflow(
        pattern_bytes,
        map_data=map_data,
        compile_report=compile_report,
        runtime_report=runtime_report,
        chunk_event_budget=args.chunk_event_budget,
        top_n=args.top,
    )
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(render_underflow_analysis_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
