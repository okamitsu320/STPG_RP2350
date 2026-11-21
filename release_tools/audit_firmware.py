"""Read-only ELF32/UF2 correspondence and metadata-privacy audit. No hardware I/O.

Reports counts, identities and addresses, never the contents of matched strings.
No stripping, image patching, rebuilding, or publication is performed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import zipfile

FLASH_BASE = 0x10000000
FLASH_LIMIT = 0x11000000
ARM_SECURE_FAMILY = 0xE48BFF59
PRIVATE_PATH = re.compile(rb"/(?:home|Users)/[^\x00\s\"']+")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def elf_flash(raw: bytes) -> tuple[bytes, dict]:
    if len(raw) < 52 or raw[:7] != b"\x7fELF\x01\x01\x01":
        raise ValueError("expected a little-endian ELF32 file")
    fields = struct.unpack_from("<HHIIIIIHHHHHH", raw, 16)
    kind, machine, _, entry, phoff, _, _, _, phsize, count, *_ = fields
    if kind != 2 or machine != 40 or phsize != 32 or count > 64 or phoff + phsize * count > len(raw):
        raise ValueError("invalid ARM executable/program-header table")
    segments = []
    for index in range(count):
        typ, offset, _, physical, size, memsize, _, _ = struct.unpack_from("<IIIIIIII", raw, phoff + index * phsize)
        if typ != 1 or size == 0:
            continue
        if (offset + size > len(raw) or size > memsize or physical < FLASH_BASE
                or physical + size > FLASH_LIMIT):
            raise ValueError("loadable segment is outside the allowed flash image")
        segments.append((physical, raw[offset:offset + size]))
    segments.sort()
    address = FLASH_BASE
    image = bytearray()
    for physical, payload in segments:
        if physical != address:
            raise ValueError("loadable flash segments overlap or have a gap")
        image.extend(payload)
        address += len(payload)
    if not image or not FLASH_BASE <= entry < address:
        raise ValueError("missing flash image or entry point outside the image")
    return bytes(image), {"segments": len(segments), "entry": entry, "flash_base": FLASH_BASE}


def uf2_flash(raw: bytes, *, expected_size: int) -> tuple[bytes, dict]:
    if not raw or len(raw) % 512:
        raise ValueError("UF2 length is not a nonzero multiple of 512")
    count = len(raw) // 512
    pieces, ignored = [], []
    for index in range(count):
        block = raw[index * 512:(index + 1) * 512]
        m0, m1, flags, address, size, number, total, family = struct.unpack_from("<8I", block)
        if (m0 != 0x0A324655 or m1 != 0x9E5D5157
                or struct.unpack_from("<I", block, 508)[0] != 0x0AB16F30
                or size != 256):
            raise ValueError("malformed UF2 block/header/order")
        payload = block[32:32 + size]
        if address == 0x10FFFF00:
            if (flags != 0xA000 or family != 0xE48BFF57 or number != 0 or total != 2
                    or payload != b"\xef" * 256
                    or struct.unpack_from("<I", block, 32 + size)[0] != 0x9957E304):
                raise ValueError("unexpected RP2350 E10 compatibility block")
            ignored.append(index)
            continue
        if (flags != 0x2000 or family != ARM_SECURE_FAMILY
                or number != len(pieces) or total != count - 1
                or not FLASH_BASE <= address < FLASH_LIMIT):
            raise ValueError("unexpected UF2 family, flags or flash address")
        pieces.append((address, payload))
    if ignored != [0]:
        raise ValueError("expected exactly one leading E10 absolute-family ignore block")
    image = bytearray()
    for address, payload in pieces:
        if address != FLASH_BASE + len(image):
            raise ValueError("UF2 payload addresses are not contiguous")
        image.extend(payload)
    if not 0 <= len(image) - expected_size < 256 or any(image[expected_size:]):
        raise ValueError("UF2 padding beyond the expected image is invalid")
    return bytes(image[:expected_size]), {"ordinary_blocks": len(pieces), "ignore_blocks": len(ignored),
                                        "padding_bytes": len(image) - expected_size,
                                        "family": ARM_SECURE_FAMILY}


def audit_archive(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        files = [item for item in archive.infolist() if not item.is_dir()]
        if any(item.file_size > 8 * 1024 * 1024 or item.flag_bits & 1 for item in files):
            raise ValueError("oversized or encrypted firmware archive member")
        elf_members = [item for item in files if Path(item.filename).name == "stpg_firmware.elf"]
        uf2_members = [item for item in files if Path(item.filename).name == "stpg_firmware.uf2"]
        if len(elf_members) != 1 or len(uf2_members) != 1:
            raise ValueError("archive must contain exactly one nominated ELF and UF2")
        elf, uf2 = archive.read(elf_members[0]), archive.read(uf2_members[0])
    image, elf_info = elf_flash(elf)
    uf2_image, uf2_info = uf2_flash(uf2, expected_size=len(image))
    if image != uf2_image:
        raise ValueError("ELF and UF2 loadable images differ")
    loadable_paths = len(PRIVATE_PATH.findall(image))
    all_elf_paths = len(PRIVATE_PATH.findall(elf))
    return {"kind": "stpg_firmware_file_audit", "schema_version": 1,
            "archive_sha256": sha(path.read_bytes()),
            "elf": {"size_bytes": len(elf), "sha256": sha(elf), **elf_info},
            "uf2": {"size_bytes": len(uf2), "sha256": sha(uf2), **uf2_info},
            "flash_image": {"size_bytes": len(image), "sha256": sha(image), "elf_uf2_equal": True},
            "privacy": {"loadable_personal_path_occurrences": loadable_paths,
                        "elf_personal_path_occurrences": all_elf_paths,
                        "matched_text_disclosed": False,
                        "scope": "personal path signature only; not a complete confidential-workload audit"},
            "public_distribution_cleared": False,
            "device_io_performed": False, "image_modified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_archive(args.archive)
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
