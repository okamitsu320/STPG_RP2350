"""Hash-bound, human-editable wait schedules for compiled rate-vector patterns.

This module keeps the declarative schedule separate from pattern generation.
It describes waits before deterministic chunk boundaries.  ``propose`` and
``set`` only create/edit that sidecar; ``apply`` is the explicit, separately
audited step that creates a new derived rate-vector binary.  No operation in
this module authorizes physical execution.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from stpg.pattern import (
    LoopRefRecord,
    PatternValidationError,
    resolve_max_rates,
    unpack_rate_vector_full,
    validate_rate_vector,
)
from stpg.wide_wait import validate_wait_ticks

from .runtime_wait_model import _RuntimeModelCache, RuntimeWaitModelError, build_runtime_chunks, iter_runtime_events
from .map_lookup import SourceMapOccurrenceResolver
from .json_artifacts import (
    JsonArtifactError,
    json_artifact_identity,
    read_json_artifact,
    resolve_json_artifact,
)


WAIT_SCHEDULE_KIND = "stpg_wait_schedule"
WAIT_SCHEDULE_SCHEMA_VERSION = 1
WAIT_SCHEDULE_VALIDATION_KIND = "stpg_wait_schedule_validation"
WAIT_SCHEDULE_VALIDATION_VERSION = "v1"
DEFAULT_CHUNK_EVENT_CAPACITY = 2_048
DEFAULT_RING_SLOT_COUNT = 3
FORBIDDEN_STIL_RATE_MATCH_NEXT_CHUNK_FIRST_EVENT = "next_chunk_first_event"
FORBIDDEN_STIL_RATE_MATCH_LEGACY_ANY_EVENT = "any_event_in_chunk"


class WaitScheduleError(ValueError):
    """Raised when a wait schedule cannot be safely created or edited."""


def _binary_has_loop(pattern_bytes: bytes, map_path: Path | None = None) -> bool:
    """Detect Loop structure in the binary or its source map.

    A one-vector STIL Loop is deliberately lowered to a bounded
    ``VectorRecord.repeat`` because the firmware LoopRef ABI has no variable
    repeat field for that case.  Such a package is still a Loop-derived
    artifact for wait-profile safety, so the binary alone is insufficient.
    """

    try:
        _header, _rates, _slots, entries, _loop_blocks = unpack_rate_vector_full(pattern_bytes)
    except (ValueError, PatternValidationError):
        entries = ()
    if any(isinstance(entry, LoopRefRecord) for entry in entries):
        return True
    if map_path is None:
        return False
    try:
        map_data = _load_json_object(map_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, WaitScheduleError):
        return False
    vectors = map_data.get("vectors")
    if not isinstance(vectors, dict):
        return False
    for payload in vectors.values():
        if not isinstance(payload, dict):
            continue
        if payload.get("top_level_kind") in {"loop", "loop_vector_repeat", "loop_iteration"}:
            return True
        if payload.get("loop_id") is not None or payload.get("source_loop_id") is not None:
            return True
    return False


def _file_identity(path: Path) -> dict[str, Any]:
    actual = resolve_json_artifact(path)
    if not actual.exists():
        raise WaitScheduleError(f"file does not exist: {path}")
    if not actual.is_file():
        raise WaitScheduleError(f"not a regular file: {actual}")
    try:
        if actual.suffix in {".json", ".gz"}:
            decoded = json_artifact_identity(actual)
            return {
                "path": str(path),
                "size_bytes": decoded["uncompressed_size_bytes"],
                "sha256": decoded["uncompressed_sha256"],
            }
        payload = actual.read_bytes()
    except (OSError, JsonArtifactError) as exc:
        raise WaitScheduleError(f"cannot read file identity: {actual}: {exc}") from exc
    return {
        "path": str(path),
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = read_json_artifact(path)
    except (OSError, UnicodeDecodeError, JsonArtifactError, json.JSONDecodeError) as exc:
        raise WaitScheduleError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WaitScheduleError(f"JSON root must be an object: {path}")
    return value


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WaitScheduleError(f"{label} must be an integer")
    return value


def _as_positive_int(value: Any, label: str) -> int:
    result = _as_int(value, label)
    if result <= 0:
        raise WaitScheduleError(f"{label} must be positive")
    return result


def _normalize_rate_ids(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise WaitScheduleError("forbidden_rate_ids must be a list of integers") from exc
    for value in iterator:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WaitScheduleError("forbidden rate IDs must be non-negative integers")
        result.append(value)
    return sorted(set(result))


def _normalize_rate_units(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    if isinstance(values, (str, bytes)):
        raise WaitScheduleError("forbidden_stil_rate_units must be a list of names")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise WaitScheduleError("forbidden_stil_rate_units must be a list of names") from exc
    for value in iterator:
        if not isinstance(value, str) or not value.strip():
            raise WaitScheduleError("forbidden STIL rate units must be non-empty names")
        result.append(value.strip())
    return sorted(set(result))


def _forbidden_rate_ids(schedule: dict[str, Any]) -> set[int]:
    policy = schedule.get("policy")
    if not isinstance(policy, dict):
        return set()
    try:
        return set(_normalize_rate_ids(policy.get("forbidden_rate_ids", [])))
    except WaitScheduleError as exc:
        raise WaitScheduleError(f"policy.forbidden_rate_ids is invalid: {exc}") from exc


def _forbidden_rate_units(schedule: dict[str, Any]) -> set[str]:
    policy = schedule.get("policy")
    if not isinstance(policy, dict):
        return set()
    try:
        return set(_normalize_rate_units(policy.get("forbidden_stil_rate_units", [])))
    except WaitScheduleError as exc:
        raise WaitScheduleError(f"policy.forbidden_stil_rate_units is invalid: {exc}") from exc


def _forbidden_stil_rate_match(schedule: dict[str, Any]) -> str:
    """Read the schedule's source-rate boundary matching contract.

    Schedules written before the explicit policy field used whole-chunk
    matching.  Keep that interpretation for existing artifacts; all newly
    generated schedules pin the first-event rule.
    """

    policy = schedule.get("policy")
    if not isinstance(policy, dict):
        return FORBIDDEN_STIL_RATE_MATCH_LEGACY_ANY_EVENT
    match = policy.get("forbidden_stil_rate_match")
    if match is None:
        return FORBIDDEN_STIL_RATE_MATCH_LEGACY_ANY_EVENT
    if not isinstance(match, str) or match not in {
        FORBIDDEN_STIL_RATE_MATCH_NEXT_CHUNK_FIRST_EVENT,
        FORBIDDEN_STIL_RATE_MATCH_LEGACY_ANY_EVENT,
    }:
        raise WaitScheduleError(
            f"policy.forbidden_stil_rate_match is unsupported: {match!r}"
        )
    return str(match)


def _blocked_stil_rate_units(
    boundary: dict[str, Any],
    forbidden_rate_units: set[str],
    *,
    match: str,
) -> set[str] | None:
    """Return matching units, or None when the boundary source is unknown."""

    if not forbidden_rate_units:
        return set()
    if match == FORBIDDEN_STIL_RATE_MATCH_NEXT_CHUNK_FIRST_EVENT:
        unit = boundary.get("boundary_rate_unit")
        if not isinstance(unit, str) or not unit:
            return None
        return {unit} & forbidden_rate_units
    raw_rate_units = boundary.get("rate_units")
    if not isinstance(raw_rate_units, list):
        return None
    return {str(item) for item in raw_rate_units} & forbidden_rate_units


def _boundary_forbidden_by_stil_rate(
    boundary: dict[str, Any],
    forbidden_rate_units: set[str],
    *,
    match: str,
) -> bool:
    blocked_units = _blocked_stil_rate_units(
        boundary,
        forbidden_rate_units,
        match=match,
    )
    # An unknown source mapping cannot prove that the boundary is safe.
    return blocked_units is None or bool(blocked_units)


def _ensure_wait_allowed(
    boundary: dict[str, Any],
    *,
    boundary_index: int,
    wait_ticks: int,
    forbidden_rate_ids: set[int],
    forbidden_rate_units: set[str] | None = None,
    forbidden_stil_rate_match: str = FORBIDDEN_STIL_RATE_MATCH_LEGACY_ANY_EVENT,
) -> None:
    if wait_ticks <= 0:
        return
    if boundary_index == 0:
        raise WaitScheduleError(
            "a non-zero wait before the first chunk is unsupported; start at chunk 1 or later"
        )
    raw_rate_ids = boundary.get("rate_ids", [])
    if isinstance(raw_rate_ids, list):
        try:
            boundary_rate_ids = {int(rate_id) for rate_id in raw_rate_ids}
        except (TypeError, ValueError) as exc:
            raise WaitScheduleError(
                f"boundary[{boundary_index}] rate_ids are invalid"
            ) from exc
        blocked = sorted(boundary_rate_ids & forbidden_rate_ids)
        if blocked:
            raise WaitScheduleError(
                f"boundary[{boundary_index}] uses forbidden rate ID(s): {blocked}"
            )
    elif forbidden_rate_ids:
        raise WaitScheduleError(f"boundary[{boundary_index}] rate_ids are invalid")
    forbidden_rate_units = forbidden_rate_units or set()
    if forbidden_rate_units:
        blocked_units = _blocked_stil_rate_units(
            boundary,
            forbidden_rate_units,
            match=forbidden_stil_rate_match,
        )
        if blocked_units is None:
            mapping_detail = (
                "for the scheduled first event"
                if forbidden_stil_rate_match
                == FORBIDDEN_STIL_RATE_MATCH_NEXT_CHUNK_FIRST_EVENT
                else "for this chunk"
            )
            raise WaitScheduleError(
                f"boundary[{boundary_index}] has no source STIL rate-unit mapping "
                f"{mapping_detail}"
            )
        if blocked_units:
            detail = (
                "starts with forbidden STIL rate unit(s)"
                if forbidden_stil_rate_match
                == FORBIDDEN_STIL_RATE_MATCH_NEXT_CHUNK_FIRST_EVENT
                else "uses forbidden STIL rate unit(s)"
            )
            raise WaitScheduleError(
                f"boundary[{boundary_index}] {detail}: {sorted(blocked_units)}"
            )


def _ticks_to_us(wait_ticks: int, tick_hz: int) -> float:
    return float(Decimal(wait_ticks) * Decimal(1_000_000) / Decimal(tick_hz))


def _wait_ticks_from_us(value: Any, tick_hz: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise WaitScheduleError("wait_us must be a number or decimal string")
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation as exc:
        raise WaitScheduleError(f"wait_us is not numeric: {value!r}") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise WaitScheduleError("wait_us must be finite and non-negative")
    tick_value = decimal_value * Decimal(tick_hz) / Decimal(1_000_000)
    integral = tick_value.to_integral_value()
    if tick_value != integral:
        raise WaitScheduleError(
            f"wait_us={value!r} is not an integral tick value at tick_hz={tick_hz}"
        )
    return validate_wait_ticks(int(integral))


def _wait_ticks(boundary: dict[str, Any], tick_hz: int) -> int:
    if "wait_ticks" not in boundary:
        raise WaitScheduleError("boundary is missing wait_ticks; use the set helper for wait_us")
    wait_ticks = validate_wait_ticks(_as_int(boundary["wait_ticks"], "wait_ticks"))
    if "wait_us" in boundary:
        try:
            display_us = Decimal(str(boundary["wait_us"]))
        except InvalidOperation as exc:
            raise WaitScheduleError("wait_us must be numeric") from exc
        if not display_us.is_finite() or display_us < 0:
            raise WaitScheduleError("wait_us must be finite and non-negative")
        expected_us = Decimal(wait_ticks) * Decimal(1_000_000) / Decimal(tick_hz)
        # wait_us is a human-readable companion to the canonical integer.  A
        # small decimal tolerance accommodates JSON float rendering, while a
        # materially different value is rejected rather than silently used.
        tolerance = max(Decimal("1e-12"), expected_us.copy_abs() * Decimal("1e-12"))
        if abs(display_us - expected_us) > tolerance:
            raise WaitScheduleError(
                f"wait_ticks={wait_ticks} disagrees with wait_us={boundary['wait_us']!r}"
            )
    return wait_ticks


def _source_item(resolver: SourceMapOccurrenceResolver, event: Any) -> dict[str, Any]:
    selected = resolver.resolve_readonly(
        event.source_record_index,
        repeat_index=int(event.repeat_index),
        loop_iteration=event.loop_iteration,
        body_record_index=event.body_record_index,
        body_repeat_index=event.body_repeat_index,
        logical_vector_index=event.logical_vector_index,
    )
    if not selected:
        return {}
    keys = (
        "global_vector_index",
        "stil_line",
        "label",
        "pattern_name",
        "waveform_table",
    )
    return {key: selected[key] for key in keys if key in selected}


def _chunk_source_context(
    pattern_bytes: bytes,
    chunks: list[Any],
    map_data: dict[str, Any] | None,
    *,
    _model_cache: _RuntimeModelCache | None = None,
    _map_digest: tuple | None = None,
) -> list[dict[str, Any]]:
    if _model_cache is not None and (_map_digest is not None or map_data is None):
        return _model_cache.source_contexts(
            pattern_bytes, chunks, _map_digest,
            lambda: _compute_chunk_source_context(pattern_bytes, chunks, map_data, _model_cache=_model_cache))
    return _compute_chunk_source_context(pattern_bytes, chunks, map_data, _model_cache=_model_cache)


def _compute_chunk_source_context(
    pattern_bytes: bytes, chunks: list[Any], map_data: dict[str, Any] | None,
    *, _model_cache: _RuntimeModelCache | None,
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = [
        {
            "first_event": {},
            "last_event": {},
            "rate_units": [],
            "boundary_rate_unit": None,
        }
        for _ in chunks
    ]
    rate_units: list[set[str]] = [set() for _ in chunks]
    source_resolver = SourceMapOccurrenceResolver(map_data)
    chunk_index = 0
    for event in iter_runtime_events(pattern_bytes, _model_cache=_model_cache):
        while chunk_index + 1 < len(chunks) and event.event_index >= chunks[chunk_index].event_end_exclusive:
            chunk_index += 1
        if chunk_index >= len(chunks):
            break
        event_data = {
            "event_index": event.event_index,
            "logical_vector_index": event.logical_vector_index,
            "source_record_index": event.source_record_index,
            "repeat_index": event.repeat_index,
            "loop_ref_occurrence_index": event.loop_ref_occurrence_index,
            "loop_id": event.loop_id,
            "loop_iteration": event.loop_iteration,
            "body_record_index": event.body_record_index,
            "body_repeat_index": event.body_repeat_index,
            "rate_id": event.rate_id,
            "group_index": event.group_index,
        }
        is_first_event = not contexts[chunk_index]["first_event"]
        source_data = _source_item(source_resolver, event)
        if source_data:
            event_data["source"] = source_data
            waveform_table = source_data.get("waveform_table")
            if isinstance(waveform_table, str) and waveform_table:
                rate_units[chunk_index].add(waveform_table)
                if is_first_event:
                    contexts[chunk_index]["boundary_rate_unit"] = waveform_table
        if is_first_event:
            contexts[chunk_index]["first_event"] = event_data
        contexts[chunk_index]["last_event"] = event_data
    for context, units in zip(contexts, rate_units):
        context["rate_units"] = sorted(units)
    return contexts


def _stored_map_digest(path: Path) -> tuple:
    actual = resolve_json_artifact(path)
    digest = hashlib.sha256()
    size = 0
    with actual.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return str(actual), size, digest.digest()


def _pattern_chunks(
    pattern_path: Path,
    *,
    map_path: Path | None,
    chunk_event_capacity: int,
    ring_slot_count: int,
    max_rates: int | None = None,
    _model_cache: _RuntimeModelCache | None = None,
    _verified_map: tuple[tuple, dict[str, Any]] | None = None,
) -> tuple[bytes, Any, list[Any], dict[str, Any] | None, tuple | None]:
    cache = _model_cache if _model_cache is not None else _RuntimeModelCache()
    try:
        pattern_bytes = pattern_path.read_bytes()
        validate_rate_vector(pattern_bytes, max_rates=max_rates)
        total_events, chunks = build_runtime_chunks(
            pattern_bytes,
            chunk_event_capacity=chunk_event_capacity,
            _model_cache=cache,
        )
        header = cache.decode(pattern_bytes)[0]
    except (OSError, PatternValidationError, RuntimeWaitModelError) as exc:
        raise WaitScheduleError(f"cannot model rate-vector pattern {pattern_path}: {exc}") from exc
    if total_events <= 0 or not chunks:
        raise WaitScheduleError("pattern has no runtime chunks")
    if ring_slot_count < 2:
        raise WaitScheduleError("ring_slot_count must be at least 2")
    try:
        map_digest = _stored_map_digest(map_path) if map_path is not None else None
        # A warm hit needs both source contexts and a summary from a verified
        # parse. Keep checking the actual stored bytes on every call, including
        # before/after reuse; no full map document is kept in the cache.
        reuse_map = (
            map_digest is not None
            and cache.has_source_contexts(pattern_bytes, chunks, map_digest)
            and cache.map_loop_provenance(map_digest) is not None
        )
        if _verified_map is not None:
            if map_digest is None or _verified_map[0] != map_digest:
                raise WaitScheduleError("preloaded source map differs from stored bytes")
            map_data = _verified_map[1]
        else:
            map_data = _load_json_object(map_path) if map_path is not None and not reuse_map else None
        if map_path is not None and _stored_map_digest(map_path) != map_digest:
            raise WaitScheduleError("source map changed while modeling; retry as a new host conversion")
        if map_digest is not None and map_data is not None:
            cache.remember_map_summary(map_digest, map_data)
    except OSError as exc:
        raise WaitScheduleError(f"cannot read source map {map_path}: {exc}") from exc
    return pattern_bytes, header, chunks, map_data, map_digest


def build_wait_schedule(
    pattern_path: Path,
    *,
    map_path: Path | None = None,
    chunk_event_capacity: int = DEFAULT_CHUNK_EVENT_CAPACITY,
    ring_slot_count: int = DEFAULT_RING_SLOT_COUNT,
    max_rates: int | None = None,
    forbidden_rate_ids: Iterable[int] = (),
    forbidden_stil_rate_units: Iterable[str] = (),
    notes: Iterable[str] = (),
    _model_cache: _RuntimeModelCache | None = None,
) -> dict[str, Any]:
    """Create a zero-wait template with every deterministic chunk boundary."""

    chunk_event_capacity = _as_positive_int(chunk_event_capacity, "chunk_event_capacity")
    ring_slot_count = _as_positive_int(ring_slot_count, "ring_slot_count")
    forbidden_rate_ids = _normalize_rate_ids(forbidden_rate_ids)
    forbidden_stil_rate_units = _normalize_rate_units(forbidden_stil_rate_units)
    cache = _model_cache if _model_cache is not None else _RuntimeModelCache()
    pattern_bytes, header, chunks, map_data, map_digest = _pattern_chunks(
        pattern_path,
        map_path=map_path,
        chunk_event_capacity=chunk_event_capacity,
        ring_slot_count=ring_slot_count,
        max_rates=max_rates,
        _model_cache=cache,
    )
    try:
        contexts = _chunk_source_context(pattern_bytes, chunks, map_data, _model_cache=cache, _map_digest=map_digest)
    except (RuntimeWaitModelError, ValueError, TypeError, KeyError) as exc:
        raise WaitScheduleError(f"cannot resolve chunk source-rate mapping: {exc}") from exc
    if forbidden_stil_rate_units:
        available_units = {
            unit for context in contexts for unit in context.get("rate_units", [])
        }
        if map_path is None or not available_units:
            raise WaitScheduleError(
                "STIL rate-unit exclusion requires --map with source waveform_table mapping"
            )
        unknown_units = sorted(set(forbidden_stil_rate_units) - available_units)
        if unknown_units:
            raise WaitScheduleError(
                f"forbidden STIL rate unit(s) are not used by this pattern: {unknown_units}"
            )
    boundaries: list[dict[str, Any]] = []
    for index, (chunk, context) in enumerate(zip(chunks, contexts)):
        boundary = chunk.to_dict(int(header.tick_hz))
        boundary.pop("chunk_index", None)
        boundary.update(
            {
                "before_chunk_index": index,
                "wait_ticks": 0,
                "wait_us": 0.0,
                "source_context": context,
                "rate_units": list(context.get("rate_units", [])),
                "boundary_rate_unit": context.get("boundary_rate_unit"),
            }
        )
        boundaries.append(boundary)
    total_events = sum(chunk.event_count for chunk in chunks)
    total_consume_ticks = sum(chunk.consume_ticks for chunk in chunks)
    return {
        "kind": WAIT_SCHEDULE_KIND,
        "schema_version": WAIT_SCHEDULE_SCHEMA_VERSION,
        "identity": {
            "pattern": _file_identity(pattern_path),
            "map": _file_identity(map_path) if map_path is not None else None,
        },
        "model": {
            "tick_hz": int(header.tick_hz),
            "chunk_event_capacity": chunk_event_capacity,
            "ring_slot_count": ring_slot_count,
            "max_rate_count": resolve_max_rates(max_rates),
            "expanded_event_count": total_events,
            "chunk_count": len(chunks),
            "total_consume_ticks": total_consume_ticks,
            "total_consume_us": _ticks_to_us(total_consume_ticks, int(header.tick_hz)),
        },
        "policy": {
            "placement": "before_chunk",
            "canonical_wait_unit": "ticks",
            "automatic_insertion": False,
            "pattern_mutation": False,
            "forbidden_rate_ids": forbidden_rate_ids,
            "forbidden_stil_rate_units": forbidden_stil_rate_units,
            "forbidden_stil_rate_match": (
                FORBIDDEN_STIL_RATE_MATCH_NEXT_CHUNK_FIRST_EVENT
            ),
            "physical_execution_authorized": False,
        },
        "boundaries": boundaries,
        "edit_history": [],
        "notes": list(notes),
        "limitations": [
            "zero waits are a template; no wait is automatically inserted",
            "wait_ticks describe a future boundary schedule and do not mutate this pattern",
            "wait acceptance requires the separate Phase 3 host-only model",
            "a validated schedule is not physical approval",
        ],
    }


def _identity_mismatch(expected: dict[str, Any], actual: dict[str, Any], label: str) -> str | None:
    for key in ("size_bytes", "sha256"):
        if expected.get(key) != actual.get(key):
            return f"{label} {key} mismatch: schedule={expected.get(key)!r}, actual={actual.get(key)!r}"
    return None


def _check(checks: dict[str, dict[str, Any]], name: str, passed: bool, detail: str) -> None:
    checks[name] = {"status": "PASS" if passed else "FAIL", "detail": detail}


def validate_wait_schedule(
    pattern_path: Path,
    schedule_path: Path,
    *,
    map_path: Path | None = None,
    expected_chunk_event_capacity: int | None = None,
    expected_ring_slot_count: int | None = None,
    max_rates: int | None = None,
    _model_cache: _RuntimeModelCache | None = None,
    _verified_map: tuple[tuple, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate schedule schema, identities, and exact chunk geometry.

    This function intentionally does not judge service time or Tfill.  It
    returns a report even when validation fails so rejection evidence is
    durable and machine-readable.
    """

    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, dict[str, Any]] = {}
    cache = _model_cache if _model_cache is not None else _RuntimeModelCache()
    try:
        schedule = _load_json_object(schedule_path)
    except WaitScheduleError as exc:
        return {
            "kind": WAIT_SCHEDULE_VALIDATION_KIND,
            "schema_version": WAIT_SCHEDULE_VALIDATION_VERSION,
            "status": "REJECT",
            "errors": [str(exc)],
            "warnings": [],
            "checks": {},
            "physical_execution_authorized": False,
        }

    try:
        actual_pattern_identity = _file_identity(pattern_path)
    except WaitScheduleError as exc:
        actual_pattern_identity = {}
        errors.append(str(exc))

    _check(
        checks,
        "kind_and_schema",
        schedule.get("kind") == WAIT_SCHEDULE_KIND
        and schedule.get("schema_version") == WAIT_SCHEDULE_SCHEMA_VERSION,
        f"kind={schedule.get('kind')!r} schema_version={schedule.get('schema_version')!r}",
    )
    if checks["kind_and_schema"]["status"] == "FAIL":
        errors.append("schedule kind or schema_version is unsupported")

    try:
        schedule_model = schedule.get("model")
        if not isinstance(schedule_model, dict):
            schedule_model = {}
        raw_capacity = schedule_model.get("chunk_event_capacity", DEFAULT_CHUNK_EVENT_CAPACITY)
        raw_ring = schedule_model.get("ring_slot_count", DEFAULT_RING_SLOT_COUNT)
        raw_max_rates = schedule_model.get("max_rate_count", max_rates)
        chunk_event_capacity = _as_positive_int(raw_capacity, "model.chunk_event_capacity")
        ring_slot_count = _as_positive_int(raw_ring, "model.ring_slot_count")
        selected_max_rates = resolve_max_rates(raw_max_rates)
        pattern_bytes, header, chunks, map_data, map_digest = _pattern_chunks(
            pattern_path,
            map_path=map_path,
            chunk_event_capacity=chunk_event_capacity,
            ring_slot_count=ring_slot_count,
            max_rates=selected_max_rates,
            _model_cache=cache,
            _verified_map=_verified_map,
        )
    except (WaitScheduleError, ValueError, TypeError) as exc:
        errors.append(str(exc))
        pattern_bytes = b""
        header = None
        chunks = []
        map_data = None
        map_digest = None
    try:
        source_contexts = (
            _chunk_source_context(pattern_bytes, chunks, map_data, _model_cache=cache, _map_digest=map_digest)
            if header is not None
            else []
        )
    except (RuntimeWaitModelError, ValueError, TypeError, KeyError) as exc:
        source_contexts = []
        errors.append(f"cannot recompute chunk source-rate mapping: {exc}")

    identity = schedule.get("identity")
    if not isinstance(identity, dict):
        errors.append("schedule identity must be an object")
        identity = {}
    pattern_identity = identity.get("pattern")
    mismatch = (
        _identity_mismatch(pattern_identity, actual_pattern_identity, "pattern")
        if isinstance(pattern_identity, dict) and actual_pattern_identity
        else "pattern identity is missing or pattern could not be read"
    )
    _check(checks, "pattern_identity", mismatch is None, mismatch or "pattern SHA-256 and size match")
    if mismatch:
        errors.append(mismatch)

    scheduled_map = identity.get("map")
    if scheduled_map is not None:
        if map_path is None:
            mismatch = "schedule pins a map artifact; --map is required for validation"
        else:
            try:
                mismatch = _identity_mismatch(scheduled_map, _file_identity(map_path), "map")
            except WaitScheduleError as exc:
                mismatch = str(exc)
        _check(checks, "map_identity", mismatch is None, mismatch or "map SHA-256 and size match")
        if mismatch:
            errors.append(mismatch)
    else:
        if map_path is not None:
            warnings.append("--map was supplied but the schedule has no pinned map identity")
        _check(checks, "map_identity", True, "schedule has no map identity")

    model = schedule.get("model")
    if not isinstance(model, dict):
        errors.append("schedule model must be an object")
        model = {}
    if header is not None:
        try:
            actual_capacity = _as_positive_int(model.get("chunk_event_capacity", 0), "model.chunk_event_capacity")
            actual_ring = _as_int(model.get("ring_slot_count", 0), "model.ring_slot_count")
            model_tick_hz = _as_positive_int(model.get("tick_hz", 0), "model.tick_hz")
        except WaitScheduleError as exc:
            actual_capacity = 0
            actual_ring = 0
            model_tick_hz = 0
            errors.append(str(exc))
        _check(
            checks,
            "model_parameters",
            actual_capacity > 0
            and actual_ring >= 2
            and model_tick_hz == int(header.tick_hz),
            f"tick_hz={model.get('tick_hz')!r} capacity={actual_capacity} ring={actual_ring}",
        )
        if checks["model_parameters"]["status"] == "FAIL":
            errors.append("schedule model parameters do not match the compiled pattern")
        if expected_chunk_event_capacity is not None and actual_capacity != expected_chunk_event_capacity:
            errors.append("schedule chunk_event_capacity differs from requested validation value")
        if expected_ring_slot_count is not None and actual_ring != expected_ring_slot_count:
            errors.append("schedule ring_slot_count differs from requested validation value")
        try:
            model_max_rates = resolve_max_rates(model.get("max_rate_count", selected_max_rates))
            max_rates_matches = model_max_rates == selected_max_rates
        except (WaitScheduleError, ValueError, TypeError):
            model_max_rates = 0
            max_rates_matches = False
        _check(
            checks,
            "model_rate_limit",
            max_rates_matches,
            f"max_rate_count={model.get('max_rate_count', selected_max_rates)!r}",
        )
        if not max_rates_matches:
            errors.append("schedule max_rate_count does not match requested validation limit")

        expected_total = sum(chunk.event_count for chunk in chunks)
        expected_consume = sum(chunk.consume_ticks for chunk in chunks)
        try:
            model_matches = (
                _as_int(model.get("expanded_event_count", -1), "model.expanded_event_count") == expected_total
                and _as_int(model.get("chunk_count", -1), "model.chunk_count") == len(chunks)
                and _as_int(model.get("total_consume_ticks", -1), "model.total_consume_ticks") == expected_consume
            )
        except WaitScheduleError as exc:
            errors.append(str(exc))
            model_matches = False
        _check(checks, "model_geometry", model_matches, "expanded events, chunk count, and consume ticks match")
        if not model_matches:
            errors.append("schedule model geometry does not match the compiled pattern")

    boundaries = schedule.get("boundaries")
    boundaries_valid = isinstance(boundaries, list) and len(boundaries) == len(chunks)
    _check(
        checks,
        "boundary_count",
        boundaries_valid,
        f"schedule={len(boundaries) if isinstance(boundaries, list) else None} expected={len(chunks)}",
    )
    if not boundaries_valid:
        errors.append("boundaries must contain exactly one entry per compiled chunk")

    nonzero_waits: list[dict[str, Any]] = []
    if isinstance(boundaries, list) and header is not None:
        for index, chunk in enumerate(chunks):
            if index >= len(boundaries) or not isinstance(boundaries[index], dict):
                errors.append(f"boundary[{index}] must be an object")
                continue
            boundary = boundaries[index]
            geometry = {
                "before_chunk_index": index,
                "event_start": chunk.event_start,
                "event_end_exclusive": chunk.event_end_exclusive,
                "event_count": chunk.event_count,
                "logical_vector_start": chunk.logical_vector_start,
                "logical_vector_end_exclusive": chunk.logical_vector_end_exclusive,
                "rate_ids": list(chunk.rate_ids),
                "consume_ticks": chunk.consume_ticks,
                "consume_us": _ticks_to_us(chunk.consume_ticks, int(header.tick_hz)),
                "contains_rate_transition": len(chunk.rate_ids) > 1,
            }
            policy = schedule.get("policy")
            policy = policy if isinstance(policy, dict) else {}
            match = policy.get("forbidden_stil_rate_match")
            if match == FORBIDDEN_STIL_RATE_MATCH_NEXT_CHUNK_FIRST_EVENT:
                if index < len(source_contexts):
                    geometry["rate_units"] = source_contexts[index]["rate_units"]
                    geometry["boundary_rate_unit"] = source_contexts[index][
                        "boundary_rate_unit"
                    ]
                else:
                    errors.append(
                        f"boundary[{index}] source rate context could not be recomputed"
                    )
            elif "rate_units" in boundary:
                # Legacy schedules retain their original broad matching
                # semantics.  When a map is available, still verify the
                # stored diagnostic context against the actual source map.
                if map_data is not None and index < len(source_contexts):
                    geometry["rate_units"] = source_contexts[index]["rate_units"]
                else:
                    geometry["rate_units"] = sorted(
                        str(item) for item in boundary.get("rate_units", [])
                    )
            mismatches = [
                f"{key}={boundary.get(key)!r} expected={value!r}"
                for key, value in geometry.items()
                if boundary.get(key) != value
            ]
            if mismatches:
                errors.append(f"boundary[{index}] geometry mismatch: {', '.join(mismatches)}")
            try:
                wait_ticks = _wait_ticks(boundary, int(header.tick_hz))
            except WaitScheduleError as exc:
                errors.append(f"boundary[{index}] wait invalid: {exc}")
                continue
            if wait_ticks:
                nonzero_waits.append(
                    {
                        "before_chunk_index": index,
                        "wait_ticks": wait_ticks,
                        "wait_us": _ticks_to_us(wait_ticks, int(header.tick_hz)),
                    }
                )

    policy = schedule.get("policy")
    if not isinstance(policy, dict):
        policy = {}
    try:
        forbidden_stil_rate_match = _forbidden_stil_rate_match(schedule)
    except WaitScheduleError as exc:
        forbidden_stil_rate_match = FORBIDDEN_STIL_RATE_MATCH_LEGACY_ANY_EVENT
        errors.append(str(exc))
    try:
        forbidden_rate_ids = _normalize_rate_ids(policy.get("forbidden_rate_ids", []))
    except WaitScheduleError as exc:
        forbidden_rate_ids = []
        errors.append(f"policy.forbidden_rate_ids is invalid: {exc}")
    try:
        forbidden_rate_units = _normalize_rate_units(
            policy.get("forbidden_stil_rate_units", [])
        )
    except WaitScheduleError as exc:
        forbidden_rate_units = []
        errors.append(f"policy.forbidden_stil_rate_units is invalid: {exc}")
    if forbidden_rate_units:
        available_rate_units = {
            unit
            for context in source_contexts
            for unit in context.get("rate_units", [])
        }
        if map_path is None or not available_rate_units:
            errors.append(
                "forbidden STIL rate policy requires a source map with waveform_table mappings"
            )
        else:
            unknown_rate_units = sorted(set(forbidden_rate_units) - available_rate_units)
            if unknown_rate_units:
                errors.append(
                    f"policy.forbidden_stil_rate_units are not used by the pattern: "
                    f"{unknown_rate_units}"
                )
    if isinstance(boundaries, list):
        for index, boundary in enumerate(boundaries):
            if not isinstance(boundary, dict):
                continue
            raw_wait_ticks = boundary.get("wait_ticks", 0)
            if isinstance(raw_wait_ticks, bool) or not isinstance(raw_wait_ticks, int) or raw_wait_ticks <= 0:
                continue
            if forbidden_rate_ids:
                raw_rate_ids = boundary.get("rate_ids", [])
                if not isinstance(raw_rate_ids, list):
                    continue
                try:
                    blocked = sorted(
                        set(int(rate_id) for rate_id in raw_rate_ids) & set(forbidden_rate_ids)
                    )
                except (TypeError, ValueError):
                    blocked = []
                if blocked:
                    errors.append(
                        f"boundary[{index}] has a non-zero wait on forbidden rate ID(s): {blocked}"
                    )
            if forbidden_rate_units:
                blocked_units = _blocked_stil_rate_units(
                    boundary,
                    set(forbidden_rate_units),
                    match=forbidden_stil_rate_match,
                )
                if blocked_units is None:
                    mapping_detail = (
                        "for the scheduled first event"
                        if forbidden_stil_rate_match
                        == FORBIDDEN_STIL_RATE_MATCH_NEXT_CHUNK_FIRST_EVENT
                        else "for this chunk"
                    )
                    errors.append(
                        f"boundary[{index}] has no source STIL rate-unit mapping "
                        f"{mapping_detail}"
                    )
                    continue
                if blocked_units:
                    detail = (
                        "before a forbidden STIL rate unit"
                        if forbidden_stil_rate_match
                        == FORBIDDEN_STIL_RATE_MATCH_NEXT_CHUNK_FIRST_EVENT
                        else "within a chunk using a forbidden STIL rate unit"
                    )
                    errors.append(
                        f"boundary[{index}] has a non-zero wait {detail}: "
                        f"{sorted(blocked_units)}"
                    )
    declarative_policy = (
        isinstance(policy, dict)
        and policy.get("automatic_insertion") is False
        and policy.get("pattern_mutation") is False
    )
    applied_policy = (
        isinstance(policy, dict)
        and schedule.get("status") == "APPLIED"
        and policy.get("automatic_insertion") is False
        and policy.get("pattern_mutation") is True
        and isinstance(schedule.get("application"), dict)
    )
    policy_valid = declarative_policy or applied_policy
    _check(checks, "non_mutating_policy", policy_valid, "automatic insertion and pattern mutation are disabled")
    if not policy_valid:
        errors.append(
            "schedule policy must be declarative (automatic_insertion=false, pattern_mutation=false) "
            "or a validated APPLIED record with application provenance"
        )

    status = "PASS" if not errors else "REJECT"
    tick_hz = int(header.tick_hz) if header is not None else None
    total_wait_ticks = sum(item["wait_ticks"] for item in nonzero_waits)
    return {
        "kind": WAIT_SCHEDULE_VALIDATION_KIND,
        "schema_version": WAIT_SCHEDULE_VALIDATION_VERSION,
        "status": status,
        "input": {
            "pattern": actual_pattern_identity,
            "schedule": _file_identity(schedule_path),
            "map": _file_identity(map_path) if map_path is not None and map_path.exists() else None,
        },
        "schedule": {
            "kind": schedule.get("kind"),
            "schema_version": schedule.get("schema_version"),
            "chunk_count": len(chunks),
            "nonzero_wait_count": len(nonzero_waits),
            "total_wait_ticks": total_wait_ticks,
            "total_wait_us": _ticks_to_us(total_wait_ticks, tick_hz) if tick_hz else None,
            "waits": nonzero_waits,
            "forbidden_rate_ids": forbidden_rate_ids,
            "forbidden_stil_rate_units": forbidden_rate_units,
            "forbidden_stil_rate_match": forbidden_stil_rate_match,
        },
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "physical_execution_authorized": False,
        "limitations": [
            "PASS means schema, identity, and chunk geometry are valid only",
            "no service-time, Tfill, underflow, timing, or throughput acceptance is performed",
            "the schedule does not mutate or generate a pattern binary",
        ],
    }


def set_wait(
    pattern_path: Path,
    schedule_path: Path,
    *,
    map_path: Path | None = None,
    max_rates: int | None = None,
    before_chunk_index: int,
    wait_ticks: int | None = None,
    wait_us: Any | None = None,
    reason: str,
    output_path: Path,
) -> dict[str, Any]:
    """Set exactly one boundary wait and write a new schedule artifact."""

    if (wait_ticks is None) == (wait_us is None):
        raise WaitScheduleError("provide exactly one of wait_ticks or wait_us")
    if not reason.strip():
        raise WaitScheduleError("reason is required for an edited wait")
    validation = validate_wait_schedule(pattern_path, schedule_path, map_path=map_path, max_rates=max_rates)
    if validation["status"] != "PASS":
        raise WaitScheduleError("cannot edit a schedule that fails validation")
    schedule = _load_json_object(schedule_path)
    if schedule.get("status") == "APPLIED":
        raise WaitScheduleError("an applied schedule is immutable; edit the source schedule instead")
    model = schedule["model"]
    tick_hz = _as_positive_int(model["tick_hz"], "model.tick_hz")
    boundaries = schedule["boundaries"]
    index = _as_int(before_chunk_index, "before_chunk_index")
    if index < 0 or index >= len(boundaries):
        raise WaitScheduleError(f"before_chunk_index {index} is out of range")
    selected_ticks = (
        validate_wait_ticks(_as_int(wait_ticks, "wait_ticks"))
        if wait_ticks is not None
        else _wait_ticks_from_us(wait_us, tick_hz)
    )
    forbidden_rate_ids = _forbidden_rate_ids(schedule)
    forbidden_rate_units = _forbidden_rate_units(schedule)
    forbidden_stil_rate_match = _forbidden_stil_rate_match(schedule)
    updated = deepcopy(schedule)
    boundary = updated["boundaries"][index]
    previous_ticks = _wait_ticks(boundary, tick_hz)
    _ensure_wait_allowed(
        boundary,
        boundary_index=index,
        wait_ticks=selected_ticks,
        forbidden_rate_ids=forbidden_rate_ids,
        forbidden_rate_units=forbidden_rate_units,
        forbidden_stil_rate_match=forbidden_stil_rate_match,
    )
    boundary["wait_ticks"] = selected_ticks
    boundary["wait_us"] = _ticks_to_us(selected_ticks, tick_hz)
    boundary["reason"] = reason
    updated.setdefault("edit_history", []).append(
        {
            "operation": "set_wait",
            "before_chunk_index": index,
            "previous_wait_ticks": previous_ticks,
            "new_wait_ticks": selected_ticks,
            "new_wait_us": _ticks_to_us(selected_ticks, tick_hz),
            "reason": reason,
        }
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return updated


def set_wait_range(
    pattern_path: Path,
    schedule_path: Path,
    *,
    map_path: Path | None = None,
    max_rates: int | None = None,
    first_chunk_index: int,
    last_chunk_index: int | None = None,
    wait_ticks: int | None = None,
    wait_us: Any | None = None,
    reason: str,
    output_path: Path,
) -> dict[str, Any]:
    """Set one wait value over an inclusive range of chunk boundaries.

    This is still a declarative host-only edit.  A non-zero wait before the
    first chunk is rejected because the current rate-vector encoder has no
    preceding group into which that pause can be losslessly placed.
    """

    if (wait_ticks is None) == (wait_us is None):
        raise WaitScheduleError("provide exactly one of wait_ticks or wait_us")
    if not reason.strip():
        raise WaitScheduleError("reason is required for an edited wait range")
    validation = validate_wait_schedule(pattern_path, schedule_path, map_path=map_path, max_rates=max_rates)
    if validation["status"] != "PASS":
        raise WaitScheduleError("cannot edit a schedule that fails validation")
    schedule = _load_json_object(schedule_path)
    if schedule.get("status") == "APPLIED":
        raise WaitScheduleError("an applied schedule is immutable; edit the source schedule instead")

    model = schedule["model"]
    tick_hz = _as_positive_int(model["tick_hz"], "model.tick_hz")
    boundaries = schedule["boundaries"]
    first = _as_int(first_chunk_index, "first_chunk_index")
    last = len(boundaries) - 1 if last_chunk_index is None else _as_int(last_chunk_index, "last_chunk_index")
    if first < 0 or first >= len(boundaries):
        raise WaitScheduleError(f"first_chunk_index {first} is out of range")
    if last < first or last >= len(boundaries):
        raise WaitScheduleError(f"last_chunk_index {last} is out of range")
    selected_ticks = (
        validate_wait_ticks(_as_int(wait_ticks, "wait_ticks"))
        if wait_ticks is not None
        else _wait_ticks_from_us(wait_us, tick_hz)
    )
    if selected_ticks > 0 and first == 0:
        raise WaitScheduleError(
            "a non-zero wait before the first chunk is unsupported; start at chunk 1 or later"
        )

    forbidden_rate_ids = _forbidden_rate_ids(schedule)
    forbidden_rate_units = _forbidden_rate_units(schedule)
    forbidden_stil_rate_match = _forbidden_stil_rate_match(schedule)
    updated = deepcopy(schedule)
    changed = 0
    previous_wait_ticks: list[int] = []
    for index in range(first, last + 1):
        boundary = updated["boundaries"][index]
        _ensure_wait_allowed(
            boundary,
            boundary_index=index,
            wait_ticks=selected_ticks,
            forbidden_rate_ids=forbidden_rate_ids,
            forbidden_rate_units=forbidden_rate_units,
            forbidden_stil_rate_match=forbidden_stil_rate_match,
        )
        previous = _wait_ticks(boundary, tick_hz)
        previous_wait_ticks.append(previous)
        if previous != selected_ticks:
            changed += 1
        boundary["wait_ticks"] = selected_ticks
        boundary["wait_us"] = _ticks_to_us(selected_ticks, tick_hz)
        boundary["reason"] = reason
    updated.setdefault("edit_history", []).append(
        {
            "operation": "set_wait_range",
            "first_chunk_index": first,
            "last_chunk_index": last,
            "boundary_count": last - first + 1,
            "changed_boundary_count": changed,
            "previous_wait_ticks": previous_wait_ticks,
            "new_wait_ticks": selected_ticks,
            "new_wait_us": _ticks_to_us(selected_ticks, tick_hz),
            "reason": reason,
        }
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return updated


def set_wait_periodic(
    pattern_path: Path,
    schedule_path: Path,
    *,
    map_path: Path | None = None,
    max_rates: int | None = None,
    first_chunk_index: int,
    last_chunk_index: int | None = None,
    period: int,
    phase: int = 0,
    wait_ticks: int | None = None,
    wait_us: Any | None = None,
    reason: str,
    output_path: Path,
) -> dict[str, Any]:
    """Set one wait on periodically selected chunk boundaries.

    ``phase`` is evaluated against the absolute boundary index, so a command
    such as ``--every 3 --phase 2`` selects boundaries 2, 5, 8, ... within
    the requested range.  Unselected boundaries are preserved.  This is a
    declarative host-only edit and does not encode or upload a pattern.
    """

    if (wait_ticks is None) == (wait_us is None):
        raise WaitScheduleError("provide exactly one of wait_ticks or wait_us")
    if not reason.strip():
        raise WaitScheduleError("reason is required for an edited periodic wait")
    validation = validate_wait_schedule(pattern_path, schedule_path, map_path=map_path, max_rates=max_rates)
    if validation["status"] != "PASS":
        raise WaitScheduleError("cannot edit a schedule that fails validation")
    schedule = _load_json_object(schedule_path)
    if schedule.get("status") == "APPLIED":
        raise WaitScheduleError("an applied schedule is immutable; edit the source schedule instead")

    model = schedule["model"]
    tick_hz = _as_positive_int(model["tick_hz"], "model.tick_hz")
    boundaries = schedule["boundaries"]
    first = _as_int(first_chunk_index, "first_chunk_index")
    last = len(boundaries) - 1 if last_chunk_index is None else _as_int(last_chunk_index, "last_chunk_index")
    if first < 0 or first >= len(boundaries):
        raise WaitScheduleError(f"first_chunk_index {first} is out of range")
    if last < first or last >= len(boundaries):
        raise WaitScheduleError(f"last_chunk_index {last} is out of range")
    every = _as_positive_int(period, "period")
    phase_value = _as_int(phase, "phase")
    if phase_value < 0 or phase_value >= every:
        raise WaitScheduleError(f"phase {phase_value} must be in [0, {every - 1}]")
    selected_ticks = (
        validate_wait_ticks(_as_int(wait_ticks, "wait_ticks"))
        if wait_ticks is not None
        else _wait_ticks_from_us(wait_us, tick_hz)
    )
    selected_indices = [
        index for index in range(first, last + 1) if index % every == phase_value
    ]
    if selected_ticks > 0 and 0 in selected_indices:
        raise WaitScheduleError(
            "a non-zero wait before the first chunk is unsupported; adjust the periodic phase or range"
        )
    if not selected_indices:
        raise WaitScheduleError("periodic selection contains no boundaries in the requested range")

    forbidden_rate_ids = _forbidden_rate_ids(schedule)
    forbidden_rate_units = _forbidden_rate_units(schedule)
    forbidden_stil_rate_match = _forbidden_stil_rate_match(schedule)
    updated = deepcopy(schedule)
    changed = 0
    previous_wait_ticks: list[int] = []
    for index in selected_indices:
        boundary = updated["boundaries"][index]
        _ensure_wait_allowed(
            boundary,
            boundary_index=index,
            wait_ticks=selected_ticks,
            forbidden_rate_ids=forbidden_rate_ids,
            forbidden_rate_units=forbidden_rate_units,
            forbidden_stil_rate_match=forbidden_stil_rate_match,
        )
        previous = _wait_ticks(boundary, tick_hz)
        previous_wait_ticks.append(previous)
        if previous != selected_ticks:
            changed += 1
        boundary["wait_ticks"] = selected_ticks
        boundary["wait_us"] = _ticks_to_us(selected_ticks, tick_hz)
        boundary["reason"] = reason
    updated.setdefault("edit_history", []).append(
        {
            "operation": "set_wait_periodic",
            "first_chunk_index": first,
            "last_chunk_index": last,
            "period": every,
            "phase": phase_value,
            "selected_boundary_indices": selected_indices,
            "boundary_count": len(selected_indices),
            "changed_boundary_count": changed,
            "previous_wait_ticks": previous_wait_ticks,
            "new_wait_ticks": selected_ticks,
            "new_wait_us": _ticks_to_us(selected_ticks, tick_hz),
            "reason": reason,
        }
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return updated


def propose_wait_schedule(
    pattern_path: Path,
    *,
    map_path: Path | None = None,
    chunk_event_capacity: int = DEFAULT_CHUNK_EVENT_CAPACITY,
    ring_slot_count: int = DEFAULT_RING_SLOT_COUNT,
    max_rates: int | None = None,
    service_time_us: float | None = None,
    service_time_source: str = "",
    safety_margin_us: float = 0.0,
    max_wait_us: float | None = None,
    max_total_wait_us: float | None = None,
    max_wait_count: int | None = None,
    pattern_time_limit_us: float | None = None,
    forbidden_rate_ids: Iterable[int] = (),
    forbidden_stil_rate_units: Iterable[str] = (),
    wait_policy: str = "pressure",
    service_profile_scope: str = "non-loop",
    _model_cache: _RuntimeModelCache | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create an editable schedule and attach the bounded model proposal.

    The planner's partial result is retained even when declared limits reject
    convergence.  This makes the suggested boundaries inspectable without
    presenting a rejected plan as an accepted candidate.  The returned
    schedule is still declarative and does not mutate ``pattern_path``.
    """

    from .runtime_wait_model import analyze_pattern_with_wait_policy
    cache = _model_cache if _model_cache is not None else _RuntimeModelCache()

    forbidden_rate_ids = _normalize_rate_ids(forbidden_rate_ids)
    forbidden_stil_rate_units = _normalize_rate_units(forbidden_stil_rate_units)
    if service_profile_scope not in {"non-loop", "loop-generic"}:
        raise WaitScheduleError(
            "service_profile_scope must be one of: non-loop, loop-generic"
        )
    expected_scope = (
        "loop-generic"
        if _binary_has_loop(pattern_path.read_bytes(), map_path)
        else "non-loop"
    )
    if service_profile_scope != expected_scope:
        raise WaitScheduleError(
            f"service profile scope {service_profile_scope!r} does not match "
            f"artifact scope {expected_scope!r}; a Loop artifact requires an explicitly "
            "declared loop-generic profile"
        )
    schedule = build_wait_schedule(
        pattern_path,
        map_path=map_path,
        chunk_event_capacity=chunk_event_capacity,
        ring_slot_count=ring_slot_count,
        max_rates=max_rates,
        forbidden_rate_ids=forbidden_rate_ids,
        forbidden_stil_rate_units=forbidden_stil_rate_units,
        notes=("generated by bounded host-only wait proposal; human review required",),
        _model_cache=cache,
    )
    schedule["policy"]["wait_planning_policy"] = wait_policy
    schedule["policy"]["service_profile_scope"] = service_profile_scope
    forbidden_stil_rate_match = _forbidden_stil_rate_match(schedule)
    model_report = analyze_pattern_with_wait_policy(
        pattern_path.read_bytes(),
        chunk_event_capacity=chunk_event_capacity,
        ring_slot_count=ring_slot_count,
        service_time_us=service_time_us,
        service_time_source=service_time_source,
        safety_margin_us=safety_margin_us,
        max_wait_us=max_wait_us,
        max_total_wait_us=max_total_wait_us,
        max_wait_count=max_wait_count,
        pattern_time_limit_us=pattern_time_limit_us,
        forbidden_wait_chunks={
            int(boundary["before_chunk_index"])
            for boundary in schedule["boundaries"]
            if (
                set(int(rate_id) for rate_id in boundary.get("rate_ids", []))
                & set(forbidden_rate_ids)
            )
            or _boundary_forbidden_by_stil_rate(
                boundary,
                set(forbidden_stil_rate_units),
                match=forbidden_stil_rate_match,
            )
        },
        wait_policy=wait_policy,
        _model_cache=cache,
    )
    if isinstance(model_report.get("wait_plan"), dict):
        model_report["wait_plan"]["service_profile_scope"] = service_profile_scope
    plan = model_report.get("wait_plan", {})
    tick_hz = _as_positive_int(schedule["model"]["tick_hz"], "model.tick_hz")
    proposal_waits = plan.get("schedule", {}).get("waits", [])
    if not isinstance(proposal_waits, list):
        raise WaitScheduleError("wait planner returned a malformed schedule")
    by_chunk: dict[int, dict[str, Any]] = {}
    skipped_forbidden_boundaries: list[int] = []
    for item in proposal_waits:
        if not isinstance(item, dict):
            continue
        index = int(item["before_chunk_index"])
        boundary = schedule["boundaries"][index]
        boundary_rate_ids = {
            int(rate_id) for rate_id in boundary.get("rate_ids", [])
        }
        forbidden_by_stil_rate = _boundary_forbidden_by_stil_rate(
            boundary,
            set(forbidden_stil_rate_units),
            match=forbidden_stil_rate_match,
        )
        if (
            boundary_rate_ids & set(forbidden_rate_ids)
            or forbidden_by_stil_rate
        ):
            skipped_forbidden_boundaries.append(index)
            continue
        if index not in by_chunk:
            by_chunk[index] = dict(item)
            continue
        # A greedy planner may add a second payment at the same boundary.
        # The editable schedule has one canonical entry per boundary, so keep
        # the sum and retain an explicit indication of the coalescing.
        by_chunk[index]["wait_ticks"] = int(by_chunk[index].get("wait_ticks", 0)) + int(item["wait_ticks"])
        by_chunk[index]["wait_us"] = _ticks_to_us(int(by_chunk[index]["wait_ticks"]), tick_hz)
        by_chunk[index]["reason"] = "coalesced_host_model_proposal"
    for boundary in schedule["boundaries"]:
        index = int(boundary["before_chunk_index"])
        proposal = by_chunk.get(index)
        if proposal is None:
            continue
        wait_ticks = validate_wait_ticks(_as_int(proposal.get("wait_ticks"), "proposal.wait_ticks"))
        boundary["wait_ticks"] = wait_ticks
        boundary["wait_us"] = _ticks_to_us(wait_ticks, tick_hz)
        boundary["reason"] = str(proposal.get("reason", "host_model_proposal"))
    schedule["proposal"] = {
        "kind": "bounded_host_model_wait_proposal",
        "status": plan.get("status"),
        "reason": plan.get("reason"),
        "service_time": plan.get("service_time"),
        "limits": plan.get("limits"),
        "objective": plan.get("objective"),
        "wait_count_written": len(by_chunk),
        "forbidden_rate_ids": forbidden_rate_ids,
        "forbidden_stil_rate_units": forbidden_stil_rate_units,
        "forbidden_stil_rate_match": forbidden_stil_rate_match,
        "skipped_forbidden_boundaries": sorted(set(skipped_forbidden_boundaries)),
        "baseline_underflow_pressure_chunks": plan.get("schedule", {}).get(
            "baseline_underflow_pressure_chunks", []
        ),
        "final_underflow_pressure_chunks": plan.get("schedule", {}).get(
            "final_underflow_pressure_chunks", []
        ),
        "human_action_required": "review, edit if needed, then run cli_wait_acceptance",
    }
    schedule["edit_history"].append(
        {
            "operation": "propose_wait_schedule",
            "planner_status": plan.get("status"),
            "written_wait_count": len(by_chunk),
            "skipped_forbidden_boundary_count": len(set(skipped_forbidden_boundaries)),
        }
    )
    return schedule, model_report


def render_wait_schedule_text(schedule: dict[str, Any]) -> str:
    """Render a compact boundary table suitable for human review."""

    model = schedule.get("model", {})
    lines = [
        f"kind={schedule.get('kind')} schema={schedule.get('schema_version')}",
        f"tick_hz={model.get('tick_hz')} chunks={model.get('chunk_count')} "
        f"capacity={model.get('chunk_event_capacity')} ring={model.get('ring_slot_count')}",
        (
            "before_chunk event_start event_end entry_waveform_table "
            "chunk_waveform_tables wait_ticks wait_us"
        ),
    ]
    for boundary in schedule.get("boundaries", []):
        rate_units = boundary.get("rate_units", [])
        if not isinstance(rate_units, list):
            rate_units = []
        lines.append(
            f"{boundary.get('before_chunk_index')} {boundary.get('event_start')} "
            f"{boundary.get('event_end_exclusive')} "
            f"{boundary.get('boundary_rate_unit')} "
            f"{','.join(str(unit) for unit in rate_units)} "
            f"{boundary.get('wait_ticks')} {boundary.get('wait_us')}"
        )
    return "\n".join(lines)
