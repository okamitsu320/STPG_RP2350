"""Offline helpers of the physical GUI test request: public pattern generator and evidence packer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from stpg_runner import build_project, init_project
from stpg_runner.artifacts import read_json
from stpgview.load_bin import load_bin
from stpgview.physical import evidence, patterns

pytestmark = pytest.mark.stpg_host_only
FIXTURE = Path(__file__).parent / "fixtures" / "runner" / "pattern.stil"
# The test request quotes these values; a change here must update the request document too.
EXPECTED = {
    "gui_counter.pins.json": "b2d14b9dd43880d7667d2efc80d4c937b0f352aa86a7207b99eeacf455c7042a",
    "gui_counter.stil": "f5ebeaa3f93b7146f4ca36717a6bd7a0c2236a1180f43bdce6e3ce21e97afc25",
    "gui_counter_long.stil": "bc402224a302085e007bbd466eb9671f47f8b038744ace0957d2b85ff62b2771",
    "gui_tiny.stil": "25d60cbd475df15b37cd7ad17ad13e575fcc25aba3006c7e6f99d8209b7e8fea",
}
# Verification FPGA bench: GPIO2..10 are FPGA inputs (clk, rst_n, mode, ctrl; GPIO10 = io_oe).
FPGA_INPUT_GPIO = set(range(2, 11))
DUT_GPIO = tuple(range(2, 18))  # v0.1 wiring: logical DUT0..15 on GPIO2..17


def test_patterns_are_public_drive_only_and_byte_stable(tmp_path, capsys):
    assert patterns.main([str(tmp_path / "inputs")]) == 0
    assert json.loads(capsys.readouterr().out) == EXPECTED
    for name, sha in EXPECTED.items():
        data = (tmp_path / "inputs" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha and b"\r" not in data
    assert (tmp_path / "inputs" / "gui_tiny.stil").read_bytes() == FIXTURE.read_bytes()
    for name in ("gui_counter.stil", "gui_counter_long.stil"):
        assert b"Compare" not in (tmp_path / "inputs" / name).read_bytes()  # nothing depends on a DUT
    with pytest.raises(SystemExit, match="not an empty directory"):
        patterns.main([str(tmp_path / "inputs")])


def test_counters_drive_only_fpga_inputs_and_hold_io_oe_low(tmp_path, capsys):
    patterns.main([str(tmp_path / "inputs")])
    capsys.readouterr()
    inputs = tmp_path / "inputs"
    project = init_project(inputs / "gui_counter_long.stil", tmp_path / "long", wait_preset="none", completion="hold",
                           pin_map=inputs / "gui_counter.pins.json", max_segment_vectors=1600)
    built = build_project(project)
    patterns_ = read_json(built.bundle_dir / "runner.bundle.json")["patterns"]
    assert len(patterns_) == 2
    for entry in patterns_:
        trace = load_bin(built.bundle_dir / entry["path"])
        gpios = {sig.name: DUT_GPIO[sig.bit] for sig in trace.signals}
        assert gpios == {**{f"A{i}": 2 + i for i in range(7)}, "IOOE": 10}
        assert set(gpios.values()) <= FPGA_INPUT_GPIO - {9}  # GPIO9 stays free (for example for a marker)
    text = (inputs / "gui_counter.stil").read_text()
    assert all(line.endswith("0; }") for line in text.splitlines() if line.startswith("  V {"))  # IOOE always 0


def test_evidence_packs_only_the_given_paths(tmp_path, capsys):
    result = tmp_path / "20261017-run"
    (result / "stpg").mkdir(parents=True)
    (result / "accessory" / "own").mkdir(parents=True)  # whatever the accessory program writes is packed as it is
    (result / "stpg" / "run.json").write_text("{}")
    (result / "accessory" / "session.json").write_text("{}")
    (result / "accessory" / "own" / "data.bin").write_bytes(b"x")
    (tmp_path / "unrelated.txt").write_text("not packed")
    config = tmp_path / "cfg"
    (config / "controllers").mkdir(parents=True)
    (config / "controllers" / "gpio.json").write_text("{}")
    note = tmp_path / "report.md"
    note.write_text("# report")
    out = tmp_path / "evidence.zip"
    assert evidence.main(["--out", str(out), "--bindings", str(config), "--note", str(note), str(result)]) == 0
    capsys.readouterr()
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        env = json.loads(archive.read("environment.json"))
    assert names == {"environment.json", "manifest.json", "results/20261017-run/stpg/run.json",
                     "results/20261017-run/accessory/session.json", "results/20261017-run/accessory/own/data.bin",
                     "bindings/controllers/gpio.json", "notes/report.md"}
    assert {m["name"] for m in manifest["files"]} == names - {"manifest.json"}
    with zipfile.ZipFile(out) as archive:
        assert all(hashlib.sha256(archive.read(m["name"])).hexdigest() == m["sha256"] and
                   len(archive.read(m["name"])) == m["size"] for m in manifest["files"])
    assert env["kind"] == "stpg_gui_physical_evidence" and set(env["source_digests"]) == set(evidence.PACKAGES)
    assert "stpg_capture" not in env["source_digests"]  # the MIT core only
    with pytest.raises(SystemExit, match="exists"):
        evidence.main(["--out", str(out), str(result)])


def test_evidence_without_its_result_record_is_marked_incomplete(tmp_path, capsys, monkeypatch):
    complete = tmp_path / "20261018-run"
    complete.mkdir()
    (complete / "run.json").write_text("{}")
    empty = tmp_path / "20261018-empty"  # e.g. a run folder whose result was never written
    empty.mkdir()
    (empty / "notes.txt").write_text("partial")
    out = tmp_path / "evidence.zip"
    assert evidence.main(["--out", str(out), str(complete), str(empty)]) == 2
    printed = capsys.readouterr()
    assert "INCOMPLETE" in printed.err
    with zipfile.ZipFile(out) as archive:  # the archive is still written so it can be returned
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["incomplete"] == ["results/20261018-empty: no result record (run.json, run.json.gz, control.json, "
                                      "identify.json, capture.json, session.json)"]
    monkeypatch.setattr(evidence, "MAX_FILE_BYTES", 1)  # the record itself is too large to store
    out2 = tmp_path / "evidence2.zip"
    assert evidence.main(["--out", str(out2), str(complete)]) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["skipped_over_64MiB"] == ["results/20261018-run/run.json"]
    assert summary["incomplete"] == ["results/20261018-run/run.json: result record not stored (over 64 MiB)"]


@pytest.mark.parametrize("layout,removed", [
    ({"run.json": "{}", "control.json": "{}"}, "run.json"),  # controller-only invocation
    ({"run/run.json": "{}", "capture/capture.json": "{}", "control.json": "{}"}, "run"),  # an older run/ folder
    ({"stpg/run.json": "{}", "stpg/control.json": "{}", "accessory/session.json": "{}"}, "stpg/run.json"),
    ({"stpg/run.json": "{}", "accessory/session.json": "{}"}, "stpg"),  # the external accessory layout
])
def test_a_control_or_capture_record_cannot_hide_a_missing_stpg_run_record(tmp_path, capsys, layout, removed):
    result = tmp_path / "20261018-invocation"
    for name, text in layout.items():
        (result / name).parent.mkdir(parents=True, exist_ok=True)
        (result / name).write_text(text)
    out = tmp_path / "complete.zip"
    assert evidence.main(["--out", str(out), str(result)]) == 0  # the positive control: nothing missing
    capsys.readouterr()
    target = result / removed
    if target.is_dir():
        for child in target.iterdir():
            child.unlink()
        target.rmdir()
    else:
        target.unlink()
    assert evidence.main(["--out", str(tmp_path / "partial.zip"), str(result)]) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["incomplete"][-1] == ("results/20261018-invocation: control/accessory/capture record without the "
                                         "STPG run.json")


@pytest.mark.parametrize("existing", ["keep.txt", "gui_counter_long.stil"])
def test_the_sample_command_refuses_a_folder_that_is_not_empty(tmp_path, existing):
    folder = tmp_path / "mine"
    folder.mkdir()
    (folder / existing).write_text("keep")
    with pytest.raises(SystemExit, match="not an empty directory"):
        patterns.main([str(folder)])
    assert [path.name for path in folder.iterdir()] == [existing] and (folder / existing).read_text() == "keep"


def test_the_sample_command_refuses_a_file_and_accepts_an_empty_folder(tmp_path):
    (tmp_path / "file").write_text("keep")
    with pytest.raises(SystemExit, match="not an empty directory"):
        patterns.main([str(tmp_path / "file")])
    (tmp_path / "empty").mkdir()
    assert patterns.main([str(tmp_path / "empty")]) == 0
    assert {path.name for path in (tmp_path / "empty").iterdir()} == set(patterns.PATTERNS)
