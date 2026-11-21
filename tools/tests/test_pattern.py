from stpg.constants import (
    ERR_RATEVEC_BAD_HEADER,
    ERR_RATEVEC_BAD_OFFSET,
    ERR_RATE_PERIOD_MISMATCH,
    ERR_UNSUPPORTED,
    STPG_RATEVEC_FLAG_HAS_LOOP,
    STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4,
    STPG_VECTOR_FLAG_EXPLICIT_WAIT_P4_END,
    STPG_ACTIVE_PINS_V01,
    STPG_EVF_COMPARE_POINT,
    STPG_EVF_SAMPLE_POINT,
    STPG_RATE_SLOT_DIR_UPDATE,
    STPG_RATE_SLOT_SAMPLE,
)
from stpg.crc32 import crc32
from stpg.pattern import (
    ExecEvent,
    LoopBlock,
    LoopRefRecord,
    PatternValidationError,
    RateDesc,
    RateSlot,
    VectorRecord,
    expanded_vector_count_estimate,
    fold_adjacent_repeats,
    pack_pattern,
    pack_rate_vector,
    unpack_pattern,
    unpack_rate_vector,
    unpack_rate_vector_full,
    validate_pattern,
    validate_rate_vector,
)


def test_exec_event_crc_vector() -> None:
    event = ExecEvent(wait_ticks=100, out_value=1, out_enable=0xFFFF)
    assert crc32(event.pack()) == 0xEC76F397
    assert crc32(bytes(20)) == 0x0FD59B8D


def test_minimal_pattern_crc_vector() -> None:
    pattern = pack_pattern([ExecEvent(wait_ticks=0)], tick_hz=150_000_000)
    header, events = unpack_pattern(pattern)
    assert len(pattern) == 60
    assert header.data_crc32 == 0x0FD59B8D
    assert header.header_crc32 == 0x9E4DA4F2
    assert crc32(pattern) == 0xA3C1CA20
    assert len(events) == 1


def test_pattern_roundtrip() -> None:
    events = [
        ExecEvent(wait_ticks=i, out_value=i, out_enable=0xFFFF)
        for i in range(16)
    ]
    pattern = pack_pattern(events, tick_hz=150_000_000)
    header, unpacked = unpack_pattern(pattern)
    assert header.active_pins == STPG_ACTIVE_PINS_V01
    assert header.event_count == len(events)
    assert unpacked == events


def test_reject_rate_vector_magic() -> None:
    with_rate = pack_rate_vector(
        [RateDesc(period_ticks=16, slot_count=1, flags=0, slot_offset=0)],
        [RateSlot(time_ticks=0, flags=STPG_RATE_SLOT_DIR_UPDATE | STPG_RATE_SLOT_SAMPLE, drive_mask=1, dir_mask=1, sample_mask=1)],
        [VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=1, drive_enable=1, expect_value=1, expect_mask=1)],
        tick_hz=150_000_000,
    )
    validate_pattern(with_rate)


def test_sample_event_roundtrip() -> None:
    events = [ExecEvent(wait_ticks=10, flags=STPG_EVF_SAMPLE_POINT, sample_mask=0x000F)]
    pattern = pack_pattern(events, tick_hz=150_000_000)
    _, unpacked = unpack_pattern(pattern)
    assert unpacked == events


def test_compare_event_roundtrip() -> None:
    events = [
        ExecEvent(
            wait_ticks=10,
            flags=STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT,
            sample_mask=0x000F,
            expect_value=0x0003,
        )
    ]
    pattern = pack_pattern(events, tick_hz=150_000_000)
    _, unpacked = unpack_pattern(pattern)
    assert unpacked == events


def test_rate_vector_roundtrip() -> None:
    pattern = pack_rate_vector(
        [RateDesc(period_ticks=16, slot_count=2, flags=0, slot_offset=0)],
        [
            RateSlot(time_ticks=0, flags=1 | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=0x3, dir_mask=0x3, sample_mask=0),
            RateSlot(time_ticks=8, flags=2 | STPG_RATE_SLOT_SAMPLE, drive_mask=0x3, dir_mask=0, sample_mask=0x3),
        ],
        [VectorRecord(rate_id=0, flags=0, repeat=2, drive_value=0x3, drive_enable=0x3, expect_value=0x2, expect_mask=0x3)],
        tick_hz=150_000_000,
    )
    header, rates, slots, vectors = unpack_rate_vector(pattern)
    assert header.rate_count == 1
    assert rates[0].slot_count == 2
    assert slots[1].time_ticks == 8
    assert vectors[0].repeat == 2


def test_rate_vector_p4_end_marker_requires_single_opt_in_marker() -> None:
    rates = [RateDesc(period_ticks=16, slot_count=1, flags=0, slot_offset=0)]
    slots = [RateSlot(time_ticks=0, flags=1, drive_mask=1, dir_mask=0, sample_mask=0)]
    marked = pack_rate_vector(
        rates,
        slots,
        [VectorRecord(rate_id=0, flags=STPG_VECTOR_FLAG_EXPLICIT_WAIT_P4_END, repeat=1, drive_value=1, drive_enable=1, expect_value=0, expect_mask=0)],
        tick_hz=150_000_000,
        extra_header_flags=STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4,
    )
    validate_rate_vector(marked)
    unmarked = pack_rate_vector(
        rates,
        slots,
        [VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=1, drive_enable=1, expect_value=0, expect_mask=0)],
        tick_hz=150_000_000,
        extra_header_flags=STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4,
    )
    try:
        validate_rate_vector(unmarked)
    except PatternValidationError as exc:
        assert exc.error_code == ERR_RATEVEC_BAD_HEADER
    else:
        raise AssertionError("P4 header opt-in without an end marker should be rejected")


def test_rate_vector_loop_roundtrip() -> None:
    pattern = pack_rate_vector(
        [RateDesc(period_ticks=16, slot_count=2, flags=0, slot_offset=0)],
        [
            RateSlot(time_ticks=0, flags=1 | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=0x3, dir_mask=0x3, sample_mask=0),
            RateSlot(time_ticks=8, flags=2 | STPG_RATE_SLOT_SAMPLE, drive_mask=0x3, dir_mask=0, sample_mask=0x3),
        ],
        [LoopRefRecord(loop_id=0)],
        tick_hz=150_000_000,
        loop_blocks=[
            LoopBlock(
                loop_count=4,
                body_records=(
                    VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=0x1, drive_enable=0x3, expect_value=0x1, expect_mask=0x3),
                    VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=0x2, drive_enable=0x3, expect_value=0x2, expect_mask=0x3),
                ),
            )
        ],
    )
    header, rates, slots, entries, loop_blocks = unpack_rate_vector_full(pattern)
    assert header.flags == STPG_RATEVEC_FLAG_HAS_LOOP
    assert header.reserved0 != 0
    assert rates[0].slot_count == 2
    assert isinstance(entries[0], LoopRefRecord)
    assert len(loop_blocks) == 1
    assert loop_blocks[0].loop_count == 4
    assert loop_blocks[0].body_records[1].drive_value == 0x2


def test_fold_adjacent_repeats_only_merges_identical_vectors() -> None:
    entries = [
        VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=1, drive_enable=1, expect_value=1, expect_mask=1),
        VectorRecord(rate_id=0, flags=0, repeat=2, drive_value=1, drive_enable=1, expect_value=1, expect_mask=1),
        LoopRefRecord(loop_id=0),
        VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=2, drive_enable=1, expect_value=2, expect_mask=1),
    ]
    folded = fold_adjacent_repeats(entries)
    assert len(folded) == 3
    assert isinstance(folded[0], VectorRecord)
    assert folded[0].repeat == 3
    assert isinstance(folded[1], LoopRefRecord)


def test_expanded_vector_count_estimate_counts_loops() -> None:
    loop_blocks = [
        LoopBlock(
            loop_count=3,
            body_records=(
                VectorRecord(rate_id=0, flags=0, repeat=2, drive_value=1, drive_enable=1, expect_value=0, expect_mask=0),
                VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=2, drive_enable=1, expect_value=0, expect_mask=0),
            ),
        )
    ]
    entries = [LoopRefRecord(loop_id=0), VectorRecord(rate_id=0, flags=0, repeat=4, drive_value=3, drive_enable=1, expect_value=0, expect_mask=0)]
    assert expanded_vector_count_estimate(entries, loop_blocks) == 13


def test_rate_vector_rejects_loop_ref_out_of_range() -> None:
    pattern = pack_rate_vector(
        [RateDesc(period_ticks=16, slot_count=1, flags=0, slot_offset=0)],
        [RateSlot(time_ticks=0, flags=1, drive_mask=1, dir_mask=0, sample_mask=0)],
        [LoopRefRecord(loop_id=1)],
        tick_hz=150_000_000,
        loop_blocks=[
            LoopBlock(
                loop_count=2,
                body_records=(VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=1, drive_enable=1, expect_value=0, expect_mask=0),),
            )
        ],
    )
    try:
        validate_rate_vector(pattern)
    except PatternValidationError as exc:
        assert exc.error_code == ERR_RATEVEC_BAD_OFFSET
    else:
        raise AssertionError("out-of-range loop_id should be rejected")


def test_rate_vector_rejects_loop_flag_offset_mismatch() -> None:
    pattern = bytearray(
        pack_rate_vector(
            [RateDesc(period_ticks=16, slot_count=1, flags=0, slot_offset=0)],
            [RateSlot(time_ticks=0, flags=1, drive_mask=1, dir_mask=0, sample_mask=0)],
            [VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=1, drive_enable=1, expect_value=0, expect_mask=0)],
            tick_hz=150_000_000,
        )
    )
    pattern[8:12] = STPG_RATEVEC_FLAG_HAS_LOOP.to_bytes(4, "little")
    pattern[60:64] = crc32(bytes(pattern[:60])).to_bytes(4, "little")
    try:
        validate_rate_vector(bytes(pattern))
    except PatternValidationError as exc:
        assert exc.error_code == ERR_RATEVEC_BAD_HEADER
    else:
        raise AssertionError("loop presence flag mismatch should be rejected")


def test_rate_vector_rejects_descending_time_ticks() -> None:
    pattern = pack_rate_vector(
        [RateDesc(period_ticks=16, slot_count=2, flags=0, slot_offset=0)],
        [
            RateSlot(time_ticks=8, flags=1, drive_mask=1, dir_mask=0, sample_mask=0),
            RateSlot(time_ticks=4, flags=1, drive_mask=1, dir_mask=0, sample_mask=0),
        ],
        [VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=1, drive_enable=1, expect_value=0, expect_mask=0)],
        tick_hz=150_000_000,
    )
    try:
        validate_rate_vector(pattern)
    except PatternValidationError as exc:
        assert exc.error_code == ERR_RATE_PERIOD_MISMATCH
    else:
        raise AssertionError("descending time_ticks should be rejected")
