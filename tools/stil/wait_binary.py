"""Apply an accepted wait schedule to a derived rate-vector artifact.

The declarative schedule remains the review boundary.  This module only
mutates a copy supplied by the caller, requires a host-only acceptance report,
and records enough provenance to verify the resulting binary.  It deliberately
does not perform upload, transport, or physical execution.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Iterable

from stpg.pattern import (
    LoopRefRecord,
    PatternValidationError,
    RateDesc,
    RateDesc32,
    RateSlot,
    RateSlot32,
    VectorRecord,
    pack_rate_vector,
    pack_wide_rate_vector,
    resolve_max_rates,
    unpack_rate_vector_full,
    validate_rate_vector,
)
from stpg.constants import (
    STPG_DEFAULT_TICK_HZ, STPG_MAX_LOOP_BLOCKS, STPG_RATEVEC_HEADER_SIZE,
    STPG_RATE_DESC_SIZE, STPG_RATE_DESC32_SIZE, STPG_RATE_SLOT_SIZE,
    STPG_RATE_SLOT32_SIZE, STPG_VECTOR_RECORD_SIZE, STPG_LOOP_BLOCK_HEADER_SIZE,
)
from stpg.wide_wait import validate_wait_ticks

from .device_precheck import inspect_pattern_requirements
from .runtime_wait_model import _RuntimeModelCache
from .map_lookup import (source_map_entry_for_occurrence, source_map_slice_for_repeats,
                         validate_source_map_compression, COMPRESSED_LOOP_KINDS)
from .wait_schedule import (
    DEFAULT_CHUNK_EVENT_CAPACITY,
    DEFAULT_RING_SLOT_COUNT,
    WaitScheduleError,
    _load_json_object,
    build_wait_schedule,
    validate_wait_schedule,
)
from .json_artifacts import (
    JsonArtifactError, json_artifact_identity, read_json_artifact,
    resolve_json_artifact, write_json_artifact,
)


WAIT_PATCH_KIND = "stpg_wait_binary_patch"
WAIT_PATCH_NAME = "pattern.wait.patch.json"
WAIT_SOURCE_NAME = "pattern.wait.source.json"
FORMAT_VALIDATION_NAME = "pattern.format.validation.json"


class WaitBinaryError(ValueError):
    """Raised when an accepted schedule cannot be encoded safely."""


def _identity(path: Path) -> dict[str, Any]:
    actual = resolve_json_artifact(path)
    if not actual.is_file():
        raise WaitBinaryError(f"not a regular file: {path}")
    identity = json_artifact_identity(actual)
    return {
        "path": str(path),
        "size_bytes": identity["uncompressed_size_bytes"],
        "sha256": identity["uncompressed_sha256"],
    }


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = read_json_artifact(path)
    except (OSError, UnicodeDecodeError, JsonArtifactError, json.JSONDecodeError) as exc:
        raise WaitBinaryError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WaitBinaryError(f"JSON root must be an object: {path}")
    return value


def _rate_groups(rate: RateDesc | RateDesc32, slots: list[RateSlot | RateSlot32]) -> list[tuple[int, int, int]]:
    groups: list[tuple[int, int, int]] = []
    rate_slots = slots[rate.slot_offset : rate.slot_offset + rate.slot_count]
    start = 0
    while start < len(rate_slots):
        time_ticks = int(rate_slots[start].time_ticks)
        end = start + 1
        while end < len(rate_slots) and int(rate_slots[end].time_ticks) == time_ticks:
            end += 1
        groups.append((start, end, time_ticks))
        start = end
    if not groups:
        raise WaitBinaryError("rate has no emitted groups")
    return groups


def _clone_rate(
    rates: list[RateDesc | RateDesc32],
    slots: list[RateSlot | RateSlot32],
    *,
    rate_id: int,
    adjustments: dict[int, int],
    force_wide: bool,
) -> tuple[int, bool]:
    if rate_id < 0 or rate_id >= len(rates):
        raise WaitBinaryError(f"rate_id {rate_id} is out of range")
    if not adjustments or any(value <= 0 for value in adjustments.values()):
        raise WaitBinaryError("rate adjustments must contain positive waits")
    base_rate = rates[rate_id]
    groups = _rate_groups(base_rate, slots)
    for group_index in adjustments:
        if group_index < 0 or group_index >= len(groups):
            raise WaitBinaryError(f"group_index {group_index} is out of range for rate[{rate_id}]")

    total_wait = sum(int(value) for value in adjustments.values())
    new_period = int(base_rate.period_ticks) + total_wait
    original_slots = list(slots[base_rate.slot_offset : base_rate.slot_offset + base_rate.slot_count])
    use_wide = force_wide or isinstance(base_rate, RateDesc32) or new_period > 0xFFFF
    for slot_index, slot in enumerate(original_slots):
        group_index = next(
            index for index, (start, end, _time) in enumerate(groups) if start <= slot_index < end
        )
        shift = sum(
            int(wait)
            for index, wait in adjustments.items()
            if index > 0 and index <= group_index
        )
        if int(slot.time_ticks) + shift > 0xFFFF:
            use_wide = True
            break
    new_slots: list[RateSlot | RateSlot32] = []
    for slot_index, slot in enumerate(original_slots):
        group_index = next(
            index for index, (start, end, _time) in enumerate(groups) if start <= slot_index < end
        )
        shift = sum(
            int(wait)
            for index, wait in adjustments.items()
            if index > 0 and index <= group_index
        )
        new_time = int(slot.time_ticks) + shift
        if isinstance(slot, RateSlot32) or use_wide:
            new_slots.append(
                RateSlot32(
                    time_ticks=new_time,
                    flags=int(slot.flags),
                    drive_mask=int(slot.drive_mask),
                    dir_mask=int(slot.dir_mask),
                    sample_mask=int(slot.sample_mask),
                )
            )
        else:
            if new_time > 0xFFFF:
                raise WaitBinaryError("legacy rate slot exceeds uint16; wide encoding was not selected")
            new_slots.append(
                RateSlot(
                    time_ticks=new_time,
                    flags=int(slot.flags),
                    drive_mask=int(slot.drive_mask),
                    dir_mask=int(slot.dir_mask),
                    sample_mask=int(slot.sample_mask),
                )
            )

    new_slot_offset = len(slots)
    if use_wide:
        if new_period > 0xFFFFFFFF:
            raise WaitBinaryError("wide rate period exceeds uint32")
        cloned = RateDesc32(
            period_ticks=new_period,
            slot_count=base_rate.slot_count,
            flags=int(base_rate.flags),
            slot_offset=new_slot_offset,
        )
    else:
        if new_period > 0xFFFF:
            raise WaitBinaryError("legacy rate period exceeds uint16")
        cloned = RateDesc(
            period_ticks=new_period,
            slot_count=base_rate.slot_count,
            flags=int(base_rate.flags),
            slot_offset=new_slot_offset,
        )
    rates.append(cloned)
    slots.extend(new_slots)
    return len(rates) - 1, isinstance(cloned, RateDesc32)


def _event_occurrence_key(event: Any) -> tuple[Any, ...]:
    """Return a stable key for one emitted vector occurrence.

    A plain VectorRecord is identified by its top-level record and repeat
    index.  A LoopRefRecord needs the loop block, iteration, body record, and
    body repeat index as well: the same body record is intentionally reused
    across many loop iterations.
    """

    if event.loop_id is None:
        return ("vector", int(event.source_record_index), int(event.repeat_index))
    return (
        "loop",
        int(
            event.loop_ref_occurrence_index
            if event.loop_ref_occurrence_index is not None
            else event.source_record_index
        ),
        int(event.loop_id),
        int(event.loop_iteration),
        int(event.body_record_index),
        int(event.body_repeat_index),
    )


def _collect_wait_modifications(
    events: Iterable[Any],
    schedule: dict[str, Any],
    *,
    total_events: int,
) -> tuple[dict[tuple[Any, ...], dict[int, int]], list[dict[str, Any]], dict[tuple[Any, ...], Any]]:
    """Resolve sparse wait targets without retaining the expanded event stream."""

    modifications: dict[tuple[Any, ...], dict[int, int]] = {}
    applied_boundaries: list[dict[str, Any]] = []
    boundaries = schedule.get("boundaries", [])
    if not isinstance(boundaries, list):
        raise WaitBinaryError("schedule boundaries must be a list")
    pending: dict[int, list[tuple[int, dict[str, Any], int]]] = {}
    for ordinal, boundary in enumerate(boundaries):
        if not isinstance(boundary, dict):
            raise WaitBinaryError("schedule boundary must be an object")
        wait_ticks = validate_wait_ticks(boundary.get("wait_ticks", 0))
        if wait_ticks <= 0:
            continue
        event_start = int(boundary.get("event_start", -1))
        if event_start <= 0 or event_start >= total_events:
            raise WaitBinaryError(
                f"wait before event {event_start} cannot be encoded; first boundary is not supported"
            )
        pending.setdefault(event_start, []).append((ordinal, boundary, wait_ticks))

    targets: dict[tuple[Any, ...], Any] = {}
    applied_by_ordinal: dict[int, dict[str, Any]] = {}
    previous = None
    observed = 0
    for current in events:
        for ordinal, boundary, wait_ticks in pending.pop(observed, ()):
            target = current if current.group_index > 0 else previous
            if target is None:
                raise WaitBinaryError("wait target has no preceding runtime event")
            group_index = int(current.group_index) if current.group_index > 0 else 0
            key = _event_occurrence_key(target)
            targets[key] = target
            group_adjustments = modifications.setdefault(key, {})
            group_adjustments[group_index] = group_adjustments.get(group_index, 0) + wait_ticks
            applied_by_ordinal[ordinal] = {
                "before_chunk_index": int(boundary["before_chunk_index"]),
                "before_event": observed,
                "wait_ticks": wait_ticks,
                "target_source_record_index": int(target.source_record_index),
                "target_repeat_index": int(target.repeat_index),
                "target_logical_vector_index": int(target.logical_vector_index),
                "target_rate_id": int(target.rate_id),
                "target_group_index": group_index,
                "target_loop_id": target.loop_id,
                "target_loop_ref_occurrence_index": target.loop_ref_occurrence_index,
                "target_loop_iteration": target.loop_iteration,
                "target_body_record_index": target.body_record_index,
                "target_body_repeat_index": target.body_repeat_index,
            }
        previous = current
        observed += 1
    if observed != total_events or pending:
        raise WaitBinaryError("runtime event/chunk geometry is inconsistent")
    applied_boundaries = [applied_by_ordinal[key] for key in sorted(applied_by_ordinal)]
    return modifications, applied_boundaries, targets


def _repeat_rate_runs(record: VectorRecord, changes: dict[int, Any]) -> Iterable[tuple[int, int, Any]]:
    """Sparse, maximal equal-rate runs, shared by sizing and materialization."""
    cursor = 0
    run_start = 0
    current: Any = record.rate_id
    for index in sorted(changes):
        if index < cursor or index >= record.repeat:
            raise WaitBinaryError("wait target repeat index is out of range")
        # A gap restores the original rate, without walking every repeat.
        if index > cursor and current != record.rate_id:
            yield run_start, cursor, current
            run_start, current = cursor, record.rate_id
        rate = changes[index]
        if rate != current:
            if index > run_start:
                yield run_start, index, current
            run_start, current = index, rate
        cursor = index + 1
    if cursor < record.repeat and current != record.rate_id:
        yield run_start, cursor, current
        run_start, current = cursor, record.rate_id
    if run_start < record.repeat:
        yield run_start, record.repeat, current


def _wait_layout(entries, loop_blocks, *, occurrence_rates, rate_count, slot_count, wide):
    """Exact packer layout, including original (even unused) loop blocks.

    Does not expand untouched loops, construct maps, or allocate an oversized
    binary. Only touched occurrences and their repeat runs need inspection.
    """
    plain_changes: dict[int, dict[int, Any]] = {}
    loop_changes: dict[int, dict[int, dict[int, dict[int, Any]]]] = {}
    for key, rate in occurrence_rates.items():
        if key[0] == "vector":
            plain_changes.setdefault(key[1], {})[key[2]] = rate
        else:
            loop_changes.setdefault(key[1], {}).setdefault(key[3], {}).setdefault(key[4], {})[key[5]] = rate
    records = 0
    block_count = len(loop_blocks)
    loop_bytes = sum(STPG_LOOP_BLOCK_HEADER_SIZE + STPG_VECTOR_RECORD_SIZE * len(block.body_records)
                     for block in loop_blocks)
    for index, entry in enumerate(entries):
        if isinstance(entry, VectorRecord):
            records += sum(1 for _ in _repeat_rate_runs(entry, plain_changes.get(index, {})))
            continue
        loop = loop_blocks[entry.loop_id]
        iterations = loop_changes.get(index, {})
        if not iterations:
            records += 1
            continue
        cursor = 0
        runs = 0
        for iteration in sorted(iterations):
            if iteration < cursor or iteration >= loop.loop_count:
                raise WaitBinaryError("loop wait target iteration is out of range")
            runs += int(iteration > cursor)
            for body_index, record in enumerate(loop.body_records):
                records += sum(1 for _ in _repeat_rate_runs(record, iterations[iteration].get(body_index, {})))
            cursor = iteration + 1
        runs += int(cursor < loop.loop_count)
        records += runs
        block_count += runs
        loop_bytes += runs * (STPG_LOOP_BLOCK_HEADER_SIZE + STPG_VECTOR_RECORD_SIZE * len(loop.body_records))
    size = (STPG_RATEVEC_HEADER_SIZE
            + rate_count * (STPG_RATE_DESC32_SIZE if wide else STPG_RATE_DESC_SIZE)
            + slot_count * (STPG_RATE_SLOT32_SIZE if wide else STPG_RATE_SLOT_SIZE)
            + records * STPG_VECTOR_RECORD_SIZE + loop_bytes)
    return {"pattern_bytes_after_waits": size, "vector_record_count": records,
            "loop_block_count": block_count, "slot_count": slot_count}


def inspect_wait_schedule_encoding(
    pattern_bytes: bytes,
    *,
    schedule: dict[str, Any],
    max_rates: int | None = None,
    _model_cache: _RuntimeModelCache | None = None,
) -> dict[str, Any]:
    """Check whether a schedule fits the rate-vector encoding budget.

    Waits are encoded by cloning rate descriptors for the distinct
    ``(base_rate, group_adjustments)`` signatures they require.  This check
    performs the same signature accounting as binary application, but never
    mutates the pattern and never writes an artifact.  It is intentionally
    separate from service-time acceptance: a schedule can be accepted by the
    bounded runtime model and still be unrepresentable by the device format.
    """

    try:
        rate_limit = resolve_max_rates(max_rates)
        cache = _model_cache if _model_cache is not None else _RuntimeModelCache()
        header, rates, slots, entries, loop_blocks = cache.decode(pattern_bytes)
    except (ValueError, PatternValidationError) as exc:
        return {
            "kind": "stpg_wait_binary_encoding_check",
            "schema_version": 1,
            "status": "REJECT",
            "errors": [f"cannot parse rate-vector pattern: {exc}"],
            "warnings": [],
            "physical_execution_authorized": False,
        }
    if len(rates) > rate_limit:
        return {
            "kind": "stpg_wait_binary_encoding_check",
            "schema_version": 1,
            "status": "REJECT",
            "errors": [f"pattern contains {len(rates)} rates; selected device limit is {rate_limit}"],
            "warnings": [],
            "physical_execution_authorized": False,
        }
    if any(not isinstance(entry, VectorRecord) for entry in entries):
        unsupported = [type(entry).__name__ for entry in entries if not isinstance(entry, (VectorRecord, LoopRefRecord))]
        if unsupported:
            return {
                "kind": "stpg_wait_binary_encoding_check",
                "schema_version": 1,
                "status": "REJECT",
                "errors": [f"rate-vector contains unsupported entry type(s): {unsupported}"],
                "warnings": [],
                "physical_execution_authorized": False,
            }

    from .runtime_wait_model import build_runtime_chunks, iter_runtime_events

    try:
        total_events, chunks = build_runtime_chunks(
            pattern_bytes,
            chunk_event_capacity=int(schedule["model"]["chunk_event_capacity"]),
            _model_cache=cache,
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        return {
            "kind": "stpg_wait_binary_encoding_check",
            "schema_version": 1,
            "status": "REJECT",
            "errors": [f"runtime event/chunk geometry cannot be inspected: {exc}"],
            "warnings": [],
            "physical_execution_authorized": False,
        }
    if not chunks or total_events != sum(chunk.event_count for chunk in chunks):
        return {
            "kind": "stpg_wait_binary_encoding_check",
            "schema_version": 1,
            "status": "REJECT",
            "errors": ["runtime event/chunk geometry is inconsistent"],
            "warnings": [],
            "physical_execution_authorized": False,
        }

    modifications, applied_boundaries, occurrence_events = _collect_wait_modifications(
        iter_runtime_events(pattern_bytes, _model_cache=cache), schedule, total_events=total_events,
    )
    return _inspect_wait_encoding_targets(
        header, rates, slots, total_events=total_events,
        modifications=modifications, applied_boundaries=applied_boundaries,
        occurrence_events=occurrence_events, rate_limit=rate_limit,
        entries=entries, loop_blocks=loop_blocks, pattern_size_bytes=len(pattern_bytes),
    )


def _inspect_wait_encoding_targets(
    header: Any,
    rates: list[Any],
    slots: list[Any],
    *,
    total_events: int,
    modifications: dict[tuple[Any, ...], dict[int, int]],
    applied_boundaries: list[dict[str, Any]],
    occurrence_events: dict[tuple[Any, ...], Any],
    rate_limit: int,
    entries: Any,
    loop_blocks: Any,
    pattern_size_bytes: int,
) -> dict[str, Any]:
    """Shared feasibility accounting for independently resolved sparse targets."""
    errors: list[str] = []
    capacity_errors: list[str] = []
    occurrence_rates: dict[tuple[Any, ...], Any] = {}

    signature_occurrences: dict[tuple[int, tuple[tuple[int, int], ...]], list[dict[str, int]]] = {}
    force_wide = header.version == 2
    for key, adjustments in sorted(modifications.items(), key=lambda item: repr(item[0])):
        event = occurrence_events.get(key)
        if event is None:
            errors.append(f"wait target occurrence is not present in runtime events: {key!r}")
            continue
        base_rate = rates[event.rate_id]
        groups = _rate_groups(base_rate, slots)
        total_wait = sum(int(value) for value in adjustments.values())
        new_period = int(base_rate.period_ticks) + total_wait
        if new_period > 0xFFFFFFFF:
            errors.append("wide rate period exceeds uint32")
        use_wide = force_wide or isinstance(base_rate, RateDesc32) or new_period > 0xFFFF
        for slot_index, slot in enumerate(
            slots[base_rate.slot_offset : base_rate.slot_offset + base_rate.slot_count]
        ):
            group_index = next(
                index for index, (start, end, _time) in enumerate(groups) if start <= slot_index < end
            )
            shift = sum(
                int(wait)
                for index, wait in adjustments.items()
                if index > 0 and index <= group_index
            )
            if int(slot.time_ticks) + shift > 0xFFFF:
                use_wide = True
        force_wide = force_wide or use_wide
        signature = (int(event.rate_id), tuple(sorted((int(k), int(v)) for k, v in adjustments.items())))
        occurrence_rates[key] = signature
        occurrence = {
            "source_record_index": int(event.source_record_index),
            "repeat_index": int(event.repeat_index),
        }
        if event.loop_id is not None:
            occurrence.update(
                {
                    "loop_ref_occurrence_index": int(
                        event.loop_ref_occurrence_index
                        if event.loop_ref_occurrence_index is not None
                        else event.source_record_index
                    ),
                    "loop_id": int(event.loop_id),
                    "loop_iteration": int(event.loop_iteration),
                    "body_record_index": int(event.body_record_index),
                    "body_repeat_index": int(event.body_repeat_index),
                }
            )
        signature_occurrences.setdefault(signature, []).append(
            occurrence
        )

    signatures = [
        {
            "base_rate_id": base_rate_id,
            "adjustments": [
                {"group_index": int(index), "wait_ticks": int(wait)}
                for index, wait in adjustments
            ],
            "occurrence_count": len(occurrences),
            "occurrences": occurrences,
        }
        for (base_rate_id, adjustments), occurrences in sorted(signature_occurrences.items())
    ]

    original_rate_count = len(rates)
    projected_rate_count = original_rate_count + len(signatures)
    if projected_rate_count > rate_limit:
        errors.append(
            f"wait application would require {projected_rate_count} rates; device limit is "
            f"{rate_limit}"
        )
        capacity_errors.append("rate_count")
    layout = _wait_layout(
        entries, loop_blocks, occurrence_rates=occurrence_rates,
        rate_count=projected_rate_count,
        slot_count=len(slots) + sum(int(rates[signature[0]].slot_count) for signature in signature_occurrences),
        wide=force_wide,
    )
    if layout["loop_block_count"] > STPG_MAX_LOOP_BLOCKS:
        errors.append("wait application would require more than "
                      f"STPG_MAX_LOOP_BLOCKS={STPG_MAX_LOOP_BLOCKS} after loop splitting")
        capacity_errors.append("loop_block_count")
    status = "PASS" if not errors else "REJECT"
    return {
        "kind": "stpg_wait_binary_encoding_check",
        "schema_version": 1,
        "status": status,
        "input": {
            "pattern_version": int(header.version),
            "event_count": total_events,
            "original_rate_count": original_rate_count,
        },
        "schedule": {
            "nonzero_wait_count": len(applied_boundaries),
            "total_wait_ticks": sum(item["wait_ticks"] for item in applied_boundaries),
            "boundaries": applied_boundaries,
        },
        "rate_budget": {
            "max_rate_count": rate_limit,
            "original_rate_count": original_rate_count,
            "added_rate_count": len(signatures),
            "projected_rate_count": projected_rate_count,
            "fits": projected_rate_count <= rate_limit,
        },
        "encoding": {
            "wide_rate_vector_promoted": force_wide,
            "output_pattern_version": 2 if force_wide else 1,
        },
        "capacity": {"pattern_bytes_before_waits": pattern_size_bytes, **layout,
                     "nonzero_wait_count": len(applied_boundaries),
                     "max_loop_blocks": STPG_MAX_LOOP_BLOCKS},
        "capacity_errors": capacity_errors,
        "unique_rate_signatures": signatures,
        "errors": errors,
        "warnings": [],
        "physical_execution_authorized": False,
        "limitations": [
            "host-only encoding layout inspection; no binary is generated",
            "rate-budget fit does not establish service time, Tfill, timing, or underflow safety",
        ],
    }


def _set_map_local_index(payload: dict[str, Any], local_index: int) -> dict[str, Any]:
    updated = deepcopy(payload)
    source_map = updated.get("source_map")
    if not isinstance(source_map, list) or not source_map:
        raise WaitBinaryError("map vector source_map must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    for item in source_map:
        if not isinstance(item, dict):
            raise WaitBinaryError("map source_map entries must be objects")
        copied = dict(item)
        copied["local_vector_index"] = local_index
        normalized.append(copied)
    updated["source_map"] = normalized
    updated["repeat"] = len(normalized)
    return updated


def _pack_rate_vector_variant(
    *,
    rates: list[RateDesc | RateDesc32],
    slots: list[RateSlot | RateSlot32],
    vectors: list[VectorRecord | LoopRefRecord],
    loop_blocks: list[Any] | None = None,
    tick_hz: int,
    active_pins: int,
    original_flags: int,
    force_wide: bool,
    max_rates: int | None = None,
) -> tuple[bytes, bool]:
    loop_blocks = loop_blocks or []
    wide = force_wide or any(isinstance(rate, RateDesc32) for rate in rates)
    if not wide:
        if any(not isinstance(rate, RateDesc) for rate in rates) or any(not isinstance(slot, RateSlot) for slot in slots):
            wide = True
    flags = original_flags & 0x00000002
    if wide:
        wide_rates = [
            rate
            if isinstance(rate, RateDesc32)
            else RateDesc32(rate.period_ticks, rate.slot_count, rate.flags, rate.slot_offset)
            for rate in rates
        ]
        wide_slots = [
            slot
            if isinstance(slot, RateSlot32)
            else RateSlot32(slot.time_ticks, slot.flags, slot.drive_mask, slot.dir_mask, slot.sample_mask)
            for slot in slots
        ]
        payload = pack_wide_rate_vector(
            wide_rates,
            wide_slots,
            vectors,
            tick_hz=tick_hz,
            active_pins=active_pins,
            loop_blocks=loop_blocks,
            extra_header_flags=flags,
            max_rates=max_rates,
        )
    else:
        payload = pack_rate_vector(
            [rate for rate in rates if isinstance(rate, RateDesc)],
            [slot for slot in slots if isinstance(slot, RateSlot)],
            vectors,
            tick_hz=tick_hz,
            active_pins=active_pins,
            loop_blocks=loop_blocks,
            extra_header_flags=flags,
            max_rates=max_rates,
        )
    validate_rate_vector(payload, max_rates=max_rates)
    return payload, wide


def apply_wait_schedule_to_pattern(
    pattern_bytes: bytes,
    *,
    schedule: dict[str, Any],
    map_data: dict[str, Any],
    max_rates: int | None = None,
    _model_cache: _RuntimeModelCache | None = None,
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """Encode schedule waits into a copied rate-vector pattern.

    A wait before a non-first group is represented by shifting that group and
    later groups in the vector.  A wait before a vector's first group is
    represented by extending the preceding vector's period.  This preserves
    the event count and source-map identity while making the pause part of the
    uploaded binary.  For preserved loops, only the affected iteration is
    lowered to plain VectorRecord entries; untouched iteration runs remain
    LoopRefRecord entries.  This is a host-only rewrite and never performs
    upload or physical execution.
    """

    cache = _model_cache if _model_cache is not None else _RuntimeModelCache()
    header, decoded_rates, decoded_slots, entries, loop_blocks = cache.decode(pattern_bytes)
    # Rate cloning is the one mutable consumer: never append to cached tables.
    rates, slots = list(decoded_rates), list(decoded_slots)
    if any(not isinstance(entry, (VectorRecord, LoopRefRecord)) for entry in entries):
        raise WaitBinaryError("rate-vector contains an unsupported entry type")
    map_vectors = map_data.get("vectors")
    if not isinstance(map_vectors, dict):
        raise WaitBinaryError("map.json has no vectors table")
    for local_index, entry in enumerate(entries):
        payload = map_vectors.get(str(local_index))
        if not isinstance(payload, dict):
            raise WaitBinaryError(f"map.json missing vectors[{local_index}]")
        if isinstance(entry, VectorRecord):
            source_map = payload.get("source_map")
            compressed = payload.get("source_map_compression")
            try:
                validate_source_map_compression(payload, repeat=entry.repeat)
            except ValueError as exc:
                raise WaitBinaryError(str(exc)) from exc
            compressed_ok = (
                isinstance(compressed, dict)
                and compressed.get("kind") in COMPRESSED_LOOP_KINDS
                and isinstance(source_map, list)
                and len(source_map) == 1
            )
            if not compressed_ok and (not isinstance(source_map, list) or len(source_map) != entry.repeat):
                raise WaitBinaryError(
                    f"map.json vectors[{local_index}] source_map length does not match repeat"
                )
        else:
            if entry.loop_id < 0 or entry.loop_id >= len(loop_blocks):
                raise WaitBinaryError(
                    f"rate-vector loop reference {entry.loop_id} is out of range"
                )
            body_source_map = payload.get("body_source_map", payload.get("source_map"))
            if not isinstance(body_source_map, list) or len(body_source_map) != len(loop_blocks[entry.loop_id].body_records):
                raise WaitBinaryError(
                    f"map.json vectors[{local_index}] body_source_map length does not match loop body"
                )

    from .runtime_wait_model import build_runtime_chunks, iter_runtime_events

    total_events, chunks = build_runtime_chunks(
        pattern_bytes,
        chunk_event_capacity=int(schedule["model"]["chunk_event_capacity"]),
        _model_cache=cache,
    )
    if not chunks or total_events != sum(chunk.event_count for chunk in chunks):
        raise WaitBinaryError("runtime event/chunk geometry is inconsistent")

    modifications, applied_boundaries, occurrence_events = _collect_wait_modifications(
        iter_runtime_events(pattern_bytes, _model_cache=cache), schedule, total_events=total_events,
    )
    encoding_report = _inspect_wait_encoding_targets(
        header, rates, slots, total_events=total_events,
        modifications=modifications, applied_boundaries=applied_boundaries,
        occurrence_events=occurrence_events, rate_limit=resolve_max_rates(max_rates),
        entries=entries, loop_blocks=loop_blocks, pattern_size_bytes=len(pattern_bytes),
    )
    if encoding_report.get("status") != "PASS":
        details = "; ".join(str(item) for item in encoding_report.get("errors", []))
        raise WaitBinaryError(f"wait encoding feasibility check failed: {details}")

    if not applied_boundaries:
        raise WaitBinaryError("schedule contains no non-zero waits to apply")

    original_rate_count = len(rates)
    signatures: dict[tuple[int, tuple[tuple[int, int], ...]], int] = {}
    occurrence_rate_ids: dict[tuple[Any, ...], int] = {}
    force_wide = header.version == 2
    for key, adjustments in sorted(modifications.items(), key=lambda item: repr(item[0])):
        event = occurrence_events.get(key)
        if event is None:
            raise WaitBinaryError(f"wait target occurrence is not present in runtime events: {key!r}")
        signature = (event.rate_id, tuple(sorted((int(k), int(v)) for k, v in adjustments.items())))
        cloned_rate_id = signatures.get(signature)
        if cloned_rate_id is None:
            cloned_rate_id, clone_is_wide = _clone_rate(
                rates,
                slots,
                rate_id=event.rate_id,
                adjustments=adjustments,
                force_wide=force_wide,
            )
            signatures[signature] = cloned_rate_id
            force_wide = force_wide or clone_is_wide
        occurrence_rate_ids[key] = cloned_rate_id

    rate_limit = resolve_max_rates(max_rates)
    if len(rates) > rate_limit:
        raise WaitBinaryError(
            f"wait application would require {len(rates)} rates; device limit is {rate_limit}; "
            "coarsen the schedule or use a pattern-specific generator"
        )

    patched_entries: list[VectorRecord | LoopRefRecord] = []
    patched_loop_blocks = list(loop_blocks)
    patched_map_vectors: dict[str, Any] = {}
    split_segments: list[dict[str, Any]] = []
    plain_changes: dict[int, dict[int, int]] = {}
    loop_changes: dict[int, dict[int, dict[int, dict[int, int]]]] = {}
    for key, rate_id in occurrence_rate_ids.items():
        if key[0] == "vector":
            plain_changes.setdefault(key[1], {})[key[2]] = rate_id
        else:
            loop_changes.setdefault(key[1], {}).setdefault(key[3], {}).setdefault(key[4], {})[key[5]] = rate_id

    def add_map_entry(payload: dict[str, Any], *, owned: bool = False) -> None:
        local_index = len(patched_entries) - 1
        copied = payload if owned else deepcopy(payload)

        def rebase_source_entry(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            result = {
                **value,
                "local_vector_index": local_index,
                "top_level_entry_index": local_index,
            }
            for nested_field in ("body_source_entries",):
                nested = result.get(nested_field)
                if isinstance(nested, list):
                    result[nested_field] = [rebase_source_entry(item) for item in nested]
            return result

        for field in ("source_map", "body_source_map"):
            values = copied.get(field)
            if isinstance(values, list):
                copied[field] = [rebase_source_entry(item) for item in values]
        patched_map_vectors[str(local_index)] = copied

    def add_loop_run(
        *,
        old_index: int,
        old_entry: LoopRefRecord,
        old_loop: Any,
        old_payload: dict[str, Any],
        iteration_start: int,
        iteration_end: int,
    ) -> None:
        if iteration_end <= iteration_start:
            return
        if len(patched_loop_blocks) >= STPG_MAX_LOOP_BLOCKS:
            raise WaitBinaryError(
                "wait application would require more than "
                f"STPG_MAX_LOOP_BLOCKS={STPG_MAX_LOOP_BLOCKS} after loop splitting"
            )
        new_loop_id = len(patched_loop_blocks)
        patched_loop_blocks.append(
            replace(old_loop, loop_count=iteration_end - iteration_start)
        )
        patched_entries.append(LoopRefRecord(loop_id=new_loop_id))
        body_source_map = old_payload.get("body_source_map", old_payload.get("source_map", []))
        map_payload = deepcopy(old_payload)
        map_payload.update(
            {
                "top_level_kind": "loop",
                "loop_id": new_loop_id,
                "loop_count": iteration_end - iteration_start,
                "repeat": iteration_end - iteration_start,
                "source_loop_id": int(old_entry.loop_id),
                "loop_iteration_start": iteration_start,
                "loop_iteration_end": iteration_end,
                "source_map": deepcopy(body_source_map),
                "body_source_map": deepcopy(body_source_map),
            }
        )

        body_vector_count = int(map_payload.get("body_logical_vector_count", 0) or 0)
        if body_vector_count <= 0 and isinstance(body_source_map, list):
            body_vector_count = sum(
                int(item.get("body_repeat_count", 1))
                for item in body_source_map
                if isinstance(item, dict)
            )
        logical_offset = int(iteration_start) * body_vector_count
        map_payload["logical_vector_base"] = int(
            map_payload.get("logical_vector_base", 0)
        ) + logical_offset
        map_payload["iteration_base"] = int(map_payload.get("iteration_base", 0)) + int(
            iteration_start
        )

        def shift_source_entry(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            shifted = deepcopy(value)
            for field in (
                "global_vector_index",
                "expanded_logical_vector_index",
                "expanded_logical_vector_base",
            ):
                if field in shifted and shifted[field] is not None:
                    shifted[field] = int(shifted[field]) + logical_offset
            if isinstance(shifted.get("body_source_entries"), list):
                shifted["body_source_entries"] = [
                    shift_source_entry(item) for item in shifted["body_source_entries"]
                ]
            shifted["loop_id"] = new_loop_id
            shifted["loop_iteration_base"] = map_payload["iteration_base"]
            return shifted

        map_payload["source_map"] = [
            shift_source_entry(item) for item in map_payload.get("source_map", [])
        ]
        map_payload["body_source_map"] = [
            shift_source_entry(item) for item in map_payload.get("body_source_map", [])
        ]
        add_map_entry(map_payload)
        split_segments.append(
            {
                "old_local_vector_index": old_index,
                "new_local_vector_index": len(patched_entries) - 1,
                "kind": "loop_run",
                "source_loop_id": int(old_entry.loop_id),
                "loop_id": new_loop_id,
                "loop_iteration_start": iteration_start,
                "loop_iteration_end": iteration_end,
            }
        )

    def add_loop_iteration(
        *,
        old_index: int,
        old_entry: LoopRefRecord,
        old_loop: Any,
        old_payload: dict[str, Any],
        iteration: int,
    ) -> None:
        body_source_map = old_payload.get("body_source_map", old_payload.get("source_map", []))
        for body_index, body_record in enumerate(old_loop.body_records):
            source_payload = (
                body_source_map[body_index]
                if isinstance(body_source_map, list) and body_index < len(body_source_map)
                else {}
            )
            for start, end, rate_id in _repeat_rate_runs(
                body_record, loop_changes[old_index][iteration].get(body_index, {}),
            ):
                patched_entries.append(replace(body_record, rate_id=rate_id, repeat=end - start))
                source_map: list[dict[str, Any]] = []
                for body_repeat_index in range(start, end):
                    source_item = source_map_entry_for_occurrence(
                        old_payload,
                        loop_iteration=iteration,
                        body_record_index=body_index,
                        body_repeat_index=body_repeat_index,
                    )
                    if not source_item:
                        source_item = dict(source_payload) if isinstance(source_payload, dict) else {}
                    source_item.update(
                        {
                            "repeat_iteration": body_repeat_index - start,
                            "top_level_repeat_index": body_repeat_index - start,
                            "body_repeat_index": body_repeat_index,
                            "body_record_index": body_index,
                            "loop_id": int(old_entry.loop_id),
                            "loop_iteration": int(old_payload.get("iteration_base", 0)) + iteration,
                        }
                    )
                    source_map.append(source_item)
                patched_map_vectors[str(len(patched_entries) - 1)] = {
                    "top_level_kind": "loop_iteration",
                    "repeat": end - start,
                    "loop_id": int(old_entry.loop_id),
                    "loop_iteration": int(old_payload.get("iteration_base", 0)) + iteration,
                    "body_record_index": body_index,
                    "involved_bits": sorted(
                        bit
                        for bit in range(24)
                        if (int(body_record.drive_enable) | int(body_record.expect_mask)) & (1 << bit)
                    ),
                    "marker_events": [],
                    "source_map": source_map,
                }
                patched_map_vectors[str(len(patched_entries) - 1)]["source_map"] = [
                    {
                        **item,
                        "local_vector_index": len(patched_entries) - 1,
                        "top_level_entry_index": len(patched_entries) - 1,
                    }
                    for item in source_map
                ]
                changed = rate_id != body_record.rate_id
                split_segments.append(
                    {
                        "old_local_vector_index": old_index,
                        "new_local_vector_index": len(patched_entries) - 1,
                        "kind": "loop_iteration_patched" if changed else "loop_iteration_expanded",
                        "source_loop_id": int(old_entry.loop_id),
                        "loop_iteration": iteration,
                        "body_record_index": body_index,
                        "rate_id": rate_id,
                        "repeat": end - start,
                    }
                )

    for old_index, entry in enumerate(entries):
        original_payload = map_vectors[str(old_index)]
        if isinstance(entry, VectorRecord):
            for start, end, rate_id in _repeat_rate_runs(entry, plain_changes.get(old_index, {})):
                patched_entries.append(replace(entry, rate_id=rate_id, repeat=end - start))
                source_map = source_map_slice_for_repeats(
                    original_payload,
                    start=start,
                    end=end - 1,
                )
                for offset, item in enumerate(source_map):
                    item["repeat_iteration"] = offset
                    item["top_level_repeat_index"] = offset
                map_payload = {
                    key: deepcopy(value)
                    for key, value in original_payload.items()
                    if key not in {"source_map", "source_map_compression"}
                }
                map_payload["source_map"] = source_map
                map_payload["repeat"] = end - start
                add_map_entry(map_payload, owned=True)
                split_segments.append(
                    {
                        "old_local_vector_index": old_index,
                        "new_local_vector_index": len(patched_entries) - 1,
                        "kind": "patched" if rate_id != entry.rate_id else "original",
                        "old_rate_id": entry.rate_id,
                        "rate_id": rate_id,
                        "repeat": end - start,
                        "global_vector_start": source_map[0].get("global_vector_index"),
                        "global_vector_end": source_map[-1].get("global_vector_index"),
                    }
                )
            continue

        if not isinstance(entry, LoopRefRecord):
            raise WaitBinaryError("rate-vector contains an unsupported entry type")
        if entry.loop_id < 0 or entry.loop_id >= len(loop_blocks):
            raise WaitBinaryError(f"loop reference {entry.loop_id} is out of range")
        loop = loop_blocks[entry.loop_id]
        patched_iterations = sorted(loop_changes.get(old_index, {}))
        if not patched_iterations:
            patched_entries.append(entry)
            add_map_entry(original_payload)
            continue
        cursor = 0
        for iteration in patched_iterations:
            if iteration < cursor or iteration >= loop.loop_count:
                raise WaitBinaryError(f"loop wait target iteration {iteration} is out of range")
            add_loop_run(
                old_index=old_index,
                old_entry=entry,
                old_loop=loop,
                old_payload=original_payload,
                iteration_start=cursor,
                iteration_end=iteration,
            )
            add_loop_iteration(
                old_index=old_index,
                old_entry=entry,
                old_loop=loop,
                old_payload=original_payload,
                iteration=iteration,
            )
            cursor = iteration + 1
        add_loop_run(
            old_index=old_index,
            old_entry=entry,
            old_loop=loop,
            old_payload=original_payload,
            iteration_start=cursor,
            iteration_end=loop.loop_count,
        )

    patched_map = deepcopy({key: value for key, value in map_data.items() if key != "vectors"})
    patched_map["vectors"] = patched_map_vectors
    payload, output_wide = _pack_rate_vector_variant(
        rates=rates,
        slots=slots,
        vectors=patched_entries,
        loop_blocks=patched_loop_blocks,
        tick_hz=int(header.tick_hz),
        active_pins=int(header.active_pins),
        original_flags=int(header.flags),
        force_wide=force_wide,
        max_rates=max_rates,
    )
    validate_rate_vector(payload, expected_tick_hz=int(header.tick_hz), max_rates=max_rates)
    predicted = encoding_report["capacity"]
    if (len(payload) != predicted["pattern_bytes_after_waits"]
            or len(patched_entries) != predicted["vector_record_count"]
            or len(patched_loop_blocks) != predicted["loop_block_count"]
            or len(slots) != predicted["slot_count"]):
        raise WaitBinaryError("wait encoding layout differs from its capacity prediction")
    output_event_count = sum(1 for _event in iter_runtime_events(payload))
    if output_event_count != total_events:
        raise WaitBinaryError(
            f"wait application changed expanded event count: {total_events} -> {output_event_count}"
        )
    requirements = inspect_pattern_requirements(payload)
    patch_manifest = {
        "kind": WAIT_PATCH_KIND,
        "schema_version": 1,
        "status": "APPLIED",
        "encoding": {
            "placement": "before_chunk_boundary",
            "first_boundary_wait_supported": False,
            "wide_rate_vector_promoted": output_wide,
            "input_pattern_version": int(header.version),
            "output_pattern_version": 2 if output_wide else 1,
            "rate_budget": encoding_report["rate_budget"],
            "capacity": encoding_report["capacity"],
        },
        "input": {
            "size_bytes": len(pattern_bytes),
            "event_count": total_events,
            "rate_count": original_rate_count,
        },
        "output": {
            "size_bytes": len(payload),
            "event_count": output_event_count,
            "rate_count": len(rates),
            "estimated_event_count": requirements.estimated_event_count,
        },
        "applied_boundaries": applied_boundaries,
        "split_segments": split_segments,
        "limitations": [
            "host-only derived artifact; no physical authorization",
            "wait-target loop iterations are expanded into VectorRecord entries; untouched runs remain LoopRefRecord entries",
            "the schedule acceptance remains a bounded model result, not a timing sign-off",
        ],
    }
    return payload, patched_map, patch_manifest


def apply_wait_schedule_files(
    pattern_path: Path,
    schedule_path: Path,
    *,
    map_path: Path,
    acceptance_path: Path,
    output_dir: Path,
    require_empty: bool = True,
    max_rates: int | None = None,
    compact_package_sidecars: bool = False,
    _model_cache: _RuntimeModelCache | None = None,
) -> dict[str, Any]:
    """Write a complete host-only derived wait artifact bundle."""

    cache = _model_cache if _model_cache is not None else _RuntimeModelCache()
    validation = validate_wait_schedule(
        pattern_path, schedule_path, map_path=map_path, max_rates=max_rates, _model_cache=cache)
    if validation.get("status") != "PASS":
        raise WaitBinaryError("source wait schedule validation failed")
    schedule = _read_object(schedule_path)
    if schedule.get("status") == "APPLIED":
        raise WaitBinaryError("an applied schedule cannot be applied again")
    acceptance = _read_object(acceptance_path)
    if acceptance.get("status") != "ACCEPTED":
        raise WaitBinaryError(
            f"binary application requires acceptance status ACCEPTED; got {acceptance.get('status')!r}"
        )
    reported_schedule = acceptance.get("schedule_validation", {}).get("input", {}).get("schedule", {})
    if isinstance(reported_schedule, dict) and reported_schedule.get("sha256"):
        if reported_schedule["sha256"] != _identity(schedule_path)["sha256"]:
            raise WaitBinaryError("acceptance report is not bound to the supplied schedule")

    nonzero_boundaries = [
        boundary
        for boundary in schedule.get("boundaries", [])
        if isinstance(boundary, dict) and int(boundary.get("wait_ticks", 0)) > 0
    ]
    if not nonzero_boundaries:
        # A zero-wait schedule is a valid result: the correct action is to
        # retain the source binary, not to manufacture a byte-identical
        # derived package.  Keep this as a successful no-op so operators do
        # not mistake "no wait required" for a malformed schedule.
        return {
            "kind": WAIT_PATCH_KIND,
            "status": "NOT_REQUIRED",
            "output_dir": str(output_dir),
            "changed": False,
            "reason": (
                "schedule contains no non-zero waits; source pattern remains the "
                "correct artifact and no derived binary was written"
            ),
            "input": {
                "pattern": _identity(pattern_path),
                "schedule": _identity(schedule_path),
                "map": _identity(map_path),
                "acceptance": _identity(acceptance_path),
            },
            "physical_execution_authorized": False,
        }

    try:
        pattern_bytes = pattern_path.read_bytes()
        map_data = _read_object(map_path)
    except OSError as exc:
        raise WaitBinaryError(f"cannot read source artifact: {exc}") from exc
    patched_pattern, patched_map, patch_manifest = apply_wait_schedule_to_pattern(
        pattern_bytes,
        schedule=schedule,
        map_data=map_data,
        max_rates=max_rates,
        _model_cache=cache,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    generated_names = {
        "pattern.bin",
        "pattern.map.json",
        "pattern.wait.json",
        "pattern.wait.source.json",
        "pattern.wait.validation.json",
        "pattern.wait.acceptance.json",
        WAIT_PATCH_NAME,
        FORMAT_VALIDATION_NAME,
    }
    if require_empty and any(output_dir.iterdir()):
        raise WaitBinaryError(f"output directory must be empty: {output_dir}")
    if not require_empty:
        collisions = sorted(name for name in generated_names if resolve_json_artifact(output_dir / name).exists())
        if collisions:
            raise WaitBinaryError(f"output directory already contains generated artifacts: {collisions}")

    output_pattern = output_dir / "pattern.bin"
    output_map = output_dir / "pattern.map.json"
    def write_sidecar(name: str, value: dict[str, Any]) -> Path:
        return write_json_artifact(
            output_dir / name, value,
            compression="auto" if compact_package_sidecars else "none",
            min_compress_bytes=16 * 1024,
            indent=None if compact_package_sidecars else 2,
            sort_keys=True,
            compact_vector_map=compact_package_sidecars and name == "pattern.map.json",
        )

    output_pattern.write_bytes(patched_pattern)
    write_sidecar("pattern.map.json", patched_map)
    write_sidecar(WAIT_SOURCE_NAME, schedule)
    write_sidecar("pattern.wait.acceptance.json", acceptance)
    del map_data, patched_map

    model_data = schedule.get("model", {})
    applied_schedule = build_wait_schedule(
        output_pattern,
        map_path=output_map,
        chunk_event_capacity=int(model_data.get("chunk_event_capacity", DEFAULT_CHUNK_EVENT_CAPACITY)),
        ring_slot_count=int(model_data.get("ring_slot_count", DEFAULT_RING_SLOT_COUNT)),
        max_rates=max_rates,
        _model_cache=cache,
    )
    for source_boundary, applied_boundary in zip(schedule["boundaries"], applied_schedule["boundaries"]):
        wait_ticks = int(source_boundary["wait_ticks"])
        applied_boundary["wait_ticks"] = wait_ticks
        applied_boundary["wait_us"] = wait_ticks * 1_000_000.0 / int(model_data.get("tick_hz", STPG_DEFAULT_TICK_HZ))
        if source_boundary.get("reason") is not None:
            applied_boundary["reason"] = source_boundary["reason"]
    applied_schedule["status"] = "APPLIED"
    applied_schedule["policy"] = {
        "placement": "before_chunk",
        "canonical_wait_unit": "ticks",
        "automatic_insertion": False,
        "pattern_mutation": True,
        "forbidden_rate_ids": list(
            schedule.get("policy", {}).get("forbidden_rate_ids", [])
            if isinstance(schedule.get("policy"), dict)
            else []
        ),
        "physical_execution_authorized": False,
    }
    applied_schedule["application"] = {
        "source_schedule": _identity(schedule_path),
        "source_pattern": _identity(pattern_path),
        "source_acceptance": _identity(acceptance_path),
        "patch_kind": WAIT_PATCH_KIND,
    }
    write_sidecar("pattern.wait.json", applied_schedule)
    applied_validation = validate_wait_schedule(
        output_pattern,
        output_dir / "pattern.wait.json",
        map_path=output_map,
        max_rates=max_rates,
        _model_cache=cache,
    )
    write_sidecar("pattern.wait.validation.json", applied_validation)
    if applied_validation.get("status") != "PASS":
        raise WaitBinaryError("derived applied schedule failed validation")

    patch_manifest["input"]["pattern"] = _identity(pattern_path)
    patch_manifest["input"]["schedule"] = _identity(schedule_path)
    patch_manifest["input"]["map"] = _identity(map_path)
    patch_manifest["input"]["acceptance"] = _identity(acceptance_path)
    patch_manifest["output"]["pattern"] = _identity(output_pattern)
    patch_manifest["output"]["map"] = _identity(output_map)
    write_sidecar(WAIT_PATCH_NAME, patch_manifest)
    patched_requirements = inspect_pattern_requirements(patched_pattern)
    format_report = {
        "kind": "stpg_pattern_format_validation",
        "schema_version": 1,
        "status": "PASS",
        "pattern": _identity(output_pattern),
        "requirements": {
            "mode": patched_requirements.mode,
            "estimated_event_count": patched_requirements.estimated_event_count,
            "size_bytes": len(patched_pattern),
        },
        "physical_execution_authorized": False,
        "limitations": ["format/CRC validation is host-only and is not a physical run result"],
    }
    write_sidecar(FORMAT_VALIDATION_NAME, format_report)
    return {
        "kind": WAIT_PATCH_KIND,
        "status": "VERIFIED",
        "output_dir": str(output_dir),
        "patch_manifest": patch_manifest,
        "validation": applied_validation,
        "physical_execution_authorized": False,
    }
