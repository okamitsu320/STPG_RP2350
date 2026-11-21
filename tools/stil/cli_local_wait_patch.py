from __future__ import annotations

import argparse
from pathlib import Path

from .local_wait_patch import (
    _default_sidecar,
    load_candidate_from_report_or_analyze,
    patch_local_wait_candidate,
    RUNTIME_PAGE30_POLICIES,
    write_local_wait_patch_outputs,
)
from .underflow_analyze import load_optional_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Patch a compiled rate-vector pattern with a boundary-near local wait")
    parser.add_argument("input", type=Path, help="compiled .bin rate-vector pattern")
    parser.add_argument("-o", "--output", type=Path, required=True, help="output patched .bin")
    parser.add_argument("--map", dest="map_path", type=Path, help="optional .map.json sidecar")
    parser.add_argument("--compile-report", dest="compile_report_path", type=Path, help="optional .compile_report.json sidecar")
    parser.add_argument("--runtime-report", dest="runtime_report_path", type=Path, help="optional practical_baseline_probe JSON sidecar")
    parser.add_argument("--prefer-runtime-page30", action="store_true", help="prefer PAGE30-derived local-wait candidate over compile_report ordering")
    parser.add_argument(
        "--runtime-page30-policy",
        choices=RUNTIME_PAGE30_POLICIES,
        default="exact",
        help="policy applied when candidate_source=runtime_page30",
    )
    parser.add_argument("--candidate-index", type=int, required=True, help="index into practical_local_wait_candidates")
    parser.add_argument("--extra-ticks", type=int, help="override applied extra ticks; must be >= candidate minimum")
    parser.add_argument("--target-global-range", help="optional start:end override within the selected candidate global range")
    parser.add_argument(
        "--all-spans-for-rate",
        action="store_true",
        help="apply the candidate's cloned rate to every span using the same original rate_id",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    map_path = args.map_path or _default_sidecar(args.input, ".map.json")
    compile_report_path = args.compile_report_path or _default_sidecar(args.input, ".compile_report.json")
    runtime_report_path = args.runtime_report_path
    map_data = load_optional_json(map_path)
    report_data = load_optional_json(compile_report_path)
    runtime_report_data = load_optional_json(runtime_report_path)
    if map_data is None:
        raise SystemExit("map.json is required for local wait patching")
    if report_data is None and runtime_report_data is None:
        raise SystemExit("compile_report.json is required for local wait patching")

    input_bytes = args.input.read_bytes()
    candidate, candidate_source = load_candidate_from_report_or_analyze(
        pattern_bytes=input_bytes,
        map_data=map_data,
        report_data=report_data,
        runtime_report_data=runtime_report_data,
        prefer_runtime_page30=args.prefer_runtime_page30,
        runtime_page30_policy=args.runtime_page30_policy,
        candidate_index=args.candidate_index,
    )
    patched_pattern, patched_map, manifest = patch_local_wait_candidate(
        input_bytes,
        map_data=map_data,
        report_data={"capability_precheck": {"runtime_precheck": {"practical_local_wait_candidates": [candidate]}}},
        candidate_index=args.candidate_index,
        extra_ticks=args.extra_ticks,
        target_global_start=None if args.target_global_range is None else int(args.target_global_range.split(":", 1)[0]),
        target_global_end=None if args.target_global_range is None else int(args.target_global_range.split(":", 1)[1]),
        all_spans_for_rate=args.all_spans_for_rate,
    )
    write_local_wait_patch_outputs(args.output, patched_pattern, patched_map, manifest)

    print(f"WROTE pattern={args.output}")
    print(f"WROTE map={args.output.with_suffix('.map.json')}")
    print(f"WROTE manifest={args.output.with_suffix('.local_wait_patch.json')}")
    print(
        "PATCH "
        f"candidate_index={args.candidate_index} "
        f"candidate_source={candidate_source} "
        f"rate[{candidate['rate_id']}] "
        f"group[{candidate['group_index']}] "
        f"min_extra={candidate['minimum_extra_ticks']} "
        f"applied_extra={manifest['applied_extra_ticks']} "
        f"all_spans_for_rate={'yes' if manifest['all_spans_for_rate'] else 'no'}"
    )
    print(
        "RANGE "
        f"global={candidate.get('global_vector_start')}:{candidate.get('global_vector_end')} "
        f"expanded={candidate.get('expanded_vector_start')}:{int(candidate.get('expanded_vector_end_exclusive', 1)) - 1} "
        f"patched_ranges={manifest.get('patched_global_ranges', [])}"
    )
    print(
        "POST "
        f"remaining_local_wait_candidates={len(manifest['post_patch_runtime_hints']['local_wait_candidates'])} "
        f"remaining_generator_avoidance_candidates={len(manifest['post_patch_runtime_hints']['generator_avoidance_candidates'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
