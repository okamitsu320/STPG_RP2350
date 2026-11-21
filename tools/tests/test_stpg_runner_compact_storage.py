"""Lossless public source storage and strict snapshot/receipt provenance."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import shutil

import pytest

from stil import source_artifacts as source_io
from stil.json_artifacts import read_json_artifact
from stil.pipeline import CompileOptions, compile_stil
from stil.segmented import verify_segmented_manifest
from stil.segmented_package import build_segmented_packages
from stpg_runner import build_project, init_project, load_project, prepare_run, sdk_info, verify_bundle
from stpg_runner import build as builder
from stpg_runner.artifacts import RunnerError, canonical_json, identity, read_json, write_json
from stpg_runner.cli import main
from stpg_runner.project import project_template
from stpg_runner.source_storage import SOURCE_STORAGE_NAME, bundle_source_paths
from helpers.benchmark_runner_verification import synthetic_source

pytestmark = pytest.mark.stpg_host_only


def public_project(tmp_path, *, legacy=False, count=4000, pin_map=False):
    source = tmp_path / "public.stil"
    source.write_text(synthetic_source(count), encoding="utf-8")
    pins = tmp_path / "pins.json"
    if pin_map:
        write_json(pins, {"active_pins": 16, "signals": {"A": {"bit": 0}, "B": {"bit": 1}},
                          "public_padding": "synthetic padding, not a signal; " * 2000})
    root = tmp_path / "p"
    if legacy:
        (root / "inputs").mkdir(parents=True)
        shutil.copyfile(source, root / "inputs/pattern.stil")
        project = root / "project.stpg-project.json"
        write_json(project, project_template(stil="inputs/pattern.stil", wait_preset="none", completion="hold").to_dict())
    else:
        project = init_project(source, root, wait_preset="none", completion="hold", pin_map=pins if pin_map else None)
    return source, project


@pytest.mark.parametrize("legacy", [False, True])
def test_new_and_legacy_projects_have_lossless_compressed_self_contained_snapshots(tmp_path, legacy):
    source, project_path = public_project(tmp_path, legacy=legacy)
    project_bytes = project_path.read_bytes()
    original_input = load_project(project_path).input_paths(project_path.parent)["stil"]
    expected_bytes = source.read_bytes()
    if legacy:
        assert original_input.suffix == ".stil"
    else:
        assert original_input.suffix == ".gz" and original_input.stat().st_size < len(expected_bytes) // 4
    result = build_project(project_path)
    assert project_path.read_bytes() == project_bytes  # no implicit input/settings migration
    assert original_input.is_file()
    storage = read_json(result.bundle_dir / SOURCE_STORAGE_NAME)["inputs"]["stil"]
    assert storage["input_encoding"] == ("identity" if legacy else "gzip") and storage["encoding"] == "gzip"
    snapshots = bundle_source_paths(result.bundle_dir)
    assert gzip.decompress(snapshots["stil"].read_bytes()) == expected_bytes
    assert snapshots["stil"].suffix == ".gz"
    actual_binary = next(result.bundle_dir.rglob("pattern.bin")).read_bytes()
    expected_binary, _, _ = compile_stil(CompileOptions(input_path=source, mode="auto", loop_policy="preserve", strict=True))
    assert actual_binary == expected_binary  # storage must not alter any pattern bit
    copy = tmp_path / "relocated"
    shutil.copytree(result.bundle_dir, copy)
    # Original files become unavailable; no fallback to the external project.
    original_input.rename(original_input.with_name("original-unavailable"))
    assert verify_bundle(copy, manifest_sha256=result.manifest_sha256)["status"] == "PASS"
    plan = prepare_run(copy, manifest_sha256=result.manifest_sha256)
    assert plan.segments[0].payload == actual_binary


def test_large_pin_map_and_plain_small_legacy_paths_are_both_supported(tmp_path):
    source, project = public_project(tmp_path, pin_map=True)
    inputs = load_project(project).input_paths(project.parent)
    assert inputs["pin_map"].suffix == ".gz"
    assert read_json(inputs["pin_map"])["signals"] == {"A": {"bit": 0}, "B": {"bit": 1}}
    result = build_project(project)
    assert bundle_source_paths(result.bundle_dir)["pin_map"].suffix == ".gz"
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"
    small = tmp_path / "small.stil"
    small.write_text(synthetic_source(8), encoding="utf-8")
    path = init_project(small, tmp_path / "small", wait_preset="none", completion="hold")
    assert load_project(path).to_dict()["input"]["stil"] == "inputs/pattern.stil"


def test_old_bundle_without_storage_descriptor_still_checks_exact_raw_sources(tmp_path):
    _, project = public_project(tmp_path, count=8, legacy=True)
    result = build_project(project)
    (result.bundle_dir / SOURCE_STORAGE_NAME).unlink()
    manifest = read_json(result.bundle_dir / "runner.bundle.json")
    del manifest["files"][SOURCE_STORAGE_NAME]
    write_json(result.bundle_dir / "runner.bundle.json", manifest)
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"
    source = bundle_source_paths(result.bundle_dir)["stil"]
    source.write_text(source.read_text() + "\n// changed raw legacy source\n", encoding="utf-8")
    assert verify_bundle(result.bundle_dir)["status"] == "REJECT"


@pytest.mark.parametrize("crlf", [False, True])
def test_compression_is_deterministic_verbatim_and_never_nested(tmp_path, crlf):
    content = ("// 公開の合成データ\n" * 4000).replace("\n", "\r\n" if crlf else "\n").encode("utf-8")
    source = tmp_path / "source.stil"
    source.write_bytes(content)
    one, info = source_io.copy_source_artifact(source, tmp_path / "one/input.stil")
    two, _ = source_io.copy_source_artifact(source, tmp_path / "日本語/input.stil")
    assert one.read_bytes() == two.read_bytes() and gzip.decompress(one.read_bytes()) == content
    assert info["decoded"] == identity(source)
    already = tmp_path / "already.stil.gz"
    already.write_bytes(gzip.compress(content, mtime=1234))
    copied, copied_info = source_io.copy_source_artifact(already, tmp_path / "copied/input.stil")
    assert copied.read_bytes() == already.read_bytes()  # do not repin supplied compressed bytes
    assert copied_info["input_encoding"] == "gzip" and copied_info["decoded"] == identity(source)
    assert source_io.read_source_text(copied) == content.decode().replace("\r\n", "\n")


@pytest.mark.parametrize("bad", [b"not gzip", b"\x1f\x8b", gzip.compress(b"{}", mtime=0)[:-4]])
def test_corrupt_compression_rejects_without_plain_fallback(tmp_path, bad):
    source = tmp_path / "source.stil.gz"
    source.write_bytes(bad)
    with pytest.raises((ValueError, OSError)):
        source_io.copy_source_artifact(source, tmp_path / "copy/source.stil")
    with pytest.raises((ValueError, OSError)):
        source_io.read_source_text(source)


def test_source_destination_is_never_overwritten(tmp_path):
    source = tmp_path / "public.stil"
    source.write_text(synthetic_source(8), encoding="utf-8")
    target = tmp_path / "target.stil.gz"
    target.write_bytes(b"reserved")
    with pytest.raises(FileExistsError):
        source_io.copy_source_artifact(source, target)
    assert target.read_bytes() == b"reserved"


def rehash_member(root, member):
    manifest_path = root / "runner.bundle.json"
    manifest = read_json(manifest_path)
    manifest["files"][member] = identity(root / member)
    write_json(manifest_path, manifest)


@pytest.mark.parametrize("case", ["path_parent", "path_windows", "encoding", "decoded", "raw_binding", "roles", "alias",
                                  "entry_null", "bool_size", "unknown_field", "bad_version", "bool_version", "nested_gzip"])
def test_rehashed_storage_tampering_is_rejected_semantically(tmp_path, case):
    _, project = public_project(tmp_path, legacy=case == "decoded", pin_map=case == "alias")
    result = build_project(project)
    root = result.bundle_dir
    assert verify_bundle(root, reuse=True)["status"] == "PASS"
    path = root / SOURCE_STORAGE_NAME
    data = read_json(path)
    entry = data["inputs"]["stil"]
    if case == "path_parent":
        entry["path"] = "../package/pattern.bin"
    elif case == "path_windows":
        entry["path"] = "inputs\\pattern.stil.gz"
    elif case == "encoding":
        entry["encoding"] = "identity"
    elif case == "decoded":
        entry["decoded"]["sha256"] = "0" * 64
    elif case == "raw_binding":
        # Same decoded bytes, different gzip member: supplied gzip input binding
        # is stored-byte exact, not a permission to recompress an approved input.
        snapshot = root / "sources" / entry["path"]
        snapshot.write_bytes(gzip.compress(gzip.decompress(snapshot.read_bytes()), mtime=99))
        rehash_member(root, snapshot.relative_to(root).as_posix())
    elif case == "roles":
        data["inputs"]["extra"] = deepcopy(entry)
    elif case == "alias":
        data["inputs"]["pin_map"]["path"] = entry["path"]
    elif case == "entry_null":
        data["inputs"]["stil"] = None
    elif case == "bool_size":
        entry["decoded"]["size_bytes"] = True
    elif case == "unknown_field":
        entry["unchecked"] = True
    elif case == "bad_version":
        data["schema_version"] = 2
    elif case == "nested_gzip":
        snapshot = root / "sources" / entry["path"]
        entry["input_encoding"] = "identity"
        entry["decoded"] = identity(snapshot)  # nested member decodes to the bound gzip bytes, not STIL
        snapshot.write_bytes(gzip.compress(snapshot.read_bytes(), mtime=0))
        rehash_member(root, snapshot.relative_to(root).as_posix())
    else:
        data["schema_version"] = True
    write_json(path, data)
    rehash_member(root, SOURCE_STORAGE_NAME)
    assert verify_bundle(root, reuse=True)["status"] == "REJECT"


def test_input_change_stops_before_compile_and_keeps_failed_reservation(tmp_path, monkeypatch):
    _, project = public_project(tmp_path, legacy=True)
    original_input = load_project(project).input_paths(project.parent)["stil"]
    original_bytes = original_input.read_bytes()
    original = builder.copy_source_artifact
    def changed(source, destination):
        result = original(source, destination)
        source.write_bytes(original_bytes + b"\n// public concurrent edit\n")
        return result
    monkeypatch.setattr(builder, "copy_source_artifact", changed)
    def forbidden(*args, **kwargs):
        pytest.fail("compile must not follow a changed input snapshot")
    monkeypatch.setattr(builder, "_packages", forbidden)
    with pytest.raises(RunnerError, match="input changed"):
        build_project(project)
    root, = (project.parent / "build").iterdir()
    assert read_json(root / "build.state.json")["status"] == "FAILED"
    prefix = {p.relative_to(root).as_posix(): identity(p) for p in root.rglob("*") if p.is_file()}
    original_input.write_bytes(original_bytes)
    with pytest.raises(RunnerError, match="existing cache is incomplete/corrupt"):
        build_project(project)
    assert prefix == {p.relative_to(root).as_posix(): identity(p) for p in root.rglob("*") if p.is_file()}


def test_large_segmented_wait_json_uses_real_compressed_paths_and_verifies(tmp_path):
    source = tmp_path / "public.stil"
    source.write_text(synthetic_source(1600), encoding="utf-8")
    output = tmp_path / "segments"
    report = build_segmented_packages(
        input_path=source, output_dir=output, report_path=tmp_path / "segmented.report.json",
        ranges=((0, 799), (800, 1599)), total_vectors=1600, mode="rate-vector", loop_policy="preserve",
        tick_hz=200000000, chunk_event_capacity=4, service_time_us=0.5,
        service_time_source="public declared assumption, not measured", max_wait_us=4000,
        max_total_wait_us=1e11, segment_wait_plan="every-boundary", compact_sidecars=True)
    nominal = output / "nominal"
    assert (nominal / "segment.wait.plan.json.gz").is_file()
    assert not (nominal / "segment.wait.plan.json").exists()
    plan = report["variants"][0]["segment_wait_plan"]
    assert plan["path"].endswith(".json.gz") and identity(Path(plan["path"]))["sha256"] == plan["sha256"]
    for directory in nominal.glob("segment-*"):
        assert (directory / "projected.wait.json.gz").is_file()
        assert not (directory / "projected.wait.json").exists()
        assert read_json_artifact(directory / "projected.wait.acceptance.json")["status"] == "ACCEPTED"
    assert (nominal / "segmented.manifest.json").is_file()
    assert verify_segmented_manifest(nominal / "segmented.manifest.json")["status"] == "PASS"


def test_local_sdk_diagnostic_has_no_project_or_device_access(capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("version must not inspect a project")
    monkeypatch.setattr(builder, "load_project", forbidden)
    assert main(["version"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tool_sha256"] == builder.tool_fingerprint()
    assert report["device_io_performed"] is False and report["physical_execution_authorized"] is False
    assert Path(report["sdk_directory"]).is_dir() and report["python"]
    monkeypatch.setattr(builder, "tool_fingerprint", lambda: "f" * 64)
    assert sdk_info()["code_changed_since_import"] is True


def test_source_roles_cannot_be_removed_by_rehashing_the_bundle_binding(tmp_path):
    _, project = public_project(tmp_path, pin_map=True)
    result = build_project(project)
    manifest_path = result.bundle_dir / "runner.bundle.json"
    manifest = read_json(manifest_path)
    del manifest["binding"]["inputs"]["pin_map"]
    import hashlib
    key = hashlib.sha256(canonical_json(manifest["binding"])).hexdigest()
    manifest["cache_key"] = key
    write_json(result.bundle_dir / "build.state.json", {"status": "HOST_ONLY_VERIFIED", "cache_key": key})
    storage_path = result.bundle_dir / SOURCE_STORAGE_NAME
    storage = read_json(storage_path)
    del storage["inputs"]["pin_map"]
    write_json(storage_path, storage)
    for member in (SOURCE_STORAGE_NAME, "build.state.json"):
        manifest["files"][member] = identity(result.bundle_dir / member)
    write_json(manifest_path, manifest)
    assert verify_bundle(result.bundle_dir)["status"] == "REJECT"


def test_large_incompressible_bytes_are_not_larger_or_reencoded(tmp_path):
    import random
    data = random.Random(177).randbytes(70000)
    source = tmp_path / "public.stil"
    source.write_bytes(data)
    stored, record = source_io.copy_source_artifact(source, tmp_path / "stored/input.stil")
    assert stored.suffix == ".stil" and stored.read_bytes() == data
    assert record["encoding"] == "identity" and record["decoded"] == identity(source)


def test_compressed_json_keeps_strict_keys_and_checks_the_stored_hash(tmp_path):
    target = tmp_path / "pins.json.gz"
    target.write_bytes(gzip.compress(b'{"x":1,"x":2}', mtime=0))
    with pytest.raises(RunnerError, match="duplicate JSON key"):
        read_json(target)
    target.write_bytes(gzip.compress(b'{"x":1}', mtime=0))
    digest = identity(target)["sha256"]
    assert read_json(target, expected_sha256=digest) == {"x": 1}
    target.write_bytes(gzip.compress(b'{"x":2}', mtime=0))
    with pytest.raises(RunnerError, match="SHA-256 mismatch"):
        read_json(target, expected_sha256=digest)


def test_rate_variant_reader_preserves_plain_and_gzip_input_behavior(tmp_path):
    from stil.rate_sweep import derive_rate_variant, parse_variant_spec
    source = tmp_path / "public.stil"
    source.write_text(synthetic_source(10), encoding="utf-8")
    compressed = tmp_path / "public.stil.gz"
    compressed.write_bytes(gzip.compress(source.read_bytes(), mtime=0))
    first, second = tmp_path / "first.stil", tmp_path / "second.stil"
    variant = parse_variant_spec("public-slow=NORMAL:250ns")
    derive_rate_variant(source, first, variant, tick_hz=200000000)
    derive_rate_variant(compressed, second, variant, tick_hz=200000000)
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize("mutation", ["copy_raw", "skip_roles", "skip_decoded_binding"])
def test_live_storage_mutants_are_detected_by_positive_or_negative_acceptance(tmp_path, monkeypatch, mutation):
    if mutation == "copy_raw":
        original = builder.copy_source_artifact
        def mutant(*args, **kwargs):
            return original(*args, **kwargs, min_compress_bytes=10 ** 12)
        monkeypatch.setattr(builder, "copy_source_artifact", mutant)
        with pytest.raises(AssertionError):
            test_new_and_legacy_projects_have_lossless_compressed_self_contained_snapshots(tmp_path, False)
    elif mutation == "skip_roles":
        original = builder.bundle_source_paths
        def mutant(root, *, manifest):
            binding = manifest["binding"]
            if binding["project"]["input"].get("pin_map") is not None and "pin_map" not in binding["inputs"]:
                return {"stil": root / "sources" / binding["inputs"]["stil"]["path"]}
            return original(root, manifest=manifest)
        monkeypatch.setattr(builder, "bundle_source_paths", mutant)
        with pytest.raises(AssertionError):
            test_source_roles_cannot_be_removed_by_rehashing_the_bundle_binding(tmp_path)
    else:
        import stpg_runner.source_storage as storage
        original = storage.decoded_source_identity
        def mutant(path):
            actual = original(path)
            return {**actual, "sha256": "0" * 64}
        monkeypatch.setattr(storage, "decoded_source_identity", mutant)
        # The positive build must no longer get semantic acceptance.
        with pytest.raises(ValueError, match="bundle verification rejected"):
            test_new_and_legacy_projects_have_lossless_compressed_self_contained_snapshots(tmp_path, True)


def test_nested_member_cannot_hide_behind_an_original_path_without_gzip_suffix(tmp_path):
    import hashlib
    from stpg_runner.source_storage import SOURCE_STORAGE_KIND
    # The original supplied input was gzip by magic, despite its .stil name.
    # A forged descriptor must not reinterpret those bound bytes as plain STIL.
    original = gzip.compress(synthetic_source(8).encode(), mtime=0)
    original_identity = {"size_bytes": len(original), "sha256": hashlib.sha256(original).hexdigest()}
    snapshot = tmp_path / "sources/inputs/public.stil.gz"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(gzip.compress(original, mtime=0))
    write_json(tmp_path / SOURCE_STORAGE_NAME, {
        "kind": SOURCE_STORAGE_KIND, "schema_version": 1,
        "inputs": {"stil": {"path": "inputs/public.stil.gz", "input_encoding": "identity",
                             "encoding": "gzip", "decoded": original_identity}},
    })
    manifest = {
        "binding": {"project": {"input": {"stil": "inputs/public.stil"}},
                    "inputs": {"stil": {"path": "inputs/public.stil", **original_identity}}},
        "files": {SOURCE_STORAGE_NAME: identity(tmp_path / SOURCE_STORAGE_NAME),
                  "sources/inputs/public.stil.gz": identity(snapshot)},
    }
    with pytest.raises(RunnerError, match="nested gzip"):
        bundle_source_paths(tmp_path, manifest=manifest)
