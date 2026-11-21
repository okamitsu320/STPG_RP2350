from stpg.crc32 import crc32


def test_crc32_vectors() -> None:
    assert crc32(b"") == 0x00000000
    assert crc32(b"\x00") == 0xD202EF8D
    assert crc32(b"123456789") == 0xCBF43926
    assert crc32(b"\xFF" * 32) == 0xFF6CAB0B
    assert crc32(bytes(range(256))) == 0x29058C73
