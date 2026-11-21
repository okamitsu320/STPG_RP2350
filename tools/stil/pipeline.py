from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from stpg.constants import STPG_DEFAULT_TICK_HZ, STPG_MAX_EVENT_COUNT

from .backend_macro import compile_macro_stream
from .backend_rate import RateBuildResult, ShapingOptions, compile_rate_vector
from .backend_raw import compile_raw_event
from .device_precheck import estimate_direct_execution_fit, inspect_pattern_requirements
from .errors import StilError
from .family_analyze import analyze_micro_op_families, build_micro_op_diagnostics
from .json_artifacts import write_json_artifact
from .mapfile import build_map
from .markers import inject_markers, summarize_marker_specs
from .parser import parse_stil_file
from .prepared_source import PreparedStilSource
from .report import build_compile_report
from .resolve import build_ir_with_scale, load_pin_map, resolve_document, slice_resolved_pattern
from .runtime_capacity import (
    DEFAULT_RUNTIME_PROFILE,
    RuntimeCapacityProfile,
    assess_runtime_capacity,
    get_runtime_capacity_profile,
    runtime_capacity_observation,
    runtime_capacity_contract,
)
from .underflow_analyze import analyze_rate_vector_underflow, build_practical_runtime_hints
from stpg.pattern import rate_vector_structure_metrics, resolve_max_rates


def _raise_first_hard_error(diagnostics: list[dict[str, object]], *, gate_policy: str) -> None:
    if gate_policy != "error":
        return
    for item in diagnostics:
        if item.get("severity") != "error":
            continue
        if str(item.get("code") or "") != "ERR_PRACTICAL_COMPARE24_TIMING":
            continue
        code = str(item.get("code") or "ERR_COMPILE")
        line = item.get("line")
        message = str(item.get("message") or code)
        if line is not None:
            raise StilError(code, f"line {line}: {message}")
        raise StilError(code, message)


@dataclass(frozen=True)
class CompileOptions:
    input_path: Path
    mode: str = "auto"
    tick_hz: int = STPG_DEFAULT_TICK_HZ
    pin_map_path: Path | None = None
    pattern_burst: str | None = None
    pattern: str | None = None
    strict: bool = False
    allow_approx: bool = False
    prefer_raw: bool = False
    until_vector: int | None = None
    vector_range: tuple[int, int] | None = None
    time_scale: Decimal = Decimal("1.0")
    markers: tuple[dict, ...] = ()
    practical_chunk_shaping: bool = False
    practical_chunk_shaping_target_range: tuple[int, int] | None = None
    practical_chunk_shaping_max_run_vectors: int = 64
    practical_gate_policy: str = "error"
    runtime_profile: str | RuntimeCapacityProfile = DEFAULT_RUNTIME_PROFILE
    max_rates: int | None = None
    loop_policy: str = "expand"
    prepared_source: PreparedStilSource | None = field(default=None, repr=False, compare=False)


def _try_select_direct_exec_raw(ir, *, tick_hz: int, allow_approx: bool, family_report: dict) -> tuple[object | None, dict[str, object]]:
    summary = family_report["summary"]
    candidate_info: dict[str, object] = {
        "eligible": False,
        "selected": False,
        "reason": None,
    }
    if getattr(ir, "loop_preserved", False):
        candidate_info["reason"] = "preserved_loop_requires_rate_vector_backend"
        return None, candidate_info
    if summary["unsupported_vectors"] != 0:
        candidate_info["reason"] = "unsupported_micro_op_families_present"
        return None, candidate_info
    if ir.approximate_reasons:
        candidate_info["reason"] = "approximation_required"
        return None, candidate_info
    try:
        raw_result = compile_raw_event(ir, tick_hz=tick_hz, allow_approx=allow_approx)
    except Exception as exc:
        candidate_info["reason"] = str(exc)
        return None, candidate_info
    requirements = inspect_pattern_requirements(raw_result.pattern_bytes)
    direct_fit = estimate_direct_execution_fit(requirements)
    candidate_info.update(
        {
            "eligible": True,
            "supported": direct_fit.supported,
            "estimated_event_count": direct_fit.estimated_event_count,
            "estimated_sample_count": direct_fit.estimated_sample_count,
            "event_limit": direct_fit.event_limit,
            "sample_limit": direct_fit.sample_limit,
            "reason": direct_fit.reason,
        }
    )
    if not direct_fit.supported:
        return None, candidate_info
    candidate_info["selected"] = True
    candidate_info["reason"] = "supported_families_exact_and_fit"
    return raw_result, candidate_info


def _compile_prepared_rate_vector(ir, *, prepared_source: PreparedStilSource | None, **kwargs) -> RateBuildResult:
    if prepared_source is not None:
        return prepared_source._rate_vector(ir, compiler=compile_rate_vector, **kwargs)
    return compile_rate_vector(ir, **kwargs)


def compile_stil(options: CompileOptions) -> tuple[bytes, dict, dict]:
    result = _compile_stil_impl(options, binary_only=False)
    assert isinstance(result, tuple)
    return result


@dataclass(frozen=True)
class _CompiledBinary:
    payload: bytes
    strategy: str
    rate_waveform_tables: tuple[str, ...] = ()


def _compile_stil_binary_probe(options: CompileOptions) -> tuple[bytes, str]:
    """Encode a capacity candidate, not a fully analyzed/admitted package.

    The planner separately inspects the binary and checks runtime capacities.
    Complete compilation and independent disk verification still run when
    the selected segment is packaged. Report-only policy is intentional:
    omitted map/report diagnostics cannot authorize a normal compile.
    """

    if options.practical_gate_policy != "report":
        raise ValueError("binary capacity probes require practical_gate_policy=report")
    result = _compile_stil_impl(options, binary_only=True)
    assert isinstance(result, _CompiledBinary)
    return result.payload, result.strategy


def _compile_stil_wait_probe(options: CompileOptions) -> _CompiledBinary:
    """Artifactless rate-vector probe with the encoder's source-rate binding."""
    if options.practical_gate_policy != "report" or options.mode != "rate-vector" or options.prefer_raw:
        raise ValueError("wait capacity probes require rate-vector and practical_gate_policy=report")
    result = _compile_stil_impl(options, binary_only=True)
    assert isinstance(result, _CompiledBinary)
    if not result.rate_waveform_tables:
        raise ValueError("wait capacity probe has no emitted source-rate binding")
    return result


def _compile_stil_impl(options: CompileOptions, *, binary_only: bool) -> tuple[bytes, dict, dict] | _CompiledBinary:
    if options.prepared_source is not None:
        options.prepared_source.validate_binding(
            options.input_path, pin_map_path=options.pin_map_path,
            pattern_burst=options.pattern_burst, pattern=options.pattern, loop_policy=options.loop_policy)
        selection = (0, options.until_vector) if options.until_vector is not None else options.vector_range
        resolved = options.prepared_source.resolve(selection)
        document = resolved.document
    else:
        document = parse_stil_file(options.input_path, loop_policy=options.loop_policy)
        pin_map = load_pin_map(options.pin_map_path, document=document)
        resolved = resolve_document(
            document,
            pin_map=pin_map,
            pattern_burst_name=options.pattern_burst,
            pattern_name=options.pattern,
            loop_policy=options.loop_policy,
        )
        if options.until_vector is not None:
            resolved = slice_resolved_pattern(resolved, start=0, end=options.until_vector)
        elif options.vector_range is not None:
            resolved = slice_resolved_pattern(resolved, start=options.vector_range[0], end=options.vector_range[1])
    ir = (
        options.prepared_source._compiler_ir(resolved, tick_hz=options.tick_hz, time_scale=options.time_scale)
        if options.prepared_source is not None
        else build_ir_with_scale(resolved, tick_hz=options.tick_hz, time_scale=options.time_scale)
    )
    ir = inject_markers(ir, options.markers)
    if ir.loop_preserved and (
        options.prefer_raw or options.mode in {"raw-event", "macro-stream"}
    ):
        raise StilError(
            "ERR_LOOP_BACKEND_UNSUPPORTED",
            "loop-policy=preserve requires mode=rate-vector or mode=auto; raw-event and macro-stream cannot retain loop structure",
        )
    fallback_reasons: list[str] = []
    # Auto's raw-direct selection depends on this analysis. Explicit binary
    # probes need only encoding; a full compile always emits the analysis.
    family_report = (
        None
        if binary_only and (options.prefer_raw or options.mode in {"rate-vector", "raw-event", "macro-stream"})
        else analyze_micro_op_families(ir)
    )

    shaping = ShapingOptions(
        enabled=options.practical_chunk_shaping,
        target_global_range=options.practical_chunk_shaping_target_range,
        max_run_vectors=options.practical_chunk_shaping_max_run_vectors,
    )
    direct_exec_candidate: dict[str, object] = {
        "eligible": False,
        "selected": False,
        "reason": "not_evaluated",
    }
    compile_strategy = "unknown"
    rate_result: RateBuildResult | None = None

    if options.prefer_raw:
        raw_result = compile_raw_event(ir, tick_hz=options.tick_hz, allow_approx=options.allow_approx)
        mode_emitted = raw_result.report_mode
        payload = raw_result.pattern_bytes
        rate_count = 0
        slot_count = 0
        vector_count = raw_result.vector_count
        compile_strategy = "raw-event-explicit"
    elif options.mode == "rate-vector":
        rate_result = _compile_prepared_rate_vector(
            ir,
            prepared_source=options.prepared_source if not options.markers else None,
            tick_hz=options.tick_hz,
            allow_approx=options.allow_approx,
            shaping=shaping,
            max_rates=options.max_rates,
        )
        mode_emitted = rate_result.report_mode
        payload = rate_result.pattern_bytes
        rate_count = rate_result.rate_count
        slot_count = rate_result.slot_count
        vector_count = rate_result.vector_count
        compile_strategy = "rate-vector-explicit"
    elif options.mode == "raw-event":
        raw_result = compile_raw_event(ir, tick_hz=options.tick_hz, allow_approx=options.allow_approx)
        mode_emitted = raw_result.report_mode
        payload = raw_result.pattern_bytes
        rate_count = 0
        slot_count = 0
        vector_count = raw_result.vector_count
        compile_strategy = "raw-event-explicit"
    elif options.mode == "macro-stream":
        macro_result = compile_macro_stream(ir, tick_hz=options.tick_hz)
        mode_emitted = macro_result.report_mode
        payload = macro_result.pattern_bytes
        rate_count = 0
        slot_count = 0
        vector_count = macro_result.vector_count
        compile_strategy = "macro-stream-explicit"
    else:
        assert family_report is not None
        raw_direct_result, direct_exec_candidate = _try_select_direct_exec_raw(
            ir,
            tick_hz=options.tick_hz,
            allow_approx=options.allow_approx,
            family_report=family_report,
        )
        if raw_direct_result is not None:
            mode_emitted = raw_direct_result.report_mode
            payload = raw_direct_result.pattern_bytes
            rate_count = 0
            slot_count = 0
            vector_count = raw_direct_result.vector_count
            compile_strategy = "raw-event-auto-direct-exec"
        else:
            try:
                rate_result = _compile_prepared_rate_vector(
                    ir,
                    prepared_source=options.prepared_source if not options.markers else None,
                    tick_hz=options.tick_hz,
                    allow_approx=options.allow_approx,
                    shaping=shaping,
                    max_rates=options.max_rates,
                )
                mode_emitted = rate_result.report_mode
                payload = rate_result.pattern_bytes
                rate_count = rate_result.rate_count
                slot_count = rate_result.slot_count
                vector_count = rate_result.vector_count
                compile_strategy = "rate-vector-auto"
            except Exception as exc:
                if ir.loop_preserved:
                    raise StilError(
                        "ERR_LOOP_BACKEND_UNSUPPORTED",
                        "preserved loop could not be compiled by the rate-vector backend; refusing raw-event fallback",
                    ) from exc
                if options.strict:
                    raise
                fallback_reasons.append(str(exc))
                raw_result = compile_raw_event(ir, tick_hz=options.tick_hz, allow_approx=options.allow_approx)
                mode_emitted = raw_result.report_mode
                payload = raw_result.pattern_bytes
                rate_count = 0
                slot_count = 0
                vector_count = raw_result.vector_count
                compile_strategy = "raw-event-auto-fallback"

    if binary_only:
        if options.prepared_source is not None:
            options.prepared_source._release_rate_encoding()
        return _CompiledBinary(payload, compile_strategy,
                               rate_result.rate_waveform_tables if rate_result is not None else ())

    assert family_report is not None
    map_data = build_map(
        ir,
        mode_emitted=mode_emitted,
        shaping=shaping if mode_emitted == "rate-vector" else None,
        pattern_bytes=payload,
        _rate_build_result=rate_result if mode_emitted == "rate-vector" else None,
    )
    rate_shaping_segments = (
        rate_result.practical_chunk_shaping_segments if rate_result is not None else ()
    )
    # Do not extend the encoding's source-group lifetime into report/wait work.
    del rate_result
    if options.prepared_source is not None:
        options.prepared_source._release_rate_encoding()
    requirements = inspect_pattern_requirements(payload)
    runtime_profile = (
        get_runtime_capacity_profile(options.runtime_profile)
        if isinstance(options.runtime_profile, str)
        else options.runtime_profile
    )
    capacity_diagnostics = assess_runtime_capacity(
        requirements,
        pattern_size_bytes=len(payload),
        profile=runtime_profile,
    )
    capacity_observation = runtime_capacity_observation(
        requirements,
        pattern_size_bytes=len(payload),
        profile=runtime_profile,
    )
    practical_hints: dict[str, object] = {}
    if mode_emitted == "rate-vector":
        practical_report = analyze_rate_vector_underflow(payload, map_data=map_data)
        practical_hints = build_practical_runtime_hints(practical_report)
    report_diagnostics = [
        {
            "severity": diagnostic.severity,
            "code": diagnostic.code,
            "message": diagnostic.message,
            "line": diagnostic.line,
            "column": diagnostic.column,
            "context": diagnostic.context,
        }
        for diagnostic in document.diagnostics
    ]
    report_diagnostics.extend(
        {
            "severity": issue["severity"],
            "code": issue["code"],
            "message": issue["message"],
            "line": None,
            "column": None,
            "context": issue.get("context"),
        }
        for issue in capacity_diagnostics
    )
    for item in practical_hints.get("practical_chunk_warnings", []):
        report_diagnostics.append(
            {
                "severity": "warning",
                "code": item["code"],
                "message": item["message"],
                "line": item["line"],
                "column": item["column"],
                "context": item["context"],
            }
        )
    for item in practical_hints.get("practical_run_warnings", []):
        report_diagnostics.append(
            {
                "severity": "warning",
                "code": item["code"],
                "message": item["message"],
                "line": item["line"],
                "column": item["column"],
                "context": item["context"],
            }
        )
    for item in practical_hints.get("practical_hard_errors", []):
        report_diagnostics.append(
            {
                "severity": "error",
                "code": item["code"],
                "message": item["message"],
                "line": item["line"],
                "column": item["column"],
                "context": item["context"],
            }
        )
    report_diagnostics.extend(build_micro_op_diagnostics(family_report))
    _raise_first_hard_error(report_diagnostics, gate_policy=options.practical_gate_policy)
    if options.practical_chunk_shaping and mode_emitted == "rate-vector":
        report_diagnostics.append(
            {
                "severity": "note",
                "code": "NOTE_PRACTICAL_CHUNK_SHAPING_APPLIED",
                "message": "practical chunk shaping split long homogeneous runs near the target global vector range",
                "line": None,
                "column": None,
                "context": {
                    "target_global_range": list(options.practical_chunk_shaping_target_range)
                    if options.practical_chunk_shaping_target_range is not None
                    else None,
                    "max_run_vectors": options.practical_chunk_shaping_max_run_vectors,
                    "segment_count": len(rate_shaping_segments),
                },
            }
        )
    direct_exec_fit = estimate_direct_execution_fit(requirements)
    structure_metrics = {}
    if mode_emitted == "rate-vector":
        structure_metrics = rate_vector_structure_metrics(payload)
    report_data = build_compile_report(
        ir,
        mode_requested="raw-event" if options.prefer_raw else options.mode,
        mode_emitted=mode_emitted,
        rate_count=rate_count,
        slot_count=slot_count,
        vector_count=vector_count,
        fallback_reasons=fallback_reasons,
        requested_time_scale=str(options.time_scale),
        tick_hz=options.tick_hz,
        markers=summarize_marker_specs(ir.markers),
        terminal_drive_value=ir.terminal_drive_value,
        terminal_drive_enable=ir.terminal_drive_enable,
        micro_op_family_analysis=family_report,
        runtime_precheck={
            "estimated_event_count": requirements.estimated_event_count,
            "estimated_sample_count": direct_exec_fit.estimated_sample_count,
            "estimated_compare_count": requirements.estimated_compare_count,
            "non_streaming_event_limit": runtime_profile.full_expansion_event_limit
            if requirements.mode == "rate-vector"
            else len(ir.vectors),
            "runtime_capacity": runtime_capacity_contract(runtime_profile),
            "runtime_capacity_observation": capacity_observation,
            "runtime_capacity_diagnostics": capacity_diagnostics,
            "compile_strategy": compile_strategy,
            "direct_execution_candidate": direct_exec_candidate,
            "direct_execution_fit": {
                "supported": direct_exec_fit.supported,
                "estimated_event_count": direct_exec_fit.estimated_event_count,
                "estimated_sample_count": direct_exec_fit.estimated_sample_count,
                "estimated_compare_count": requirements.estimated_compare_count,
                "event_limit": direct_exec_fit.event_limit,
                "sample_limit": direct_exec_fit.sample_limit,
                "reason": direct_exec_fit.reason,
            },
            "streaming_poc_compatible": requirements.streaming_poc_incompatibility is None,
            "streaming_poc_incompatibility": requirements.streaming_poc_incompatibility,
            "streaming_poc_timing_issues": list(requirements.streaming_poc_timing_issues),
            "streaming_poc_required_drive_gap_ticks": 10,
            "streaming_poc_required_sample_gap_ticks": 11,
            "reduced_density_candidate": requirements.reduced_density_candidate,
            "reduced_density_incompatibilities": list(requirements.reduced_density_incompatibilities),
            "exact_density_opportunities": list(requirements.exact_density_opportunities),
            "micro_op_family_summary": family_report["summary"],
            "micro_op_unsupported_examples": family_report["unsupported_examples"],
            "micro_op_family_examples": family_report["family_examples"],
            "practical_chunk_windows": practical_hints.get("top_chunk_windows", []),
            "practical_generator_avoidance_candidates": practical_hints.get("generator_avoidance_candidates", []),
            "practical_local_wait_candidates": practical_hints.get("local_wait_candidates", []),
            "practical_hard_errors": practical_hints.get("practical_hard_errors", []),
            "practical_chunk_shaping_applied": bool(
                options.practical_chunk_shaping and mode_emitted == "rate-vector"
            ),
            "practical_gate_policy": options.practical_gate_policy,
            "practical_chunk_shaping_policy": None
            if not options.practical_chunk_shaping
            else {
                "target_global_range": list(options.practical_chunk_shaping_target_range)
                if options.practical_chunk_shaping_target_range is not None
                else None,
                "max_run_vectors": options.practical_chunk_shaping_max_run_vectors,
            },
            "practical_chunk_shaping_segments": []
            if not options.practical_chunk_shaping or mode_emitted != "rate-vector"
            else list(rate_shaping_segments),
        },
        diagnostics=report_diagnostics,
        pin_map_path=options.pin_map_path,
        structure_metrics=structure_metrics,
    )
    return payload, map_data, report_data


def write_compile_outputs(
    output: Path,
    payload: bytes,
    map_data: dict,
    report_data: dict,
    *,
    json_compression: str = "none",
) -> tuple[Path, Path]:
    output.write_bytes(payload)
    map_path = write_json_artifact(
        output.with_suffix(".map.json"),
        map_data,
        compression=json_compression,
        indent=None if json_compression != "none" else 2,
        sort_keys=False,
    )
    report_path = write_json_artifact(
        output.with_suffix(".compile_report.json"),
        report_data,
        compression=json_compression,
        indent=None if json_compression != "none" else 2,
        sort_keys=False,
    )
    return map_path, report_path
