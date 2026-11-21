"""Create, inspect, edit, and validate human-editable wait schedules."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .wait_schedule import (
    DEFAULT_CHUNK_EVENT_CAPACITY,
    DEFAULT_RING_SLOT_COUNT,
    WaitScheduleError,
    build_wait_schedule,
    render_wait_schedule_text,
    set_wait,
    set_wait_range,
    set_wait_periodic,
    validate_wait_schedule,
    propose_wait_schedule,
)
from .wait_binary import WaitBinaryError, apply_wait_schedule_files, inspect_wait_schedule_encoding
from .json_artifacts import read_json_artifact, write_json_artifact
from .wait_acceptance import evaluate_wait_schedule


def _write_json(path: Path, value: dict) -> Path:
    # Standalone schedules are deliberately human-editable.  Package builds
    # compress generated sidecars automatically; this CLI preserves the
    # requested path unless a future explicit compression option is selected.
    return write_json_artifact(path, value, compression="none", sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a zero-wait boundary template")
    create.add_argument("pattern", type=Path)
    create.add_argument("-o", "--output", type=Path, required=True)
    create.add_argument("--map", dest="map_path", type=Path)
    create.add_argument("--chunk-event-capacity", type=int, default=DEFAULT_CHUNK_EVENT_CAPACITY)
    create.add_argument("--ring-slot-count", type=int, default=DEFAULT_RING_SLOT_COUNT)
    create.add_argument("--max-rates", type=int)
    create.add_argument("--forbid-rate-id", action="append", type=int, default=[])
    create.add_argument(
        "--forbid-stil-rate",
        action="append",
        default=[],
        metavar="WAVEFORM_TABLE",
        help=(
            "forbid a non-zero wait only when the next chunk's first event "
            "uses this STIL WaveformTable"
        ),
    )
    create.add_argument("--note", action="append", default=[])

    show = subparsers.add_parser("show", help="render a schedule boundary table")
    show.add_argument("schedule", type=Path)

    validate = subparsers.add_parser("validate", help="validate identity and boundary geometry")
    validate.add_argument("pattern", type=Path)
    validate.add_argument("schedule", type=Path)
    validate.add_argument("--map", dest="map_path", type=Path)
    validate.add_argument("--json-out", type=Path)
    validate.add_argument("--chunk-event-capacity", type=int)
    validate.add_argument("--ring-slot-count", type=int)
    validate.add_argument("--max-rates", type=int)

    edit = subparsers.add_parser("set", help="set exactly one boundary wait and write a new schedule")
    edit.add_argument("pattern", type=Path)
    edit.add_argument("schedule", type=Path)
    edit.add_argument("--map", dest="map_path", type=Path)
    edit.add_argument("--max-rates", type=int)
    edit.add_argument("--before-chunk", type=int, required=True)
    wait_group = edit.add_mutually_exclusive_group(required=True)
    wait_group.add_argument("--wait-ticks", type=int)
    wait_group.add_argument("--wait-us")
    edit.add_argument("--reason", required=True)
    edit.add_argument("-o", "--output", type=Path, required=True)

    edit_range = subparsers.add_parser(
        "set-range", help="set one wait value over an inclusive boundary range"
    )
    edit_range.add_argument("pattern", type=Path)
    edit_range.add_argument("schedule", type=Path)
    edit_range.add_argument("--map", dest="map_path", type=Path)
    edit_range.add_argument("--max-rates", type=int)
    edit_range.add_argument("--from-chunk", dest="first_chunk", type=int, required=True)
    edit_range.add_argument("--to-chunk", dest="last_chunk", type=int)
    range_wait_group = edit_range.add_mutually_exclusive_group(required=True)
    range_wait_group.add_argument("--wait-ticks", type=int)
    range_wait_group.add_argument("--wait-us")
    edit_range.add_argument("--reason", required=True)
    edit_range.add_argument("-o", "--output", type=Path, required=True)

    edit_periodic = subparsers.add_parser(
        "set-periodic", help="set one wait on periodically selected boundaries"
    )
    edit_periodic.add_argument("pattern", type=Path)
    edit_periodic.add_argument("schedule", type=Path)
    edit_periodic.add_argument("--map", dest="map_path", type=Path)
    edit_periodic.add_argument("--max-rates", type=int)
    edit_periodic.add_argument("--from-chunk", dest="first_chunk", type=int, required=True)
    edit_periodic.add_argument("--to-chunk", dest="last_chunk", type=int)
    edit_periodic.add_argument("--every", dest="period", type=int, required=True)
    edit_periodic.add_argument("--phase", type=int, default=0)
    periodic_wait_group = edit_periodic.add_mutually_exclusive_group(required=True)
    periodic_wait_group.add_argument("--wait-ticks", type=int)
    periodic_wait_group.add_argument("--wait-us")
    edit_periodic.add_argument("--reason", required=True)
    edit_periodic.add_argument("-o", "--output", type=Path, required=True)

    encoding = subparsers.add_parser(
        "encoding-check", help="check whether waits fit the device rate-vector budget"
    )
    encoding.add_argument("pattern", type=Path)
    encoding.add_argument("schedule", type=Path)
    encoding.add_argument("--map", dest="map_path", type=Path)
    encoding.add_argument("--max-rates", type=int)
    encoding.add_argument("--json-out", type=Path, required=True)

    propose = subparsers.add_parser(
        "propose", help="write an editable schedule from the bounded host-only model"
    )
    propose.add_argument("pattern", type=Path)
    propose.add_argument("-o", "--output", type=Path, required=True)
    propose.add_argument("--map", dest="map_path", type=Path)
    propose.add_argument("--chunk-event-capacity", type=int, default=DEFAULT_CHUNK_EVENT_CAPACITY)
    propose.add_argument("--ring-slot-count", type=int, default=DEFAULT_RING_SLOT_COUNT)
    propose.add_argument("--max-rates", type=int)
    propose.add_argument("--service-us", type=float, required=True)
    propose.add_argument("--service-source", required=True)
    propose.add_argument(
        "--service-profile-scope",
        choices=("non-loop", "loop-generic"),
        default="non-loop",
        help="bind the declared service budget to a non-loop or preserved-Loop artifact",
    )
    propose.add_argument("--safety-margin-us", type=float, default=0.0)
    propose.add_argument("--max-wait-us", type=float, required=True)
    propose.add_argument("--max-total-wait-us", type=float, required=True)
    propose.add_argument("--max-wait-count", type=int)
    propose.add_argument("--pattern-time-limit-us", type=float)
    propose.add_argument(
        "--wait-policy",
        choices=("pressure", "every-boundary"),
        default="pressure",
        help=(
            "pressure=place waits only at modeled pressure points; "
            "every-boundary=place the configured wait at every eligible boundary"
        ),
    )
    propose.add_argument("--forbid-rate-id", action="append", type=int, default=[])
    propose.add_argument(
        "--forbid-stil-rate",
        action="append",
        default=[],
        metavar="WAVEFORM_TABLE",
        help=(
            "forbid a non-zero wait only when the next chunk's first event "
            "uses this STIL WaveformTable"
        ),
    )
    propose.add_argument("--json-out", type=Path, required=True, help="write the full model proposal report")
    propose.add_argument(
        "--acceptance-out",
        type=Path,
        help="also evaluate the generated proposal and write the acceptance report",
    )

    apply = subparsers.add_parser(
        "apply", help="apply an ACCEPTED schedule to a new derived rate-vector artifact bundle"
    )
    apply.add_argument("pattern", type=Path)
    apply.add_argument("schedule", type=Path)
    apply.add_argument("--map", dest="map_path", type=Path, required=True)
    apply.add_argument("--acceptance", type=Path, required=True)
    apply.add_argument("--output-dir", type=Path, required=True)
    apply.add_argument("--max-rates", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            schedule = build_wait_schedule(
                args.pattern,
                map_path=args.map_path,
                chunk_event_capacity=args.chunk_event_capacity,
                ring_slot_count=args.ring_slot_count,
                max_rates=args.max_rates,
                forbidden_rate_ids=args.forbid_rate_id,
                forbidden_stil_rate_units=args.forbid_stil_rate,
                notes=args.note,
            )
            written = _write_json(args.output, schedule)
            print(f"WROTE {written}")
            return 0
        if args.command == "show":
            schedule = read_json_artifact(args.schedule)
            print(render_wait_schedule_text(schedule))
            return 0
        if args.command == "validate":
            report = validate_wait_schedule(
                args.pattern,
                args.schedule,
                map_path=args.map_path,
                expected_chunk_event_capacity=args.chunk_event_capacity,
                expected_ring_slot_count=args.ring_slot_count,
                max_rates=args.max_rates,
            )
            if args.json_out:
                _write_json(args.json_out, report)
            print(json.dumps({"status": report["status"], "errors": len(report["errors"])}, sort_keys=True))
            return 0 if report["status"] == "PASS" else 2
        if args.command == "set":
            updated = set_wait(
                args.pattern,
                args.schedule,
                map_path=args.map_path,
                before_chunk_index=args.before_chunk,
                max_rates=args.max_rates,
                wait_ticks=args.wait_ticks,
                wait_us=args.wait_us,
                reason=args.reason,
                output_path=args.output,
            )
            print(f"WROTE {args.output} wait_ticks={updated['boundaries'][args.before_chunk]['wait_ticks']}")
            return 0
        if args.command == "set-range":
            updated = set_wait_range(
                args.pattern,
                args.schedule,
                map_path=args.map_path,
                first_chunk_index=args.first_chunk,
                last_chunk_index=args.last_chunk,
                max_rates=args.max_rates,
                wait_ticks=args.wait_ticks,
                wait_us=args.wait_us,
                reason=args.reason,
                output_path=args.output,
            )
            first = args.first_chunk
            last = args.last_chunk if args.last_chunk is not None else len(updated["boundaries"]) - 1
            print(
                f"WROTE {args.output} range={first}:{last} "
                f"wait_ticks={updated['boundaries'][first]['wait_ticks']}"
            )
            return 0
        if args.command == "set-periodic":
            updated = set_wait_periodic(
                args.pattern,
                args.schedule,
                map_path=args.map_path,
                first_chunk_index=args.first_chunk,
                last_chunk_index=args.last_chunk,
                max_rates=args.max_rates,
                period=args.period,
                phase=args.phase,
                wait_ticks=args.wait_ticks,
                wait_us=args.wait_us,
                reason=args.reason,
                output_path=args.output,
            )
            history = updated["edit_history"][-1]
            print(
                f"WROTE {args.output} selected={history['boundary_count']} "
                f"wait_ticks={history['new_wait_ticks']} every={history['period']} phase={history['phase']}"
            )
            return 0
        if args.command == "encoding-check":
            validation = validate_wait_schedule(
                args.pattern,
                args.schedule,
                map_path=args.map_path,
                max_rates=args.max_rates,
            )
            if validation.get("status") != "PASS":
                report = {
                    "kind": "stpg_wait_binary_encoding_check",
                    "schema_version": 1,
                    "status": "REJECT",
                    "errors": ["schedule validation failed before encoding inspection"],
                    "validation": validation,
                    "physical_execution_authorized": False,
                }
            else:
                schedule = read_json_artifact(args.schedule)
                report = inspect_wait_schedule_encoding(
                    args.pattern.read_bytes(),
                    schedule=schedule,
                    max_rates=args.max_rates,
                )
                report["validation"] = validation
            _write_json(args.json_out, report)
            print(json.dumps({"status": report["status"], "errors": len(report.get("errors", []))}, sort_keys=True))
            return 0 if report["status"] == "PASS" else 2
        if args.command == "propose":
            schedule, model_report = propose_wait_schedule(
                args.pattern,
                map_path=args.map_path,
                chunk_event_capacity=args.chunk_event_capacity,
                ring_slot_count=args.ring_slot_count,
                max_rates=args.max_rates,
                service_time_us=args.service_us,
                service_time_source=args.service_source,
                safety_margin_us=args.safety_margin_us,
                max_wait_us=args.max_wait_us,
                max_total_wait_us=args.max_total_wait_us,
                max_wait_count=args.max_wait_count,
                pattern_time_limit_us=args.pattern_time_limit_us,
                forbidden_rate_ids=args.forbid_rate_id,
                forbidden_stil_rate_units=args.forbid_stil_rate,
                wait_policy=args.wait_policy,
                service_profile_scope=args.service_profile_scope,
            )
            _write_json(args.output, schedule)
            _write_json(args.json_out, model_report)
            status = model_report.get("wait_plan", {}).get("status")
            acceptance_status = None
            if args.acceptance_out is not None:
                acceptance = evaluate_wait_schedule(
                    args.pattern,
                    args.output,
                    map_path=args.map_path,
                    service_time_us=args.service_us,
                    service_time_source=args.service_source,
                    safety_margin_us=args.safety_margin_us,
                    max_wait_us=args.max_wait_us,
                    max_total_wait_us=args.max_total_wait_us,
                    max_wait_count=args.max_wait_count,
                    pattern_time_limit_us=args.pattern_time_limit_us,
                    max_rates=args.max_rates,
                    service_profile_scope=args.service_profile_scope,
                )
                _write_json(args.acceptance_out, acceptance)
                acceptance_status = acceptance.get("status")
            print(json.dumps({
                "status": status,
                "acceptance_status": acceptance_status,
                "schedule": str(args.output),
                "report": str(args.json_out),
                "acceptance": str(args.acceptance_out) if args.acceptance_out is not None else None,
            }, sort_keys=True))
            if status not in {"planned", "no_wait_needed"}:
                return 2
            return 0 if acceptance_status in {None, "ACCEPTED"} else 2
        if args.command == "apply":
            report = apply_wait_schedule_files(
                args.pattern,
                args.schedule,
                map_path=args.map_path,
                acceptance_path=args.acceptance,
                output_dir=args.output_dir,
                max_rates=args.max_rates,
            )
            print(json.dumps({"status": report["status"], "output_dir": str(args.output_dir)}, sort_keys=True))
            return 0 if report["status"] in {"VERIFIED", "NOT_REQUIRED"} else 2
    except (OSError, WaitScheduleError, WaitBinaryError, json.JSONDecodeError) as exc:
        print(f"REJECT {exc}")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
