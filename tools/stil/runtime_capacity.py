"""Host-side runtime capacity profiles for pattern admission.

The binary format can be much larger than the SRAM expanded-event buffers.
Keep those limits explicit so source analysis and send-time preflight agree
with the firmware build that is going to consume the pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stpg.constants import (
    CAP_PRACTICAL_STREAMING,
    CAP_STREAM_FAIL_STOP,
    STPG_MAX_PATTERN_BYTES,
    STPG_MAX_EVENT_COUNT,
    STPG_FAIL_LOG_MAX,
    STPG_PSRAM_PATTERN_BASE,
    STPG_PSRAM_PATTERN_BYTES,
    STPG_PSRAM_RESULT_BASE,
    STPG_PSRAM_RESULT_BYTES,
    STPG_PSRAM_STAGING_BASE,
    STPG_PSRAM_STAGING_BYTES,
    STPG_PSRAM_LEGACY_PATTERN_BASE,
    STPG_PSRAM_LEGACY_PATTERN_BYTES,
    STPG_PSRAM_LEGACY_RESULT_BASE,
    STPG_PSRAM_LEGACY_RESULT_BYTES,
    STPG_PSRAM_LEGACY_STAGING_BASE,
    STPG_PSRAM_LEGACY_STAGING_BYTES,
    STPG_PSRAM_5M2M_PATTERN_BASE,
    STPG_PSRAM_5M2M_PATTERN_BYTES,
    STPG_PSRAM_5M2M_RESULT_BASE,
    STPG_PSRAM_5M2M_RESULT_BYTES,
    STPG_PSRAM_5M2M_STAGING_BASE,
    STPG_PSRAM_5M2M_STAGING_BYTES,
)


@dataclass(frozen=True)
class RuntimeCapacityProfile:
    """Capacity contract for one known firmware/runtime configuration."""

    name: str
    full_expansion_event_limit: int
    streaming_event_limit: int
    streaming_sample_limit: int
    streaming_compare_limit: int
    psram_pattern_bytes: int = STPG_PSRAM_PATTERN_BYTES
    direct_pattern_bytes: int = STPG_MAX_PATTERN_BYTES
    streaming_compare_free_only: bool = False
    supported_stream_modes: tuple[str, ...] = ("rate-vector", "macro-stream", "macro-p2")
    required_capability: int = 0
    fail_log_entry_limit: int = STPG_FAIL_LOG_MAX
    psram_pattern_base: int = STPG_PSRAM_PATTERN_BASE
    psram_result_base: int = STPG_PSRAM_RESULT_BASE
    psram_result_bytes: int = STPG_PSRAM_RESULT_BYTES
    psram_staging_base: int = STPG_PSRAM_STAGING_BASE
    psram_staging_bytes: int = STPG_PSRAM_STAGING_BYTES


# The fail-stop image streams expanded events from PSRAM while retaining
# sample/compare metadata in bounded SRAM. Its event frontier is uint32-bounded;
# the 6551 SRAM limit applies to direct expansion and retained sample metadata,
# not to the total streaming event count. Keep the name stable so package
# metadata can bind a pattern to that exact contract.
STREAMING_FAIL_STOP_SRAM_PROFILE = "streaming-fail-stop-sram-5m2m"


RUNTIME_CAPACITY_PROFILES: dict[str, RuntimeCapacityProfile] = {
    "current": RuntimeCapacityProfile(
        name="current",
        full_expansion_event_limit=STPG_MAX_EVENT_COUNT,
        streaming_event_limit=STPG_MAX_EVENT_COUNT,
        streaming_sample_limit=STPG_MAX_EVENT_COUNT,
        streaming_compare_limit=STPG_MAX_EVENT_COUNT,
        direct_pattern_bytes=STPG_MAX_PATTERN_BYTES,
    ),
    "f57-ring6": RuntimeCapacityProfile(
        name="f57-ring6",
        full_expansion_event_limit=STPG_MAX_EVENT_COUNT,
        streaming_event_limit=40960,
        streaming_sample_limit=STPG_MAX_EVENT_COUNT,
        streaming_compare_limit=STPG_MAX_EVENT_COUNT,
        direct_pattern_bytes=STPG_MAX_PATTERN_BYTES,
        streaming_compare_free_only=True,
    ),
    "psram-capacity-compare-free": RuntimeCapacityProfile(
        name="psram-capacity-compare-free",
        full_expansion_event_limit=STPG_MAX_EVENT_COUNT,
        streaming_event_limit=0xFFFFFFFF,
        streaming_sample_limit=STPG_MAX_EVENT_COUNT,
        streaming_compare_limit=STPG_MAX_EVENT_COUNT,
        direct_pattern_bytes=STPG_MAX_PATTERN_BYTES,
        streaming_compare_free_only=True,
    ),
    "psram-compare-sample-streaming": RuntimeCapacityProfile(
        name="psram-compare-sample-streaming",
        full_expansion_event_limit=STPG_MAX_EVENT_COUNT,
        streaming_event_limit=0xFFFFFFFF,
        streaming_sample_limit=STPG_PSRAM_LEGACY_RESULT_BYTES // 4,
        # Compare metadata is rebuilt one output chunk at a time. It is not
        # retained for the whole run; only bounded per-chunk scratch and the
        # finite fail log remain in firmware.
        streaming_compare_limit=0xFFFFFFFF,
        streaming_compare_free_only=False,
        supported_stream_modes=("rate-vector",),
        direct_pattern_bytes=STPG_MAX_PATTERN_BYTES,
        psram_pattern_base=STPG_PSRAM_LEGACY_PATTERN_BASE,
        psram_pattern_bytes=STPG_PSRAM_LEGACY_PATTERN_BYTES,
        psram_result_base=STPG_PSRAM_LEGACY_RESULT_BASE,
        psram_result_bytes=STPG_PSRAM_LEGACY_RESULT_BYTES,
        psram_staging_base=STPG_PSRAM_LEGACY_STAGING_BASE,
        psram_staging_bytes=STPG_PSRAM_LEGACY_STAGING_BYTES,
    ),
    "psram-compare-sample-streaming-practical": RuntimeCapacityProfile(
        name="psram-compare-sample-streaming-practical",
        full_expansion_event_limit=STPG_MAX_EVENT_COUNT,
        streaming_event_limit=0xFFFFFFFF,
        streaming_sample_limit=STPG_PSRAM_RESULT_BYTES // 4,
        streaming_compare_limit=0xFFFFFFFF,
        streaming_compare_free_only=False,
        supported_stream_modes=("rate-vector",),
        direct_pattern_bytes=STPG_MAX_PATTERN_BYTES,
        required_capability=CAP_PRACTICAL_STREAMING,
    ),
    "psram-compare-sample-streaming-practical-5m2m": RuntimeCapacityProfile(
        name="psram-compare-sample-streaming-practical-5m2m",
        full_expansion_event_limit=STPG_MAX_EVENT_COUNT,
        streaming_event_limit=0xFFFFFFFF,
        streaming_sample_limit=STPG_PSRAM_5M2M_RESULT_BYTES // 4,
        streaming_compare_limit=0xFFFFFFFF,
        streaming_compare_free_only=False,
        supported_stream_modes=("rate-vector",),
        required_capability=CAP_PRACTICAL_STREAMING,
        psram_pattern_base=STPG_PSRAM_5M2M_PATTERN_BASE,
        psram_pattern_bytes=STPG_PSRAM_5M2M_PATTERN_BYTES,
        psram_result_base=STPG_PSRAM_5M2M_RESULT_BASE,
        psram_result_bytes=STPG_PSRAM_5M2M_RESULT_BYTES,
        psram_staging_base=STPG_PSRAM_5M2M_STAGING_BASE,
        psram_staging_bytes=STPG_PSRAM_5M2M_STAGING_BYTES,
    ),
    STREAMING_FAIL_STOP_SRAM_PROFILE: RuntimeCapacityProfile(
        name=STREAMING_FAIL_STOP_SRAM_PROFILE,
        full_expansion_event_limit=STPG_MAX_EVENT_COUNT,
        streaming_event_limit=0xFFFFFFFF,
        streaming_sample_limit=STPG_MAX_EVENT_COUNT,
        streaming_compare_limit=0xFFFFFFFF,
        streaming_compare_free_only=False,
        supported_stream_modes=("rate-vector",),
        required_capability=CAP_STREAM_FAIL_STOP,
        psram_pattern_base=STPG_PSRAM_5M2M_PATTERN_BASE,
        psram_pattern_bytes=STPG_PSRAM_5M2M_PATTERN_BYTES,
        psram_result_base=STPG_PSRAM_5M2M_RESULT_BASE,
        psram_result_bytes=STPG_PSRAM_5M2M_RESULT_BYTES,
        psram_staging_base=STPG_PSRAM_5M2M_STAGING_BASE,
        psram_staging_bytes=STPG_PSRAM_5M2M_STAGING_BYTES,
    ),
}

# The current source/build contract is the named 5M2M layout. Keep the
# profile name explicit so a legacy 6M/1M image cannot be mistaken for it.
DEFAULT_RUNTIME_PROFILE = "psram-compare-sample-streaming-practical-5m2m"
_UNBOUNDED_LIMIT = 0xFFFFFFFF


def get_runtime_capacity_profile(name: str = DEFAULT_RUNTIME_PROFILE) -> RuntimeCapacityProfile:
    try:
        return RUNTIME_CAPACITY_PROFILES[name]
    except KeyError as exc:
        choices = ", ".join(sorted(RUNTIME_CAPACITY_PROFILES))
        raise ValueError(f"unknown runtime profile {name!r}; choose one of: {choices}") from exc


def runtime_capacity_contract(profile: RuntimeCapacityProfile) -> dict[str, Any]:
    return {
        "name": profile.name,
        "psram_pattern_bytes": profile.psram_pattern_bytes,
        "psram_pattern_base": profile.psram_pattern_base,
        "psram_result_base": profile.psram_result_base,
        "psram_result_bytes": profile.psram_result_bytes,
        "psram_staging_base": profile.psram_staging_base,
        "psram_staging_bytes": profile.psram_staging_bytes,
        "direct_pattern_bytes": profile.direct_pattern_bytes,
        "full_expansion_event_limit": profile.full_expansion_event_limit,
        "streaming_event_limit": profile.streaming_event_limit,
        "streaming_sample_limit": profile.streaming_sample_limit,
        "streaming_compare_limit": profile.streaming_compare_limit,
        "streaming_compare_free_only": profile.streaming_compare_free_only,
        "supported_stream_modes": list(profile.supported_stream_modes),
        "required_capability": profile.required_capability,
        "fail_log_entry_limit": profile.fail_log_entry_limit,
    }


def _requirement_counts(requirements: Any) -> tuple[str, int, bool, bool, int, int]:
    mode = str(getattr(requirements, "mode", ""))
    event_count = int(getattr(requirements, "estimated_event_count", 0))
    requires_sample = bool(getattr(requirements, "requires_sample", False))
    requires_compare = bool(getattr(requirements, "requires_compare", False))
    sample_count_value = getattr(requirements, "estimated_sample_count", None)
    compare_count_value = getattr(requirements, "estimated_compare_count", None)
    sample_count = (
        event_count if sample_count_value is None and requires_sample else int(sample_count_value or 0)
    )
    compare_count = (
        event_count if compare_count_value is None and requires_compare else int(compare_count_value or 0)
    )
    return mode, event_count, requires_sample, requires_compare, sample_count, compare_count


def _capacity_metric(
    value: int | None,
    limit: int | None,
    *,
    required: bool = True,
    scope: str,
    not_applicable_status: str = "not_applicable",
) -> dict[str, Any]:
    if value is None:
        return {
            "value": None,
            "limit": limit,
            "remaining": None,
            "utilization_percent": None,
            "status": "unknown",
            "scope": scope,
        }
    if not required:
        return {
            "value": value,
            "limit": limit,
            "remaining": None,
            "utilization_percent": None,
            "status": not_applicable_status,
            "scope": scope,
        }
    if limit is None or limit >= _UNBOUNDED_LIMIT:
        return {
            "value": value,
            "limit": limit,
            "remaining": None,
            "utilization_percent": None,
            "status": "not_bounded",
            "scope": scope,
        }
    remaining = limit - value
    return {
        "value": value,
        "limit": limit,
        "remaining": remaining,
        "utilization_percent": round((value * 100.0) / limit, 3) if limit else None,
        "status": "exceeds_limit" if value > limit else ("at_limit" if value == limit else "within_limit"),
        "scope": scope,
    }


def runtime_capacity_observation(
    requirements: Any,
    *,
    pattern_size_bytes: int | None,
    profile: RuntimeCapacityProfile,
) -> dict[str, Any]:
    """Return explanatory capacity metrics without performing device I/O.

    The observation is deliberately richer than :func:`assess_runtime_capacity`.
    It is suitable for source-analysis and package reports, including patterns
    that fit exactly at a limit.  ``sample_count`` is the number of output
    sample words (one per sample-bearing time group and emitted repeat), while
    ``compare_count`` is the number of effective compared sample groups after
    the expected-mask intersection.  Neither is a pin count or a STIL line
    count.
    """

    mode, event_count, requires_sample, requires_compare, sample_count, compare_count = (
        _requirement_counts(requirements)
    )
    streaming_required = event_count > profile.full_expansion_event_limit
    supported_format = mode in {"rate-vector", "macro-stream", "macro-p2"}
    execution_path = "streaming" if streaming_required else "direct"
    if not supported_format:
        execution_path = "unsupported"

    blocking_codes: list[str] = []
    if pattern_size_bytes is not None and pattern_size_bytes > profile.psram_pattern_bytes:
        blocking_codes.append("ERR_PSRAM_PATTERN_BYTES_LIMIT")
    if (
        pattern_size_bytes is not None
        and mode in {"raw-event", "macro-stream", "macro-p2"}
        and pattern_size_bytes > profile.direct_pattern_bytes
    ):
        blocking_codes.append("ERR_RUNTIME_DIRECT_BYTES_LIMIT")
    if mode not in {"rate-vector", "macro-stream", "macro-p2"}:
        if event_count > profile.full_expansion_event_limit:
            blocking_codes.append("ERR_RUNTIME_EVENT_LIMIT")
    elif streaming_required:
        if mode not in profile.supported_stream_modes:
            blocking_codes.append("ERR_RUNTIME_STREAM_MODE_UNSUPPORTED")
        if event_count > profile.streaming_event_limit:
            blocking_codes.append("ERR_RUNTIME_STREAM_EVENT_LIMIT")
        if requires_sample and sample_count > profile.streaming_sample_limit:
            blocking_codes.append("ERR_RUNTIME_STREAM_SAMPLE_LIMIT")
        if requires_compare and compare_count > profile.streaming_compare_limit:
            blocking_codes.append("ERR_RUNTIME_STREAM_COMPARE_LIMIT")
        if profile.streaming_compare_free_only and (requires_sample or requires_compare):
            blocking_codes.append("ERR_RUNTIME_STREAM_COMPARE_FREE_ONLY")

    event_limit = (
        profile.streaming_event_limit if streaming_required else profile.full_expansion_event_limit
    )
    metrics = {
        "pattern_bytes": _capacity_metric(
            pattern_size_bytes,
            profile.psram_pattern_bytes,
            scope="psram_pattern_region",
        ),
        "event_count": _capacity_metric(
            event_count,
            event_limit,
            scope="streaming_event_limit" if streaming_required else "full_expansion_event_limit",
        ),
        "sample_count": _capacity_metric(
            sample_count,
            profile.streaming_sample_limit,
            required=requires_sample and streaming_required,
            scope="streaming_sample_result_region",
            not_applicable_status="not_applicable_direct" if not streaming_required else "not_required",
        ),
        "compare_count": _capacity_metric(
            compare_count,
            profile.streaming_compare_limit,
            required=requires_compare and streaming_required,
            scope="streaming_compare_metadata",
            not_applicable_status="not_applicable_direct" if not streaming_required else "not_required",
        ),
    }
    mismatch_log = {
        "possible_mismatch_entries": compare_count,
        "capacity": profile.fail_log_entry_limit,
        "remaining_by_count": profile.fail_log_entry_limit - compare_count,
        "status": (
            "may_overflow_if_mismatches"
            if compare_count > profile.fail_log_entry_limit
            else "cannot_overflow_by_compare_count"
        ),
        "scope": "stored_fail_log",
    }
    boundary_metrics = [
        name for name in ("pattern_bytes", "event_count", "sample_count", "compare_count")
        if metrics[name]["status"] == "at_limit"
    ]
    status = "REJECT" if blocking_codes else ("REVIEW" if boundary_metrics else "PASS")
    return {
        "status": status,
        "runtime_profile": runtime_capacity_contract(profile),
        "mode": mode,
        "execution_path": execution_path,
        "streaming_required": streaming_required,
        "streaming_mode_supported": mode in profile.supported_stream_modes,
        "requires_sample": requires_sample,
        "requires_compare": requires_compare,
        "metrics": metrics,
        "result_reporting": {"mismatch_log": mismatch_log},
        "blocking_codes": blocking_codes,
        "boundary_metrics": boundary_metrics,
        "definitions": {
            "sample_count": (
                "sample words emitted: one per sample-bearing time group per emitted vector repeat; "
                "a group may cover multiple sample pins"
            ),
            "compare_count": (
                "effective compared sample groups: sample group mask intersected with the vector expect mask"
            ),
        },
        "next_action": (
            "reduce sample-bearing groups or use a firmware/result-storage candidate"
            if "ERR_RUNTIME_STREAM_SAMPLE_LIMIT" in blocking_codes
            else "reduce effective compare groups or use a profile with compare capacity"
            if "ERR_RUNTIME_STREAM_COMPARE_LIMIT" in blocking_codes
            else "review zero remaining headroom before physical execution"
            if boundary_metrics
            else "continue to wait/package validation"
        ),
    }


def render_runtime_capacity_observation(observation: dict[str, Any]) -> str:
    """Render the common capacity observation for human-facing CLI output."""

    lines = [
        "CAPACITY "
        f"status={observation.get('status')} "
        f"profile={(observation.get('runtime_profile') or {}).get('name')} "
        f"path={observation.get('execution_path')} "
        f"streaming_required={observation.get('streaming_required')}"
    ]
    metrics = observation.get("metrics") or {}
    labels = (
        ("pattern_bytes", "PATTERN_BYTES"),
        ("event_count", "EVENT_COUNT"),
        ("sample_count", "SAMPLE_COUNT"),
        ("compare_count", "COMPARE_COUNT"),
    )
    for key, label in labels:
        metric = metrics.get(key) or {}
        lines.append(
            f"CAPACITY_{label} "
            f"value={metric.get('value')} limit={metric.get('limit')} "
            f"remaining={metric.get('remaining')} "
            f"utilization_percent={metric.get('utilization_percent')} "
            f"status={metric.get('status')} scope={metric.get('scope')}"
        )
    mismatch_log = (observation.get("result_reporting") or {}).get("mismatch_log") or {}
    if mismatch_log:
        lines.append(
            "CAPACITY_FAIL_LOG "
            f"possible_mismatch_entries={mismatch_log.get('possible_mismatch_entries')} "
            f"capacity={mismatch_log.get('capacity')} "
            f"remaining_by_count={mismatch_log.get('remaining_by_count')} "
            f"status={mismatch_log.get('status')} scope={mismatch_log.get('scope')}"
        )
    if observation.get("blocking_codes"):
        lines.append("CAPACITY_BLOCKING_CODES " + ",".join(observation["blocking_codes"]))
    if observation.get("boundary_metrics"):
        lines.append("CAPACITY_BOUNDARY_METRICS " + ",".join(observation["boundary_metrics"]))
    lines.append(f"CAPACITY_NEXT_ACTION {observation.get('next_action')}")
    return "\n".join(lines)


def assess_runtime_capacity(
    requirements: Any,
    *,
    pattern_size_bytes: int | None,
    profile: RuntimeCapacityProfile,
) -> list[dict[str, Any]]:
    """Return blocking diagnostics for a compiled pattern and profile.

    A rate-vector beyond the full-expansion limit requires streaming. The
    practical bounded Rate Vector profile can keep the output ring in SRAM,
    capture samples in the PSRAM result region, and rebuild compare metadata
    per chunk. Other profiles and unsupported formats remain fail-closed;
    a byte-size fit alone never proves that a device can execute the pattern.
    """

    diagnostics: list[dict[str, Any]] = []
    mode, event_count, requires_sample, requires_compare, sample_count, compare_count = (
        _requirement_counts(requirements)
    )
    context = {
        "runtime_profile": runtime_capacity_contract(profile),
        "mode": mode,
        "estimated_event_count": event_count,
        "requires_sample": requires_sample,
        "requires_compare": requires_compare,
        "estimated_sample_count": sample_count,
        "estimated_compare_count": compare_count,
        "pattern_size_bytes": pattern_size_bytes,
    }

    if pattern_size_bytes is not None and pattern_size_bytes > profile.psram_pattern_bytes:
        diagnostics.append(
            {
                "severity": "error",
                "code": "ERR_PSRAM_PATTERN_BYTES_LIMIT",
                "message": (
                    f"compiled {mode} pattern is {pattern_size_bytes} bytes, exceeding the "
                    f"PSRAM pattern region limit {profile.psram_pattern_bytes}"
                ),
                "context": {**context, "limit": profile.psram_pattern_bytes},
            }
        )

    # Raw and macro streams are materialized into the bounded SRAM pattern
    # buffer by the current firmware before direct execution.  A PSRAM byte
    # fit is therefore not sufficient for those formats.  Rate Vector is the
    # only format whose oversized path reads compressed records from PSRAM.
    if (
        pattern_size_bytes is not None
        and mode in {"raw-event", "macro-stream", "macro-p2"}
        and pattern_size_bytes > profile.direct_pattern_bytes
    ):
        diagnostics.append(
            {
                "severity": "error",
                "code": "ERR_RUNTIME_DIRECT_BYTES_LIMIT",
                "message": (
                    f"{mode} pattern is {pattern_size_bytes} bytes, exceeding the "
                    f"direct-execution format limit {profile.direct_pattern_bytes}; "
                    "only compressed Rate Vector has an oversized PSRAM streaming path"
                ),
                "context": {**context, "limit": profile.direct_pattern_bytes},
            }
        )

    if mode not in {"rate-vector", "macro-stream", "macro-p2"}:
        if event_count > profile.full_expansion_event_limit:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "ERR_RUNTIME_EVENT_LIMIT",
                    "message": (
                        f"{mode} pattern has {event_count} events, exceeding the full-expansion "
                        f"limit {profile.full_expansion_event_limit}"
                    ),
                    "context": {**context, "limit": profile.full_expansion_event_limit},
                }
            )
        return diagnostics

    streaming_required = event_count > profile.full_expansion_event_limit
    if not streaming_required:
        return diagnostics

    if mode not in profile.supported_stream_modes:
        diagnostics.append(
            {
                "severity": "error",
                "code": "ERR_RUNTIME_STREAM_MODE_UNSUPPORTED",
                "message": (
                    f"runtime profile {profile.name} does not implement oversized "
                    f"streaming for {mode}; supported formats: "
                    f"{', '.join(profile.supported_stream_modes)}"
                ),
                "context": {
                    **context,
                    "streaming_required": True,
                    "supported_stream_modes": list(profile.supported_stream_modes),
                },
            }
        )
        return diagnostics

    if event_count > profile.streaming_event_limit:
        diagnostics.append(
            {
                "severity": "error",
                "code": "ERR_RUNTIME_STREAM_EVENT_LIMIT",
                "message": (
                    f"{mode} pattern requires streaming with {event_count} emitted events, "
                    f"exceeding the {profile.name} streaming limit {profile.streaming_event_limit}"
                ),
                "context": {**context, "limit": profile.streaming_event_limit, "streaming_required": True},
            }
        )

    if requires_sample and sample_count > profile.streaming_sample_limit:
        diagnostics.append(
            {
                "severity": "error",
                "code": "ERR_RUNTIME_STREAM_SAMPLE_LIMIT",
                "message": (
                    f"sample-bearing streaming pattern has {sample_count} sample events, "
                    f"exceeding the sample storage limit {profile.streaming_sample_limit}"
                ),
                "context": {**context, "limit": profile.streaming_sample_limit, "streaming_required": True},
            }
        )

    if requires_compare and compare_count > profile.streaming_compare_limit:
        diagnostics.append(
            {
                "severity": "error",
                "code": "ERR_RUNTIME_STREAM_COMPARE_LIMIT",
                "message": (
                    f"compare-bearing streaming pattern has {compare_count} compare events, "
                    f"exceeding the compare metadata limit {profile.streaming_compare_limit}"
                ),
                "context": {**context, "limit": profile.streaming_compare_limit, "streaming_required": True},
            }
        )

    if profile.streaming_compare_free_only and (requires_sample or requires_compare):
        diagnostics.append(
            {
                "severity": "error",
                "code": "ERR_RUNTIME_STREAM_COMPARE_FREE_ONLY",
                "message": (
                    f"runtime profile {profile.name} admits oversized streaming only for "
                    "compare-free patterns"
                ),
                "context": {**context, "streaming_required": True},
            }
        )
    return diagnostics
