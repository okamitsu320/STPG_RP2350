"""Pure-file selection of single versus capacity-aware runner packages.

This is not an acquisition/retry policy. Unknown compile errors and wait
failures never authorize fallback. Final packages retain all existing gates.
"""

from __future__ import annotations

from typing import Any

from .artifacts import RunnerError
from stil.source_analysis import source_rejection  # public compatibility re-export
from stil.backend_rate import _is_loop_block_capacity_error

DEFAULT_SEGMENT_VECTORS = 100_000
CAPACITY_CODES = frozenset({
    "ERR_PSRAM_PATTERN_BYTES_LIMIT", "ERR_RUNTIME_STREAM_EVENT_LIMIT",
    "ERR_RUNTIME_STREAM_SAMPLE_LIMIT", "ERR_RUNTIME_STREAM_COMPARE_LIMIT",
    "ERR_RUNTIME_DIRECT_BYTES_LIMIT", "ERR_RUNTIME_EVENT_LIMIT",
    "ERR_TOO_MANY_LOOP_BLOCKS",
})
_CAPACITY_ERRORS = {
    "rate-vector": {
        "rate vector exceeds PSRAM pattern region": "ERR_PSRAM_PATTERN_BYTES_LIMIT",
        "wide rate vector exceeds PSRAM pattern region": "ERR_PSRAM_PATTERN_BYTES_LIMIT",
        "rate vector exceeds the runtime event limit": "ERR_RUNTIME_STREAM_EVENT_LIMIT",
        "rate vector exceeds the PSRAM result region": "ERR_RUNTIME_STREAM_SAMPLE_LIMIT",
        "rate vector exceeds the runtime compare limit": "ERR_RUNTIME_STREAM_COMPARE_LIMIT",
    },
    # Exact encoder messages, not an arbitrary 'out of range' substring.
    "raw-event": {"event_count out of range": "ERR_RUNTIME_STREAM_EVENT_LIMIT"},
    "macro-stream": {"macro-stream exceeds Stage A size": "ERR_RUNTIME_DIRECT_BYTES_LIMIT"},
}
_WRAPPERS = {"ERR_REQUESTED_BACKEND_UNAVAILABLE", "ERR_NO_BACKEND_AVAILABLE"}


def capacity_reasons(report: dict[str, Any], mode: str) -> list[str]:
    """A conservative allowlist: capacity must be the only global error class."""
    if report.get("status") not in {"PASS", "REVIEW", "REJECT"}:
        return []
    errors = [item for item in report.get("anomalies", []) if item.get("severity") == "error"]
    if ((report["status"] == "REJECT" and not errors)
            or any(item.get("code") not in CAPACITY_CODES | _WRAPPERS for item in errors)):
        return []
    backend = "rate-vector" if mode == "auto" else mode
    candidate = report.get("capability", {}).get("backend_candidates", {}).get(backend, {})
    if candidate.get("status") == "available":
        blocking = candidate.get("runtime_capacity_assessment", {}).get("blocking_codes", [])
        # The Loop-block limit is an unavailable typed encoder leaf, never a
        # runtime-capacity observation on an already available binary.
        if blocking and all(code in CAPACITY_CODES - {"ERR_TOO_MANY_LOOP_BLOCKS"} for code in blocking):
            return sorted(set(blocking))
    elif candidate.get("status") == "unavailable" and report["status"] == "REJECT":
        if (backend == "rate-vector" and candidate.get("loop_block_capacity") is True
                and _is_loop_block_capacity_error(candidate.get("error_code"), candidate.get("error"))):
            return ["ERR_TOO_MANY_LOOP_BLOCKS"]
        code = _CAPACITY_ERRORS.get(backend, {}).get(candidate.get("error"))
        if code and candidate.get("error_code") == "ERR_BACKEND_VALIDATION":
            return [code]
    return []


def segmentation_decision(settings: dict[str, Any], report: dict[str, Any], *, legacy: bool = False) -> dict[str, Any]:
    """Plan from the saved whole-source analysis; never from a failed RUN."""
    policy = settings["segmentation"]
    mode = policy["mode"]
    codes = capacity_reasons(report, settings["compile"]["mode"])
    if codes and mode == "single":
        raise RunnerError(source_rejection(report))
    if report.get("status") not in {"PASS", "REVIEW"} and not (mode == "auto" and codes):
        raise RunnerError(source_rejection(report))
    vectors = report.get("vectors", {})
    count = vectors.get("logical_vector_count", vectors.get("vector_count"))
    if type(count) is not int or count < 1:
        raise RunnerError("source analysis has no positive logical vector count")
    requested = policy["max_vectors"]
    cause = "capacity" if codes else "vector_limit" if requested is not None and count > requested else "none"
    cap = None
    capacity_first = not legacy and cause == "capacity" and requested is None
    if cause != "none":
        if count < 2:
            raise RunnerError("capacity-aware segmentation cannot split one logical vector")
        # build_segment_ranges needs at least two ranges, even when an explicit
        # upper bound exceeds the whole source. The planner may shorten further.
        if not capacity_first:
            cap = min(requested or DEFAULT_SEGMENT_VECTORS, count - 1)
    decision = {
        "kind": "stpg_runner_segmentation_decision", "schema_version": 1 if legacy else 2,
        "policy": dict(policy), "source_vector_count": count,
        "initial_analysis_status": report["status"], "cause": cause,
        "capacity_codes": codes, "planning_max_vectors": cap,
        "package_kind": "segmented" if cause != "none" else "single",
        "physical_execution_authorized": False,
    }
    if not legacy:
        decision.update(planning_strategy="capacity-first" if capacity_first else "explicit-cap" if cap else "single",
                        initial_probe_vectors=DEFAULT_SEGMENT_VECTORS if capacity_first else None)
    return decision
