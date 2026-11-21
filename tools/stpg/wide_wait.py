"""Pure timing helpers for versioned 32-bit waits and legacy ring chunks."""

from __future__ import annotations

from dataclasses import dataclass

LEGACY_MAX_WAIT_TICKS = 0xFFFF
MAX_WAIT_TICKS_32 = 0xFFFFFFFF


class WideWaitError(ValueError):
    """Raised when a wait or conversion violates the wire contract."""


def validate_wait_ticks(wait_ticks: int, *, maximum: int = MAX_WAIT_TICKS_32) -> int:
    if isinstance(wait_ticks, bool) or not isinstance(wait_ticks, int):
        raise WideWaitError("wait_ticks must be an integer")
    if not 0 <= wait_ticks <= maximum:
        raise WideWaitError(f"wait_ticks must be in [0, {maximum}]")
    return wait_ticks


def split_legacy_wait(wait_ticks: int, *, chunk_ticks: int = LEGACY_MAX_WAIT_TICKS) -> tuple[int, ...]:
    """Represent one wide wait as positive legacy chunks without changing its sum."""

    validate_wait_ticks(wait_ticks)
    validate_wait_ticks(chunk_ticks, maximum=LEGACY_MAX_WAIT_TICKS)
    if chunk_ticks == 0:
        raise WideWaitError("chunk_ticks must be positive")
    if wait_ticks == 0:
        return (0,)
    full, remainder = divmod(wait_ticks, chunk_ticks)
    chunks = (chunk_ticks,) * full
    return chunks if remainder == 0 else chunks + (remainder,)


def ticks_from_microseconds(microseconds: int | float, tick_hz: int) -> int:
    if microseconds < 0 or tick_hz <= 0:
        raise WideWaitError("microseconds and tick_hz must be positive")
    ticks = round(float(microseconds) * tick_hz / 1_000_000.0)
    return validate_wait_ticks(ticks)


@dataclass(frozen=True)
class RingRateEquivalence:
    tick_hz: int
    column_ticks: int
    column_count: int
    collapsed_period_ticks: int
    expanded_chunks: tuple[int, ...]

    @property
    def expanded_sum_ticks(self) -> int:
        return sum(self.expanded_chunks)

    @property
    def exact(self) -> bool:
        return self.expanded_sum_ticks == self.collapsed_period_ticks


@dataclass(frozen=True)
class WideRateSlot:
    time_ticks: int
    flags: int = 0


@dataclass(frozen=True)
class WideRate:
    period_ticks: int
    slots: tuple[WideRateSlot, ...]


def validate_wide_rate(rate: WideRate) -> WideRate:
    validate_wait_ticks(rate.period_ticks)
    if rate.period_ticks == 0 or not rate.slots:
        raise WideWaitError("a wide rate requires a non-zero period and slots")
    previous = -1
    for slot in rate.slots:
        validate_wait_ticks(slot.time_ticks)
        if slot.time_ticks >= rate.period_ticks or slot.time_ticks < previous:
            raise WideWaitError("wide rate slot times must be ordered and inside the period")
        previous = slot.time_ticks
    return rate


def model_ring_rate_equivalence(
    *, tick_hz: int, column_ticks: int, column_count: int
) -> RingRateEquivalence:
    if tick_hz <= 0 or column_ticks <= 0 or column_count <= 0:
        raise WideWaitError("tick_hz, column_ticks, and column_count must be positive")
    collapsed = validate_wait_ticks(column_ticks * column_count)
    chunks = (validate_wait_ticks(column_ticks),) * column_count
    return RingRateEquivalence(tick_hz, column_ticks, column_count, collapsed, chunks)
