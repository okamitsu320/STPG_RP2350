"""Command line entry point: ``stpg-view`` (also ``python -m stpgview``)."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .session import Session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stpg-view",
        description=(
            "View STPG patterns (STIL, pattern.bin with its map.json, or a package directory) "
            "in a local browser UI, or export them as VCD."
        ),
    )
    parser.add_argument("inputs", nargs="*", help=".stil, pattern .bin, map/pin-map/manifest JSON, or package dir")
    parser.add_argument("--pin-map", help="pin map JSON (signal names/bits)")
    parser.add_argument("--map", help="compiler map JSON for a pattern .bin (default: found next to the .bin)")
    parser.add_argument("--vcd", type=Path, help="write a VCD of the first input and exit (no UI)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="port (default: any free port)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    runner = parser.add_argument_group("runner")
    runner.add_argument("--no-device-run", action="store_true",
                        help="view/build/plan only: the GUI never opens an STPG or analyzer port")
    runner.add_argument("--demo-device", action="store_true",
                        help="add a simulated STPG and analyzer (no hardware) to try the runner UI")
    runner.add_argument("--config-dir", help="settings folder for local bindings (default: STPG_CONFIG_DIR, else the "
                        "per-user folder); use the same folder for stpg-la")
    runner.add_argument("--results-dir", type=Path,
                        help="results for bundles outside a project (default: ~/stpg-work/runs)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session = Session()
    if args.inputs:
        try:
            session.load(args.inputs, pin_map=args.pin_map, map_path=args.map)
        except (ValueError, OSError) as exc:
            print(f"stpg-view: {exc}", file=sys.stderr)
            return 2
    if args.vcd:
        if not session.order:
            print("stpg-view: --vcd needs an input", file=sys.stderr)
            return 2
        from .vcd import export_vcd

        trace = session.get(session.order[0])
        export_vcd(trace, args.vcd)
        print(f"wrote {args.vcd} ({trace.vector_count} vectors, {len(trace.signals)} signals)")
        return 0
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("stpg-view: warning: serving on a non-loopback address exposes local files to the network; "
              "the runner is disabled", file=sys.stderr)
    from .config_location import ConfigLocationError
    from .server import serve

    try:
        serve(session, host=args.host, port=args.port, open_browser=not args.no_browser,
              device_run=not args.no_device_run, demo=args.demo_device, config_dir=args.config_dir,
              results_dir=args.results_dir)
    except ConfigLocationError as exc:  # never a silent fall-back to another folder
        print(f"stpg-view: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
