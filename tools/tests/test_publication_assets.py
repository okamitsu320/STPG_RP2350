"""Independent synthetic structures; no private artifact or device is needed."""
import hashlib
from pathlib import Path
import struct
import zipfile

import pytest

from release_tools.audit_firmware import ARM_SECURE_FAMILY, FLASH_BASE, elf_flash, uf2_flash
from release_tools.prepare_release_assets import sanitize_elf_metadata
from release_tools.audit_distribution import audit, member_path, scan_members


def synthetic_elf(*, allocated_metadata=False):
    raw = bytearray(1024)
    raw[:7] = b"\x7fELF\x01\x01\x01"
    struct.pack_into("<HHIIIIIHHHHHH", raw, 16, 2, 40, 1, FLASH_BASE + 1, 52, 512, 0,
                     52, 32, 1, 40, 3, 0)
    struct.pack_into("<8I", raw, 52, 1, 256, FLASH_BASE, FLASH_BASE, 16, 16, 5, 4)
    raw[256:272] = bytes(range(16))
    value = b"/" + b"home" + b"/synthetic/source/file.c"
    raw[400:400 + len(value)] = value
    struct.pack_into("<10I", raw, 552, 0, 1, 2, FLASH_BASE, 256, 16, 0, 0, 4, 0)
    struct.pack_into("<10I", raw, 592, 0, 1, 2 if allocated_metadata else 0, 0, 400, len(value), 0, 0, 1, 0)
    return bytes(raw)


def uf2_block(address, payload, *, flags=0x2000, family=ARM_SECURE_FAMILY, number=0, total=1):
    block = bytearray(512)
    struct.pack_into("<8I", block, 0, 0x0A324655, 0x9E5D5157, flags, address, 256, number, total, family)
    block[32:288] = payload
    if flags == 0xA000:
        struct.pack_into("<I", block, 288, 0x9957E304)
    struct.pack_into("<I", block, 508, 0x0AB16F30)
    return bytes(block)


def synthetic_uf2():
    ignore = uf2_block(0x10FFFF00, b"\xef" * 256, flags=0xA000, family=0xE48BFF57, total=2)
    payload = bytes(range(16)) + bytes(240)
    return ignore + uf2_block(FLASH_BASE, payload)


def test_metadata_redaction_preserves_headers_allocated_and_loadable_bytes():
    raw = synthetic_elf()
    changed, count = sanitize_elf_metadata(raw)
    assert count == 1 and len(changed) == len(raw) and changed != raw
    assert changed[:84] == raw[:84] and changed[512:632] == raw[512:632]
    assert changed[256:272] == raw[256:272]
    assert elf_flash(changed)[0] == bytes(range(16)) == elf_flash(raw)[0]
    assert b"synthetic/source" not in changed


def test_metadata_redaction_refuses_allocated_match():
    with pytest.raises(ValueError, match="allocated/loadable/header"):
        sanitize_elf_metadata(synthetic_elf(allocated_metadata=True))


def test_uf2_positive_control_is_independently_packed_and_equal_to_elf():
    image, detail = uf2_flash(synthetic_uf2(), expected_size=16)
    assert image == elf_flash(synthetic_elf())[0] == bytes(range(16))
    assert detail["ordinary_blocks"] == 1 and detail["padding_bytes"] == 240


@pytest.mark.parametrize("offset", [0, 8, 24, 288, 512, 512 + 8, 512 + 20, 512 + 24, 512 + 28, 512 + 508])
def test_uf2_rejects_independent_header_or_ignore_mutations(offset):
    raw = bytearray(synthetic_uf2())
    raw[offset] ^= 1
    with pytest.raises(ValueError):
        uf2_flash(bytes(raw), expected_size=16)


def distribution_members():
    return [("stpgview/static/" + name, b"public synthetic text")
            for name in ("app.js", "index.html", "runner.js", "style.css")] + [
                ("pkg.dist-info/licenses/LICENSE", b"MIT"),
                ("pkg.dist-info/licenses/THIRD_PARTY_NOTICES.md", b"scope")]


def test_distribution_positive_and_missing_asset_controls():
    members = distribution_members()
    assert scan_members(members, wheel=True)["files"] == 6
    with pytest.raises(ValueError, match="GUI static"):
        scan_members(members[1:], wheel=True)


@pytest.mark.parametrize("path", ["../outside", "/absolute", "a\\b", "C:/drive", "a//b", "a/./b",
                                 "a/CON.txt", "a/trailing.", "a/trailing ", "a/wild*.py"])
def test_archive_paths_reject_traversal_or_nonportable_forms(path):
    with pytest.raises(ValueError):
        member_path(path)


def test_distribution_rejects_extra_package_private_path_and_binary():
    for name, data in [("scripts/physical_runner.py", b""), ("stpg/old.elf", b""),
                       ("stpg/private.py", b"/" + b"home" + b"/synthetic/private")]:
        with pytest.raises(ValueError):
            scan_members(distribution_members() + [(name, data)], wheel=True)


@pytest.mark.parametrize("mutate", ["none", "code", "notice"])
def test_actual_wheel_hashes_bind_code_and_notices(tmp_path, mutate):
    members = distribution_members() + [("stpg/demo.py", b"value = 1\n")]
    expected = {name: hashlib.sha256(raw).hexdigest() for name, raw in members
                if name.startswith(("stpg/", "stpgview/"))}
    notices = {name.split("/licenses/", 1)[1]: hashlib.sha256(raw).hexdigest()
               for name, raw in members if "/licenses/" in name}
    target = "stpg/demo.py" if mutate == "code" else "pkg.dist-info/licenses/LICENSE"
    path = tmp_path / "synthetic.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, raw in members:
            archive.writestr(name, raw + b"changed" if mutate != "none" and name == target else raw)
    if mutate == "none":
        assert audit(path, expected_package_files=expected, expected_notices=notices)["structure_check"] == "COMPLETE"
    else:
        with pytest.raises(ValueError, match="inventory/hash"):
            audit(path, expected_package_files=expected, expected_notices=notices)


@pytest.mark.parametrize("name,raw", [
    ("stpg_capture/driver.py", b"pass\n"),
    ("stpg_la/driver.py", b"pass\n"),
    ("stpgview/runner/service.py", b"from stpg_capture import AnalyzerConfig\n"),
    ("stpgview/runner/service.py", b'import importlib\nimportlib.import_module("stpg_la")\n'),
])
def test_actual_distribution_boundary_rejects_la_package_or_import_edge(name, raw):
    with pytest.raises(ValueError):
        scan_members(distribution_members() + [(name, raw)], wheel=True)


def test_source_archive_directory_entries_are_bounded(tmp_path, monkeypatch):
    import tarfile
    from release_tools import audit_distribution as module
    monkeypatch.setattr(module, "MAX_FILES", 2)
    target = tmp_path / "many-dirs.tar.gz"
    with tarfile.open(target, "w:gz") as archive:
        for index in range(5):
            member = tarfile.TarInfo(f"source/dir-{index}")
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
    with pytest.raises(ValueError, match="including directories"):
        audit(target)


@pytest.mark.parametrize("name", ["runner_no_hardware.py", "test_runner_gui_handoff.py",
                                "test_runner_round3_handoff.py", "old.patch"])
def test_source_archive_excludes_all_old_review_paths_but_keeps_test_helpers(name):
    with pytest.raises(ValueError, match="internal review"):
        scan_members(distribution_members() + [("source/tools/reviews/" + name, b"synthetic")], wheel=False)
    with pytest.raises(ValueError, match="internal review"):
        scan_members(distribution_members() + [("source/tools/reviews/audit_runner_result.py", b"pass\n")], wheel=False)
    assert scan_members(distribution_members() + [("source/tools/tests/helpers/audit_runner_result.py", b"pass\n")],
                        wheel=False)["files"] == 7


def test_source_inventory_moves_public_helpers_to_tests_and_excludes_private_trees(tmp_path):
    from release_tools.source_manifest import source_files
    folder = tmp_path / "tools/reviews"
    folder.mkdir(parents=True)
    for name in ["audit_runner_result.py", "runner_no_hardware.py", "old.patch"]:
        (folder / name).write_text("pass\n")
    helper = tmp_path / "tools/tests/helpers/audit_runner_result.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("pass\n")
    for excluded in ("doc", "docs", "recovery-safe", "release_coordination", "Docs"):
        target = tmp_path / excluded
        target.mkdir()
        (target / "private.md").write_text("synthetic private placeholder")
    assert set(source_files(tmp_path)) == {"tools/tests/helpers/audit_runner_result.py"}
    manifest = (Path(__file__).parents[2] / "MANIFEST.in").read_text()
    assert "recursive-include tools/reviews" not in manifest
    assert "recursive-include tools/tests" in manifest
