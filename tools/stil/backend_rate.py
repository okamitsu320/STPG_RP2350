from __future__ import annotations

from dataclasses import dataclass

from stpg.constants import (
    STPG_DRIVE_NONE,
    STPG_DRIVE_ONE,
    STPG_DRIVE_VECTOR,
    STPG_DRIVE_ZERO,
    STPG_MAX_SLOTS_PER_RATE,
    STPG_MAX_LOOP_BLOCKS,
    STPG_RATE_SLOT_DIR_UPDATE,
    STPG_RATE_SLOT_SAMPLE,
)
from stpg.pattern import (
    RateDesc,
    RateDesc32,
    LoopBlock,
    LoopRefRecord,
    RateSlot,
    RateSlot32,
    VectorRecord,
    fold_adjacent_repeats,
    pack_rate_vector,
    pack_wide_rate_vector,
    validate_rate_vector,
)

from .ast import CompileArtifacts, IRVector
from .errors import StilError
from .waveform_actions import (
    STIL_DRIVE_RELEASE,
    drive_level_for_char,
    drive_op_for_event,
    final_drive_enable,
    final_drive_level,
    ADDED_WAVEFORM_CHARS,
    event_char,
    vector_compare_char,
)


@dataclass(frozen=True)
class RateBuildResult:
    pattern_bytes: bytes
    report_mode: str = "rate-vector"
    rate_count: int = 0
    slot_count: int = 0
    vector_count: int = 0
    folded_vectors: tuple[VectorRecord, ...] = ()
    folded_source_vectors: tuple[tuple, ...] = ()
    practical_chunk_shaping_segments: tuple[dict, ...] = ()
    loop_blocks: tuple[LoopBlock, ...] = ()
    sequence_entries: tuple[VectorRecord | LoopRefRecord, ...] = ()
    # Emitted IDs are not source-level rate names. Capacity-only wait probes
    # need this exact binding to honor forbidden WaveformTables without maps.
    rate_waveform_tables: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShapingOptions:
    enabled: bool = False
    target_global_range: tuple[int, int] | None = None
    max_run_vectors: int = 64


class _SlotSignatureCache:
    """Bounded content reuse owned by one encoding pass, never by the process.

    Resolve each signal's character/actions first: data-dependent vector
    drive and H/L sample operations can share slots, but fixed edges, release,
    direction-only Z and no-op T must remain distinct. Pin maps, markers and
    time scaling are bound by the bit and complete timed actions, not a table
    name. The original builder still validates every new semantic shape.
    """

    def __init__(self, max_entries: int = 1024) -> None:
        self.max_entries = max_entries
        self._signatures: dict[tuple, tuple[tuple[int, int, int, int, int], ...]] = {}
        self._signal_actions: dict[tuple, tuple] = {}

    def signature(self, vector: IRVector) -> tuple[tuple[int, int, int, int, int], ...]:
        if self.max_entries <= 0:
            return _build_slot_signature(vector)
        key_parts = []
        for signal_name, bit in vector.signal_bit_assignments.items():
            char = vector.signal_chars[signal_name]
            events = vector.signal_events.get(signal_name, ())
            signal_key = (bit, char, events)
            actions = self._signal_actions.get(signal_key)
            if actions is None:
                actions = (bit, _resolved_signal_slot_actions(char, events))
                if len(self._signal_actions) < self.max_entries:
                    self._signal_actions[signal_key] = actions
            key_parts.append(actions)
        key = tuple(key_parts)
        signature = self._signatures.get(key)
        if signature is None:
            signature = _build_slot_signature(vector)
            # Unusual sources with many shapes still have bounded extra
            # memory; overflow does not evict useful earlier entries.
            if len(self._signatures) < self.max_entries:
                self._signatures[key] = signature
        return signature


def _resolved_signal_slot_actions(char: str, events: tuple) -> tuple:
    """Normalize exactly the inputs used by ``_build_slot_signature``.

    Validate all Drive arguments before resolving phases, as the builder's
    gathering pass does. Keep unsupported characters as distinct sentinels:
    raising here could change error order relative to a later invalid Drive.
    Direction-only Z does not participate in drive-op conflict checks, unlike
    an explicit Drive(Z). Even T's no-op timestamp creates a phase.
    """

    final_level = final_drive_level(char, events)
    actions = []
    for event in events:
        effective_char = event_char(event, char=char)
        op = drive_op_for_event(event, char=effective_char)
        if op is None and effective_char in ("1", "R", "0", "F"):
            implied_level = drive_level_for_char(effective_char)
            op = (
                STPG_DRIVE_VECTOR
                if event.time_ticks == 0 and final_level == implied_level
                else STPG_DRIVE_ONE if implied_level == 1 else STPG_DRIVE_ZERO
            )
        elif op in (STPG_DRIVE_ZERO, STPG_DRIVE_ONE):
            event_level = 1 if op == STPG_DRIVE_ONE else 0
            if event.time_ticks == 0 and final_level == event_level:
                op = STPG_DRIVE_VECTOR
        if op is not None:
            action = ("drive", op)
        elif effective_char == "Z":
            action = ("direction_only",)
        elif effective_char in ("H", "L", "X"):
            action = ("sample_no_compare",) if char in ADDED_WAVEFORM_CHARS and effective_char == "X" else ("sample",)
        elif effective_char == "T":
            action = ("noop",)
        else:
            action = ("unsupported", char)
        actions.append((event.time_ticks, action))
    return tuple(actions)


def _vector_rate_key(vector, *, _slot_signatures: _SlotSignatureCache | None = None) -> tuple:  # type: ignore[no-untyped-def]
    return (
        vector.rate_key.waveform_table_name,
        vector.rate_key.period_ticks,
        vector.rate_key.normalized_edge_topology,
        (_slot_signatures.signature(vector) if _slot_signatures is not None else _build_slot_signature(vector)),
    )


def _build_vector_record(vector, rate_id: int) -> VectorRecord:  # type: ignore[no-untyped-def]
    drive_value = 0
    drive_enable = 0
    for signal_name, char in vector.drive_chars.items():
        bit = vector.signal_bit_assignments[signal_name]
        events = vector.signal_events.get(signal_name, ())
        final_level = final_drive_level(char, events)
        final_enable = final_drive_enable(char, events)
        if final_level == 1:
            drive_value |= 1 << bit
        elif final_level == 0:
            drive_value &= ~(1 << bit)
        elif final_level is not None:
            raise StilError("ERR_RATE_VECTOR_UNSUPPORTED_CHAR", f"unsupported rate-vector char {char}")
        if final_enable is True:
            drive_enable |= 1 << bit
        elif final_enable is False:
            drive_enable &= ~(1 << bit)
        elif final_enable is None and final_level is None and char not in ("Z", "T", "H", "L", "X"):
            raise StilError("ERR_RATE_VECTOR_UNSUPPORTED_CHAR", f"unsupported rate-vector char {char}")
    expect_value = 0
    expect_mask = 0
    for signal_name, char in vector.compare_chars.items():
        char = vector_compare_char(char, vector.signal_events.get(signal_name, ()))
        bit = vector.signal_bit_assignments[signal_name]
        if char == "H":
            expect_value |= 1 << bit
            # The care mask is independent of the expected level: both H and
            # L are constrained compare states.  In particular, an L->H
            # mutation inside a preserved loop changes expect_value but must
            # not add or remove a mask bit.
            expect_mask |= 1 << bit
        elif char == "L":
            expect_mask |= 1 << bit
        elif char in ("X", "T"):
            pass
        else:
            raise StilError("ERR_RATE_VECTOR_UNSUPPORTED_CHAR", f"unsupported rate-vector char {char}")
    return VectorRecord(
        rate_id=rate_id,
        flags=0,
        repeat=1,
        drive_value=drive_value,
        drive_enable=drive_enable,
        expect_value=expect_value,
        expect_mask=expect_mask,
    )


class _VectorRecordCache:
    """Bounded complete-semantic reuse within one encoding, never admission.

    Include bits, characters and *all* timed actions, not just the rate/table
    name or slot shape: vector-dependent Drive and compare levels differ even
    when they have identical slots. Values are frozen VectorRecords; folding
    constructs new records. Failed computations are never cached.
    """

    def __init__(self, max_entries: int = 1024) -> None:
        self.max_entries = max_entries
        self._records: dict[tuple, VectorRecord] = {}

    def record(self, vector, rate_id: int) -> VectorRecord:  # type: ignore[no-untyped-def]
        if self.max_entries <= 0:
            return _build_vector_record(vector, rate_id)
        try:
            key = (rate_id, *[
                tuple((vector.signal_bit_assignments[name], char, vector.signal_events.get(name, ()))
                      for name, char in chars.items())
                for chars in (vector.drive_chars, vector.compare_chars)])
            result = self._records.get(key)
        except (KeyError, TypeError):
            # Keep the original error path/order for an inconsistent manually
            # constructed IR; only normal immutable IR participates in reuse.
            return _build_vector_record(vector, rate_id)
        if result is None:
            result = _build_vector_record(vector, rate_id)
            if len(self._records) < self.max_entries:
                self._records[key] = result
        return result


def build_rate_vector_records(
    ir, *, _slot_signatures: _SlotSignatureCache | None = None,
) -> list[VectorRecord]:  # type: ignore[no-untyped-def]
    slot_signatures = _slot_signatures if _slot_signatures is not None else _SlotSignatureCache()
    rate_ids: dict[tuple, int] = {}
    next_rate_id = 0
    vectors: list[VectorRecord] = []
    records = _VectorRecordCache()
    for vector in ir.vectors:
        key = _vector_rate_key(vector, _slot_signatures=slot_signatures)
        if key not in rate_ids:
            rate_ids[key] = next_rate_id
            next_rate_id += 1
        vectors.append(records.record(vector, rate_ids[key]))
    return vectors


def _same_shape(prev: VectorRecord, entry: VectorRecord) -> bool:
    return (
        prev.rate_id == entry.rate_id
        and prev.flags == entry.flags
        and prev.drive_value == entry.drive_value
        and prev.drive_enable == entry.drive_enable
        and prev.expect_value == entry.expect_value
        and prev.expect_mask == entry.expect_mask
    )


def _same_shape_except_rate(prev: VectorRecord, entry: VectorRecord) -> bool:
    return (
        prev.flags == entry.flags
        and prev.drive_value == entry.drive_value
        and prev.drive_enable == entry.drive_enable
        and prev.expect_value == entry.expect_value
        and prev.expect_mask == entry.expect_mask
    )


def _fold_with_sources(
    vectors: list[VectorRecord],
    source_vectors: list,
) -> tuple[list[VectorRecord], list[tuple]]:  # type: ignore[no-untyped-def]
    folded_records: list[VectorRecord] = []
    folded_source_vectors: list[list] = []
    for vector, source_vector in zip(vectors, source_vectors):
        if folded_records and _same_shape(folded_records[-1], vector) and folded_records[-1].repeat < 0xFFFF:
            prev = folded_records[-1]
            folded_records[-1] = VectorRecord(
                rate_id=prev.rate_id,
                flags=prev.flags,
                repeat=prev.repeat + 1,
                drive_value=prev.drive_value,
                drive_enable=prev.drive_enable,
                expect_value=prev.expect_value,
                expect_mask=prev.expect_mask,
            )
            folded_source_vectors[-1].append(source_vector)
            continue
        folded_records.append(vector)
        folded_source_vectors.append([source_vector])
    return folded_records, [tuple(group) for group in folded_source_vectors]


def build_folded_rate_vector_records(
    ir,
    *,
    shaping: ShapingOptions | None = None,
    _unfolded_records: list[VectorRecord] | None = None,
) -> tuple[list[VectorRecord], list[tuple], list[dict]]:  # type: ignore[no-untyped-def]
    unfolded_records = (
        build_rate_vector_records(ir) if _unfolded_records is None else _unfolded_records
    )
    shaping = shaping or ShapingOptions()
    target_range = shaping.target_global_range if shaping.enabled else None
    max_run_vectors = max(1, shaping.max_run_vectors)
    shaping_segments: list[dict] = []
    shaped_vectors = list(unfolded_records)
    source_vectors = list(ir.vectors)

    if shaping.enabled and target_range is not None and shaped_vectors:
        base_rate_count = max(vector.rate_id for vector in shaped_vectors) + 1
        clone_rate_ids: dict[int, tuple[int, int]] = {}
        next_rate_id = base_rate_count
        run_start = 0
        while run_start < len(shaped_vectors):
            run_end = run_start + 1
            while run_end < len(shaped_vectors) and shaped_vectors[run_end].rate_id == shaped_vectors[run_start].rate_id:
                run_end += 1
            base_rate_id = shaped_vectors[run_start].rate_id
            run_sources = source_vectors[run_start:run_end]
            run_global_start = run_sources[0].top_level_index
            run_global_end = run_sources[-1].top_level_index
            if run_global_end < target_range[0] or run_global_start > target_range[1]:
                run_start = run_end
                continue
            if base_rate_id not in clone_rate_ids:
                clone_rate_ids[base_rate_id] = (next_rate_id, next_rate_id + 1)
                next_rate_id += 2
            alias_ids = clone_rate_ids[base_rate_id]
            alias_cursor = 0
            target_cursor = run_start
            while target_cursor < run_end:
                global_index = source_vectors[target_cursor].top_level_index
                if global_index < target_range[0] or global_index > target_range[1]:
                    target_cursor += 1
                    continue
                chunk_end = min(target_cursor + max_run_vectors, run_end)
                while chunk_end < run_end and source_vectors[chunk_end].top_level_index <= target_range[1] and (chunk_end - target_cursor) < max_run_vectors:
                    chunk_end += 1
                alias_rate_id = alias_ids[alias_cursor % len(alias_ids)]
                for index in range(target_cursor, chunk_end):
                    shaped_vectors[index] = VectorRecord(
                        rate_id=alias_rate_id,
                        flags=shaped_vectors[index].flags,
                        repeat=1,
                        drive_value=shaped_vectors[index].drive_value,
                        drive_enable=shaped_vectors[index].drive_enable,
                        expect_value=shaped_vectors[index].expect_value,
                        expect_mask=shaped_vectors[index].expect_mask,
                    )
                shaping_segments.append(
                    {
                        "base_rate_id": base_rate_id,
                        "rate_id": alias_rate_id,
                        "global_vector_start": source_vectors[target_cursor].top_level_index,
                        "global_vector_end": source_vectors[chunk_end - 1].top_level_index,
                        "repeat": chunk_end - target_cursor,
                        "shaped": True,
                    }
                )
                alias_cursor += 1
                target_cursor = chunk_end
            shaping_segments.append(
                {
                    "base_rate_id": base_rate_id,
                    "clone_rate_ids": list(alias_ids),
                    "global_vector_start": max(run_global_start, target_range[0]),
                    "global_vector_end": min(run_global_end, target_range[1]),
                    "shaped": True,
                    "kind": "clone_pair",
                }
            )
            run_start = run_end

    folded_records, folded_source_vectors = _fold_with_sources(shaped_vectors, source_vectors)
    return folded_records, folded_source_vectors, shaping_segments


def _build_rate_tables(
    ir_vectors, *, _slot_signatures: _SlotSignatureCache | None = None,
) -> tuple[list[RateDesc], list[RateSlot], dict[tuple, int]]:  # type: ignore[no-untyped-def]
    slot_signatures = _slot_signatures if _slot_signatures is not None else _SlotSignatureCache()
    rate_ids: dict[tuple, int] = {}
    rates: list[RateDesc] = []
    slots: list[RateSlot] = []
    for vector in ir_vectors:
        key = _vector_rate_key(vector, _slot_signatures=slot_signatures)
        if key in rate_ids:
            continue
        rate_ids[key] = len(rates)
        slot_offset = len(slots)
        slot_signature = key[-1]
        rates.append(
            RateDesc(
                period_ticks=vector.rate_key.period_ticks,
                slot_count=len(slot_signature),
                flags=0,
                slot_offset=slot_offset,
            )
        )
        slots.extend(
            RateSlot(
                time_ticks=time_ticks,
                flags=flags,
                drive_mask=drive_mask,
                dir_mask=dir_mask,
                sample_mask=sample_mask,
            )
            for time_ticks, flags, drive_mask, dir_mask, sample_mask in slot_signature
        )
    return rates, slots, rate_ids


def _pack_rate_entries(
    rates: list[RateDesc],
    slots: list[RateSlot],
    entries,
    loop_blocks: list[LoopBlock],
    *,
    tick_hz: int,
    active_pins: int,
    max_rates: int | None,
) -> tuple[bytes, bool]:
    if any(rate.period_ticks > 0xFFFF for rate in rates):
        wide_rates = [
            RateDesc32(
                period_ticks=rate.period_ticks,
                slot_count=rate.slot_count,
                flags=rate.flags,
                slot_offset=rate.slot_offset,
            )
            for rate in rates
        ]
        wide_slots = [
            RateSlot32(
                time_ticks=slot.time_ticks,
                flags=slot.flags,
                drive_mask=slot.drive_mask,
                dir_mask=slot.dir_mask,
                sample_mask=slot.sample_mask,
            )
            for slot in slots
        ]
        return (
            pack_wide_rate_vector(
                wide_rates,
                slots=wide_slots,
                vectors=entries,
                tick_hz=tick_hz,
                active_pins=active_pins,
                loop_blocks=loop_blocks,
                max_rates=max_rates,
            ),
            True,
        )
    return (
        pack_rate_vector(
            rates,
            slots,
            entries,
            tick_hz=tick_hz,
            active_pins=active_pins,
            loop_blocks=loop_blocks,
            max_rates=max_rates,
        ),
        False,
    )


def _is_loop_block_capacity_error(code: object, message: object) -> bool:
    """Recognize only this encoder's exact block-limit cause, not all Loop errors."""
    return (code == "ERR_TOO_MANY_LOOP_BLOCKS"
            and message == f"preserved loop package exceeds STPG_MAX_LOOP_BLOCKS={STPG_MAX_LOOP_BLOCKS}")


def build_preserved_loop_rate_vector(ir, *, tick_hz: int, max_rates: int | None = None,
                                     allow_approx: bool = False) -> RateBuildResult:  # type: ignore[no-untyped-def]
    """Compile structural STIL loops through the existing host/firmware ABI.

    The firmware ABI has no repeat field on LoopRefRecord.  A loop body with
    one effective record is therefore represented as bounded VectorRecord
    repeats; bodies with two or more records use one LoopBlock plus a
    LoopRefRecord.  Nested loops cannot be represented by the current ABI and
    are rejected explicitly instead of being silently expanded.
    """

    if not ir.sequence_items:
        raise StilError("ERR_LOOP_PRESERVE_UNAVAILABLE", "loop-preserved IR has no sequence items")
    if ir.approximate_reasons and not allow_approx:
        raise StilError("ERR_APPROXIMATION_REQUIRED", "preserved loops require exact timing unless --allow-approx is explicit")
    slot_signatures = _SlotSignatureCache()
    rates, slots, rate_ids = _build_rate_tables(ir.vectors, _slot_signatures=slot_signatures)
    active_pins = ir.resolved_pattern.pin_map.active_pins
    if any(bit >= active_pins for vector in ir.vectors for bit in vector.signal_bit_assignments.values()):
        raise StilError("ERR_TOO_MANY_ACTIVE_PINS", "signal bit exceeds active_pins in pin map")

    records = _VectorRecordCache()

    def record_for(vector) -> VectorRecord:  # type: ignore[no-untyped-def]
        return records.record(vector, rate_ids[_vector_rate_key(vector, _slot_signatures=slot_signatures)])

    entries: list[VectorRecord | LoopRefRecord] = []
    loop_blocks: list[LoopBlock] = []
    for item in ir.sequence_items:
        if not hasattr(item, "items"):
            entries.append(record_for(item))
            continue
        body_items = tuple(item.items)
        if int(item.loop_count) <= 0 or int(item.loop_count) > 0xFFFFFFFF:
            raise StilError(
                "ERR_LOOP_COUNT_UNSUPPORTED",
                "preserved loop count must fit the firmware uint32 LoopBlock field",
            )
        if not body_items or any(hasattr(body_item, "items") for body_item in body_items):
            raise StilError(
                "ERR_NESTED_LOOP_UNSUPPORTED",
                "current Rate Vector ABI supports LoopBlock bodies containing VectorRecord only",
            )
        body_records = list(fold_adjacent_repeats([record_for(body_item) for body_item in body_items]))
        if len(body_records) == 1:
            base = body_records[0]
            remaining = int(item.loop_count) * int(base.repeat)
            while remaining:
                repeat = min(remaining, 0xFFFF)
                entries.append(
                    VectorRecord(
                        rate_id=base.rate_id,
                        flags=base.flags,
                        repeat=repeat,
                        drive_value=base.drive_value,
                        drive_enable=base.drive_enable,
                        expect_value=base.expect_value,
                        expect_mask=base.expect_mask,
                    )
                )
                remaining -= repeat
            continue
        if len(loop_blocks) >= STPG_MAX_LOOP_BLOCKS:
            raise StilError(
                "ERR_TOO_MANY_LOOP_BLOCKS",
                f"preserved loop package exceeds STPG_MAX_LOOP_BLOCKS={STPG_MAX_LOOP_BLOCKS}",
            )
        loop_id = len(loop_blocks)
        loop_blocks.append(LoopBlock(loop_count=int(item.loop_count), body_records=tuple(body_records)))
        entries.append(LoopRefRecord(loop_id=loop_id))

    payload, wide = _pack_rate_entries(
        rates,
        slots,
        entries,
        loop_blocks,
        tick_hz=tick_hz,
        active_pins=active_pins,
        max_rates=max_rates,
    )
    validate_rate_vector(payload, expected_tick_hz=tick_hz, max_rates=max_rates)
    return RateBuildResult(
        pattern_bytes=payload,
        rate_count=len(rates),
        slot_count=len(slots),
        vector_count=int(ir.logical_vector_count or len(ir.vectors)),
        folded_vectors=tuple(entry for entry in entries if isinstance(entry, VectorRecord)),
        folded_source_vectors=(),
        loop_blocks=tuple(loop_blocks),
        sequence_entries=tuple(entries),
        rate_waveform_tables=tuple(str(key[0]) for key in rate_ids),
    )


def compile_rate_vector(
    ir,
    *,
    tick_hz: int,
    allow_approx: bool = False,
    shaping: ShapingOptions | None = None,
    max_rates: int | None = None,
) -> RateBuildResult:  # type: ignore[no-untyped-def]
    if ir.loop_preserved or ir.sequence_items:
        return build_preserved_loop_rate_vector(ir, tick_hz=tick_hz, max_rates=max_rates, allow_approx=allow_approx)
    if not ir.vectors:
        raise StilError("ERR_EMPTY_PATTERN", "pattern has no vectors")
    if ir.approximate_reasons and not allow_approx:
        raise StilError("ERR_APPROXIMATION_REQUIRED", "rate-vector backend requires approximation; rerun with --allow-approx or use --prefer-raw")

    signal_bits = sorted({bit for vector in ir.vectors for bit in vector.signal_bit_assignments.values()})
    active_pins = ir.resolved_pattern.pin_map.active_pins
    if any(bit >= active_pins for bit in signal_bits):
        raise StilError("ERR_TOO_MANY_ACTIVE_PINS", "signal bit exceeds active_pins in pin map")
    slot_signatures = _SlotSignatureCache()
    rates, slots, _rate_ids = _build_rate_tables(ir.vectors, _slot_signatures=slot_signatures)
    rate_waveform_tables = [str(key[0]) for key in _rate_ids]
    vectors = build_rate_vector_records(ir, _slot_signatures=slot_signatures)
    folded_vectors, folded_source_vectors, shaping_segments = build_folded_rate_vector_records(
        ir, shaping=shaping, _unfolded_records=vectors,
    )
    if shaping is not None and shaping.enabled and shaping_segments:
        clone_rate_ids = sorted(
            {
                int(segment["rate_id"])
                for segment in shaping_segments
                if "rate_id" in segment
            }
        )
        for clone_rate_id in clone_rate_ids:
            source_rate_id = int(next(segment["base_rate_id"] for segment in shaping_segments if segment.get("rate_id") == clone_rate_id))
            source_rate = rates[source_rate_id]
            rate_waveform_tables.append(rate_waveform_tables[source_rate_id])
            rates.append(
                RateDesc(
                    period_ticks=source_rate.period_ticks,
                    slot_count=source_rate.slot_count,
                    flags=source_rate.flags,
                    slot_offset=source_rate.slot_offset,
                )
            )
    # The original v1 ABI stores period_ticks in uint16.  Do not approximate
    # or wrap a longer STIL period: promote the complete Rate Vector to the
    # v2 wide-wait ABI when any rate needs more than 65535 ticks.  The vector
    # records remain byte-for-byte compatible; only rate/slot table widths
    # change, as specified by the v2 format.
    if any(rate.period_ticks > 0xFFFF for rate in rates):
        wide_rates = [
            RateDesc32(
                period_ticks=rate.period_ticks,
                slot_count=rate.slot_count,
                flags=rate.flags,
                slot_offset=rate.slot_offset,
            )
            for rate in rates
        ]
        wide_slots = [
            RateSlot32(
                time_ticks=slot.time_ticks,
                flags=slot.flags,
                drive_mask=slot.drive_mask,
                dir_mask=slot.dir_mask,
                sample_mask=slot.sample_mask,
            )
            for slot in slots
        ]
        payload = pack_wide_rate_vector(
            wide_rates,
            wide_slots,
            folded_vectors,
            tick_hz=tick_hz,
            active_pins=active_pins,
            max_rates=max_rates,
        )
    else:
        payload = pack_rate_vector(
            rates,
            slots,
            folded_vectors,
            tick_hz=tick_hz,
            active_pins=active_pins,
            max_rates=max_rates,
        )
    validate_rate_vector(payload, expected_tick_hz=tick_hz, max_rates=max_rates)
    return RateBuildResult(
        pattern_bytes=payload,
        rate_count=len(rates),
        slot_count=len(slots),
        vector_count=len(vectors),
        folded_vectors=tuple(folded_vectors),
        folded_source_vectors=tuple(folded_source_vectors),
        practical_chunk_shaping_segments=tuple(shaping_segments),
        rate_waveform_tables=tuple(rate_waveform_tables),
    )


def _build_slot_signature(vector) -> tuple[tuple[int, int, int, int, int], ...]:  # type: ignore[no-untyped-def]
    phase_actions: dict[int, list[tuple[int, str, object, int | None]]] = {}
    for signal_name, bit in vector.signal_bit_assignments.items():
        char = vector.signal_chars[signal_name]
        final_level = final_drive_level(char, vector.signal_events.get(signal_name, ()))
        for signal_event in vector.signal_events.get(signal_name, ()):
            phase_actions.setdefault(signal_event.time_ticks, []).append(
                (bit, char, signal_event, final_level)
            )
    if not phase_actions:
        phase_actions[0] = []

    slot_signature: list[tuple[int, int, int, int, int]] = []
    direction_states: dict[int, set[bool]] = {}
    for time_ticks in sorted(phase_actions):
        drive_masks: dict[int, int] = {}
        drive_bits: dict[int, int] = {}
        dir_mask = 0
        sample_mask = 0
        has_sample = False
        for bit, source_char, signal_event, final_level in phase_actions[time_ticks]:
            char = event_char(signal_event, char=source_char)
            mask = 1 << bit
            drive_op = drive_op_for_event(signal_event, char=char)
            if drive_op is None and char in ("1", "R", "0", "F"):
                # Preserve the legacy direction-bearing forms while keeping
                # the value correct when an event is not at vector start.
                implied_level = drive_level_for_char(char)
                drive_op = (
                    STPG_DRIVE_VECTOR
                    if signal_event.time_ticks == 0 and final_level == implied_level
                    else (STPG_DRIVE_ONE if implied_level == 1 else STPG_DRIVE_ZERO)
                )
            elif drive_op in (STPG_DRIVE_ZERO, STPG_DRIVE_ONE) and signal_event.time_ticks == 0:
                event_level = 1 if drive_op == STPG_DRIVE_ONE else 0
                if final_level == event_level:
                    drive_op = STPG_DRIVE_VECTOR
            if drive_op is not None:
                previous_op = drive_bits.get(bit)
                if previous_op is not None and previous_op != drive_op:
                    raise StilError(
                        "ERR_CONFLICTING_DRIVE_ACTIONS",
                        f"conflicting drive operations for bit {bit} at time {time_ticks}",
                    )
                drive_bits[bit] = drive_op
                dir_mask |= mask
                direction_states.setdefault(bit, set()).add(drive_op != STIL_DRIVE_RELEASE)
                if drive_op != STIL_DRIVE_RELEASE:
                    drive_masks[drive_op] = drive_masks.get(drive_op, 0) | mask
            elif char == "Z":
                dir_mask |= mask
                direction_states.setdefault(bit, set()).add(False)
            elif char in ("H", "L", "X"):
                has_sample = True
                if source_char not in ADDED_WAVEFORM_CHARS or char != "X":
                    sample_mask |= mask
            elif char == "T":
                pass
            else:
                raise StilError("ERR_RATE_VECTOR_UNSUPPORTED_CHAR", f"unsupported rate-vector char {char}")
        operations = sorted(drive_masks.items())
        if not operations:
            operations = [(STPG_DRIVE_NONE, 0)]
        for operation_index, (drive_op, drive_mask) in enumerate(operations):
            flags = drive_op
            if operation_index == 0:
                if dir_mask != 0:
                    flags |= STPG_RATE_SLOT_DIR_UPDATE
                if has_sample:
                    flags |= STPG_RATE_SLOT_SAMPLE
            slot_signature.append(
                (
                    time_ticks,
                    flags,
                    drive_mask,
                    dir_mask if operation_index == 0 else 0,
                    sample_mask if operation_index == 0 else 0,
                )
            )

    conflicting_direction = sorted(bit for bit, states in direction_states.items() if len(states) > 1)
    if conflicting_direction:
        raise StilError(
            "ERR_RATE_VECTOR_DIRECTION_UNSUPPORTED",
            "rate-vector ABI applies the final vector output-enable state to every direction update; "
            f"cannot encode within-vector drive/release transitions on bits {conflicting_direction}",
        )
    if len(slot_signature) > STPG_MAX_SLOTS_PER_RATE:
        raise StilError(
            "ERR_RATE_TOO_COMPLEX",
            f"rate requires {len(slot_signature)} slots; maximum is {STPG_MAX_SLOTS_PER_RATE}",
        )
    return tuple(slot_signature)
