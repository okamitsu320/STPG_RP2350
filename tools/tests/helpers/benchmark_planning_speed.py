"""Public, generated host-only planning benchmark; never accepts a user pattern.

Run serially with a chosen SDK on PYTHONPATH. --profile adds profiler overhead;
compare wall times only between unprofiled runs with the same public options.
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
from pathlib import Path
import pstats
import tempfile
import time

import stil.resolve as resolver
import stil.segmented_package as planner
import stpg.pattern as pattern
from stil.json_artifacts import read_json_artifact
from stpg_runner import build_project, init_project, verify_bundle
from stpg_runner.build import tool_fingerprint


def synthetic_source(vectors: int, *, loops: bool = False, wide: bool = False) -> str:
    """Eight ordinary drive signals; no private data, compare or device access."""
    signals = [f"A{i}" for i in range(8)]
    prefix = ("Signals {\n" + "\n".join(f" {s} In;" for s in signals) + "\n}\n"
              + "SignalGroups {\n ALL = '" + " + ".join(signals) + "';\n}\n"
              + "Timing {\n WaveformTable NORMAL {\n Period '" + ("400us" if wide else "400ns")
              + "';\n Waveforms {\n"
              + "\n".join(f" {s} {{ 0: 0ns Drive(0); 1: 0ns Drive(1); }}" for s in signals)
              + "\n}\n}\n}\nPattern MAIN {\n W NORMAL;\n")
    def vector(i):
        # Adjacent identical vectors exercise compressed records as well.
        return f" V {{ ALL = {i // 4 % 256:08b}; }}\n"
    if not loops:
        body = "".join(vector(i) for i in range(vectors))
    else:
        blocks, tail = divmod(vectors, 8)
        body = "".join(vector(i * 8) + vector(i * 8 + 4)
                       + " Loop 3 {\n" + vector(i * 8) + vector(i * 8 + 4) + " }\n"
                       for i in range(blocks))
        body += "".join(vector(blocks * 8 + i) for i in range(tail))
    return prefix + body + "}\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vectors", type=int, default=20_000)
    ap.add_argument("--region-bytes", type=int, default=100_000)
    ap.add_argument("--wait-preset", choices=("none", "chunk-4ms"), default="none")
    ap.add_argument("--loops", action="store_true")
    ap.add_argument("--wide", action="store_true")
    ap.add_argument("--segment-cap", type=int)
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()
    if not 2 <= args.vectors <= 800_000 or not 100 <= args.region_bytes <= 5_177_344:
        ap.error("require 2..800000 vectors and 100..5177344 bytes")
    root = Path(tempfile.mkdtemp(prefix="stpg-planning-benchmark-"))
    source = root / "public.stil"
    data = synthetic_source(args.vectors, loops=args.loops, wide=args.wide).encode("ascii")
    source.write_bytes(data)
    # Only a test encoder boundary changes. Runtime profiles and real final
    # package validators are NOT replaced by passing dictionaries.
    pattern.STPG_PSRAM_PATTERN_BYTES = args.region_bytes
    counts = {"lowerings": 0, "decodes": 0}
    def counted(key, function):
        def call(*a, **kw):
            counts[key] += 1
            return function(*a, **kw)
        return call
    resolver.lower_pattern_control_flow = counted("lowerings", resolver.lower_pattern_control_flow)
    pattern._decode_rate_vector_full = counted("decodes", pattern._decode_rate_vector_full)
    wait = (dict(service_us=50, service_source="public declared benchmark assumption; not measured",
                 service_scope="loop-generic" if args.loops else "non-loop", max_total_wait_us=1e11)
            if args.wait_preset != "none" else {})
    project = init_project(source, root / "project", wait_preset=args.wait_preset, completion="hold",
                           max_segment_vectors=args.segment_cap, **wait)
    events = []
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    begin, cpu_begin = time.perf_counter(), time.process_time()
    # No heartbeat thread in the profiler: cumulative wait/join frames would
    # otherwise obscure CPU work. Unprofiled runs retain stage wall timings.
    built = build_project(project, on_progress=None if profiler else events.append)
    wall, cpu = time.perf_counter() - begin, time.process_time() - cpu_begin
    if profiler:
        profiler.disable()
    try:
        import resource
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except ImportError:
        max_rss = None
    binaries = sorted(built.bundle_dir.glob("segmented/nominal/segment-*/package/pattern.bin"))
    if binaries:
        manifest = read_json_artifact(built.bundle_dir / "segmented/nominal/segmented.manifest.json")
        plan = manifest["capacity_plan"]
    else:
        binaries = [built.bundle_dir / "package/pattern.bin"]
        plan = {}
    phases = {}
    starts = {}
    for event in events:
        if event.event == "start":
            starts[event.stage] = event.elapsed_s
        elif event.event == "done" and event.stage in starts:
            phases[event.stage] = phases.get(event.stage, 0) + event.elapsed_s - starts.pop(event.stage)
    top, top_self = [], []
    if profiler:
        stats = pstats.Stats(profiler).stats
        for (file, line, name), (cc, nc, tt, ct, _callers) in sorted(
                stats.items(), key=lambda pair: pair[1][3], reverse=True)[:24]:
            top.append({"file": Path(file).name, "line": line, "function": name,
                        "calls": nc, "primitive_calls": cc, "self_s": tt, "cumulative_s": ct})
        for (file, line, name), (cc, nc, tt, ct, _callers) in sorted(
                stats.items(), key=lambda pair: pair[1][2], reverse=True)[:16]:
            top_self.append({"file": Path(file).name, "line": line, "function": name,
                             "calls": nc, "self_s": tt, "cumulative_s": ct})
    verification = verify_bundle(built.bundle_dir, manifest_sha256=built.manifest_sha256)
    assert verification["status"] == "PASS", verification
    print(json.dumps({"kind": "public_planning_speed_benchmark", "options": vars(args),
                      "workspace": str(root), "tool_sha256": tool_fingerprint(),
                      "source_sha256": hashlib.sha256(data).hexdigest(),
                      "wall_s": wall, "cpu_s": cpu, "phases_s": phases, **counts,
                      "probe_count": plan.get("probe_count"), "ranges": plan.get("ranges"),
                      "binaries": [{"size_bytes": p.stat().st_size,
                                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in binaries],
                      "verification": verification["status"], "profile": top, "profile_self": top_self,
                      "max_rss_native_units": max_rss,
                      "device_io_performed": False, "physical_execution_authorized": False}))


if __name__ == "__main__":
    main()
