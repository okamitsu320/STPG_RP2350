"""Source-level STIL analysis shared by the compiler and ``stil.analyze``.

The compiler's compile report describes an emitted pattern.  This module
describes the input before it becomes a pattern so that malformed or
surprising STIL is visible before a wait schedule or a physical run is
considered.  It is deliberately host-only: no output pattern is written and
no device is contacted.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from stpg.constants import STPG_DEFAULT_TICK_HZ, STPG_MAX_EVENT_COUNT, STPG_T_MIN_TICKS
from stpg.pattern import (PatternValidationError, rate_vector_structure_metrics, resolve_max_rates,
                          streaming_poc_timing_issues)

from .backend_macro import compile_macro_stream
from .backend_rate import (compile_rate_vector, _build_rate_tables, _vector_rate_key,
                          _SlotSignatureCache, _is_loop_block_capacity_error)
from .backend_raw import compile_raw_event
from .ast import LoopDecl, ResolvedLoop, VectorDecl
from .device_precheck import inspect_pattern_requirements
from .errors import StilError
from .family_analyze import analyze_micro_op_families, build_micro_op_diagnostics
from .parser import parse_stil_file
from .prepared_source import PreparedStilSource
from .resolve import (
    _time_to_ticks,
    _time_text_to_seconds,
    build_ir_with_scale,
    load_pin_map,
    resolve_document,
    slice_resolved_pattern,
)
from .runtime_capacity import (
    DEFAULT_RUNTIME_PROFILE,
    RuntimeCapacityProfile,
    assess_runtime_capacity,
    get_runtime_capacity_profile,
    render_runtime_capacity_observation,
    runtime_capacity_observation,
    runtime_capacity_contract,
)


ANALYSIS_KIND = "stpg_stil_source_analysis"
ANALYSIS_VERSION = "v2"
_KNOWN_ACTIONS = {"Drive", "Compare", "DirZ", "DirDrive", "DirInput", "DirOutput"}


def source_rejection(report: dict[str, Any]) -> str:
    """One error rendering for package, segmented and runner build paths."""
    # Rejected front-end analyses intentionally have capability=None. Rendering
    # their reason must not replace that rejection with an AttributeError.
    anomalies = report.get("anomalies")
    reasons = [f"{item.get('code')}: {item.get('message')}" for item in
               (anomalies if isinstance(anomalies, (list, tuple)) else [])
               if isinstance(item, dict) and item.get("severity") == "error"]
    capability = report.get("capability")
    candidates = capability.get("backend_candidates") if isinstance(capability, dict) else None
    for name, item in (candidates.items() if isinstance(candidates, dict) else []):
        if not isinstance(item, dict):
            continue
        if item.get("error"):
            reasons.append(f"{name}: {item['error']}")
        assessment = item.get("runtime_capacity_assessment")
        codes = assessment.get("blocking_codes") if isinstance(assessment, dict) else None
        for code in (codes if isinstance(codes, (list, tuple)) else []):
            reasons.append(f"{name}: {code}")
    return "source analysis is REJECT; package generation is blocked: " + "; ".join(
        reasons or ["no usable source analysis; inspect the retained source.analysis.json"])


def _file_identity(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    result: dict[str, Any] = {"path": str(path)}
    try:
        payload = path.read_bytes()
    except OSError as exc:
        result["read_error"] = str(exc)
        return result
    result.update(
        {
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    return result


def _location_payload(location: Any | None) -> dict[str, Any] | None:
    if location is None:
        return None
    return {
        "source_file": location.source_file,
        "line": int(location.line),
        "column": int(location.column),
        "label": location.label,
        "expansion_trace": list(location.expansion_trace),
    }


def _diagnostic(
    severity: str,
    code: str,
    message: str,
    *,
    location: Any | None = None,
    context: Any | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "line": None if location is None else int(location.line),
        "column": None if location is None else int(location.column),
        "context": context,
    }


def _append_document_diagnostics(document: Any, anomalies: list[dict[str, Any]]) -> None:
    for item in document.diagnostics:
        # The supported parser intentionally treats the STIL Timing wrapper as
        # optional and parses its WaveformTable children at top level.  Do not
        # turn that compatibility behavior into a false REVIEW result.
        if item.code == "WARN_IGNORED_TOPLEVEL" and item.message in {
            "ignored top-level line: Timing {",
            "ignored top-level line: }",
        }:
            continue
        anomalies.append(
            _diagnostic(
                item.severity,
                item.code,
                item.message,
                location=item,
                context=item.context,
            )
        )


def _record_duplicate_names(
    items: list[Any],
    *,
    code: str,
    kind: str,
    anomalies: list[dict[str, Any]],
) -> None:
    seen: dict[str, Any] = {}
    for item in items:
        name = str(item.name)
        if name in seen:
            anomalies.append(
                _diagnostic(
                    "error",
                    code,
                    f"duplicate {kind} declaration: {name}",
                    location=item.location,
                    context={"first_declaration_line": int(seen[name].location.line)},
                )
            )
        else:
            seen[name] = item


def _parse_time_seconds(text: str, *, time_scale: Decimal) -> Decimal:
    return _time_text_to_seconds(text, time_scale=time_scale)


def _check_document_structure(
    document: Any,
    *,
    tick_hz: int,
    time_scale: Decimal,
    anomalies: list[dict[str, Any]],
) -> dict[str, Any]:
    _record_duplicate_names(
        list(document.signals),
        code="ERR_DUPLICATE_SIGNAL",
        kind="signal",
        anomalies=anomalies,
    )
    _record_duplicate_names(
        list(document.signal_groups),
        code="ERR_DUPLICATE_SIGNAL_GROUP",
        kind="signal group",
        anomalies=anomalies,
    )
    _record_duplicate_names(
        list(document.waveform_tables),
        code="ERR_DUPLICATE_WAVEFORM_TABLE",
        kind="waveform table",
        anomalies=anomalies,
    )
    _record_duplicate_names(
        list(document.pattern_bursts),
        code="ERR_DUPLICATE_PATTERN_BURST",
        kind="pattern burst",
        anomalies=anomalies,
    )
    _record_duplicate_names(
        list(document.patterns),
        code="ERR_DUPLICATE_PATTERN",
        kind="pattern",
        anomalies=anomalies,
    )

    signal_names = {signal.name for signal in document.signals}
    group_names = {group.name for group in document.signal_groups}
    for group in document.signal_groups:
        if not group.signals or any(not member for member in group.signals):
            anomalies.append(
                _diagnostic(
                    "error",
                    "ERR_EMPTY_SIGNAL_GROUP",
                    f"signal group {group.name} contains an empty member",
                    location=group.location,
                )
            )
        if len(set(group.signals)) != len(group.signals):
            anomalies.append(
                _diagnostic(
                    "error",
                    "ERR_DUPLICATE_GROUP_MEMBER",
                    f"signal group {group.name} repeats a signal",
                    location=group.location,
                    context={"signals": list(group.signals)},
                )
            )
        for member in group.signals:
            if member not in signal_names:
                anomalies.append(
                    _diagnostic(
                        "error",
                        "ERR_GROUP_UNKNOWN_SIGNAL",
                        f"signal group {group.name} references unknown signal {member}",
                        location=group.location,
                    )
                )

    wft_summaries: list[dict[str, Any]] = []
    for table in document.waveform_tables:
        table_summary: dict[str, Any] = {
            "name": table.name,
            "location": _location_payload(table.location),
            "period_text": table.period_text,
            "period_ticks": None,
            "period_ns": None,
            "waveform_count": len(table.waveforms),
            "characters": {},
            "minimum_gap_ticks": None,
            "minimum_gap_ns": None,
            "out_of_range_events": 0,
            "non_monotonic_event_lists": 0,
            "same_time_event_lists": 0,
        }
        try:
            period_ticks, exact = _time_to_ticks(table.period_text, tick_hz, time_scale=time_scale)
            period_seconds = _parse_time_seconds(table.period_text, time_scale=time_scale)
            table_summary["period_ticks"] = period_ticks
            table_summary["period_ns"] = format(period_seconds * Decimal("1e9"), "f")
            if period_ticks <= 0:
                anomalies.append(
                    _diagnostic(
                        "error",
                        "ERR_NON_POSITIVE_PERIOD",
                        f"waveform table {table.name} has non-positive period {table.period_text}",
                        location=table.location,
                    )
                )
            elif period_ticks < STPG_T_MIN_TICKS:
                anomalies.append(
                    _diagnostic(
                        "note",
                        "NOTE_PERIOD_BELOW_RAW_EVENT_MIN",
                        f"waveform table {table.name} period quantizes to {period_ticks} ticks, below raw-event minimum {STPG_T_MIN_TICKS}",
                        location=table.location,
                        context={"period_ticks": period_ticks, "raw_event_min_ticks": STPG_T_MIN_TICKS},
                    )
                )
            if not exact:
                anomalies.append(
                    _diagnostic(
                        "warning",
                        "WARN_PERIOD_QUANTIZED",
                        f"waveform table {table.name} period {table.period_text} is not an integral device tick",
                        location=table.location,
                        context={"period_ticks": period_ticks, "tick_hz": tick_hz},
                    )
                )
        except (InvalidOperation, ValueError) as exc:
            anomalies.append(
                _diagnostic(
                    "error",
                    "ERR_INVALID_PERIOD",
                    f"waveform table {table.name} has invalid period {table.period_text}: {exc}",
                    location=table.location,
                )
            )

        waveform_targets: set[str] = set()
        for waveform in table.waveforms:
            if waveform.signal_or_group in waveform_targets:
                anomalies.append(
                    _diagnostic(
                        "error",
                        "ERR_DUPLICATE_WAVEFORM_TARGET",
                        f"waveform table {table.name} defines {waveform.signal_or_group} more than once",
                        location=waveform.location,
                    )
                )
            waveform_targets.add(waveform.signal_or_group)
            if waveform.signal_or_group not in signal_names and waveform.signal_or_group not in group_names:
                anomalies.append(
                    _diagnostic(
                        "error",
                        "ERR_WAVEFORM_UNKNOWN_TARGET",
                        f"waveform table {table.name} references unknown signal/group {waveform.signal_or_group}",
                        location=waveform.location,
                    )
                )
            char_names: set[str] = set()
            for char_decl in waveform.chars:
                char = str(char_decl.char)
                table_summary["characters"].setdefault(char, 0)
                table_summary["characters"][char] += 1
                if char in char_names:
                    anomalies.append(
                        _diagnostic(
                            "error",
                            "ERR_DUPLICATE_WAVEFORM_CHAR",
                            f"waveform table {table.name} defines {waveform.signal_or_group}.{char} more than once",
                            location=char_decl.location,
                        )
                    )
                char_names.add(char)
                raw_times: list[Decimal] = []
                tick_times: list[int] = []
                for event in char_decl.events:
                    if event.action_name not in _KNOWN_ACTIONS:
                        anomalies.append(
                            _diagnostic(
                                "warning",
                                "WARN_UNSUPPORTED_WAVEFORM_ACTION",
                                f"waveform action {event.action_name} is outside the supported STPG action set",
                                location=event.location,
                                context={"signal_or_group": waveform.signal_or_group, "character": char},
                            )
                        )
                    try:
                        seconds = _parse_time_seconds(event.time_text, time_scale=time_scale)
                        ticks, exact = _time_to_ticks(event.time_text, tick_hz, time_scale=time_scale)
                    except (InvalidOperation, ValueError) as exc:
                        anomalies.append(
                            _diagnostic(
                                "error",
                                "ERR_INVALID_EVENT_TIME",
                                f"invalid event time {event.time_text}: {exc}",
                                location=event.location,
                            )
                        )
                        continue
                    raw_times.append(seconds)
                    tick_times.append(ticks)
                    period_ticks = table_summary.get("period_ticks")
                    if seconds < 0 or (period_ticks is not None and ticks >= int(period_ticks)):
                        table_summary["out_of_range_events"] += 1
                        anomalies.append(
                            _diagnostic(
                                "error",
                                "ERR_EVENT_TIME_OUT_OF_RANGE",
                                f"event time {event.time_text} is outside waveform table {table.name} period; "
                                "supported interval is 0 <= time < period (boundary edges are not relocated)",
                                location=event.location,
                                context={"event_ticks": ticks, "period_ticks": period_ticks},
                            )
                        )
                    if not exact:
                        anomalies.append(
                            _diagnostic(
                                "note",
                                "NOTE_EVENT_TIME_QUANTIZED",
                                f"event time {event.time_text} is not an integral device tick",
                                location=event.location,
                                context={"event_ticks": ticks, "tick_hz": tick_hz},
                            )
                        )
                    if seconds > 0 and ticks == 0:
                        anomalies.append(
                            _diagnostic(
                                "warning",
                                "WARN_EVENT_QUANTIZED_TO_ZERO",
                                f"positive event time {event.time_text} quantizes to tick zero",
                                location=event.location,
                            )
                        )
                if raw_times and raw_times != sorted(raw_times):
                    table_summary["non_monotonic_event_lists"] += 1
                    anomalies.append(
                        _diagnostic(
                            "warning",
                            "WARN_EVENT_ORDER_NON_MONOTONIC",
                            f"events for {table.name}.{waveform.signal_or_group}.{char} are not source-order monotonic",
                            location=char_decl.location,
                        )
                    )
                if len(set(tick_times)) != len(tick_times):
                    table_summary["same_time_event_lists"] += 1
                    anomalies.append(
                        _diagnostic(
                            "warning",
                            "WARN_SAME_TIME_WAVEFORM_EVENTS",
                            f"events for {table.name}.{waveform.signal_or_group}.{char} share a device tick",
                            location=char_decl.location,
                            context={"tick_times": tick_times},
                        )
                    )
                if tick_times and table_summary.get("period_ticks", 0) > 0:
                    sorted_ticks = sorted(set(tick_times))
                    gaps = [right - left for left, right in zip(sorted_ticks, sorted_ticks[1:])]
                    gaps.append(int(table_summary["period_ticks"]) - sorted_ticks[-1] + sorted_ticks[0])
                    positive_gaps = [gap for gap in gaps if gap >= 0]
                    if positive_gaps:
                        minimum_gap = min(positive_gaps)
                        current = table_summary["minimum_gap_ticks"]
                        if current is None or minimum_gap < current:
                            table_summary["minimum_gap_ticks"] = minimum_gap
                            table_summary["minimum_gap_ns"] = format(
                                Decimal(minimum_gap) * Decimal(1_000_000_000) / Decimal(tick_hz),
                                "f",
                            )
        wft_summaries.append(table_summary)

    known_patterns = {pattern.name for pattern in document.patterns}
    for burst in document.pattern_bursts:
        for pattern_name in burst.patterns:
            if pattern_name not in known_patterns:
                anomalies.append(
                    _diagnostic(
                        "error",
                        "ERR_PATTERN_BURST_UNKNOWN_PATTERN",
                        f"pattern burst {burst.name} references unknown pattern {pattern_name}",
                        location=burst.location,
                    )
                )

    return {
        "signals": [
            {
                "name": signal.name,
                "direction": signal.direction,
                "location": _location_payload(signal.location),
            }
            for signal in document.signals
        ],
        "signal_groups": [
            {
                "name": group.name,
                "signals": list(group.signals),
                "location": _location_payload(group.location),
            }
            for group in document.signal_groups
        ],
        "waveform_tables": wft_summaries,
        "pattern_bursts": [
            {
                "name": burst.name,
                "patterns": list(burst.patterns),
                "macro_domains": list(burst.macro_domains),
                "procedure_domains": list(burst.procedure_domains),
                "location": _location_payload(burst.location),
            }
            for burst in document.pattern_bursts
        ],
        "patterns": [
            {
                "name": pattern.name,
                **_pattern_item_counts(pattern),
                "location": _location_payload(pattern.location),
            }
            for pattern in document.patterns
        ],
    }


def _pattern_item_counts(pattern: Any) -> dict[str, Any]:
    """Return source and logical counts without expanding a preserved loop."""

    if not pattern.items:
        return {
            "vector_count": len(pattern.vectors),
            "source_vector_count": len(pattern.vectors),
            "logical_vector_count": len(pattern.vectors),
            "loop_count": 0,
            "loop_preserved": False,
        }

    def count(items: tuple[Any, ...]) -> tuple[int, int, int]:
        source = logical = loops = 0
        for item in items:
            if isinstance(item, LoopDecl):
                child_source, child_logical, child_loops = count(item.items)
                source += child_source
                logical += item.loop_count * child_logical
                loops += 1 + child_loops
            elif isinstance(item, VectorDecl):
                source += 1
                logical += 1
            else:  # pragma: no cover - parser guarantees PatternItem types
                raise TypeError(f"unexpected pattern item: {type(item).__name__}")
        return source, logical, loops

    source, logical, loops = count(pattern.items)
    return {
        "vector_count": logical,
        "source_vector_count": source,
        "logical_vector_count": logical,
        "loop_count": loops,
        "loop_preserved": True,
    }


def _vector_analysis(ir: Any, *, anomalies: list[dict[str, Any]]) -> dict[str, Any]:
    source_vector_count = len(ir.vectors)
    vector_count = int(ir.logical_vector_count or source_vector_count)
    target_histogram: Counter[str] = Counter()
    waveform_histogram: Counter[str] = Counter()
    char_histogram: Counter[str] = Counter()
    drive_vector_count = 0
    compare_vector_count = 0
    tri_state_vector_count = 0
    no_event_vector_count = 0
    event_group_count = 0
    source_boundaries: list[dict[str, Any]] = []
    previous_waveform: str | None = None
    for vector in ir.vectors:
        target_histogram.update(vector.signal_chars.keys())
        waveform_histogram[vector.waveform_table_name] += 1
        char_histogram.update(vector.signal_chars.values())
        if vector.drive_chars:
            drive_vector_count += 1
        if vector.compare_chars:
            compare_vector_count += 1
        if "Z" in vector.signal_chars.values():
            tri_state_vector_count += 1
        event_times = {
            int(event.time_ticks)
            for events in vector.signal_events.values()
            for event in events
        }
        event_group_count += len(event_times)
        if not event_times:
            no_event_vector_count += 1
        if previous_waveform is not None and previous_waveform != vector.waveform_table_name:
            source_boundaries.append(
                {
                    "before_vector": int(vector.vector_index) - 1,
                    "after_vector": int(vector.vector_index),
                    "after_stil_line": int(vector.stil_line),
                    "from_waveform_table": previous_waveform,
                    "to_waveform_table": vector.waveform_table_name,
                }
            )
        previous_waveform = vector.waveform_table_name
    if no_event_vector_count:
        anomalies.append(
            _diagnostic(
                "warning",
                "WARN_VECTOR_WITHOUT_EFFECTIVE_EVENT",
                f"{no_event_vector_count} resolved vector(s) contain no effective drive or compare event",
                context={"count": no_event_vector_count},
            )
        )
    return {
        "vector_count": vector_count,
        "source_vector_count": source_vector_count,
        "logical_vector_count": vector_count,
        "loop_preserved": bool(ir.loop_preserved),
        "event_group_count": event_group_count,
        "target_signal_use_histogram": dict(sorted(target_histogram.items())),
        "waveform_table_histogram": dict(sorted(waveform_histogram.items())),
        "character_histogram": dict(sorted(char_histogram.items())),
        "drive_vector_count": drive_vector_count,
        "compare_vector_count": compare_vector_count,
        "tri_state_vector_count": tri_state_vector_count,
        "no_effective_event_vector_count": no_event_vector_count,
        "normalized_transition_count": int(ir.resolved_pattern.normalized_transition_count),
        "waveform_table_transition_count": len(source_boundaries),
        "waveform_table_transition_examples": source_boundaries[:32],
    }


def _timing_summary(ir: Any, *, tick_hz: int, time_scale: Decimal) -> dict[str, Any]:
    period_histogram: Counter[str] = Counter()
    period_ticks: dict[str, int] = {}
    for vector in ir.vectors:
        name = vector.waveform_table_name
        period_histogram[name] += 1
        period_ticks[name] = int(vector.rate_key.period_ticks)
    return {
        "tick_hz": int(tick_hz),
        "time_scale": str(time_scale),
        "source_vector_count": len(ir.vectors),
        "logical_vector_count": int(ir.logical_vector_count or len(ir.vectors)),
        "loop_preserved": bool(ir.loop_preserved),
        "period_ticks_by_waveform_table": dict(sorted(period_ticks.items())),
        "vector_use_by_waveform_table": dict(sorted(period_histogram.items())),
        "approximation_required": bool(ir.approximate_reasons),
        "approximation_count": len(ir.approximations),
        "approximation_reasons": list(ir.approximate_reasons),
    }


def _candidate_report(
    name: str,
    compiler: Callable[..., Any],
    ir: Any,
    *,
    tick_hz: int,
    max_rates: int | None = None,
    runtime_profile: RuntimeCapacityProfile | None = None,
    _prepared_source: PreparedStilSource | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": name, "status": "unavailable", "error_code": None, "error": None}
    observe_capacity_timing = False
    try:
        if ir.loop_preserved and name != "rate-vector":
            raise StilError(
                "ERR_LOOP_BACKEND_UNSUPPORTED",
                f"preserved STIL loops require the rate-vector backend; {name} cannot retain loop structure",
            )
        if name == "rate-vector":
            if _prepared_source is None:
                built = compiler(ir, tick_hz=tick_hz, allow_approx=True, max_rates=max_rates)
            else:
                built = _prepared_source._rate_vector(
                    ir, compiler=compiler, tick_hz=tick_hz, allow_approx=True, max_rates=max_rates)
        elif name == "raw-event":
            built = compiler(ir, tick_hz=tick_hz, allow_approx=True)
        else:
            built = compiler(ir, tick_hz=tick_hz)
        requirements = inspect_pattern_requirements(built.pattern_bytes)
        structure_metrics = (
            rate_vector_structure_metrics(built.pattern_bytes)
            if name == "rate-vector"
            else {}
        )
        result.update(
            {
                "status": "available",
                "vector_count": int(getattr(built, "vector_count", len(ir.vectors))),
                "rate_count": int(getattr(built, "rate_count", 0)),
                "slot_count": int(getattr(built, "slot_count", 0)),
                "estimated_event_count": int(requirements.estimated_event_count),
                "pattern_size_bytes": len(built.pattern_bytes),
                "estimated_sample_count": int(requirements.estimated_sample_count),
                "estimated_compare_count": int(requirements.estimated_compare_count),
                "requires_compare": bool(requirements.requires_compare),
                "requires_sample": bool(requirements.requires_sample),
                "streaming_poc_compatible": requirements.streaming_poc_incompatibility is None,
                "streaming_poc_incompatibility": requirements.streaming_poc_incompatibility,
                "streaming_poc_timing_issues": list(requirements.streaming_poc_timing_issues),
                "structure_metrics": structure_metrics,
            }
        )
        if runtime_profile is not None:
            result["runtime_capacity_assessment"] = runtime_capacity_observation(
                requirements,
                pattern_size_bytes=len(built.pattern_bytes),
                profile=runtime_profile,
            )
    except StilError as exc:
        result.update({"error_code": exc.code, "error": str(exc)})
        if name == "rate-vector" and exc.__cause__ is None and _is_loop_block_capacity_error(exc.code, str(exc)):
            result["loop_block_capacity"] = True  # typed leaf, not an arbitrary Loop/parser failure
            observe_capacity_timing = True
    except PatternValidationError as exc:
        result.update({"error_code": "ERR_BACKEND_VALIDATION", "error": str(exc)})
        if name == "rate-vector" and str(exc) in {
            "rate vector exceeds PSRAM pattern region", "wide rate vector exceeds PSRAM pattern region",
            "rate vector exceeds the runtime event limit",
            "rate vector exceeds the PSRAM result region", "rate vector exceeds the runtime compare limit",
        }:
            observe_capacity_timing = True
    except Exception as exc:  # pragma: no cover - defensive boundary for malformed host input
        result["error"] = f"{type(exc).__name__}: {exc}"
    if observe_capacity_timing:
        # A failed single-package capacity check cannot hide an independent
        # mandatory streaming timing error. No encoded artifact is admitted.
        cache = _SlotSignatureCache()
        rates, slots, rate_ids = _build_rate_tables(ir.vectors, _slot_signatures=cache)
        counts = [len({slot.time_ticks for slot in slots[rate.slot_offset:rate.slot_offset + rate.slot_count]})
                  for rate in rates]
        def events(items):
            return sum(int(item.loop_count) * events(item.items) if hasattr(item, "items") else
                       counts[rate_ids[_vector_rate_key(item, _slot_signatures=cache)]] for item in items)
        issues = streaming_poc_timing_issues(rates, slots)
        result.update({"estimated_event_count": events(ir.sequence_items or ir.vectors),
                       "streaming_poc_timing_issues": issues,
                       "streaming_poc_compatible": not issues,
                       "streaming_poc_incompatibility": issues[0]["message"] if issues else None,
                       "streaming_timing_observation": "unpacked_ir_after_capacity_rejection"})
    return result


def _segmentation_advice(
    *,
    mode_requested: str,
    backend_candidates: dict[str, Any],
    anomalies: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe whether a single-package capacity failure can use segmentation.

    A capacity failure is not converted into a PASS: the unsplit artifact is
    still inadmissible.  This advice lets the CLI offer the explicit,
    host-only segmented packaging path without masking other errors.
    """

    base = {
        "available": False,
        "status": "NOT_APPLICABLE",
        "physical_execution_authorized": False,
        "recommended_segment_vector_count": 100_000,
    }
    if mode_requested != "rate-vector":
        return {
            **base,
            "reason": "segmentation fallback is currently opt-in for requested mode=rate-vector",
        }

    candidate = backend_candidates.get("rate-vector")
    if not isinstance(candidate, dict):
        return {**base, "reason": "rate-vector candidate was not evaluated"}

    capacity_failure = False
    capacity_reason = ""
    capacity_reason_code = ""
    assessment = candidate.get("runtime_capacity_assessment")
    if isinstance(assessment, dict):
        blocking_codes = set(assessment.get("blocking_codes") or [])
        capacity_messages = {
            "ERR_PSRAM_PATTERN_BYTES_LIMIT": "compiled rate-vector pattern exceeds the PSRAM pattern region",
            "ERR_RUNTIME_STREAM_EVENT_LIMIT": "expanded streaming event count exceeds the runtime event limit",
            "ERR_RUNTIME_STREAM_SAMPLE_LIMIT": "expanded streaming sample count exceeds the PSRAM result region",
            "ERR_RUNTIME_STREAM_COMPARE_LIMIT": "expanded streaming compare count exceeds the runtime compare limit",
        }
        for code in (
            "ERR_PSRAM_PATTERN_BYTES_LIMIT",
            "ERR_RUNTIME_STREAM_EVENT_LIMIT",
            "ERR_RUNTIME_STREAM_SAMPLE_LIMIT",
            "ERR_RUNTIME_STREAM_COMPARE_LIMIT",
        ):
            if code in blocking_codes:
                capacity_failure = True
                capacity_reason_code = code
                capacity_reason = capacity_messages[code]
                break
    if (
        candidate.get("status") == "unavailable"
        and str(candidate.get("error") or "") in {
            "rate vector exceeds PSRAM pattern region",
            "wide rate vector exceeds PSRAM pattern region",
            "rate vector exceeds the runtime event limit",
            "rate vector exceeds the PSRAM result region",
            "rate vector exceeds the runtime compare limit",
        }
    ):
        capacity_failure = True
        error_to_code = {
            "rate vector exceeds PSRAM pattern region": "ERR_PSRAM_PATTERN_BYTES_LIMIT",
            "wide rate vector exceeds PSRAM pattern region": "ERR_PSRAM_PATTERN_BYTES_LIMIT",
            "rate vector exceeds the runtime event limit": "ERR_RUNTIME_STREAM_EVENT_LIMIT",
            "rate vector exceeds the PSRAM result region": "ERR_RUNTIME_STREAM_SAMPLE_LIMIT",
            "rate vector exceeds the runtime compare limit": "ERR_RUNTIME_STREAM_COMPARE_LIMIT",
        }
        capacity_reason_code = error_to_code[str(candidate.get("error"))]
        capacity_reason = {
            "ERR_PSRAM_PATTERN_BYTES_LIMIT": "compiled rate-vector pattern exceeds the PSRAM pattern region",
            "ERR_RUNTIME_STREAM_EVENT_LIMIT": "expanded streaming event count exceeds the runtime event limit",
            "ERR_RUNTIME_STREAM_SAMPLE_LIMIT": "expanded streaming sample count exceeds the PSRAM result region",
            "ERR_RUNTIME_STREAM_COMPARE_LIMIT": "expanded streaming compare count exceeds the runtime compare limit",
        }[capacity_reason_code]
    if (not capacity_failure and candidate.get("status") == "unavailable"
            and candidate.get("loop_block_capacity") is True
            and _is_loop_block_capacity_error(candidate.get("error_code"), candidate.get("error"))):
        capacity_failure = True
        capacity_reason_code = "ERR_TOO_MANY_LOOP_BLOCKS"
        capacity_reason = str(candidate["error"])
    if not capacity_failure:
        return {**base, "reason": "no segmentable PSRAM pattern-region failure was detected"}

    error_codes = {
        str(item.get("code"))
        for item in anomalies
        if item.get("severity") == "error"
    }
    allowed_wrapper_codes = {
        "ERR_PSRAM_PATTERN_BYTES_LIMIT",
        "ERR_RUNTIME_STREAM_EVENT_LIMIT",
        "ERR_RUNTIME_STREAM_SAMPLE_LIMIT",
        "ERR_RUNTIME_STREAM_COMPARE_LIMIT",
        "ERR_TOO_MANY_LOOP_BLOCKS",
        "ERR_REQUESTED_BACKEND_UNAVAILABLE",
    }
    if not error_codes.issubset(allowed_wrapper_codes):
        return {
            **base,
            "reason": "additional non-capacity errors prevent segmentation fallback",
            "blocking_codes": sorted(error_codes),
        }

    return {
        **base,
        "available": True,
        "status": "RECOMMENDED",
        "reason_code": capacity_reason_code,
        "reason": capacity_reason,
        "next_action": "run stil.cli_analyze --segment-on-capacity or stil.cli_segment_package",
    }


def _finalize_status(anomalies: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    errors = [item for item in anomalies if item.get("severity") == "error"]
    warnings = [item for item in anomalies if item.get("severity") == "warning"]
    if errors:
        return "REJECT", errors, warnings
    if warnings:
        return "REVIEW", errors, warnings
    return "PASS", errors, warnings


def _wait_planning_summary(
    *,
    rate_candidate: dict[str, Any] | None,
    profile: RuntimeCapacityProfile,
) -> dict[str, Any]:
    """Describe what source analysis can and cannot decide about waits.

    A WaveformTable gap is an intra-rate encoding issue; a chunk-boundary wait
    cannot repair it.  Conversely, the absence of such a gap does not prove
    that a long streaming pattern needs no refill wait.  Keeping these two
    decisions separate prevents a 5 ms value (or any other value) from being
    presented as an automatic or universal insertion.
    """

    if not isinstance(rate_candidate, dict) or rate_candidate.get("status") != "available":
        return {
            "streaming_timing_gate": {
                "status": "UNAVAILABLE",
                "reason": "rate-vector backend candidate is unavailable",
            },
            "boundary_wait_decision": {
                "status": "UNAVAILABLE",
                "reason": "source timing cannot be mapped to a rate-vector boundary schedule",
            },
            "automatic_wait_insertion": False,
            "wait_value_source": "none",
        }

    issues = [item for item in rate_candidate.get("streaming_poc_timing_issues", []) if isinstance(item, dict)]
    requires_streaming = int(rate_candidate.get("estimated_event_count", 0)) > profile.full_expansion_event_limit
    if issues:
        deficits = [int(item.get("deficit_ticks", 0)) for item in issues]
        gaps = [int(item.get("gap_ticks", 0)) for item in issues]
        timing_gate = {
            "status": "REJECT",
            "reason": "one or more intra-rate gaps are below the streaming-poc overhead",
            "issue_count": len(issues),
            "minimum_gap_ticks": min(gaps),
            "maximum_deficit_ticks": max(deficits),
            "issues": issues,
        }
        boundary_wait = {
            "status": "NOT_A_REMEDIATION" if requires_streaming else "NOT_REQUIRED_FOR_DIRECT",
            "reason": (
                "chunk-boundary waits do not repair an intra-rate gap"
                if requires_streaming
                else "selected execution is direct; streaming-poc timing is reported but is not selected"
            ),
        }
    elif requires_streaming:
        timing_gate = {
            "status": "PASS",
            "reason": "no intra-rate streaming-poc gap violation was found",
            "issue_count": 0,
        }
        boundary_wait = {
            "status": "MODEL_REQUIRED",
            "reason": "refill headroom for oversized streaming must be evaluated with an explicit service profile",
        }
    else:
        timing_gate = {
            "status": "PASS",
            "reason": "no intra-rate streaming-poc gap violation was found",
            "issue_count": 0,
        }
        boundary_wait = {
            "status": "NOT_REQUIRED_FOR_STREAMING",
            "reason": "the estimated event count does not require oversized streaming",
        }
    return {
        "streaming_timing_gate": timing_gate,
        "selected_execution_path": "streaming" if requires_streaming else "direct",
        "selected_execution_path_status": "REJECT" if issues and requires_streaming else "PASS",
        "boundary_wait_decision": boundary_wait,
        "automatic_wait_insertion": False,
        "wait_value_source": "none_until_declared_service_profile",
        "profile": runtime_capacity_contract(profile),
    }


def build_source_analysis_report(
    ir: Any,
    *,
    mode_requested: str = "auto",
    mode_emitted: str | None = None,
    tick_hz: int = STPG_DEFAULT_TICK_HZ,
    time_scale: Decimal = Decimal("1.0"),
    pin_map_path: Path | None = None,
    include_backend_candidates: bool = True,
    runtime_profile: str | RuntimeCapacityProfile = DEFAULT_RUNTIME_PROFILE,
    max_rates: int | None = None,
    _prepared_source: PreparedStilSource | None = None,
    _micro_op_family_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-ready source analysis report from resolved compiler IR."""

    document = ir.resolved_pattern.document
    profile = (
        get_runtime_capacity_profile(runtime_profile)
        if isinstance(runtime_profile, str)
        else runtime_profile
    )
    anomalies: list[dict[str, Any]] = []
    _append_document_diagnostics(document, anomalies)
    structure = _check_document_structure(
        document,
        tick_hz=tick_hz,
        time_scale=time_scale,
        anomalies=anomalies,
    )
    vector_summary = _vector_analysis(ir, anomalies=anomalies)
    family_report = (
        analyze_micro_op_families(ir) if _micro_op_family_analysis is None
        else deepcopy(_micro_op_family_analysis)
    )
    anomalies.extend(build_micro_op_diagnostics(family_report))
    if ir.approximate_reasons:
        anomalies.append(
            _diagnostic(
                "warning",
                "WARN_SELECTED_TIMING_APPROXIMATION",
                "selected pattern requires device-tick timing quantization",
                context={
                    "approximation_count": len(ir.approximations),
                    "approximation_reasons": list(ir.approximate_reasons),
                },
            )
        )

    backend_candidates: dict[str, Any] = {}
    if include_backend_candidates:
        backend_candidates = {
            "raw-event": _candidate_report(
                "raw-event", compile_raw_event, ir, tick_hz=tick_hz, runtime_profile=profile
            ),
            "rate-vector": _candidate_report(
                "rate-vector",
                compile_rate_vector,
                ir,
                tick_hz=tick_hz,
                max_rates=max_rates,
                runtime_profile=profile,
                _prepared_source=_prepared_source,
            ),
            "macro-stream": _candidate_report(
                "macro-stream", compile_macro_stream, ir, tick_hz=tick_hz, runtime_profile=profile
            ),
        }
        requested_candidate = backend_candidates.get(mode_requested)
        if mode_requested in backend_candidates and requested_candidate.get("status") != "available":
            anomalies.append(
                _diagnostic(
                    "error",
                    "ERR_REQUESTED_BACKEND_UNAVAILABLE",
                    f"requested backend {mode_requested} is unavailable for this STIL input",
                    context=requested_candidate,
                )
            )
        if mode_requested == "auto" and not any(
            item.get("status") == "available" for item in backend_candidates.values()
        ):
            anomalies.append(
                _diagnostic(
                    "error",
                    "ERR_NO_BACKEND_AVAILABLE",
                    "no STPG backend can represent this STIL input",
                    context=backend_candidates,
                )
            )
        rate_candidate = backend_candidates.get("rate-vector")
        if isinstance(rate_candidate, dict) and rate_candidate.get("status") == "available":
            rate_capacity = rate_candidate.get("runtime_capacity_assessment") or {}
            timing_issues = rate_candidate.get("streaming_poc_timing_issues") or []
            requires_streaming = int(rate_candidate.get("estimated_event_count", 0)) > STPG_MAX_EVENT_COUNT
            if mode_requested == "rate-vector" or (mode_requested == "auto" and requires_streaming):
                if rate_capacity.get("boundary_metrics"):
                    anomalies.append(
                        _diagnostic(
                            "warning",
                            "WARN_RUNTIME_CAPACITY_AT_LIMIT",
                            (
                                "rate-vector runtime capacity has zero remaining headroom for "
                                + ", ".join(str(item) for item in rate_capacity["boundary_metrics"])
                            ),
                            context=rate_capacity,
                        )
                    )
                for capacity_issue in assess_runtime_capacity(
                    SimpleNamespace(
                        mode="rate-vector",
                        estimated_event_count=int(rate_candidate.get("estimated_event_count", 0)),
                        estimated_sample_count=int(rate_candidate.get("estimated_sample_count", 0)),
                        estimated_compare_count=int(rate_candidate.get("estimated_compare_count", 0)),
                        requires_sample=bool(rate_candidate.get("requires_sample", False)),
                        requires_compare=bool(rate_candidate.get("requires_compare", False)),
                    ),
                    pattern_size_bytes=rate_candidate.get("pattern_size_bytes"),
                    profile=profile,
                ):
                    anomalies.append(
                        _diagnostic(
                            str(capacity_issue["severity"]),
                            str(capacity_issue["code"]),
                            str(capacity_issue["message"]),
                            context=capacity_issue.get("context"),
                        )
                    )
        if isinstance(rate_candidate, dict) and (rate_candidate.get("status") == "available"
                or rate_candidate.get("streaming_timing_observation") == "unpacked_ir_after_capacity_rejection"):
            timing_issues = rate_candidate.get("streaming_poc_timing_issues") or []
            requires_streaming = int(rate_candidate.get("estimated_event_count", 0)) > STPG_MAX_EVENT_COUNT
            if timing_issues and (mode_requested == "rate-vector" or requires_streaming):
                # A small rate-vector can still run through the bounded
                # non-streaming path. Once the event estimate exceeds that
                # path's limit, the same timing issue is a hard preflight
                # failure because streaming-poc is required.
                severity = "error" if requires_streaming else "warning"
                code = "ERR_STREAMING_POC_TIMING" if requires_streaming else "WARN_STREAMING_POC_TIMING"
                for issue in timing_issues:
                    if not isinstance(issue, dict):
                        continue
                    anomalies.append(
                        _diagnostic(
                            severity,
                            code,
                            str(issue.get("message", "rate-vector timing is incompatible with streaming-poc")),
                            context={
                                "backend": "rate-vector",
                                "streaming_required_by_event_count": requires_streaming,
                                "non_streaming_event_limit": profile.full_expansion_event_limit,
                                "streaming_poc_required_drive_gap_ticks": 10,
                                "streaming_poc_required_sample_gap_ticks": 11,
                                **issue,
                                "resolution": (
                                    "adjust the STIL WaveformTable timing, choose a supported encoding, "
                                    "or design a firmware candidate; chunk boundary waits do not repair "
                                    "intra-rate gaps"
                                ),
                            },
                        )
                    )

    rate_candidate_for_wait = backend_candidates.get("rate-vector")
    wait_summary = _wait_planning_summary(
        rate_candidate=rate_candidate_for_wait if isinstance(rate_candidate_for_wait, dict) else None,
        profile=profile,
    )
    runtime_capacity_assessments = {
        name: candidate.get("runtime_capacity_assessment")
        for name, candidate in backend_candidates.items()
        if isinstance(candidate, dict) and candidate.get("runtime_capacity_assessment") is not None
    }
    segmentation = _segmentation_advice(
        mode_requested=mode_requested,
        backend_candidates=backend_candidates,
        anomalies=anomalies,
    )
    # Keep the concrete capacity cause visible even when the backend probe
    # failed inside the packer before it could emit a runtime observation.
    # The generic requested-backend wrapper is useful for control flow, but it
    # is not sufficient for a user or a segmentation planner to understand
    # why the single package was rejected.
    segmentation_reason_code = segmentation.get("reason_code")
    if segmentation.get("available") is True and segmentation_reason_code:
        existing_codes = {str(item.get("code")) for item in anomalies}
        if str(segmentation_reason_code) not in existing_codes:
            anomalies.append(
                _diagnostic(
                    "error",
                    str(segmentation_reason_code),
                    str(segmentation.get("reason")),
                    context={"segmentation": segmentation},
                )
            )
    status, errors, warnings = _finalize_status(anomalies)
    selected_pattern = ir.resolved_pattern.selected_pattern_name
    return {
        "kind": ANALYSIS_KIND,
        "analysis_version": ANALYSIS_VERSION,
        "status": status,
        "decision": {
            "status": status,
            "physical_execution_authorized": False,
            "generation_ready": status == "PASS",
            "error_count": len(errors),
            "warning_count": len(warnings),
            "blocking_codes": sorted({str(item["code"]) for item in errors}),
            "review_codes": sorted({str(item["code"]) for item in warnings}),
        },
        "input": {
            "source": _file_identity(Path(document.source_file)),
            "pin_map": _file_identity(pin_map_path),
        },
        "selection": {
            "pattern_burst": ir.resolved_pattern.selected_pattern_burst,
            "pattern": selected_pattern,
            "mode_requested": mode_requested,
            "mode_emitted": mode_emitted,
            "loop_policy": "preserve" if ir.loop_preserved else "expand",
        },
        "source_structure": structure,
        "vectors": vector_summary,
        "timing": _timing_summary(ir, tick_hz=tick_hz, time_scale=time_scale),
        "runtime_capacity": runtime_capacity_contract(profile),
        "runtime_capacity_assessments": runtime_capacity_assessments,
        "segmentation": segmentation,
        "rate_vector_contract": {"max_rate_count": resolve_max_rates(max_rates)},
        "capability": {
            "active_pins": int(ir.resolved_pattern.pin_map.active_pins),
            "mapped_signal_count": len(ir.resolved_pattern.pin_map.signals),
            "max_assigned_bit": max(
                (int(assignment.bit) for assignment in ir.resolved_pattern.pin_map.signals.values()),
                default=-1,
            ),
            "micro_op_family_analysis": family_report,
            "backend_candidates": backend_candidates,
        },
        "wait_planning_inputs": {
            "resolved_vector_count": len(ir.vectors),
            "logical_vector_count": int(ir.logical_vector_count or len(ir.vectors)),
            "loop_preserved": bool(ir.loop_preserved),
            "emitted_event_group_count": vector_summary["event_group_count"],
            "rate_transition_count": vector_summary["waveform_table_transition_count"],
            "rate_transition_examples": vector_summary["waveform_table_transition_examples"],
            **wait_summary,
            "service_time_is_measured": False,
        },
        "normalization": {
            "transition_count": int(ir.resolved_pattern.normalized_transition_count),
            "transition_summary": list(ir.resolved_pattern.normalized_transition_summary),
        },
        "anomalies": anomalies,
    }


def _error_report(
    input_path: Path,
    *,
    pin_map_path: Path | None,
    mode: str,
    pattern_burst: str | None,
    pattern: str | None,
    tick_hz: int,
    time_scale: Decimal,
    code: str,
    message: str,
    max_rates: int | None = None,
) -> dict[str, Any]:
    anomaly = _diagnostic("error", code, message)
    return {
        "kind": ANALYSIS_KIND,
        "analysis_version": ANALYSIS_VERSION,
        "status": "REJECT",
        "decision": {
            "status": "REJECT",
            "physical_execution_authorized": False,
            "generation_ready": False,
            "error_count": 1,
            "warning_count": 0,
            "blocking_codes": [code],
            "review_codes": [],
        },
        "input": {"source": _file_identity(input_path), "pin_map": _file_identity(pin_map_path)},
        "selection": {
            "pattern_burst": pattern_burst,
            "pattern": pattern,
            "mode_requested": mode,
            "mode_emitted": None,
        },
        "source_structure": None,
        "vectors": None,
        "timing": {"tick_hz": int(tick_hz), "time_scale": str(time_scale)},
        "rate_vector_contract": {"max_rate_count": resolve_max_rates(max_rates)},
        "capability": None,
        "wait_planning_inputs": None,
        "normalization": None,
        "anomalies": [anomaly],
    }


def analyze_stil_source(
    input_path: Path,
    *,
    mode: str = "auto",
    tick_hz: int = STPG_DEFAULT_TICK_HZ,
    pin_map_path: Path | None = None,
    pattern_burst: str | None = None,
    pattern: str | None = None,
    time_scale: Decimal = Decimal("1.0"),
    include_backend_candidates: bool = True,
    runtime_profile: str | RuntimeCapacityProfile = DEFAULT_RUNTIME_PROFILE,
    max_rates: int | None = None,
    until_vector: int | None = None,
    vector_range: tuple[int, int] | None = None,
    loop_policy: str = "expand",
    prepared_source: PreparedStilSource | None = None,
) -> dict[str, Any]:
    """Analyze a STIL source and always return a durable JSON-ready report."""

    if tick_hz <= 0:
        return _error_report(
            input_path,
            pin_map_path=pin_map_path,
            mode=mode,
            pattern_burst=pattern_burst,
            pattern=pattern,
            tick_hz=tick_hz,
            time_scale=time_scale,
            code="ERR_INVALID_TICK_HZ",
            message=f"tick_hz must be positive, got {tick_hz}",
            max_rates=max_rates,
        )
    if not time_scale.is_finite() or time_scale <= 0:
        return _error_report(
            input_path,
            pin_map_path=pin_map_path,
            mode=mode,
            pattern_burst=pattern_burst,
            pattern=pattern,
            tick_hz=tick_hz,
            time_scale=time_scale,
            code="ERR_INVALID_TIME_SCALE",
            message=f"time_scale must be finite and positive, got {time_scale}",
            max_rates=max_rates,
        )
    if until_vector is not None and vector_range is not None:
        return _error_report(
            input_path,
            pin_map_path=pin_map_path,
            mode=mode,
            pattern_burst=pattern_burst,
            pattern=pattern,
            tick_hz=tick_hz,
            time_scale=time_scale,
            code="ERR_INVALID_VECTOR_SELECTION",
            message="until_vector and vector_range are mutually exclusive",
            max_rates=max_rates,
        )
    if until_vector is not None and until_vector < 0:
        return _error_report(
            input_path,
            pin_map_path=pin_map_path,
            mode=mode,
            pattern_burst=pattern_burst,
            pattern=pattern,
            tick_hz=tick_hz,
            time_scale=time_scale,
            code="ERR_INVALID_VECTOR_SELECTION",
            message=f"until_vector must be non-negative, got {until_vector}",
            max_rates=max_rates,
        )
    if vector_range is not None:
        if len(vector_range) != 2 or vector_range[0] < 0 or vector_range[0] > vector_range[1]:
            return _error_report(
                input_path,
                pin_map_path=pin_map_path,
                mode=mode,
                pattern_burst=pattern_burst,
                pattern=pattern,
                tick_hz=tick_hz,
                time_scale=time_scale,
                code="ERR_INVALID_VECTOR_SELECTION",
                message=f"vector_range must be a non-empty inclusive range, got {vector_range!r}",
                max_rates=max_rates,
            )
    try:
        if prepared_source is not None:
            prepared_source.validate_binding(input_path, pin_map_path=pin_map_path,
                                              pattern_burst=pattern_burst, pattern=pattern,
                                              loop_policy=loop_policy)
            document = prepared_source.document()
        else:
            document = parse_stil_file(input_path, loop_policy=loop_policy)
    except StilError as exc:
        return _error_report(
            input_path,
            pin_map_path=pin_map_path,
            mode=mode,
            pattern_burst=pattern_burst,
            pattern=pattern,
            tick_hz=tick_hz,
            time_scale=time_scale,
            code=exc.code,
            message=str(exc),
            max_rates=max_rates,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return _error_report(
            input_path,
            pin_map_path=pin_map_path,
            mode=mode,
            pattern_burst=pattern_burst,
            pattern=pattern,
            tick_hz=tick_hz,
            time_scale=time_scale,
            code="ERR_STIL_INPUT",
            message=str(exc),
            max_rates=max_rates,
        )

    structural_anomalies: list[dict[str, Any]] = []
    source_structure = _check_document_structure(
        document,
        tick_hz=tick_hz,
        time_scale=time_scale,
        anomalies=structural_anomalies,
    )
    try:
        if prepared_source is not None:
            selection = (0, until_vector) if until_vector is not None else vector_range
            try:
                resolved = prepared_source.resolve(selection)
            except StilError as exc:
                if exc.code == "ERR_BAD_VECTOR_RANGE":
                    # Preserve the legacy error report's full lowered counts.
                    _update_lowered_pattern_structure(source_structure, prepared_source.resolve())
                raise
            _update_lowered_pattern_structure(source_structure, resolved)
        else:
            pin_map = load_pin_map(pin_map_path, document=document)
            resolved = resolve_document(
                document,
                pin_map=pin_map,
                pattern_burst_name=pattern_burst,
                pattern_name=pattern,
                loop_policy=loop_policy,
            )
            _update_lowered_pattern_structure(source_structure, resolved)
            if until_vector is not None:
                resolved = slice_resolved_pattern(resolved, start=0, end=until_vector)
            elif vector_range is not None:
                resolved = slice_resolved_pattern(resolved, start=vector_range[0], end=vector_range[1])
        try:
            ir = (
                prepared_source._compiler_ir(resolved, tick_hz=tick_hz, time_scale=time_scale)
                if prepared_source is not None
                else build_ir_with_scale(resolved, tick_hz=tick_hz, time_scale=time_scale)
            )
        except (StilError, InvalidOperation, KeyError, TypeError, ValueError):
            if prepared_source is not None and selection is not None:
                # The legacy error report retained full-source lowered counts
                # before slicing. Pay for a full resolution only on this error
                # path, not during a successful selected-range conversion.
                _update_lowered_pattern_structure(source_structure, prepared_source.resolve())
            raise
    except StilError as exc:
        report = _error_report(
            input_path,
            pin_map_path=pin_map_path,
            mode=mode,
            pattern_burst=pattern_burst,
            pattern=pattern,
            tick_hz=tick_hz,
            time_scale=time_scale,
            code=exc.code,
            message=str(exc),
        )
        report["source_structure"] = {
            **source_structure,
        }
        report["anomalies"].extend(structural_anomalies)
        status, errors, warnings = _finalize_status(report["anomalies"])
        report["status"] = status
        report["decision"].update(
            {
                "status": status,
                "generation_ready": status == "PASS",
                "error_count": len(errors),
                "warning_count": len(warnings),
                "blocking_codes": sorted({str(item["code"]) for item in errors}),
                "review_codes": sorted({str(item["code"]) for item in warnings}),
            }
        )
        return report
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        report = _error_report(
            input_path,
            pin_map_path=pin_map_path,
            mode=mode,
            pattern_burst=pattern_burst,
            pattern=pattern,
            tick_hz=tick_hz,
            time_scale=time_scale,
            code="ERR_STIL_RESOLUTION",
            message=str(exc),
        )
        report["source_structure"] = source_structure
        report["anomalies"].extend(structural_anomalies)
        status, errors, warnings = _finalize_status(report["anomalies"])
        report["status"] = status
        report["decision"].update(
            {
                "status": status,
                "generation_ready": status == "PASS",
                "error_count": len(errors),
                "warning_count": len(warnings),
                "blocking_codes": sorted({str(item["code"]) for item in errors}),
                "review_codes": sorted({str(item["code"]) for item in warnings}),
            }
        )
        return report
    except Exception as exc:  # pragma: no cover - defensive boundary for malformed host input
        return _error_report(
            input_path,
            pin_map_path=pin_map_path,
            mode=mode,
            pattern_burst=pattern_burst,
            pattern=pattern,
            tick_hz=tick_hz,
            time_scale=time_scale,
            code="ERR_STIL_ANALYSIS_INTERNAL",
            message=f"{type(exc).__name__}: {exc}",
            max_rates=max_rates,
        )

    report = build_source_analysis_report(
        ir,
        mode_requested=mode,
        tick_hz=tick_hz,
        time_scale=time_scale,
        pin_map_path=pin_map_path,
        include_backend_candidates=include_backend_candidates,
        runtime_profile=runtime_profile,
        max_rates=max_rates,
        _prepared_source=prepared_source,
    )
    _update_lowered_pattern_structure(report["source_structure"], resolved)
    report["selection"].update(
        {
            "scope": "selected_vector_range" if until_vector is not None or vector_range is not None else "full_source",
            "until_vector": until_vector,
            "vector_range": list(vector_range) if vector_range is not None else None,
            "resolved_vector_count": len(ir.vectors),
            "logical_vector_count": int(ir.logical_vector_count or len(ir.vectors)),
            "loop_policy": "preserve" if ir.loop_preserved else "expand",
        }
    )
    return report


def _update_lowered_pattern_structure(source_structure: dict[str, Any], resolved: Any) -> None:
    """Replace parser-only counts with the selected statically lowered counts."""

    def logical_vectors(items: tuple[Any, ...]) -> int:
        total = 0
        for item in items:
            if isinstance(item, ResolvedLoop):
                total += item.loop_count * logical_vectors(item.items)
            else:
                total += 1
        return total

    logical_count = logical_vectors(tuple(resolved.items)) if resolved.items else len(resolved.vectors)
    loop_count = 0

    def count_loops(items: tuple[Any, ...]) -> None:
        nonlocal loop_count
        for item in items:
            if isinstance(item, ResolvedLoop):
                loop_count += 1
                count_loops(item.items)

    count_loops(tuple(resolved.items))
    for pattern_summary in source_structure.get("patterns", []):
        if pattern_summary.get("name") == resolved.selected_pattern_name:
            pattern_summary.update(
                {
                    "vector_count": logical_count,
                    "source_vector_count": len(resolved.vectors),
                    "logical_vector_count": logical_count,
                    "loop_count": loop_count,
                    "loop_preserved": bool(resolved.loop_preserved),
                    "control_flow_lowered": True,
                }
            )
            break


def render_source_analysis_text(report: dict[str, Any]) -> str:
    decision = report.get("decision") or {}
    lines = [
        f"STATUS {report.get('status')} errors={decision.get('error_count', 0)} warnings={decision.get('warning_count', 0)}",
    ]
    selection = report.get("selection") or {}
    lines.append(
        f"SELECTION pattern={selection.get('pattern')} burst={selection.get('pattern_burst')} mode={selection.get('mode_requested')}"
    )
    vectors = report.get("vectors") or {}
    if vectors:
        lines.append(
            "VECTORS "
            f"count={vectors.get('vector_count')} "
            f"event_groups={vectors.get('event_group_count')} "
            f"drive={vectors.get('drive_vector_count')} "
            f"compare={vectors.get('compare_vector_count')} "
            f"tri_state={vectors.get('tri_state_vector_count')}"
        )
    timing = report.get("timing") or {}
    lines.append(f"TIMING tick_hz={timing.get('tick_hz')} time_scale={timing.get('time_scale')}")
    capability = report.get("capability") or {}
    candidates = capability.get("backend_candidates") or {}
    rate_candidate = candidates.get("rate-vector") or {}
    if rate_candidate:
        issues = rate_candidate.get("streaming_poc_timing_issues") or []
        lines.append(
            "RATE_VECTOR_STREAMING_POC "
            f"compatible={rate_candidate.get('streaming_poc_compatible')} issues={len(issues)}"
        )
        for issue in issues:
            if isinstance(issue, dict):
                lines.append(f"RATE_VECTOR_TIMING {issue.get('message')}")
    assessments = report.get("runtime_capacity_assessments") or {}
    shown_assessments: list[str] = []
    for name in ("rate-vector", selection.get("mode_emitted"), selection.get("mode_requested")):
        if name and name in assessments and name not in shown_assessments:
            shown_assessments.append(name)
    for name in shown_assessments:
        lines.append(f"RUNTIME_CAPACITY_BACKEND {name}")
        lines.extend(render_runtime_capacity_observation(assessments[name]).splitlines())
    for item in (report.get("anomalies") or [])[:12]:
        location = f" line={item.get('line')}" if item.get("line") is not None else ""
        lines.append(f"{str(item.get('severity', '')).upper()} {item.get('code')}{location}: {item.get('message')}")
    return "\n".join(lines)


def write_source_analysis_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
