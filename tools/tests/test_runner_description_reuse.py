"""Owned, bounded offline result reuse; changed bytes never become a receipt."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shutil

import pytest

from stpg_runner import build_project, describe_result, init_project
from stpg_runner import build, results
from stpg_runner.artifacts import RunnerError, canonical_json, identity, read_json, write_json
from stpg_runner.progress import BuildCancelled, CancellationToken
from test_runner_results_r8 import simulate

pytestmark = pytest.mark.stpg_host_only
FIXTURE = Path(__file__).parent / "fixtures/runner/pattern.stil"


@pytest.fixture(scope="module")
def example(tmp_path_factory):
    root = tmp_path_factory.mktemp("public-description-reuse")
    built = build_project(init_project(FIXTURE, root / "project", wait_preset="none", completion="release"))
    return built, simulate(built, root / "run")


@pytest.fixture
def sample(example, tmp_path):
    built, report = example
    root = tmp_path / "bundle"
    shutil.copytree(built.bundle_dir, root)
    with results._description_lock:
        results._descriptions.clear()
    return root, built.manifest_sha256, deepcopy(report)


def describe(sample, **kwargs):
    root, pin, report = sample
    return describe_result(root, manifest_sha256=pin, result=report, **kwargs)


def count_matching(monkeypatch):
    calls = []
    original = results._describe_result
    def match(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(results, "_describe_result", match)
    return calls


def test_same_snapshot_reuses_matching_but_rehashes_all_bytes(sample, monkeypatch):
    calls = count_matching(monkeypatch)
    inventory = []
    original = build._verify_inventory
    def checked(*args, **kwargs):
        inventory.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(build, "_verify_inventory", checked)
    first = describe(sample)
    n = len(inventory)
    assert describe(sample) == first
    assert len(calls) == 1 and len(inventory) == n + 1
    first["segments"].clear()  # caller never owns a cache value
    assert describe(sample)["segments"] and len(calls) == 1


def test_old_sdk_offline_reuse_is_not_execution_admission(sample, monkeypatch):
    root, _, report = sample
    manifest = read_json(root / build.BUNDLE_NAME)
    manifest["binding"]["tool_sha256"] = "0" * 64
    manifest["cache_key"] = hashlib.sha256(canonical_json(manifest["binding"])).hexdigest()
    write_json(root / "build.state.json", {"status": "HOST_ONLY_VERIFIED", "cache_key": manifest["cache_key"]})
    manifest["files"]["build.state.json"] = identity(root / "build.state.json")
    write_json(root / build.BUNDLE_NAME, manifest)
    pin = identity(root / build.BUNDLE_NAME)["sha256"]
    report["plan"]["manifest_sha256"] = pin
    calls = count_matching(monkeypatch)
    sample = root, pin, report
    assert describe(sample) == describe(sample) and len(calls) == 1
    from stpg_runner import prepare_run
    with pytest.raises(RunnerError, match="differs from the bundle"):
        prepare_run(root, manifest_sha256=pin)


@pytest.mark.parametrize("change", ["same_size_mtime", "missing", "extra", "symlink", "manifest"])
def test_reuse_rejects_changed_bundle_without_matching(sample, monkeypatch, change):
    calls = count_matching(monkeypatch)
    describe(sample)
    root, _, _ = sample
    path = root / next(iter(read_json(root / build.BUNDLE_NAME)["files"]))
    if change == "same_size_mtime":
        stat, payload = path.stat(), path.read_bytes()
        path.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif change == "missing":
        path.unlink()
    elif change == "extra":
        (root / "unlisted.txt").write_text("public")
    elif change == "symlink":
        (root / "linked").symlink_to(path)
    else:
        (root / build.BUNDLE_NAME).write_bytes((root / build.BUNDLE_NAME).read_bytes() + b" ")
    with pytest.raises((RunnerError, OSError)):
        describe(sample)
    assert len(calls) == 1


def test_changed_result_and_installed_sdk_cannot_reuse(sample, monkeypatch):
    calls = count_matching(monkeypatch)
    describe(sample)
    sample[2]["comparison"] = "MISMATCH"
    assert describe(sample)["comparison"] == "MISMATCH" and len(calls) == 2
    original = build.tool_fingerprint
    monkeypatch.setattr(build, "tool_fingerprint", lambda: "f" * 64)
    assert describe(sample)["comparison"] == "MISMATCH" and len(calls) == 3
    monkeypatch.setattr(build, "tool_fingerprint", original)


def test_explicit_fresh_check_revalidates_semantics_and_matching(sample, monkeypatch):
    calls = count_matching(monkeypatch)
    describe(sample)
    original = build._verify_bundle
    semantics = []
    def checked(*args, **kwargs):
        semantics.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(build, "_verify_bundle", checked)
    assert describe(sample, reuse=False) == describe(sample)
    assert len(calls) == 2 and len(semantics) == 1


@pytest.mark.parametrize("warm", [False, True])
def test_cancel_and_callback_failure_do_not_publish_a_description(sample, monkeypatch, warm):
    calls = count_matching(monkeypatch)
    if warm:
        describe(sample)
    token = CancellationToken()
    def cancel(event):
        token.cancel()
    with pytest.raises(BuildCancelled):
        describe(sample, cancel=token, on_progress=cancel)
    assert not results._descriptions
    def fail(event):
        raise RuntimeError("public callback failure")
    with pytest.raises(RuntimeError, match="callback failure"):
        describe(sample, on_progress=fail)
    assert not results._descriptions
    assert describe(sample)["segments"]


@pytest.mark.parametrize("warm", [False, True])
def test_callback_mutation_is_detected_after_mapping_or_reuse(sample, warm):
    if warm:
        describe(sample)
    def changed(event):
        if event.event == "done":
            (sample[0] / "callback-added.txt").write_text("public")
    with pytest.raises(RunnerError):
        describe(sample, on_progress=changed)
    assert not results._descriptions


def test_mapping_failure_and_sdk_change_during_description_are_not_cached(sample, monkeypatch):
    original = results._describe_result
    def fail(*args, **kwargs):
        raise RunnerError("synthetic mapping failure")
    monkeypatch.setattr(results, "_describe_result", fail)
    with pytest.raises(RunnerError, match="mapping failure"):
        describe(sample)
    assert not results._descriptions
    monkeypatch.setattr(results, "_describe_result", original)
    fingerprint = [build.tool_fingerprint()]
    monkeypatch.setattr(build, "tool_fingerprint", lambda: fingerprint[0])
    def changed(event):
        if event.event == "done":
            fingerprint[0] = "e" * 64
    with pytest.raises(RunnerError, match="SDK changed"):
        describe(sample, on_progress=changed)
    assert not results._descriptions


def test_cache_is_bounded_and_lru_eviction_rematches(sample, monkeypatch):
    monkeypatch.setattr(results, "_DESCRIPTION_LIMIT", 2)
    calls = count_matching(monkeypatch)
    for label in ("one", "two", "one", "three", "two"):
        sample[2]["public_test_label"] = label
        describe(sample)
    assert len(calls) == 4 and len(results._descriptions) == 2
    monkeypatch.setattr(results, "_DESCRIPTION_BYTES", 1)
    results._descriptions.clear()
    describe(sample, reuse=False)
    sample[2]["public_test_label"] = "oversize"
    describe(sample)
    assert all(sum(map(len, pair)) <= 1 for pair in results._descriptions.values())


def test_snapshot_is_owned_before_progress_callback(sample):
    original = sample[2]["comparison"]
    def changed(event):
        sample[2]["comparison"] = "MISMATCH"
    assert describe(sample, on_progress=changed)["comparison"] == original
    assert describe(sample)["comparison"] == "MISMATCH"


def test_progress_reports_cold_then_reused_mapping(sample):
    events = []
    describe(sample, on_progress=events.append)
    describe(sample, on_progress=events.append)
    assert [(e.stage, e.event) for e in events] == [
        ("result_description", "start"), ("result_description", "done"),
        ("result_description_reuse", "start"), ("result_description_reuse", "done")]


def test_byte_budget_evicts_even_below_entry_count_limit(sample, monkeypatch):
    calls = count_matching(monkeypatch)
    describe(sample)
    size = sum(map(len, next(iter(results._descriptions.values()))))
    monkeypatch.setattr(results, "_DESCRIPTION_BYTES", size + 1)
    sample[2]["public_test_label"] = "two"
    describe(sample)
    assert len(results._descriptions) == 1
    del sample[2]["public_test_label"]
    describe(sample)
    assert len(calls) == 3


def test_stored_result_changes_are_read_again(sample, tmp_path, monkeypatch):
    calls = count_matching(monkeypatch)
    folder = tmp_path / "result"
    folder.mkdir()
    results.write_run_result(folder, sample[2])
    kwargs = {"manifest_sha256": sample[1], "result": folder}
    first = describe_result(sample[0], **kwargs)
    assert describe_result(sample[0], **kwargs) == first and len(calls) == 1
    sample[2]["comparison"] = "MISMATCH"
    results.write_run_result(folder, sample[2])
    assert describe_result(sample[0], **kwargs)["comparison"] == "MISMATCH" and len(calls) == 2


def test_cold_semantics_and_source_span_share_bounded_exact_byte_decoding(sample, monkeypatch):
    import stpg.pattern as pattern
    calls = []
    original = pattern._decode_rate_vector_full
    def decode(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(pattern, "_decode_rate_vector_full", decode)
    assert describe(sample, reuse=False)["segments"]
    assert len(calls) == 1
    describe(sample)
    count = len(calls)
    describe(sample)
    assert len(calls) == count
