"""CRC-32/ISO-HDLC helpers."""

from __future__ import annotations

import binascii


def crc32(data: bytes | bytearray | memoryview, crc: int = 0) -> int:
    """Return zlib-compatible CRC32 as an unsigned 32-bit integer."""

    return binascii.crc32(data, crc) & 0xFFFFFFFF
