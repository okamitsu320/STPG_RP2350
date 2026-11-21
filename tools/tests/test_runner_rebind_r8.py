"""Synthetic immutable bundles; no compiler invocation during migration."""
from copy import deepcopy
import hashlib
from pathlib import Path
import shutil

import pytest

from stpg_runner import (CancellationToken, build_project, init_project, prepare_run,
                         rebind_bundle, verify_bundle)
from stpg_runner.artifacts import RunnerError, canonical_json, identity, read_json, write_json
from stpg_runner.build import BUNDLE_NAME, tool_fingerprint
from stpg_runner.cli import main
from stpg_runner.progress import BuildCancelled

pytestmark = pytest.mark.stpg_host_only
FIXTURE = Path(__file__).parent / "fixtures/runner/pattern.stil"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("r8-rebind")
    path = init_project(FIXTURE, root / "project", wait_preset="none", completion="release")
    return build_project(path)


def inventory(root):
    return {path.relative_to(root).as_posix(): identity(path) for path in root.rglob("*") if path.is_file()}


def older(built, tmp_path):
    """Unit fixture with a different SDK binding, not a claim of old compiler execution."""
    root = tmp_path / "old"
    shutil.copytree(built.bundle_dir, root)
    manifest = read_json(root / BUNDLE_NAME)
    manifest["binding"]["tool_sha256"] = "0" * 64
    manifest["binding"]["python"] = "3.10.0"
    manifest["cache_key"] = hashlib.sha256(canonical_json(manifest["binding"])).hexdigest()
    write_json(root / "build.state.json", {"status": "HOST_ONLY_VERIFIED", "cache_key": manifest["cache_key"]})
    manifest["files"]["build.state.json"] = identity(root / "build.state.json")
    write_json(root / BUNDLE_NAME, manifest)
    assert verify_bundle(root)["status"] == "PASS"
    return root, identity(root / BUNDLE_NAME)["sha256"]


def test_explicit_rebind_passes_without_compiling_or_modifying_original(built, tmp_path, monkeypatch):
    root, pin = older(built, tmp_path)
    before = inventory(root)
    with pytest.raises(RunnerError, match="differs"):
        prepare_run(root, manifest_sha256=pin)
    import stpg_runner.build as builder
    def forbidden(*args, **kwargs):
        pytest.fail("rebind called the compiler")
    for name in ("_packages", "build_pattern_package", "build_segmented_packages"):
        monkeypatch.setattr(builder, name, forbidden)
    result = rebind_bundle(root, manifest_sha256=pin)
    assert inventory(root) == before
    assert result.bundle_dir.parent == root.parent and result.bundle_dir != root
    assert verify_bundle(result.bundle_dir, manifest_sha256=result.manifest_sha256)["status"] == "PASS"
    plan = prepare_run(result.bundle_dir, manifest_sha256=result.manifest_sha256)
    current, original = read_json(result.bundle_dir / BUNDLE_NAME), read_json(root / BUNDLE_NAME)
    assert current["binding"]["tool_sha256"] == tool_fingerprint()
    assert current["binding"]["rebind"]["source_manifest_sha256"] == pin
    assert current["binding"]["rebind"]["compile_tool_sha256"] == "0" * 64
    assert current["binding"]["rebind"]["compile_python"] == "3.10.0"
    assert current["cache_key"] != original["cache_key"]
    assert current["cache_key"] == result.bundle_dir.name
    for item in original["patterns"]:
        assert (root / item["path"]).read_bytes() == (result.bundle_dir / item["path"]).read_bytes()
    assert plan.segments
    with pytest.raises(FileExistsError):
        rebind_bundle(root, manifest_sha256=pin)


def test_same_sdk_is_verified_noop(built, tmp_path):
    no_output = tmp_path / "unused"
    result = rebind_bundle(built.bundle_dir, manifest_sha256=built.manifest_sha256, output_dir=no_output)
    assert result.bundle_dir == built.bundle_dir and result.cache_hit
    assert not no_output.exists()


@pytest.mark.parametrize("change", ["pin", "extra", "symlink", "corrupt", "schema", "state"])
def test_old_bundle_rejection_happens_before_output(built, tmp_path, change):
    root, pin = older(built, tmp_path)
    if change == "pin":
        pin = "1" * 64
    elif change == "extra":
        (root / "extra.txt").write_text("not inventoried")
    elif change == "symlink":
        (root / "extra.txt").symlink_to(root / "project.json")
    elif change == "corrupt":
        manifest = read_json(root / BUNDLE_NAME)
        (root / manifest["patterns"][0]["path"]).write_bytes(b"corrupt")
    elif change == "schema":
        manifest = read_json(root / BUNDLE_NAME)
        manifest["schema_version"] = 99
        write_json(root / BUNDLE_NAME, manifest)
        pin = identity(root / BUNDLE_NAME)["sha256"]
    else:
        write_json(root / "build.state.json", {"status": "FAILED"})
    output = tmp_path / "new"
    with pytest.raises(RunnerError):
        rebind_bundle(root, manifest_sha256=pin, output_dir=output)
    assert not output.exists()


@pytest.mark.parametrize("target", ["same", "inside", "parent", "existing", "symlink"])
def test_invalid_destination_never_overwrites_old_bundle(built, tmp_path, target):
    root, pin = older(built, tmp_path)
    output = {"same": root, "inside": root / "nested", "parent": root.parent,
              "existing": tmp_path / "exists", "symlink": tmp_path / "link"}[target]
    if target == "existing":
        output.mkdir()
    if target == "symlink":
        output.symlink_to(tmp_path / "must-not-create")
    before = inventory(root)
    with pytest.raises((RunnerError, FileExistsError)):
        rebind_bundle(root, manifest_sha256=pin, output_dir=output)
    assert inventory(root) == before
    assert not (tmp_path / "must-not-create").exists()


def test_cancellation_leaves_a_rejected_prefix_not_a_runnable_bundle(built, tmp_path):
    root, pin = older(built, tmp_path)
    token = CancellationToken()
    def progress(event):
        if event.stage == "rebind_copy" and event.event == "progress":
            token.cancel()
    output = tmp_path / "new"
    with pytest.raises(BuildCancelled):
        rebind_bundle(root, manifest_sha256=pin, output_dir=output, on_progress=progress, cancel=token)
    assert output.is_dir() and verify_bundle(output)["status"] == "REJECT"
    with pytest.raises(FileExistsError):
        rebind_bundle(root, manifest_sha256=pin, output_dir=output)


def test_rebind_origin_tampering_is_not_accepted(built, tmp_path):
    root, pin = older(built, tmp_path)
    result = rebind_bundle(root, manifest_sha256=pin, output_dir=tmp_path / "new")
    manifest = read_json(result.bundle_dir / BUNDLE_NAME)
    manifest["binding"]["rebind"]["compile_tool_sha256"] = "f" * 64
    manifest["cache_key"] = hashlib.sha256(canonical_json(manifest["binding"])).hexdigest()
    write_json(result.bundle_dir / "build.state.json", {"status": "HOST_ONLY_VERIFIED", "cache_key": manifest["cache_key"]})
    manifest["files"]["build.state.json"] = identity(result.bundle_dir / "build.state.json")
    write_json(result.bundle_dir / BUNDLE_NAME, manifest)
    assert verify_bundle(result.bundle_dir)["status"] == "REJECT"


def test_cli_rebind_names_new_bundle(built, tmp_path, capsys):
    root, pin = older(built, tmp_path)
    assert main(["rebind", str(root), "--manifest-sha256", pin, "--output-dir", str(tmp_path / "new"),
                 "--progress", "none"]) == 0
    import json
    assert Path(json.loads(capsys.readouterr().out)["bundle_dir"]) == tmp_path / "new"


def test_second_migration_preserves_original_compile_identity(built, tmp_path):
    root, pin = older(built, tmp_path)
    first = rebind_bundle(root, manifest_sha256=pin, output_dir=tmp_path / "first")
    # Synthetic intervening SDK identity, not an actual compiler execution.
    manifest = read_json(first.bundle_dir / BUNDLE_NAME)
    manifest["binding"]["tool_sha256"] = "1" * 64
    manifest["cache_key"] = hashlib.sha256(canonical_json(manifest["binding"])).hexdigest()
    write_json(first.bundle_dir / "build.state.json", {"status": "HOST_ONLY_VERIFIED", "cache_key": manifest["cache_key"]})
    manifest["files"]["build.state.json"] = identity(first.bundle_dir / "build.state.json")
    write_json(first.bundle_dir / BUNDLE_NAME, manifest)
    assert verify_bundle(first.bundle_dir)["status"] == "PASS"
    second = rebind_bundle(first.bundle_dir, manifest_sha256=identity(first.bundle_dir / BUNDLE_NAME)["sha256"],
                           output_dir=tmp_path / "second")
    assert verify_bundle(second.bundle_dir)["status"] == "PASS"
    value = read_json(second.bundle_dir / BUNDLE_NAME)["binding"]["rebind"]
    assert value["compile_tool_sha256"] == "0" * 64
    assert value["source_tool_sha256"] == "1" * 64
    assert len(list((second.bundle_dir / "provenance").iterdir())) == 2
