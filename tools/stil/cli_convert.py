"""One-command host-only STIL conversion and explicit wait preparation."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import shutil
from typing import Any

from .cli_package import _add_compile_options
from .markers import parse_marker_specs
from .pattern_package import (
    PatternPackageError,
    PatternPackageOptions,
    build_pattern_package,
    derive_pattern_package_with_wait,
)
from .wait_acceptance import evaluate_wait_schedule
from .wait_schedule import WaitScheduleError, propose_wait_schedule
from .workspace import add_work_dir_options, resolve_work_dir


CONVERSION_KIND = "stpg_stil_conversion_workflow"
CONVERSION_SCHEMA_VERSION = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "convert one STIL input into a verified host-only package and, optionally, "
            "prepare an explicit wait proposal"
        )
    )
    parser.add_argument("input", type=Path)
    add_work_dir_options(parser, scope="convert")
    _add_compile_options(parser)
    parser.add_argument(
        "--wait-policy",
        choices=("none", "propose", "apply"),
        default="none",
        help=(
            "none: build one package; propose: add an editable wait proposal and acceptance; "
            "apply: derive a package only when the proposal is accepted"
        ),
    )
    parser.add_argument("--wait-schedule", type=Path, help="explicit schedule for the package when --wait-policy=none")
    parser.add_argument("--forbid-rate-id", action="append", type=int, default=[])
    parser.add_argument(
        "--forbid-stil-rate",
        action="append",
        default=[],
        metavar="WAVEFORM_TABLE",
        help=(
            "forbid a non-zero wait only when the next chunk's first event "
            "uses this STIL WaveformTable"
        ),
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
    parser.add_argument("--json-out", type=Path)
    return parser


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _package_options(args: argparse.Namespace, output_dir: Path, *, wait_schedule: Path | None) -> PatternPackageOptions:
    return PatternPackageOptions(
        input_path=args.input,
        output_dir=output_dir,
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
        wait_schedule_path=wait_schedule,
        service_time_us=args.service_us,
        service_time_source=args.service_source,
        service_profile_scope=args.service_profile_scope,
        safety_margin_us=args.safety_margin_us,
        max_wait_us=args.max_wait_us,
        max_total_wait_us=args.max_total_wait_us,
        max_wait_count=args.max_wait_count,
        pattern_time_limit_us=args.pattern_time_limit_us,
        runtime_profile=args.runtime_profile,
        max_rates=args.max_rates,
        chunk_event_capacity=args.chunk_event_capacity,
        ring_slot_count=args.ring_slot_count,
        forbidden_stil_rate_units=tuple(args.forbid_stil_rate),
    )


def _empty_output_dir(path: Path) -> None:
    if path.exists() and not path.is_dir():
        raise PatternPackageError(f"output path is not a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise PatternPackageError(f"output directory must be empty: {path}")


def _proposal_and_acceptance(args: argparse.Namespace, package_dir: Path, output_dir: Path) -> tuple[dict, dict, dict]:
    if args.wait_schedule is not None:
        raise PatternPackageError("--wait-schedule cannot be combined with --wait-policy propose/apply")
    if args.chunk_event_capacity <= 0 or args.ring_slot_count < 2:
        raise PatternPackageError("chunk-event-capacity must be positive and ring-slot-count must be at least 2")
    schedule, proposal = propose_wait_schedule(
        package_dir / "pattern.bin",
        map_path=package_dir / "pattern.map.json",
        chunk_event_capacity=args.chunk_event_capacity,
        ring_slot_count=args.ring_slot_count,
        max_rates=args.max_rates,
        service_time_us=args.service_us,
        service_time_source=args.service_source,
        service_profile_scope=args.service_profile_scope,
        safety_margin_us=args.safety_margin_us,
        max_wait_us=args.max_wait_us,
        max_total_wait_us=args.max_total_wait_us,
        max_wait_count=args.max_wait_count,
        pattern_time_limit_us=args.pattern_time_limit_us,
        forbidden_rate_ids=args.forbid_rate_id,
        forbidden_stil_rate_units=args.forbid_stil_rate,
    )
    schedule_path = output_dir / "wait.proposed.json"
    proposal_path = output_dir / "wait.proposal.json"
    acceptance_path = output_dir / "wait.acceptance.json"
    _write_json(schedule_path, schedule)
    _write_json(proposal_path, proposal)
    acceptance = evaluate_wait_schedule(
        package_dir / "pattern.bin",
        schedule_path,
        map_path=package_dir / "pattern.map.json",
        service_time_us=args.service_us,
        service_time_source=args.service_source,
        safety_margin_us=args.safety_margin_us,
        max_wait_us=args.max_wait_us,
        max_total_wait_us=args.max_total_wait_us,
        max_wait_count=args.max_wait_count,
        pattern_time_limit_us=args.pattern_time_limit_us,
        max_rates=args.max_rates,
    )
    _write_json(acceptance_path, acceptance)
    return schedule, proposal, acceptance


def _build_conversion(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    output_dir = resolve_work_dir(args)
    _empty_output_dir(output_dir)
    if args.wait_policy == "none":
        if args.forbid_rate_id:
            raise PatternPackageError(
                "--forbid-rate-id requires --wait-policy propose or apply"
            )
        package_dir = output_dir / "package"
        package = build_pattern_package(
            _package_options(args, package_dir, wait_schedule=args.wait_schedule)
        )
        report = {
            "kind": CONVERSION_KIND,
            "schema_version": CONVERSION_SCHEMA_VERSION,
            "status": "READY",
            "package_dir": str(package_dir),
            "wait_policy": "none",
            "package": package,
            "physical_execution_authorized": False,
            "device_io_performed": False,
            "work_dir": str(output_dir),
            "next_step": "verify package; review wait sidecars if present; use the package run command only after device approval",
        }
        return report, 0

    base_dir = output_dir / "base-package"
    package = build_pattern_package(_package_options(args, base_dir, wait_schedule=None))
    schedule, proposal, acceptance = _proposal_and_acceptance(args, base_dir, output_dir)
    acceptance_status = acceptance.get("status")
    wait_plan_status = proposal.get("wait_plan", {}).get("status")
    nonzero_waits = sum(
        1
        for item in schedule.get("boundaries", [])
        if isinstance(item, dict) and int(item.get("wait_ticks", 0)) > 0
    )
    final_package_dir: Path | None = base_dir
    application: dict[str, Any] = {"status": "NOT_REQUESTED"}
    return_code = 0
    status = "READY_FOR_REVIEW"
    if args.wait_policy == "apply":
        if acceptance_status == "ACCEPTED" and nonzero_waits:
            final_package_dir = output_dir / "package"
            derived = derive_pattern_package_with_wait(
                base_dir,
                schedule_path=output_dir / "wait.proposed.json",
                acceptance_path=output_dir / "wait.acceptance.json",
                output_dir=final_package_dir,
            )
            application = {"status": "VERIFIED", "package": derived}
            status = "READY"
        elif acceptance_status == "ACCEPTED" and nonzero_waits == 0:
            final_package_dir = output_dir / "package"
            shutil.copytree(base_dir, final_package_dir)
            application = {
                "status": "NOT_REQUIRED",
                "reason": "accepted proposal contains no non-zero waits; base package is the final package",
            }
            status = "READY"
        else:
            final_package_dir = None
            application = {
                "status": "BLOCKED",
                "reason": "wait proposal was not accepted; no derived package was created",
            }
            status = "WAIT_REVIEW"
            return_code = 2
    elif acceptance_status not in {"ACCEPTED", "REQUIRES_SERVICE_PROFILE"}:
        status = "WAIT_REVIEW"
        return_code = 2

    report = {
        "kind": CONVERSION_KIND,
        "schema_version": CONVERSION_SCHEMA_VERSION,
        "status": status,
        "package_dir": str(final_package_dir) if final_package_dir is not None else None,
        "base_package_dir": str(base_dir),
        "wait_policy": args.wait_policy,
        "wait": {
            "schedule": str(output_dir / "wait.proposed.json"),
            "proposal": str(output_dir / "wait.proposal.json"),
            "acceptance": str(output_dir / "wait.acceptance.json"),
            "proposal_status": wait_plan_status,
            "acceptance_status": acceptance_status,
            "nonzero_wait_count": nonzero_waits,
        },
        "application": application,
        "physical_execution_authorized": False,
        "device_io_performed": False,
        "work_dir": str(output_dir),
        "next_step": (
            "review/edit wait.proposed.json, rerun acceptance, then apply explicitly"
            if status != "READY"
            else "verify package before any device operation"
        ),
    }
    return report, return_code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report, return_code = _build_conversion(args)
    except (OSError, PatternPackageError, WaitScheduleError, ValueError) as exc:
        print(json.dumps({"status": "REJECT", "error": str(exc)}, sort_keys=True))
        return 2
    output = args.json_out or resolve_work_dir(args) / "conversion.report.json"
    _write_json(output, report)
    print(json.dumps({"status": report["status"], "package_dir": report.get("package_dir"), "report": str(output)}, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
