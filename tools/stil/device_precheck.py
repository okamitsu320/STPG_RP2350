from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from stpg.constants import (
    CAP_EXPECT_COMPARE,
    CAP_FAIL_STOP,
    CAP_STREAM_FAIL_STOP,
    CAP_STREAM_WAIT_WATCHDOG,
    CAP_PSRAM_STREAMING,
    CAP_RATE_VECTOR_MODE,
    CAP_RUN_16PIN,
    CAP_RUN_24PIN,
    CAP_SAMPLE_CAPTURE,
    CAP_UPLOAD_FLASH,
    CAP_UPLOAD_PSRAM,
    CAP_UPLOAD_SRAM,
    LEGACY_STREAM_STALL_TIMEOUT_US,
    RUN_FLAG_STREAMING_POC,
    RUN_FLAG_FAIL_STOP,
    STORAGE_FLASH,
    STORAGE_PSRAM,
    STORAGE_SRAM,
    STPG_EVF_COMPARE_POINT,
    STPG_EVF_SAMPLE_POINT,
    STPG_MACROP2VEC_MAGIC,
    STPG_MACROVEC_MAGIC,
    STPG_MAX_EVENT_COUNT,
    STPG_PATTERN_MAGIC,
    STPG_RATE_SLOT_DIR_UPDATE,
    STPG_RATE_SLOT_SAMPLE,
    STPG_RATEVEC_MAGIC,
)
from stpg.pattern import (
    LoopRefRecord,
    emitted_event_count_estimate,
    emitted_macro_event_count_estimate,
    emitted_macro_p2_event_count_estimate,
    unpack_macro_p2_vector,
    sample_macro_event_count_estimate,
    sample_macro_p2_event_count_estimate,
    streaming_poc_timing_issues,
    unpack_macro_vector,
    unpack_pattern,
    unpack_rate_vector_full,
)

from .runtime_capacity import (
    DEFAULT_RUNTIME_PROFILE,
    RuntimeCapacityProfile,
    STREAMING_FAIL_STOP_SRAM_PROFILE,
    assess_runtime_capacity,
    get_runtime_capacity_profile,
    render_runtime_capacity_observation,
    runtime_capacity_observation,
)


@dataclass(frozen=True)
class PatternRequirements:
    tick_hz: int
    active_pins: int
    mode: str
    requires_sample: bool
    requires_compare: bool
    estimated_event_count: int
    streaming_poc_incompatibility: str | None
    estimated_sample_count: int = 0
    estimated_compare_count: int = 0
    streaming_poc_timing_issues: tuple[dict[str, int | bool | str], ...] = ()
    reduced_density_candidate: bool = False
    reduced_density_incompatibilities: tuple[str, ...] = ()
    exact_density_opportunities: tuple[str, ...] = ()
    requires_stream_wait_watchdog: bool = False


@dataclass(frozen=True)
class DirectExecutionFit:
    supported: bool
    estimated_event_count: int
    estimated_sample_count: int
    event_limit: int
    sample_limit: int
    reason: str | None = None


def _analyze_reduced_density_candidate(rates, slots, loops) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    reasons: list[str] = []
    if loops:
        reasons.append("loop blocks present")
    for rate_id, rate in enumerate(rates):
        rate_slots = slots[rate.slot_offset : rate.slot_offset + rate.slot_count]
        merged_groups: list[dict[str, int | bool]] = []
        index = 0
        while index < len(rate_slots):
            merged_time = rate_slots[index].time_ticks
            sample_trigger = False
            op_count = 0
            dir_update = False
            while index < len(rate_slots) and rate_slots[index].time_ticks == merged_time:
                slot = rate_slots[index]
                if (slot.flags & STPG_RATE_SLOT_SAMPLE) != 0:
                    sample_trigger = True
                if (slot.flags & STPG_RATE_SLOT_DIR_UPDATE) != 0 and slot.dir_mask != 0:
                    dir_update = True
                if slot.drive_mask != 0:
                    op_count += 1
                index += 1
            merged_groups.append(
                {
                    "time_ticks": merged_time,
                    "sample_trigger": sample_trigger,
                    "op_count": op_count,
                    "dir_update": dir_update,
                }
            )
        if len(merged_groups) != 2:
            reasons.append(f"rate[{rate_id}] merged_group_count={len(merged_groups)}")
            continue
        drive_group, sample_group = merged_groups
        if drive_group["time_ticks"] != 0:
            reasons.append(f"rate[{rate_id}] drive_group_time!=0")
        if bool(drive_group["sample_trigger"]):
            reasons.append(f"rate[{rate_id}] drive_group_has_sample")
        if not bool(sample_group["sample_trigger"]):
            reasons.append(f"rate[{rate_id}] sample_group_missing_sample")
        if int(sample_group["op_count"]) != 0:
            reasons.append(f"rate[{rate_id}] sample_group_has_drive")
        if bool(sample_group["dir_update"]):
            reasons.append(f"rate[{rate_id}] sample_group_has_dir_update")
        sample_time = int(sample_group["time_ticks"])
        if sample_time == 0 or sample_time >= rate.period_ticks:
            reasons.append(f"rate[{rate_id}] sample_group_time_out_of_range")
    candidate = not reasons
    opportunities = ("reduced-density-compare",) if candidate else ()
    return candidate, tuple(reasons), opportunities


def _rate_vector_sample_compare_counts(rates, slots, vectors, loops) -> tuple[int, int]:
    """Count emitted sample and effective compare points without expansion.

    A rate slot group becomes one output event.  Sample capture follows the
    slot's SAMPLE flag; compare follows the intersection of that group's
    sample mask and the vector's expected mask.  Loop bodies are multiplied
    by their loop count, keeping this calculation bounded by the encoded
    source tables.
    """

    def counts_for_record(record) -> tuple[int, int]:
        rate = rates[record.rate_id]
        rate_slots = slots[rate.slot_offset : rate.slot_offset + rate.slot_count]
        sample_groups = 0
        compare_groups = 0
        index = 0
        while index < len(rate_slots):
            group_time = rate_slots[index].time_ticks
            group_sample = False
            group_sample_mask = 0
            while index < len(rate_slots) and rate_slots[index].time_ticks == group_time:
                slot = rate_slots[index]
                if (slot.flags & STPG_RATE_SLOT_SAMPLE) != 0:
                    group_sample = True
                    group_sample_mask |= int(slot.sample_mask)
                index += 1
            if group_sample:
                sample_groups += 1
                if (group_sample_mask & int(record.expect_mask)) != 0:
                    compare_groups += 1
        return record.repeat * sample_groups, record.repeat * compare_groups

    sample_count = 0
    compare_count = 0
    for entry in vectors:
        if isinstance(entry, LoopRefRecord):
            loop_block = loops[entry.loop_id]
            body_sample = 0
            body_compare = 0
            for record in loop_block.body_records:
                record_sample, record_compare = counts_for_record(record)
                body_sample += record_sample
                body_compare += record_compare
            sample_count += loop_block.loop_count * body_sample
            compare_count += loop_block.loop_count * body_compare
        else:
            record_sample, record_compare = counts_for_record(entry)
            sample_count += record_sample
            compare_count += record_compare
    return sample_count, compare_count


def _macro_compare_count(records) -> int:
    return sum(
        record.repeat
        for record in records
        if record.family in (1, 2, 4, 5)
        and (record.flags & STPG_EVF_COMPARE_POINT) != 0
    )


def _macro_p2_compare_count(records) -> int:
    total = 0
    for record in records:
        if record.exec_family in (1, 2, 0x81, 4, 5) and (
            record.flags & STPG_EVF_COMPARE_POINT
        ) != 0:
            # M1/M2/0x81 have one sampled phase per record; M4/M5 have one
            # sampled event as well.  The flags preserve Compare L/X/H
            # semantics, including L where expect_value is zero.
            total += record.repeat
    return total


def _requires_stream_wait_watchdog(tick_hz, rates, slots, vectors, loops) -> bool:
    """Check actual logical gaps without expanding repeats/LoopRef sequences."""
    if not rates:
        return False
    limit = (tick_hz * LEGACY_STREAM_STALL_TIMEOUT_US + 999_999) // 1_000_000
    first_times = [slots[rate.slot_offset].time_ticks for rate in rates]
    if max(rate.period_ticks for rate in rates) + max(first_times) < limit:
        return False
    edges = []
    for rate, first in zip(rates, first_times):
        previous, inside = first, 0
        for slot in slots[rate.slot_offset + 1:rate.slot_offset + rate.slot_count]:
            inside = max(inside, slot.time_ticks - previous)
            previous = slot.time_ticks
        edges.append((first, rate.period_ticks - previous, inside))

    @lru_cache(maxsize=None)
    def loop_gaps(loop_id):
        loop = loops[loop_id]
        first, tail, largest = span(loop.body_records)
        return first, tail, max(largest, tail + first if loop.loop_count > 1 else 0)

    def span(records):
        start = None
        previous_tail = largest = 0
        for record in records:
            if isinstance(record, LoopRefRecord):
                first, tail, inside = loop_gaps(record.loop_id)
            else:
                first, tail, inside = edges[record.rate_id]
                if record.repeat > 1:
                    inside = max(inside, tail + first)
            if start is None:
                start = first
            else:
                largest = max(largest, previous_tail + first)
            largest = max(largest, inside)
            previous_tail = tail
        return start or 0, previous_tail, largest

    first, _, largest = span(vectors)
    return max(first, largest) >= limit


def inspect_pattern_requirements(pattern_bytes: bytes) -> PatternRequirements:
    magic = int.from_bytes(pattern_bytes[:4], "little")
    if magic == STPG_PATTERN_MAGIC:
        header, events = unpack_pattern(pattern_bytes)
        requires_sample = any((event.flags & STPG_EVF_SAMPLE_POINT) != 0 for event in events)
        requires_compare = any((event.flags & STPG_EVF_COMPARE_POINT) != 0 for event in events)
        return PatternRequirements(
            tick_hz=header.tick_hz,
            active_pins=header.active_pins,
            mode="raw-event",
            requires_sample=requires_sample,
            requires_compare=requires_compare,
            estimated_event_count=len(events),
            streaming_poc_incompatibility=None,
            estimated_sample_count=sum((event.flags & STPG_EVF_SAMPLE_POINT) != 0 for event in events),
            estimated_compare_count=sum((event.flags & STPG_EVF_COMPARE_POINT) != 0 for event in events),
        )
    if magic == STPG_RATEVEC_MAGIC:
        header, rates, slots, vectors, loops = unpack_rate_vector_full(pattern_bytes)
        estimated_sample_count, estimated_compare_count = _rate_vector_sample_compare_counts(
            rates, slots, vectors, loops
        )
        requires_sample = estimated_sample_count != 0
        requires_compare = estimated_compare_count != 0
        reduced_density_candidate, reduced_density_incompatibilities, exact_density_opportunities = (
            _analyze_reduced_density_candidate(rates, slots, loops)
        )
        timing_issues = tuple(streaming_poc_timing_issues(rates, slots))
        return PatternRequirements(
            tick_hz=header.tick_hz,
            active_pins=header.active_pins,
            mode="rate-vector",
            requires_sample=requires_sample,
            requires_compare=requires_compare,
            estimated_event_count=emitted_event_count_estimate(rates, slots, vectors, loops),
            streaming_poc_incompatibility=(
                str(timing_issues[0]["message"]) if timing_issues else None
            ),
            estimated_sample_count=estimated_sample_count,
            estimated_compare_count=estimated_compare_count,
            streaming_poc_timing_issues=timing_issues,
            reduced_density_candidate=reduced_density_candidate,
            reduced_density_incompatibilities=reduced_density_incompatibilities,
            exact_density_opportunities=exact_density_opportunities,
            requires_stream_wait_watchdog=_requires_stream_wait_watchdog(
                header.tick_hz, rates, slots, vectors, loops),
        )
    if magic == STPG_MACROVEC_MAGIC:
        header, records = unpack_macro_vector(pattern_bytes)
        estimated_sample_count = sample_macro_event_count_estimate(records)
        estimated_compare_count = _macro_compare_count(records)
        requires_sample = estimated_sample_count != 0
        requires_compare = estimated_compare_count != 0
        return PatternRequirements(
            tick_hz=header.tick_hz,
            active_pins=header.active_pins,
            mode="macro-stream",
            requires_sample=requires_sample,
            requires_compare=requires_compare,
            estimated_event_count=emitted_macro_event_count_estimate(records),
            streaming_poc_incompatibility=None,
            estimated_sample_count=estimated_sample_count,
            estimated_compare_count=estimated_compare_count,
            reduced_density_candidate=False,
            reduced_density_incompatibilities=("macro-stream",),
            exact_density_opportunities=("macro-stream-direct-decode", "macro-stream-streaming-refill"),
        )
    if magic == STPG_MACROP2VEC_MAGIC:
        header, records = unpack_macro_p2_vector(pattern_bytes)
        estimated_sample_count = sample_macro_p2_event_count_estimate(records)
        estimated_compare_count = _macro_p2_compare_count(records)
        requires_sample = estimated_sample_count != 0
        requires_compare = estimated_compare_count != 0
        return PatternRequirements(
            tick_hz=header.tick_hz,
            active_pins=header.active_pins,
            mode="macro-p2",
            requires_sample=requires_sample,
            requires_compare=requires_compare,
            estimated_event_count=emitted_macro_p2_event_count_estimate(records),
            streaming_poc_incompatibility=None,
            estimated_sample_count=estimated_sample_count,
            estimated_compare_count=estimated_compare_count,
            reduced_density_candidate=False,
            reduced_density_incompatibilities=("macro-p2",),
            exact_density_opportunities=("macro-p2-direct-decode",),
        )
    raise ValueError("unknown pattern magic")


def validate_device_compatibility(
    requirements: PatternRequirements,
    *,
    device_clock_tick_hz: int,
    device_active_pins: int,
    device_capability: int,
    storage_target: int,
    run_flags: int = 0,
    run_requested: bool = True,
    pattern_size_bytes: int | None = None,
    runtime_profile: str | RuntimeCapacityProfile = DEFAULT_RUNTIME_PROFILE,
) -> None:
    """Validate a pattern against device capabilities and the requested operation.

    ``run_requested=False`` is used by upload-only callers.  Upload admission
    still checks format, storage, and runtime-capacity contracts, but it does
    not apply execution-only limits such as the expanded non-streaming event
    ceiling or streaming run capability.
    """
    profile = (
        get_runtime_capacity_profile(runtime_profile)
        if isinstance(runtime_profile, str)
        else runtime_profile
    )
    if device_clock_tick_hz != requirements.tick_hz:
        raise RuntimeError(
            f"device tick_hz mismatch: pattern expects {requirements.tick_hz}, device reports {device_clock_tick_hz}"
        )
    if requirements.active_pins == 16:
        if device_active_pins not in (0,) and device_active_pins not in (16, 24):
            raise RuntimeError(
                f"device active_pins mismatch: pattern expects <=16 active pins, device reports {device_active_pins}"
            )
    elif requirements.active_pins == 24:
        if device_active_pins not in (0,) and device_active_pins not in (16, 24):
            raise RuntimeError(
                f"device active_pins mismatch: unsupported device active_pins report {device_active_pins}"
            )
    if requirements.active_pins == 16 and (device_capability & CAP_RUN_16PIN) == 0:
        raise RuntimeError("device does not advertise 16-pin run capability")
    if requirements.active_pins == 24 and (device_capability & CAP_RUN_24PIN) == 0:
        raise RuntimeError("device does not advertise 24-pin run capability")
    if requirements.mode in ("rate-vector", "macro-stream", "macro-p2") and storage_target == STORAGE_SRAM:
        raise RuntimeError(f"{requirements.mode} patterns require PSRAM or Flash target; SRAM target is raw-event only")
    if storage_target == STORAGE_SRAM and (device_capability & CAP_UPLOAD_SRAM) == 0:
        raise RuntimeError("device does not advertise SRAM upload capability")
    if storage_target == STORAGE_PSRAM and (device_capability & CAP_UPLOAD_PSRAM) == 0:
        raise RuntimeError("device does not advertise PSRAM upload capability")
    if storage_target == STORAGE_FLASH and (device_capability & CAP_UPLOAD_FLASH) == 0:
        raise RuntimeError("device does not advertise Flash upload capability")
    if requirements.mode == "rate-vector" and (device_capability & CAP_RATE_VECTOR_MODE) == 0:
        raise RuntimeError("device does not advertise rate-vector capability")
    if requirements.requires_sample and (device_capability & CAP_SAMPLE_CAPTURE) == 0:
        raise RuntimeError("pattern requires sample capture but device does not advertise it")
    if requirements.requires_compare and (device_capability & CAP_EXPECT_COMPARE) == 0:
        raise RuntimeError("pattern requires compare but device does not advertise it")
    if (
        run_requested
        and requirements.requires_compare
        and (run_flags & RUN_FLAG_STREAMING_POC) != 0
        and (run_flags & RUN_FLAG_FAIL_STOP) == 0
        and (device_capability & CAP_STREAM_FAIL_STOP) != 0
    ):
        raise RuntimeError(
            "compare-bearing streaming RUN without --fail-stop is not checked by "
            "SRAM-sample streaming fail-stop firmware; use --fail-stop with the "
            "matching runtime profile or a practical streaming firmware"
        )
    if run_requested and (run_flags & RUN_FLAG_FAIL_STOP) != 0:
        if (run_flags & RUN_FLAG_STREAMING_POC) != 0:
            if profile.name != STREAMING_FAIL_STOP_SRAM_PROFILE:
                raise RuntimeError(
                    "streaming fail-stop requires the "
                    f"{STREAMING_FAIL_STOP_SRAM_PROFILE} runtime profile; "
                    f"{profile.name} is not an SRAM fail-stop contract"
                )
            if (device_capability & CAP_STREAM_FAIL_STOP) == 0:
                raise RuntimeError(
                    "streaming fail-stop requested but device does not advertise "
                    "SRAM-sample streaming fail-stop; use a firmware build with "
                    "STPG_STREAM_FAIL_STOP=ON"
                )
            if requirements.mode != "rate-vector":
                raise RuntimeError("streaming fail-stop is supported only for Rate Vector patterns")
            if storage_target != STORAGE_PSRAM:
                raise RuntimeError(
                    "streaming fail-stop requires a PSRAM-backed Rate Vector pattern"
                )
            if requirements.reduced_density_candidate:
                raise RuntimeError(
                    "streaming fail-stop is not supported for reduced-density Rate Vector candidates"
                )
            if requirements.estimated_sample_count > STPG_MAX_EVENT_COUNT:
                raise RuntimeError(
                    "streaming fail-stop requires SRAM-resident samples; estimated sample count "
                    f"{requirements.estimated_sample_count} exceeds {STPG_MAX_EVENT_COUNT}"
                )
        elif (device_capability & CAP_FAIL_STOP) == 0:
            raise RuntimeError(
                "fail-stop requested but device does not advertise materialized compare fail-stop; "
                "use a firmware build with STPG_FAIL_STOP=ON"
            )
    if (
        run_requested
        and requirements.mode in ("rate-vector", "macro-stream", "macro-p2")
        and storage_target in (STORAGE_PSRAM, STORAGE_FLASH)
        and (run_flags & RUN_FLAG_STREAMING_POC) == 0
        and requirements.estimated_event_count > STPG_MAX_EVENT_COUNT
    ):
        raise RuntimeError(
            "pattern expands to "
            f"{requirements.estimated_event_count} events, exceeding non-streaming limit {STPG_MAX_EVENT_COUNT}; "
            "rerun with --streaming-poc"
        )
    if run_requested and (run_flags & RUN_FLAG_STREAMING_POC) != 0:
        if requirements.mode not in profile.supported_stream_modes:
            raise RuntimeError(
                f"streaming-poc is not supported for {requirements.mode} by runtime profile "
                f"{profile.name}; use direct execution or a supported Rate Vector package"
            )
    capacity_errors = assess_runtime_capacity(
        requirements,
        pattern_size_bytes=pattern_size_bytes,
        profile=profile,
    )
    if capacity_errors:
        raise RuntimeError(capacity_errors[0]["message"])
    if (
        requirements.estimated_event_count > profile.full_expansion_event_limit
        and profile.required_capability
        and (device_capability & profile.required_capability) != profile.required_capability
    ):
        raise RuntimeError(
            f"device does not advertise runtime profile {profile.name}; "
            "flash/build the matching practical streaming firmware before upload/RUN"
        )
    if (run_flags & RUN_FLAG_STREAMING_POC) != 0 and (device_capability & CAP_PSRAM_STREAMING) == 0:
        raise RuntimeError("streaming-poc requested but device does not advertise PSRAM streaming capability")
    if (run_requested and (run_flags & RUN_FLAG_STREAMING_POC) != 0
            and requirements.requires_stream_wait_watchdog
            and (device_capability & CAP_STREAM_WAIT_WATCHDOG) == 0):
        raise RuntimeError(
            "Rate Vector streaming gap of at least 250 ms requires firmware with the "
            "timer-aware stream watchdog (CAP_STREAM_WAIT_WATCHDOG); not a host response timeout"
        )
    if (run_flags & RUN_FLAG_STREAMING_POC) != 0 and requirements.streaming_poc_incompatibility is not None:
        raise RuntimeError(
            "streaming-poc unsupported for current runtime: "
            f"{requirements.streaming_poc_incompatibility}"
        )


def estimate_direct_execution_fit(requirements: PatternRequirements) -> DirectExecutionFit:
    estimated_event_count = requirements.estimated_event_count
    estimated_sample_count = requirements.estimated_sample_count
    event_limit = STPG_MAX_EVENT_COUNT
    sample_limit = STPG_MAX_EVENT_COUNT
    if estimated_event_count > event_limit:
        return DirectExecutionFit(
            supported=False,
            estimated_event_count=estimated_event_count,
            estimated_sample_count=estimated_sample_count,
            event_limit=event_limit,
            sample_limit=sample_limit,
            reason=f"estimated_event_count>{event_limit}",
        )
    if estimated_sample_count > sample_limit:
        return DirectExecutionFit(
            supported=False,
            estimated_event_count=estimated_event_count,
            estimated_sample_count=estimated_sample_count,
            event_limit=event_limit,
            sample_limit=sample_limit,
            reason=f"estimated_sample_count>{sample_limit}",
        )
    return DirectExecutionFit(
        supported=True,
        estimated_event_count=estimated_event_count,
        estimated_sample_count=estimated_sample_count,
        event_limit=event_limit,
        sample_limit=sample_limit,
        reason=None,
    )


def summarize_runtime_precheck(
    requirements: PatternRequirements,
    *,
    pattern_size_bytes: int | None = None,
    runtime_profile: str | RuntimeCapacityProfile = DEFAULT_RUNTIME_PROFILE,
) -> str:
    profile = (
        get_runtime_capacity_profile(runtime_profile)
        if isinstance(runtime_profile, str)
        else runtime_profile
    )
    direct_fit = estimate_direct_execution_fit(requirements)
    observation = runtime_capacity_observation(
        requirements,
        pattern_size_bytes=pattern_size_bytes,
        profile=profile,
    )
    lines = [
        f"mode={requirements.mode}",
        f"active_pins={requirements.active_pins}",
        f"tick_hz={requirements.tick_hz}",
        f"estimated_event_count={requirements.estimated_event_count}",
        f"pattern_size_bytes={pattern_size_bytes if pattern_size_bytes is not None else 'unknown'}",
        f"runtime_profile={profile.name}",
        f"runtime_profile_required_capability=0x{profile.required_capability:08X}",
    ]
    lines.extend(render_runtime_capacity_observation(observation).splitlines())
    if requirements.mode in ("rate-vector", "macro-stream"):
        lines.append(f"non_streaming_event_limit={profile.full_expansion_event_limit}")
        lines.append(f"streaming_event_limit={profile.streaming_event_limit}")
        lines.append(f"streaming_sample_limit={profile.streaming_sample_limit}")
        lines.append(f"streaming_compare_limit={profile.streaming_compare_limit}")
        lines.append(f"direct_exec_fit={'yes' if direct_fit.supported else 'no'}")
        lines.append(f"direct_exec_estimated_sample_count={direct_fit.estimated_sample_count}")
        if direct_fit.reason is not None:
            lines.append(f"direct_exec_reason={direct_fit.reason}")
        if requirements.streaming_poc_incompatibility is None:
            lines.append("streaming_poc_compatible=yes")
        else:
            lines.append("streaming_poc_compatible=no")
            lines.append(f"streaming_poc_reason={requirements.streaming_poc_incompatibility}")
            lines.append(
                f"streaming_poc_timing_issue_count={len(requirements.streaming_poc_timing_issues)}"
            )
            for issue in requirements.streaming_poc_timing_issues:
                lines.append(f"streaming_poc_timing_issue={issue['message']}")
        lines.append(f"reduced_density_candidate={'yes' if requirements.reduced_density_candidate else 'no'}")
        if requirements.reduced_density_incompatibilities:
            lines.append(
                "reduced_density_reasons=" + ",".join(requirements.reduced_density_incompatibilities)
            )
        if requirements.exact_density_opportunities:
            lines.append(
                "exact_density_opportunities=" + ",".join(requirements.exact_density_opportunities)
            )
    return "\n".join(lines)
