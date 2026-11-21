"""New-project inputs of the GUI runner: preprocessing, folders, segmentation, copies, build hints.

Host only, public synthetic STIL. The independent oracle for preprocessing is the
repository's own ``stpg-stil-preprocess`` CLI (``stil.cli_preprocess.main``): the
project the GUI creates must hold byte-identical files. Builds go through the
SDK (``stpg_runner.build_project``) unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stil.cli_preprocess import main as preprocess_main
from stpg_runner import build_project, verify_bundle
from stpg_runner.artifacts import RunnerError, identity, read_json, write_json
from stpgview.runner.service import RunnerService
from stpgview.session import Session

from stpg_hardware_guard import forbid_hardware

pytestmark = pytest.mark.stpg_host_only

FIXTURES = Path(__file__).parent / "fixtures"
RUNNER_STIL = FIXTURES / "runner" / "pattern.stil"

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
    Period '100ns';
    Waveforms {
      A { 0 { '0ns' D; } }
      A { 1 { '0ns' U; } }
      B { L { '50ns' L; } }
      B { H { '50ns' H; } }
      B { X { '0ns' X; } }
      C { 0 { '0ns' D; } }
      C { 1 { '0ns' U; } }
    }
  }
}
"""
ATE_PATTERN = """STIL 1.0;
Pattern P {
  W W1;
  V { ALL = 0L1; }
  V { ALL = 1H0; }
  V { ALL = 0X1; }
  V { ALL = 1H1; }
}
"""


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
    value = {"input_form": "stil", "stil": str(RUNNER_STIL), "directory": str(directory), "wait_preset": "none",
             "completion": "hold", "storage": "psram", "compile_mode": "auto", "loop_policy": "preserve",
             "segmentation": "single"}
    value.update(override)
    return value


def cli_preprocess(tmp_path: Path, project: Path, pattern: Path, *ignore: str) -> tuple[bytes, bytes]:
    """What the CUI preprocessor writes for the same input (the oracle)."""

    out = tmp_path / f"cli-{len(list(tmp_path.glob('cli-*')))}"
    args = [str(project), str(pattern), "--work-dir", str(out), "--pin-map"]
    for name in ignore:
        args += ["--ignore-signal", name]
    assert preprocess_main(args) == 0
    (stil,) = out.glob("*.pre.stil")
    (pins,) = out.glob("*.pinmap.json")
    return stil.read_bytes(), pins.read_bytes()


def built(project_path: str) -> Path:
    result = build_project(Path(project_path))
    assert verify_bundle(result.bundle_dir, manifest_sha256=result.manifest_sha256)["status"] == "PASS"
    return result.bundle_dir


# ---- preprocessing -------------------------------------------------------------------------

def test_ate_pair_is_preprocessed_like_the_cli_and_builds(svc, tmp_path, ate):
    project, pattern = ate
    info = svc.init_project(request(tmp_path / "new", input_form="ate-pair", stil=str(pattern), ate_project=str(project)))
    root = Path(info["project_dir"])
    expected_stil, expected_pins = cli_preprocess(tmp_path, project, pattern)
    assert (root / "inputs" / "pattern.stil").read_bytes() == expected_stil
    assert json.loads((root / "inputs" / "pins.json").read_bytes()) == json.loads(expected_pins)
    record = read_json(root / "preprocess.json")
    assert record["form"] == "ate-pair" and record["ignore_signals"] == []
    assert record["sources"]["project"] == {"path": str(project.resolve()), **identity(project)}
    assert record["sources"]["pattern"] == {"path": str(pattern.resolve()), **identity(pattern)}
    assert record["output"]["inputs/pattern.stil"] == identity(root / "inputs" / "pattern.stil")
    assert record["pin_map"]["source"].startswith("generated")
    assert info["preprocess"] == record and info["settings"]["input"]["pin_map"] == "inputs/pins.json"
    bundle = built(info["project_path"])
    # The build binds the converted STIL; the record is not part of the bundle.
    assert (bundle / "sources" / "inputs" / "pattern.stil").read_bytes() == expected_stil
    assert not (bundle / "preprocess.json").exists()
    assert project.read_text() == ATE_PROJECT and pattern.read_text() == ATE_PATTERN  # originals untouched


def test_ate_single_file_and_ignored_signal(svc, tmp_path, ate):
    project, pattern = ate
    single = tmp_path / "single.stil"
    single.write_text(ATE_PROJECT + ATE_PATTERN.replace("STIL 1.0;\n", ""), encoding="utf-8")
    info = svc.init_project(request(tmp_path / "one", input_form="ate-single", stil=str(single), ignore_signals="C"))
    root = Path(info["project_dir"])
    expected_stil, expected_pins = cli_preprocess(tmp_path, single, single, "C")
    assert (root / "inputs" / "pattern.stil").read_bytes() == expected_stil
    pins = json.loads((root / "inputs" / "pins.json").read_bytes())
    assert pins == json.loads(expected_pins) and "C" not in pins["signals"]
    assert read_json(root / "preprocess.json")["ignore_signals"] == ["C"]
    built(info["project_path"])


def test_a_given_pin_map_is_used_as_is(svc, tmp_path, ate):
    project, pattern = ate
    given = tmp_path / "given.pins.json"
    write_json(given, {"active_pins": 3, "signals": {
        "A": {"bit": 2, "direction": "input"}, "B": {"bit": 0, "direction": "output"}, "C": {"bit": 1, "direction": "input"}}})
    info = svc.init_project(request(tmp_path / "g", input_form="ate-pair", stil=str(pattern), ate_project=str(project),
                                    pin_map=str(given)))
    root = Path(info["project_dir"])
    assert (root / "inputs" / "pins.json").read_bytes() == given.read_bytes()
    assert read_json(root / "preprocess.json")["pin_map"]["source"] == "given"


@pytest.mark.parametrize("form, files, message", [
    ("stil", "pattern", "has a Pattern but no Signals"),
    ("stil", "project", "looks like an ATE project file"),
    ("stil", "single", "uses tester event codes"),
    ("ate-pair", "swapped", "look swapped"),
    ("ate-pair", "no-project", "choose the ATE project file"),
    ("ate-single", "toolchain-pattern-only", "has no Signals/Timing block"),
])
def test_a_wrong_input_form_is_rejected_before_any_folder_is_made(svc, tmp_path, ate, form, files, message):
    project, pattern = ate
    single = tmp_path / "single.stil"
    single.write_text(ATE_PROJECT + ATE_PATTERN.replace("STIL 1.0;\n", ""), encoding="utf-8")
    pattern_only = tmp_path / "pattern-only.stil"
    pattern_only.write_text(RUNNER_STIL.read_text().split("Pattern MAIN")[1], encoding="utf-8")
    choice = {"pattern": {"stil": str(pattern)}, "project": {"stil": str(project)}, "single": {"stil": str(single)},
              "swapped": {"stil": str(project), "ate_project": str(pattern)}, "no-project": {"stil": str(pattern)},
              "toolchain-pattern-only": {"stil": str(pattern_only)}}[files]
    target = tmp_path / "never"
    with pytest.raises(RunnerError, match=message):
        svc.init_project(request(target, input_form=form, **choice))
    assert not target.exists()


def test_unknown_ignored_signal_and_stil_form_options_are_rejected(svc, tmp_path, ate):
    project, pattern = ate
    with pytest.raises(RunnerError, match=r"ignore signals not declared .*\['Z'\]"):
        svc.init_project(request(tmp_path / "x", input_form="ate-pair", stil=str(pattern), ate_project=str(project),
                                 ignore_signals="C, Z"))
    with pytest.raises(RunnerError, match="apply only to ATE-generator input"):
        svc.init_project(request(tmp_path / "y", ignore_signals="A"))
    assert not (tmp_path / "x").exists() and not (tmp_path / "y").exists()


# ---- folders and segmentation --------------------------------------------------------------

def test_an_existing_folder_is_never_reused(svc, tmp_path):
    folder = tmp_path / "exists"
    folder.mkdir()
    (folder / "keep.txt").write_text("mine")
    with pytest.raises(RunnerError, match="already exists; type the name of a new folder"):
        svc.init_project(request(folder))
    assert [p.name for p in folder.iterdir()] == ["keep.txt"]
    nested = svc.init_project(request(tmp_path / "parent" / "child"))  # parents are made, the folder is new
    assert Path(nested["project_dir"]).name == "child"


@pytest.mark.parametrize("override, expected", [
    ({"segmentation": "single"}, {"mode": "single", "max_vectors": None}),
    ({"segmentation": "auto"}, {"mode": "auto", "max_vectors": None}),
    ({"segmentation": "split", "max_segment_vectors": "2"}, {"mode": "auto", "max_vectors": 2}),
    ({"segmentation": None, "max_segment_vectors": 3}, {"max_vectors": 3}),  # older API caller
    ({"segmentation": None}, {"max_vectors": None}),
])
def test_segmentation_choice(svc, tmp_path, override, expected):
    info = svc.init_project(request(tmp_path / "s", **override))
    assert info["settings"]["segmentation"] == expected


@pytest.mark.parametrize("override, message", [
    ({"segmentation": "single", "max_segment_vectors": "5"}, "only with segmentation = auto or split"),
    ({"segmentation": "split"}, "needs max vectors per segment"),
    ({"segmentation": "split", "max_segment_vectors": "0"}, "needs max vectors per segment"),
    ({"segmentation": "guess"}, "must be auto, single or split"),
    ({"input_form": "guess"}, "input_form must be one of"),
])
def test_segmentation_and_form_rejections(svc, tmp_path, override, message):
    with pytest.raises(RunnerError, match=message):
        svc.init_project(request(tmp_path / "r", **override))
    assert not (tmp_path / "r").exists()


# ---- the same project as the CUI -----------------------------------------------------------

@pytest.mark.parametrize("cli, gui", [
    ([], {}),
    (["--wait-preset", "chunk-4ms", "--service-us", "4000", "--service-scope", "non-loop",
      "--service-source", "declared, not measured", "--max-total-wait-us", "1000000"],
     {"wait_preset": "chunk-4ms", "service_us": "4000", "service_scope": "non-loop",
      "service_source": "declared, not measured", "max_total_wait_us": "1000000"}),
    (["--max-segment-vectors", "2"], {"max_segment_vectors": "2"}),
    (["--segmentation", "single"], {"segmentation": "single"}),
])
def test_the_dialog_defaults_make_the_project_stpg_runner_init_makes(svc, tmp_path, capsys, cli, gui):
    """What the page sends with its preselected values (automatic segmentation, psram, auto, preserve)."""

    from stpg_runner.cli import main as runner_main

    args = ["init", str(RUNNER_STIL), "--directory", str(tmp_path / "cui"), "--completion", "hold"]
    assert runner_main(args + (cli if "--wait-preset" in cli else ["--wait-preset", "none", *cli])) == 0
    capsys.readouterr()
    info = svc.init_project(request(tmp_path / "gui", **{"segmentation": "auto", **gui}))
    assert read_json(Path(info["project_path"])) == read_json(tmp_path / "cui" / "project.stpg-project.json")


# ---- copies --------------------------------------------------------------------------------

def test_a_copy_keeps_the_preprocess_record_only_for_identical_inputs(svc, tmp_path, ate):
    project, pattern = ate
    first = svc.init_project(request(tmp_path / "first", input_form="ate-pair", stil=str(pattern),
                                     ate_project=str(project)))
    inputs = Path(first["project_dir"]) / "inputs"
    same = svc.init_project(request(tmp_path / "same", stil=str(inputs / "pattern.stil"),
                                    pin_map=str(inputs / "pins.json"), copy_of=first["project_path"],
                                    segmentation="split", max_segment_vectors="2"))
    record = read_json(Path(same["project_dir"]) / "preprocess.json")
    assert record["copied_from"] == first["project_dir"] and record["sources"] == first["preprocess"]["sources"]
    assert same["settings"]["segmentation"] == {"mode": "auto", "max_vectors": 2}
    other = svc.init_project(request(tmp_path / "other", copy_of=first["project_path"]))  # a different STIL
    assert other["preprocess"] is None and not (Path(other["project_dir"]) / "preprocess.json").exists()
    no_pins = svc.init_project(request(tmp_path / "nopins", stil=str(inputs / "pattern.stil"),
                                       copy_of=first["project_path"]))  # same STIL, pin map dropped
    assert no_pins["preprocess"] is None


def test_copy_blockers_name_settings_new_project_cannot_reproduce(svc, tmp_path):
    info = svc.init_project(request(tmp_path / "p"))
    assert info["copy_blockers"] == []
    path = Path(info["project_path"])
    raw = read_json(path)
    raw["input"]["pattern"] = "MAIN"
    raw["target"]["tick_hz"] = 100_000_000
    write_json(path, raw)
    blockers = svc.open_project(str(path))["copy_blockers"]
    assert any("Pattern/PatternBurst" in item for item in blockers) and any("target" in item for item in blockers)


# ---- build failures that need segmentation -------------------------------------------------

def _long_raw(tmp_path: Path, count: int) -> Path:
    """A raw-event pattern longer than one direct package can hold (event limit), cheap to compile."""

    prefix = RUNNER_STIL.read_text().split("Pattern MAIN")[0]
    body = "\n".join("V { ALL = 1X; }" if i % 2 else "V { ALL = 01; }" for i in range(count))
    path = tmp_path / f"long{count}.stil"
    path.write_text(prefix + "Pattern MAIN {\nW NORMAL;\n" + body + "\n}\n", encoding="utf-8")
    return path


def _wait(job, timeout=120.0):
    import time

    end = time.monotonic() + timeout
    while job.status == "running":
        assert time.monotonic() < end, "job did not finish"
        time.sleep(0.02)
    return job.public(0)


REASON = "raw-event: event_count out of range"


@pytest.mark.parametrize("override, setting", [
    ({"segmentation": "single"}, "This project is set to one package (Segmentation = single)."),
    # API callers without a mode get a project of the earlier meaning (the SDK keeps it for old projects).
    ({"segmentation": None}, "segmentation has no mode) and keeps the old meaning: always one package."),
    ({"segmentation": None, "max_segment_vectors": "9000"},
     "keeps the old meaning: segments only when it has more than 9000 vectors."),
])
def test_a_pattern_that_does_not_fit_one_package_says_how_to_segment_and_the_copy_builds(svc, tmp_path, override,
                                                                                        setting):
    source = _long_raw(tmp_path, 8000)
    single = svc.init_project(request(tmp_path / "single", stil=str(source), compile_mode="raw-event",
                                      loop_policy="expand", **override))
    failed = _wait(svc.start_build(single["project_path"]))
    assert failed["status"] == "failed"
    error = failed["error"]
    # The SDK's message now carries the reasons; the GUI adds where the analysis is and what to do, once.
    assert error.startswith("RunnerError: source analysis is REJECT; package generation is blocked: ")
    assert error.count(REASON) == 1 and "RunnerError: RunnerError" not in error
    assert "Source analysis: " in error and "source.analysis.json" in error
    assert "The pattern does not fit in one package (8000 vectors)." in error and setting in error
    assert "make a copy of this project with Segmentation = automatic: no vector count is needed" in error
    # Building the same inputs again: the SDK names the kept FAILED reservation without reasons; the GUI adds them.
    again = _wait(svc.start_build(single["project_path"]))
    assert again["status"] == "failed" and "existing cache is incomplete/corrupt" in again["error"]
    assert again["error"].count(REASON) == 1 and setting in again["error"]
    # Positive control: the copy the hint describes (automatic, no vector count) builds as segments made to fit.
    copy = svc.init_project(request(tmp_path / "auto", stil=single["inputs"]["stil"]["path"],
                                    compile_mode="raw-event", loop_policy="expand", segmentation="auto",
                                    copy_of=single["project_path"]))
    assert copy["settings"]["segmentation"] == {"mode": "auto", "max_vectors": None}
    done = _wait(svc.start_build(copy["project_path"]))
    assert done["status"] == "done", done["error"]
    manifest = read_json(Path(done["result"]["bundle_dir"]) / "runner.bundle.json")
    assert manifest["package_kind"] == "segmented" and len(manifest["patterns"]) == 2
    assert (done["result"]["package_kind"], done["result"]["segment_count"]) == ("segmented", 2)


def test_other_build_failures_are_passed_through_unchanged(svc, tmp_path):
    info = svc.init_project(request(tmp_path / "scope", wait_preset="chunk-4ms", service_us="4000",
                                    service_source="declared, not measured", service_scope="loop-generic",
                                    max_total_wait_us="40000"))
    failed = _wait(svc.start_build(info["project_path"]))
    assert failed["status"] == "failed" and "scope" in failed["error"]
    assert "does not fit in one package" not in failed["error"] and "Source analysis: " not in failed["error"]
