"""Public synthetic result storage, strict compatibility and STIL mapping."""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from stpg_runner import build_project, describe_result, init_project, read_run_result, run_result_path
from stpg_runner.artifacts import RunnerError, write_json
from stpg_runner.results import write_run_result
from stpg_runner.cli import main
from test_stpg_runner_execution import Clock, DEVICE, FirmwarePeer, ports
from stpg_runner import run_bundle

pytestmark = pytest.mark.stpg_host_only
FIXTURE = Path(__file__).parent / "fixtures/runner/pattern.stil"


def minimal():
    return {"kind": "stpg_runner_run_result", "schema_version": 1, "sample": [0] * 5000}


def test_new_record_is_only_deterministic_atomic_gzip(tmp_path):
    value = minimal()
    path = write_run_result(tmp_path, value)
    first = path.read_bytes()
    assert path.name == "run.json.gz" and not (tmp_path / "run.json").exists()
    assert first[4:8] == b"\0" * 4
    assert read_run_result(tmp_path) == value
    assert read_run_result(path, expected_sha256=hashlib.sha256(first).hexdigest()) == value
    assert len(first) < len(gzip.decompress(first)) / 5
    write_run_result(tmp_path, value)
    assert path.read_bytes() == first and not list(tmp_path.glob(".stpg-*"))


def test_atomic_failure_preserves_previous_checkpoint(tmp_path, monkeypatch):
    import stpg_runner.artifacts as artifacts
    first = write_run_result(tmp_path, minimal()).read_bytes()
    def fail(*args):
        raise OSError("synthetic rename failure")
    monkeypatch.setattr(artifacts.os, "replace", fail)
    with pytest.raises(OSError):
        write_run_result(tmp_path, {**minimal(), "changed": True})
    assert (tmp_path / "run.json.gz").read_bytes() == first
    assert not list(tmp_path.glob(".stpg-*"))


def test_legacy_and_ambiguous_explicit_path(tmp_path):
    plain = tmp_path / "run.json"
    write_json(plain, minimal())
    assert read_run_result(tmp_path) == minimal() and run_result_path(plain) == plain
    write_json(tmp_path / "run.json.gz", minimal())
    for path in (tmp_path, plain, tmp_path / "run.json.gz"):
        with pytest.raises(RunnerError, match="ambiguous"):
            read_run_result(path)
    with pytest.raises(RunnerError):
        write_run_result(tmp_path, minimal())


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("payload", [b'{"a":1,"a":2}', b'{"x":NaN}', b'[]', b'{}', b'{broken', b'\xff'])
def test_bad_json_is_not_adopted(tmp_path, compressed, payload):
    path = tmp_path / ("run.json.gz" if compressed else "run.json")
    path.write_bytes(gzip.compress(payload) if compressed else payload)
    with pytest.raises(RunnerError):
        read_run_result(path)


def test_corrupt_gzip_missing_and_wrong_hash(tmp_path):
    with pytest.raises(RunnerError):
        read_run_result(tmp_path)
    path = write_run_result(tmp_path, minimal())
    with pytest.raises(RunnerError, match="SHA-256"):
        read_run_result(path, expected_sha256="0" * 64)
    path.write_bytes(path.read_bytes()[:-2])
    with pytest.raises(RunnerError):
        read_run_result(path)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("r8-public-results")
    project = init_project(FIXTURE, root / "project", wait_preset="none", completion="release")
    return build_project(project)


def simulate(built, directory):
    clock, peer = Clock(), FirmwarePeer()
    return run_bundle(built.bundle_dir, manifest_sha256=built.manifest_sha256, device=DEVICE,
                      output_dir=directory, confirm_run=True, stream_factory=lambda _: peer,
                      port_provider=ports, clock=clock, sleep=clock.sleep)


def test_description_uses_exact_bundle_and_redacts_paths_and_usb(built, tmp_path, capsys):
    report = simulate(built, tmp_path / "run")
    assert report["outcome"] == "COMPLETED", report["errors"]
    description = describe_result(built.bundle_dir, manifest_sha256=built.manifest_sha256, result=tmp_path / "run")
    segment = description["segments"][0]
    assert segment["source_range"]["first"]["global_vector_index"] == 0
    assert segment["source_range"]["last"]["global_vector_index"] == 3
    assert segment["source_range"]["first"]["stil_line"] == 19
    assert segment["fail_log"]["status"] == "NOT_NEEDED"
    text = json.dumps(description)
    for forbidden in (str(built.bundle_dir), str(tmp_path), DEVICE.serial_number, DEVICE.port, 'input_file', 'source_path'):
        assert forbidden not in text
    assert main(["describe", str(built.bundle_dir), "--manifest-sha256", built.manifest_sha256,
                 "--result", str(tmp_path / "run")]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "stpg_result_description"
    legacy = deepcopy(report)
    del legacy["segments"][0]["fail_log"]
    assert describe_result(built.bundle_dir, manifest_sha256=built.manifest_sha256, result=legacy)["segments"][0]["fail_log"]["status"] == "UNAVAILABLE_LEGACY"


@pytest.mark.parametrize("change", ["pin", "pattern", "expected", "index"])
def test_description_rejects_foreign_or_edited_result(built, tmp_path, change):
    report = simulate(built, tmp_path / "run")
    if change == "pin":
        report["plan"]["manifest_sha256"] = "0" * 64
    elif change == "pattern":
        report["plan"]["segments"][0]["sha256"] = "0" * 64
    elif change == "expected":
        report["segments"][0]["expected"]["events"] += 1
    else:
        report["segments"][0]["index"] = 9
    with pytest.raises(RunnerError):
        describe_result(built.bundle_dir, manifest_sha256=built.manifest_sha256, result=report)


@pytest.mark.parametrize("section,key,value", [
    ("root", "outcome", {}), ("root", "comparison", []), ("root", "transactions", [None]),
    ("plan", "segments", [None]), ("segment", "terminal", None),
    ("segment", "outcome", {}), ("segment", "comparison", []),
    ("segment", "fail_log", 0), ("segment", "fail_log", {}),
    ("log", "status", []), ("log", "entries", [None]),
])
def test_description_malformed_structures_are_explicit_rejections(built, tmp_path, section, key, value):
    report = simulate(built, tmp_path / "run")
    target = {"root": report, "plan": report["plan"], "segment": report["segments"][0],
              "log": report["segments"][0]["fail_log"]}[section]
    target[key] = value
    with pytest.raises(RunnerError):
        describe_result(built.bundle_dir, manifest_sha256=built.manifest_sha256, result=report)
