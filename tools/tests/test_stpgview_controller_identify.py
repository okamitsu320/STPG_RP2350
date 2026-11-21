"""Read-only GPIO controller identify through the GUI (host only, in-memory peers, hardware guard on).

Controller identify sends only the identity (v1) or status (legacy) request through the SDK session and keeps its
exchange in ``identify.json`` under the results folder. (Analyzer identify belongs to the separate
logic-analyzer tool.) Peers are in-memory models; the hardware guard forbids real serial opens and USB enumeration.
"""

from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from stpg_runner import ControllerConfig, identify_controller
from stpg_runner.artifacts import RunnerError
from stpg_runner.device import DeviceLease
from stpgview.runner import service as service_module
from stpgview.runner.service import RunnerService
from stpgview.runner.simulated import SIMULATED_DEVICE, SimulatedController, controller_ports
from stpgview.session import Session

from stpg_hardware_guard import forbid_hardware, plug_in_bound_stpgs
from test_stpgview_runner import call, server  # noqa: F401 - HTTP fixture

pytestmark = pytest.mark.stpg_host_only
CONTROLLER = ControllerConfig("MOCK-GPIO", 0x2E8A, 0x000A, "PUBLIC-GPIO-0001", "rp2040-gpio-v1", allow_high=(14,))
LEGACY = ControllerConfig("MOCK-XIAO", 0x2E8A, 0x000A, "PUBLIC-XIAO-0001", "legacy-3", allow_high=(2,),
                          legacy_pins=(0, 1, 2))


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture
def svc(tmp_path):
    service = RunnerService(config_dir=tmp_path / "cfg", device_run=True, demo=True, results_dir=tmp_path / "runs",
                            session=Session(tmp_path / "work"))
    service.store.save("devices", "dev", SIMULATED_DEVICE.to_dict())
    service.store.save("controllers", "gpio", CONTROLLER.to_dict())
    service.store.save("controllers", "xiao", LEGACY.to_dict())
    plug_in_bound_stpgs(service)
    return service


def wait(job, timeout=60.0):
    end = time.monotonic() + timeout
    while job.status == "running":
        assert time.monotonic() < end, "job did not finish"
        time.sleep(0.02)
    return job.public(0)


def _saved(result) -> dict:
    return json.loads(Path(result["result_file"]).read_text())


class OldSketch(SimulatedController):
    """The old three-channel sketch: line commands ``<ch><op>``, ``s``, ``?``; no identify command."""

    def __init__(self):
        super().__init__(legacy=True)

    def _command(self, text):
        if text and not text[0].isdigit() and text not in ("s", "S", "?"):
            return f"Invalid channel: {text[0]}"
        return super()._command(text)


def _inject(monkeypatch, peer):
    def injected(config, **kwargs):
        kwargs.update(stream_factory=lambda _config: peer, port_provider=lambda: controller_ports(config))
        return identify_controller(config, **kwargs)
    monkeypatch.setattr(service_module, "identify_controller", injected)


def test_identify_v1_sends_only_the_identity_request(svc, monkeypatch):
    peer = SimulatedController()
    _inject(monkeypatch, peer)
    result = wait(svc.start_controller_identify("gpio"))["result"]
    assert result["status"] == "IDENTIFIED" and result["protocol"] == "rp2040-gpio-v1"
    assert len(result["sent"]) == 1 and result["sent"][0].startswith("i ") and result["pin_commands_written"] == 0
    assert peer.applied == [] and peer.closed == 1 and result["physical_readback"] is False
    assert result["received_lines"][-1].startswith("STPG_GPIO_V1 ")


def test_identify_of_an_old_sketch_rejects_with_a_hint_and_writes_no_pin(svc, monkeypatch):
    peer = OldSketch()
    _inject(monkeypatch, peer)
    result = wait(svc.start_controller_identify("gpio"))["result"]
    assert result["status"] == "REJECT" and "legacy-3" in result["hint"]
    assert [w[:2] for w in peer.writes] == [b"i "] and peer.applied == [] and result["pin_commands_written"] == 0
    assert result["received_lines"] == ["Invalid channel: i"]
    peer = OldSketch()
    _inject(monkeypatch, peer)
    legacy = wait(svc.start_controller_identify("xiao"))["result"]
    assert legacy["status"] == "IDENTIFIED" and legacy["sent"] == ["s"] and legacy["hint"] is None
    assert legacy["legacy_states"] == {"0": "Hi-Z", "1": "Hi-Z", "2": "Hi-Z"} and peer.applied == []


def test_identify_respects_leases_device_run_and_the_demo(svc, monkeypatch, tmp_path):
    peer = SimulatedController()
    _inject(monkeypatch, peer)
    with DeviceLease(CONTROLLER):
        result = wait(svc.start_controller_identify("gpio"))["result"]
    assert result["status"] == "REJECT" and "lease" in result["error"] and peer.writes == []
    monkeypatch.setattr(service_module, "identify_controller", identify_controller)  # demo injects its own peer
    demo = wait(svc.start_controller_identify("simulated"))["result"]
    assert demo["status"] == "IDENTIFIED" and demo["simulated"] is True
    off = RunnerService(config_dir=tmp_path / "cfg", device_run=False, demo=False, results_dir=tmp_path / "runs",
                        session=Session(tmp_path / "work2"))
    with pytest.raises(RunnerError, match="disabled"):
        off.start_controller_identify("gpio")
    with pytest.raises(RunnerError):
        svc.start_controller_identify("missing")


def test_http_controller_identify_route(server):  # noqa: F811 - pytest fixture
    t = {"X-STPG-Token": "tok"}
    assert call(server, "GET", "/api/runner/control/identify", headers=t)[0] == 405
    status, job = call(server, "POST", "/api/runner/control/identify", {"controller": "simulated"}, t)
    assert status == 200 and job["device"] is True
    for _ in range(500):
        status, job = call(server, "GET", f"/api/runner/job?id={job['id']}", headers=t)
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["result"]["status"] == "IDENTIFIED" and job["result"]["pin_commands_written"] == 0


def test_controller_identify_result_points_at_its_saved_evidence(svc, monkeypatch):
    peer = SimulatedController()
    _inject(monkeypatch, peer)
    result = wait(svc.start_controller_identify("gpio"))["result"]
    assert result["status"] == "IDENTIFIED" and result["outcome"] == "COMPLETED" and result["result_persisted"]
    saved = _saved(result)
    assert saved["kind"] == "stpg_gpio_identify_result" and saved["status"] == "IDENTIFIED"
    assert Path(result["result_file"]).parent.parent == Path(svc.results_dir)
    assert saved["pin_commands_written"] == 0 and peer.applied == []
