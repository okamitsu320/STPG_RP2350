"""Portable STPG project/build/plan commands and explicit execution preview."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .artifacts import RunnerError
from .build import build_project, init_project, verify_bundle
from .progress import BuildCancelled, ProgressEvent
from .project import WAIT_MODES
from .device import PortBindingError, load_device_config
from .execution import prepare_run, public_summary, run_bundle
from .execution_gate import check_execution_contract
from .diagnostics import sdk_info
from .results import RUN_RESULT_NAME, describe_result
from .rebinding import rebind_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="copy a normalized STIL input into a new portable project")
    init.add_argument("input", type=Path)
    init.add_argument("--directory", type=Path, required=True, help="new directory; never overwritten")
    init.add_argument("--pin-map", type=Path)
    init.add_argument("--wait-preset", choices=WAIT_MODES, required=True)
    init.add_argument("--chunk-wait-us", type=int,
                      help="positive integer microseconds, required only with chunk-custom; not the service budget")
    init.add_argument("--completion", choices=("hold", "release"), required=True)
    init.add_argument("--service-us", type=float)
    init.add_argument("--service-source", default="")
    init.add_argument("--service-scope", choices=("non-loop", "loop-generic"))
    init.add_argument("--max-total-wait-us", type=float)
    init.add_argument("--forbid-stil-rate", action="append", default=[])
    init.add_argument("--segmentation", choices=("auto", "single"), default="auto",
                      help="auto: segment on capacity only (default); single: never segment")
    init.add_argument("--max-segment-vectors", type=int,
                      help="optional per-segment vector upper bound, not a segment count")
    init.add_argument("--storage", choices=("sram", "psram"), default="psram")
    init.add_argument("--mode", choices=("auto", "raw-event", "rate-vector", "macro-stream"), default="auto")
    init.add_argument("--loop-policy", choices=("expand", "preserve"), default="preserve")
    init.add_argument("--allow-approx", action="store_true",
                      help="explicitly allow nearest-tick timing rounding (default: exact); not a speed/streaming repair")
    build = commands.add_parser("build", help="compile or reuse a completely verified offline bundle")
    build.add_argument("project", type=Path)
    build.add_argument("--work-dir", type=Path)
    build.add_argument("--progress", choices=("text", "jsonl", "none"), default="text")
    verify = commands.add_parser("verify", help="verify one bundle without opening a device")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--manifest-sha256")
    verify.add_argument("--progress", choices=("text", "jsonl", "none"), default="text")
    rebind = commands.add_parser("rebind", help="offline semantic verification and new SDK binding without recompiling")
    rebind.add_argument("bundle", type=Path)
    rebind.add_argument("--manifest-sha256", required=True)
    rebind.add_argument("--output-dir", type=Path, help="new bundle directory; default: sibling content-addressed cache")
    rebind.add_argument("--progress", choices=("text", "jsonl", "none"), default="text")
    describe = commands.add_parser("describe", help="offline STIL-correlated run evidence, without USB identity or paths")
    describe.add_argument("bundle", type=Path)
    describe.add_argument("--manifest-sha256", required=True)
    describe.add_argument("--result", type=Path, required=True)
    plan = commands.add_parser("plan", help="verify/snapshot an execution plan without device contact")
    plan.add_argument("bundle", type=Path)
    plan.add_argument("--manifest-sha256", required=True)
    plan.add_argument("--progress", choices=("text", "jsonl", "none"), default="text")
    commands.add_parser("check", help="pure-file execution structural regression gate, not approval")
    commands.add_parser("version", help="local SDK fingerprint, import snapshot and Python/install details; no device I/O")
    run = commands.add_parser("run", help="UNVALIDATED execution preview; opens only the explicitly bound device")
    run.add_argument("bundle", type=Path)
    run.add_argument("--manifest-sha256", required=True)
    run.add_argument("--device-config", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True, help="new one-shot directory outside the bundle")
    run.add_argument("--confirm-run", action="store_true", help="explicit Run intent, not candidate-specific approval")
    run.add_argument("--progress", choices=("text", "jsonl", "none"), default="text")
    run.add_argument("--controller-config", type=Path, help="optional RP2040/XIAO binding; never auto-resets RUN")
    run.add_argument("--control-plan", type=Path, help="explicit GPIO2+ segment actions (requires controller config)")
    run.add_argument("--external-accessory", type=Path,
                     help="explicit local config for a separately installed accessory process; never auto-discovered")
    # Preserve fail-closed rejection of removed options, without advertising them.
    run.add_argument("--analyzer-config", type=Path, help=argparse.SUPPRESS)
    run.add_argument("--capture-plan", type=Path, help=argparse.SUPPRESS)
    run.add_argument("--marker-derivation", type=Path, help=argparse.SUPPRESS)
    control = commands.add_parser("control", help="separate explicit manual GPIO set / HW reset; never auto-RUN")
    actions = control.add_subparsers(dest="control_command", required=True)
    for name in ("set", "reset", "identify"):
        item = actions.add_parser(name)
        item.add_argument("--controller-config", type=Path, required=True)
        item.add_argument("--output-dir", type=Path, required=True)
        if name == "identify":
            item.add_argument("--confirm-identify", action="store_true")
            continue
        item.add_argument("--confirm-control", action="store_true")
        item.add_argument("--target-device-config", type=Path, help="required for RUN pins; target is leased, not opened")
        if name == "reset":
            item.add_argument("--target", choices=("stpg", "la"), required=True)
            item.add_argument("--pulse-ms", type=int, required=True)
            item.add_argument("--settle-ms", type=int, default=0)
        else:
            item.add_argument("--pin", type=int, required=True)
            item.add_argument("--state", choices=("LOW", "Hi-Z", "HIGH"), required=True)
            item.add_argument("--allow-run-high", action="store_true")
    return parser


def _progress(style: str, event: ProgressEvent) -> None:
    if style == "jsonl":
        print(json.dumps(event.to_dict(), sort_keys=True), file=sys.stderr, flush=True)
    elif style == "text":
        count = "" if event.total is None else f" {event.completed or 0}/{event.total}"
        detail = f" ({event.detail})" if event.detail else ""
        print(f"[{event.elapsed_s:.1f}s] {event.stage}: {event.event}{count}{detail}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    io_possible = False
    try:
        if args.command == "init":
            path = init_project(
                args.input, args.directory, wait_preset=args.wait_preset, completion=args.completion,
                pin_map=args.pin_map, service_us=args.service_us, service_source=args.service_source,
                chunk_wait_us=args.chunk_wait_us,
                service_scope=args.service_scope,
                max_total_wait_us=args.max_total_wait_us,
                forbidden_stil_rates=tuple(args.forbid_stil_rate),
                max_segment_vectors=args.max_segment_vectors,
                segmentation_mode=args.segmentation,
                storage=args.storage, compile_mode=args.mode, loop_policy=args.loop_policy,
                allow_approx=args.allow_approx,
            )
            report = {"status": "PROJECT_CREATED", "project": str(path), "device_io_performed": False,
                      "physical_execution_authorized": False, "execution_available": True}
        elif args.command == "build":
            report = build_project(args.project, work_dir=args.work_dir,
                                   on_progress=lambda event: _progress(args.progress, event)).to_dict()
        elif args.command == "verify":
            report = verify_bundle(args.bundle, manifest_sha256=args.manifest_sha256,
                                   on_progress=lambda event: _progress(args.progress, event))
        elif args.command == "plan":
            report = prepare_run(args.bundle, manifest_sha256=args.manifest_sha256,
                                 on_progress=lambda event: _progress(args.progress, event)).to_dict()
        elif args.command == "rebind":
            report = rebind_bundle(args.bundle, manifest_sha256=args.manifest_sha256, output_dir=args.output_dir,
                                   on_progress=lambda event: _progress(args.progress, event)).to_dict()
        elif args.command == "describe":
            report = describe_result(args.bundle, manifest_sha256=args.manifest_sha256, result=args.result)
            print(json.dumps(report, sort_keys=True))
            return 0  # successful offline formatting, regardless of the recorded run outcome
        elif args.command == "check":
            report = check_execution_contract()
        elif args.command == "version":
            report = sdk_info()
        elif args.command == "control":
            from .artifacts import read_json
            from .controller import load_controller_config, manual_control
            if args.control_command == "identify":
                from .controller import identify_controller
                config = load_controller_config(args.controller_config)
                io_possible = True
                result = identify_controller(config, output_dir=args.output_dir, confirm_identify=args.confirm_identify)
                print(json.dumps({key: result[key] for key in ("kind", "schema_version", "status", "protocol", "errors",
                                                              "pin_commands_written", "physical_readback",
                                                              "result_persisted", "result_file")}, sort_keys=True))
                return 130 if result["status"] == "CANCELLED" else 0 if result["status"] == "IDENTIFIED" else 2
            target = None
            if args.target_device_config:
                value = read_json(args.target_device_config)
                if not isinstance(value, dict):
                    raise RunnerError("target binding must be an object")
                if value.get("kind") == "stpg_runner_device":
                    from .device import DeviceConfig
                    target = DeviceConfig.from_dict(value)
                    target_kind = "stpg"
                elif value.get("kind") in ("stpg_capture_analyzer", "stpg_external_device"):
                    from .external_binding import ExternalDeviceBinding
                    target = ExternalDeviceBinding.from_dict(value)
                    target_kind = "la"
                else:
                    raise RunnerError("unsupported target binding kind")
            pin = (0 if args.target == "stpg" else 1) if args.control_command == "reset" else args.pin
            if pin in (0, 1) and (target is None or target_kind != ("stpg" if pin == 0 else "la")):
                raise RunnerError("RUN pin requires its matching STPG/LA target binding")
            controller = load_controller_config(args.controller_config)
            io_possible = True
            result = manual_control(controller, output_dir=args.output_dir,
                                    pin=pin, target=target, confirm_control=args.confirm_control,
                                    state=args.state if args.control_command == "set" else None,
                                    pulse_ms=args.pulse_ms if args.control_command == "reset" else None,
                                    settle_ms=args.settle_ms if args.control_command == "reset" else 0,
                                    allow_run_high=args.allow_run_high if args.control_command == "set" else False)
            report = {key: result[key] for key in ("kind", "schema_version", "outcome", "result_persisted", "errors",
                                                   "electrical_release_verified")}
            report["result_file"] = str(args.output_dir.resolve() / "control.json")
        else:
            if any((args.analyzer_config, args.capture_plan, args.marker_derivation)):
                raise RunnerError("in-process analyzer options were removed; use --external-accessory with a separate tool")
            selected = any((args.controller_config, args.control_plan, args.external_accessory))
            if selected:
                from .artifacts import read_json
                from .accessories import run_with_accessories, accessory_exit
                from .controller import load_controller_config
                options = {}
                runner = run_with_accessories
                if args.external_accessory:
                    from .external_accessory import load_external_accessory, run_with_external_accessory
                    runner = run_with_external_accessory
                    options["spec"] = load_external_accessory(args.external_accessory)
                io_possible = True
                result = runner(args.bundle, manifest_sha256=args.manifest_sha256,
                                              device=load_device_config(args.device_config), output_dir=args.output_dir,
                                              confirm_run=args.confirm_run,
                                              controller=load_controller_config(args.controller_config) if args.controller_config else None,
                                              control_plan=read_json(args.control_plan) if args.control_plan else None,
                                              on_progress=lambda event: _progress(args.progress, event), **options)
                code = accessory_exit(result)
                control = result["control"]
                capture = result["capture"]
                external = result.get("external")
                report = {"kind": "stpg_runner_accessory_result", "schema_version": 1,
                          "run": result["run"], "result_file": result["run_result_file"],
                          "control": {key: control[key] for key in ("outcome", "result_persisted", "errors")} if control else None,
                          "capture": {key: capture[key] for key in ("outcome", "alignment_status", "timing", "result_persisted", "errors")} if capture else None,
                          "external": {key: external[key] for key in ("outcome", "mode", "adopted", "result_persisted", "errors")} if external else None,
                          "exit_code": code}
                print(json.dumps(report, sort_keys=True))
                return code
            io_possible = True
            result = run_bundle(args.bundle, manifest_sha256=args.manifest_sha256,
                                device=load_device_config(args.device_config), output_dir=args.output_dir,
                                confirm_run=args.confirm_run,
                                on_progress=lambda event: _progress(args.progress, event))
            report = public_summary(result)
            report["result_file"] = str(args.output_dir.resolve() / RUN_RESULT_NAME)
    except BuildCancelled:
        print(json.dumps({"status": "CANCELLED", "device_io_performed": None if io_possible else False,
                          "device_io_possible": io_possible, "physical_execution_authorized": False}))
        return 130
    except (RunnerError, OSError, ValueError) as exc:
        report = {"status": "REJECT", "errors": [f"{type(exc).__name__}: {exc}"],
                  "device_io_performed": None if io_possible else False, "device_io_possible": io_possible,
                  "physical_execution_authorized": False,
                  "execution_available": False}
        if isinstance(exc, PortBindingError):
            report["port_check"] = exc.details
    except KeyboardInterrupt:
        print(json.dumps({"status": "CANCELLED", "device_io_performed": None if io_possible else False,
                          "device_io_possible": io_possible,
                          "physical_execution_authorized": False, "execution_available": False}))
        return 130
    print(json.dumps(report, sort_keys=True))
    outcome = report.get("outcome", report.get("status"))
    if outcome == "CANCELLED":
        return 130
    if outcome in {"REJECT", "DEVICE_ERROR"}:
        return 2
    return 3 if report.get("comparison") == "MISMATCH" else 0


if __name__ == "__main__":
    raise SystemExit(main())
