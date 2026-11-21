from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from .underflow_analyze import analyze_rate_vector_underflow, build_practical_runtime_hints, load_optional_json
from stpg.pattern import (
    RateDesc,
    RateSlot,
    VectorRecord,
    pack_rate_vector,
    unpack_rate_vector_full,
    validate_rate_vector,
)


class LocalWaitPatchError(ValueError):
    pass


RUNTIME_PAGE30_POLICY_EXACT = "exact"
RUNTIME_PAGE30_POLICY_PLUS1 = "plus1"
RUNTIME_PAGE30_POLICIES = (
    RUNTIME_PAGE30_POLICY_EXACT,
    RUNTIME_PAGE30_POLICY_PLUS1,
)


DEFAULT_BATCH_MAX_ROUNDS = 8
DEFAULT_BATCH_MAX_NEW_RATES = 8
DEFAULT_BATCH_MAX_TOTAL_ADDED_TICKS = 24
DEFAULT_BATCH_MAX_ADDED_TICKS_PER_ROOT_FAMILY = 12
LOCAL_WAIT_BATCH_MODE_PRACTICAL_COMPARE24 = "practical_compare24"
LOCAL_WAIT_BATCH_MODE_GENERIC_LOCAL_WAIT = "generic_local_wait"
LOCAL_WAIT_BATCH_MODES = (
    LOCAL_WAIT_BATCH_MODE_PRACTICAL_COMPARE24,
    LOCAL_WAIT_BATCH_MODE_GENERIC_LOCAL_WAIT,
)


def _default_sidecar(path: Path, suffix: str) -> Path | None:
    candidate = path.with_suffix(suffix)
    return candidate if candidate.exists() else None


def load_candidate_from_report(report_data: dict[str, Any], candidate_index: int) -> dict[str, Any]:
    runtime_precheck = report_data.get("capability_precheck", {}).get("runtime_precheck", {})
    candidates = runtime_precheck.get("practical_local_wait_candidates", [])
    if not isinstance(candidates, list) or not candidates:
        raise LocalWaitPatchError("compile_report has no practical_local_wait_candidates")
    if candidate_index < 0 or candidate_index >= len(candidates):
        raise LocalWaitPatchError(
            f"candidate_index {candidate_index} out of range for {len(candidates)} practical_local_wait_candidates"
        )
    candidate = candidates[candidate_index]
    if candidate.get("kind") != "local_wait":
        raise LocalWaitPatchError(f"candidate[{candidate_index}] is not a local_wait candidate")
    required_keys = (
        "rate_id",
        "group_index",
        "minimum_extra_ticks",
        "expanded_vector_start",
        "expanded_vector_end_exclusive",
    )
    missing = [key for key in required_keys if key not in candidate]
    if missing:
        raise LocalWaitPatchError(f"candidate[{candidate_index}] is missing keys: {', '.join(missing)}")
    return candidate


def apply_runtime_page30_policy(candidate: dict[str, Any], policy: str) -> dict[str, Any]:
    if policy not in RUNTIME_PAGE30_POLICIES:
        raise LocalWaitPatchError(f"unsupported runtime PAGE30 policy: {policy}")
    adjusted = dict(candidate)
    if adjusted.get("candidate_source") != "runtime_page30":
        return adjusted
    if policy == RUNTIME_PAGE30_POLICY_EXACT:
        return adjusted

    adjusted["minimum_extra_ticks"] = int(adjusted["minimum_extra_ticks"]) + 1
    if "minimum_extra_ns" in adjusted:
        base_ticks = max(int(candidate.get("minimum_extra_ticks", 1)), 1)
        tick_ns = float(candidate["minimum_extra_ns"]) / float(base_ticks)
        adjusted["minimum_extra_ns"] = round(float(adjusted["minimum_extra_ticks"]) * tick_ns, 3)
    adjusted["message"] = (
        f"{adjusted.get('message', '')}; policy=runtime_page30+1tick"
        if adjusted.get("message")
        else "policy=runtime_page30+1tick"
    )
    adjusted["runtime_page30_policy"] = policy
    return adjusted


def load_candidate_from_report_or_analyze(
    *,
    pattern_bytes: bytes,
    map_data: dict[str, Any],
    report_data: dict[str, Any] | None,
    runtime_report_data: dict[str, Any] | None = None,
    prefer_runtime_page30: bool = False,
    runtime_page30_policy: str = RUNTIME_PAGE30_POLICY_EXACT,
    candidate_index: int,
) -> tuple[dict[str, Any], str]:
    runtime_precheck = {} if report_data is None else report_data.get("capability_precheck", {}).get("runtime_precheck", {})
    candidates = runtime_precheck.get("practical_local_wait_candidates", [])
    if not prefer_runtime_page30 and isinstance(candidates, list) and candidates:
        return load_candidate_from_report(report_data or {}, candidate_index), "compile_report"

    practical_report = analyze_rate_vector_underflow(
        pattern_bytes,
        map_data=map_data,
        runtime_report=runtime_report_data,
    )
    practical_hints = build_practical_runtime_hints(practical_report)
    synthesized_report = {
        "capability_precheck": {
            "runtime_precheck": {
                "practical_local_wait_candidates": practical_hints.get("local_wait_candidates", []),
            }
        }
    }
    candidate = apply_runtime_page30_policy(
        load_candidate_from_report(synthesized_report, candidate_index),
        runtime_page30_policy,
    )
    return candidate, str(candidate.get("candidate_source", "offline_analyzer"))


def _rate_groups(rate: RateDesc, slots: list[RateSlot]) -> list[tuple[int, int, int]]:
    groups: list[tuple[int, int, int]] = []
    rate_slots = slots[rate.slot_offset : rate.slot_offset + rate.slot_count]
    if not rate_slots:
        return groups
    start = 0
    while start < len(rate_slots):
        time_ticks = rate_slots[start].time_ticks
        end = start + 1
        while end < len(rate_slots) and rate_slots[end].time_ticks == time_ticks:
            end += 1
        groups.append((start, end, time_ticks))
        start = end
    return groups


def clone_rate_with_local_wait(
    rates: list[RateDesc],
    slots: list[RateSlot],
    *,
    rate_id: int,
    group_index: int,
    extra_ticks: int,
) -> tuple[list[RateDesc], list[RateSlot], int]:
    if extra_ticks <= 0:
        raise LocalWaitPatchError("extra_ticks must be > 0")
    if rate_id < 0 or rate_id >= len(rates):
        raise LocalWaitPatchError(f"rate_id {rate_id} out of range")
    base_rate = rates[rate_id]
    groups = _rate_groups(base_rate, slots)
    if group_index < 0 or group_index >= len(groups):
        raise LocalWaitPatchError(f"group_index {group_index} out of range for rate[{rate_id}]")

    original_slots = slots[base_rate.slot_offset : base_rate.slot_offset + base_rate.slot_count]
    new_rate_slots = list(original_slots)
    new_period_ticks = base_rate.period_ticks + extra_ticks

    if group_index > 0:
        group_start_slot, _, _ = groups[group_index]
        for slot_offset in range(group_start_slot, len(new_rate_slots)):
            slot = new_rate_slots[slot_offset]
            new_rate_slots[slot_offset] = replace(slot, time_ticks=slot.time_ticks + extra_ticks)

    new_slot_offset = len(slots)
    cloned_rate = RateDesc(
        period_ticks=new_period_ticks,
        slot_count=base_rate.slot_count,
        flags=base_rate.flags,
        slot_offset=new_slot_offset,
    )
    patched_rates = list(rates)
    patched_rates.append(cloned_rate)
    patched_slots = list(slots)
    patched_slots.extend(new_rate_slots)
    return patched_rates, patched_slots, len(patched_rates) - 1


def _slice_vector_payload(payload: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    sliced = dict(payload)
    source_map = list(payload.get("source_map", []))[start:end]
    sliced["source_map"] = source_map
    sliced["repeat"] = len(source_map)
    return sliced


def _collapse_global_indices(global_indices: list[int]) -> list[dict[str, int]]:
    if not global_indices:
        return []
    ordered = sorted(global_indices)
    ranges: list[dict[str, int]] = []
    start = ordered[0]
    prev = ordered[0]
    for value in ordered[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append({"global_vector_start": start, "global_vector_end": prev})
        start = value
        prev = value
    ranges.append({"global_vector_start": start, "global_vector_end": prev})
    return ranges


def _candidate_key(candidate: dict[str, Any]) -> tuple[int, int, int | None, int | None]:
    return (
        int(candidate.get("rate_id", -1)),
        int(candidate.get("group_index", -1)),
        None if candidate.get("global_vector_start") is None else int(candidate["global_vector_start"]),
        None if candidate.get("global_vector_end") is None else int(candidate["global_vector_end"]),
    )


def _practical_compare24_local_wait_candidates(hints: dict[str, Any]) -> list[dict[str, Any]]:
    local_waits = list(hints.get("local_wait_candidates", []))
    practical_hard_errors = list(hints.get("practical_hard_errors", []))
    if not practical_hard_errors:
        return []

    allowed_keys: set[tuple[int, int, int | None, int | None]] = set()
    for error in practical_hard_errors:
        if error.get("code") != "ERR_PRACTICAL_COMPARE24_TIMING":
            continue
        context = error.get("context", {})
        allowed_keys.add(
            (
                int(context.get("rate_id", -1)),
                int(context.get("group_index", -1)),
                None if context.get("global_vector_start") is None else int(context["global_vector_start"]),
                None if context.get("global_vector_end") is None else int(context["global_vector_end"]),
            )
        )
    return [candidate for candidate in local_waits if _candidate_key(candidate) in allowed_keys]


def _generic_local_wait_candidates(hints: dict[str, Any]) -> list[dict[str, Any]]:
    local_waits = list(hints.get("local_wait_candidates", []))

    def sort_key(item: dict[str, Any]) -> tuple[int, int, int, int]:
        expanded_start = int(item.get("expanded_vector_start", 1 << 30))
        global_start = int(item.get("global_vector_start", expanded_start))
        group_index = int(item.get("group_index", 1 << 20))
        minimum_extra = int(item.get("minimum_extra_ticks", 0))
        return (expanded_start, global_start, group_index, minimum_extra)

    return sorted(local_waits, key=sort_key)


def _rate_count(pattern_bytes: bytes) -> int:
    _, rates, _, _, _ = unpack_rate_vector_full(pattern_bytes)
    return len(rates)


def _batch_round_candidate(
    *,
    current_pattern: bytes,
    current_map: dict[str, Any],
    compile_report_data: dict[str, Any] | None,
    round_index: int,
    candidate_mode: str,
) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
    report = analyze_rate_vector_underflow(current_pattern, map_data=current_map)
    hints = build_practical_runtime_hints(report, top_chunk_n=4, top_candidate_n=32)
    if candidate_mode == LOCAL_WAIT_BATCH_MODE_PRACTICAL_COMPARE24:
        candidates = _practical_compare24_local_wait_candidates(hints)
        if not candidates:
            return None, hints, "current_analyzer"
        if round_index == 0 and compile_report_data is not None:
            compile_waits = (
                compile_report_data.get("capability_precheck", {})
                .get("runtime_precheck", {})
                .get("practical_local_wait_candidates", [])
            )
            for candidate in compile_waits:
                if _candidate_key(candidate) in {_candidate_key(item) for item in candidates}:
                    return dict(candidate), hints, "compile_report"
        return dict(candidates[0]), hints, "current_analyzer"

    if candidate_mode == LOCAL_WAIT_BATCH_MODE_GENERIC_LOCAL_WAIT:
        candidates = _generic_local_wait_candidates(hints)
        if not candidates:
            return None, hints, "current_analyzer"
        return dict(candidates[0]), hints, "current_analyzer"

    raise LocalWaitPatchError(f"unsupported candidate_mode: {candidate_mode}")


def apply_local_wait_batch(
    pattern_bytes: bytes,
    *,
    map_data: dict[str, Any],
    compile_report_data: dict[str, Any] | None = None,
    candidate_mode: str = LOCAL_WAIT_BATCH_MODE_PRACTICAL_COMPARE24,
    max_rounds: int = DEFAULT_BATCH_MAX_ROUNDS,
    max_new_rates: int = DEFAULT_BATCH_MAX_NEW_RATES,
    max_total_added_ticks: int = DEFAULT_BATCH_MAX_TOTAL_ADDED_TICKS,
    max_added_ticks_per_root_family: int = DEFAULT_BATCH_MAX_ADDED_TICKS_PER_ROOT_FAMILY,
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    if candidate_mode not in LOCAL_WAIT_BATCH_MODES:
        raise LocalWaitPatchError(f"unsupported candidate_mode: {candidate_mode}")
    if max_rounds <= 0:
        raise LocalWaitPatchError("max_rounds must be > 0")
    if max_new_rates < 0:
        raise LocalWaitPatchError("max_new_rates must be >= 0")
    if max_total_added_ticks < 0:
        raise LocalWaitPatchError("max_total_added_ticks must be >= 0")
    if max_added_ticks_per_root_family < 0:
        raise LocalWaitPatchError("max_added_ticks_per_root_family must be >= 0")

    current_pattern = pattern_bytes
    current_map = deepcopy(map_data)
    initial_rate_count = _rate_count(pattern_bytes)
    total_added_ticks = 0
    root_added_ticks: dict[int, int] = {}
    lineage: list[dict[str, int]] = []
    current_to_root = {rate_id: rate_id for rate_id in range(initial_rate_count)}
    previous_front_key: tuple[int, int, int | None, int | None] | None = None
    stagnant_rounds = 0
    previous_candidate_count: int | None = None
    round_records: list[dict[str, Any]] = []
    final_hints: dict[str, Any] = {}
    stop_reason = "unknown"
    baseline_generator_avoidance_count: int | None = None
    baseline_chunk_boundary_warning_count: int | None = None

    for round_index in range(max_rounds):
        candidate, hints, candidate_source = _batch_round_candidate(
            current_pattern=current_pattern,
            current_map=current_map,
            compile_report_data=compile_report_data,
            round_index=round_index,
            candidate_mode=candidate_mode,
        )
        final_hints = hints
        practical_candidates = _practical_compare24_local_wait_candidates(hints)
        generic_candidates = _generic_local_wait_candidates(hints)
        candidate_count_before = (
            len(practical_candidates)
            if candidate_mode == LOCAL_WAIT_BATCH_MODE_PRACTICAL_COMPARE24
            else len(generic_candidates)
        )
        if baseline_generator_avoidance_count is None:
            baseline_generator_avoidance_count = len(hints.get("generator_avoidance_candidates", []))
        if baseline_chunk_boundary_warning_count is None:
            baseline_chunk_boundary_warning_count = len(hints.get("practical_chunk_warnings", []))
        if candidate is None:
            stop_reason = (
                "no_practical_compare24_candidates"
                if candidate_mode == LOCAL_WAIT_BATCH_MODE_PRACTICAL_COMPARE24
                else "no_local_wait_candidates"
            )
            break

        candidate_key = _candidate_key(candidate)
        same_front = previous_front_key == candidate_key
        candidate_count_not_decreased = (
            previous_candidate_count is not None and candidate_count_before >= previous_candidate_count
        )
        if same_front or candidate_count_not_decreased:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        previous_front_key = candidate_key
        previous_candidate_count = candidate_count_before

        current_rate_id = int(candidate["rate_id"])
        root_rate_id = current_to_root.get(current_rate_id, current_rate_id)
        applied_extra_ticks = int(candidate["minimum_extra_ticks"])

        if total_added_ticks + applied_extra_ticks > max_total_added_ticks:
            stop_reason = "max_total_added_ticks_reached"
            break
        if root_added_ticks.get(root_rate_id, 0) + applied_extra_ticks > max_added_ticks_per_root_family:
            stop_reason = "max_added_ticks_per_root_family_reached"
            break

        patched_pattern, patched_map, patch_manifest = patch_local_wait_candidate(
            current_pattern,
            map_data=current_map,
            report_data={
                "capability_precheck": {"runtime_precheck": {"practical_local_wait_candidates": [candidate]}}
            },
            candidate_index=0,
            all_spans_for_rate=True,
        )

        patched_rate_id = int(patch_manifest["patched_rate_id"])
        current_to_root[patched_rate_id] = root_rate_id
        root_added_ticks[root_rate_id] = root_added_ticks.get(root_rate_id, 0) + applied_extra_ticks
        total_added_ticks += applied_extra_ticks
        lineage.append(
            {
                "round_index": round_index,
                "root_rate_id": root_rate_id,
                "parent_rate_id": current_rate_id,
                "current_rate_id": patched_rate_id,
            }
        )

        next_report = analyze_rate_vector_underflow(patched_pattern, map_data=patched_map)
        next_hints = build_practical_runtime_hints(next_report, top_chunk_n=4, top_candidate_n=32)
        next_practical_candidates = _practical_compare24_local_wait_candidates(next_hints)
        next_generic_candidates = _generic_local_wait_candidates(next_hints)
        next_candidate_count = (
            len(next_practical_candidates)
            if candidate_mode == LOCAL_WAIT_BATCH_MODE_PRACTICAL_COMPARE24
            else len(next_generic_candidates)
        )
        next_generator_avoidance_count = len(next_hints.get("generator_avoidance_candidates", []))
        next_chunk_boundary_warning_count = len(next_hints.get("practical_chunk_warnings", []))

        round_records.append(
            {
                "mode": candidate_mode,
                "round_index": round_index,
                "candidate_source": candidate_source,
                "root_rate_id": root_rate_id,
                "parent_rate_id": current_rate_id,
                "current_rate_id": patched_rate_id,
                "rate_id": current_rate_id,
                "group_index": int(candidate["group_index"]),
                "minimum_extra_ticks": int(candidate["minimum_extra_ticks"]),
                "applied_extra_ticks": applied_extra_ticks,
                "all_spans_for_rate": True,
                "patched_global_ranges": patch_manifest.get("patched_global_ranges", []),
                "remaining_local_wait_candidate_count": len(next_hints.get("local_wait_candidates", [])),
                "remaining_practical_compare24_candidate_count": len(next_practical_candidates),
                "remaining_generator_avoidance_candidate_count": len(next_hints.get("generator_avoidance_candidates", [])),
                "remaining_active_candidate_count": next_candidate_count,
                "remaining_practical_chunk_warning_count": next_chunk_boundary_warning_count,
                "new_rate_count": _rate_count(patched_pattern) - initial_rate_count,
                "total_added_ticks": total_added_ticks,
                "root_added_ticks": dict(sorted(root_added_ticks.items())),
                "front_candidate_before": candidate,
                "front_candidate_after": (
                    next_practical_candidates[0]
                    if candidate_mode == LOCAL_WAIT_BATCH_MODE_PRACTICAL_COMPARE24 and next_practical_candidates
                    else next_generic_candidates[0]
                    if candidate_mode == LOCAL_WAIT_BATCH_MODE_GENERIC_LOCAL_WAIT and next_generic_candidates
                    else None
                ),
                "post_patch_summary": patch_manifest.get("post_patch_summary", {}),
            }
        )

        current_pattern = patched_pattern
        current_map = patched_map
        final_hints = next_hints

        if (
            candidate_mode == LOCAL_WAIT_BATCH_MODE_PRACTICAL_COMPARE24 and not next_practical_candidates
        ) or (
            candidate_mode == LOCAL_WAIT_BATCH_MODE_GENERIC_LOCAL_WAIT and not next_generic_candidates
        ):
            stop_reason = (
                "no_practical_compare24_candidates"
                if candidate_mode == LOCAL_WAIT_BATCH_MODE_PRACTICAL_COMPARE24
                else "no_local_wait_candidates"
            )
            break
        if (_rate_count(current_pattern) - initial_rate_count) >= max_new_rates:
            stop_reason = "max_new_rates_reached"
            break
        if (
            candidate_mode == LOCAL_WAIT_BATCH_MODE_GENERIC_LOCAL_WAIT
            and baseline_generator_avoidance_count is not None
            and next_generator_avoidance_count > baseline_generator_avoidance_count
        ):
            stop_reason = "generator_avoidance_count_increased"
            break
        if (
            candidate_mode == LOCAL_WAIT_BATCH_MODE_GENERIC_LOCAL_WAIT
            and baseline_chunk_boundary_warning_count is not None
            and next_chunk_boundary_warning_count > baseline_chunk_boundary_warning_count
        ):
            stop_reason = "chunk_boundary_warning_count_increased"
            break
        if stagnant_rounds >= 2:
            stop_reason = "residual_family_not_shrinking"
            break
    else:
        stop_reason = "max_rounds_reached"

    manifest = {
        "kind": "local_wait_batch",
        "mode": candidate_mode,
        "input_pattern_size": len(pattern_bytes),
        "output_pattern_size": len(current_pattern),
        "initial_rate_count": initial_rate_count,
        "final_rate_count": _rate_count(current_pattern),
        "new_rate_count": _rate_count(current_pattern) - initial_rate_count,
        "total_added_ticks": total_added_ticks,
        "root_added_ticks": dict(sorted(root_added_ticks.items())),
        "max_rounds": max_rounds,
        "max_new_rates": max_new_rates,
        "max_total_added_ticks": max_total_added_ticks,
        "max_added_ticks_per_root_family": max_added_ticks_per_root_family,
        "stop_reason": stop_reason,
        "lineage": lineage,
        "rounds": round_records,
        "final_runtime_hints": {
            "local_wait_candidates": final_hints.get("local_wait_candidates", []),
            "generator_avoidance_candidates": final_hints.get("generator_avoidance_candidates", []),
            "practical_hard_errors": final_hints.get("practical_hard_errors", []),
        },
    }
    return current_pattern, current_map, manifest


def patch_vectors_for_global_indices(
    vectors: list[VectorRecord],
    map_data: dict[str, Any],
    *,
    original_rate_id: int,
    cloned_rate_id: int,
    target_global_indices: set[int],
) -> tuple[list[VectorRecord], dict[str, Any], list[dict[str, Any]], list[int]]:
    if not target_global_indices:
        raise LocalWaitPatchError("target_global_indices must be non-empty")

    original_vectors = map_data.get("vectors")
    if not isinstance(original_vectors, dict):
        raise LocalWaitPatchError("map.json has no vectors table")

    patched_vectors: list[VectorRecord] = []
    patched_map = deepcopy(map_data)
    patched_map_vectors: dict[str, Any] = {}
    segments: list[dict[str, Any]] = []
    new_local_index = 0
    covered_globals: list[int] = []

    for old_local_index, vector in enumerate(vectors):
        payload = original_vectors.get(str(old_local_index))
        if payload is None:
            raise LocalWaitPatchError(f"map.json missing vectors[{old_local_index}]")
        source_map = list(payload.get("source_map", []))
        if len(source_map) != vector.repeat:
            raise LocalWaitPatchError(
                f"map.json vectors[{old_local_index}] source_map length {len(source_map)} != repeat {vector.repeat}"
            )

        selected_positions = []
        for position, item in enumerate(source_map):
            global_index = item.get("global_vector_index")
            if global_index is not None and int(global_index) in target_global_indices:
                selected_positions.append(position)
                covered_globals.append(int(global_index))

        if not selected_positions:
            patched_vectors.append(vector)
            patched_map_vectors[str(new_local_index)] = payload
            new_local_index += 1
            continue

        if vector.rate_id != original_rate_id:
            raise LocalWaitPatchError(
                f"selected global indices overlap local vector {old_local_index} with rate_id={vector.rate_id}, "
                f"expected rate_id={original_rate_id}"
            )

        start = 0
        while start < vector.repeat:
            end = start + 1
            start_selected = start in selected_positions
            while end < vector.repeat and ((end in selected_positions) == start_selected):
                end += 1
            split_rate_id = cloned_rate_id if start_selected else vector.rate_id
            repeat = end - start
            new_vector = replace(vector, rate_id=split_rate_id, repeat=repeat)
            patched_vectors.append(new_vector)
            new_payload = _slice_vector_payload(payload, start, end)
            patched_map_vectors[str(new_local_index)] = new_payload
            segments.append(
                {
                    "old_local_vector_index": old_local_index,
                    "new_local_vector_index": new_local_index,
                    "kind": "patched" if start_selected else "original",
                    "rate_id": split_rate_id,
                    "repeat": repeat,
                    "global_vector_start": new_payload["source_map"][0].get("global_vector_index"),
                    "global_vector_end": new_payload["source_map"][-1].get("global_vector_index"),
                    "stil_line_start": new_payload["source_map"][0].get("stil_line"),
                    "stil_line_end": new_payload["source_map"][-1].get("stil_line"),
                }
            )
            new_local_index += 1
            start = end

    missing_globals = sorted(target_global_indices - set(covered_globals))
    if missing_globals:
        raise LocalWaitPatchError(
            f"selected global vector indices were not fully covered by patched local vectors: {missing_globals[:8]}"
        )

    patched_map["vectors"] = patched_map_vectors
    return patched_vectors, patched_map, segments, covered_globals


def patch_vectors_for_global_indices_multi_rate(
    vectors: list[VectorRecord],
    map_data: dict[str, Any],
    *,
    cloned_rate_ids: dict[int, int],
    target_global_indices: set[int],
) -> tuple[list[VectorRecord], dict[str, Any], list[dict[str, Any]], list[int]]:
    if not target_global_indices:
        raise LocalWaitPatchError("target_global_indices must be non-empty")
    if not cloned_rate_ids:
        raise LocalWaitPatchError("cloned_rate_ids must be non-empty")

    original_vectors = map_data.get("vectors")
    if not isinstance(original_vectors, dict):
        raise LocalWaitPatchError("map.json has no vectors table")

    patched_vectors: list[VectorRecord] = []
    patched_map = deepcopy(map_data)
    patched_map_vectors: dict[str, Any] = {}
    segments: list[dict[str, Any]] = []
    new_local_index = 0
    covered_globals: list[int] = []

    for old_local_index, vector in enumerate(vectors):
        payload = original_vectors.get(str(old_local_index))
        if payload is None:
            raise LocalWaitPatchError(f"map.json missing vectors[{old_local_index}]")
        source_map = list(payload.get("source_map", []))
        if len(source_map) != vector.repeat:
            raise LocalWaitPatchError(
                f"map.json vectors[{old_local_index}] source_map length {len(source_map)} != repeat {vector.repeat}"
            )

        selected_positions = []
        for position, item in enumerate(source_map):
            global_index = item.get("global_vector_index")
            if global_index is not None and int(global_index) in target_global_indices:
                selected_positions.append(position)
                covered_globals.append(int(global_index))

        if not selected_positions:
            patched_vectors.append(vector)
            patched_map_vectors[str(new_local_index)] = payload
            new_local_index += 1
            continue

        if vector.rate_id not in cloned_rate_ids:
            raise LocalWaitPatchError(
                f"selected global indices overlap local vector {old_local_index} with unmapped rate_id={vector.rate_id}"
            )

        selected_set = set(selected_positions)
        start = 0
        while start < vector.repeat:
            end = start + 1
            start_selected = start in selected_set
            while end < vector.repeat and ((end in selected_set) == start_selected):
                end += 1
            split_rate_id = cloned_rate_ids[vector.rate_id] if start_selected else vector.rate_id
            repeat = end - start
            new_vector = replace(vector, rate_id=split_rate_id, repeat=repeat)
            patched_vectors.append(new_vector)
            new_payload = _slice_vector_payload(payload, start, end)
            patched_map_vectors[str(new_local_index)] = new_payload
            segments.append(
                {
                    "old_local_vector_index": old_local_index,
                    "new_local_vector_index": new_local_index,
                    "kind": "patched" if start_selected else "original",
                    "old_rate_id": vector.rate_id,
                    "rate_id": split_rate_id,
                    "repeat": repeat,
                    "global_vector_start": new_payload["source_map"][0].get("global_vector_index"),
                    "global_vector_end": new_payload["source_map"][-1].get("global_vector_index"),
                    "stil_line_start": new_payload["source_map"][0].get("stil_line"),
                    "stil_line_end": new_payload["source_map"][-1].get("stil_line"),
                }
            )
            new_local_index += 1
            start = end

    missing_globals = sorted(target_global_indices - set(covered_globals))
    if missing_globals:
        raise LocalWaitPatchError(
            f"selected global vector indices were not fully covered by patched local vectors: {missing_globals[:8]}"
        )

    patched_map["vectors"] = patched_map_vectors
    return patched_vectors, patched_map, segments, covered_globals


def patch_vectors_for_candidate(
    vectors: list[VectorRecord],
    map_data: dict[str, Any],
    *,
    candidate: dict[str, Any],
    cloned_rate_id: int,
    all_spans_for_rate: bool = False,
) -> tuple[list[VectorRecord], dict[str, Any], list[dict[str, Any]]]:
    if all_spans_for_rate:
        original_vectors = map_data.get("vectors")
        if not isinstance(original_vectors, dict):
            raise LocalWaitPatchError("map.json has no vectors table")
        target_global_indices: set[int] = set()
        for old_local_index, vector in enumerate(vectors):
            if vector.rate_id != int(candidate["rate_id"]):
                continue
            payload = original_vectors.get(str(old_local_index))
            if payload is None:
                raise LocalWaitPatchError(f"map.json missing vectors[{old_local_index}]")
            source_map = list(payload.get("source_map", []))
            if len(source_map) != vector.repeat:
                raise LocalWaitPatchError(
                    f"map.json vectors[{old_local_index}] source_map length {len(source_map)} != repeat {vector.repeat}"
                )
            for item in source_map:
                global_index = item.get("global_vector_index")
                if global_index is not None:
                    target_global_indices.add(int(global_index))
        if not target_global_indices:
            raise LocalWaitPatchError(f"no vectors found for rate_id={candidate['rate_id']}")
        patched_vectors, patched_map, segments, covered_globals = patch_vectors_for_global_indices(
            vectors,
            map_data,
            original_rate_id=int(candidate["rate_id"]),
            cloned_rate_id=cloned_rate_id,
            target_global_indices=target_global_indices,
        )
        if sorted(covered_globals) != sorted(target_global_indices):
            raise LocalWaitPatchError("all-span candidate global vector set was not fully covered")
        return patched_vectors, patched_map, segments

    global_start = candidate.get("global_vector_start")
    global_end = candidate.get("global_vector_end")
    if global_start is None or global_end is None:
        raise LocalWaitPatchError("candidate does not include global_vector_start/global_vector_end")

    patched_vectors, patched_map, segments, covered_globals = patch_vectors_for_global_indices(
        vectors,
        map_data,
        original_rate_id=int(candidate["rate_id"]),
        cloned_rate_id=cloned_rate_id,
        target_global_indices=set(range(int(global_start), int(global_end) + 1)),
    )
    expected_globals = list(range(int(global_start), int(global_end) + 1))
    if sorted(covered_globals) != expected_globals:
        raise LocalWaitPatchError("candidate global vector range was not fully covered by patched local vectors")
    for item in segments:
        if item["kind"] == "original" and item["global_vector_start"] is not None:
            if int(item["global_vector_start"]) < int(global_start):
                item["kind"] = "prefix"
            elif int(item["global_vector_end"]) > int(global_end):
                item["kind"] = "suffix"
    return patched_vectors, patched_map, segments


def patch_local_wait_candidate(
    pattern_bytes: bytes,
    *,
    map_data: dict[str, Any],
    report_data: dict[str, Any],
    candidate_index: int,
    extra_ticks: int | None = None,
    target_global_start: int | None = None,
    target_global_end: int | None = None,
    all_spans_for_rate: bool = False,
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    candidate = dict(load_candidate_from_report(report_data, candidate_index))
    applied_extra_ticks = int(candidate["minimum_extra_ticks"]) if extra_ticks is None else int(extra_ticks)
    if applied_extra_ticks < int(candidate["minimum_extra_ticks"]):
        raise LocalWaitPatchError(
            f"extra_ticks {applied_extra_ticks} is below minimum_extra_ticks {candidate['minimum_extra_ticks']}"
        )
    if target_global_start is not None or target_global_end is not None:
        if candidate.get("global_vector_start") is None or candidate.get("global_vector_end") is None:
            raise LocalWaitPatchError("candidate has no global_vector range for target override")
        override_start = int(candidate["global_vector_start"]) if target_global_start is None else int(target_global_start)
        override_end = int(candidate["global_vector_end"]) if target_global_end is None else int(target_global_end)
        if override_start > override_end:
            raise LocalWaitPatchError("target global range start must be <= end")
        if override_start < int(candidate["global_vector_start"]) or override_end > int(candidate["global_vector_end"]):
            raise LocalWaitPatchError("target global range must be contained within candidate global range")
        candidate["global_vector_start"] = override_start
        candidate["global_vector_end"] = override_end

    header, rates, slots, entries, loop_blocks = unpack_rate_vector_full(pattern_bytes)
    if loop_blocks:
        raise LocalWaitPatchError("local wait patch helper does not support loop_blocks yet")
    for entry in entries:
        if not isinstance(entry, VectorRecord):
            raise LocalWaitPatchError("local wait patch helper does not support LoopRefRecord entries")
    vectors = [entry for entry in entries if isinstance(entry, VectorRecord)]

    patched_rates, patched_slots, cloned_rate_id = clone_rate_with_local_wait(
        rates,
        slots,
        rate_id=int(candidate["rate_id"]),
        group_index=int(candidate["group_index"]),
        extra_ticks=applied_extra_ticks,
    )
    patched_vectors, patched_map, split_segments = patch_vectors_for_candidate(
        vectors,
        map_data,
        candidate=candidate,
        cloned_rate_id=cloned_rate_id,
        all_spans_for_rate=all_spans_for_rate,
    )
    patched_pattern = pack_rate_vector(
        patched_rates,
        patched_slots,
        patched_vectors,
        tick_hz=header.tick_hz,
        active_pins=header.active_pins,
        loop_blocks=(),
    )
    validate_rate_vector(patched_pattern)

    post_report = analyze_rate_vector_underflow(patched_pattern, map_data=patched_map)
    post_hints = build_practical_runtime_hints(post_report)
    patched_global_indices = []
    for item in split_segments:
        if item["kind"] != "patched":
            continue
        start = item.get("global_vector_start")
        end = item.get("global_vector_end")
        if start is None or end is None:
            continue
        patched_global_indices.extend(range(int(start), int(end) + 1))

    manifest = {
        "kind": "local_wait_patch",
        "candidate_index": candidate_index,
        "applied_extra_ticks": applied_extra_ticks,
        "candidate": candidate,
        "all_spans_for_rate": all_spans_for_rate,
        "target_global_start": candidate.get("global_vector_start"),
        "target_global_end": candidate.get("global_vector_end"),
        "patched_rate_id": cloned_rate_id,
        "input_pattern_size": len(pattern_bytes),
        "output_pattern_size": len(patched_pattern),
        "patched_global_vector_count": len(patched_global_indices),
        "patched_global_ranges": _collapse_global_indices(patched_global_indices),
        "split_segments": split_segments,
        "post_patch_runtime_hints": {
            "top_chunk_windows": post_hints.get("top_chunk_windows", []),
            "generator_avoidance_candidates": post_hints.get("generator_avoidance_candidates", []),
            "local_wait_candidates": post_hints.get("local_wait_candidates", []),
        },
        "post_patch_summary": post_report.get("summary", {}),
    }
    return patched_pattern, patched_map, manifest


def patch_periodic_local_wait(
    pattern_bytes: bytes,
    *,
    map_data: dict[str, Any],
    report_data: dict[str, Any],
    candidate_index: int,
    interval_vectors: int,
    extra_ticks: int | None = None,
    width_vectors: int = 1,
    target_global_start: int | None = None,
    target_global_end: int | None = None,
    phase_vectors: int = 0,
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    if interval_vectors <= 0:
        raise LocalWaitPatchError("interval_vectors must be > 0")
    if width_vectors <= 0:
        raise LocalWaitPatchError("width_vectors must be > 0")
    if phase_vectors < 0 or phase_vectors >= interval_vectors:
        raise LocalWaitPatchError("phase_vectors must satisfy 0 <= phase_vectors < interval_vectors")

    candidate = dict(load_candidate_from_report(report_data, candidate_index))
    applied_extra_ticks = int(candidate["minimum_extra_ticks"]) if extra_ticks is None else int(extra_ticks)
    if applied_extra_ticks < int(candidate["minimum_extra_ticks"]):
        raise LocalWaitPatchError(
            f"extra_ticks {applied_extra_ticks} is below minimum_extra_ticks {candidate['minimum_extra_ticks']}"
        )

    base_start = int(candidate["global_vector_start"])
    base_end = int(candidate["global_vector_end"])
    override_start = base_start if target_global_start is None else int(target_global_start)
    override_end = base_end if target_global_end is None else int(target_global_end)
    if override_start > override_end:
        raise LocalWaitPatchError("target global range start must be <= end")
    if override_start < base_start or override_end > base_end:
        raise LocalWaitPatchError("target global range must be contained within candidate global range")

    target_globals: set[int] = set()
    cursor = override_start + phase_vectors
    while cursor <= override_end:
        for value in range(cursor, min(cursor + width_vectors, override_end + 1)):
            target_globals.add(value)
        cursor += interval_vectors
    if not target_globals:
        raise LocalWaitPatchError("periodic wait selection produced no target vectors")

    header, rates, slots, entries, loop_blocks = unpack_rate_vector_full(pattern_bytes)
    if loop_blocks:
        raise LocalWaitPatchError("periodic local wait helper does not support loop_blocks yet")
    for entry in entries:
        if not isinstance(entry, VectorRecord):
            raise LocalWaitPatchError("periodic local wait helper does not support LoopRefRecord entries")
    vectors = [entry for entry in entries if isinstance(entry, VectorRecord)]

    patched_rates, patched_slots, cloned_rate_id = clone_rate_with_local_wait(
        rates,
        slots,
        rate_id=int(candidate["rate_id"]),
        group_index=int(candidate["group_index"]),
        extra_ticks=applied_extra_ticks,
    )
    patched_vectors, patched_map, split_segments, covered_globals = patch_vectors_for_global_indices(
        vectors,
        map_data,
        original_rate_id=int(candidate["rate_id"]),
        cloned_rate_id=cloned_rate_id,
        target_global_indices=target_globals,
    )
    patched_pattern = pack_rate_vector(
        patched_rates,
        patched_slots,
        patched_vectors,
        tick_hz=header.tick_hz,
        active_pins=header.active_pins,
        loop_blocks=(),
    )
    validate_rate_vector(patched_pattern)

    post_report = analyze_rate_vector_underflow(patched_pattern, map_data=patched_map)
    post_hints = build_practical_runtime_hints(post_report)

    manifest = {
        "kind": "periodic_local_wait_patch",
        "candidate_index": candidate_index,
        "applied_extra_ticks": applied_extra_ticks,
        "candidate": candidate,
        "target_global_start": override_start,
        "target_global_end": override_end,
        "interval_vectors": interval_vectors,
        "width_vectors": width_vectors,
        "phase_vectors": phase_vectors,
        "patched_rate_id": cloned_rate_id,
        "input_pattern_size": len(pattern_bytes),
        "output_pattern_size": len(patched_pattern),
        "patched_global_vector_count": len(covered_globals),
        "patched_global_ranges": _collapse_global_indices(covered_globals),
        "split_segments": split_segments,
        "post_patch_runtime_hints": {
            "top_chunk_windows": post_hints.get("top_chunk_windows", []),
            "generator_avoidance_candidates": post_hints.get("generator_avoidance_candidates", []),
            "local_wait_candidates": post_hints.get("local_wait_candidates", []),
        },
        "post_patch_summary": post_report.get("summary", {}),
    }
    return patched_pattern, patched_map, manifest


def patch_periodic_local_wait_global_span(
    pattern_bytes: bytes,
    *,
    map_data: dict[str, Any],
    target_global_start: int,
    target_global_end: int,
    group_index: int,
    interval_vectors: int,
    extra_ticks: int,
    width_vectors: int = 1,
    phase_vectors: int = 0,
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    if interval_vectors <= 0:
        raise LocalWaitPatchError("interval_vectors must be > 0")
    if width_vectors <= 0:
        raise LocalWaitPatchError("width_vectors must be > 0")
    if phase_vectors < 0 or phase_vectors >= interval_vectors:
        raise LocalWaitPatchError("phase_vectors must satisfy 0 <= phase_vectors < interval_vectors")
    if extra_ticks <= 0:
        raise LocalWaitPatchError("extra_ticks must be > 0")
    if target_global_start > target_global_end:
        raise LocalWaitPatchError("target global range start must be <= end")

    header, rates, slots, entries, loop_blocks = unpack_rate_vector_full(pattern_bytes)
    if loop_blocks:
        raise LocalWaitPatchError("global-span periodic local wait helper does not support loop_blocks yet")
    for entry in entries:
        if not isinstance(entry, VectorRecord):
            raise LocalWaitPatchError("global-span periodic local wait helper does not support LoopRefRecord entries")
    vectors = [entry for entry in entries if isinstance(entry, VectorRecord)]

    original_vectors = map_data.get("vectors")
    if not isinstance(original_vectors, dict):
        raise LocalWaitPatchError("map.json has no vectors table")

    target_globals: set[int] = set()
    cursor = target_global_start + phase_vectors
    while cursor <= target_global_end:
        for value in range(cursor, min(cursor + width_vectors, target_global_end + 1)):
            target_globals.add(value)
        cursor += interval_vectors
    if not target_globals:
        raise LocalWaitPatchError("periodic wait selection produced no target vectors")

    rate_ids_to_clone: set[int] = set()
    for old_local_index, vector in enumerate(vectors):
        payload = original_vectors.get(str(old_local_index))
        if payload is None:
            raise LocalWaitPatchError(f"map.json missing vectors[{old_local_index}]")
        source_map = list(payload.get("source_map", []))
        for item in source_map:
            global_index = item.get("global_vector_index")
            if global_index is not None and int(global_index) in target_globals:
                rate_ids_to_clone.add(vector.rate_id)
                break
    if not rate_ids_to_clone:
        raise LocalWaitPatchError("target global range did not intersect any vectors")

    patched_rates = list(rates)
    patched_slots = list(slots)
    cloned_rate_ids: dict[int, int] = {}
    for rate_id in sorted(rate_ids_to_clone):
        patched_rates, patched_slots, cloned_rate_id = clone_rate_with_local_wait(
            patched_rates,
            patched_slots,
            rate_id=rate_id,
            group_index=group_index,
            extra_ticks=extra_ticks,
        )
        cloned_rate_ids[rate_id] = cloned_rate_id

    patched_vectors, patched_map, split_segments, covered_globals = patch_vectors_for_global_indices_multi_rate(
        vectors,
        map_data,
        cloned_rate_ids=cloned_rate_ids,
        target_global_indices=target_globals,
    )
    patched_pattern = pack_rate_vector(
        patched_rates,
        patched_slots,
        patched_vectors,
        tick_hz=header.tick_hz,
        active_pins=header.active_pins,
        loop_blocks=(),
    )
    validate_rate_vector(patched_pattern)

    post_report = analyze_rate_vector_underflow(patched_pattern, map_data=patched_map)
    post_hints = build_practical_runtime_hints(post_report)

    manifest = {
        "kind": "periodic_local_wait_global_span_patch",
        "target_global_start": target_global_start,
        "target_global_end": target_global_end,
        "interval_vectors": interval_vectors,
        "width_vectors": width_vectors,
        "phase_vectors": phase_vectors,
        "group_index": group_index,
        "applied_extra_ticks": extra_ticks,
        "patched_rate_ids": cloned_rate_ids,
        "input_pattern_size": len(pattern_bytes),
        "output_pattern_size": len(patched_pattern),
        "patched_global_vector_count": len(covered_globals),
        "patched_global_ranges": _collapse_global_indices(covered_globals),
        "split_segments": split_segments,
        "post_patch_runtime_hints": {
            "top_chunk_windows": post_hints.get("top_chunk_windows", []),
            "generator_avoidance_candidates": post_hints.get("generator_avoidance_candidates", []),
            "local_wait_candidates": post_hints.get("local_wait_candidates", []),
        },
        "post_patch_summary": post_report.get("summary", {}),
    }
    return patched_pattern, patched_map, manifest


def write_local_wait_patch_outputs(
    output: Path,
    patched_pattern: bytes,
    patched_map: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    output.write_bytes(patched_pattern)
    output.with_suffix(".map.json").write_text(json.dumps(patched_map, indent=2), encoding="utf-8")
    output.with_suffix(".local_wait_patch.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
