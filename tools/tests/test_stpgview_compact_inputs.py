"""Compressed project inputs in the GUI and the project log; public data.

Since SDK 9bf78b6c, ``init_project`` stores a STIL or pin map of at least 32 KiB as deterministic gzip
(``inputs/pattern.stil.gz``) when that is smaller, and the project names the actual file. The GUI must read the
decoded bytes for its form check and Loop hint, take the actual input names for ATE creation and Copy, and keep a
preprocessing record only while the decoded inputs are the ones it describes. These are the four GUI cases of
The SDK's compact-storage behaviour, rewritten on public synthetic data.

The project log (``stpg-view.log.jsonl``) says which SDK files and which Python ran each build.
"""

from __future__ import annotations

import gzip
import json
import os
import platform
from pathlib import Path

import pytest

from stil.cli_preprocess import main as preprocess_main
from stil.source_artifacts import decoded_source_identity
from stpg_runner import CancellationToken, load_project, verify_bundle
from stpg_runner import build as builder
from stpg_runner.artifacts import RunnerError, identity, read_json, write_json
from stpgview.physical.patterns import counter
from stpgview.runner.inputs import _shape
from stpgview.runner.service import PROJECT_LOG, RunnerService, logged_build
from stpgview.session import Session

from stpg_hardware_guard import forbid_hardware

pytestmark = pytest.mark.stpg_host_only

ATE_PROJECT = """STIL 1.0;
Signals {
  A In;
  B Out;
  C In;
}
SignalGroups {
  ALL = 'A + B + C';
}
Timing {
  WaveformTable W1 {
    Period '200ns';
    Waveforms {
      A { 0 { '0ns' D; } }
      A { 1 { '0ns' U; } }
      B { L { '100ns' L; } }
      B { H { '100ns' H; } }
      C { 0 { '0ns' D; } }
      C { 1 { '0ns' U; } }
    }
  }
}
"""
# About 72 KB: above the SDK's 32 KiB threshold, so the converted STIL is stored compressed.
ATE_PATTERN = "STIL 1.0;\nPattern P {\n  W W1;\n" + "  V { ALL = 0L1; }\n  V { ALL = 1H0; }\n" * 2000 + "}\n"


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture
def svc(tmp_path):
    return RunnerService(config_dir=tmp_path / "cfg", device_run=False, demo=False, results_dir=tmp_path / "runs",
                         session=Session(tmp_path / "work"))


@pytest.fixture
def ate(tmp_path):
    folder = tmp_path / "ate"
    folder.mkdir()
    (folder / "proj_project.stil").write_text(ATE_PROJECT, encoding="utf-8")
    (folder / "pat.stil").write_text(ATE_PATTERN, encoding="utf-8")
    return folder / "proj_project.stil", folder / "pat.stil"


def request(directory: Path, **override) -> dict:
    value = {"input_form": "stil", "directory": str(directory), "wait_preset": "none", "completion": "hold",
             "storage": "psram", "compile_mode": "auto", "loop_policy": "preserve", "segmentation": "auto"}
    value.update(override)
    return value


def looped_counter(root: Path) -> Path:
    """About 96 KB of public counter STIL with one Loop block at the end."""

    path = root / "looped.stil"
    path.write_text(counter(4000).removesuffix("}\n") + "  Loop 3 {\n    V { ALL = 01010101; }\n  }\n}\n",
                    encoding="utf-8")
    return path


def stored_inputs(project_path: str | Path) -> dict[str, Path]:
    path = Path(project_path)
    return load_project(path).input_paths(path.parent)


def log_lines(project_dir: str | Path) -> list[dict]:
    return [json.loads(line) for line in (Path(project_dir) / PROJECT_LOG).read_text(encoding="utf-8").splitlines()]


def _wait(job, timeout=120.0):
    import time

    end = time.monotonic() + timeout
    while job.status == "running":
        assert time.monotonic() < end, "job did not finish"
        time.sleep(0.02)
    return job.public(0)


# ---- compressed inputs ------------------------------------------------------------------------

def test_a_compressed_stil_keeps_the_form_check_and_the_loop_hint(svc, tmp_path):
    source = looped_counter(tmp_path)
    first = svc.init_project(request(tmp_path / "first", stil=str(source)))
    stil = stored_inputs(first["project_path"])["stil"]
    assert stil.name == "pattern.stil.gz" and gzip.decompress(stil.read_bytes()) == source.read_bytes()
    assert first["inputs"]["stil"]["path"] == str(stil) and first["stil_loops"] == 1  # read from the decoded bytes
    assert _shape(stil) == _shape(source)  # Signals/Timing/Pattern seen in the decoded text
    # Copy: the form check reads the compressed input (it would reject a Pattern without Signals); the gzip
    # input is kept byte for byte, not compressed again.
    copy = svc.init_project(request(tmp_path / "copy", stil=first["inputs"]["stil"]["path"],
                                    copy_of=first["project_path"]))
    assert stored_inputs(copy["project_path"])["stil"].read_bytes() == stil.read_bytes()
    assert copy["stil_loops"] == 1


def test_a_large_ate_input_is_created_from_the_stored_files_and_builds(svc, tmp_path, ate):
    project, pattern = ate
    info = svc.init_project(request(tmp_path / "new", input_form="ate-pair", stil=str(pattern),
                                    ate_project=str(project)))
    inputs = stored_inputs(info["project_path"])
    assert inputs["stil"].name == "pattern.stil.gz" and inputs["pin_map"].name == "pins.json"  # the map is small
    out = tmp_path / "cli"
    assert preprocess_main([str(project), str(pattern), "--work-dir", str(out), "--pin-map"]) == 0
    (expected,) = out.glob("*.pre.stil")
    assert gzip.decompress(inputs["stil"].read_bytes()) == expected.read_bytes()  # the CUI preprocessor's bytes
    root = Path(info["project_dir"])
    record = read_json(root / "preprocess.json")
    # Output identities are over the decoded bytes, at the logical names; stored_inputs names the actual files.
    assert record["output"]["inputs/pattern.stil"] == decoded_source_identity(inputs["stil"])
    assert record["pin_map"]["inputs/pins.json"] == identity(inputs["pin_map"])
    assert record["stored_inputs"] == {role: {"path": item.relative_to(root).as_posix(), **identity(item)}
                                       for role, item in inputs.items()}
    done = _wait(svc.start_build(info["project_path"]))
    assert done["status"] == "done", done["error"]
    bundle = Path(done["result"]["bundle_dir"])
    assert verify_bundle(bundle, manifest_sha256=done["result"]["manifest_sha256"])["status"] == "PASS"
    assert [p.name for p in (bundle / "sources" / "inputs").iterdir() if p.name.startswith("pattern")] == [
        "pattern.stil.gz"]


def test_a_copy_keeps_the_preprocess_record_only_for_the_same_decoded_inputs(svc, tmp_path, ate):
    project, pattern = ate
    first = svc.init_project(request(tmp_path / "first", input_form="ate-pair", stil=str(pattern),
                                     ate_project=str(project)))
    inputs = stored_inputs(first["project_path"])
    same = svc.init_project(request(tmp_path / "same", stil=str(inputs["stil"]), pin_map=str(inputs["pin_map"]),
                                    copy_of=first["project_path"]))
    record, root = same["preprocess"], Path(same["project_dir"])
    assert record["copied_from"] == first["project_dir"] and record["sources"] == first["preprocess"]["sources"]
    assert record["stored_inputs"] == {role: {"path": item.relative_to(root).as_posix(), **identity(item)}
                                       for role, item in stored_inputs(same["project_path"]).items()}
    # Negative: the same name and codec, one more comment line in the decoded STIL.
    changed = tmp_path / "changed.stil.gz"
    changed.write_bytes(gzip.compress(gzip.decompress(inputs["stil"].read_bytes()) + b"// public change\n",
                                      mtime=0))
    other = svc.init_project(request(tmp_path / "other", stil=str(changed), pin_map=str(inputs["pin_map"]),
                                     copy_of=first["project_path"]))
    assert other["preprocess"] is None and not (Path(other["project_dir"]) / "preprocess.json").exists()


def test_a_plain_project_with_an_old_record_copies_to_compressed_inputs(svc, tmp_path, ate, monkeypatch):
    project, pattern = ate
    store = builder.copy_source_artifact
    with monkeypatch.context() as scope:  # an SDK before 9bf78b6c: inputs stored as they are
        scope.setattr(builder, "copy_source_artifact",
                      lambda *args, **kwargs: store(*args, **kwargs, min_compress_bytes=10 ** 12))
        old = svc.init_project(request(tmp_path / "old", input_form="ate-pair", stil=str(pattern),
                                       ate_project=str(project)))
    inputs = stored_inputs(old["project_path"])
    assert inputs["stil"].name == "pattern.stil"
    record = read_json(Path(old["project_dir"]) / "preprocess.json")
    for key in ("output_identity", "stored_inputs"):  # the schema-1 record of those versions
        record.pop(key)
    write_json(Path(old["project_dir"]) / "preprocess.json", record)
    copy = svc.init_project(request(tmp_path / "copy", stil=str(inputs["stil"]), pin_map=str(inputs["pin_map"]),
                                    copy_of=old["project_path"]))
    assert stored_inputs(copy["project_path"])["stil"].name == "pattern.stil.gz"
    assert copy["preprocess"]["sources"] == record["sources"] and copy["preprocess"]["copied_from"] == old["project_dir"]


# ---- project log --------------------------------------------------------------------------------

def test_each_build_logs_the_sdk_and_python_that_ran_it(svc, tmp_path):
    info = svc.init_project(request(tmp_path / "p", stil=str(looped_counter(tmp_path))))
    first = _wait(svc.start_build(info["project_path"]))
    second = _wait(svc.start_build(info["project_path"]))
    assert first["status"] == second["status"] == "done", (first["error"], second["error"])
    lines = log_lines(info["project_dir"])
    assert [line["status"] for line in lines] == ["started", "HOST_ONLY_VERIFIED"] * 2
    start, end = lines[0], lines[1]
    bundle = Path(first["result"]["bundle_dir"])
    assert start["event"] == end["event"] == "build" and start["project"] == "project.stpg-project.json"
    assert start["workspace"] == "build" and start["platform"] == f"{platform.system()} {platform.release()}"
    assert start["sdk"]["tool_sha256"] == builder.tool_fingerprint() == end["sdk_tool_sha256"]
    assert start["sdk"]["code_changed_since_import"] is False and end["sdk_code_changed_since_import"] is False
    assert start["sdk"]["python"] == platform.python_version()
    assert Path(start["sdk"]["sdk_directory"]) == Path(builder.__file__).resolve().parent
    # The log names what the bundle itself binds, and it is not part of the build identity: a second build is
    # a cache hit of the same reservation although the log grew.
    assert read_json(bundle / "runner.bundle.json")["binding"]["tool_sha256"] == start["sdk"]["tool_sha256"]
    assert end["reservation"] == f"build/{bundle.name}" and end["manifest_sha256"] == first["result"]["manifest_sha256"]
    assert end["cache_hit"] is False and lines[3]["cache_hit"] is True and lines[3]["reservation"] == end["reservation"]
    assert end["segment_count"] == 1 and end["elapsed_s"] >= 0 and "error" not in end
    assert not list(bundle.rglob(PROJECT_LOG))


def test_a_failed_build_logs_the_sdk_error_and_its_reservation(svc, tmp_path):
    info = svc.init_project(request(tmp_path / "p", stil=str(looped_counter(tmp_path)), wait_preset="chunk-4ms",
                                    service_us="4000", service_source="declared, not measured",
                                    service_scope="non-loop", max_total_wait_us="40000"))
    first = _wait(svc.start_build(info["project_path"]))
    assert first["status"] == "failed"
    failed = log_lines(info["project_dir"])[1]
    kept = Path(info["project_dir"]) / failed["reservation"]
    assert failed["reservation"].startswith("build/") and kept.is_dir()
    os.utime(kept, (kept.stat().st_atime, kept.stat().st_mtime - 3600))  # an hour old: not "the newest folder"
    again = _wait(svc.start_build(info["project_path"]))
    assert again["status"] == "failed"
    lines = log_lines(info["project_dir"])
    assert [line["status"] for line in lines] == ["started", "FAILED"] * 2
    repeated = lines[3]
    assert failed["error"].split(": ", 1)[1] in first["error"]  # the SDK's own text, without the GUI's hints
    # The same inputs again: the SDK names the kept reservation instead of building.
    assert "existing cache is incomplete/corrupt; not overwritten" in repeated["error"]
    assert repeated["reservation"] == failed["reservation"]


def test_a_cancelled_build_is_logged(tmp_path, svc):
    info = svc.init_project(request(tmp_path / "p", stil=str(looped_counter(tmp_path))))
    token = CancellationToken()
    token.cancel()
    path = Path(info["project_path"])
    with pytest.raises(RunnerError, match="cancelled"):
        logged_build(path, path.parent / "build", cancel=token)
    assert [line["status"] for line in log_lines(path.parent)] == ["started", "CANCELLED"]


def test_a_log_that_cannot_be_written_does_not_stop_the_build(svc, tmp_path):
    info = svc.init_project(request(tmp_path / "p", stil=str(looped_counter(tmp_path))))
    (Path(info["project_dir"]) / PROJECT_LOG).mkdir()  # opening it to append fails
    done = _wait(svc.start_build(info["project_path"]))
    assert done["status"] == "done", done["error"]
