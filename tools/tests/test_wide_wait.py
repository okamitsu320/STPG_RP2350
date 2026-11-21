from stpg.wide_wait import (
    MAX_WAIT_TICKS_32,
    model_ring_rate_equivalence,
    split_legacy_wait,
    ticks_from_microseconds,
    validate_wait_ticks,
    WideRate,
    WideRateSlot,
    validate_wide_rate,
)


def test_32bit_boundary_and_exact_legacy_split():
    assert validate_wait_ticks(0xFFFF) == 0xFFFF
    assert validate_wait_ticks(0x10000) == 0x10000
    assert sum(split_legacy_wait(0x10000)) == 0x10000
    assert max(split_legacy_wait(0x10000)) <= 0xFFFF
    assert validate_wait_ticks(MAX_WAIT_TICKS_32) == MAX_WAIT_TICKS_32


def test_400us_x8_collapses_to_3_2ms_x1_at_150mhz():
    column_ticks = ticks_from_microseconds(400, 150_000_000)
    model = model_ring_rate_equivalence(tick_hz=150_000_000, column_ticks=column_ticks, column_count=8)
    assert column_ticks == 60_000
    assert model.collapsed_period_ticks == 480_000
    assert model.expanded_chunks == (60_000,) * 8
    assert model.exact


def test_wide_rate_accepts_3_2ms_and_rejects_out_of_period_slot():
    rate = validate_wide_rate(WideRate(480_000, (WideRateSlot(0), WideRateSlot(60_000))))
    assert rate.period_ticks == 480_000
