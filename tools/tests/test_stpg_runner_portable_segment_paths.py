"""Portable writer/SDK checks using only public synthetic STIL and local files.

The Windows emulation changes only the segmented writer's local Path binding:
filesystem operations stay native, but relative_to returns PureWindowsPath.
It exercises the real writer rather than accepting a stubbed build result.
"""

from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil

import pytest

import stil.segmented_package as writer
from stpg_runner import build_project, init_project, prepare_run, verify_bundle
from stpg_runner.artifacts import RunnerError, identity, portable_path, read_json, write_json


pytestmark = pytest.mark.stpg_host_only
FIXTURE = Path(__file__).parent / "fixtures" / "runner" / "pattern.stil"
FIELDS = ("path", "map_path", "package_manifest_path", "wait_schedule.path")


@pytest.fixture(autouse=True)
def no_contact(monkeypatch):
    import serial
    from serial.tools import list_ports

    def forbidden(*args, **kwargs):
        raise AssertionError("portable artifact tests must not contact hardware")

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(list_ports, "comports", forbidden)


def windows_relative_paths(monkeypatch, native_path_type):
    class WindowsRelativePath(native_path_type):
        def relative_to(self, *args, **kwargs):
            return PureWindowsPath(super().relative_to(*args, **kwargs))

    # Do not monkeypatch pathlib globally or change any filesystem resolution.
    monkeypatch.setattr(writer, "Path", WindowsRelativePath)


def create(tmp_path, *, segmented=True, waited=False):
    kwargs = {}
    if waited:
        kwargs = {"service_us": 4000, "service_scope": "non-loop",
                  "service_source": "public synthetic model; not measured",
                  "max_total_wait_us": 100000}
    return init_project(
        FIXTURE, tmp_path / "project space 日本語", completion="hold",
        wait_preset="chunk-4ms" if waited else "none",
        max_segment_vectors=2 if segmented else None, **kwargs,
    )


@pytest.mark.parametrize("path_type,root", [
    (PurePosixPath, "/work/variant"),
    (PureWindowsPath, r"C:\work\variant"),
    (PureWindowsPath, r"\\server\share\work\variant"),
])
@pytest.mark.parametrize("name", ["pattern.bin", "pattern.map.json.gz", "pattern.wait.json"])
def test_relative_posix_uses_path_flavour_not_native_str(path_type, root, name):
    base = path_type(root)
    child = base / "segment space 日本語" / "package" / name
    assert writer._relative_posix(child, base, field="segment[0].path") == (
        f"segment space 日本語/package/{name}"
    )


@pytest.mark.parametrize("base,child", [
    (PurePosixPath("/work/variant"), PurePosixPath("/work/outside.bin")),
    (PureWindowsPath("C:/work/variant"), PureWindowsPath("D:/outside.bin")),
    (PurePosixPath("/work/variant"), PurePosixPath("/work/variant")),
    (PurePosixPath("/work/variant"), PurePosixPath("/work/variant/../outside.bin")),
    (PurePosixPath("/work/variant"), PurePosixPath("/work/variant/segment\\bad/pattern.bin")),
    (PurePosixPath("/work/variant"), PurePosixPath("/work/variant/C:/pattern.bin")),
    (PurePosixPath("/work/variant"), PurePosixPath("/work/variant/bad\x00/pattern.bin")),
])
def test_writer_rejects_invalid_reference_with_field(base, child):
    with pytest.raises(writer.SegmentedPackageError, match=r"segment\[2\]\.map_path:"):
        writer._relative_posix(child, base, field="segment[2].map_path")


@pytest.mark.parametrize("waited", [False, True])
def test_windows_relative_rendering_builds_portable_bundle(tmp_path, monkeypatch, waited):
    windows_relative_paths(monkeypatch, type(tmp_path))
    project = create(tmp_path, waited=waited)
    result = build_project(project)
    assert result.package_kind == "segmented" and result.segment_count == 2
    manifest = read_json(result.bundle_dir / "runner.bundle.json")
    package = read_json(result.bundle_dir / manifest["package_manifest"])
    for index, segment in enumerate(package["segments"]):
        prefix = f"segment-{index:04d}/package/"
        assert segment["path"] == prefix + "pattern.bin"
        assert segment["package_manifest_path"] == prefix + "manifest.json"
        assert segment["map_path"] in {prefix + "pattern.map.json", prefix + "pattern.map.json.gz"}
        assert segment["wait_schedule"]["path"] in {
            prefix + "pattern.wait.json", prefix + "pattern.wait.json.gz",
        }
    assert [(p["expected"]["events"], p["expected"]["samples"], p["expected"]["compares"])
            for p in manifest["patterns"]] == [(3, 1, 0), (3, 1, 0)]
    assert verify_bundle(result.bundle_dir, manifest_sha256=result.manifest_sha256)["status"] == "PASS"
    assert len(prepare_run(result.bundle_dir, manifest_sha256=result.manifest_sha256).segments) == 2
    hit = build_project(project)
    assert hit.cache_hit and hit.bundle_dir == result.bundle_dir
    assert hit.manifest_sha256 == result.manifest_sha256
    relocated = tmp_path / "relocated bundle 日本語"
    shutil.copytree(result.bundle_dir, relocated)
    assert verify_bundle(relocated, manifest_sha256=result.manifest_sha256)["status"] == "PASS"


def test_single_package_is_unchanged_by_windows_writer_emulation(tmp_path, monkeypatch):
    windows_relative_paths(monkeypatch, type(tmp_path))
    result = build_project(create(tmp_path, segmented=False))
    assert result.package_kind == "single" and result.segment_count == 1
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


def test_writer_rejects_before_manifest_and_new_tool_keeps_failed_prefix(tmp_path, monkeypatch):
    import stpg_runner.build as builder

    class InvalidRelativePath(type(tmp_path)):
        def relative_to(self, *args, **kwargs):
            value = super().relative_to(*args, **kwargs).as_posix()
            return PurePosixPath(value.replace("/", "\\"))

    project = create(tmp_path)
    with monkeypatch.context() as legacy:
        # Represent a prior compiler identity without editing production source
        # or repairing/rebinding any failed reservation.
        legacy.setattr(builder, "tool_fingerprint", lambda: "0" * 64)
        legacy.setattr(writer, "Path", InvalidRelativePath)
        with pytest.raises(writer.SegmentedPackageError, match=r"segment\[0\]\.path:"):
            build_project(project)
    failed, = (project.parent / "build").iterdir()
    assert read_json(failed / "build.state.json")["status"] == "FAILED"
    assert not (failed / "segmented" / "nominal" / "segmented.manifest.json").exists()
    prefix = {p.relative_to(failed).as_posix(): identity(p)
              for p in failed.rglob("*") if p.is_file()}
    result = build_project(project)
    assert result.bundle_dir != failed and not result.cache_hit
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"
    assert {p.relative_to(failed).as_posix(): identity(p)
            for p in failed.rglob("*") if p.is_file()} == prefix


@pytest.fixture
def segmented_bundle(tmp_path):
    return build_project(create(tmp_path))


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [
    r"segment-0000\package\pattern.bin", "../outside.bin", "C:/outside.bin", "/outside.bin",
])
def test_nonportable_segment_reference_has_context_before_legacy_read(
        segmented_bundle, monkeypatch, field, value):
    import stpg_runner.build as builder

    root = segmented_bundle.bundle_dir
    bundle = read_json(root / "runner.bundle.json")
    manifest_path = root / bundle["package_manifest"]
    segmented = read_json(manifest_path)
    record = segmented["segments"][0]
    if field == "wait_schedule.path":
        record["wait_schedule"]["path"] = value
    else:
        record[field] = value
    write_json(manifest_path, segmented)
    bundle["files"][bundle["package_manifest"]] = identity(manifest_path)
    write_json(root / "runner.bundle.json", bundle)

    def forbidden(*args, **kwargs):
        raise AssertionError("untrusted path reached legacy segmented reader")

    monkeypatch.setattr(builder, "verify_segmented_manifest", forbidden)
    monkeypatch.setattr(builder, "load_manifest", forbidden)
    result = verify_bundle(root)
    assert result["status"] == "REJECT" and len(result["errors"]) == 1
    assert f"segment[0].{field}:" in result["errors"][0]
    assert repr(value) in result["errors"][0]
    assert result["device_io_performed"] is False


@pytest.mark.parametrize("value", [None, "", "a\\b.bin", "bad\x00.bin"])
def test_portable_path_first_error_retains_value(value):
    with pytest.raises(RunnerError) as caught:
        portable_path(value)
    assert repr(value) in str(caught.value)
