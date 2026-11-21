"""Bounded host-only acceptance of a declarative wait schedule."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .runtime_wait_model import (
    _RuntimeModelCache,
    _map_has_loop_provenance,
    RuntimeWaitModelError,
    build_runtime_model,
    simulate_explicit_waits,
)
from .wait_schedule import WaitScheduleError, _stored_map_digest, validate_wait_schedule
from .json_artifacts import JsonArtifactError, read_json_artifact
from stpg.pattern import LoopRefRecord, unpack_rate_vector_full


WAIT_ACCEPTANCE_KIND = "stpg_wait_schedule_acceptance"
WAIT_ACCEPTANCE_VERSION = "v1"
SERVICE_PROFILE_SCOPES = ("non-loop", "loop-generic")


def _has_loop_provenance(
    pattern_path: Path, map_path: Path | None, *, _model_cache: _RuntimeModelCache | None = None,
) -> bool:
    """Detect structural Loop provenance, including a split plain iteration."""

    try:
        pattern_bytes = pattern_path.read_bytes()
        decoded = (
            _model_cache.decode(pattern_bytes) if _model_cache is not None
            else unpack_rate_vector_full(pattern_bytes)
        )
        entries = decoded[3]
    except (OSError, ValueError):
        entries = ()
    if any(isinstance(entry, LoopRefRecord) for entry in entries):
        return True
    if map_path is None:
        return False
    try:
        map_digest = _stored_map_digest(map_path)
        cached = _model_cache.map_loop_provenance(map_digest) if _model_cache is not None else None
        map_data = read_json_artifact(map_path) if cached is None else None
        if _stored_map_digest(map_path) != map_digest:
            raise WaitScheduleError("source map changed while checking Loop provenance")
    except (OSError, UnicodeDecodeError, JsonArtifactError, json.JSONDecodeError):
        return False
    if cached is not None:
        return cached
    if _model_cache is not None and isinstance(map_data, dict):
        _model_cache.remember_map_summary(map_digest, map_data)
    return _map_has_loop_provenance(map_data)


def _wait_decision_status(
    *,
    acceptance_status: str,
    wait_count: int,
    baseline_underflow: list[int],
    candidate_underflow: list[int],
) -> str:
    """Return an operator-facing wait decision without changing legacy status.

    ``status`` remains ``ACCEPTED``/``REJECTED`` for compatibility with the
    original Phase 3 contract.  This second status makes the important
    distinction between a missing service profile, a genuinely unnecessary
    wait, and a rejected wait candidate visible to humans and matrix tools.
    """

    if acceptance_status == "REQUIRES_SERVICE_PROFILE":
        return "WAIT_UNDETERMINED"
    if acceptance_status != "ACCEPTED":
        return "WAIT_REJECTED"
    if wait_count == 0:
        return "WAIT_REQUIRED" if baseline_underflow else "WAIT_NOT_REQUIRED"
    if candidate_underflow:
        return "WAIT_REJECTED"
    return "WAIT_ACCEPTED"


def _load_schedule(path: Path) -> dict[str, Any]:
    try:
        value = read_json_artifact(path)
    except (OSError, UnicodeDecodeError, JsonArtifactError, json.JSONDecodeError) as exc:
        raise WaitScheduleError(f"cannot read schedule JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WaitScheduleError("schedule JSON root must be an object")
    return value


def _error_report(
    *,
    status: str,
    pattern_path: Path,
    schedule_path: Path,
    errors: list[str],
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "kind": WAIT_ACCEPTANCE_KIND,
        "schema_version": WAIT_ACCEPTANCE_VERSION,
        "status": status,
        "scope": "bounded_host_model_only",
        "input": {"pattern_path": str(pattern_path), "schedule_path": str(schedule_path)},
        "errors": errors,
        "warnings": [],
        "wait_decision": {
            "status": "WAIT_INVALID" if status == "REJECTED" else "WAIT_UNDETERMINED",
            "reason": "schedule validation or required model inputs are unavailable",
        },
        "physical_execution_authorized": False,
        "steady_tfill": {
            "status": "not_emitted",
            "reason": "host-only acceptance has no physical trace coverage or steady-data measurement",
        },
        "limitations": [
            "this status does not establish a universal service time, Tfill, timing, throughput, or robustness",
            "this report does not mutate a pattern and is not physical approval",
        ],
    }
    if validation is not None:
        report["schedule_validation"] = validation
    return report


def evaluate_wait_schedule(
    pattern_path: Path,
    schedule_path: Path,
    *,
    map_path: Path | None = None,
    service_time_us: float | None = None,
    service_time_source: str = "",
    safety_margin_us: float = 0.0,
    max_wait_us: float | None = None,
    max_total_wait_us: float | None = None,
    max_wait_count: int | None = None,
    pattern_time_limit_us: float | None = None,
    max_rates: int | None = None,
    service_profile_scope: str = "non-loop",
    _model_cache: _RuntimeModelCache | None = None,
) -> dict[str, Any]:
    """Evaluate one schedule against a declared service-time model.

    A schedule is accepted only when the candidate simulation has no modeled
    underflow pressure and all explicitly supplied limits hold.  Missing
    service time is intentionally a non-accepting result.
    """

    cache = _model_cache if _model_cache is not None else _RuntimeModelCache()
    validation = validate_wait_schedule(
        pattern_path, schedule_path, map_path=map_path, max_rates=max_rates, _model_cache=cache
    )
    if validation["status"] != "PASS":
        return _error_report(
            status="REJECTED",
            pattern_path=pattern_path,
            schedule_path=schedule_path,
            errors=["schedule validation failed"],
            validation=validation,
        )
    if service_time_us is None:
        return _error_report(
            status="REQUIRES_SERVICE_PROFILE",
            pattern_path=pattern_path,
            schedule_path=schedule_path,
            errors=["service_time_us must be supplied before model acceptance"],
            validation=validation,
        )
    if (
        isinstance(service_time_us, bool)
        or not isinstance(service_time_us, (int, float))
        or not math.isfinite(float(service_time_us))
        or service_time_us <= 0
    ):
        return _error_report(
            status="REJECTED",
            pattern_path=pattern_path,
            schedule_path=schedule_path,
            errors=["service_time_us must be positive"],
            validation=validation,
        )
    if not isinstance(service_time_source, str) or not service_time_source.strip():
        return _error_report(
            status="REJECTED",
            pattern_path=pattern_path,
            schedule_path=schedule_path,
            errors=["service_time_source is required"],
            validation=validation,
        )
    if service_profile_scope not in SERVICE_PROFILE_SCOPES:
        return _error_report(
            status="REJECTED",
            pattern_path=pattern_path,
            schedule_path=schedule_path,
            errors=[
                "service_profile_scope must be one of: "
                + ", ".join(SERVICE_PROFILE_SCOPES)
            ],
            validation=validation,
        )
    try:
        has_loop_provenance = _has_loop_provenance(pattern_path, map_path, _model_cache=cache)
    except WaitScheduleError as exc:
        return _error_report(
            status="REJECTED", pattern_path=pattern_path, schedule_path=schedule_path,
            errors=[str(exc)], validation=validation,
        )
    expected_scope = "loop-generic" if has_loop_provenance else "non-loop"
    if service_profile_scope != expected_scope:
        return _error_report(
            status="REJECTED",
            pattern_path=pattern_path,
            schedule_path=schedule_path,
            errors=[
                f"service profile scope {service_profile_scope!r} does not match "
                f"artifact scope {expected_scope!r}; a Loop artifact requires "
                "an explicitly declared loop-generic profile"
            ],
            validation=validation,
        )
    if (
        isinstance(safety_margin_us, bool)
        or not isinstance(safety_margin_us, (int, float))
        or not math.isfinite(float(safety_margin_us))
        or safety_margin_us < 0
    ):
        return _error_report(
            status="REJECTED",
            pattern_path=pattern_path,
            schedule_path=schedule_path,
            errors=["safety_margin_us must be non-negative"],
            validation=validation,
        )
    for value, label in (
        (max_wait_us, "max_wait_us"),
        (max_total_wait_us, "max_total_wait_us"),
        (pattern_time_limit_us, "pattern_time_limit_us"),
    ):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            return _error_report(
                status="REJECTED",
                pattern_path=pattern_path,
                schedule_path=schedule_path,
                errors=[f"{label} must be positive when supplied"],
                validation=validation,
            )
    if max_wait_count is not None and (
        isinstance(max_wait_count, bool)
        or not isinstance(max_wait_count, int)
        or max_wait_count < 0
    ):
        return _error_report(
            status="REJECTED",
            pattern_path=pattern_path,
            schedule_path=schedule_path,
            errors=["max_wait_count must be non-negative"],
            validation=validation,
        )

    try:
        schedule = _load_schedule(schedule_path)
        model_data = schedule["model"]
        pattern_bytes = pattern_path.read_bytes()
        model = build_runtime_model(
            pattern_bytes,
            chunk_event_capacity=int(model_data["chunk_event_capacity"]),
            ring_slot_count=int(model_data["ring_slot_count"]),
            _model_cache=cache,
        )
        explicit_waits = {
            int(item["before_chunk_index"]): int(item["wait_ticks"])
            for item in schedule["boundaries"]
            if int(item["wait_ticks"]) > 0
        }
        baseline = simulate_explicit_waits(
            model,
            explicit_waits={},
            service_time_us=service_time_us,
            service_time_source=service_time_source,
            safety_margin_us=safety_margin_us,
        )
        candidate = simulate_explicit_waits(
            model,
            explicit_waits=explicit_waits,
            service_time_us=service_time_us,
            service_time_source=service_time_source,
            safety_margin_us=safety_margin_us,
        )
    except (OSError, KeyError, TypeError, ValueError, RuntimeWaitModelError, WaitScheduleError) as exc:
        return _error_report(
            status="REJECTED",
            pattern_path=pattern_path,
            schedule_path=schedule_path,
            errors=[f"cannot evaluate schedule: {exc}"],
            validation=validation,
        )

    tick_hz = int(model["model"]["tick_hz"])
    total_wait_ticks = sum(explicit_waits.values())
    total_wait_us = total_wait_ticks * 1_000_000.0 / tick_hz
    max_single_wait_ticks = max(explicit_waits.values(), default=0)
    max_single_wait_us = max_single_wait_ticks * 1_000_000.0 / tick_hz
    errors: list[str] = []
    warnings: list[str] = []
    if max_wait_us is not None and max_single_wait_us > max_wait_us:
        errors.append(f"max_wait_us exceeded: {max_single_wait_us} > {max_wait_us}")
    if max_total_wait_us is not None and total_wait_us > max_total_wait_us:
        errors.append(f"max_total_wait_us exceeded: {total_wait_us} > {max_total_wait_us}")
    if max_wait_count is not None and len(explicit_waits) > max_wait_count:
        errors.append(f"max_wait_count exceeded: {len(explicit_waits)} > {max_wait_count}")
    total_consume_us = float(model["pattern"]["total_consume_us"])
    if pattern_time_limit_us is not None and total_consume_us + total_wait_us > pattern_time_limit_us:
        errors.append(
            f"pattern_time_limit_us exceeded: {total_consume_us + total_wait_us} > {pattern_time_limit_us}"
        )

    baseline_underflow = list(baseline["simulation"]["underflow_chunks"])
    candidate_underflow = list(candidate["simulation"]["underflow_chunks"])
    if candidate_underflow:
        errors.append(f"candidate retains modeled underflow pressure at chunks {candidate_underflow}")
    if not baseline_underflow and explicit_waits:
        warnings.append("baseline model has no underflow pressure; configured waits are not modeled as necessary")

    status = "ACCEPTED" if not errors else "REJECTED"
    wait_decision = _wait_decision_status(
        acceptance_status=status,
        wait_count=len(explicit_waits),
        baseline_underflow=baseline_underflow,
        candidate_underflow=candidate_underflow,
    )
    return {
        "kind": WAIT_ACCEPTANCE_KIND,
        "schema_version": WAIT_ACCEPTANCE_VERSION,
        "status": status,
        "scope": "bounded_host_model_only",
        "wait_decision": {
            "status": wait_decision,
            "reason": (
                "no explicit waits and no modeled baseline deficit"
                if wait_decision == "WAIT_NOT_REQUIRED"
                else "explicit wait schedule is accepted by the bounded host model"
                if wait_decision == "WAIT_ACCEPTED"
                else "baseline model contains pressure but no wait was supplied"
                if wait_decision == "WAIT_REQUIRED"
                else "the bounded model or its declared limits rejected this schedule"
                if wait_decision == "WAIT_REJECTED"
                else "service profile is required before a wait decision can be made"
            ),
        },
        "input": {
            "pattern_path": str(pattern_path),
            "schedule_path": str(schedule_path),
        },
        "service_profile": candidate["service_time"],
        "service_profile_scope": service_profile_scope,
        "limits": {
            "max_wait_us": max_wait_us,
            "max_total_wait_us": max_total_wait_us,
            "max_wait_count": max_wait_count,
            "pattern_time_limit_us": pattern_time_limit_us,
        },
        "comparison": {
            "baseline_underflow_chunks": baseline_underflow,
            "candidate_underflow_chunks": candidate_underflow,
            "cleared_underflow_chunks": [item for item in baseline_underflow if item not in candidate_underflow],
            "baseline_consumer_end_us": baseline["simulation"]["consumer_end_ticks"] * 1_000_000.0 / tick_hz,
            "candidate_consumer_end_us": candidate["simulation"]["consumer_end_ticks"] * 1_000_000.0 / tick_hz,
        },
        "wait_profile": {
            "wait_count": len(explicit_waits),
            "total_wait_ticks": total_wait_ticks,
            "total_wait_us": total_wait_us,
            "max_single_wait_ticks": max_single_wait_ticks,
            "max_single_wait_us": max_single_wait_us,
            "waits": [
                {"before_chunk_index": index, "wait_ticks": ticks, "wait_us": ticks * 1_000_000.0 / tick_hz}
                for index, ticks in sorted(explicit_waits.items())
            ],
        },
        "schedule_validation": validation,
        "errors": errors,
        "warnings": warnings,
        "steady_tfill": {
            "status": "not_emitted",
            "reason": "host-only model has no physical trace coverage or steady-data measurement",
        },
        "physical_execution_authorized": False,
        "limitations": [
            "service time and safety margin are supplied assumptions, not universal worst cases",
            "no DMA/IRQ/Core1/QMI/XIP timing or physical trace is measured here",
            "ACCEPTED means only that this bounded model has no remaining modeled underflow pressure",
            "pattern binary is not mutated and no physical execution is authorized",
        ],
    }
