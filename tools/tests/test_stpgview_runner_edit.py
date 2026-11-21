"""Edit settings of a runner project that has not built yet, and what a failed build tells the user.

Host only, public synthetic STIL. The oracles are the SDK's own: ``stpg_runner.init_project``
for the project file an edit must leave (the same settings written by the SDK), the SDK loader for
validation, ``stpg-stil-preprocess`` for ATE input, and ``stil.source_analysis`` for a rejected
segment's reasons.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from stil.segmented_package import SegmentedPackageError
from stil.source_analysis import analyze_stil_source, write_source_analysis_report
from stpg_runner import init_project
from stpg_runner.artifacts import RunnerError, identity, read_json, write_json
from stpgview.runner import service as service_module
from stpgview.runner.service import DERIVED_PROJECT_FILE, RunnerService, project_options, stil_loop_count
from stpgview.session import Session
from stpgview.stil_inputs import stil_role

from stpg_hardware_guard import forbid_hardware
from test_stpgview_runner_inputs import (ATE_PATTERN, ATE_PROJECT, RUNNER_STIL, _long_raw, _wait, cli_preprocess,
                                         request)

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture
def svc(tmp_path):
    return RunnerService(config_dir=tmp_path / "cfg", device_run=False, demo=False, results_dir=tmp_path / "runs",
                         session=Session(tmp_path / "work"))


def edit(svc, info: dict, **override) -> dict:
    value = {key: item for key, item in request(Path(info["project_dir"])).items()
             if key not in {"input_form", "stil", "directory"}}
    value.update(project=info["project_path"], project_sha256=info["project_sha256"])
    value.update(override)
    return svc.edit_project(value)


def sdk_project(tmp_path: Path, source: Path, **override) -> dict:
    """The project file stpg_runner.init_project writes for these form values (the oracle)."""

    folder = tmp_path / f"oracle-{len(list(tmp_path.glob('oracle-*')))}"
    return read_json(init_project(source, folder, **project_options(request(folder, **override))))


# ---- edit in place -----------------------------------------------------------------------

def test_a_failed_project_is_edited_in_place_and_keeps_the_failed_attempt(svc, tmp_path):
    source = _long_raw(tmp_path, 8000)
    single = dict(stil=str(source), compile_mode="raw-event", loop_policy="expand", segmentation="single")
    info = svc.init_project(request(tmp_path / "p", **single))
    failed = _wait(svc.start_build(info["project_path"]))
    assert failed["status"] == "failed" and "does not fit in one package" in failed["error"]
    # The hint names Edit settings as one way to get automatic segmentation.
    assert "with Edit settings…" in failed["error"]

    before = svc.open_project(info["project_path"])
    assert before["edit_blockers"] == []
    (attempt,) = before["reservations"]
    assert attempt["status"] == "FAILED" and attempt["current_settings"] is True
    assert attempt["settings"]["segmentation"] == "single" and attempt["settings"]["loop_policy"] == "expand"

    after = edit(svc, before, **{**single, "segmentation": "auto"})
    project = read_json(Path(info["project_path"]))
    # Byte for byte what the SDK writes for the same choices; inputs and target are the project's own.
    assert project == sdk_project(tmp_path, source, **{**single, "segmentation": "auto"})
    assert project["input"] == before["settings"]["input"] and project["target"] == before["settings"]["target"]
    assert after["settings"]["segmentation"] == {"mode": "auto", "max_vectors": None}
    (kept,) = after["reservations"]
    assert kept["key"] == attempt["key"] and kept["status"] == "FAILED" and kept["current_settings"] is False
    assert read_json(Path(kept["path"]) / "project.json")["segmentation"] == {"mode": "single", "max_vectors": None}

    done = _wait(svc.start_build(info["project_path"]))
    assert done["status"] == "done", done["error"]
    assert (done["result"]["package_kind"], done["result"]["segment_count"]) == ("segmented", 2)
    built = svc.open_project(info["project_path"])
    assert {r["status"] for r in built["reservations"]} == {"FAILED", "HOST_ONLY_VERIFIED"}
    assert [r["current_settings"] for r in built["reservations"] if r["status"] == "HOST_ONLY_VERIFIED"] == [True]

    # A project with a successful build is copied, not edited: the file stays as it is.
    assert any("successful build" in item for item in built["edit_blockers"])
    unchanged = identity(Path(info["project_path"]))
    with pytest.raises(RunnerError, match="without a successful build"):
        edit(svc, built, **{**single, "segmentation": "auto", "completion": "release"})
    assert identity(Path(info["project_path"])) == unchanged
    assert built["copy_blockers"] == []


def test_edit_keeps_pattern_selection_and_target_and_writes_an_explicit_segmentation_mode(svc, tmp_path):
    # An API caller without a mode makes a project of the earlier meaning (no segmentation mode).
    info = svc.init_project(request(tmp_path / "p", segmentation=None))
    path = Path(info["project_path"])
    raw = read_json(path)
    assert "mode" not in raw["segmentation"]
    raw["input"]["pattern"] = "MAIN"
    raw["target"]["max_rates"] = 32
    write_json(path, raw)
    opened = svc.open_project(str(path))
    after = edit(svc, opened, segmentation="auto", wait_preset="chunk-4ms", service_us="4000",
                 service_scope="non-loop", service_source="declared, not measured", max_total_wait_us="1000000")
    saved = read_json(path)
    assert saved["segmentation"] == {"mode": "auto", "max_vectors": None}
    assert saved["input"]["pattern"] == "MAIN" and saved["target"]["max_rates"] == 32
    assert saved["wait"] == {"preset": "chunk-4ms", "service_us": 4000.0, "service_source": "declared, not measured",
                             "service_scope": "non-loop", "max_total_wait_us": 1000000.0, "forbidden_stil_rates": []}
    assert after["settings"] == saved


@pytest.mark.parametrize("change, message", [
    ({"compile_mode": "raw-event", "loop_policy": "preserve"}, "raw-event/macro-stream require explicit loop_policy=expand"),
    ({"wait_preset": "chunk-4ms"}, "wait.service_us"),
    ({"loop_policy": ""}, "choose explicitly"),
    ({"segmentation": "single", "max_segment_vectors": "5"}, "used only with segmentation = auto"),
])
def test_invalid_settings_are_rejected_and_the_file_is_unchanged(svc, tmp_path, change, message):
    info = svc.init_project(request(tmp_path / "p"))
    before = identity(Path(info["project_path"]))
    with pytest.raises(RunnerError, match=message.replace("(", r"\(").replace("+", r"\+")):
        edit(svc, info, **change)
    assert identity(Path(info["project_path"])) == before


def test_edit_refuses_a_file_that_changed_since_it_was_opened(svc, tmp_path):
    info = svc.init_project(request(tmp_path / "p"))
    path = Path(info["project_path"])
    raw = read_json(path)
    raw["execution"]["completion"] = "release"
    write_json(path, raw)  # for example the CUI or another window
    changed = identity(path)
    with pytest.raises(RunnerError, match="changed since it was opened"):
        edit(svc, info, storage="psram")
    assert identity(path) == changed


def test_edit_is_not_offered_for_a_marker_derived_project(svc, tmp_path):
    info = svc.init_project(request(tmp_path / "p"))
    write_json(Path(info["project_dir"]) / DERIVED_PROJECT_FILE, {"kind": "test"})  # written by the LA tool
    opened = svc.open_project(info["project_path"])
    assert any("marker project derived by the logic-analyzer tool" in item for item in opened["edit_blockers"])
    with pytest.raises(RunnerError, match="marker project derived by the logic-analyzer tool"):
        edit(svc, opened)


def test_edit_waits_for_a_running_build_of_the_project(svc, tmp_path):
    info = svc.init_project(request(tmp_path / "p"))
    release = threading.Event()
    job = svc.jobs.start("build", "held", lambda job: release.wait(30), key=f"build:{Path(info['project_path'])}")
    try:
        opened = svc.open_project(info["project_path"])
        assert "a build of it is running" in opened["edit_blockers"]
        with pytest.raises(RunnerError, match="a build of it is running"):
            edit(svc, opened)
    finally:
        release.set()
        _wait(job)
    assert svc.open_project(info["project_path"])["edit_blockers"] == []


# ---- wait scope hint ---------------------------------------------------------------------

def test_the_project_reports_how_many_loop_statements_its_stil_has(svc, tmp_path):
    prefix = RUNNER_STIL.read_text().split("Pattern MAIN")[0]
    source = tmp_path / "loops.stil"
    source.write_text(prefix + "Pattern MAIN {\n  W NORMAL;\n  // Loop 5 { is a comment\n  Loop 3 {\n    V { ALL = 01; }\n  }\n"
                      "  V { ALL = 10; }\n\tLoop 2 {\n    V { ALL = 01; }\n    Loop 4 {\n      V { ALL = 10; }\n    }\n  }\n}\n")
    assert stil_loop_count(source) == 3
    assert svc.init_project(request(tmp_path / "p", stil=str(source)))["stil_loops"] == 3
    assert svc.init_project(request(tmp_path / "q"))["stil_loops"] == 0


def test_loop_count_reads_across_chunk_boundaries(tmp_path, monkeypatch):
    source = tmp_path / "long.stil"
    lines = [("  Loop 7 {" if i % 3 == 0 else "  V { ALL = 01; }") for i in range(30000)]
    source.write_text("\n".join(lines) + "\n")
    expected = sum(1 for line in lines if line.startswith("  Loop"))
    assert stil_loop_count(source) == expected
    # Same count when the file is read in chunks that cut lines.
    real_open = Path.open

    class Small:
        def __init__(self, stream):
            self.stream = stream

        def read(self, size=-1):
            return self.stream.read(1000)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stream.close()

    monkeypatch.setattr(Path, "open", lambda self, mode="r", *a, **k: Small(real_open(self, mode, *a, **k)))
    assert stil_loop_count(source) == expected


# ---- ATE project file with a commented-out Signals block -------------------------------------

COMMENTED = ATE_PROJECT.replace("Signals {", "/*\nSignals {", 1).replace("SignalGroups {", "*/\nSignalGroups {", 1)


def test_an_ate_project_file_with_commented_signals_is_preprocessed_like_the_cli(svc, tmp_path):
    folder = tmp_path / "ate"
    folder.mkdir()
    project, pattern = folder / "proj_project.stil", folder / "pat.stil"
    project.write_text(COMMENTED, encoding="utf-8")
    pattern.write_text(ATE_PATTERN, encoding="utf-8")
    assert "/*\nSignals {" in COMMENTED
    info = svc.init_project(request(tmp_path / "new", input_form="ate-pair", stil=str(pattern), ate_project=str(project)))
    expected_stil, expected_pins = cli_preprocess(tmp_path, project, pattern)
    root = Path(info["project_dir"])
    assert (root / "inputs" / "pattern.stil").read_bytes() == expected_stil
    assert json.loads((root / "inputs" / "pins.json").read_bytes()) == json.loads(expected_pins)
    # The viewer pairs the same two files.
    assert stil_role(project) == "project" and stil_role(pattern) == "pattern"
    assert len(Session(tmp_path / "view").load([project, pattern])) == 1


def test_a_project_file_without_any_signals_block_is_still_rejected(svc, tmp_path):
    folder = tmp_path / "ate"
    folder.mkdir()
    project, pattern = folder / "proj_project.stil", folder / "pat.stil"
    head, rest = ATE_PROJECT.split("SignalGroups {", 1)
    project.write_text("STIL 1.0;\nSignalGroups {" + rest, encoding="utf-8")
    pattern.write_text(ATE_PATTERN, encoding="utf-8")
    with pytest.raises(RunnerError, match="has no Signals/Timing block"):
        svc.init_project(request(tmp_path / "new", input_form="ate-pair", stil=str(pattern), ate_project=str(project)))
    assert not (tmp_path / "new").exists()


# ---- a segment rejected by its own source analysis ------------------------------------------

FAST = """Header { Title "fast"; }
Signals {
  D0 InOut;
  D1 InOut;
}
SignalGroups {
  ALL = 'D0 + D1';
}
Timing {
  WaveformTable FAST {
    Period '20ns';
    Waveforms {
      ALL { 0: 0ns Drive(0); 1: 0ns Drive(1); X: 5ns Compare(X); }
    }
  }
  WaveformTable SLOW {
    Period '400ns';
    Waveforms {
      ALL { 0: 0ns Drive(0); 1: 0ns Drive(1); X: 100ns Compare(X); }
    }
  }
}
Pattern MAIN {
  W SLOW;
  V { ALL = 01; }
  W FAST;
"""


def test_a_rejected_segment_names_its_own_reasons_not_the_whole_source(svc, tmp_path, monkeypatch):
    source = tmp_path / "fast.stil"
    source.write_text(FAST + "\n".join("  V { ALL = 1X; }" if i % 2 else "  V { ALL = 01; }" for i in range(7000)) + "\n}\n")
    # The SDK's analysis of a segment that streaming must play (more events than the direct path holds).
    analysis = analyze_stil_source(source, mode="rate-vector", loop_policy="preserve")
    assert analysis["status"] == "REJECT"
    assert any(item["code"] == "ERR_STREAMING_POC_TIMING" for item in analysis["anomalies"])
    info = svc.init_project(request(tmp_path / "p", stil=str(source), segmentation="auto"))
    message = "variant nominal segment 1 failed: source analysis is REJECT; package generation is blocked"

    def failing_build(path, **kwargs):  # what the SDK leaves behind for such a build
        folder = Path(path).parent / "build" / ("0" * 64)
        segment = folder / "segmented" / "nominal" / "segment-0001" / ".source-package"
        segment.mkdir(parents=True)
        write_source_analysis_report(segment / "source.analysis.json", analysis)
        write_json(folder / "source.analysis.json", {"status": "REJECT", "anomalies": [
            {"severity": "error", "code": "ERR_PSRAM_PATTERN_BYTES_LIMIT", "message": "whole source too large"}]})
        write_json(folder / "build.state.json", {"status": "FAILED", "error": f"SegmentedPackageError: {message}"})
        raise SegmentedPackageError(message)

    monkeypatch.setattr(service_module, "build_project", failing_build)
    failed = _wait(svc.start_build(info["project_path"]))
    error = failed["error"]
    assert failed["status"] == "failed" and error.startswith(f"RunnerError: SegmentedPackageError: {message}. Segment 1 ")
    assert "segment-0001/.source-package/source.analysis.json" in error
    assert "ERR_STREAMING_POC_TIMING: rate[" in error and "= FAST (period 4 ticks)" in error
    assert "the STIL WaveformTable timing has to change" in error
    # Not the whole-source (capacity) advice: the pattern is already built in segments.
    assert "whole source too large" not in error and "make a copy" not in error


# ---- wait scope per segment (open SDK request C1) --------------------------------------------

def _plain_then_loop(tmp_path: Path) -> Path:
    prefix = RUNNER_STIL.read_text().split("Pattern MAIN")[0].replace("Period '400ns'", "Period '20000ns'")
    plain = "\n".join(f"  V {{ ALL = {'01' if i % 2 else '10'}; }}" for i in range(9000))
    source = tmp_path / "plain_then_loop.stil"
    source.write_text(prefix + "Pattern MAIN {\n  W NORMAL;\n" + plain + "\n  Loop 10 {\n    V { ALL = 01; }\n"
                      "    V { ALL = 10; }\n  }\n}\n")
    return source


WAIT = dict(wait_preset="chunk-12ms", service_us="8000", service_source="declared, not measured",
            max_total_wait_us="10000000", compile_mode="rate-vector")


def test_mixed_loop_segments_build_and_the_wrong_whole_scope_still_rejects(svc, tmp_path):
    source = _plain_then_loop(tmp_path)
    info = svc.init_project(request(tmp_path / "seg", stil=str(source), loop_policy="preserve", service_scope="loop-generic",
                                    segmentation="auto", max_segment_vectors="3000", **WAIT))
    assert info["stil_loops"] == 1
    done = _wait(svc.start_build(info["project_path"]), timeout=300)
    assert done["status"] == "done", done["error"]
    assert done["result"]["segment_count"] >= 3
    # A successful project stays immutable: the bad scope is a separate project.
    other = svc.init_project(request(tmp_path / "wrong-scope", stil=str(source), loop_policy="preserve",
                 service_scope="non-loop", segmentation="auto", max_segment_vectors="3000", **WAIT))
    moved = _wait(svc.start_build(other["project_path"]), timeout=300)["error"]
    assert "does not match whole segmented sequence scope 'loop-generic'" in moved
    # Positive controls: the same STIL as one package with loop-generic, and expanded in segments.
    whole = svc.init_project(request(tmp_path / "one", stil=str(source), loop_policy="preserve",
                                     service_scope="loop-generic", segmentation="single", **WAIT))
    assert _wait(svc.start_build(whole["project_path"]), timeout=300)["status"] == "done"
    expanded = svc.init_project(request(tmp_path / "exp", stil=str(source), loop_policy="expand", service_scope="non-loop",
                                        segmentation="auto", max_segment_vectors="3000", **WAIT))
    done = _wait(svc.start_build(expanded["project_path"]), timeout=300)
    assert done["status"] == "done", done["error"]
    assert done["result"]["segment_count"] >= 3


def test_capacity_and_bad_timing_reject_in_whole_source_without_segment_retry(svc, tmp_path):
    """End to end through the SDK, no wait (the segment's analysis is under ``package/``).

    270 000 alternating vectors do not fit the PSRAM pattern region (20 bytes each), so automatic
    segmentation splits for capacity; only then does segment 0 meet the streaming timing gate.
    """

    source = tmp_path / "big_fast.stil"
    with source.open("w") as stream:
        stream.write(FAST.split("Pattern MAIN")[0].replace(
            "  WaveformTable SLOW {\n    Period '400ns';\n    Waveforms {\n"
            "      ALL { 0: 0ns Drive(0); 1: 0ns Drive(1); X: 100ns Compare(X); }\n    }\n  }\n", "")
                     + "Pattern MAIN {\n  W FAST;\n")
        stream.writelines("  V { ALL = 01; }\n" if i % 2 else "  V { ALL = 10; }\n" for i in range(270000))
        stream.write("}\n")
    info = svc.init_project(request(tmp_path / "p", stil=str(source), loop_policy="expand", segmentation="auto"))
    failed = _wait(svc.start_build(info["project_path"]), timeout=600)
    error = failed["error"]
    assert failed["status"] == "failed"
    assert "segmented; reason=capacity" not in json.dumps(failed["events"])
    assert "source analysis is REJECT; package generation is blocked" in error
    assert "ERR_STREAMING_POC_TIMING: rate[0] cycle-wrap gap" in error
    # The whole-source rejection names the WaveformTable and explains the limit.
    assert "By period: rate[0] = FAST (period 4 ticks)." in error
    assert "ERR_STREAMING_POC_TIMING: this pattern is played by streaming, and streaming needs at least 10 ticks" in error
    reservation, = svc.open_project(info["project_path"])["reservations"]
    assert not (Path(reservation["path"]) / "segmented").exists()
    assert "make a copy" not in error
