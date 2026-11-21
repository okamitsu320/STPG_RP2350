"""Acceptance cases of the GUI runner on waited and segmented bundles (host only, simulated devices)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import time

import pytest

from stpg import constants as c
from stpg_runner import build_project
from stpg_runner.artifacts import RunnerError, read_json
from stpg_runner.cli import main as runner_main
from stpgview.runner import service as service_module
from stpgview.runner.service import ACKNOWLEDGEMENTS, RunnerService
from stpgview.runner.simulated import SimulatedStpg
from stpgview.load_bin import load_bin
from stpgview.session import Session

from stpg_public_patterns import counter_stil

from stpg_hardware_guard import forbid_hardware

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)  # real serial opens and USB metadata enumeration fail the test
RUNNER_STIL = Path(__file__).parent / "fixtures" / "runner" / "pattern.stil"
ACKS = {key: True for key in ACKNOWLEDGEMENTS}


def wait(job, timeout=120.0):
    end = time.monotonic() + timeout
    while job.status == "running":
        if time.monotonic() > end:
            raise AssertionError("job did not finish")
        time.sleep(0.02)
    return job.public(0)


@pytest.fixture
def svc(tmp_path):
    return RunnerService(config_dir=tmp_path / "cfg", device_run=True, demo=True, results_dir=tmp_path / "runs",
                         session=Session(tmp_path / "work"))


@pytest.fixture(scope="module")
def waited(tmp_path_factory):
    """1600 identical vectors with an explicit 4 ms wait preset (nonzero waits are applied)."""

    root = tmp_path_factory.mktemp("gui-waited")
    source = root / "long.stil"
    prefix = RUNNER_STIL.read_text(encoding="utf-8").split("Pattern MAIN")[0]
    source.write_text(prefix + "Pattern MAIN {\nW NORMAL;\n" + "\n".join("V { ALL = 1X; }" for _ in range(1600))
                      + "\n}\n", encoding="utf-8")
    svc = RunnerService(config_dir=root / "cfg", device_run=False, demo=False, results_dir=root / "r")
    project = svc.init_project({"stil": str(source), "directory": str(root / "project"), "wait_preset": "chunk-4ms",
                                "completion": "hold", "storage": "psram", "compile_mode": "auto",
                                "loop_policy": "preserve", "service_us": "4000", "service_scope": "non-loop",
                                "service_source": "synthetic, not measured", "max_total_wait_us": "40000"})
    result = wait(svc.start_build(project["project_path"]))
    assert result["status"] == "done", result["error"]
    return result["result"]


def counter_en_stil(n: int, split: int) -> str:
    """Address counter on every vector; EN=1 before ``split`` and 0 after, so later states are new."""

    lines = counter_stil(2).split("\n")
    head = lines[:lines.index("  W NORMAL;") + 1]
    body = []
    for i in range(n):
        a = (i // 4) % 256
        bits = "".join("1" if a >> b & 1 else "0" for b in range(8))
        body.append(f"  V {{ ALL = {bits}P{1 if i < split else 0}{'HL'[i % 2]}{'HLX'[i % 3]}; }}")
    return "\n".join(head + body + ["}"]) + "\n"


@pytest.fixture(scope="module")
def segmented(tmp_path_factory):
    """Counter pattern (3200 vectors) split into segments by the CUI."""

    root = tmp_path_factory.mktemp("gui-segmented")
    source = root / "counter.stil"
    source.write_text(counter_en_stil(3200, 1600), encoding="utf-8")
    assert runner_main(["init", str(source), "--directory", str(root / "project"), "--wait-preset", "none",
                        "--completion", "hold", "--max-segment-vectors", "1600"]) == 0
    return build_project(root / "project" / "project.stpg-project.json")


def test_waits_extend_vectors_without_renumbering(waited):
    manifest = read_json(Path(waited["bundle_dir"]) / "runner.bundle.json")
    assert len(manifest["patterns"]) == 1
    trace = load_bin(Path(waited["bundle_dir"]) / manifest["patterns"][0]["path"])
    assert trace.vector_count == 1600
    extended = [i for i in range(trace.vector_count) if trace.extra_ps[i]]
    assert extended, "the 4 ms preset should have inserted waits"
    schedule = read_json(Path(waited["bundle_dir"]) / "wait.proposed.json")
    assert any(boundary["wait_ticks"] > 0 for boundary in schedule["boundaries"])
    index = extended[0]
    assert trace.periods[index] > trace.periods[0]
    assert trace.starts[index + 1] - trace.starts[index] == trace.periods[index]


def test_waited_bundle_opens_and_runs_in_the_gui(svc, waited):
    info = svc.open_bundle(waited["bundle_dir"], waited["manifest_sha256"])
    assert info["verification"]["status"] == "PASS" and info["settings"]["wait"]["preset"] == "chunk-4ms"
    prep = svc.prepare_run({"bundle": waited["bundle_dir"], "manifest_sha256": waited["manifest_sha256"],
                            "device": "simulated"})
    result = wait(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))["result"]
    assert result["run"]["outcome"] == "COMPLETED"


def test_segmented_bundle_runs_every_segment_with_hold(svc, segmented):
    info = svc.open_bundle(str(segmented.bundle_dir), segmented.manifest_sha256)
    assert info["verification"]["status"] == "PASS" and info["package_kind"] == "segmented"
    segments = len(info["patterns"])
    assert segments >= 2
    prep = svc.prepare_run({"bundle": str(segmented.bundle_dir), "manifest_sha256": segmented.manifest_sha256,
                            "device": "simulated"})
    assert len(prep["summary"]["segments"]) == segments and prep["summary"]["external"] is None
    assert prep["summary"]["segments"][0]["run_flags"] & c.RUN_FLAG_SEGMENT_END_HOLD
    result = wait(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))["result"]
    assert result["run"]["outcome"] == "COMPLETED"
    assert result["run"]["completed_segments"] == segments and "external" not in result


def test_corrupt_unlisted_and_relocated_bundles(svc, segmented, tmp_path):
    copy = tmp_path / "relocated bundle"
    shutil.copytree(segmented.bundle_dir, copy)
    assert svc.open_bundle(str(copy), segmented.manifest_sha256)["verification"]["status"] == "PASS"
    (copy / "unlisted.txt").write_text("x")
    assert svc.open_bundle(str(copy), segmented.manifest_sha256)["verification"]["status"] == "REJECT"
    (copy / "unlisted.txt").unlink()
    target = next(p for p in copy.rglob("pattern.bin"))
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))
    rejected = svc.open_bundle(str(copy), segmented.manifest_sha256)
    assert rejected["verification"]["status"] == "REJECT" and rejected["verification"]["errors"]
    with pytest.raises(RunnerError):
        svc.prepare_run({"bundle": str(copy), "manifest_sha256": segmented.manifest_sha256, "device": "simulated"})


def test_forbidden_wft_and_ignored_options_are_rejected(svc, tmp_path):
    with pytest.raises(RunnerError, match="wait=none"):
        svc.init_project({"stil": str(RUNNER_STIL), "directory": str(tmp_path / "a"), "wait_preset": "none",
                          "completion": "hold", "storage": "psram", "compile_mode": "auto",
                          "loop_policy": "preserve", "forbidden_stil_rates": "NORMAL"})
    assert not (tmp_path / "a").exists()


def test_output_directory_must_be_new_and_outside_the_bundle(svc, segmented, tmp_path):
    base = {"bundle": str(segmented.bundle_dir), "manifest_sha256": segmented.manifest_sha256, "device": "simulated"}
    with pytest.raises(RunnerError, match="outside the immutable bundle"):
        svc.prepare_run({**base, "output_dir": str(Path(segmented.bundle_dir) / "results")})
    (tmp_path / "taken").mkdir()
    with pytest.raises(RunnerError, match="new result directory"):
        svc.prepare_run({**base, "output_dir": str(tmp_path / "taken")})


class _Variant(SimulatedStpg):
    fails = 0
    terminal_error = 0
    close_error = False

    def __init__(self, **kw):
        super().__init__(fails=type(self).fails, **kw)

    def _handle(self, command):
        if command.cmd == c.CMD_GET_STATUS and self.terminal_error and self.last.state == c.STATE_RUNNING:
            self._queue(replace(self.last, seq=command.seq, rsp=c.RSP_STATUS, state=c.STATE_ERROR,
                                error_code=self.terminal_error, run_progress=1))
            return
        super()._handle(command)

    def close(self):
        super().close()
        if type(self).close_error:
            raise OSError("injected close failure")


@pytest.mark.parametrize("variant,outcome,comparison", [
    ({"fails": 3}, "COMPLETED", "MISMATCH"),
    ({"terminal_error": 5}, "DEVICE_ERROR", "UNOBSERVED"),
    ({"close_error": True}, "REJECT", "UNOBSERVED"),
])
def test_device_outcomes_are_rendered_separately(svc, segmented, monkeypatch, variant, outcome, comparison):
    cls = type("Peer", (_Variant,), dict(variant))
    monkeypatch.setattr(service_module, "SimulatedStpg", cls)
    prep = svc.prepare_run({"bundle": str(segmented.bundle_dir), "manifest_sha256": segmented.manifest_sha256,
                            "device": "simulated"})
    job = wait(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))
    run = job["result"]["run"]
    assert (run["outcome"], run["comparison"]) == (outcome, comparison)
    assert run["firmware_validation_pass"] is False
    if outcome != "COMPLETED":
        assert run["errors"]
