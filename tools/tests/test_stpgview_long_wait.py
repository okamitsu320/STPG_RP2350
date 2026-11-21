"""GUI side of long Rate Vector periods: a long WFT period is source timing, and a streamed gap of 250 ms or more needs
firmware that advertises the timer-aware stream watchdog.

Public synthetic patterns, the simulated STPG, the simulated GPIO controller and the SDK's public accessory peer only.
Nothing here opens a real port or enumerates USB.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from stpg import constants as c
from stpg.pattern import unpack_rate_vector_full
from stpg_runner import build_project, init_project
from stpg_runner.artifacts import read_json
from stpg_runner.build import BUNDLE_NAME
from stpgview.load_bin import load_bin
from stpgview.runner import service as service_module
from stpgview.runner import simulated as simulated_module

from stpg_hardware_guard import forbid_hardware
from test_stpg_runner_long_wait import write_source
from test_stpgview_runner_accessories import (Traffic, external_config, gpio_plan, prepare, run, svc,  # noqa: F401
                                              wait)
from test_stpgview_results_r8 import ACKS, FAIL_LOG_ACK, compare_source as r8_compare_source

pytestmark = pytest.mark.stpg_host_only
WATCHDOG_TEXT = "timer-aware stream watchdog"
START_CHECK = ["INFO", "GET_CLOCK_INFO", "GET_STATUS"]


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


def with_watchdog(monkeypatch):
    """The simulated STPG (and its demo binding) as firmware that advertises CAP_STREAM_WAIT_WATCHDOG."""

    device = replace(simulated_module.SIMULATED_DEVICE,
                     capability=simulated_module.SIMULATED_DEVICE.capability | c.CAP_STREAM_WAIT_WATCHDOG)
    monkeypatch.setattr(simulated_module, "SIMULATED_DEVICE", device)
    monkeypatch.setattr(service_module, "SIMULATED_DEVICE", device)
    return device


def compare_source(path: Path, period: str = "3s", vectors: int = 12, slow_at: int = 7) -> Path:
    """D0 drives, D1 compares; one vector of the second half has a long WFT period."""

    text = r8_compare_source(path, vectors).read_text()
    table = text[text.index("  WaveformTable NORMAL"):text.index("}\nPattern MAIN")]
    text = text.replace(table, table + table.replace("NORMAL", "SLOW").replace("'400ns'", f"'{period}'"))
    lines = text.splitlines()
    first = lines.index("  W NORMAL;") + 1
    lines[first + slow_at:first + slow_at + 1] = ["  W SLOW;", lines[first + slow_at], "  W NORMAL;"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def gui_build(svc, source: Path, folder: Path, **settings) -> dict:
    info = svc.init_project({"stil": str(source), "directory": str(folder), "completion": "hold", "storage": "psram",
                             "compile_mode": "rate-vector", "loop_policy": "preserve", **settings})
    done = wait(svc.start_build(info["project_path"]))
    assert done["status"] == "done", done["error"]
    return done["result"]["bundle"]


def streamed(tmp_path, period="3s", name="project"):
    source = write_source(tmp_path / f"{name}.stil", period=period)
    return build_project(init_project(source, tmp_path / name, wait_preset="none", completion="hold",
                                      max_segment_vectors=6, compile_mode="rate-vector"))


def simulated_run(svc, bundle_dir, manifest_sha256, monkeypatch=None, **peer):
    if peer:
        stpg = simulated_module.SimulatedStpg
        monkeypatch.setattr(service_module, "SimulatedStpg", lambda: stpg(**peer))
    prep = svc.prepare_run({"bundle": str(bundle_dir), "manifest_sha256": manifest_sha256, "device": "simulated"})
    done = wait(svc.start_run({"nonce": prep["nonce"], "acknowledgements": {**ACKS, FAIL_LOG_ACK: True}}))
    assert done["status"] == "done", done["error"]
    return done["result"]


def assert_refused_before_reset(view):
    assert view["run"]["outcome"] == "REJECT" and view["run"]["completed_segments"] == 0
    assert WATCHDOG_TEXT in view["run"]["errors"][-1] and "not a host response timeout" in view["run"]["errors"][-1]
    assert [row["request"] for row in view["exchanges"]["rows"]] == START_CHECK
    assert all(row["error"] is None and row["answer"].endswith("IDLE") for row in view["exchanges"]["rows"])
    assert not view["exchanges"]["run_write_attempted"] and not view["exchanges"]["link_lost"]
    # none of the notices for another symptom
    for key in ("start_check", "left_loaded", "stopped_at", "identity_mismatch", "fail_log_stop"):
        assert view[key] is None, key
    assert view["segments"] == []


# ---- 1: a long WFT period builds into mixed narrow/wide segments -----------------------------------------------

def test_the_gui_builds_and_verifies_mixed_narrow_and_wide_segments(svc, tmp_path):
    source = write_source(tmp_path / "synthetic.stil")  # 12 vectors, one 10 s WFT period
    bundle = gui_build(svc, source, tmp_path / "project", wait_preset="chunk-4ms", service_us="1",
                       service_source="synthetic budget", service_scope="non-loop", max_total_wait_us="10",
                       max_segment_vectors="6")
    assert bundle["verification"]["status"] == "PASS" and bundle["package_kind"] == "segmented"
    root = Path(bundle["bundle_dir"])
    payloads = [(root / item["path"]).read_bytes() for item in bundle["patterns"]]
    assert [unpack_rate_vector_full(p)[0].version for p in payloads] == [c.STPG_PATTERN_VERSION,
                                                                       c.STPG_PATTERN_VERSION_WIDE_WAIT]
    # the same bundle opened again (Open bundle…) verifies, and the plan has both segments
    reopened = svc.open_bundle(str(root), bundle["manifest_sha256"])
    assert reopened["verification"]["status"] == "PASS"
    # the viewer decodes each segment; the 10 s period stays a period, not an inserted wait
    traces = [load_bin(root / item["path"]) for item in bundle["patterns"]]
    assert [t.vector_count for t in traces] == [6, 6]


def test_the_logic_analyzer_timeline_reads_a_mixed_bundle(svc, tmp_path):
    timeline = pytest.importorskip("stpg_capture.timeline")
    from stpg_runner.progress import Reporter

    source = write_source(tmp_path / "synthetic.stil")
    bundle = gui_build(svc, source, tmp_path / "project", wait_preset="none", max_segment_vectors="6")
    loaded = timeline._load_bundle_timeline(Path(bundle["bundle_dir"]), manifest_sha256=bundle["manifest_sha256"],
                                            progress=Reporter())
    assert [s.trace.vector_count for s in loaded.segments] == [6, 6]


# ---- 2: a streamed gap of 250 ms or more needs the firmware capability ------------------------------------------

@pytest.mark.parametrize("period", ["3s", "10s"])
def test_old_firmware_is_refused_before_reset_and_says_why(svc, tmp_path, monkeypatch, period):
    built = streamed(tmp_path, period)
    peers = []
    stpg = simulated_module.SimulatedStpg
    monkeypatch.setattr(service_module, "SimulatedStpg", lambda: peers.append(stpg()) or peers[-1])
    view = simulated_run(svc, built.bundle_dir, built.manifest_sha256)
    assert_refused_before_reset(view)
    assert peers[0].commands == [c.CMD_INFO, c.CMD_GET_CLOCK_INFO, c.CMD_GET_STATUS] and not peers[0].buffer
    assert view["overall"]["exit_code"] == 2


def test_with_a_gpio_controller_nothing_is_set_and_the_controller_is_not_started(svc, tmp_path, monkeypatch):
    built = streamed(tmp_path)
    gui = Traffic().install(monkeypatch)
    view = run(svc, prepare(svc, built, gpio=gpio_plan(built)))["result"]
    assert_refused_before_reset(view)
    assert gui.gpio.applied == [] and [k for k, _ in gui.events if k in ("gpio", "run")] == []
    control = view["control"]
    assert control["outcome"] != "COMPLETED" and not control.get("identity_lines")


def test_with_an_external_accessory_it_is_not_asked_to_collect(svc, tmp_path, monkeypatch):
    built = streamed(tmp_path)
    gui = Traffic().install(monkeypatch)
    view = run(svc, prepare(svc, built, external=external_config(tmp_path / "accessory", built)))["result"]
    assert_refused_before_reset(view)
    assert [m for k, m in gui.events if k == "accessory"].count("before_run") == 0
    assert view["external"]["outcome"] != "COMPLETED"


def test_firmware_with_the_capability_runs_both_segments(svc, tmp_path, monkeypatch):
    with_watchdog(monkeypatch)
    built = streamed(tmp_path)
    view = simulated_run(svc, built.bundle_dir, built.manifest_sha256)
    assert view["run"]["outcome"] == "COMPLETED" and view["run"]["completed_segments"] == 2, view["run"]["errors"]
    assert view["identity_mismatch"] is None


def test_updated_firmware_with_an_old_binding_names_the_new_capability(svc, tmp_path, monkeypatch):
    built = streamed(tmp_path)
    gui = Traffic().install(monkeypatch)  # the "dev" binding keeps the old capability
    reported = replace(simulated_module.SIMULATED_DEVICE,
                       capability=simulated_module.SIMULATED_DEVICE.capability | c.CAP_STREAM_WAIT_WATCHDOG)
    monkeypatch.setattr(simulated_module, "SIMULATED_DEVICE", reported)  # what the STPG now answers
    view = run(svc, prepare(svc, built))["result"]
    assert view["run"]["outcome"] == "REJECT" and gui.stpg.commands == [c.CMD_INFO]
    mismatch = view["identity_mismatch"]
    assert mismatch["only_reported"] == ["CAP_STREAM_WAIT_WATCHDOG"] and mismatch["only_expected"] == []
    assert WATCHDOG_TEXT not in " ".join(view["run"]["errors"])  # the binding is the first thing to fix


def test_ordinary_rates_stream_on_old_firmware(svc, tmp_path):
    built = streamed(tmp_path, "400ns")
    view = simulated_run(svc, built.bundle_dir, built.manifest_sha256)
    assert view["run"]["outcome"] == "COMPLETED" and view["run"]["completed_segments"] == 2, view["run"]["errors"]


def test_a_single_non_streamed_pattern_with_a_long_period_runs_on_old_firmware(svc, tmp_path):
    source = write_source(tmp_path / "single.stil", period="3s")
    built = build_project(init_project(source, tmp_path / "single", wait_preset="none", completion="hold",
                                       compile_mode="rate-vector"))
    assert built.package_kind == "single"
    view = simulated_run(svc, built.bundle_dir, built.manifest_sha256)
    assert view["run"]["outcome"] == "COMPLETED", view["run"]["errors"]


class DeviceTimeout(simulated_module.SimulatedStpg):
    """The STPG answers its second STATUS after RUN with state ERROR / ERR_TIMEOUT (a firmware timeout)."""

    def _queue(self, response):
        if response.rsp == c.RSP_STATUS and response.state == c.STATE_RUNNING and self.polls == 2:
            response = replace(response, state=c.STATE_ERROR, error_code=c.ERR_TIMEOUT)
        super()._queue(response)


def test_a_device_reported_timeout_is_named_and_is_not_a_missing_reply(svc, tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "SimulatedStpg", DeviceTimeout)
    built = streamed(tmp_path, "400ns")
    view = simulated_run(svc, built.bundle_dir, built.manifest_sha256)
    assert view["run"]["outcome"] == "DEVICE_ERROR"
    segment = view["segments"][0]
    assert (segment["outcome"], segment["terminal"]["error_code"], segment["error_name"]) == (
        "DEVICE_ERROR", c.ERR_TIMEOUT, "ERR_TIMEOUT")
    last = view["exchanges"]["rows"][-1]
    assert (last["request"], last["answer"], last["error"]) == ("GET_STATUS", "STATUS · ERROR", None)
    assert view["stopped_at"] is None  # the reply arrived: not the "no complete reply" diagnosis


def test_every_firmware_error_code_has_one_name():
    names = service_module._ERR_NAMES
    assert names[c.ERR_TIMEOUT] == "ERR_TIMEOUT" and names[c.ERR_DMA_ERROR] == "ERR_DMA_ERROR"
    codes = [value for name, value in vars(c).items() if name.startswith("ERR_") and type(value) is int]
    assert len(codes) == len(set(codes)) == len(names)


# ---- 3: compare failures in a wide segment map to the STIL ------------------------------------------------------

def test_fails_in_a_wide_segment_are_described_against_the_stil(svc, tmp_path, monkeypatch):
    with_watchdog(monkeypatch)
    source = compare_source(tmp_path / "compare.stil")
    bundle = gui_build(svc, source, tmp_path / "project", wait_preset="none", max_segment_vectors="6")
    root = Path(bundle["bundle_dir"])
    versions = [unpack_rate_vector_full((root / item["path"]).read_bytes())[0].version for item in bundle["patterns"]]
    assert versions == [c.STPG_PATTERN_VERSION, c.STPG_PATTERN_VERSION_WIDE_WAIT]
    view = simulated_run(svc, root, bundle["manifest_sha256"], monkeypatch, fails=2)
    assert view["run"]["comparison"] == "MISMATCH", view["run"]["errors"]
    described = svc.describe_run({"result": view["result_dir"]})
    assert described["available"], described.get("reason")
    lines = source.read_text().splitlines()
    vector_lines = [number for number, text in enumerate(lines, 1) if text.lstrip().startswith("V {")]
    for segment in described["description"]["segments"]:
        entries = segment["fail_log"]["entries"]
        assert entries, segment["index"]
        own = vector_lines[6 * segment["index"]:6 * segment["index"] + 6]  # 6 vectors per segment
        assert all(entry["source"]["stil_line"] in own for entry in entries), (segment["index"], entries)
    assert read_json(root / BUNDLE_NAME)["package_kind"] == "segmented"
