"""STPG command and response packing."""

from __future__ import annotations

from dataclasses import dataclass
import struct

from .constants import (
    ERR_BAD_CRC,
    ERR_BAD_MAGIC,
    ERR_BAD_VERSION,
    STPG_CMD_MAGIC,
    STPG_CMD_SIZE,
    STPG_PROTO_VERSION,
    STPG_RSP_SIZE,
)
from .crc32 import crc32

COMMAND_STRUCT = struct.Struct("<IHHIIIIII")
RESPONSE_STRUCT = struct.Struct("<IHHIIIIIIIIIIIIII")


@dataclass(frozen=True)
class StpgCommand:
    cmd: int
    seq: int
    arg0: int = 0
    arg1: int = 0
    arg2: int = 0
    arg3: int = 0
    magic: int = STPG_CMD_MAGIC
    version: int = STPG_PROTO_VERSION
    packet_crc32: int = 0

    def pack(self) -> bytes:
        prefix = struct.pack(
            "<IHHIIIII",
            self.magic,
            self.version,
            self.cmd,
            self.seq,
            self.arg0,
            self.arg1,
            self.arg2,
            self.arg3,
        )
        return prefix + struct.pack("<I", crc32(prefix))

    @classmethod
    def unpack(cls, data: bytes, *, validate_crc: bool = True) -> "StpgCommand":
        if len(data) != STPG_CMD_SIZE:
            raise ValueError(f"StpgCommand must be {STPG_CMD_SIZE} bytes")
        fields = COMMAND_STRUCT.unpack(data)
        magic, version, cmd, seq, arg0, arg1, arg2, arg3, packet_crc = fields
        if magic != STPG_CMD_MAGIC:
            raise ValueError(f"bad command magic: error={ERR_BAD_MAGIC}")
        if version != STPG_PROTO_VERSION:
            raise ValueError(f"bad protocol version: error={ERR_BAD_VERSION}")
        if validate_crc and crc32(data[:28]) != packet_crc:
            raise ValueError(f"bad command crc: error={ERR_BAD_CRC}")
        return cls(cmd, seq, arg0, arg1, arg2, arg3, magic, version, packet_crc)


@dataclass(frozen=True)
class StpgStatusResponse:
    rsp: int
    seq: int
    state: int
    capability: int
    allowed_cmds: int
    active_pins: int
    pattern_size: int = 0
    event_count: int = 0
    run_progress: int = 0
    result_flags: int = 0
    fail_count: int = 0
    sample_count: int = 0
    error_code: int = 0
    firmware_version: int = 0
    magic: int = STPG_CMD_MAGIC
    version: int = STPG_PROTO_VERSION
    packet_crc32: int = 0

    def pack(self) -> bytes:
        prefix = struct.pack(
            "<IHHIIIIIIIIIIIII",
            self.magic,
            self.version,
            self.rsp,
            self.seq,
            self.state,
            self.capability,
            self.allowed_cmds,
            self.active_pins,
            self.pattern_size,
            self.event_count,
            self.run_progress,
            self.result_flags,
            self.fail_count,
            self.sample_count,
            self.error_code,
            self.firmware_version,
        )
        return prefix + struct.pack("<I", crc32(prefix))

    @classmethod
    def unpack(cls, data: bytes, *, validate_crc: bool = True) -> "StpgStatusResponse":
        if len(data) != STPG_RSP_SIZE:
            raise ValueError(f"StpgStatusResponse must be {STPG_RSP_SIZE} bytes")
        fields = RESPONSE_STRUCT.unpack(data)
        (
            magic,
            version,
            rsp,
            seq,
            state,
            capability,
            allowed_cmds,
            active_pins,
            pattern_size,
            event_count,
            run_progress,
            result_flags,
            fail_count,
            sample_count,
            error_code,
            firmware_version,
            packet_crc,
        ) = fields
        if magic != STPG_CMD_MAGIC:
            raise ValueError(f"bad response magic: error={ERR_BAD_MAGIC}")
        if version != STPG_PROTO_VERSION:
            raise ValueError(f"bad protocol version: error={ERR_BAD_VERSION}")
        if validate_crc and crc32(data[:60]) != packet_crc:
            raise ValueError(f"bad response crc: error={ERR_BAD_CRC}")
        return cls(
            rsp,
            seq,
            state,
            capability,
            allowed_cmds,
            active_pins,
            pattern_size,
            event_count,
            run_progress,
            result_flags,
            fail_count,
            sample_count,
            error_code,
            firmware_version,
            magic,
            version,
            packet_crc,
        )


assert COMMAND_STRUCT.size == STPG_CMD_SIZE
assert RESPONSE_STRUCT.size == STPG_RSP_SIZE
