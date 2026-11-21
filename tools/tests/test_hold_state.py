from __future__ import annotations

import random

import pytest

from stil.hold_state import derive_terminal_hold
from stpg.constants import STPG_EVF_SAMPLE_POINT
from stpg.pattern import (
    ExecEvent,
    ExecEvent32,
    LoopBlock,
    LoopRefRecord,
    MacroVectorRecord,
    RateDesc,
    RateDesc32,
    RateSlot,
    RateSlot32,
    VectorRecord,
    pack_pattern,
    pack_macro_vector,
    pack_rate_vector,
    pack_wide_pattern,
    pack_wide_rate_vector,
)


def _expanded_reference(rates, slots, entries, loops, value, enable):
    """Deliberately scalar, expanded implementation of the firmware slot rules."""

    def apply(record):
        nonlocal value, enable
        rate = rates[record.rate_id]
        for _ in range(record.repeat):
            for slot in slots[rate.slot_offset : rate.slot_offset + rate.slot_count]:
                mask = slot.drive_mask
                op = slot.flags & 7
                if op == 1:
                    value = (value & ~mask) | (record.drive_value & mask)
                elif op == 2:
                    value &= ~mask
                elif op == 3:
                    value |= mask
                elif op == 4:
                    value = (value & ~mask) | (~record.drive_value & mask)
                if slot.flags & 8:
                    enable = (enable & ~slot.dir_mask) | (record.drive_enable & slot.dir_mask)

    for entry in entries:
        if isinstance(entry, LoopRefRecord):
            loop = loops[entry.loop_id]
            for _ in range(loop.loop_count):
                for record in loop.body_records:
                    apply(record)
        else:
            apply(entry)
    return value, enable


@pytest.mark.parametrize("active_pins", [16, 24])
@pytest.mark.parametrize("wide", [False, True])
def test_hold_matches_expanded_slot_operations(active_pins: int, wide: bool) -> None:
    rng = random.Random(428)
    mask = (1 << active_pins) - 1
    desc_type = RateDesc32 if wide else RateDesc
    slot_type = RateSlot32 if wide else RateSlot
    pack = pack_wide_rate_vector if wide else pack_rate_vector
    rates, slots = [], []
    for rate_id in range(5):
        rates.append(desc_type(100, 8, 0, len(slots)))
        # Equal-time slots, overlapping masks, all five operations, optional
        # direction updates, and sample-only slots must preserve ordering.
        for index in range(8):
            slots.append(slot_type(
                index // 2, (index + rate_id) % 5 | (8 if index % 3 else 0),
                rng.getrandbits(active_pins), rng.getrandbits(active_pins), 0,
            ))

    def record():
        return VectorRecord(rng.randrange(5), 0, rng.randrange(1, 6),
                            rng.getrandbits(active_pins), rng.getrandbits(active_pins), 0, 0)

    loops = [LoopBlock(3, tuple(record() for _ in range(5))),
             LoopBlock(4, tuple(record() for _ in range(7)))]
    entries = [record() if index % 4 else LoopRefRecord(index % 2) for index in range(100)]
    payload = pack(rates, slots, entries, loop_blocks=loops,
                   tick_hz=200_000_000, active_pins=active_pins)
    for value, enable in [(0, 0), (mask, mask), (mask // 3, mask // 5)]:
        assert derive_terminal_hold(payload, value, enable) == _expanded_reference(
            rates, slots, entries, loops, value, enable
        )


@pytest.mark.parametrize("active_pins", [16, 24])
def test_untouched_bits_and_complement_are_not_lost_or_toggled(active_pins: int) -> None:
    retained = 1 << (active_pins - 1)
    payload = pack_rate_vector(
        [RateDesc(100, 2, 0, 0)],
        [RateSlot(0, 4 | 8, 1, 1, 0), RateSlot(10, 0, 0, 0, 0)],
        [VectorRecord(0, 0, 2, 0, 1, 0, 0)],
        tick_hz=200_000_000, active_pins=active_pins,
    )
    assert derive_terminal_hold(payload, retained, retained) == (retained | 1, retained | 1)


def test_large_repeat_and_loop_counts_are_not_expanded() -> None:
    payload = pack_rate_vector(
        [RateDesc(100, 1, 0, 0)], [RateSlot(0, 1 | 8, 1, 1, 0)],
        [LoopRefRecord(0)],
        loop_blocks=[LoopBlock(0xFFFFFFFF, (VectorRecord(0, 0, 65535, 1, 1, 0, 0),))],
        tick_hz=200_000_000,
    )
    assert derive_terminal_hold(payload, 4, 4) == (5, 5)


@pytest.mark.parametrize("active_pins", [16, 24])
@pytest.mark.parametrize("wide", [False, True])
def test_raw_events_have_absolute_terminal_outputs(active_pins: int, wide: bool) -> None:
    event_type = ExecEvent32 if wide else ExecEvent
    pack = pack_wide_pattern if wide else pack_pattern
    payload = pack([event_type(20, out_value=5, out_enable=5),
                    event_type(20, out_value=0, out_enable=1)],
                   tick_hz=200_000_000, active_pins=active_pins)
    assert derive_terminal_hold(payload, 4, 4) == (0, 1)


@pytest.mark.parametrize("value,enable", [(-1, 0), (0, -1), (1 << 16, 0), (0, 1 << 16)])
def test_hold_rejects_entry_bits_outside_pattern(value: int, enable: int) -> None:
    payload = pack_rate_vector(
        [RateDesc(100, 1, 0, 0)], [RateSlot(0, 1, 1, 0, 0)],
        [VectorRecord(0, 0, 1, 0, 0, 0, 0)], tick_hz=200_000_000,
    )
    with pytest.raises(ValueError, match="entry HOLD"):
        derive_terminal_hold(payload, value, enable)


def test_hold_rejects_corrupt_binary() -> None:
    payload = pack_pattern([ExecEvent(20)], tick_hz=200_000_000)
    with pytest.raises(ValueError, match="CRC|crc"):
        derive_terminal_hold(payload[:-1] + bytes([payload[-1] ^ 1]))


@pytest.mark.parametrize("active_pins", [16, 24])
def test_legacy_macro_terminal_keeps_sample_only_state(active_pins: int) -> None:
    high_bit = 1 << (active_pins - 1)
    records = [MacroVectorRecord(100, 0, 3, 4, STPG_EVF_SAMPLE_POINT, 0, 0, 1, 0)]
    payload = pack_macro_vector(records, tick_hz=200_000_000, active_pins=active_pins)
    assert derive_terminal_hold(payload, high_bit, high_bit) == (high_bit, high_bit)
    for family in (1, 2, 3, 5):
        records.insert(0, MacroVectorRecord(100, 20 if family in (1, 2) else 0,
                                          2, family, 0, high_bit, high_bit, 0, 0))
        payload = pack_macro_vector(records, tick_hz=200_000_000, active_pins=active_pins)
        assert derive_terminal_hold(payload) == (high_bit, high_bit)
