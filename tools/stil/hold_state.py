"""Derive logical HOLD state from binary operations, without expanding repeats.

A Rate Vector updates only the bits named by its slots.  Its standalone map
therefore cannot describe the terminal state of a resumed segment: bits not
written by that segment retain the preceding segment's state.
"""

from __future__ import annotations

from stpg.constants import (
    STPG_DRIVE_COMPLEMENT,
    STPG_DRIVE_ONE,
    STPG_DRIVE_VECTOR,
    STPG_DRIVE_ZERO,
    STPG_MACROVEC_MAGIC,
    STPG_PATTERN_MAGIC,
    STPG_RATE_SLOT_DIR_UPDATE,
    STPG_RATE_SLOT_DRIVE_OP_MASK,
    STPG_RATEVEC_MAGIC,
)
from stpg.pattern import (
    LoopRefRecord,
    unpack_macro_vector,
    unpack_pattern,
    unpack_rate_vector_full,
    validate_pattern,
)


def derive_terminal_hold(
    pattern_bytes: bytes, entry_value: int = 0, entry_enable: int = 0
) -> tuple[int, int]:
    """Return the terminal logical value/enable of a complete successful run.

    The firmware's COMPLEMENT operation assigns ``~vector.drive_value``;
    it does *not* toggle the current state.  All slot operations are masked
    assignments of constants for a given record.  Repeating a record or a
    Loop body is consequently idempotent for terminal state, and can be
    evaluated once regardless of the encoded repeat/loop count.  Work and
    memory are bounded by the encoded tables, not the expanded event count.

    This predicts only successful terminal state, not timing or the state at
    an early abort.  No transport or source-map data is used.
    """

    validate_pattern(pattern_bytes)
    magic = int.from_bytes(pattern_bytes[:4], "little")
    if magic == STPG_PATTERN_MAGIC:
        header, events = unpack_pattern(pattern_bytes)
    elif magic == STPG_MACROVEC_MAGIC:
        # Keep the existing explicit macro-stream package-build interface.
        # This does not add macro-stream support to the segmented executor.
        header, macro_records = unpack_macro_vector(pattern_bytes)
    elif magic == STPG_RATEVEC_MAGIC:
        header, rates, slots, entries, loops = unpack_rate_vector_full(pattern_bytes)
    else:
        raise ValueError("unsupported binary format for HOLD derivation")
    pin_mask = (1 << header.active_pins) - 1
    if entry_value < 0 or entry_enable < 0 or (entry_value | entry_enable) & ~pin_mask:
        raise ValueError("entry HOLD state exceeds binary active_pins")

    if magic == STPG_PATTERN_MAGIC:
        # Raw events carry absolute outputs, unlike Rate Vector slot masks.
        return events[-1].out_value & pin_mask, events[-1].out_enable & pin_mask
    if magic == STPG_MACROVEC_MAGIC:
        value, enable = entry_value, entry_enable
        for record in macro_records:
            if record.family != 4:  # Family 4 is sample-only, preserving outputs.
                value, enable = record.drive_value, record.drive_enable
        return value, enable

    # Per-rate template: assigned bits, vector-dependent bits, XOR constants,
    # and direction-update bits. Later slots override earlier ones, including
    # overlapping masks and equal-time slots, just as in the emitter.
    templates: list[tuple[int, int, int, int]] = []
    for rate in rates:
        update = source = constant = direction = 0
        for slot in slots[rate.slot_offset : rate.slot_offset + rate.slot_count]:
            op = slot.flags & STPG_RATE_SLOT_DRIVE_OP_MASK
            mask = slot.drive_mask
            if op in (STPG_DRIVE_VECTOR, STPG_DRIVE_ZERO, STPG_DRIVE_ONE, STPG_DRIVE_COMPLEMENT):
                update |= mask
                source &= ~mask
                constant &= ~mask
                if op in (STPG_DRIVE_VECTOR, STPG_DRIVE_COMPLEMENT):
                    source |= mask
                if op in (STPG_DRIVE_ONE, STPG_DRIVE_COMPLEMENT):
                    constant |= mask
            if slot.flags & STPG_RATE_SLOT_DIR_UPDATE:
                direction |= slot.dir_mask
        templates.append((update, source, constant, direction))

    value, enable = entry_value, entry_enable
    for entry in entries:
        records = loops[entry.loop_id].body_records if isinstance(entry, LoopRefRecord) else (entry,)
        for record in records:
            update, source, constant, direction = templates[record.rate_id]
            value = (value & ~update) | ((record.drive_value & source) ^ constant)
            enable = (enable & ~direction) | (record.drive_enable & direction)
    return value, enable
