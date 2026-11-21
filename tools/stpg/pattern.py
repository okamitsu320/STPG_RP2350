"""Raw Event Mode pattern binary helpers."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import struct
from threading import get_ident
from typing import Iterable, Sequence

from .constants import (
    ERR_BAD_CRC,
    ERR_BAD_MAGIC,
    ERR_RATEVEC_BAD_CRC,
    ERR_RATEVEC_BAD_HEADER,
    ERR_RATEVEC_BAD_OFFSET,
    ERR_RATE_PERIOD_MISMATCH,
    ERR_RATE_TOO_COMPLEX,
    ERR_VECTOR_BAD_RATE_ID,
    ERR_BAD_SIZE,
    ERR_BAD_VERSION,
    ERR_LOOP_UNSUPPORTED,
    ERR_PATTERN_INVALID,
    ERR_UNSUPPORTED,
    STPG_EVF_COMPARE_POINT,
    STPG_EVF_SAMPLE_POINT,
    STPG_ACTIVE_PINS_V01,
    STPG_ACTIVE_PINS_V11,
    STPG_EXEC_EVENT_SIZE,
    STPG_EXEC_EVENT32_SIZE,
    STPG_HEADER_SIZE,
    STPG_MACRO_RECORD_SIZE,
    STPG_MACRO_P2_RECORD_SIZE,
    STPG_MACROVEC_HEADER_SIZE,
    STPG_MACROP2VEC_MAGIC,
    STPG_MACROVEC_MAGIC,
    STPG_MAX_EVENT_COUNT,
    STPG_MAX_CONFIGURABLE_RATES,
    STPG_MAX_LOOP_BLOCKS,
    STPG_MAX_PATTERN_BYTES,
    STPG_MAX_RATES,
    STPG_MAX_SLOTS_PER_RATE,
    STPG_LOOP_BLOCK_HEADER_SIZE,
    STPG_PATTERN_MAGIC,
    STPG_PATTERN_VERSION,
    STPG_PATTERN_VERSION_WIDE_WAIT,
    STPG_PSRAM_PATTERN_BYTES,
    STPG_RATE_DESC_SIZE,
    STPG_RATE_DESC32_SIZE,
    STPG_RATE_SLOT_DRIVE_OP_MASK,
    STPG_RATE_SLOT_SAMPLE,
    STPG_RATE_SLOT_VALID_FLAG_MASK,
    STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4,
    STPG_RATE_SLOT_SIZE,
    STPG_RATE_SLOT32_SIZE,
    STPG_RATEVEC_FLAG_HAS_LOOP,
    STPG_RATEVEC_MAGIC,
    STPG_RATEVEC_HEADER_SIZE,
    STPG_T_MIN_TICKS,
    STPG_VECTOR_FLAG_LOOP_REF,
    STPG_VECTOR_FLAG_EXPLICIT_WAIT_P4_END,
    STPG_VECTOR_VALID_FLAG_MASK,
    STPG_VECTOR_RECORD_SIZE,
)
from .crc32 import crc32

EXEC_EVENT_STRUCT = struct.Struct("<HHIIII")
EXEC_EVENT32_STRUCT = struct.Struct("<IHHIIII")
PATTERN_HEADER_STRUCT = struct.Struct("<IHHIIIIIIII")
RATE_VECTOR_HEADER_STRUCT = struct.Struct("<IHHIIIIIIIIIIIIII")
MACRO_VECTOR_HEADER_STRUCT = struct.Struct("<IHHIIIIIIII")
RATE_DESC_STRUCT = struct.Struct("<HBBI")
RATE_SLOT_STRUCT = struct.Struct("<HHIII")
RATE_DESC32_STRUCT = struct.Struct("<IBBHI")
RATE_SLOT32_STRUCT = struct.Struct("<IHHIII")
VECTOR_RECORD_STRUCT = struct.Struct("<BBHIIII")
MACRO_VECTOR_RECORD_STRUCT = struct.Struct("<HHHBB3s3s3s3s")
MACRO_P2_VECTOR_RECORD_STRUCT = struct.Struct("<HHHBBBB3s3s3s3s3s3s3s3s")
LOOP_BLOCK_HEADER_STRUCT = struct.Struct("<II")

STREAMING_POC_WAIT_OVERHEAD_NO_SAMPLE = 10
STREAMING_POC_WAIT_OVERHEAD_SAMPLE = 11


@dataclass(frozen=True)
class ExecEvent:
    wait_ticks: int
    flags: int = 0
    out_value: int = 0
    out_enable: int = 0
    sample_mask: int = 0
    expect_value: int = 0

    def pack(self) -> bytes:
        return EXEC_EVENT_STRUCT.pack(
            self.wait_ticks,
            self.flags,
            self.out_value,
            self.out_enable,
            self.sample_mask,
            self.expect_value,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "ExecEvent":
        if len(data) != STPG_EXEC_EVENT_SIZE:
            raise ValueError(f"ExecEvent must be {STPG_EXEC_EVENT_SIZE} bytes")
        return cls(*EXEC_EVENT_STRUCT.unpack(data))


@dataclass(frozen=True)
class ExecEvent32:
    """Version-2 raw event with an explicit 32-bit wait field."""

    wait_ticks: int
    flags: int = 0
    out_value: int = 0
    out_enable: int = 0
    sample_mask: int = 0
    expect_value: int = 0

    def pack(self) -> bytes:
        return EXEC_EVENT32_STRUCT.pack(
            self.wait_ticks,
            self.flags,
            0,
            self.out_value,
            self.out_enable,
            self.sample_mask,
            self.expect_value,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "ExecEvent32":
        if len(data) != STPG_EXEC_EVENT32_SIZE:
            raise ValueError(f"ExecEvent32 must be {STPG_EXEC_EVENT32_SIZE} bytes")
        wait_ticks, flags, reserved, out_value, out_enable, sample_mask, expect_value = EXEC_EVENT32_STRUCT.unpack(data)
        if reserved != 0:
            raise ValueError("ExecEvent32 reserved field must be zero")
        return cls(wait_ticks, flags, out_value, out_enable, sample_mask, expect_value)


@dataclass(frozen=True)
class PatternHeader:
    magic: int
    version: int
    header_size: int
    flags: int
    active_pins: int
    tick_hz: int
    event_size: int
    event_count: int
    data_size: int
    data_crc32: int
    header_crc32: int

    def pack(self) -> bytes:
        return PATTERN_HEADER_STRUCT.pack(
            self.magic,
            self.version,
            self.header_size,
            self.flags,
            self.active_pins,
            self.tick_hz,
            self.event_size,
            self.event_count,
            self.data_size,
            self.data_crc32,
            self.header_crc32,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "PatternHeader":
        if len(data) != STPG_HEADER_SIZE:
            raise ValueError(f"PatternHeader must be {STPG_HEADER_SIZE} bytes")
        return cls(*PATTERN_HEADER_STRUCT.unpack(data))


@dataclass(frozen=True)
class RateVectorHeader:
    magic: int
    version: int
    header_size: int
    flags: int
    active_pins: int
    tick_hz: int
    rate_count: int
    slot_count: int
    vector_count: int
    rate_table_offset: int
    slot_table_offset: int
    vector_offset: int
    data_crc32: int
    reserved0: int
    reserved1: int
    reserved2: int
    header_crc32: int

    def pack(self) -> bytes:
        return RATE_VECTOR_HEADER_STRUCT.pack(
            self.magic,
            self.version,
            self.header_size,
            self.flags,
            self.active_pins,
            self.tick_hz,
            self.rate_count,
            self.slot_count,
            self.vector_count,
            self.rate_table_offset,
            self.slot_table_offset,
            self.vector_offset,
            self.data_crc32,
            self.reserved0,
            self.reserved1,
            self.reserved2,
            self.header_crc32,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "RateVectorHeader":
        if len(data) != STPG_RATEVEC_HEADER_SIZE:
            raise ValueError(f"RateVectorHeader must be {STPG_RATEVEC_HEADER_SIZE} bytes")
        return cls(*RATE_VECTOR_HEADER_STRUCT.unpack(data))


@dataclass(frozen=True)
class MacroVectorHeader:
    magic: int
    version: int
    header_size: int
    flags: int
    active_pins: int
    tick_hz: int
    record_size: int
    record_count: int
    data_size: int
    data_crc32: int
    header_crc32: int

    def pack(self) -> bytes:
        return MACRO_VECTOR_HEADER_STRUCT.pack(
            self.magic,
            self.version,
            self.header_size,
            self.flags,
            self.active_pins,
            self.tick_hz,
            self.record_size,
            self.record_count,
            self.data_size,
            self.data_crc32,
            self.header_crc32,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "MacroVectorHeader":
        if len(data) != STPG_MACROVEC_HEADER_SIZE:
            raise ValueError(f"MacroVectorHeader must be {STPG_MACROVEC_HEADER_SIZE} bytes")
        return cls(*MACRO_VECTOR_HEADER_STRUCT.unpack(data))


@dataclass(frozen=True)
class RateDesc:
    period_ticks: int
    slot_count: int
    flags: int
    slot_offset: int

    def pack(self) -> bytes:
        return RATE_DESC_STRUCT.pack(self.period_ticks, self.slot_count, self.flags, self.slot_offset)

    @classmethod
    def unpack(cls, data: bytes) -> "RateDesc":
        if len(data) != STPG_RATE_DESC_SIZE:
            raise ValueError(f"RateDesc must be {STPG_RATE_DESC_SIZE} bytes")
        return cls(*RATE_DESC_STRUCT.unpack(data))


@dataclass(frozen=True)
class RateSlot:
    time_ticks: int
    flags: int
    drive_mask: int
    dir_mask: int
    sample_mask: int

    def pack(self) -> bytes:
        return RATE_SLOT_STRUCT.pack(
            self.time_ticks,
            self.flags,
            self.drive_mask,
            self.dir_mask,
            self.sample_mask,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "RateSlot":
        if len(data) != STPG_RATE_SLOT_SIZE:
            raise ValueError(f"RateSlot must be {STPG_RATE_SLOT_SIZE} bytes")
        return cls(*RATE_SLOT_STRUCT.unpack(data))


@dataclass(frozen=True)
class RateDesc32:
    """Version-2 rate descriptor; the v1 descriptor remains unchanged."""

    period_ticks: int
    slot_count: int
    flags: int
    slot_offset: int
    reserved: int = 0

    def pack(self) -> bytes:
        return RATE_DESC32_STRUCT.pack(
            self.period_ticks,
            self.slot_count,
            self.flags,
            self.reserved,
            self.slot_offset,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "RateDesc32":
        if len(data) != STPG_RATE_DESC32_SIZE:
            raise ValueError(f"RateDesc32 must be {STPG_RATE_DESC32_SIZE} bytes")
        period_ticks, slot_count, flags, reserved, slot_offset = RATE_DESC32_STRUCT.unpack(data)
        if reserved != 0:
            raise ValueError("RateDesc32 reserved field must be zero")
        return cls(period_ticks, slot_count, flags, slot_offset, reserved)


@dataclass(frozen=True)
class RateSlot32:
    """Version-2 rate slot; time_ticks is intentionally uint32 on the wire."""

    time_ticks: int
    flags: int
    drive_mask: int
    dir_mask: int
    sample_mask: int
    reserved: int = 0

    def pack(self) -> bytes:
        return RATE_SLOT32_STRUCT.pack(
            self.time_ticks,
            self.flags,
            self.reserved,
            self.drive_mask,
            self.dir_mask,
            self.sample_mask,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "RateSlot32":
        if len(data) != STPG_RATE_SLOT32_SIZE:
            raise ValueError(f"RateSlot32 must be {STPG_RATE_SLOT32_SIZE} bytes")
        time_ticks, flags, reserved, drive_mask, dir_mask, sample_mask = RATE_SLOT32_STRUCT.unpack(data)
        if reserved != 0:
            raise ValueError("RateSlot32 reserved field must be zero")
        return cls(time_ticks, flags, drive_mask, dir_mask, sample_mask, reserved)


@dataclass(frozen=True)
class VectorRecord:
    rate_id: int
    flags: int
    repeat: int
    drive_value: int
    drive_enable: int
    expect_value: int
    expect_mask: int

    def pack(self) -> bytes:
        return VECTOR_RECORD_STRUCT.pack(
            self.rate_id,
            self.flags,
            self.repeat,
            self.drive_value,
            self.drive_enable,
            self.expect_value,
            self.expect_mask,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "VectorRecord":
        if len(data) != STPG_VECTOR_RECORD_SIZE:
            raise ValueError(f"VectorRecord must be {STPG_VECTOR_RECORD_SIZE} bytes")
        return cls(*VECTOR_RECORD_STRUCT.unpack(data))


def _pack_u24(value: int) -> bytes:
    if value < 0 or value > 0xFFFFFF:
        raise ValueError("24-bit field out of range")
    return value.to_bytes(3, "little")


def _unpack_u24(data: bytes) -> int:
    if len(data) != 3:
        raise ValueError("24-bit field must be exactly 3 bytes")
    return int.from_bytes(data, "little")


@dataclass(frozen=True)
class MacroVectorRecord:
    period_ticks: int
    phase_ticks: int
    repeat: int
    family: int
    flags: int
    drive_value: int
    drive_enable: int
    sample_mask: int
    expect_value: int

    def pack(self) -> bytes:
        return MACRO_VECTOR_RECORD_STRUCT.pack(
            self.period_ticks,
            self.phase_ticks,
            self.repeat,
            self.family,
            self.flags,
            _pack_u24(self.drive_value),
            _pack_u24(self.drive_enable),
            _pack_u24(self.sample_mask),
            _pack_u24(self.expect_value),
        )

    @classmethod
    def unpack(cls, data: bytes) -> "MacroVectorRecord":
        if len(data) != STPG_MACRO_RECORD_SIZE:
            raise ValueError(f"MacroVectorRecord must be {STPG_MACRO_RECORD_SIZE} bytes")
        (
            period_ticks,
            phase_ticks,
            repeat,
            family,
            flags,
            drive_value,
            drive_enable,
            sample_mask,
            expect_value,
        ) = MACRO_VECTOR_RECORD_STRUCT.unpack(data)
        return cls(
            period_ticks=period_ticks,
            phase_ticks=phase_ticks,
            repeat=repeat,
            family=family,
            flags=flags,
            drive_value=_unpack_u24(drive_value),
            drive_enable=_unpack_u24(drive_enable),
            sample_mask=_unpack_u24(sample_mask),
            expect_value=_unpack_u24(expect_value),
        )


@dataclass(frozen=True)
class MacroVectorRecordP2:
    period_ticks: int
    repeat: int
    source_family: int
    exec_family: int
    flags: int
    wait_first_ticks: int
    wait_second_ticks: int
    drive0_value: int
    drive0_enable: int
    drive1_value: int
    drive1_enable: int
    sample0_mask: int
    sample1_mask: int
    expect0_value: int
    expect1_value: int

    def pack(self) -> bytes:
        return MACRO_P2_VECTOR_RECORD_STRUCT.pack(
            self.period_ticks,
            self.repeat,
            self.wait_first_ticks,
            self.source_family,
            self.exec_family,
            self.flags,
            self.wait_second_ticks,
            _pack_u24(self.drive0_value),
            _pack_u24(self.drive0_enable),
            _pack_u24(self.drive1_value),
            _pack_u24(self.drive1_enable),
            _pack_u24(self.sample0_mask),
            _pack_u24(self.sample1_mask),
            _pack_u24(self.expect0_value),
            _pack_u24(self.expect1_value),
        )

    @classmethod
    def unpack(cls, data: bytes) -> "MacroVectorRecordP2":
        if len(data) != STPG_MACRO_P2_RECORD_SIZE:
            raise ValueError(f"MacroVectorRecordP2 must be {STPG_MACRO_P2_RECORD_SIZE} bytes")
        (
            period_ticks,
            repeat,
            wait_first_ticks,
            source_family,
            exec_family,
            flags,
            wait_second_ticks,
            drive0_value,
            drive0_enable,
            drive1_value,
            drive1_enable,
            sample0_mask,
            sample1_mask,
            expect0_value,
            expect1_value,
        ) = MACRO_P2_VECTOR_RECORD_STRUCT.unpack(data)
        return cls(
            period_ticks=period_ticks,
            repeat=repeat,
            source_family=source_family,
            exec_family=exec_family,
            flags=flags,
            wait_first_ticks=wait_first_ticks,
            wait_second_ticks=wait_second_ticks,
            drive0_value=_unpack_u24(drive0_value),
            drive0_enable=_unpack_u24(drive0_enable),
            drive1_value=_unpack_u24(drive1_value),
            drive1_enable=_unpack_u24(drive1_enable),
            sample0_mask=_unpack_u24(sample0_mask),
            sample1_mask=_unpack_u24(sample1_mask),
            expect0_value=_unpack_u24(expect0_value),
            expect1_value=_unpack_u24(expect1_value),
        )


@dataclass(frozen=True)
class LoopRefRecord:
    loop_id: int
    flags: int = STPG_VECTOR_FLAG_LOOP_REF

    def pack(self) -> bytes:
        return VECTOR_RECORD_STRUCT.pack(
            0,
            self.flags,
            0,
            self.loop_id,
            0,
            0,
            0,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "LoopRefRecord":
        if len(data) != STPG_VECTOR_RECORD_SIZE:
            raise ValueError(f"LoopRefRecord must be {STPG_VECTOR_RECORD_SIZE} bytes")
        rate_id, flags, repeat, loop_id, reserved2, reserved3, reserved4 = VECTOR_RECORD_STRUCT.unpack(data)
        if rate_id != 0 or repeat != 0 or reserved2 != 0 or reserved3 != 0 or reserved4 != 0:
            raise ValueError("invalid LoopRefRecord payload")
        return cls(loop_id=loop_id, flags=flags)


@dataclass(frozen=True)
class LoopBlock:
    loop_count: int
    body_records: tuple[VectorRecord, ...]

    def pack(self) -> bytes:
        return LOOP_BLOCK_HEADER_STRUCT.pack(self.loop_count, len(self.body_records)) + b"".join(
            record.pack() for record in self.body_records
        )


RateVectorEntry = VectorRecord | LoopRefRecord


class PatternValidationError(ValueError):
    def __init__(self, message: str, error_code: int):
        super().__init__(message)
        self.error_code = error_code


def build_header(events: Iterable[ExecEvent], tick_hz: int, active_pins: int = STPG_ACTIVE_PINS_V01) -> PatternHeader:
    events_list = list(events)
    if not 1 <= len(events_list) <= STPG_MAX_EVENT_COUNT:
        raise PatternValidationError("event_count out of range", ERR_PATTERN_INVALID)
    data = b"".join(event.pack() for event in events_list)
    data_crc = crc32(data)
    pre_crc = PATTERN_HEADER_STRUCT.pack(
        STPG_PATTERN_MAGIC,
        STPG_PATTERN_VERSION,
        STPG_HEADER_SIZE,
        0,
        active_pins,
        tick_hz,
        STPG_EXEC_EVENT_SIZE,
        len(events_list),
        len(data),
        data_crc,
        0,
    )[:36]
    return PatternHeader(
        STPG_PATTERN_MAGIC,
        STPG_PATTERN_VERSION,
        STPG_HEADER_SIZE,
        0,
        active_pins,
        tick_hz,
        STPG_EXEC_EVENT_SIZE,
        len(events_list),
        len(data),
        data_crc,
        crc32(pre_crc),
    )


def pack_pattern(events: Iterable[ExecEvent], tick_hz: int, active_pins: int = STPG_ACTIVE_PINS_V01) -> bytes:
    events_list = list(events)
    header = build_header(events_list, tick_hz, active_pins)
    pattern = header.pack() + b"".join(event.pack() for event in events_list)
    if len(pattern) > STPG_MAX_PATTERN_BYTES:
        raise PatternValidationError("pattern exceeds Stage A size", ERR_BAD_SIZE)
    return pattern


def build_wide_header(events: Iterable[ExecEvent32], tick_hz: int, active_pins: int = STPG_ACTIVE_PINS_V01) -> PatternHeader:
    events_list = list(events)
    if not 1 <= len(events_list) <= STPG_MAX_EVENT_COUNT:
        raise PatternValidationError("event_count out of range", ERR_PATTERN_INVALID)
    data = b"".join(event.pack() for event in events_list)
    data_crc = crc32(data)
    pre_crc = PATTERN_HEADER_STRUCT.pack(
        STPG_PATTERN_MAGIC,
        STPG_PATTERN_VERSION_WIDE_WAIT,
        STPG_HEADER_SIZE,
        0,
        active_pins,
        tick_hz,
        STPG_EXEC_EVENT32_SIZE,
        len(events_list),
        len(data),
        data_crc,
        0,
    )[:36]
    return PatternHeader(
        STPG_PATTERN_MAGIC,
        STPG_PATTERN_VERSION_WIDE_WAIT,
        STPG_HEADER_SIZE,
        0,
        active_pins,
        tick_hz,
        STPG_EXEC_EVENT32_SIZE,
        len(events_list),
        len(data),
        data_crc,
        crc32(pre_crc),
    )


def pack_wide_pattern(events: Iterable[ExecEvent32], tick_hz: int, active_pins: int = STPG_ACTIVE_PINS_V01) -> bytes:
    events_list = list(events)
    header = build_wide_header(events_list, tick_hz, active_pins)
    pattern = header.pack() + b"".join(event.pack() for event in events_list)
    if len(pattern) > STPG_MAX_PATTERN_BYTES:
        raise PatternValidationError("pattern exceeds Stage A size", ERR_BAD_SIZE)
    return pattern


def pack_rate_vector(
    rates: Iterable[RateDesc],
    slots: Iterable[RateSlot],
    vectors: Iterable[RateVectorEntry],
    *,
    tick_hz: int,
    active_pins: int = STPG_ACTIVE_PINS_V01,
    loop_blocks: Iterable[LoopBlock] = (),
    extra_header_flags: int = 0,
    max_rates: int | None = None,
) -> bytes:
    rate_list = list(rates)
    slot_list = list(slots)
    vector_list = list(vectors)
    loop_block_list = list(loop_blocks)
    rate_limit = resolve_max_rates(max_rates)
    if not 1 <= len(rate_list) <= rate_limit:
        raise PatternValidationError("rate_count out of range", ERR_RATEVEC_BAD_HEADER)
    if len(slot_list) == 0 or len(vector_list) == 0:
        raise PatternValidationError("slot_count and vector_count must be non-zero", ERR_RATEVEC_BAD_HEADER)
    if len(loop_block_list) > STPG_MAX_LOOP_BLOCKS:
        raise PatternValidationError(
            f"loop block count {len(loop_block_list)} exceeds STPG_MAX_LOOP_BLOCKS={STPG_MAX_LOOP_BLOCKS}",
            ERR_LOOP_UNSUPPORTED,
        )
    for loop_index, loop_block in enumerate(loop_block_list):
        if not 1 <= loop_block.loop_count <= 0xFFFFFFFF:
            raise PatternValidationError(
                f"loop block {loop_index} loop_count must be in uint32 range",
                ERR_LOOP_UNSUPPORTED,
            )
        if len(loop_block.body_records) == 0:
            raise PatternValidationError(f"loop block {loop_index} body_records must be non-empty", ERR_LOOP_UNSUPPORTED)
        if len(loop_block.body_records) > 0xFFFFFFFF:
            raise PatternValidationError(
                f"loop block {loop_index} body record count exceeds uint32 ABI",
                ERR_LOOP_UNSUPPORTED,
            )
        for body_index, record in enumerate(loop_block.body_records):
            if not isinstance(record, VectorRecord):
                raise PatternValidationError(
                    f"loop block {loop_index} body record {body_index} must be VectorRecord",
                    ERR_LOOP_UNSUPPORTED,
                )
    for entry_index, entry in enumerate(vector_list):
        if isinstance(entry, LoopRefRecord) and not 0 <= entry.loop_id <= 0xFFFFFFFF:
            raise PatternValidationError(
                f"vector entry {entry_index} loop_id exceeds uint32 ABI",
                ERR_LOOP_UNSUPPORTED,
            )

    rate_blob = b"".join(rate.pack() for rate in rate_list)
    slot_blob = b"".join(slot.pack() for slot in slot_list)
    loop_blob = b"".join(loop_block.pack() for loop_block in loop_block_list)
    vector_blob = b"".join(vector.pack() for vector in vector_list)
    rate_table_offset = STPG_RATEVEC_HEADER_SIZE
    slot_table_offset = rate_table_offset + len(rate_blob)
    loop_block_offset = slot_table_offset + len(slot_blob) if loop_blob else 0
    vector_offset = (loop_block_offset + len(loop_blob)) if loop_blob else (slot_table_offset + len(slot_blob))
    payload = rate_blob + slot_blob + loop_blob + vector_blob
    if extra_header_flags & ~STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4:
        raise PatternValidationError("unsupported extra rate-vector header flags", ERR_RATEVEC_BAD_HEADER)
    header_flags = (STPG_RATEVEC_FLAG_HAS_LOOP if loop_blob else 0) | extra_header_flags
    pre_crc = RATE_VECTOR_HEADER_STRUCT.pack(
        STPG_RATEVEC_MAGIC,
        STPG_PATTERN_VERSION,
        STPG_RATEVEC_HEADER_SIZE,
        header_flags,
        active_pins,
        tick_hz,
        len(rate_list),
        len(slot_list),
        len(vector_list),
        rate_table_offset,
        slot_table_offset,
        vector_offset,
        crc32(payload),
        loop_block_offset,
        0,
        0,
        0,
    )[:60]
    header = RateVectorHeader(
        STPG_RATEVEC_MAGIC,
        STPG_PATTERN_VERSION,
        STPG_RATEVEC_HEADER_SIZE,
        header_flags,
        active_pins,
        tick_hz,
        len(rate_list),
        len(slot_list),
        len(vector_list),
        rate_table_offset,
        slot_table_offset,
        vector_offset,
        crc32(payload),
        loop_block_offset,
        0,
        0,
        crc32(pre_crc),
    )
    return header.pack() + payload


def pack_wide_rate_vector(
    rates: Iterable[RateDesc32],
    slots: Iterable[RateSlot32],
    vectors: Iterable[RateVectorEntry],
    *,
    tick_hz: int,
    active_pins: int = STPG_ACTIVE_PINS_V01,
    loop_blocks: Iterable[LoopBlock] = (),
    extra_header_flags: int = 0,
    max_rates: int | None = None,
) -> bytes:
    """Pack the versioned uint32 rate-vector ABI.

    Vector and loop records intentionally retain their v1 layout. Only the
    rate descriptor/slot tables change size and width in version 2.
    """

    rate_list = list(rates)
    slot_list = list(slots)
    vector_list = list(vectors)
    loop_block_list = list(loop_blocks)
    rate_limit = resolve_max_rates(max_rates)
    if not 1 <= len(rate_list) <= rate_limit:
        raise PatternValidationError("rate_count out of range", ERR_RATEVEC_BAD_HEADER)
    if not slot_list or not vector_list:
        raise PatternValidationError("slot_count and vector_count must be non-zero", ERR_RATEVEC_BAD_HEADER)
    if len(loop_block_list) > STPG_MAX_LOOP_BLOCKS:
        raise PatternValidationError(
            f"loop block count {len(loop_block_list)} exceeds STPG_MAX_LOOP_BLOCKS={STPG_MAX_LOOP_BLOCKS}",
            ERR_LOOP_UNSUPPORTED,
        )
    for index, rate in enumerate(rate_list):
        if not isinstance(rate, RateDesc32) or rate.reserved != 0:
            raise PatternValidationError(f"rate {index} must be RateDesc32 with zero reserved", ERR_RATEVEC_BAD_HEADER)
        if not 0 <= rate.period_ticks <= 0xFFFFFFFF or not 0 <= rate.slot_offset <= 0xFFFFFFFF:
            raise PatternValidationError(f"rate {index} exceeds uint32 ABI", ERR_RATEVEC_BAD_HEADER)
    for index, slot in enumerate(slot_list):
        if not isinstance(slot, RateSlot32) or slot.reserved != 0:
            raise PatternValidationError(f"slot {index} must be RateSlot32 with zero reserved", ERR_RATEVEC_BAD_HEADER)
        for value in (slot.time_ticks, slot.drive_mask, slot.dir_mask, slot.sample_mask):
            if not 0 <= value <= 0xFFFFFFFF:
                raise PatternValidationError(f"slot {index} exceeds uint32 ABI", ERR_RATEVEC_BAD_HEADER)
    for loop_index, loop_block in enumerate(loop_block_list):
        if not 1 <= loop_block.loop_count <= 0xFFFFFFFF or not loop_block.body_records:
            raise PatternValidationError(f"loop block {loop_index} must be non-empty and uint32-safe", ERR_LOOP_UNSUPPORTED)
        if len(loop_block.body_records) > 0xFFFFFFFF:
            raise PatternValidationError(
                f"loop block {loop_index} body record count exceeds uint32 ABI",
                ERR_LOOP_UNSUPPORTED,
            )
        if any(not isinstance(record, VectorRecord) for record in loop_block.body_records):
            raise PatternValidationError(f"loop block {loop_index} body record must be VectorRecord", ERR_LOOP_UNSUPPORTED)
    for entry_index, entry in enumerate(vector_list):
        if isinstance(entry, LoopRefRecord) and not 0 <= entry.loop_id <= 0xFFFFFFFF:
            raise PatternValidationError(
                f"vector entry {entry_index} loop_id exceeds uint32 ABI",
                ERR_LOOP_UNSUPPORTED,
            )

    rate_blob = b"".join(rate.pack() for rate in rate_list)
    slot_blob = b"".join(slot.pack() for slot in slot_list)
    loop_blob = b"".join(loop_block.pack() for loop_block in loop_block_list)
    vector_blob = b"".join(vector.pack() for vector in vector_list)
    rate_table_offset = STPG_RATEVEC_HEADER_SIZE
    slot_table_offset = rate_table_offset + len(rate_blob)
    loop_block_offset = slot_table_offset + len(slot_blob) if loop_blob else 0
    vector_offset = (loop_block_offset + len(loop_blob)) if loop_blob else (slot_table_offset + len(slot_blob))
    payload = rate_blob + slot_blob + loop_blob + vector_blob
    if extra_header_flags & ~STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4:
        raise PatternValidationError("unsupported extra rate-vector header flags", ERR_RATEVEC_BAD_HEADER)
    header_flags = (STPG_RATEVEC_FLAG_HAS_LOOP if loop_blob else 0) | extra_header_flags
    data_crc32 = crc32(payload)
    pre_crc = RATE_VECTOR_HEADER_STRUCT.pack(
        STPG_RATEVEC_MAGIC,
        STPG_PATTERN_VERSION_WIDE_WAIT,
        STPG_RATEVEC_HEADER_SIZE,
        header_flags,
        active_pins,
        tick_hz,
        len(rate_list),
        len(slot_list),
        len(vector_list),
        rate_table_offset,
        slot_table_offset,
        vector_offset,
        data_crc32,
        loop_block_offset,
        0,
        0,
        0,
    )[:60]
    header = RateVectorHeader(
        STPG_RATEVEC_MAGIC,
        STPG_PATTERN_VERSION_WIDE_WAIT,
        STPG_RATEVEC_HEADER_SIZE,
        header_flags,
        active_pins,
        tick_hz,
        len(rate_list),
        len(slot_list),
        len(vector_list),
        rate_table_offset,
        slot_table_offset,
        vector_offset,
        data_crc32,
        loop_block_offset,
        0,
        0,
        crc32(pre_crc),
    )
    pattern = header.pack() + payload
    if len(pattern) > STPG_PSRAM_PATTERN_BYTES:
        raise PatternValidationError("wide rate vector exceeds PSRAM pattern region", ERR_BAD_SIZE)
    return pattern


def pack_macro_vector(
    records: Iterable[MacroVectorRecord],
    *,
    tick_hz: int,
    active_pins: int = STPG_ACTIVE_PINS_V01,
) -> bytes:
    record_list = list(records)
    if len(record_list) == 0:
        raise PatternValidationError("record_count must be non-zero", ERR_PATTERN_INVALID)
    payload = b"".join(record.pack() for record in record_list)
    data_crc32 = crc32(payload)
    pre_crc = MACRO_VECTOR_HEADER_STRUCT.pack(
        STPG_MACROVEC_MAGIC,
        STPG_PATTERN_VERSION,
        STPG_MACROVEC_HEADER_SIZE,
        0,
        active_pins,
        tick_hz,
        STPG_MACRO_RECORD_SIZE,
        len(record_list),
        len(payload),
        data_crc32,
        0,
    )[:36]
    header = MacroVectorHeader(
        STPG_MACROVEC_MAGIC,
        STPG_PATTERN_VERSION,
        STPG_MACROVEC_HEADER_SIZE,
        0,
        active_pins,
        tick_hz,
        STPG_MACRO_RECORD_SIZE,
        len(record_list),
        len(payload),
        data_crc32,
        crc32(pre_crc),
    )
    pattern = header.pack() + payload
    if len(pattern) > STPG_MAX_PATTERN_BYTES:
        raise PatternValidationError("macro-stream pattern exceeds Stage A size", ERR_BAD_SIZE)
    return pattern


def pack_macro_p2_vector(
    records: Iterable[MacroVectorRecordP2],
    *,
    tick_hz: int,
    active_pins: int = STPG_ACTIVE_PINS_V01,
) -> bytes:
    record_list = list(records)
    if len(record_list) == 0:
        raise PatternValidationError("record_count must be non-zero", ERR_PATTERN_INVALID)
    payload = b"".join(record.pack() for record in record_list)
    data_crc32 = crc32(payload)
    pre_crc = MACRO_VECTOR_HEADER_STRUCT.pack(
        STPG_MACROP2VEC_MAGIC,
        STPG_PATTERN_VERSION,
        STPG_MACROVEC_HEADER_SIZE,
        0,
        active_pins,
        tick_hz,
        STPG_MACRO_P2_RECORD_SIZE,
        len(record_list),
        len(payload),
        data_crc32,
        0,
    )[:36]
    header = MacroVectorHeader(
        STPG_MACROP2VEC_MAGIC,
        STPG_PATTERN_VERSION,
        STPG_MACROVEC_HEADER_SIZE,
        0,
        active_pins,
        tick_hz,
        STPG_MACRO_P2_RECORD_SIZE,
        len(record_list),
        len(payload),
        data_crc32,
        crc32(pre_crc),
    )
    pattern = header.pack() + payload
    if len(pattern) > STPG_MAX_PATTERN_BYTES:
        raise PatternValidationError("macro-p2 pattern exceeds Stage A size", ERR_BAD_SIZE)
    return pattern


def unpack_events(data: bytes, event_count: int) -> list[ExecEvent]:
    expected_size = event_count * STPG_EXEC_EVENT_SIZE
    if len(data) != expected_size:
        raise PatternValidationError("event data size mismatch", ERR_BAD_SIZE)
    return [
        ExecEvent.unpack(data[i : i + STPG_EXEC_EVENT_SIZE])
        for i in range(0, len(data), STPG_EXEC_EVENT_SIZE)
    ]


def unpack_pattern(data: bytes) -> tuple[PatternHeader, list[ExecEvent] | list[ExecEvent32]]:
    validate_pattern(data)
    header = PatternHeader.unpack(data[:STPG_HEADER_SIZE])
    if header.version == STPG_PATTERN_VERSION_WIDE_WAIT:
        return header, unpack_wide_events(data[STPG_HEADER_SIZE:], header.event_count)
    return header, unpack_events(data[STPG_HEADER_SIZE:], header.event_count)


def unpack_wide_events(data: bytes, event_count: int) -> list[ExecEvent32]:
    expected_size = event_count * STPG_EXEC_EVENT32_SIZE
    if len(data) != expected_size:
        raise PatternValidationError("wide event data size mismatch", ERR_BAD_SIZE)
    return [
        ExecEvent32.unpack(data[i : i + STPG_EXEC_EVENT32_SIZE])
        for i in range(0, len(data), STPG_EXEC_EVENT32_SIZE)
    ]


def unpack_wide_pattern(data: bytes) -> tuple[PatternHeader, list[ExecEvent32]]:
    validate_pattern(data)
    header = PatternHeader.unpack(data[:STPG_HEADER_SIZE])
    if header.version != STPG_PATTERN_VERSION_WIDE_WAIT:
        raise PatternValidationError("not a wide-wait pattern", ERR_BAD_VERSION)
    return header, unpack_wide_events(data[STPG_HEADER_SIZE:], header.event_count)


def unpack_macro_vector(data: bytes) -> tuple[MacroVectorHeader, list[MacroVectorRecord]]:
    validate_pattern(data)
    header = MacroVectorHeader.unpack(data[:STPG_MACROVEC_HEADER_SIZE])
    payload = data[STPG_MACROVEC_HEADER_SIZE:]
    records = [
        MacroVectorRecord.unpack(payload[i : i + STPG_MACRO_RECORD_SIZE])
        for i in range(0, len(payload), STPG_MACRO_RECORD_SIZE)
    ]
    return header, records


def unpack_macro_p2_vector(data: bytes) -> tuple[MacroVectorHeader, list[MacroVectorRecordP2]]:
    validate_macro_p2_vector(data)
    header = MacroVectorHeader.unpack(data[:STPG_MACROVEC_HEADER_SIZE])
    payload = data[STPG_MACROVEC_HEADER_SIZE:]
    records = [
        MacroVectorRecordP2.unpack(payload[i : i + STPG_MACRO_P2_RECORD_SIZE])
        for i in range(0, len(payload), STPG_MACRO_P2_RECORD_SIZE)
    ]
    return header, records


def emitted_macro_p2_event_count_estimate(records: Sequence[MacroVectorRecordP2]) -> int:
    total = 0
    for record in records:
        events_per_repeat = 2 if record.exec_family in (1, 2, 0x81) else 1
        total += record.repeat * events_per_repeat
    return total


def sample_macro_p2_event_count_estimate(records: Sequence[MacroVectorRecordP2]) -> int:
    total = 0
    for record in records:
        samples_per_repeat = 0
        if record.exec_family in (1, 2, 0x81, 4, 5):
            if record.sample0_mask != 0:
                samples_per_repeat += 1
            if record.exec_family in (1, 2, 0x81) and record.sample1_mask != 0:
                samples_per_repeat += 1
        total += record.repeat * samples_per_repeat
    return total


def unpack_rate_vector(data: bytes) -> tuple[RateVectorHeader, list[RateDesc], list[RateSlot], list[RateVectorEntry]]:
    validate_rate_vector(data)
    header, rates, slots, vectors, _ = unpack_rate_vector_full(data)
    return header, rates, slots, vectors


def validate_pattern(
    data: bytes,
    *,
    expected_tick_hz: int | None = None,
    max_rates: int | None = None,
) -> None:
    if len(data) < 4:
        raise PatternValidationError("pattern too small", ERR_BAD_SIZE)
    magic = struct.unpack("<I", data[:4])[0]
    if magic == STPG_RATEVEC_MAGIC:
        validate_rate_vector(data, expected_tick_hz=expected_tick_hz, max_rates=max_rates)
        return
    if magic == STPG_MACROVEC_MAGIC:
        validate_macro_vector(data, expected_tick_hz=expected_tick_hz)
        return
    if magic == STPG_MACROP2VEC_MAGIC:
        validate_macro_p2_vector(data, expected_tick_hz=expected_tick_hz)
        return
    if magic != STPG_PATTERN_MAGIC:
        raise PatternValidationError("bad pattern magic", ERR_BAD_MAGIC)
    if len(data) < STPG_HEADER_SIZE:
        raise PatternValidationError("missing PatternHeader", ERR_BAD_SIZE)
    if len(data) > STPG_MAX_PATTERN_BYTES:
        raise PatternValidationError("pattern exceeds Stage A size", ERR_BAD_SIZE)

    header = PatternHeader.unpack(data[:STPG_HEADER_SIZE])
    if header.version not in (STPG_PATTERN_VERSION, STPG_PATTERN_VERSION_WIDE_WAIT):
        raise PatternValidationError("bad pattern version", ERR_BAD_VERSION)
    if header.header_size != STPG_HEADER_SIZE:
        raise PatternValidationError("bad header_size", ERR_PATTERN_INVALID)
    if crc32(data[:36]) != header.header_crc32:
        raise PatternValidationError("bad header_crc32", ERR_BAD_CRC)
    if header.flags != 0:
        raise PatternValidationError("PatternHeader.flags is unsupported", ERR_UNSUPPORTED)
    if header.active_pins not in (STPG_ACTIVE_PINS_V01, STPG_ACTIVE_PINS_V11):
        raise PatternValidationError("active_pins is unsupported", ERR_UNSUPPORTED)
    pin_mask = 0xFFFFFF if header.active_pins == STPG_ACTIVE_PINS_V11 else 0xFFFF
    if expected_tick_hz is not None and header.tick_hz != expected_tick_hz:
        raise PatternValidationError("tick_hz is unsupported", ERR_UNSUPPORTED)
    expected_event_size = STPG_EXEC_EVENT_SIZE if header.version == STPG_PATTERN_VERSION else STPG_EXEC_EVENT32_SIZE
    if header.event_size != expected_event_size:
        raise PatternValidationError("bad event_size", ERR_PATTERN_INVALID)
    if not 1 <= header.event_count <= STPG_MAX_EVENT_COUNT:
        raise PatternValidationError("event_count out of range", ERR_PATTERN_INVALID)
    if header.data_size != header.event_size * header.event_count:
        raise PatternValidationError("data_size mismatch", ERR_PATTERN_INVALID)
    if header.data_size + STPG_HEADER_SIZE != len(data):
        raise PatternValidationError("total size mismatch", ERR_BAD_SIZE)
    event_data = data[STPG_HEADER_SIZE:]
    if crc32(event_data) != header.data_crc32:
        raise PatternValidationError("bad data_crc32", ERR_BAD_CRC)

    events = unpack_events(event_data, header.event_count) if header.version == STPG_PATTERN_VERSION else unpack_wide_events(event_data, header.event_count)
    for index, event in enumerate(events):
        if event.flags & ~(STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT):
            raise PatternValidationError(f"event {index} flags unsupported", ERR_UNSUPPORTED)
        if event.sample_mask & ~pin_mask:
            raise PatternValidationError(f"event {index} sample_mask unsupported", ERR_UNSUPPORTED)
        if event.expect_value & ~pin_mask:
            raise PatternValidationError(f"event {index} expect_value unsupported", ERR_UNSUPPORTED)


def validate_rate_vector(
    data: bytes,
    *,
    expected_tick_hz: int | None = None,
    max_rates: int | None = None,
) -> None:
    if len(data) < STPG_RATEVEC_HEADER_SIZE:
        raise PatternValidationError("missing RateVectorHeader", ERR_BAD_SIZE)
    if len(data) > STPG_PSRAM_PATTERN_BYTES:
        raise PatternValidationError("rate vector exceeds PSRAM pattern region", ERR_BAD_SIZE)
    header = RateVectorHeader.unpack(data[:STPG_RATEVEC_HEADER_SIZE])
    if header.magic != STPG_RATEVEC_MAGIC:
        raise PatternValidationError("bad rate vector magic", ERR_BAD_MAGIC)
    if header.version not in (STPG_PATTERN_VERSION, STPG_PATTERN_VERSION_WIDE_WAIT):
        raise PatternValidationError("bad rate vector version", ERR_BAD_VERSION)
    if header.header_size != STPG_RATEVEC_HEADER_SIZE:
        raise PatternValidationError("bad rate vector header_size", ERR_RATEVEC_BAD_HEADER)
    if crc32(data[:60]) != header.header_crc32:
        raise PatternValidationError("bad rate vector header_crc32", ERR_RATEVEC_BAD_CRC)
    allowed_flags = STPG_RATEVEC_FLAG_HAS_LOOP | STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4
    if header.flags & ~allowed_flags:
        raise PatternValidationError("RateVectorHeader.flags is unsupported", ERR_UNSUPPORTED)
    if header.active_pins not in (STPG_ACTIVE_PINS_V01, STPG_ACTIVE_PINS_V11):
        raise PatternValidationError("active_pins is unsupported", ERR_UNSUPPORTED)
    pin_mask = 0xFFFFFF if header.active_pins == STPG_ACTIVE_PINS_V11 else 0xFFFF
    if expected_tick_hz is not None and header.tick_hz != expected_tick_hz:
        raise PatternValidationError("tick_hz is unsupported", ERR_UNSUPPORTED)
    rate_limit = resolve_max_rates(max_rates)
    if not 1 <= header.rate_count <= rate_limit:
        raise PatternValidationError("rate_count out of range", ERR_RATEVEC_BAD_HEADER)
    if header.slot_count < 1 or header.vector_count < 1:
        raise PatternValidationError("slot_count/vector_count out of range", ERR_RATEVEC_BAD_HEADER)
    if (header.rate_table_offset & 3) or (header.slot_table_offset & 3) or (header.vector_offset & 3):
        raise PatternValidationError("table offset must be 4-byte aligned", ERR_RATEVEC_BAD_OFFSET)
    if header.reserved1 != 0 or header.reserved2 != 0:
        raise PatternValidationError("delta/reserved sections are unsupported", ERR_UNSUPPORTED)
    has_loop = (header.flags & STPG_RATEVEC_FLAG_HAS_LOOP) != 0
    if has_loop != (header.reserved0 != 0):
        raise PatternValidationError("loop presence flag and loop_block_offset mismatch", ERR_RATEVEC_BAD_HEADER)
    if header.rate_table_offset < STPG_RATEVEC_HEADER_SIZE:
        raise PatternValidationError("rate_table_offset overlaps header", ERR_RATEVEC_BAD_OFFSET)
    rate_desc_size = STPG_RATE_DESC32_SIZE if header.version == STPG_PATTERN_VERSION_WIDE_WAIT else STPG_RATE_DESC_SIZE
    rate_slot_size = STPG_RATE_SLOT32_SIZE if header.version == STPG_PATTERN_VERSION_WIDE_WAIT else STPG_RATE_SLOT_SIZE
    rate_end = header.rate_table_offset + header.rate_count * rate_desc_size
    slot_end = header.slot_table_offset + header.slot_count * rate_slot_size
    vector_end = header.vector_offset + header.vector_count * STPG_VECTOR_RECORD_SIZE
    loop_section_start = header.reserved0 if header.reserved0 != 0 else header.vector_offset
    if header.reserved0 != 0 and (header.reserved0 & 3):
        raise PatternValidationError("loop_block_offset must be 4-byte aligned", ERR_RATEVEC_BAD_OFFSET)
    if not (header.rate_table_offset <= header.slot_table_offset <= loop_section_start <= header.vector_offset):
        raise PatternValidationError("table offsets must be nondecreasing", ERR_RATEVEC_BAD_OFFSET)
    if rate_end > len(data) or slot_end > len(data) or vector_end > len(data):
        raise PatternValidationError("table section out of range", ERR_RATEVEC_BAD_OFFSET)
    if rate_end > header.slot_table_offset or slot_end > loop_section_start:
        raise PatternValidationError("table section overlap", ERR_RATEVEC_BAD_OFFSET)
    payload_crc = crc32(data[header.rate_table_offset:vector_end])
    if payload_crc != header.data_crc32:
        raise PatternValidationError("bad rate vector data_crc32", ERR_RATEVEC_BAD_CRC)

    try:
        _, rates, slots, vectors, loop_blocks = unpack_rate_vector_full(data)
    except ValueError as exc:
        raise PatternValidationError(str(exc), ERR_RATEVEC_BAD_HEADER) from exc
    for rate_index, rate in enumerate(rates):
        if rate.period_ticks == 0:
            raise PatternValidationError(f"rate {rate_index} period_ticks must be non-zero", ERR_RATE_PERIOD_MISMATCH)
        if not 1 <= rate.slot_count <= STPG_MAX_SLOTS_PER_RATE:
            raise PatternValidationError(f"rate {rate_index} slot_count too large", ERR_RATE_TOO_COMPLEX)
        if rate.flags != 0:
            raise PatternValidationError(f"rate {rate_index} flags unsupported", ERR_RATEVEC_BAD_HEADER)
        if rate.slot_offset + rate.slot_count > header.slot_count:
            raise PatternValidationError(f"rate {rate_index} slot range out of bounds", ERR_RATEVEC_BAD_OFFSET)
        prev_time_ticks = 0
        for slot_index in range(rate.slot_count):
            slot = slots[rate.slot_offset + slot_index]
            drive_op = slot.flags & STPG_RATE_SLOT_DRIVE_OP_MASK
            if drive_op > 4:
                raise PatternValidationError("slot drive_op unsupported", ERR_RATEVEC_BAD_HEADER)
            if slot.flags & ~STPG_RATE_SLOT_VALID_FLAG_MASK:
                raise PatternValidationError("slot reserved flags unsupported", ERR_RATEVEC_BAD_HEADER)
            if slot.drive_mask & ~pin_mask or slot.dir_mask & ~pin_mask or slot.sample_mask & ~pin_mask:
                raise PatternValidationError("slot mask exceeds active_pins", ERR_RATEVEC_BAD_HEADER)
            if slot.time_ticks >= rate.period_ticks:
                raise PatternValidationError("slot time_ticks exceeds period", ERR_RATE_PERIOD_MISMATCH)
            if slot_index > 0 and slot.time_ticks < prev_time_ticks:
                raise PatternValidationError("slot time_ticks must be nondecreasing", ERR_RATE_PERIOD_MISMATCH)
            prev_time_ticks = slot.time_ticks
    for loop_index, loop_block in enumerate(loop_blocks):
        if loop_index >= STPG_MAX_LOOP_BLOCKS:
            raise PatternValidationError(
                f"loop block count exceeds STPG_MAX_LOOP_BLOCKS={STPG_MAX_LOOP_BLOCKS}",
                ERR_LOOP_UNSUPPORTED,
            )
        if loop_block.loop_count < 1:
            raise PatternValidationError(f"loop block {loop_index} loop_count must be >= 1", ERR_LOOP_UNSUPPORTED)
        if len(loop_block.body_records) == 0:
            raise PatternValidationError(f"loop block {loop_index} body_records must be non-empty", ERR_LOOP_UNSUPPORTED)
        for body_index, vector in enumerate(loop_block.body_records):
            _validate_vector_record(vector, header, f"loop block {loop_index} body record {body_index}")
    p4_end_marker_count = 0
    for index, vector in enumerate(vectors):
        if isinstance(vector, LoopRefRecord):
            if vector.flags != STPG_VECTOR_FLAG_LOOP_REF:
                raise PatternValidationError(f"loop ref {index} flags unsupported", ERR_RATEVEC_BAD_HEADER)
            if vector.loop_id >= len(loop_blocks):
                raise PatternValidationError(f"loop ref {index} bad loop_id", ERR_RATEVEC_BAD_OFFSET)
            continue
        _validate_vector_record(vector, header, f"vector {index}")
        if vector.flags == STPG_VECTOR_FLAG_EXPLICIT_WAIT_P4_END:
            p4_end_marker_count += 1
    if (header.flags & STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4) and p4_end_marker_count != 1:
        raise PatternValidationError("P4 header opt-in requires exactly one end marker", ERR_RATEVEC_BAD_HEADER)


def validate_macro_vector(data: bytes, *, expected_tick_hz: int | None = None) -> None:
    if len(data) < STPG_MACROVEC_HEADER_SIZE:
        raise PatternValidationError("missing MacroVectorHeader", ERR_BAD_SIZE)
    if len(data) > STPG_MAX_PATTERN_BYTES:
        raise PatternValidationError("macro-stream exceeds Stage A size", ERR_BAD_SIZE)
    header = MacroVectorHeader.unpack(data[:STPG_MACROVEC_HEADER_SIZE])
    if header.magic != STPG_MACROVEC_MAGIC:
        raise PatternValidationError("bad macro-stream magic", ERR_BAD_MAGIC)
    if header.version != STPG_PATTERN_VERSION:
        raise PatternValidationError("bad macro-stream version", ERR_BAD_VERSION)
    if header.header_size != STPG_MACROVEC_HEADER_SIZE:
        raise PatternValidationError("bad macro-stream header_size", ERR_PATTERN_INVALID)
    if crc32(data[:36]) != header.header_crc32:
        raise PatternValidationError("bad macro-stream header_crc32", ERR_BAD_CRC)
    if header.flags != 0:
        raise PatternValidationError("macro-stream flags unsupported", ERR_UNSUPPORTED)
    if header.active_pins not in (STPG_ACTIVE_PINS_V01, STPG_ACTIVE_PINS_V11):
        raise PatternValidationError("active_pins is unsupported", ERR_UNSUPPORTED)
    if expected_tick_hz is not None and header.tick_hz != expected_tick_hz:
        raise PatternValidationError("tick_hz is unsupported", ERR_UNSUPPORTED)
    if header.record_size != STPG_MACRO_RECORD_SIZE:
        raise PatternValidationError("bad macro-stream record_size", ERR_PATTERN_INVALID)
    if header.record_count == 0:
        raise PatternValidationError("record_count out of range", ERR_PATTERN_INVALID)
    if header.data_size != header.record_count * STPG_MACRO_RECORD_SIZE:
        raise PatternValidationError("macro-stream data_size mismatch", ERR_PATTERN_INVALID)
    if header.data_size + STPG_MACROVEC_HEADER_SIZE != len(data):
        raise PatternValidationError("macro-stream total size mismatch", ERR_BAD_SIZE)
    payload = data[STPG_MACROVEC_HEADER_SIZE:]
    if crc32(payload) != header.data_crc32:
        raise PatternValidationError("bad macro-stream data_crc32", ERR_BAD_CRC)
    pin_mask = 0xFFFFFF if header.active_pins == STPG_ACTIVE_PINS_V11 else 0xFFFF
    for index in range(0, len(payload), STPG_MACRO_RECORD_SIZE):
        record = MacroVectorRecord.unpack(payload[index : index + STPG_MACRO_RECORD_SIZE])
        label = f"macro record {index // STPG_MACRO_RECORD_SIZE}"
        if record.repeat < 1:
            raise PatternValidationError(f"{label} repeat must be >= 1", ERR_PATTERN_INVALID)
        if record.flags & ~(STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT):
            raise PatternValidationError(f"{label} flags unsupported", ERR_UNSUPPORTED)
        if (record.flags & STPG_EVF_COMPARE_POINT) != 0 and (record.flags & STPG_EVF_SAMPLE_POINT) == 0:
            raise PatternValidationError(f"{label} compare flag requires sample flag", ERR_PATTERN_INVALID)
        if record.sample_mask != 0 and (record.flags & STPG_EVF_SAMPLE_POINT) == 0:
            raise PatternValidationError(f"{label} sample_mask requires sample flag", ERR_PATTERN_INVALID)
        if record.period_ticks < STPG_T_MIN_TICKS:
            raise PatternValidationError(f"{label} period_ticks too small", ERR_RATE_PERIOD_MISMATCH)
        if record.drive_value & ~pin_mask or record.drive_enable & ~pin_mask:
            raise PatternValidationError(f"{label} drive bits exceed active_pins", ERR_UNSUPPORTED)
        if record.sample_mask & ~pin_mask or record.expect_value & ~pin_mask:
            raise PatternValidationError(f"{label} compare bits exceed active_pins", ERR_UNSUPPORTED)
        if record.family not in (1, 2, 3, 4, 5):
            raise PatternValidationError(f"{label} family unsupported", ERR_UNSUPPORTED)
        if record.family in (1, 2):
            if record.phase_ticks == 0 or record.phase_ticks >= record.period_ticks:
                raise PatternValidationError(f"{label} phase_ticks out of range", ERR_RATE_PERIOD_MISMATCH)
        else:
            if record.phase_ticks != 0:
                raise PatternValidationError(f"{label} phase_ticks must be zero", ERR_RATE_PERIOD_MISMATCH)
        if record.family == 3 and record.sample_mask != 0:
            raise PatternValidationError(f"{label} drive-only must not sample", ERR_PATTERN_INVALID)
        if record.family == 4 and record.sample_mask == 0:
            raise PatternValidationError(f"{label} sample-only requires sample_mask", ERR_PATTERN_INVALID)


def validate_macro_p2_vector(data: bytes, *, expected_tick_hz: int | None = None) -> None:
    if len(data) < STPG_MACROVEC_HEADER_SIZE:
        raise PatternValidationError("missing MacroVectorHeader", ERR_BAD_SIZE)
    if len(data) > STPG_MAX_PATTERN_BYTES:
        raise PatternValidationError("macro-p2 exceeds Stage A size", ERR_BAD_SIZE)
    header = MacroVectorHeader.unpack(data[:STPG_MACROVEC_HEADER_SIZE])
    if header.magic != STPG_MACROP2VEC_MAGIC:
        raise PatternValidationError("bad macro-p2 magic", ERR_BAD_MAGIC)
    if header.version != STPG_PATTERN_VERSION:
        raise PatternValidationError("bad macro-p2 version", ERR_BAD_VERSION)
    if header.header_size != STPG_MACROVEC_HEADER_SIZE:
        raise PatternValidationError("bad macro-p2 header_size", ERR_PATTERN_INVALID)
    if crc32(data[:36]) != header.header_crc32:
        raise PatternValidationError("bad macro-p2 header_crc32", ERR_BAD_CRC)
    if header.flags != 0:
        raise PatternValidationError("macro-p2 flags unsupported", ERR_UNSUPPORTED)
    if header.active_pins not in (STPG_ACTIVE_PINS_V01, STPG_ACTIVE_PINS_V11):
        raise PatternValidationError("active_pins is unsupported", ERR_UNSUPPORTED)
    if expected_tick_hz is not None and header.tick_hz != expected_tick_hz:
        raise PatternValidationError("tick_hz is unsupported", ERR_UNSUPPORTED)
    if header.record_size != STPG_MACRO_P2_RECORD_SIZE:
        raise PatternValidationError("bad macro-p2 record_size", ERR_PATTERN_INVALID)
    if header.record_count == 0:
        raise PatternValidationError("record_count out of range", ERR_PATTERN_INVALID)
    if header.data_size != header.record_count * STPG_MACRO_P2_RECORD_SIZE:
        raise PatternValidationError("macro-p2 data_size mismatch", ERR_PATTERN_INVALID)
    if header.data_size + STPG_MACROVEC_HEADER_SIZE != len(data):
        raise PatternValidationError("macro-p2 total size mismatch", ERR_BAD_SIZE)
    payload = data[STPG_MACROVEC_HEADER_SIZE:]
    if crc32(payload) != header.data_crc32:
        raise PatternValidationError("bad macro-p2 data_crc32", ERR_BAD_CRC)
    pin_mask = 0xFFFFFF if header.active_pins == STPG_ACTIVE_PINS_V11 else 0xFFFF
    for index in range(0, len(payload), STPG_MACRO_P2_RECORD_SIZE):
        record = MacroVectorRecordP2.unpack(payload[index : index + STPG_MACRO_P2_RECORD_SIZE])
        label = f"macro-p2 record {index // STPG_MACRO_P2_RECORD_SIZE}"
        if record.repeat < 1:
            raise PatternValidationError(f"{label} repeat must be >= 1", ERR_PATTERN_INVALID)
        if record.source_family not in (1, 2, 3, 4, 5):
            raise PatternValidationError(f"{label} source_family unsupported", ERR_UNSUPPORTED)
        if record.exec_family not in (1, 2, 3, 4, 5, 0x81):
            raise PatternValidationError(f"{label} exec_family unsupported", ERR_UNSUPPORTED)
        if record.period_ticks < STPG_T_MIN_TICKS:
            raise PatternValidationError(f"{label} period_ticks too small", ERR_RATE_PERIOD_MISMATCH)
        if record.wait_first_ticks + record.wait_second_ticks > record.period_ticks:
            raise PatternValidationError(f"{label} wait budget exceeds period", ERR_RATE_PERIOD_MISMATCH)
        for field_name, field_value in (
            ("drive0_value", record.drive0_value),
            ("drive0_enable", record.drive0_enable),
            ("drive1_value", record.drive1_value),
            ("drive1_enable", record.drive1_enable),
            ("sample0_mask", record.sample0_mask),
            ("sample1_mask", record.sample1_mask),
            ("expect0_value", record.expect0_value),
            ("expect1_value", record.expect1_value),
        ):
            if field_value & ~pin_mask:
                raise PatternValidationError(f"{label} {field_name} exceeds active_pins", ERR_UNSUPPORTED)
        if record.expect0_value & ~record.sample0_mask:
            raise PatternValidationError(f"{label} expect0_value requires sample0_mask", ERR_PATTERN_INVALID)
        if record.expect1_value & ~record.sample1_mask:
            raise PatternValidationError(f"{label} expect1_value requires sample1_mask", ERR_PATTERN_INVALID)
        if record.exec_family in (1, 2, 0x81):
            if record.wait_first_ticks == 0:
                raise PatternValidationError(f"{label} wait_first_ticks must be non-zero for 2-event exec", ERR_RATE_PERIOD_MISMATCH)
        elif record.exec_family in (3, 4, 5):
            if record.wait_first_ticks != 0:
                raise PatternValidationError(f"{label} wait_first_ticks must be zero for 1-event exec", ERR_RATE_PERIOD_MISMATCH)


def emitted_macro_event_count_estimate(records: Sequence[MacroVectorRecord]) -> int:
    total = 0
    for record in records:
        events_per_repeat = 2 if record.family in (1, 2) else 1
        total += record.repeat * events_per_repeat
    return total


def sample_macro_event_count_estimate(records: Sequence[MacroVectorRecord]) -> int:
    total = 0
    for record in records:
        if record.family in (1, 2, 4, 5):
            total += record.repeat
    return total


def expand_macro_records_to_events(
    records: Sequence[MacroVectorRecord],
    *,
    active_pins: int = STPG_ACTIVE_PINS_V01,
) -> list[ExecEvent]:
    pin_mask = 0xFFFFFF if active_pins == STPG_ACTIVE_PINS_V11 else 0xFFFF
    current_value = 0
    current_enable = 0
    have_prev_time = False
    prev_time_ticks = 0
    prev_period_ticks = 0
    events: list[ExecEvent] = []

    def emit_event(
        *,
        time_ticks: int,
        sample_trigger: bool,
        flags: int,
        out_value: int,
        out_enable: int,
        sample_mask: int,
        expect_value: int,
        first_in_cycle: bool,
        period_ticks: int,
    ) -> None:
        nonlocal have_prev_time, prev_time_ticks, prev_period_ticks
        if not have_prev_time:
            desired_delta_ticks = time_ticks
        elif first_in_cycle:
            desired_delta_ticks = prev_period_ticks - prev_time_ticks + time_ticks
        else:
            desired_delta_ticks = time_ticks - prev_time_ticks

        if desired_delta_ticks == 0:
            wait_ticks = 0
        else:
            overhead_ticks = STREAMING_POC_WAIT_OVERHEAD_SAMPLE if sample_trigger else STREAMING_POC_WAIT_OVERHEAD_NO_SAMPLE
            if desired_delta_ticks < overhead_ticks:
                raise PatternValidationError("macro-stream phase delta below required overhead", ERR_RATE_PERIOD_MISMATCH)
            wait_ticks = desired_delta_ticks - overhead_ticks

        events.append(
            ExecEvent(
                wait_ticks=wait_ticks,
                flags=flags,
                out_value=out_value & pin_mask,
                out_enable=out_enable & pin_mask,
                sample_mask=sample_mask & pin_mask,
                expect_value=expect_value & pin_mask,
            )
        )
        have_prev_time = True
        prev_time_ticks = time_ticks
        prev_period_ticks = period_ticks

    for record in records:
        drive_value = record.drive_value & pin_mask
        drive_enable = record.drive_enable & pin_mask
        sample_mask = record.sample_mask & pin_mask
        expect_value = record.expect_value & pin_mask
        for _ in range(record.repeat):
            first_in_cycle = True
            if record.family == 1:
                emit_event(
                    time_ticks=0,
                    sample_trigger=False,
                    flags=0,
                    out_value=drive_value,
                    out_enable=drive_enable,
                    sample_mask=0,
                    expect_value=0,
                    first_in_cycle=first_in_cycle,
                    period_ticks=record.period_ticks,
                )
                first_in_cycle = False
                emit_event(
                    time_ticks=record.phase_ticks,
                    sample_trigger=(record.flags & STPG_EVF_SAMPLE_POINT) != 0,
                    flags=record.flags,
                    out_value=drive_value,
                    out_enable=drive_enable,
                    sample_mask=sample_mask,
                    expect_value=expect_value,
                    first_in_cycle=first_in_cycle,
                    period_ticks=record.period_ticks,
                )
                current_value = drive_value
                current_enable = drive_enable
            elif record.family == 2:
                emit_event(
                    time_ticks=0,
                    sample_trigger=(record.flags & STPG_EVF_SAMPLE_POINT) != 0,
                    flags=record.flags,
                    out_value=current_value,
                    out_enable=current_enable,
                    sample_mask=sample_mask,
                    expect_value=expect_value,
                    first_in_cycle=first_in_cycle,
                    period_ticks=record.period_ticks,
                )
                first_in_cycle = False
                emit_event(
                    time_ticks=record.phase_ticks,
                    sample_trigger=False,
                    flags=0,
                    out_value=drive_value,
                    out_enable=drive_enable,
                    sample_mask=0,
                    expect_value=0,
                    first_in_cycle=first_in_cycle,
                    period_ticks=record.period_ticks,
                )
                current_value = drive_value
                current_enable = drive_enable
            elif record.family == 3:
                emit_event(
                    time_ticks=0,
                    sample_trigger=False,
                    flags=0,
                    out_value=drive_value,
                    out_enable=drive_enable,
                    sample_mask=0,
                    expect_value=0,
                    first_in_cycle=first_in_cycle,
                    period_ticks=record.period_ticks,
                )
                current_value = drive_value
                current_enable = drive_enable
            elif record.family == 4:
                emit_event(
                    time_ticks=0,
                    sample_trigger=(record.flags & STPG_EVF_SAMPLE_POINT) != 0,
                    flags=record.flags,
                    out_value=current_value,
                    out_enable=current_enable,
                    sample_mask=sample_mask,
                    expect_value=expect_value,
                    first_in_cycle=first_in_cycle,
                    period_ticks=record.period_ticks,
                )
            elif record.family == 5:
                emit_event(
                    time_ticks=0,
                    sample_trigger=(record.flags & STPG_EVF_SAMPLE_POINT) != 0,
                    flags=record.flags,
                    out_value=drive_value,
                    out_enable=drive_enable,
                    sample_mask=sample_mask,
                    expect_value=expect_value,
                    first_in_cycle=first_in_cycle,
                    period_ticks=record.period_ticks,
                )
                current_value = drive_value
                current_enable = drive_enable
            else:
                raise PatternValidationError("macro-stream family unsupported during expansion", ERR_UNSUPPORTED)
    return events


def expand_macro_pattern(data: bytes) -> tuple[MacroVectorHeader, list[ExecEvent]]:
    header, records = unpack_macro_vector(data)
    return header, expand_macro_records_to_events(records, active_pins=header.active_pins)


def unpack_rate_vector_sections(
    data: bytes, header: RateVectorHeader
) -> tuple[
    RateVectorHeader,
    list[RateDesc | RateDesc32],
    list[RateSlot | RateSlot32],
    list[RateVectorEntry],
]:
    _, rates, slots, vectors, _ = unpack_rate_vector_full(data)
    return header, rates, slots, vectors


_decode_scope: ContextVar[tuple | None] = ContextVar("stpg_rate_vector_decode_scope", default=None)
_DECODE_BYTES_LIMIT = 8 * 1024 * 1024
_DECODE_RECORD_LIMIT = 400_000
_DECODE_ENTRY_LIMIT = 16


@contextmanager
def rate_vector_decode_scope():
    """Bounded exact-byte decoding memo for one synchronous host validation.

    Not a validation cache: CRC, capacities, maps and schedules still run.
    Records are frozen; every caller receives its own list containers. No
    expanded events, exceptions or payloads survive the outer scope.
    """
    current = _decode_scope.get()
    if current is not None and current[0] == get_ident():
        yield
        return
    token = _decode_scope.set((get_ident(), OrderedDict()))
    try:
        yield
    finally:
        _decode_scope.reset(token)


def unpack_rate_vector_full(
    data: bytes,
) -> tuple[RateVectorHeader, list[RateDesc | RateDesc32], list[RateSlot | RateSlot32], list[RateVectorEntry], list[LoopBlock]]:
    scope = _decode_scope.get()
    if scope is None or scope[0] != get_ident():
        return _decode_rate_vector_full(data)
    payload = bytes(data)  # mutable callers cannot change a cached key
    memo = scope[1]
    cached = memo.get(payload)
    if cached is None:
        header, rates, slots, vectors, loops = _decode_rate_vector_full(payload)
        cached = (header, tuple(rates), tuple(slots), tuple(vectors), tuple(loops))
        records = sum(len(items) for items in cached[1:]) + sum(len(loop.body_records) for loop in loops)
        if len(payload) <= _DECODE_BYTES_LIMIT and records <= _DECODE_RECORD_LIMIT:
            while memo and (len(memo) >= _DECODE_ENTRY_LIMIT
                            or sum(len(key) for key in memo) + len(payload) > _DECODE_BYTES_LIMIT
                            or sum(value[1] for value in memo.values()) + records > _DECODE_RECORD_LIMIT):
                memo.popitem(last=False)
            memo[payload] = (cached, records)
    else:
        cached = cached[0]
    return cached[0], *[list(items) for items in cached[1:]]


def _decode_rate_vector_full(
    data: bytes,
) -> tuple[RateVectorHeader, list[RateDesc | RateDesc32], list[RateSlot | RateSlot32], list[RateVectorEntry], list[LoopBlock]]:
    header = RateVectorHeader.unpack(data[:STPG_RATEVEC_HEADER_SIZE])
    rate_size = STPG_RATE_DESC32_SIZE if header.version == STPG_PATTERN_VERSION_WIDE_WAIT else STPG_RATE_DESC_SIZE
    slot_size = STPG_RATE_SLOT32_SIZE if header.version == STPG_PATTERN_VERSION_WIDE_WAIT else STPG_RATE_SLOT_SIZE
    rate_unpack = RateDesc32.unpack if header.version == STPG_PATTERN_VERSION_WIDE_WAIT else RateDesc.unpack
    slot_unpack = RateSlot32.unpack if header.version == STPG_PATTERN_VERSION_WIDE_WAIT else RateSlot.unpack
    rate_blob = data[header.rate_table_offset : header.rate_table_offset + header.rate_count * rate_size]
    slot_blob = data[header.slot_table_offset : header.slot_table_offset + header.slot_count * slot_size]
    vector_blob = data[header.vector_offset : header.vector_offset + header.vector_count * STPG_VECTOR_RECORD_SIZE]
    rates = [rate_unpack(rate_blob[i : i + rate_size]) for i in range(0, len(rate_blob), rate_size)]
    slots = [slot_unpack(slot_blob[i : i + slot_size]) for i in range(0, len(slot_blob), slot_size)]
    loop_blocks: list[LoopBlock] = []
    if header.reserved0 != 0:
        loop_offset = header.reserved0
        while loop_offset < header.vector_offset:
            if loop_offset + STPG_LOOP_BLOCK_HEADER_SIZE > header.vector_offset:
                raise PatternValidationError("truncated LoopBlock header", ERR_RATEVEC_BAD_OFFSET)
            loop_count, body_record_count = LOOP_BLOCK_HEADER_STRUCT.unpack(
                data[loop_offset : loop_offset + STPG_LOOP_BLOCK_HEADER_SIZE]
            )
            if loop_count < 1 or body_record_count < 1:
                raise PatternValidationError("LoopBlock must be non-empty", ERR_LOOP_UNSUPPORTED)
            body_start = loop_offset + STPG_LOOP_BLOCK_HEADER_SIZE
            body_end = body_start + body_record_count * STPG_VECTOR_RECORD_SIZE
            if body_end > header.vector_offset:
                raise PatternValidationError("LoopBlock body out of range", ERR_RATEVEC_BAD_OFFSET)
            body_records = tuple(
                VectorRecord.unpack(data[i : i + STPG_VECTOR_RECORD_SIZE])
                for i in range(body_start, body_end, STPG_VECTOR_RECORD_SIZE)
            )
            loop_blocks.append(LoopBlock(loop_count=loop_count, body_records=body_records))
            loop_offset = body_end
        if loop_offset != header.vector_offset:
            raise PatternValidationError("LoopBlock section misaligned", ERR_RATEVEC_BAD_OFFSET)
    vectors: list[RateVectorEntry] = []
    for i in range(0, len(vector_blob), STPG_VECTOR_RECORD_SIZE):
        raw = vector_blob[i : i + STPG_VECTOR_RECORD_SIZE]
        flags = raw[1]
        if flags == STPG_VECTOR_FLAG_LOOP_REF:
            vectors.append(LoopRefRecord.unpack(raw))
        else:
            vectors.append(VectorRecord.unpack(raw))
    return header, rates, slots, vectors, loop_blocks


def _validate_vector_record(vector: VectorRecord, header: RateVectorHeader, label: str) -> None:
    pin_mask = 0xFFFFFF if header.active_pins == STPG_ACTIVE_PINS_V11 else 0xFFFF
    if vector.rate_id >= header.rate_count:
        raise PatternValidationError(f"{label} bad rate_id", ERR_VECTOR_BAD_RATE_ID)
    if vector.repeat < 1:
        raise PatternValidationError(f"{label} repeat must be >= 1", ERR_RATEVEC_BAD_HEADER)
    if vector.flags & ~STPG_VECTOR_VALID_FLAG_MASK:
        raise PatternValidationError(f"{label} flags unsupported", ERR_RATEVEC_BAD_HEADER)
    if vector.flags not in (0, STPG_VECTOR_FLAG_EXPLICIT_WAIT_P4_END):
        raise PatternValidationError(f"{label} flags unsupported", ERR_RATEVEC_BAD_HEADER)
    if vector.flags == STPG_VECTOR_FLAG_EXPLICIT_WAIT_P4_END and not (header.flags & STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4):
        raise PatternValidationError(f"{label} P4 marker lacks header opt-in", ERR_RATEVEC_BAD_HEADER)
    if vector.drive_value & ~pin_mask or vector.drive_enable & ~pin_mask or vector.expect_value & ~pin_mask or vector.expect_mask & ~pin_mask:
        raise PatternValidationError(f"{label} exceeds active_pins", ERR_RATEVEC_BAD_HEADER)


def fold_adjacent_repeats(entries: Sequence[RateVectorEntry]) -> list[RateVectorEntry]:
    folded: list[RateVectorEntry] = []
    for entry in entries:
        if not isinstance(entry, VectorRecord):
            folded.append(entry)
            continue
        if folded and isinstance(folded[-1], VectorRecord):
            prev = folded[-1]
            assert isinstance(prev, VectorRecord)
            same_shape = (
                prev.rate_id == entry.rate_id
                and prev.flags == entry.flags
                and prev.drive_value == entry.drive_value
                and prev.drive_enable == entry.drive_enable
                and prev.expect_value == entry.expect_value
                and prev.expect_mask == entry.expect_mask
            )
            if same_shape and prev.repeat + entry.repeat <= 0xFFFF:
                folded[-1] = VectorRecord(
                    rate_id=prev.rate_id,
                    flags=prev.flags,
                    repeat=prev.repeat + entry.repeat,
                    drive_value=prev.drive_value,
                    drive_enable=prev.drive_enable,
                    expect_value=prev.expect_value,
                    expect_mask=prev.expect_mask,
                )
                continue
        folded.append(entry)
    return folded


def expanded_vector_count_estimate(entries: Sequence[RateVectorEntry], loop_blocks: Sequence[LoopBlock]) -> int:
    total = 0
    for entry in entries:
        if isinstance(entry, LoopRefRecord):
            loop_block = loop_blocks[entry.loop_id]
            total += loop_block.loop_count * sum(record.repeat for record in loop_block.body_records)
        else:
            total += entry.repeat
    return total


def rate_vector_structure_metrics(data: bytes) -> dict[str, int]:
    """Return explicit encoded and expanded counts for a rate-vector artifact.

    ``RateVectorHeader.vector_count`` is the number of top-level entries, not
    the number of logical vectors executed by the firmware when LoopRefRecord
    entries are present.  Keep both values (and LoopBlock body records)
    available to reports and segment admission so callers never have to infer
    capacity from the ambiguous legacy ``vector_count`` field.
    """

    header, _rates, _slots, entries, loop_blocks = unpack_rate_vector_full(data)
    return {
        "top_level_entry_count": len(entries),
        "encoded_record_count": len(entries) + sum(len(loop.body_records) for loop in loop_blocks),
        "loop_block_count": len(loop_blocks),
        "loop_body_record_count": sum(len(loop.body_records) for loop in loop_blocks),
        "expanded_logical_vector_count": expanded_vector_count_estimate(entries, loop_blocks),
        "rate_count": int(header.rate_count),
        "slot_count": int(header.slot_count),
        "encoded_pattern_bytes": len(data),
    }


def emitted_event_count_estimate(
    rates: Sequence[RateDesc],
    slots: Sequence[RateSlot],
    entries: Sequence[RateVectorEntry],
    loop_blocks: Sequence[LoopBlock],
) -> int:
    def emitted_slots_for_rate(rate_id: int) -> int:
        rate = rates[rate_id]
        count = 0
        prev_time: int | None = None
        for index in range(rate.slot_offset, rate.slot_offset + rate.slot_count):
            time_ticks = slots[index].time_ticks
            if prev_time is None or time_ticks != prev_time:
                count += 1
                prev_time = time_ticks
        return count

    total = 0
    for entry in entries:
        if isinstance(entry, LoopRefRecord):
            loop_block = loop_blocks[entry.loop_id]
            body_total = 0
            for body_record in loop_block.body_records:
                body_total += body_record.repeat * emitted_slots_for_rate(body_record.rate_id)
            total += loop_block.loop_count * body_total
        else:
            total += entry.repeat * emitted_slots_for_rate(entry.rate_id)
    return total


def streaming_poc_timing_issues(
    rates: Sequence[RateDesc],
    slots: Sequence[RateSlot],
) -> list[dict[str, int | bool | str]]:
    """Return every nonzero rate-group gap below streaming-poc overhead.

    This is a format/runtime compatibility check, not a refill or throughput
    check.  Keeping all violations lets source analysis identify every STIL
    timing problem before a binary is packaged or sent to a device.
    """

    issues: list[dict[str, int | bool | str]] = []
    for rate_id, rate in enumerate(rates):
        rate_slots = slots[rate.slot_offset : rate.slot_offset + rate.slot_count]
        if not rate_slots:
            continue
        merged_groups: list[tuple[int, bool]] = []
        index = 0
        while index < len(rate_slots):
            merged_time = rate_slots[index].time_ticks
            sample_trigger = False
            while index < len(rate_slots) and rate_slots[index].time_ticks == merged_time:
                if (rate_slots[index].flags & STPG_RATE_SLOT_SAMPLE) != 0:
                    sample_trigger = True
                index += 1
            merged_groups.append((merged_time, sample_trigger))
        if not merged_groups:
            continue
        prev_time = merged_groups[-1][0]
        for group_index, (time_ticks, sample_trigger) in enumerate(merged_groups):
            if group_index == 0:
                delta_ticks = rate.period_ticks - prev_time + time_ticks
            else:
                delta_ticks = time_ticks - prev_time
            prev_time = time_ticks
            if delta_ticks == 0:
                continue
            required_ticks = (
                STREAMING_POC_WAIT_OVERHEAD_SAMPLE if sample_trigger else STREAMING_POC_WAIT_OVERHEAD_NO_SAMPLE
            )
            if delta_ticks < required_ticks:
                group_label = "cycle-wrap" if group_index == 0 else f"group {group_index}"
                sample_label = "sample" if sample_trigger else "drive-only"
                issues.append(
                    {
                        "rate_id": int(rate_id),
                        "group_index": int(group_index),
                        "group_label": group_label,
                        "group_time_ticks": int(time_ticks),
                        "period_ticks": int(rate.period_ticks),
                        "gap_ticks": int(delta_ticks),
                        "required_ticks": int(required_ticks),
                        "deficit_ticks": int(required_ticks - delta_ticks),
                        "sample_trigger": bool(sample_trigger),
                        "operation": sample_label,
                        "message": (
                            f"rate[{rate_id}] {group_label} gap {delta_ticks} ticks is below "
                            f"streaming-poc {sample_label} overhead {required_ticks}"
                        ),
                    }
                )
    return issues


def streaming_poc_timing_incompatibility(
    rates: Sequence[RateDesc],
    slots: Sequence[RateSlot],
) -> str | None:
    """Return the first compatibility message for legacy callers."""

    issues = streaming_poc_timing_issues(rates, slots)
    if issues:
        return str(issues[0]["message"])
    return None


assert EXEC_EVENT_STRUCT.size == STPG_EXEC_EVENT_SIZE
assert PATTERN_HEADER_STRUCT.size == STPG_HEADER_SIZE
assert RATE_VECTOR_HEADER_STRUCT.size == STPG_RATEVEC_HEADER_SIZE
assert MACRO_VECTOR_HEADER_STRUCT.size == STPG_MACROVEC_HEADER_SIZE
assert RATE_DESC_STRUCT.size == STPG_RATE_DESC_SIZE
assert RATE_SLOT_STRUCT.size == STPG_RATE_SLOT_SIZE
assert RATE_DESC32_STRUCT.size == STPG_RATE_DESC32_SIZE
assert RATE_SLOT32_STRUCT.size == STPG_RATE_SLOT32_SIZE
assert VECTOR_RECORD_STRUCT.size == STPG_VECTOR_RECORD_SIZE
assert MACRO_VECTOR_RECORD_STRUCT.size == STPG_MACRO_RECORD_SIZE
assert LOOP_BLOCK_HEADER_STRUCT.size == STPG_LOOP_BLOCK_HEADER_SIZE


def resolve_max_rates(max_rates: int | None) -> int:
    """Return the selected host-side RateDesc admission limit.

    The default remains the deployed device contract.  Candidate builds may
    pass an explicit limit while keeping the baseline host behavior unchanged.
    """

    limit = STPG_MAX_RATES if max_rates is None else max_rates
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= STPG_MAX_CONFIGURABLE_RATES:
        raise PatternValidationError(
            f"max_rates must be an integer in the range 1..{STPG_MAX_CONFIGURABLE_RATES}",
            ERR_RATEVEC_BAD_HEADER,
        )
    return limit
