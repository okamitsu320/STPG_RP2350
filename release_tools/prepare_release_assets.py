"""Prepare NEW metadata-redacted ELF/unchanged UF2 assets offline, never flash them.

The nominated archive is preserved. Output is exclusive; no retries/overwrites.
Identity/correspondence/privacy evidence is recorded before accepting outputs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct
import zipfile

if __package__:
    from .audit_firmware import PRIVATE_PATH, audit_archive, elf_flash, sha
else:
    from audit_firmware import PRIVATE_PATH, audit_archive, elf_flash, sha


def sanitize_elf_metadata(raw: bytes) -> tuple[bytes, int]:
    """Redact matching non-allocated metadata strings without relaying out ELF.

    Ordinary objcopy --strip-debug changes this image's overlapping BSS/LMA
    program headers. Instead keep all headers, offsets and string lengths and
    reject a match touching ANY allocated section or loadable file bytes.
    No instruction/data/initialization byte is edited.
    """
    elf_flash(raw)
    fields = struct.unpack_from("<HHIIIIIHHHHHH", raw, 16)
    phoff, shoff, phsize, phnum, shsize, shnum = fields[4], fields[5], fields[8], fields[9], fields[10], fields[11]
    if shsize != 40 or shnum > 1024 or shoff + shsize * shnum > len(raw):
        raise ValueError("invalid ELF section table")
    protected = [(0, 52), (phoff, phoff + phsize * phnum), (shoff, shoff + shsize * shnum)]
    for i in range(phnum):
        typ, offset, _, _, size, _, _, _ = struct.unpack_from("<8I", raw, phoff + i * phsize)
        if typ == 1 and size:
            protected.append((offset, offset + size))
    for i in range(shnum):
        _, typ, flags, _, offset, size, *_ = struct.unpack_from("<10I", raw, shoff + i * shsize)
        if flags & 2 and typ != 8 and size:  # SHF_ALLOC, excluding SHT_NOBITS
            protected.append((offset, offset + size))
    result = bytearray(raw)
    matches = list(PRIVATE_PATH.finditer(raw))
    for match in matches:
        start, end = match.span()
        if any(start < hi and end > lo for lo, hi in protected):
            raise ValueError("personal path touches allocated/loadable/header bytes; refusing to modify it")
        length = end - start
        replacement = b"/redacted/" + b"_" * (length - 10) if length >= 10 else b"_" * length
        result[start:end] = replacement
    for lo, hi in protected:
        if result[lo:hi] != raw[lo:hi]:
            raise ValueError("protected ELF bytes changed")
    return bytes(result), len(matches)


def prepare(archive_path: Path, destination: Path) -> dict:
    original = audit_archive(archive_path)
    if original["privacy"]["loadable_personal_path_occurrences"]:
        raise ValueError("original loadable image contains a personal path; do not patch it for publication")
    destination.mkdir()  # exclusive; no overwrite, including failed attempts
    with zipfile.ZipFile(archive_path) as archive:
        elf = archive.read(next(p for p in archive.namelist() if p.endswith("/stpg_firmware.elf")))
        uf2 = archive.read(next(p for p in archive.namelist() if p.endswith("/stpg_firmware.uf2")))
    output = destination / "stpg-rp2350-experimental-200mhz.elf"
    prepared, count = sanitize_elf_metadata(elf)
    output.write_bytes(prepared)
    image, _ = elf_flash(prepared)
    old_image, _ = elf_flash(elf)
    if image != old_image or PRIVATE_PATH.search(prepared):
        raise ValueError("redacted ELF changed loadable bytes or retains a personal path; output is NOT accepted")
    uf2_path = destination / "stpg-rp2350-experimental-200mhz.uf2"
    uf2_path.write_bytes(uf2)
    records = [{"name": p.name, "size_bytes": p.stat().st_size, "sha256": sha(p.read_bytes())}
               for p in (output, uf2_path)]
    report = {"kind": "stpg_prepared_firmware_assets", "schema_version": 1,
              "original_archive_sha256": original["archive_sha256"],
              "original_elf_sha256": sha(elf), "operation": "fixed-length personal-path redaction in nonallocated ELF metadata",
              "preparation_script_sha256": sha(Path(__file__).read_bytes()),
              "redacted_metadata_path_occurrences": count,
              "files": records, "flash_image": original["flash_image"],
              "all_elf_pt_load_file_bytes_equal": True, "uf2_unchanged": True,
              "prepared_elf_personal_path_occurrences": 0,
              "public_distribution_cleared": False, "device_io_performed": False,
              "source_correspondence_and_binary_notices": "REVIEW_REQUIRED"}
    (destination / "firmware-assets.json").write_text(json.dumps(report, indent=2) + "\n")
    (destination / "SHA256SUMS").write_text("".join(f"{r['sha256']}  {r['name']}\n" for r in records))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.archive, args.output_dir), indent=2))


if __name__ == "__main__":
    main()
