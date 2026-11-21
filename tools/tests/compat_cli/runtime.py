from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from stpg.client import StpgClient
from stpg.constants import RUN_FLAG_FAIL_STOP, RUN_FLAG_STREAMING_POC, STORAGE_FLASH, STORAGE_PSRAM, STORAGE_SRAM
from stpg.constants import STATE_DONE, STATE_ERROR, STPG_FAIL_LOG_MAX
from stpg.pattern import ExecEvent, pack_pattern
from stpg.transport import SerialTransport

from stil.device_precheck import (
    estimate_direct_execution_fit,
    inspect_pattern_requirements,
    summarize_runtime_precheck,
    validate_device_compatibility,
)
from stil.markers import parse_marker_specs
from stil.pipeline import CompileOptions, compile_stil, write_compile_outputs
from stil.report import render_fail_report_text
from stil.runtime_capacity import DEFAULT_RUNTIME_PROFILE, RUNTIME_CAPACITY_PROFILES, get_runtime_capacity_profile
from stpg.constants import STPG_DEFAULT_TICK_HZ


@dataclass(frozen=True)
class RunRecord:
    scale: str
    repeat_index: int
    state: int
    error_code: int
    sample_count: int
    fail_count: int
    result_flags: int
    mode_emitted: str
    run_path: str
    output_file: str
    run_index: int = 0
    elapsed_ms: float = 0.0
    execution_class: str = "unknown"
    compare_result: str = "not_available"
    policy_action: str = "unknown"
    coarse_fail_signal: str = "not_requested"
    error_detail: str | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile STIL and run repeat/window/sweep workflows")
    parser.add_argument("input", type=Path)
    parser.add_argument("--port", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("auto", "rate-vector", "raw-event", "macro-stream"), default="auto")
    parser.add_argument("--tick-hz", type=int, default=STPG_DEFAULT_TICK_HZ)
    parser.add_argument("--pin-map", type=Path)
    parser.add_argument("--pattern-burst")
    parser.add_argument("--pattern")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--allow-approx", action="store_true")
    parser.add_argument("--prefer-raw", action="store_true")
    parser.add_argument("--until-vector", type=int)
    parser.add_argument("--vector-range")
    parser.add_argument("--repeat", type=int, default=1, help="number of runs per timing scale (default: 1)")
    parser.add_argument(
        "--abort-after-run-fail",
        "--stop-on-first-fail",
        dest="abort_after_run_fail",
        action="store_true",
        help=(
            "abort later repeat/sweep/window runs after a completed RUN reports "
            "compare fail; does not stop the current RUN "
            "(--stop-on-first-fail is a deprecated alias)"
        ),
    )
    parser.add_argument("--sweep", help="comma-separated timing scales, e.g. 0.95,1.00,1.05")
    parser.add_argument("--target", choices=("sram", "psram", "flash"), default="sram")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument(
        "--upload-end-timeout",
        type=float,
        help="bounded response-read timeout for UPLOAD_END; defaults to --timeout",
    )
    parser.add_argument(
        "--run-response-timeout",
        type=float,
        help="bounded response-read timeout for RUN; defaults to --timeout",
    )
    parser.add_argument(
        "--terminal-status-response-timeout",
        type=float,
        help="bounded response-read timeout for each terminal STATUS; defaults to --timeout",
    )
    parser.add_argument(
        "--wait-terminal",
        type=float,
        default=20.0,
        help="seconds to poll for a terminal state after RUN (default: 20)",
    )
    parser.add_argument("--chunk-size", type=int, default=4096)
    parser.add_argument("--streaming-poc", action="store_true")
    parser.add_argument(
        "--fail-stop",
        action="store_true",
        help="request best-effort same-RUN compare fail stop on compatible firmware",
    )
    parser.add_argument(
        "--fail-stop-limit",
        type=int,
        default=0,
        help="stop on the Nth compare mismatch (0 means one when --fail-stop is enabled)",
    )
    parser.add_argument("--summary-out", type=Path)
    parser.add_argument("--fail-report-out", type=Path)
    parser.add_argument("--marker", action="append", default=[])
    parser.add_argument("--marker-clear", action="append", default=[])
    parser.add_argument(
        "--coarse-fail-signal",
        help="optional mapped signal asserted after a compare-failing run",
    )
    parser.add_argument(
        "--runtime-profile",
        choices=tuple(sorted(RUNTIME_CAPACITY_PROFILES)),
        default=DEFAULT_RUNTIME_PROFILE,
        help="firmware capacity contract used for local preflight",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _validate_runtime_arguments(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    vector_range = None
    if args.vector_range is not None:
        try:
            start_text, end_text = args.vector_range.split(":", 1)
            vector_range = (int(start_text), int(end_text))
        except (ValueError, TypeError) as exc:
            raise SystemExit("--vector-range must be START:END with integer bounds") from exc
    scales = _parse_scales(args.sweep)
    marker_specs = parse_marker_specs(args.marker, args.marker_clear)
    runtime_profile = get_runtime_capacity_profile(args.runtime_profile)
    records: list[RunRecord] = []
    aggregated_fail_report: list[str] = []
    planned_runs = len(scales) * args.repeat
    run_index = 0
    stop_reason = "completed"

    with SerialTransport(args.port, timeout=args.timeout) as transport:
        client = StpgClient(transport)
        device_clock = client.clock_info()
        for scale in scales:
            scale_tag = str(scale).replace(".", "p")
            output = args.output_dir / f"{args.input.stem}_scale_{scale_tag}.bin"
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
                    time_scale=scale,
                    markers=marker_specs,
                    runtime_profile=runtime_profile,
                )
            )
            requirements = inspect_pattern_requirements(payload)
            storage_target = {
                "sram": STORAGE_SRAM,
                "psram": STORAGE_PSRAM,
                "flash": STORAGE_FLASH,
            }[args.target]
            selected_run_flags, run_path = _select_run_flags(
                requirements,
                storage_target=storage_target,
                streaming_poc_requested=args.streaming_poc,
            )
            if args.fail_stop:
                selected_run_flags |= RUN_FLAG_FAIL_STOP
                run_path = f"{run_path}+firmware-fail-stop"
            write_compile_outputs(output, payload, map_data, report_data)
            try:
                validate_device_compatibility(
                    requirements,
                    # GET_CLOCK_INFO encodes tick_hz in the response's
                    # pattern_size field for protocol compatibility.
                    device_clock_tick_hz=device_clock.pattern_size,
                    device_active_pins=device_clock.active_pins,
                    device_capability=device_clock.capability,
                    storage_target=storage_target,
                    run_flags=selected_run_flags,
                    pattern_size_bytes=len(payload),
                    runtime_profile=runtime_profile,
                )
            except RuntimeError as exc:
                raise SystemExit(
                    f"{exc}\ncompile_report={output.with_suffix('.compile_report.json')}\n"
                    f"{summarize_runtime_precheck(requirements, pattern_size_bytes=len(payload), runtime_profile=runtime_profile)}"
                ) from exc
            for repeat_index in range(args.repeat):
                started = time.monotonic()
                try:
                    status = _run_pattern(
                        client,
                        payload,
                        target=args.target,
                        chunk_size=args.chunk_size,
                        run_flags=selected_run_flags,
                        fail_stop_limit=args.fail_stop_limit if args.fail_stop else 0,
                        upload_end_timeout=args.upload_end_timeout,
                        run_response_timeout=args.run_response_timeout,
                        terminal_status_response_timeout=args.terminal_status_response_timeout,
                        terminal_timeout=args.wait_terminal,
                    )
                except Exception as exc:
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                    records.append(
                        _build_error_record(
                            scale=scale,
                            repeat_index=repeat_index,
                            run_index=run_index,
                            mode_emitted=report_data["mode_emitted"],
                            run_path=run_path,
                            output_file=output,
                            elapsed_ms=elapsed_ms,
                            detail=str(exc),
                        )
                    )
                    stop_reason = "execution_error"
                    _write_runtime_outputs(
                        records,
                        aggregated_fail_report,
                        args,
                        planned_runs=planned_runs,
                        stop_reason=stop_reason,
                    )
                    print(f"runtime execution error at run_index={run_index}: {exc}")
                    return 2

                elapsed_ms = (time.monotonic() - started) * 1000.0
                execution_class, compare_result = _classify_terminal_status(status)
                coarse_signal = "not_requested"
                error_detail = None
                if execution_class == "completed" and status.fail_count > 0:
                    try:
                        entries = client.read_fail_log(0, min(status.fail_count, STPG_FAIL_LOG_MAX))
                        aggregated_fail_report.append(
                            render_fail_report_text(entries, map_data, pattern_bytes=payload)
                        )
                        if args.coarse_fail_signal:
                            _emit_fail_indication(
                                client,
                                report_data,
                                map_data,
                                args.coarse_fail_signal,
                                asserted=True,
                                chunk_size=args.chunk_size,
                            )
                            coarse_signal = "asserted_after_run"
                    except Exception as exc:
                        execution_class = "reporting_error"
                        compare_result = "not_available"
                        error_detail = str(exc)
                        stop_reason = "reporting_error"
                elif execution_class == "completed" and args.coarse_fail_signal:
                    # A reset is performed before every run.  A passing run
                    # therefore leaves the coarse signal unasserted without
                    # adding another GPIO-changing mini-run.
                    coarse_signal = "not_asserted_reset_before_run"

                has_later_run = run_index + 1 < planned_runs
                policy_action = _policy_action(
                    execution_class=execution_class,
                    compare_result=compare_result,
                    abort_after_run_fail=args.abort_after_run_fail,
                    has_later_run=has_later_run,
                )
                records.append(
                    RunRecord(
                        scale=str(scale),
                        repeat_index=repeat_index,
                        state=status.state,
                        error_code=status.error_code,
                        sample_count=status.sample_count,
                        fail_count=status.fail_count,
                        result_flags=status.result_flags,
                        mode_emitted=report_data["mode_emitted"],
                        run_path=run_path,
                        output_file=str(output),
                        run_index=run_index,
                        elapsed_ms=round(elapsed_ms, 3),
                        execution_class=execution_class,
                        compare_result=compare_result,
                        policy_action=policy_action,
                        coarse_fail_signal=coarse_signal,
                        error_detail=error_detail,
                    )
                )
                run_index += 1
                if execution_class != "completed":
                    stop_reason = "execution_error" if execution_class == "execution_error" else "reporting_error"
                    _write_runtime_outputs(
                        records,
                        aggregated_fail_report,
                        args,
                        planned_runs=planned_runs,
                        stop_reason=stop_reason,
                    )
                    return 2
                if compare_result == "compare_fail" and args.abort_after_run_fail:
                    stop_reason = "campaign_abort_after_compare_fail"
                    _write_runtime_outputs(
                        records,
                        aggregated_fail_report,
                        args,
                        planned_runs=planned_runs,
                        stop_reason=stop_reason,
                    )
                    return 0
        _write_runtime_outputs(
            records,
            aggregated_fail_report,
            args,
            planned_runs=planned_runs,
            stop_reason=stop_reason,
        )
    return 0


def _validate_runtime_arguments(args: argparse.Namespace) -> None:
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")
    for name in (
        "timeout",
        "upload_end_timeout",
        "run_response_timeout",
        "terminal_status_response_timeout",
        "wait_terminal",
    ):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            option = "--" + name.replace("_", "-")
            raise SystemExit(f"{option} must be a finite positive number")
    if args.chunk_size < 1:
        raise SystemExit("--chunk-size must be positive")
    if args.fail_stop_limit < 0 or args.fail_stop_limit > STPG_FAIL_LOG_MAX:
        raise SystemExit(f"--fail-stop-limit must be in the range 0..{STPG_FAIL_LOG_MAX}")
    if args.fail_stop_limit != 0 and not args.fail_stop:
        raise SystemExit("--fail-stop-limit requires --fail-stop")


def _parse_scales(value: str | None) -> list[Decimal]:
    if not value:
        return [Decimal("1.0")]
    scales: list[Decimal] = []
    for item in value.split(","):
        try:
            scale = Decimal(item.strip())
        except Exception as exc:
            raise SystemExit(f"invalid --sweep scale: {item!r}") from exc
        if not scale.is_finite() or scale <= 0:
            raise SystemExit(f"--sweep scales must be finite and positive: {item!r}")
        scales.append(scale)
    if not scales:
        raise SystemExit("--sweep must contain at least one scale")
    return scales


def _classify_terminal_status(status) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    if status.state == STATE_DONE and status.error_code == 0:
        return "completed", "compare_fail" if status.fail_count > 0 else "pass"
    return "execution_error", "not_available"


def _policy_action(
    *,
    execution_class: str,
    compare_result: str,
    abort_after_run_fail: bool,
    has_later_run: bool,
) -> str:
    if execution_class != "completed":
        return "abort_on_execution_error"
    if compare_result == "compare_fail" and abort_after_run_fail:
        return "abort_after_compare_fail"
    if has_later_run:
        return "continue_to_next_run"
    return "complete_no_later_run"


def _build_error_record(
    *,
    scale: Decimal,
    repeat_index: int,
    run_index: int,
    mode_emitted: str,
    run_path: str,
    output_file: Path,
    elapsed_ms: float,
    detail: str,
) -> RunRecord:
    return RunRecord(
        scale=str(scale),
        repeat_index=repeat_index,
        state=-1,
        error_code=-1,
        sample_count=0,
        fail_count=0,
        result_flags=0,
        mode_emitted=mode_emitted,
        run_path=run_path,
        output_file=str(output_file),
        run_index=run_index,
        elapsed_ms=round(elapsed_ms, 3),
        execution_class="execution_error",
        compare_result="not_available",
        policy_action="abort_on_execution_error",
        error_detail=detail,
    )


def _write_runtime_outputs(
    records: list[RunRecord],
    fail_reports: list[str],
    args: argparse.Namespace,
    *,
    planned_runs: int,
    stop_reason: str,
) -> None:
    _write_outputs(
        records,
        fail_reports,
        args.summary_out,
        args.fail_report_out,
        metadata={
            "schema_version": 1,
            "comparison_control_policy": (
                "abort_after_completed_run_fail" if args.abort_after_run_fail else "continue"
            ),
            "campaign_abort_scope": "subsequent_run_or_window",
            "realtime_fail_stop": False,
            "firmware_fail_stop_requested": getattr(args, "fail_stop", False),
            "coarse_fail_signal": args.coarse_fail_signal,
            "planned_runs": planned_runs,
            "total_runs": len(records),
            "pass_runs": sum(record.compare_result == "pass" for record in records),
            "fail_runs": sum(record.compare_result == "compare_fail" for record in records),
            "execution_error_runs": sum(
                record.execution_class in ("execution_error", "reporting_error") for record in records
            ),
            "first_fail_run_index": next(
                (record.run_index for record in records if record.compare_result == "compare_fail"),
                None,
            ),
            "stop_reason": stop_reason,
            "records": [asdict(record) for record in records],
        },
    )


def _select_run_flags(requirements, *, storage_target: int, streaming_poc_requested: bool) -> tuple[int, str]:
    if streaming_poc_requested:
        return RUN_FLAG_STREAMING_POC, "streaming-poc-forced"
    if requirements.mode == "macro-stream":
        return 0, "direct-exec-macro-stream"
    if requirements.mode != "rate-vector":
        return 0, "direct-exec-raw-event"
    if storage_target not in (STORAGE_PSRAM, STORAGE_FLASH):
        return 0, "direct-exec-sram"
    direct_fit = estimate_direct_execution_fit(requirements)
    if direct_fit.supported:
        return 0, "direct-exec-rate-vector"
    if storage_target == STORAGE_PSRAM:
        return RUN_FLAG_STREAMING_POC, "streaming-poc-auto"
    return 0, "direct-exec-rate-vector-oversize"


def _run_pattern(
    client: StpgClient,
    payload: bytes,
    *,
    target: str,
    chunk_size: int,
    run_flags: int,
    upload_end_timeout: float | None = None,
    run_response_timeout: float | None = None,
    terminal_status_response_timeout: float | None = None,
    terminal_timeout: float = 20.0,
    fail_stop_limit: int = 0,
):
    storage_target = {
        "sram": STORAGE_SRAM,
        "psram": STORAGE_PSRAM,
        "flash": STORAGE_FLASH,
    }[target]
    client.reset()
    upload_kwargs = {"storage_target": storage_target, "chunk_size": chunk_size}
    if upload_end_timeout is not None:
        upload_kwargs["end_response_timeout"] = upload_end_timeout
    client.upload(payload, **upload_kwargs)
    run_kwargs = {"run_flags": run_flags}
    if (run_flags & RUN_FLAG_FAIL_STOP) != 0:
        run_kwargs["aux_arg"] = fail_stop_limit
    if run_response_timeout is not None:
        run_kwargs["response_timeout"] = run_response_timeout
    client.run(**run_kwargs)
    return _wait_for_terminal_status(
        client,
        timeout_s=terminal_timeout,
        response_timeout=terminal_status_response_timeout,
    )


def _wait_for_terminal_status(
    client: StpgClient,
    *,
    timeout_s: float,
    response_timeout: float | None = None,
):
    deadline = time.monotonic() + timeout_s
    last_status = None
    while time.monotonic() < deadline:
        status = client.status() if response_timeout is None else client.status(response_timeout=response_timeout)
        last_status = status
        if status.state in (STATE_DONE, STATE_ERROR):
            return status
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for terminal state, last={last_status}")


def _emit_fail_indication(client: StpgClient, report_data: dict, map_data: dict, signal_name: str, *, asserted: bool, chunk_size: int) -> None:
    mini_pattern = _build_state_update_pattern(report_data, map_data, signal_name, asserted=asserted)
    client.upload(mini_pattern, storage_target=STORAGE_SRAM, chunk_size=chunk_size)
    client.run_preserve_gpio_state()
    client.status()


def _build_state_update_pattern(report_data: dict, map_data: dict, signal_name: str, *, asserted: bool) -> bytes:
    signal_map = map_data.get("signal_map", {})
    if signal_name not in signal_map:
        raise RuntimeError(f"coarse fail signal not found in map: {signal_name}")
    payload = signal_map[signal_name]
    bit = int(payload["bit"])
    active_pins = int(report_data["capability_precheck"]["active_pins"])
    if active_pins == 24 and bit == 20:
        raise RuntimeError("logical bit20/GPIO23 cannot be used for externally observed fail indication on current board")
    base_value = int(report_data.get("terminal_drive_value", 0))
    base_enable = int(report_data.get("terminal_drive_enable", 0))
    mask = 1 << bit
    out_value = (base_value | mask) if asserted else (base_value & ~mask)
    out_enable = base_enable | mask
    mini_pattern = pack_pattern(
        [ExecEvent(wait_ticks=16, out_value=out_value, out_enable=out_enable)],
        tick_hz=int(report_data["capability_precheck"].get("tick_hz", STPG_DEFAULT_TICK_HZ)),
        active_pins=active_pins,
    )
    return mini_pattern


def _write_outputs(
    records: list[RunRecord],
    fail_reports: list[str],
    summary_out: Path | None,
    fail_report_out: Path | None,
    *,
    metadata: dict | None = None,
) -> None:
    if summary_out is not None:
        summary_out.parent.mkdir(parents=True, exist_ok=True)
        summary = [asdict(record) for record in records] if metadata is None else metadata
        summary_out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if fail_report_out is not None and fail_reports:
        fail_report_out.parent.mkdir(parents=True, exist_ok=True)
        fail_report_out.write_text("\n\n".join(fail_reports), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
