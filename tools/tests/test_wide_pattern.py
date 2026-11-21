from stpg.pattern import (
    ExecEvent32,
    RateDesc32,
    RateSlot32,
    VectorRecord,
    pack_wide_pattern,
    pack_wide_rate_vector,
    unpack_pattern,
    unpack_rate_vector_full,
    unpack_wide_pattern,
    validate_pattern,
)
from stpg.pattern import PatternValidationError


def test_wide_pattern_roundtrip_preserves_32bit_wait():
    pattern = pack_wide_pattern([ExecEvent32(wait_ticks=480_000, out_value=3, out_enable=0xFFFF)], 150_000_000)
    validate_pattern(pattern)
    header, events = unpack_wide_pattern(pattern)
    assert header.version == 2
    assert header.event_size == 24
    assert events[0].wait_ticks == 480_000
    assert events[0].out_value == 3
    _, generic_events = unpack_pattern(pattern)
    assert generic_events == events


def test_wide_rate_vector_roundtrip_preserves_psram_uint32_state():
    pattern = pack_wide_rate_vector(
        [RateDesc32(period_ticks=480_000, slot_count=2, flags=0, slot_offset=0)],
        [
            RateSlot32(time_ticks=0, flags=1, drive_mask=0x3, dir_mask=0x3, sample_mask=0),
            RateSlot32(time_ticks=240_000, flags=0x12, drive_mask=0x3, dir_mask=0, sample_mask=0x3),
        ],
        [VectorRecord(rate_id=0, flags=0, repeat=2, drive_value=0x3, drive_enable=0x3, expect_value=0, expect_mask=0x3)],
        tick_hz=150_000_000,
    )
    validate_pattern(pattern)
    header, rates, slots, vectors, _ = unpack_rate_vector_full(pattern)
    assert header.version == 2
    assert rates[0].period_ticks == 480_000
    assert slots[1].time_ticks == 240_000
    assert vectors[0].repeat == 2


def test_wide_rate_vector_rejects_nonzero_reserved_wire_fields():
    pattern = bytearray(
        pack_wide_rate_vector(
            [RateDesc32(period_ticks=480_000, slot_count=1, flags=0, slot_offset=0)],
            [RateSlot32(time_ticks=240_000, flags=0, drive_mask=0x3, dir_mask=0x3, sample_mask=0)],
            [VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=0x3, drive_enable=0x3, expect_value=0, expect_mask=0)],
            tick_hz=150_000_000,
        )
    )
    # RateDesc32.reserved is the 16-bit field after flags. Recompute both CRCs
    # so the assertion reaches the ABI reserved-field validator.
    pattern[64 + 6] = 1
    from stpg.pattern import RATE_VECTOR_HEADER_STRUCT, crc32

    header = list(RATE_VECTOR_HEADER_STRUCT.unpack(pattern[:64]))
    header[12] = crc32(bytes(pattern[64:]))
    header[16] = 0
    header[16] = crc32(RATE_VECTOR_HEADER_STRUCT.pack(*header)[:60])
    pattern[:64] = RATE_VECTOR_HEADER_STRUCT.pack(*header)
    try:
        validate_pattern(bytes(pattern))
    except PatternValidationError as exc:
        assert "reserved" in str(exc)
    else:
        raise AssertionError("nonzero RateDesc32.reserved was accepted")
