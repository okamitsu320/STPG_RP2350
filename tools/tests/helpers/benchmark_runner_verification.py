"""Offline public-synthetic benchmark; never imports a physical backend.

PYTHONPATH=tools .venv/bin/python tools/reviews/benchmark_runner_verification.py
For the public request8 default-mode capacity regression, add
--vectors 300000 --capacity-first (host build can take several minutes).
Add --wait-preset chunk-4ms for the public request9 post-wait capacity check.
Use a disk-backed temporary workspace for much larger --vectors values. Times
are host measurements, not device/USB/waveform throughput or hard deadlines.
"""
from contextlib import nullcontext
import argparse
import json
from pathlib import Path
import tempfile
import time

from stpg import pattern
from stpg_runner import build as builder, prepare_run


def synthetic_source(vectors: int) -> str:
    prefix = """Signals {
 A InOut;
 B InOut;
}
SignalGroups {
 ALL = 'A + B';
}
Timing {
 WaveformTable NORMAL {
  Period '200ns';
  Waveforms {
   ALL { 0: 0ns Drive(0); 1: 0ns Drive(1); }
  }
 }
}
Pattern MAIN {
 W NORMAL;
"""
    return prefix + "".join(f"V {{ ALL = {i & 1}{1 - (i & 1)}; }}\n" for i in range(vectors)) + "}\n"


def storage_summary(project: Path) -> dict:
    """This script's public generated project only; never accepts user input."""
    files = [path for path in project.rglob("*") if path.is_file()]
    sources = [path for path in files if path.name.endswith((".stil", ".stil.gz"))]
    return {
        "project_total_bytes": sum(p.stat().st_size for p in files),
        "input_stil_bytes": sum(p.stat().st_size for p in sources if p.is_relative_to(project / "inputs")),
        "snapshot_stil_bytes": sum(p.stat().st_size for p in sources if "sources" in p.relative_to(project).parts),
        "raw_stil_members": [p.relative_to(project).as_posix() for p in sources if p.suffix == ".stil"],
        "pattern_bin_bytes": sum(p.stat().st_size for p in files if p.name == "pattern.bin"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vectors", type=int, default=20_000)
    parser.add_argument("--segment-cap", type=int, default=None,
                        help="explicit cap (default10000, unless --capacity-first)")
    parser.add_argument("--capacity-first", action="store_true",
                        help="SDK defaults: mode auto, preserve, automatic segmentation with no vector cap")
    parser.add_argument("--wait-preset", choices=("none", "chunk-4ms"), default="none",
                        help="public synthetic assumption: 50us service, not a device measurement")
    args = parser.parse_args()
    if args.capacity_first and args.segment_cap is not None:
        parser.error("--capacity-first and --segment-cap are mutually exclusive")
    cap = None if args.capacity_first else (10_000 if args.segment_cap is None else args.segment_cap)
    if not 2 <= args.vectors <= 1_000_000 or cap is not None and not 1 <= cap < args.vectors:
        parser.error("require 2..1000000 vectors and, when capped, a smaller positive cap")
    root = Path(tempfile.mkdtemp(prefix="stpg-verify-benchmark-"))
    source = root / "public.stil"
    # This script generates its own public source. It accepts no user pattern.
    source.write_text(synthetic_source(args.vectors), encoding="utf-8")
    measurements = []
    actual_decode = pattern._decode_rate_vector_full
    decoded = []
    def counted(raw):
        decoded.append(len(raw))
        return actual_decode(raw)
    pattern._decode_rate_vector_full = counted
    def measure(name, action):
        decoded.clear()
        begin, cpu_begin = time.perf_counter(), time.process_time()
        result = action()
        measurements.append({"step": name, "wall_s": time.perf_counter() - begin,
                             "cpu_s": time.process_time() - cpu_begin, "actual_decodes": len(decoded)})
        return result
    wait_options = (dict(service_us=50, service_source="public benchmark assumption, not measured",
                         service_scope="non-loop", max_total_wait_us=1e11)
                    if args.wait_preset != "none" else {})
    project = builder.init_project(source, root / "project", wait_preset=args.wait_preset, completion="hold",
                                   compile_mode="auto" if args.capacity_first else "rate-vector",
                                   max_segment_vectors=cap, **wait_options)
    result = measure("build", lambda: builder.build_project(project))
    options = dict(manifest_sha256=result.manifest_sha256)
    original_scope = builder.rate_vector_decode_scope
    try:
        builder.rate_vector_decode_scope = nullcontext
        baseline = measure("full_without_decode_memo", lambda: builder.verify_bundle(result.bundle_dir, **options))
    finally:
        builder.rate_vector_decode_scope = original_scope
    full = measure("full_with_decode_memo", lambda: builder.verify_bundle(result.bundle_dir, **options))
    assert baseline == full and full["status"] == "PASS"
    builder._clear_verification_receipts()
    cold = measure("cold_prepare", lambda: prepare_run(result.bundle_dir, **options))
    warm = measure("warm_prepare", lambda: prepare_run(result.bundle_dir, **options))
    assert [s.to_dict() for s in cold.segments] == [s.to_dict() for s in warm.segments]
    reused = measure("receipt_inventory", lambda: builder.verify_bundle(result.bundle_dir, reuse=True, **options))
    assert reused["status"] == "PASS" and reused["check_mode"] == "process_receipt"
    print(json.dumps({"kind": "public_synthetic_verification_benchmark", "workspace": str(root),
                      "vectors": args.vectors, "segment_cap": cap, "capacity_first": args.capacity_first,
                      "wait_preset": args.wait_preset,
                      "segments": result.segment_count,
                      "segment_contracts": [s.expected for s in warm.segments],
                      "payload_bytes": sum(len(s.payload) for s in warm.segments),
                      "tool_sha256": builder.tool_fingerprint(), "measurements": measurements,
                      "storage": storage_summary(project.parent),
                      "device_io_performed": False, "physical_execution_authorized": False}))


if __name__ == "__main__":
    main()
