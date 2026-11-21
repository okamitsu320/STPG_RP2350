from __future__ import annotations

from dataclasses import dataclass

from stpg.constants import (
    STPG_DRIVE_ONE,
    STPG_DRIVE_ZERO,
    STPG_EVF_COMPARE_POINT,
    STPG_EVF_SAMPLE_POINT,
    STPG_T_MIN_TICKS,
)
from stpg.pattern import ExecEvent, pack_pattern, validate_pattern

from .errors import StilError
from .waveform_actions import STIL_DRIVE_RELEASE, drive_level_for_char, drive_op_for_event, event_char

STPG_PIO_EVENT_OVERHEAD_TICKS = STPG_T_MIN_TICKS


def raw_event_phase_times(vector) -> tuple[int, ...]:  # type: ignore[no-untyped-def]
    """Return the sorted phase times that compile to Raw Event records."""

    phase_times = {
        int(signal_event.time_ticks)
        for events in vector.signal_events.values()
        for signal_event in events
    }
    # Raw records act first and wait afterwards. A delayed first action needs
    # a no-op at vector start, or it would incorrectly execute at tick zero.
    return tuple(sorted(phase_times | {0}))


@dataclass(frozen=True)
class RawBuildResult:
    pattern_bytes: bytes
    report_mode: str = "raw-event"
    vector_count: int = 0


def compile_raw_event(ir, *, tick_hz: int, allow_approx: bool = False) -> RawBuildResult:  # type: ignore[no-untyped-def]
    if ir.approximate_reasons and not allow_approx:
        raise StilError("ERR_APPROXIMATION_REQUIRED", "raw-event backend requires approximation; rerun with --allow-approx")
    events: list[ExecEvent] = []
    previous_value = 0
    previous_enable = 0
    for vector in ir.vectors:
        desired_delta_ticks = vector.rate_key.period_ticks
        if desired_delta_ticks <= 0:
            raise StilError("ERR_BAD_RAW_TIMING", "raw-event vector period must be non-zero")
        if desired_delta_ticks < STPG_PIO_EVENT_OVERHEAD_TICKS:
            raise StilError(
                "ERR_BAD_RAW_TIMING",
                f"raw-event vector period {desired_delta_ticks} ticks is smaller than event overhead {STPG_PIO_EVENT_OVERHEAD_TICKS}",
            )
        phase_actions: dict[int, list[tuple[str, int, str, object]]] = {}
        for signal_name, bit in vector.signal_bit_assignments.items():
            char = vector.signal_chars[signal_name]
            for signal_event in vector.signal_events.get(signal_name, ()):
                phase_actions.setdefault(signal_event.time_ticks, []).append(
                    (signal_name, bit, char, signal_event)
                )
        phase_actions.setdefault(0, [])
        phase_times = raw_event_phase_times(vector)
        drive_value = previous_value
        out_enable = previous_enable
        for index, phase_time in enumerate(phase_times):
            flags = 0
            sample_mask = 0
            compare_mask = 0
            expect_value = 0
            phase_drive_ops: dict[int, int] = {}
            phase_direction_ops: dict[int, bool] = {}
            phase_compare_levels: dict[int, int] = {}
            for signal_name, bit, source_char, signal_event in phase_actions[phase_time]:
                char = event_char(signal_event, char=source_char)
                mask = 1 << bit
                drive_op = drive_op_for_event(signal_event, char=char)
                if drive_op is None and char in ("1", "R", "0", "F"):
                    drive_op = STPG_DRIVE_ONE if drive_level_for_char(char) == 1 else STPG_DRIVE_ZERO
                if drive_op in (STPG_DRIVE_ZERO, STPG_DRIVE_ONE, STIL_DRIVE_RELEASE):
                    direction_enabled = drive_op != STIL_DRIVE_RELEASE
                    previous_direction = phase_direction_ops.get(bit)
                    if previous_direction is not None and previous_direction != direction_enabled:
                        raise StilError(
                            "ERR_CONFLICTING_DRIVE_ACTIONS",
                            f"conflicting drive/release operations for bit {bit} at time {phase_time}",
                        )
                    phase_direction_ops[bit] = direction_enabled
                if drive_op in (STPG_DRIVE_ZERO, STPG_DRIVE_ONE):
                    previous_op = phase_drive_ops.get(bit)
                    if previous_op is not None and previous_op != drive_op:
                        raise StilError(
                            "ERR_CONFLICTING_DRIVE_ACTIONS",
                            f"conflicting drive operations for bit {bit} at time {phase_time}",
                        )
                    phase_drive_ops[bit] = drive_op
                if drive_op == STPG_DRIVE_ONE:
                    drive_value |= mask
                    out_enable |= mask
                elif drive_op == STPG_DRIVE_ZERO:
                    drive_value &= ~mask
                    out_enable |= mask
                elif drive_op == STIL_DRIVE_RELEASE:
                    out_enable &= ~mask
                elif char == "Z":
                    out_enable &= ~mask
                elif char in ("H", "L", "X"):
                    flags |= STPG_EVF_SAMPLE_POINT
                    sample_mask |= mask
                    if char in ("H", "L"):
                        expected_level = int(char == "H")
                        previous_level = phase_compare_levels.get(bit)
                        if previous_level is not None and previous_level != expected_level:
                            raise StilError(
                                "ERR_CONFLICTING_COMPARE_ACTIONS",
                                f"conflicting compare levels for bit {bit} at time {phase_time}",
                            )
                        phase_compare_levels[bit] = expected_level
                    if char == "H":
                        flags |= STPG_EVF_COMPARE_POINT
                        compare_mask |= mask
                        expect_value |= mask
                    elif char == "L":
                        flags |= STPG_EVF_COMPARE_POINT
                        compare_mask |= mask
                elif char == "T":
                    pass
                else:
                    raise StilError("ERR_RAW_UNSUPPORTED_CHAR", f"unsupported raw-event char {char}")
            next_time = phase_times[index + 1] if index + 1 < len(phase_times) else desired_delta_ticks
            phase_delta_ticks = next_time - phase_time
            if phase_delta_ticks < STPG_PIO_EVENT_OVERHEAD_TICKS:
                raise StilError(
                    "ERR_BAD_RAW_TIMING",
                    f"raw-event sub-slot {phase_delta_ticks} ticks is smaller than event overhead {STPG_PIO_EVENT_OVERHEAD_TICKS}",
                )
            if 0 < STPG_PIO_EVENT_OVERHEAD_TICKS < STPG_T_MIN_TICKS:
                raise StilError("ERR_BAD_RAW_TIMING", "invalid raw-event timing parameters")
            events.append(
                ExecEvent(
                    wait_ticks=phase_delta_ticks - STPG_PIO_EVENT_OVERHEAD_TICKS,
                    flags=flags,
                    out_value=drive_value,
                    out_enable=out_enable,
                    # Capture samples all active pins. When COMPARE_POINT is
                    # set, this ABI field is the care mask, not the union of
                    # sampled H/L/X sites. X must never become an expected L.
                    sample_mask=compare_mask if flags & STPG_EVF_COMPARE_POINT else sample_mask,
                    expect_value=expect_value,
                )
            )
        previous_value = drive_value
        previous_enable = out_enable
    payload = pack_pattern(events, tick_hz=tick_hz, active_pins=ir.resolved_pattern.pin_map.active_pins)
    validate_pattern(payload, expected_tick_hz=tick_hz)
    return RawBuildResult(pattern_bytes=payload, vector_count=len(ir.vectors))
