from __future__ import annotations

from dataclasses import dataclass

from stpg.constants import (
    STPG_DRIVE_ONE,
    STPG_DRIVE_ZERO,
    STPG_EVF_COMPARE_POINT,
    STPG_EVF_SAMPLE_POINT,
    STPG_T_MIN_TICKS,
)
from stpg.pattern import MacroVectorRecord, pack_macro_vector, validate_macro_vector

from .errors import StilError
from .family_analyze import classify_micro_op_family
from .waveform_actions import STIL_DRIVE_RELEASE, drive_op_for_event, event_char, vector_compare_char


FAMILY_ID = {
    "M1_DRIVE_THEN_SAMPLE": 1,
    "M2_SAMPLE_THEN_DRIVE": 2,
    "M3_DRIVE_ONLY": 3,
    "M4_SAMPLE_ONLY": 4,
    "M5_SIMULTANEOUS_SAMPLE_DRIVE": 5,
}


@dataclass(frozen=True)
class MacroBuildResult:
    pattern_bytes: bytes
    report_mode: str = "macro-stream"
    vector_count: int = 0
    record_count: int = 0


def _apply_drive_char(value: int, enable: int, bit: int, char: str, signal_event) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    char = event_char(signal_event, char=char)
    mask = 1 << bit
    if signal_event.action_name == "Drive":
        drive_op = drive_op_for_event(signal_event, char=char)
        if drive_op == STPG_DRIVE_ONE:
            return value | mask, enable | mask
        if drive_op == STPG_DRIVE_ZERO:
            return value & ~mask, enable | mask
        if drive_op == STIL_DRIVE_RELEASE:
            return value, enable & ~mask
    elif char in ("1", "R", "P"):
        return value | mask, enable | mask
    elif char in ("0", "F", "N"):
        return value & ~mask, enable | mask
    if char == "Z":
        return value, enable & ~mask
    return value, enable


def compile_macro_stream(ir, *, tick_hz: int):  # type: ignore[no-untyped-def]
    if ir.approximate_reasons:
        raise StilError("ERR_APPROXIMATION_REQUIRED", "macro-stream backend requires exact timing")

    records: list[MacroVectorRecord] = []
    previous_value = 0
    previous_enable = 0
    for vector in ir.vectors:
        family_info = classify_micro_op_family(vector)
        family_name = family_info["family"]
        family_id = FAMILY_ID.get(family_name)
        if family_id is None:
            raise StilError(
                "ERR_MACRO_UNSUPPORTED_FAMILY",
                f"vector line {vector.stil_line} is not in supported macro families: {family_name}",
            )

        drive_times = tuple(family_info["drive_times"])
        sample_times = tuple(family_info["sample_times"])
        period_ticks = int(vector.rate_key.period_ticks)
        if period_ticks <= 0:
            raise StilError("ERR_MACRO_BAD_TIMING", "macro-stream vector period must be non-zero")

        phase_ticks = 0
        if family_name == "M1_DRIVE_THEN_SAMPLE":
            if drive_times != (0,) or len(sample_times) != 1 or sample_times[0] <= 0:
                raise StilError(
                    "ERR_MACRO_UNSUPPORTED_SHAPE",
                    f"line {vector.stil_line}: M1 requires drive@0 and exactly one sample phase >0",
                )
            phase_ticks = int(sample_times[0])
            if phase_ticks < STPG_T_MIN_TICKS or (period_ticks - phase_ticks) < STPG_T_MIN_TICKS:
                raise StilError(
                    "ERR_MACRO_BAD_TIMING",
                    f"line {vector.stil_line}: M1 phase or tail gap is below {STPG_T_MIN_TICKS} ticks",
                )
        elif family_name == "M2_SAMPLE_THEN_DRIVE":
            if sample_times != (0,) or len(drive_times) != 1 or drive_times[0] <= 0:
                raise StilError(
                    "ERR_MACRO_UNSUPPORTED_SHAPE",
                    f"line {vector.stil_line}: M2 requires sample@0 and exactly one drive phase >0",
                )
            phase_ticks = int(drive_times[0])
            if phase_ticks < STPG_T_MIN_TICKS or (period_ticks - phase_ticks) < STPG_T_MIN_TICKS:
                raise StilError(
                    "ERR_MACRO_BAD_TIMING",
                    f"line {vector.stil_line}: M2 phase or tail gap is below {STPG_T_MIN_TICKS} ticks",
                )
        elif family_name == "M3_DRIVE_ONLY":
            if drive_times != (0,):
                raise StilError(
                    "ERR_MACRO_UNSUPPORTED_SHAPE",
                    f"line {vector.stil_line}: M3 requires exactly one drive phase at 0",
                )
        elif family_name == "M4_SAMPLE_ONLY":
            if sample_times != (0,):
                raise StilError(
                    "ERR_MACRO_UNSUPPORTED_SHAPE",
                    f"line {vector.stil_line}: M4 requires exactly one sample phase at 0",
                )
        elif family_name == "M5_SIMULTANEOUS_SAMPLE_DRIVE":
            if drive_times != (0,) or sample_times != (0,):
                raise StilError(
                    "ERR_MACRO_UNSUPPORTED_SHAPE",
                    f"line {vector.stil_line}: M5 requires same-tick sample+drive at 0",
                )
        if family_name in ("M3_DRIVE_ONLY", "M4_SAMPLE_ONLY", "M5_SIMULTANEOUS_SAMPLE_DRIVE"):
            if period_ticks < STPG_T_MIN_TICKS:
                raise StilError(
                    "ERR_MACRO_BAD_TIMING",
                    f"line {vector.stil_line}: period is below {STPG_T_MIN_TICKS} ticks",
                )

        final_value = previous_value
        final_enable = previous_enable
        sample_mask = 0
        compare_mask = 0
        expect_value = 0
        event_flags = 0

        for signal_name, bit in vector.signal_bit_assignments.items():
            source_char = vector.signal_chars[signal_name]
            events = vector.signal_events.get(signal_name, ())
            vector_compare_char(source_char, events)
            for signal_event in events:
                char = event_char(signal_event, char=source_char)
                if signal_event.action_name == "Drive" or signal_event.action_name.startswith("Dir"):
                    final_value, final_enable = _apply_drive_char(final_value, final_enable, bit, char, signal_event)
                elif signal_event.action_name == "Compare":
                    mask = 1 << bit
                    if char in ("H", "L", "X"):
                        event_flags |= STPG_EVF_SAMPLE_POINT
                        sample_mask |= mask
                        if char == "H":
                            event_flags |= STPG_EVF_COMPARE_POINT
                            compare_mask |= mask
                            expect_value |= mask
                        elif char == "L":
                            event_flags |= STPG_EVF_COMPARE_POINT
                            compare_mask |= mask
                    elif char == "T":
                        pass
                    else:
                        raise StilError(
                            "ERR_MACRO_UNSUPPORTED_CHAR",
                            f"line {vector.stil_line}: unsupported compare char {char}",
                        )

        if family_id == 3 and sample_mask != 0:
            raise StilError("ERR_MACRO_UNSUPPORTED_SHAPE", f"line {vector.stil_line}: M3 cannot carry compare/sample")
        if family_id in (1, 2, 4, 5) and sample_mask == 0:
            raise StilError(
                "ERR_MACRO_UNSUPPORTED_SHAPE",
                f"line {vector.stil_line}: sampled macro family requires at least one H/L/X compare site",
            )

        next_record = MacroVectorRecord(
            period_ticks=period_ticks,
            phase_ticks=phase_ticks,
            repeat=1,
            family=family_id,
            flags=event_flags,
            drive_value=final_value,
            drive_enable=final_enable,
            sample_mask=compare_mask if event_flags & STPG_EVF_COMPARE_POINT else sample_mask,
            expect_value=expect_value,
        )
        if records:
            prev = records[-1]
            same_shape = (
                prev.period_ticks == next_record.period_ticks
                and prev.phase_ticks == next_record.phase_ticks
                and prev.family == next_record.family
                and prev.flags == next_record.flags
                and prev.drive_value == next_record.drive_value
                and prev.drive_enable == next_record.drive_enable
                and prev.sample_mask == next_record.sample_mask
                and prev.expect_value == next_record.expect_value
                and prev.repeat < 0xFFFF
            )
            if same_shape:
                records[-1] = MacroVectorRecord(
                    period_ticks=prev.period_ticks,
                    phase_ticks=prev.phase_ticks,
                    repeat=prev.repeat + 1,
                    family=prev.family,
                    flags=prev.flags,
                    drive_value=prev.drive_value,
                    drive_enable=prev.drive_enable,
                    sample_mask=prev.sample_mask,
                    expect_value=prev.expect_value,
                )
            else:
                records.append(next_record)
        else:
            records.append(next_record)
        previous_value = final_value
        previous_enable = final_enable

    payload = pack_macro_vector(
        records,
        tick_hz=tick_hz,
        active_pins=ir.resolved_pattern.pin_map.active_pins,
    )
    validate_macro_vector(payload, expected_tick_hz=tick_hz)
    return MacroBuildResult(
        pattern_bytes=payload,
        vector_count=len(ir.vectors),
        record_count=len(records),
    )
