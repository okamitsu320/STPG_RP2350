"""Host-only runtime consumption and wait-planning model.

This module models the timing contract of the rate-vector streaming path.  It
does not modify a pattern and it does not claim to reproduce the RP2350 DMA
or Core1 scheduler.  The model's purpose is to make the assumptions used by
an automatic wait planner explicit and testable before a physical candidate
is built.

The important distinction from the older underflow analyzer is that chunk
consumption is derived from the firmware's emitted group deltas, rather than
from ``chunk_events * period``.  This matters at vector/rate boundaries and
for patterns whose vector has more than one emitted group.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from math import ceil, isfinite
from typing import Any, Callable, Iterable, Sequence

from stpg.pattern import LoopRefRecord, RateDesc, RateSlot, VectorRecord, unpack_rate_vector_full

from .map_lookup import SourceMapOccurrenceResolver


DEFAULT_CHUNK_EVENT_CAPACITY = 2_048
DEFAULT_RING_SLOT_COUNT = 3
MODEL_VERSION = "runtime-wait-model-v1"


class RuntimeWaitModelError(ValueError):
    """Raised when a pattern or wait policy cannot be modeled safely."""


@dataclass(frozen=True)
class RuntimeEvent:
    event_index: int
    logical_vector_index: int
    source_record_index: int
    repeat_index: int
    rate_id: int
    group_index: int
    desired_delta_ticks: int
    loop_id: int | None = None
    loop_iteration: int | None = None
    body_record_index: int | None = None
    body_repeat_index: int | None = None
    loop_ref_occurrence_index: int | None = None


@dataclass(frozen=True)
class RuntimeChunk:
    chunk_index: int
    event_start: int
    event_end_exclusive: int
    logical_vector_start: int
    logical_vector_end_exclusive: int
    rate_ids: tuple[int, ...]
    consume_ticks: int

    @property
    def event_count(self) -> int:
        return self.event_end_exclusive - self.event_start

    def to_dict(self, tick_hz: int) -> dict[str, Any]:
        return {
            "chunk_index": self.chunk_index,
            "event_start": self.event_start,
            "event_end_exclusive": self.event_end_exclusive,
            "event_count": self.event_count,
            "logical_vector_start": self.logical_vector_start,
            "logical_vector_end_exclusive": self.logical_vector_end_exclusive,
            "rate_ids": list(self.rate_ids),
            "consume_ticks": self.consume_ticks,
            "consume_us": self.consume_ticks * 1_000_000.0 / tick_hz,
            "contains_rate_transition": len(self.rate_ids) > 1,
        }


class _RuntimeModelCache:
    """One conversion's latest binary, geometry and small source context.

    No expanded event array or source-map document is retained. Decoded
    records and RuntimeChunks are immutable; callers receive owned lists and
    context copies. Changing bytes, capacity or max_events recomputes the
    affected work. Validation/CRC and disk identities are never cached here.
    """

    def __init__(self) -> None:
        self._pattern_bytes: bytes | None = None
        self._pattern_digest: bytes | None = None
        self._decoded: tuple | None = None
        self._chunk_options: tuple | None = None
        self._chunks: tuple[RuntimeChunk, ...] = ()
        self._total_events = 0
        self._context_key: tuple | None = None
        self._contexts: list[dict[str, Any]] | None = None
        self._map_summary_key: tuple | None = None
        self._map_has_loop: bool | None = None

    def _bind(self, pattern_bytes: bytes) -> None:
        # bytes equality also protects against an accidental hash-only match.
        if pattern_bytes == self._pattern_bytes:
            return
        self._pattern_bytes = bytes(pattern_bytes)
        self._pattern_digest = hashlib.sha256(self._pattern_bytes).digest()
        self._decoded = None
        self._chunk_options = None
        self._chunks = ()
        self._total_events = 0
        self._context_key = None
        self._contexts = None
        self._map_summary_key = None
        self._map_has_loop = None

    def decode(self, pattern_bytes: bytes) -> tuple:
        self._bind(pattern_bytes)
        if self._decoded is None:
            header, rates, slots, entries, loops = unpack_rate_vector_full(pattern_bytes)
            self._decoded = (header, tuple(rates), tuple(slots), tuple(entries), tuple(loops))
        return self._decoded

    def chunks(self, pattern_bytes: bytes, *, chunk_event_capacity: int, max_events: int) -> tuple[int, list[RuntimeChunk]]:
        self._bind(pattern_bytes)
        options = (chunk_event_capacity, max_events)
        if self._chunk_options != options:
            total, chunks = build_runtime_chunks(
                pattern_bytes, chunk_event_capacity=chunk_event_capacity, max_events=max_events,
                _model_cache=self, _reuse_chunks=False)
            self._total_events, self._chunks = total, tuple(chunks)
            self._chunk_options = options
            self._context_key = None
            self._contexts = None
        return self._total_events, list(self._chunks)

    def source_contexts(
        self, pattern_bytes: bytes, chunks: Sequence[RuntimeChunk], map_digest: tuple | None,
        compute: Callable[[], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        self._bind(pattern_bytes)
        key = (self._pattern_digest, tuple(chunks), map_digest)
        if self._contexts is None or self._context_key != key:
            self._contexts = compute()
            self._context_key = key
        return deepcopy(self._contexts)

    def has_source_contexts(
        self, pattern_bytes: bytes, chunks: Sequence[RuntimeChunk], map_digest: tuple,
    ) -> bool:
        self._bind(pattern_bytes)
        return self._contexts is not None and self._context_key == (
            self._pattern_digest, tuple(chunks), map_digest)

    def remember_map_summary(self, map_digest: tuple, map_data: dict[str, Any]) -> None:
        """Called only after the caller has verified the stored map identity."""

        self._map_summary_key = map_digest
        self._map_has_loop = _map_has_loop_provenance(map_data)

    def map_loop_provenance(self, map_digest: tuple) -> bool | None:
        return self._map_has_loop if self._map_summary_key == map_digest else None


def _map_has_loop_provenance(map_data: Any) -> bool:
    """Include split/expanded plain iterations that no longer have LoopRefs."""

    vectors = map_data.get("vectors") if isinstance(map_data, dict) else None
    if not isinstance(vectors, dict):
        return False
    return any(
        isinstance(payload, dict) and (
            payload.get("top_level_kind") in {"loop", "loop_vector_repeat", "loop_iteration"}
            or payload.get("loop_id") is not None
            or payload.get("source_loop_id") is not None
        )
        for payload in vectors.values()
    )


def _rate_groups(rate: RateDesc, slots: Sequence[RateSlot]) -> tuple[int, ...]:
    """Return one timestamp per emitted group, matching firmware merging."""

    result: list[int] = []
    previous: int | None = None
    for slot in slots[rate.slot_offset : rate.slot_offset + rate.slot_count]:
        time_ticks = int(slot.time_ticks)
        if previous is None or time_ticks != previous:
            result.append(time_ticks)
            previous = time_ticks
    if not result:
        raise RuntimeWaitModelError("rate has no emitted groups")
    return tuple(result)


def _iter_vector_repetitions(
    vectors: Sequence[VectorRecord | LoopRefRecord],
    loop_blocks: Sequence[Any],
) -> Iterable[tuple[int, int, int, VectorRecord, int | None, int | None, int | None, int | None]]:
    """Yield logical vector index, source index, repeat index, and vector.

    Loop references are expanded for the host model.  This is intentionally
    bounded by the caller's event limit so a malformed or huge loop cannot
    silently create an unbounded host-only analysis.
    """

    logical_vector_index = 0
    for source_record_index, entry in enumerate(vectors):
        if isinstance(entry, VectorRecord):
            for repeat_index in range(entry.repeat):
                yield logical_vector_index, source_record_index, repeat_index, entry, None, None, None, None
                logical_vector_index += 1
            continue

        if not isinstance(entry, LoopRefRecord):
            raise RuntimeWaitModelError("unsupported rate-vector entry type")
        if entry.loop_id < 0 or entry.loop_id >= len(loop_blocks):
            raise RuntimeWaitModelError(f"loop reference {entry.loop_id} is out of range")
        loop = loop_blocks[entry.loop_id]
        for loop_iteration in range(loop.loop_count):
            for body_index, body_record in enumerate(loop.body_records):
                if not isinstance(body_record, VectorRecord):
                    raise RuntimeWaitModelError(
                        f"loop {entry.loop_id} body[{body_index}] is not a VectorRecord"
                    )
                for repeat_index in range(body_record.repeat):
                    yield (
                        logical_vector_index,
                        source_record_index,
                        repeat_index,
                        body_record,
                        entry.loop_id,
                        loop_iteration,
                        body_index,
                        repeat_index,
                    )
                    logical_vector_index += 1


def iter_runtime_events(
    pattern_bytes: bytes,
    *,
    max_events: int = 2_000_000,
    _model_cache: _RuntimeModelCache | None = None,
) -> Iterable[RuntimeEvent]:
    """Yield the exact logical group events used for chunk timing.

    ``desired_delta_ticks`` follows
    ``stpg_stream_compute_first_group_delta_ticks`` and its intra-vector
    counterpart: the first group after a previous event includes the previous
    period remainder, while later groups use the timestamp difference.
    """

    header, rates, slots, vectors, loop_blocks = (
        _model_cache.decode(pattern_bytes) if _model_cache is not None
        else unpack_rate_vector_full(pattern_bytes)
    )
    groups_by_rate: dict[int, tuple[int, ...]] = {}
    event_index = 0
    previous_time: int | None = None
    previous_period: int | None = None
    for (
        logical_vector_index,
        source_record_index,
        repeat_index,
        vector,
        loop_id,
        loop_iteration,
        body_record_index,
        body_repeat_index,
    ) in _iter_vector_repetitions(
        vectors, loop_blocks
    ):
        rate = rates[vector.rate_id]
        period_ticks = int(rate.period_ticks)
        groups = groups_by_rate.get(vector.rate_id)
        if groups is None:
            groups = _rate_groups(rate, slots)
            groups_by_rate[vector.rate_id] = groups
        for group_index, time_ticks in enumerate(groups):
            if previous_time is None or previous_period is None:
                desired_delta = time_ticks
            elif group_index == 0:
                desired_delta = previous_period - previous_time + time_ticks
            else:
                previous_group_time = groups[group_index - 1]
                desired_delta = time_ticks - previous_group_time
            if desired_delta < 0:
                raise RuntimeWaitModelError(
                    f"negative emitted delta at event {event_index}: {desired_delta} ticks"
                )
            if event_index >= max_events:
                raise RuntimeWaitModelError(
                    f"expanded runtime event count exceeds max_events={max_events}"
                )
            yield RuntimeEvent(
                event_index=event_index,
                logical_vector_index=logical_vector_index,
                source_record_index=source_record_index,
                repeat_index=repeat_index,
                rate_id=vector.rate_id,
                group_index=group_index,
                desired_delta_ticks=desired_delta,
                loop_id=loop_id,
                loop_iteration=loop_iteration,
                body_record_index=body_record_index,
                body_repeat_index=body_repeat_index,
                loop_ref_occurrence_index=(source_record_index if loop_id is not None else None),
            )
            event_index += 1
            previous_time = time_ticks
            previous_period = period_ticks


def build_runtime_chunks(
    pattern_bytes: bytes,
    *,
    chunk_event_capacity: int = DEFAULT_CHUNK_EVENT_CAPACITY,
    max_events: int = 2_000_000,
    _model_cache: _RuntimeModelCache | None = None,
    _reuse_chunks: bool = True,
) -> tuple[int, list[RuntimeChunk]]:
    """Build fixed-capacity chunks and exact logical consumption windows."""

    if chunk_event_capacity <= 0:
        raise RuntimeWaitModelError("chunk_event_capacity must be positive")
    if _model_cache is not None and _reuse_chunks:
        return _model_cache.chunks(
            pattern_bytes, chunk_event_capacity=chunk_event_capacity, max_events=max_events)

    chunks: list[RuntimeChunk] = []
    current_start = 0
    current_vector_start: int | None = None
    current_vector_end: int | None = None
    current_rate_ids: list[int] = []
    current_ticks = 0
    current_count = 0

    def flush() -> None:
        nonlocal current_start, current_vector_start, current_vector_end
        nonlocal current_rate_ids, current_ticks, current_count
        if current_count == 0:
            return
        assert current_vector_start is not None
        assert current_vector_end is not None
        chunks.append(
            RuntimeChunk(
                chunk_index=len(chunks),
                event_start=current_start,
                event_end_exclusive=current_start + current_count,
                logical_vector_start=current_vector_start,
                logical_vector_end_exclusive=current_vector_end + 1,
                rate_ids=tuple(dict.fromkeys(current_rate_ids)),
                consume_ticks=current_ticks,
            )
        )
        current_start += current_count
        current_vector_start = None
        current_vector_end = None
        current_rate_ids = []
        current_ticks = 0
        current_count = 0

    for event in iter_runtime_events(pattern_bytes, max_events=max_events, _model_cache=_model_cache):
        if current_vector_start is None:
            current_vector_start = event.logical_vector_index
        current_vector_end = event.logical_vector_index
        current_rate_ids.append(event.rate_id)
        current_ticks += event.desired_delta_ticks
        current_count += 1
        if current_count == chunk_event_capacity:
            flush()
    flush()
    total_events = sum(chunk.event_count for chunk in chunks)
    return total_events, chunks


def runtime_chunk_source_contexts(
    pattern_bytes: bytes,
    chunks: Sequence[RuntimeChunk | dict[str, Any]],
    map_data: dict[str, Any] | None,
    *,
    _model_cache: _RuntimeModelCache | None = None,
) -> list[dict[str, Any]]:
    """Return source-rate context for each runtime chunk.

    The compiler's stable source-level rate unit is the STIL
    ``WaveformTable`` name.  Rate-vector integer IDs are an emitted-binary
    detail and may be renumbered by slicing or recompilation.  ``rate_units``
    records every source rate used anywhere in the chunk for diagnostics;
    ``boundary_rate_unit`` identifies only the rate of the chunk's first
    emitted event, which is the rate relevant to a wait placed immediately
    before that chunk.

    A source map is required when the caller asks for this information.  An
    empty result is returned for legacy schedules that do not carry a map;
    policy code must reject a requested source-unit exclusion rather than
    silently treating it as unconstrained.
    """

    if not chunks:
        return []
    rate_units: list[set[str]] = [set() for _ in chunks]
    boundary_rate_units: list[str | None] = [None for _ in chunks]
    first_events_seen: set[int] = set()
    # Runtime events are yielded in monotonically increasing event order, as
    # are the chunk ranges.  Retaining this cursor avoids restarting at the
    # first chunk for every event (O(events * chunks)) on large segmented
    # packages.
    chunk_index = 0
    source_resolver = SourceMapOccurrenceResolver(map_data)
    for event in iter_runtime_events(pattern_bytes, _model_cache=_model_cache):
        while chunk_index + 1 < len(chunks):
            end_exclusive = (
                int(chunks[chunk_index]["event_end_exclusive"])
                if isinstance(chunks[chunk_index], dict)
                else int(chunks[chunk_index].event_end_exclusive)
            )
            if event.event_index < end_exclusive:
                break
            chunk_index += 1
        if chunk_index >= len(chunks):
            break
        is_first_event = chunk_index not in first_events_seen
        first_events_seen.add(chunk_index)
        selected = source_resolver.resolve_readonly(
            event.source_record_index,
            repeat_index=int(event.repeat_index),
            loop_iteration=event.loop_iteration,
            body_record_index=event.body_record_index,
            body_repeat_index=event.body_repeat_index,
            logical_vector_index=event.logical_vector_index,
        )
        if isinstance(selected.get("waveform_table"), str):
            waveform_table = selected["waveform_table"]
            rate_units[chunk_index].add(waveform_table)
            if is_first_event:
                boundary_rate_units[chunk_index] = waveform_table
    return [
        {
            "rate_units": sorted(units),
            "boundary_rate_unit": boundary_rate_unit,
        }
        for units, boundary_rate_unit in zip(rate_units, boundary_rate_units)
    ]


def runtime_chunk_rate_units(
    pattern_bytes: bytes,
    chunks: Sequence[RuntimeChunk | dict[str, Any]],
    map_data: dict[str, Any] | None,
) -> list[list[str]]:
    """Return every source STIL rate unit used by each runtime chunk."""

    return [
        list(context["rate_units"])
        for context in runtime_chunk_source_contexts(pattern_bytes, chunks, map_data)
    ]


def _ticks_from_us(value_us: float, tick_hz: int) -> int:
    if value_us < 0:
        raise RuntimeWaitModelError("time values must be non-negative")
    return ceil(value_us * tick_hz / 1_000_000.0)


def _normalize_forbidden_wait_chunks(
    values: Iterable[int] | None,
    *,
    chunks: Sequence[dict[str, Any]],
) -> set[int]:
    """Validate chunk indices where an explicit wait cannot be represented."""

    if values is None:
        return set()
    result: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeWaitModelError("forbidden wait chunk indices must be integers")
        if value < 0 or value >= len(chunks):
            raise RuntimeWaitModelError(
                f"forbidden wait chunk index {value} is out of range"
            )
        result.add(value)
    return result


def _simulate_ring_schedule(
    chunks: Sequence[dict[str, Any]],
    *,
    ring_slot_count: int,
    service_ticks: int,
    explicit_waits: dict[int, int] | None = None,
    tick_hz: int = 1,
) -> dict[str, Any]:
    """Simulate bounded ring turnover with a serial producer.

    The first ``R-1`` chunks are treated as the initial lead.  Once chunk
    ``i`` completes, its freed slot allows the producer to start the refill
    for chunk ``i + R - 1``.  That refill must be ready when the consumer
    reaches that chunk.  This captures the finite ring horizon that a simple
    whole-pattern cumulative comparison would incorrectly discard.
    """

    waits = explicit_waits or {}
    chunk_count = len(chunks)
    consume_ticks = [int(chunk["consume_ticks"]) for chunk in chunks]
    ready_ticks: list[int | None] = [None] * chunk_count
    producer_available = 0
    consumer_time = 0
    events: list[dict[str, Any]] = []

    for chunk_index in range(chunk_count):
        explicit_wait = int(waits.get(chunk_index, 0))
        required_wait = 0
        if chunk_index >= ring_slot_count - 1:
            ready = ready_ticks[chunk_index]
            if ready is None:
                raise RuntimeWaitModelError(
                    f"missing producer ready time for chunk {chunk_index}"
                )
            required_wait = max(0, ready - consumer_time)
            consumer_time += explicit_wait
            residual_wait = max(0, ready - consumer_time)
            if residual_wait:
                # Model a natural stall as an underflow pressure point so a
                # candidate can be evaluated even before a wait is inserted.
                consumer_time = ready
            else:
                residual_wait = 0
        else:
            consumer_time += explicit_wait
            residual_wait = 0

        start = consumer_time
        end = start + consume_ticks[chunk_index]
        consumer_time = end

        target_chunk = chunk_index + ring_slot_count - 1
        producer_start = None
        producer_ready = None
        if target_chunk < chunk_count:
            producer_start = max(producer_available, end)
            producer_ready = producer_start + service_ticks
            producer_available = producer_ready
            ready_ticks[target_chunk] = producer_ready

        events.append(
            {
                "chunk_index": chunk_index,
                "required_wait_ticks": required_wait,
                "required_wait_us": required_wait * 1_000_000.0 / tick_hz,
                "explicit_wait_ticks": explicit_wait,
                "explicit_wait_us": explicit_wait * 1_000_000.0 / tick_hz,
                "residual_wait_ticks": residual_wait,
                "residual_wait_us": residual_wait * 1_000_000.0 / tick_hz,
                "consumer_start_ticks": start,
                "consumer_end_ticks": end,
                "producer_target_chunk": target_chunk if target_chunk < chunk_count else None,
                "producer_start_ticks": producer_start,
                "producer_ready_ticks": producer_ready,
            }
        )

    return {
        "events": events,
        "underflow_chunks": [
            int(item["chunk_index"])
            for item in events
            if int(item["residual_wait_ticks"]) > 0
        ],
        "consumer_end_ticks": consumer_time,
    }


def build_runtime_model(
    pattern_bytes: bytes,
    *,
    chunk_event_capacity: int = DEFAULT_CHUNK_EVENT_CAPACITY,
    ring_slot_count: int = DEFAULT_RING_SLOT_COUNT,
    max_events: int = 2_000_000,
    _model_cache: _RuntimeModelCache | None = None,
) -> dict[str, Any]:
    """Return a JSON-ready model report without a service-time assumption."""

    if ring_slot_count < 2:
        raise RuntimeWaitModelError("ring_slot_count must be at least 2")
    cache = _model_cache if _model_cache is not None else _RuntimeModelCache()
    header, rates, slots, _vectors, _loops = cache.decode(pattern_bytes)
    total_events, chunks = build_runtime_chunks(
        pattern_bytes,
        chunk_event_capacity=chunk_event_capacity,
        max_events=max_events,
        _model_cache=cache,
    )
    total_ticks = sum(chunk.consume_ticks for chunk in chunks)
    return {
        "model": {
            "version": MODEL_VERSION,
            "tick_hz": int(header.tick_hz),
            "chunk_event_capacity": chunk_event_capacity,
            "ring_slot_count": ring_slot_count,
            "steady_state_lead_slots": ring_slot_count - 1,
            "steady_state_lead_chunks": ring_slot_count - 1,
            "service_time_assumption": "not supplied; no automatic wait candidate is accepted",
        },
        "pattern": {
            "version": int(header.version),
            "rate_count": len(rates),
            "slot_count": len(slots),
            "expanded_event_count": total_events,
            "total_consume_ticks": total_ticks,
            "total_consume_us": total_ticks * 1_000_000.0 / header.tick_hz,
            "chunk_count": len(chunks),
            "chunks": [chunk.to_dict(header.tick_hz) for chunk in chunks],
        },
        "assumptions": [
            "chunk timing uses emitted group desired_delta_ticks, not chunk_events * period",
            "the first emitted event may have a zero logical delta",
            "ring lead is represented as ring_slot_count - 1 chunk horizons",
            "startup fill, DMA scheduling, Core1 request timing, and USB timing are not simulated",
            "this report is host-only and does not modify or repack the input pattern",
        ],
    }


def simulate_explicit_waits(
    model: dict[str, Any],
    *,
    explicit_waits: dict[int, int] | None,
    service_time_us: float,
    service_time_source: str,
    safety_margin_us: float = 0.0,
) -> dict[str, Any]:
    """Simulate a declared boundary schedule under one service-time budget.

    This is a bounded host model.  It exposes the existing ring simulation for
    Phase 3 without treating its result as a physical timing measurement.
    """

    if not isinstance(service_time_source, str) or not service_time_source.strip():
        raise RuntimeWaitModelError("service_time_source is required")
    if (
        isinstance(service_time_us, bool)
        or not isinstance(service_time_us, (int, float))
        or not isfinite(float(service_time_us))
        or service_time_us <= 0
    ):
        raise RuntimeWaitModelError("service_time_us must be positive")
    if (
        isinstance(safety_margin_us, bool)
        or not isinstance(safety_margin_us, (int, float))
        or not isfinite(float(safety_margin_us))
        or safety_margin_us < 0
    ):
        raise RuntimeWaitModelError("safety_margin_us must be non-negative")
    model_data = model.get("model", {})
    pattern_data = model.get("pattern", {})
    tick_hz = int(model_data.get("tick_hz", 0))
    ring_slot_count = int(model_data.get("ring_slot_count", 0))
    if tick_hz <= 0 or ring_slot_count < 2:
        raise RuntimeWaitModelError("model has invalid tick_hz or ring_slot_count")
    chunks = list(pattern_data.get("chunks", []))
    if not chunks:
        raise RuntimeWaitModelError("model has no chunks")
    waits = explicit_waits or {}
    normalized_waits: dict[int, int] = {}
    for raw_index, raw_wait in waits.items():
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise RuntimeWaitModelError("explicit wait chunk indices must be integers")
        if raw_index < 0 or raw_index >= len(chunks):
            raise RuntimeWaitModelError(f"explicit wait chunk index {raw_index} is out of range")
        if isinstance(raw_wait, bool) or not isinstance(raw_wait, int) or raw_wait < 0:
            raise RuntimeWaitModelError("explicit wait ticks must be non-negative integers")
        normalized_waits[raw_index] = raw_wait
    service_budget_us = service_time_us + safety_margin_us
    service_ticks = _ticks_from_us(service_budget_us, tick_hz)
    simulation = _simulate_ring_schedule(
        chunks,
        ring_slot_count=ring_slot_count,
        service_ticks=service_ticks,
        explicit_waits=normalized_waits,
        tick_hz=tick_hz,
    )
    return {
        "service_time": {
            "measured_or_declared_us": service_time_us,
            "safety_margin_us": safety_margin_us,
            "budget_us": service_budget_us,
            "budget_ticks": service_ticks,
            "source": service_time_source,
        },
        "explicit_waits": normalized_waits,
        "simulation": simulation,
        "limitations": [
            "service time is a supplied budget, not a universal worst case",
            "DMA interrupt latency, exact request timestamps, and firmware scheduler details are not simulated",
            "this report is host-only and does not authorize physical execution",
        ],
    }


def _every_boundary_wait_ticks(
    chunks: Sequence[dict[str, Any]], *, max_wait_ticks: int, forbidden_chunks: set[int],
) -> dict[int, int]:
    """Placement only, shared by exact encoding probes and the global planner.

    Service/count/total-time acceptance is deliberately not a capacity-search
    heuristic. The complete planner must still accept the assembled schedule.
    """
    return {
        index: max_wait_ticks
        for index, chunk in enumerate(chunks)
        if index > 0 and int(chunk.get("event_start", 0)) > 0
        and index not in forbidden_chunks
        and int(chunks[index - 1].get("consume_ticks", 0)) < max_wait_ticks
    }


def _plan_every_boundary_waits(
    model: dict[str, Any],
    *,
    chunks: Sequence[dict[str, Any]],
    ring_slot_count: int,
    tick_hz: int,
    service_ticks: int,
    service_time_us: float,
    service_time_source: str,
    safety_margin_us: float,
    max_wait_us: float,
    max_wait_ticks: int,
    max_total_wait_us: float,
    max_total_wait_ticks: int,
    max_wait_count: int | None,
    pattern_time_limit_us: float | None,
    forbidden_chunks: set[int],
    baseline: dict[str, Any],
    boundary_constraints: dict[str, Any],
) -> dict[str, Any]:
    """Build an explicit wait at every eligible chunk boundary.

    This is intentionally separate from the frequency-first pressure planner.
    It is useful when the declared service profile is close to the natural
    chunk turnover and the operator wants a conservative pause at each
    boundary.  Boundary zero and boundaries forbidden by a segmented upload
    or source-level rate policy remain zero.  A chunk whose natural consumer
    window is already at least the configured wait is also left zero, since
    that window already gives the producer at least the requested interval.
    """

    waits: list[dict[str, Any]] = []
    explicit_waits: dict[int, int] = {}
    skipped_forbidden: list[int] = []
    skipped_natural_gap: list[int] = []
    placements = _every_boundary_wait_ticks(
        chunks, max_wait_ticks=max_wait_ticks, forbidden_chunks=forbidden_chunks)
    for chunk_index, chunk in enumerate(chunks):
        if chunk_index == 0 or int(chunk.get("event_start", 0)) <= 0:
            continue
        if chunk_index in forbidden_chunks:
            skipped_forbidden.append(chunk_index)
            continue
        # The wait is encoded *before* chunk_index.  Therefore the natural
        # interval that precedes this boundary is the execution window of the
        # previous chunk, not the chunk that follows the boundary.
        if chunk_index not in placements:
            skipped_natural_gap.append(chunk_index)
            continue
        if max_wait_count is not None and len(waits) >= max_wait_count:
            return _rejected_plan(
                model,
                reason=(
                    "every-boundary wait plan exceeds max_wait_count at "
                    f"chunk {chunk_index}"
                ),
                service_time_us=service_time_us,
                service_time_source=service_time_source,
                safety_margin_us=safety_margin_us,
                waits=waits,
                boundary_constraints=boundary_constraints,
            )
        cumulative_wait = sum(int(item["wait_ticks"]) for item in waits)
        if cumulative_wait + max_wait_ticks > max_total_wait_ticks:
            return _rejected_plan(
                model,
                reason=(
                    "every-boundary wait plan exceeds max_total_wait_us at "
                    f"chunk {chunk_index}; increase the total limit or use fewer boundaries"
                ),
                service_time_us=service_time_us,
                service_time_source=service_time_source,
                safety_margin_us=safety_margin_us,
                waits=waits,
                boundary_constraints=boundary_constraints,
            )
        waits.append(
            {
                "before_chunk_index": chunk_index,
                "before_event": int(chunk["event_start"]),
                "requested_before_chunk_index": chunk_index,
                "requested_before_event": int(chunk["event_start"]),
                "relocated_forbidden_boundary": False,
                "wait_ticks": max_wait_ticks,
                "wait_us": max_wait_ticks * 1_000_000.0 / tick_hz,
                "modeled_deficit_ticks": 0,
                "batch_peak_deficit_ticks": 0,
                "natural_gap_source_chunk_index": chunk_index - 1,
                "reason": "every_eligible_chunk_boundary",
            }
        )
        explicit_waits[chunk_index] = max_wait_ticks

    final_simulation = _simulate_ring_schedule(
        chunks,
        ring_slot_count=ring_slot_count,
        service_ticks=service_ticks,
        explicit_waits=explicit_waits,
        tick_hz=tick_hz,
    )
    if final_simulation["underflow_chunks"]:
        return _rejected_plan(
            model,
            reason=(
                "every-boundary wait plan still leaves modeled ring underflow "
                f"pressure at chunks {final_simulation['underflow_chunks']}"
            ),
            service_time_us=service_time_us,
            service_time_source=service_time_source,
            safety_margin_us=safety_margin_us,
            waits=waits,
            boundary_constraints=boundary_constraints,
        )

    cumulative_wait = sum(int(item["wait_ticks"]) for item in waits)
    total_consume_ticks = int(model["pattern"].get("total_consume_ticks", 0))
    total_pattern_ticks = total_consume_ticks + cumulative_wait
    total_pattern_us = total_pattern_ticks * 1_000_000.0 / tick_hz
    if pattern_time_limit_us is not None and total_pattern_us > pattern_time_limit_us:
        return _rejected_plan(
            model,
            reason="pattern_time_limit_us exceeded",
            service_time_us=service_time_us,
            service_time_source=service_time_source,
            safety_margin_us=safety_margin_us,
            waits=waits,
            boundary_constraints=boundary_constraints,
        )

    boundary_constraints = {
        **boundary_constraints,
        "wait_policy": "every-boundary",
        "skipped_forbidden_boundary_count": len(skipped_forbidden),
        "skipped_forbidden_boundary_indices": skipped_forbidden,
        "skipped_natural_gap_count": len(skipped_natural_gap),
        "skipped_natural_gap_indices": skipped_natural_gap,
        "natural_gap_rule": "skip a boundary when the preceding chunk consume_us is greater than or equal to configured max_wait_us",
    }
    return {
        "status": "planned" if waits else "no_wait_needed",
        "bounded": True,
        "wait_policy": "every-boundary",
        "service_time": {
            "measured_or_declared_us": service_time_us,
            "safety_margin_us": safety_margin_us,
            "budget_us": service_time_us + safety_margin_us,
            "budget_ticks": service_ticks,
            "source": service_time_source,
        },
        "limits": {
            "max_wait_us": max_wait_us,
            "max_total_wait_us": max_total_wait_us,
            "max_wait_count": max_wait_count,
            "pattern_time_limit_us": pattern_time_limit_us,
        },
        "boundary_constraints": boundary_constraints,
        "objective": {
            "lexicographic_order": ["boundary_coverage", "max_wait_us", "total_wait_us"],
            "wait_count": len(waits),
            "eligible_boundary_count": sum(
                1
                for index, chunk in enumerate(chunks)
                if index > 0
                and int(chunk.get("event_start", 0)) > 0
                and index not in forbidden_chunks
                and index not in skipped_natural_gap
            ),
            "max_wait_us": max_wait_ticks * 1_000_000.0 / tick_hz if waits else 0.0,
            "total_wait_us": cumulative_wait * 1_000_000.0 / tick_hz,
        },
        "schedule": {
            "first_modeled_chunk": max(0, ring_slot_count - 1),
            "raw_deficit_max_us": max(
                (int(item["required_wait_ticks"]) for item in baseline["events"]),
                default=0,
            )
            * 1_000_000.0
            / tick_hz,
            "baseline_underflow_pressure_chunks": baseline["underflow_chunks"],
            "final_underflow_pressure_chunks": final_simulation["underflow_chunks"],
            "total_consume_us": total_consume_ticks * 1_000_000.0 / tick_hz,
            "total_pattern_us": total_pattern_us,
            "waits": waits,
        },
        "limitations": [
            "every-boundary is a conservative host-only policy, not a physical timing measurement",
            "segment first boundaries and explicitly forbidden rate boundaries remain zero",
            "a preceding chunk consumer window at least as long as max_wait_us is treated as sufficient gap",
            "the model does not simulate DMA interrupt latency or exact request timestamps",
            "physical validation is required before any wait policy can be accepted",
        ],
    }


def plan_waits(
    model: dict[str, Any],
    *,
    service_time_us: float,
    service_time_source: str,
    safety_margin_us: float = 0.0,
    max_wait_us: float | None,
    max_total_wait_us: float | None,
    max_wait_count: int | None = None,
    pattern_time_limit_us: float | None = None,
    forbidden_wait_chunks: Iterable[int] | None = None,
    wait_policy: str = "pressure",
) -> dict[str, Any]:
    """Plan boundary waits using a bounded pressure or every-boundary policy.

    The planner treats the producer as a serial service stream and the first
    ``ring_slot_count - 1`` chunks as the available lead horizon.  A wait is
    placed before the first chunk whose producer service would otherwise miss
    its deadline.  To honor the frequency-first objective in ``pressure``
    mode, the wait is
    proactively sized to cover the largest remaining modeled deficit, subject
    to the declared hard limits.  This is a lower-complexity planning model,
    not a claim about the firmware scheduler.  ``forbidden_wait_chunks``
    marks boundaries that cannot carry a wait, such as the first chunk of a
    separately uploaded segment.  When the first modeled pressure point is
    forbidden, the planner searches earlier legal boundaries and evaluates
    each relocation with the same ring simulation.  ``every-boundary`` mode
    uses the configured ``max_wait_us`` at every eligible boundary and does
    not relocate waits from forbidden boundaries.
    """

    if wait_policy not in {"pressure", "every-boundary"}:
        raise RuntimeWaitModelError(f"unsupported wait_policy: {wait_policy!r}")
    if not service_time_source.strip():
        raise RuntimeWaitModelError("service_time_source is required")
    if service_time_us <= 0:
        raise RuntimeWaitModelError("service_time_us must be positive")
    if max_wait_us is None or max_wait_us <= 0:
        return _unbounded_plan(
            model,
            reason="max_wait_us is required before automatic wait planning",
            service_time_us=service_time_us,
            service_time_source=service_time_source,
            safety_margin_us=safety_margin_us,
        )
    if max_total_wait_us is None or max_total_wait_us <= 0:
        return _unbounded_plan(
            model,
            reason="max_total_wait_us is required before automatic wait planning",
            service_time_us=service_time_us,
            service_time_source=service_time_source,
            safety_margin_us=safety_margin_us,
        )
    if max_wait_count is not None and max_wait_count < 0:
        raise RuntimeWaitModelError("max_wait_count must be non-negative")
    if pattern_time_limit_us is not None and pattern_time_limit_us <= 0:
        raise RuntimeWaitModelError("pattern_time_limit_us must be positive")

    model_data = model.get("model", {})
    pattern_data = model.get("pattern", {})
    tick_hz = int(model_data.get("tick_hz", 0))
    if tick_hz <= 0:
        raise RuntimeWaitModelError("model has no positive tick_hz")
    chunks = list(pattern_data.get("chunks", []))
    ring_slot_count = int(model_data.get("ring_slot_count", 0))
    if ring_slot_count < 2:
        raise RuntimeWaitModelError("model has invalid ring_slot_count")
    forbidden_chunks = _normalize_forbidden_wait_chunks(
        forbidden_wait_chunks,
        chunks=chunks,
    )
    waitable_chunks = {
        index
        for index, chunk in enumerate(chunks)
        if index > 0 and int(chunk.get("event_start", 0)) > 0
    }
    boundary_constraints = {
        "forbidden_wait_chunks": sorted(forbidden_chunks),
        "waitable_chunk_indices_exclude_zero": True,
    }

    service_budget_us = service_time_us + safety_margin_us
    service_ticks = _ticks_from_us(service_budget_us, tick_hz)
    max_wait_ticks = _ticks_from_us(max_wait_us, tick_hz)
    max_total_wait_ticks = _ticks_from_us(max_total_wait_us, tick_hz)
    first_modeled_chunk = max(0, ring_slot_count - 1)
    baseline = _simulate_ring_schedule(
        chunks,
        ring_slot_count=ring_slot_count,
        service_ticks=service_ticks,
        tick_hz=tick_hz,
    )
    baseline_events = baseline["events"]
    boundary_constraints["wait_policy"] = wait_policy
    if wait_policy == "every-boundary":
        return _plan_every_boundary_waits(
            model,
            chunks=chunks,
            ring_slot_count=ring_slot_count,
            tick_hz=tick_hz,
            service_ticks=service_ticks,
            service_time_us=service_time_us,
            service_time_source=service_time_source,
            safety_margin_us=safety_margin_us,
            max_wait_us=max_wait_us,
            max_wait_ticks=max_wait_ticks,
            max_total_wait_us=max_total_wait_us,
            max_total_wait_ticks=max_total_wait_ticks,
            max_wait_count=max_wait_count,
            pattern_time_limit_us=pattern_time_limit_us,
            forbidden_chunks=forbidden_chunks,
            baseline=baseline,
            boundary_constraints=boundary_constraints,
        )
    waits: list[dict[str, Any]] = []
    explicit_waits: dict[int, int] = {}
    max_iterations = max(1, len(chunks) * 2)
    for _iteration in range(max_iterations):
        simulation = _simulate_ring_schedule(
            chunks,
            ring_slot_count=ring_slot_count,
            service_ticks=service_ticks,
            explicit_waits=explicit_waits,
            tick_hz=tick_hz,
        )
        first_underflow = next(
            (
                item
                for item in simulation["events"]
                if int(item["residual_wait_ticks"]) > 0
            ),
            None,
        )
        if first_underflow is None:
            break
        chunk_index = int(first_underflow["chunk_index"])
        needed = int(first_underflow["residual_wait_ticks"])
        # Find the smallest one-shot prepayment that clears the remaining
        # modeled suffix, if it fits the declared per-wait limit.  A binary
        # search keeps the frequency-first choice without inflating total
        # pattern time more than needed.  If the cap cannot clear the suffix,
        # use the cap and continue with another bounded boundary wait.
        def simulate_candidate(candidate_chunk: int, candidate_ticks: int) -> dict[str, Any]:
            candidate_waits = dict(explicit_waits)
            candidate_waits[candidate_chunk] = (
                candidate_waits.get(candidate_chunk, 0) + candidate_ticks
            )
            return _simulate_ring_schedule(
                chunks,
                ring_slot_count=ring_slot_count,
                service_ticks=service_ticks,
                explicit_waits=candidate_waits,
                tick_hz=tick_hz,
            )

        def first_pressure(simulated: dict[str, Any]) -> tuple[int, int] | None:
            for item in simulated["events"]:
                residual = int(item["residual_wait_ticks"])
                if residual > 0:
                    return int(item["chunk_index"]), residual
            return None

        selected_chunk: int | None = None
        selected_wait_ticks: int | None = None
        selected_clears_suffix = False
        for candidate_chunk in range(chunk_index, 0, -1):
            if candidate_chunk not in waitable_chunks or candidate_chunk in forbidden_chunks:
                continue
            candidate_at_cap = simulate_candidate(candidate_chunk, max_wait_ticks)
            if not candidate_at_cap["underflow_chunks"]:
                low = needed if candidate_chunk == chunk_index else 1
                high = max_wait_ticks
                while low < high:
                    mid = (low + high) // 2
                    if not simulate_candidate(candidate_chunk, mid)["underflow_chunks"]:
                        high = mid
                    else:
                        low = mid + 1
                selected_chunk = candidate_chunk
                selected_wait_ticks = low
                selected_clears_suffix = True
                break

            pressure_after_cap = first_pressure(candidate_at_cap)
            progressed = (
                pressure_after_cap is None
                or pressure_after_cap[0] > chunk_index
                or (
                    pressure_after_cap[0] == chunk_index
                    and pressure_after_cap[1] < needed
                )
            )
            if progressed:
                selected_chunk = candidate_chunk
                selected_wait_ticks = max_wait_ticks
                break

        if selected_chunk is None or selected_wait_ticks is None:
            return _rejected_plan(
                model,
                reason=(
                    "no legal wait boundary can relieve the first modeled ring "
                    f"deadline deficit at chunk {chunk_index}; forbidden chunks="
                    f"{sorted(forbidden_chunks)}"
                ),
                service_time_us=service_time_us,
                service_time_source=service_time_source,
                safety_margin_us=safety_margin_us,
                waits=waits,
                boundary_constraints=boundary_constraints,
            )

        wait_ticks = selected_wait_ticks
        future_peak = wait_ticks if selected_clears_suffix else max_wait_ticks
        if selected_chunk == chunk_index and wait_ticks < needed:
            return _rejected_plan(
                model,
                reason="max_wait_us is smaller than a modeled ring deadline deficit",
                service_time_us=service_time_us,
                service_time_source=service_time_source,
                safety_margin_us=safety_margin_us,
                waits=waits,
                boundary_constraints=boundary_constraints,
            )
        if max_wait_count is not None and len(waits) >= max_wait_count:
            return _rejected_plan(
                model,
                reason="max_wait_count exceeded",
                service_time_us=service_time_us,
                service_time_source=service_time_source,
                safety_margin_us=safety_margin_us,
                waits=waits,
                boundary_constraints=boundary_constraints,
            )
        cumulative_wait = sum(int(item["wait_ticks"]) for item in waits)
        if cumulative_wait + wait_ticks > max_total_wait_ticks:
            return _rejected_plan(
                model,
                reason="max_total_wait_us exceeded",
                service_time_us=service_time_us,
                service_time_source=service_time_source,
                safety_margin_us=safety_margin_us,
                waits=waits,
                boundary_constraints=boundary_constraints,
            )
        wait = {
            "before_chunk_index": selected_chunk,
            "before_event": int(chunks[selected_chunk]["event_start"]),
            "requested_before_chunk_index": chunk_index,
            "requested_before_event": int(chunks[chunk_index]["event_start"]),
            "relocated_forbidden_boundary": selected_chunk != chunk_index,
            "wait_ticks": wait_ticks,
            "wait_us": wait_ticks * 1_000_000.0 / tick_hz,
            "modeled_deficit_ticks": needed,
            "batch_peak_deficit_ticks": future_peak,
            "reason": (
                "frequency_first_prepaid_deficit"
                if selected_chunk == chunk_index
                else "frequency_first_relocated_from_forbidden_boundary"
            ),
        }
        waits.append(wait)
        explicit_waits[selected_chunk] = explicit_waits.get(selected_chunk, 0) + wait_ticks
    else:
        return _rejected_plan(
            model,
            reason="wait planner did not converge within bounded iterations",
            service_time_us=service_time_us,
            service_time_source=service_time_source,
            safety_margin_us=safety_margin_us,
            waits=waits,
            boundary_constraints=boundary_constraints,
        )

    final_simulation = _simulate_ring_schedule(
        chunks,
        ring_slot_count=ring_slot_count,
        service_ticks=service_ticks,
        explicit_waits=explicit_waits,
        tick_hz=tick_hz,
    )
    if final_simulation["underflow_chunks"]:
        return _rejected_plan(
            model,
            reason="explicit wait plan still leaves modeled ring underflow pressure",
            service_time_us=service_time_us,
            service_time_source=service_time_source,
            safety_margin_us=safety_margin_us,
            waits=waits,
            boundary_constraints=boundary_constraints,
        )

    cumulative_wait = sum(int(item["wait_ticks"]) for item in waits)

    total_consume_ticks = int(pattern_data.get("total_consume_ticks", 0))
    total_pattern_ticks = total_consume_ticks + cumulative_wait
    total_pattern_us = total_pattern_ticks * 1_000_000.0 / tick_hz
    if pattern_time_limit_us is not None and total_pattern_us > pattern_time_limit_us:
        return _rejected_plan(
            model,
            reason="pattern_time_limit_us exceeded",
            service_time_us=service_time_us,
            service_time_source=service_time_source,
            safety_margin_us=safety_margin_us,
            waits=waits,
            boundary_constraints=boundary_constraints,
        )

    max_planned_wait = max((int(item["wait_ticks"]) for item in waits), default=0)
    return {
        "status": "planned" if waits else "no_wait_needed",
        "bounded": True,
        "service_time": {
            "measured_or_declared_us": service_time_us,
            "safety_margin_us": safety_margin_us,
            "budget_us": service_budget_us,
            "budget_ticks": service_ticks,
            "source": service_time_source,
        },
        "limits": {
            "max_wait_us": max_wait_us,
            "max_total_wait_us": max_total_wait_us,
            "max_wait_count": max_wait_count,
            "pattern_time_limit_us": pattern_time_limit_us,
        },
        "boundary_constraints": boundary_constraints,
        "objective": {
            "lexicographic_order": ["wait_count", "max_wait_us", "total_wait_us"],
            "wait_count": len(waits),
            "max_wait_us": max_planned_wait * 1_000_000.0 / tick_hz,
            "total_wait_us": cumulative_wait * 1_000_000.0 / tick_hz,
        },
        "schedule": {
            "first_modeled_chunk": first_modeled_chunk,
            "raw_deficit_max_us": max(
                (int(item["required_wait_ticks"]) for item in baseline_events),
                default=0,
            ) * 1_000_000.0 / tick_hz,
            "baseline_underflow_pressure_chunks": baseline["underflow_chunks"],
            "final_underflow_pressure_chunks": final_simulation["underflow_chunks"],
            "total_consume_us": total_consume_ticks * 1_000_000.0 / tick_hz,
            "total_pattern_us": total_pattern_us,
            "waits": waits,
        },
        "limitations": [
            "service time is a supplied budget, not inferred as a universal worst case",
            "the model does not simulate DMA interrupt latency or exact request timestamps",
            "wait locations are chunk-boundary plans; no binary pattern mutation is performed",
            "physical validation is required before any wait policy can be accepted",
        ],
    }


def _unbounded_plan(
    model: dict[str, Any],
    *,
    reason: str,
    service_time_us: float,
    service_time_source: str,
    safety_margin_us: float,
) -> dict[str, Any]:
    return {
        "status": "requires_limits",
        "bounded": False,
        "reason": reason,
        "service_time": {
            "measured_or_declared_us": service_time_us,
            "safety_margin_us": safety_margin_us,
            "source": service_time_source,
        },
        "objective": {
            "lexicographic_order": ["wait_count", "max_wait_us", "total_wait_us"],
            "wait_count": None,
            "max_wait_us": None,
            "total_wait_us": None,
        },
        "schedule": {"waits": []},
        "limitations": [
            "automatic wait insertion is fail-closed until explicit hard limits are supplied",
            "the runtime model remains available for consumption-only analysis",
        ],
    }


def _rejected_plan(
    model: dict[str, Any],
    *,
    reason: str,
    service_time_us: float,
    service_time_source: str,
    safety_margin_us: float,
    waits: Sequence[dict[str, Any]],
    boundary_constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tick_hz = int(model["model"]["tick_hz"])
    total_wait_ticks = sum(int(item["wait_ticks"]) for item in waits)
    return {
        "status": "rejected",
        "bounded": True,
        "reason": reason,
        "boundary_constraints": boundary_constraints or {},
        "service_time": {
            "measured_or_declared_us": service_time_us,
            "safety_margin_us": safety_margin_us,
            "budget_us": service_time_us + safety_margin_us,
            "source": service_time_source,
        },
        "objective": {
            "lexicographic_order": ["wait_count", "max_wait_us", "total_wait_us"],
            "wait_count": len(waits),
            "max_wait_us": max((float(item["wait_us"]) for item in waits), default=0.0),
            "total_wait_us": total_wait_ticks * 1_000_000.0 / tick_hz,
        },
        "schedule": {"waits": list(waits)},
        "limitations": [
            "the declared hard limits reject this candidate; no automatic pattern mutation is allowed",
            "physical validation is required even for a bounded host plan",
        ],
    }


def analyze_pattern_with_wait_policy(
    pattern_bytes: bytes,
    *,
    chunk_event_capacity: int = DEFAULT_CHUNK_EVENT_CAPACITY,
    ring_slot_count: int = DEFAULT_RING_SLOT_COUNT,
    service_time_us: float | None = None,
    service_time_source: str = "",
    safety_margin_us: float = 0.0,
    max_wait_us: float | None = None,
    max_total_wait_us: float | None = None,
    max_wait_count: int | None = None,
    pattern_time_limit_us: float | None = None,
    forbidden_wait_chunks: Iterable[int] | None = None,
    wait_policy: str = "pressure",
    _model_cache: _RuntimeModelCache | None = None,
) -> dict[str, Any]:
    """Build the model and optionally attach a fail-closed wait plan."""

    model = build_runtime_model(
        pattern_bytes,
        chunk_event_capacity=chunk_event_capacity,
        ring_slot_count=ring_slot_count,
        _model_cache=_model_cache,
    )
    if service_time_us is not None:
        model["wait_plan"] = plan_waits(
            model,
            service_time_us=service_time_us,
            service_time_source=service_time_source,
            safety_margin_us=safety_margin_us,
            max_wait_us=max_wait_us,
            max_total_wait_us=max_total_wait_us,
            max_wait_count=max_wait_count,
            pattern_time_limit_us=pattern_time_limit_us,
            forbidden_wait_chunks=forbidden_wait_chunks,
            wait_policy=wait_policy,
        )
    else:
        model["wait_plan"] = {
            "status": "requires_service_profile",
            "bounded": False,
            "reason": "service_time_us was not supplied",
            "schedule": {"waits": []},
        }
    return model
