"""GUI runner with the optional GPIO controller and external accessory (host only, synthetic inputs and byte models).

Every invocation goes through the SDK's ``run_with_accessories`` / ``run_with_external_accessory`` /
``manual_control`` with injected in-memory peers: the simulated STPG firmware peer, the RP2040 sketch's line protocol
(``SimulatedController``) and the SDK's public stdio peer (``fixtures/external_accessory/peer.py``, a separate
process that never touches a device). The hardware guard forbids real serial opens and USB metadata enumeration.
None of this is physical, electrical or Arduino-target evidence, and no logic-analyzer code is imported.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from stpg_runner import ControllerConfig, build_project
from stpg_runner import accessories, external_accessory
from stpg_runner.artifacts import RunnerError, read_json, write_json
from stpg_runner.cli import main as runner_main
from stpg_runner.controller import SUPPORTED_PINS
from stpg_runner.device import DeviceLease
from stpg_runner.external_binding import ExternalDeviceBinding
from stpgview.runner import service as service_module
from stpgview.runner.jobs import Busy
from stpgview.runner.service import ACKNOWLEDGEMENTS, RunnerService
from stpgview.runner.simulated import (SIMULATED_CONTROLLER, SIMULATED_DEVICE, SimulatedController, SimulatedStpg,
                                       controller_ports, simulated_ports)
from stpgview.session import Session

from stpg_hardware_guard import forbid_hardware, plug_in_bound_stpgs
from stpg_public_patterns import build_public as _build, counter_stil
from test_stpgview_runner import call, server  # noqa: F401 - HTTP fixture
from test_stpgview_runner_acceptance import counter_en_stil

pytestmark = pytest.mark.stpg_host_only
ACKS = {key: True for key in ACKNOWLEDGEMENTS}
# An analyzer binding as the separate logic-analyzer tool writes it; the core reads only its USB identity.
ANALYZER = ExternalDeviceBinding.from_dict({"kind": "stpg_capture_analyzer", "schema_version": 1, "port": "MOCK-LA",
                                            "vid": 0x2E8A, "pid": 0x000A, "serial_number": "PUBLIC-LA-0001",
                                            "limits": {"opaque": "to the core"}, "wiring": "same-gpio"})
PEER = Path(__file__).parent / "fixtures" / "external_accessory" / "peer.py"
CONTROLLER = ControllerConfig("MOCK-GPIO", 0x2E8A, 0x000A, "PUBLIC-GPIO-0001", "rp2040-gpio-v1", allow_high=(14, 26))
RUN_WITH_ACCESSORIES = accessories.run_with_accessories  # the SDK functions, before any test wraps them
RUN_WITH_EXTERNAL = external_accessory.run_with_external_accessory
MANUAL_CONTROL = service_module.manual_control
LEGACY = ControllerConfig("MOCK-XIAO", 0x2E8A, 0x000A, "PUBLIC-XIAO-0001", "legacy-3", allow_high=(2,),
                          legacy_pins=(0, 1, 2))


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture(scope="module")
def counter(tmp_path_factory):
    return _build(tmp_path_factory, "accessory-counter", counter_stil(1600))


@pytest.fixture(scope="module")
def segmented(tmp_path_factory):
    root = tmp_path_factory.mktemp("accessory-segmented")
    source = root / "counter.stil"
    source.write_text(counter_en_stil(3200, 1600), encoding="utf-8")
    assert runner_main(["init", str(source), "--directory", str(root / "project"), "--wait-preset", "none",
                        "--completion", "hold", "--max-segment-vectors", "1600"]) == 0
    return build_project(root / "project" / "project.stpg-project.json")


@pytest.fixture
def svc(tmp_path):
    service = RunnerService(config_dir=tmp_path / "cfg", device_run=True, demo=True, results_dir=tmp_path / "runs",
                            session=Session(tmp_path / "work"))
    service.store.save("devices", "dev", SIMULATED_DEVICE.to_dict())
    (service.store.root / "analyzers").mkdir(parents=True, exist_ok=True)  # written by the LA tool, read-only here
    write_json(service.store.root / "analyzers" / "la.json", {**ANALYZER.to_dict(), "limits": {"opaque": "to the core"}})
    service.store.save("controllers", "gpio", CONTROLLER.to_dict())
    plug_in_bound_stpgs(service)
    return service


def wait(job, timeout=120.0):
    end = time.monotonic() + timeout
    while job.status == "running":
        assert time.monotonic() < end, "job did not finish"
        time.sleep(0.02)
    return job.public(0)


def external_config(root, built=None, *, scenario="normal", mode="device", segment=0, timeout=5, binding=None):
    """A local stpg_external_accessory config for the SDK's public stdio peer (written as the accessory tool would)."""

    root.mkdir(parents=True, exist_ok=True)
    binding_file, plan_file = root / "binding.json", root / "plan.json"
    write_json(binding_file, (binding or ANALYZER).to_dict())
    write_json(plan_file, {"kind": "public_peer_plan", "schema_version": 1})
    path = root / "external.json"
    write_json(path, {"kind": "stpg_external_accessory", "schema_version": 1, "identifier": "public-peer",
                      "command": [sys.executable, str(PEER.resolve()), scenario],
                      "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
                      "binding_file": "binding.json", "binding_sha256": hashlib.sha256(binding_file.read_bytes()).hexdigest(),
                      "plan_file": "plan.json", "plan_sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest(),
                      "segment": segment, "mode": mode, "timeout_s": timeout})
    return path


class _LoggedStdin:
    """The child's stdin, logging each request method in the cross-device event list."""

    def __init__(self, raw, events):
        self.raw, self.events = raw, events

    def write(self, data):
        self.events.append(("accessory", json.loads(bytes(data))["method"]))
        return self.raw.write(data)

    def __getattr__(self, name):
        return getattr(self.raw, name)


class FaultyController(SimulatedController):
    """The sketch's protocol with one injected fault at the ``at``-th write (1 = identity / legacy status)."""

    def __init__(self, fault=None, at=0, **kwargs):
        super().__init__(**kwargs)
        self.fault, self.at, self.close_error = fault, at, fault == "close"

    def write(self, data):
        if len(self.writes) + 1 == self.at:
            if self.fault == "write":
                self.writes.append(bytes(data))
                raise OSError("synthetic write failure: unknown written-byte count")
            if self.fault == "short":
                self.writes.append(bytes(data))
                return len(data) - 1
            if self.fault == "bad":
                count = super().write(data)
                self.rx[:] = self.rx.replace(b"CH", b"XX")
                return count
            if self.fault == "partial":  # only a prefix of the ACK ever arrives
                count = super().write(data)
                del self.rx[8:]
                return count
        return super().write(data)

    def close(self):
        super().close()
        if self.close_error:
            raise OSError("synthetic controller close failure")


class Traffic:
    """Injected peers for one invocation; records the cross-device order of GPIO, accessory requests and RUN."""

    def __init__(self, *, gpio=None, controller=CONTROLLER, gpio_ports=None):
        self.events: list[tuple[str, object]] = []
        self.gpio = gpio or SimulatedController()
        inner = self.gpio.on_write
        self.gpio.on_write = lambda data: (self.events.append(("gpio", data)), inner and inner(data))
        self.controller = controller
        self.gpio_ports = gpio_ports
        self.stpg = None
        self.stpg_opened = 0
        self.launched = 0

    def stpg_factory(self, _config):
        self.stpg_opened += 1
        self.stpg = SimulatedStpg(on_run=lambda index: self.events.append(("run", index)))
        return self.stpg

    def process_factory(self, *args, **kwargs):
        self.launched += 1
        process = subprocess.Popen(*args, **kwargs)
        process.stdin = _LoggedStdin(process.stdin, self.events)
        return process

    def options(self):
        return dict(stream_factory=self.stpg_factory, port_provider=simulated_ports,
                    controller_stream_factory=lambda _config: self.gpio,
                    controller_port_provider=self.gpio_ports or (lambda: controller_ports(self.controller)),
                    sleep=lambda seconds: time.sleep(min(seconds, 0.01)))

    def install(self, monkeypatch):
        def wrapped(*args, **kwargs):
            kwargs.update(self.options())
            return RUN_WITH_ACCESSORIES(*args, **kwargs)

        def external(*args, **kwargs):
            kwargs.update(self.options(), process_factory=self.process_factory)
            return RUN_WITH_EXTERNAL(*args, **kwargs)

        monkeypatch.setattr(service_module, "run_with_accessories", wrapped)
        monkeypatch.setattr(accessories, "run_with_accessories", wrapped)  # the stpg-runner CLI imports it here
        monkeypatch.setattr(service_module, "run_with_external_accessory", external)
        monkeypatch.setattr(external_accessory, "run_with_external_accessory", external)  # and this one here
        return self


def install_manual(monkeypatch, peer, *, config=CONTROLLER, ports=None):
    def wrapped(*args, **kwargs):
        kwargs.update(stream_factory=lambda _config: peer, port_provider=ports or (lambda: controller_ports(config)),
                       sleep=lambda seconds: None)
        return MANUAL_CONTROL(*args, **kwargs)

    monkeypatch.setattr(service_module, "manual_control", wrapped)


def gpio_plan(built, actions=None):
    return {"kind": "stpg_gpio_action_plan", "schema_version": 1, "manifest_sha256": built.manifest_sha256,
            "actions": actions if actions is not None else [
                {"segment": 0, "phase": "before_run", "pin": 14, "state": "HIGH"},
                {"segment": 0, "phase": "after_terminal", "pin": 14, "state": "Hi-Z"}]}


def prepare(svc, built, *, gpio=None, external=None, controller="gpio", device="dev"):
    request = {"bundle": str(built.bundle_dir), "manifest_sha256": built.manifest_sha256, "device": device}
    if gpio is not None:
        request["control"] = {"controller": controller, "plan": gpio}
    if external is not None:
        request["external"] = {"config": str(external)}
    return svc.prepare_run(request)


def run(svc, prep):
    return wait(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))


def first(events, kind, predicate=lambda data: True):
    return next(i for i, (k, data) in enumerate(events) if k == kind and predicate(data))


# ---- all four accessory combinations, GUI and CLI with the same plan files ---------------------------------

@pytest.mark.parametrize("with_gpio,with_ext", [(False, False), (True, False), (False, True), (True, True)])
def test_four_accessory_combinations_match_the_cli(svc, counter, tmp_path, monkeypatch, capsys, with_gpio, with_ext):
    built = counter
    config = external_config(tmp_path / "accessory") if with_ext else None
    plan = gpio_plan(built) if with_gpio else None
    gui = Traffic().install(monkeypatch)
    prep = prepare(svc, built, gpio=plan, external=config)
    assert prep["summary"]["accessories"] == {"controller": "gpio" if with_gpio else None,
                                              "external": "public-peer" if with_ext else None}
    view = run(svc, prep)["result"]
    assert view["run"]["outcome"] == "COMPLETED" and view["overall"]["exit_code"] == 0
    out = Path(prep["summary"]["output_dir"])
    stpg_dir = out / "stpg" if with_ext else out
    if with_gpio:
        assert view["control"]["outcome"] == "COMPLETED" and view["control"]["result_persisted"]
        assert gui.gpio.applied == [(14, "HIGH"), (14, "Hi-Z")] and gui.gpio.closed == 1
        assert [r["acknowledged"] for r in view["control"]["requests"]] == [True, True]
        assert view["control"]["uncertain"] == [] and read_json(stpg_dir / "control.json")["outcome"] == "COMPLETED"
    else:
        assert view["control"] is None and gui.gpio.writes == [] and not (stpg_dir / "control.json").exists()
    if with_ext:
        x = view["external"]
        assert (x["identifier"], x["outcome"], x["adopted"], x["result_persisted"]) == ("public-peer", "COMPLETED", True, True)
        assert read_json(out / "accessory" / "session.json")["outcome"] == "COMPLETED" and gui.launched == 1
        assert [m for k, m in gui.events if k == "accessory"] == ["open", "before_run", "after_terminal"]
        assert first(gui.events, "accessory", lambda m: m == "before_run") < first(gui.events, "run")
        assert first(gui.events, "accessory", lambda m: m == "after_terminal") > first(gui.events, "run")
    else:
        assert "external" not in view and gui.launched == 0 and not (out / "accessory").exists()
    if with_gpio and with_ext:  # GPIO before_run, then the accessory's ARMED request, then RUN
        gpio_set = first(gui.events, "gpio", lambda data: data.endswith(b"14h\n"))
        assert gpio_set < first(gui.events, "accessory", lambda m: m == "before_run") < first(gui.events, "run")
    # The same files, saved by the GUI or written by the accessory tool, run through the stpg-runner CLI.
    args = ["run", str(built.bundle_dir), "--manifest-sha256", built.manifest_sha256,
            "--device-config", str(svc.store.root / "devices" / "dev.json"), "--output-dir", str(tmp_path / "cli"),
            "--confirm-run", "--progress", "none"]
    if with_gpio:
        saved = svc.save_plan({"plan": plan, "path": str(tmp_path / "control-plan.json")})
        args += ["--controller-config", str(svc.store.root / "controllers" / "gpio.json"),
                 "--control-plan", saved["path"]]
    if with_ext:
        args += ["--external-accessory", str(config)]
    capsys.readouterr()
    cli = Traffic().install(monkeypatch)
    if with_gpio or with_ext:
        assert runner_main(args) == view["overall"]["exit_code"] == 0
        report = json.loads(capsys.readouterr().out)
        assert report["run"]["outcome"] == view["run"]["outcome"]
        assert (report["control"] or {}).get("outcome") == (view["control"] or {}).get("outcome")
        assert (report["external"] or {}).get("outcome") == (view.get("external") or {}).get("outcome")
    else:  # plain CLI run: the ordinary SDK path, not the accessory composition
        monkeypatch.setattr("stpg_runner.cli.run_bundle", lambda *a, **k: accessories.run_bundle(
            *a, **k, stream_factory=cli.stpg_factory, port_provider=simulated_ports, sleep=lambda s: None))
        assert runner_main(args) == 0
    assert cli.gpio.applied == gui.gpio.applied and cli.launched == gui.launched


def test_stpg_only_run_never_contacts_an_accessory(svc, counter, monkeypatch):
    built = counter

    def untouched(*_args):
        raise AssertionError("an unselected accessory was enumerated or opened")

    traffic = Traffic(gpio_ports=untouched).install(monkeypatch)
    view = run(svc, prepare(svc, built))["result"]
    assert view["run"]["outcome"] == "COMPLETED" and view["control"] is None and "external" not in view
    assert view["overall"] == {"exit_code": 0, "text": service_module.EXIT_TEXT[0]}
    assert traffic.gpio.writes == [] and traffic.stpg_opened == 1 and traffic.launched == 0


# ---- selection, validation and freezing ----------------------------------------------------------------------

def test_selected_missing_or_invalid_controller_rejects_before_any_job(svc, counter, tmp_path):
    built = counter
    with pytest.raises(RunnerError, match="no controller binding named 'nope'"):
        prepare(svc, built, gpio=gpio_plan(built), controller="nope")
    (svc.store.root / "controllers" / "broken.json").write_text('{"kind": "stpg_gpio_controller"}')
    with pytest.raises(RunnerError):
        prepare(svc, built, gpio=gpio_plan(built), controller="broken")
    assert svc.bindings("controllers")["items"][0]["name"] == "simulated"
    assert any(item.get("error") for item in svc.bindings("controllers")["items"])
    with pytest.raises(RunnerError, match="1..128"):
        prepare(svc, built, gpio=gpio_plan(built, []))
    with pytest.raises(RunnerError, match="GPIO action plan"):
        prepare(svc, built, gpio="not a plan")
    with pytest.raises(RunnerError, match="cannot be combined with the simulated STPG"):
        prepare(svc, built, gpio=gpio_plan(built), device="simulated")
    with pytest.raises(RunnerError, match="simulated GPIO controller"):
        prepare(svc, built, gpio=gpio_plan(built), controller="simulated")
    off = RunnerService(config_dir=svc.store.root, device_run=False, demo=False, results_dir=tmp_path / "off")
    with pytest.raises(RunnerError, match="disabled"):
        off.prepare_control({"operation": "set", "controller": "gpio", "pin": 14, "state": "LOW"})
    assert not svc.jobs.jobs


def test_selected_controller_unplugged_rejects_without_fallback(svc, counter, monkeypatch):
    built = counter
    traffic = Traffic(gpio_ports=lambda: []).install(monkeypatch)
    view = run(svc, prepare(svc, built, gpio=gpio_plan(built)))["result"]
    assert view["control"]["outcome"] == "REJECT" and view["run"]["outcome"] == "REJECT"
    assert view["overall"]["exit_code"] == 2 and traffic.gpio.writes == []
    assert traffic.stpg.runs == 0, "RUN was sent although the selected controller is missing"


def test_confirmation_freezes_controller_binding_and_plan(svc, counter, monkeypatch):
    built = counter
    traffic = Traffic().install(monkeypatch)
    plan = gpio_plan(built)
    prep = prepare(svc, built, gpio=plan)
    svc.store.save("controllers", "gpio", replace(CONTROLLER, allow_high=(14,)).to_dict(), replace=True)
    with pytest.raises(RunnerError, match="GPIO controller binding changed"):
        svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS})
    with pytest.raises(RunnerError, match="already used"):  # consumed once, even though it was rejected
        svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS})
    svc.store.save("controllers", "gpio", CONTROLLER.to_dict(), replace=True)
    prep = prepare(svc, built, gpio=plan)
    svc.store.delete("controllers", "gpio")
    with pytest.raises(RunnerError, match="no controller binding"):
        svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS})
    svc.store.save("controllers", "gpio", CONTROLLER.to_dict())
    prep = prepare(svc, built, gpio=plan)
    plan["actions"].append({"segment": 0, "phase": "after_terminal", "pin": 26, "state": "HIGH"})  # after confirm
    view = run(svc, prep)["result"]
    assert traffic.gpio.applied == [(14, "HIGH"), (14, "Hi-Z")], "an edit after confirmation leaked into the run"
    assert [a["pin"] for a in view["control"]["planned_actions"]] == [14, 14]
    assert not svc.jobs.active_device_job()


def test_concurrent_tabs_cannot_start_a_second_device_job(svc, counter, monkeypatch):
    built = counter
    entered, release = threading.Event(), threading.Event()

    def block(data):
        if data.endswith(b"14h\n"):
            entered.set()
            release.wait(10)

    Traffic(gpio=SimulatedController(on_write=block)).install(monkeypatch)
    tab_a, tab_b = prepare(svc, built, gpio=gpio_plan(built)), prepare(svc, built, gpio=gpio_plan(built))
    job = svc.start_run({"nonce": tab_a["nonce"], "acknowledgements": ACKS})
    assert entered.wait(10)
    with pytest.raises(Busy):
        svc.start_run({"nonce": tab_b["nonce"], "acknowledgements": ACKS})
    with pytest.raises(Busy):  # a manual reset while the run owns the devices
        start_control(svc, svc.prepare_control({"operation": "set", "controller": "gpio", "pin": 26, "state": "LOW"}))
    release.set()
    assert wait(job)["result"]["run"]["outcome"] == "COMPLETED"
    with pytest.raises(RunnerError, match="already used"):
        svc.start_run({"nonce": tab_b["nonce"], "acknowledgements": ACKS})


@pytest.mark.parametrize("alias", [{"serial_number": SIMULATED_DEVICE.serial_number}, {"port": SIMULATED_DEVICE.port},
                                   {"serial_number": ANALYZER.serial_number}, {"port": ANALYZER.port}])
def test_controller_aliasing_stpg_or_analyzer_rejects(svc, counter, alias, tmp_path):
    built = counter
    config = external_config(tmp_path / "accessory")
    svc.store.save("controllers", "gpio", replace(CONTROLLER, **alias).to_dict(), replace=True)
    with pytest.raises(RunnerError, match="aliases"):
        prepare(svc, built, gpio=gpio_plan(built), external=config)
    target = "stpg" if alias in ({"serial_number": SIMULATED_DEVICE.serial_number}, {"port": SIMULATED_DEVICE.port}) else "la"
    with pytest.raises(RunnerError, match="aliases"):
        svc.prepare_control({"operation": "reset", "controller": "gpio", "target": target,
                             "target_binding": "dev" if target == "stpg" else "la", "pulse_ms": 10})
    assert list(svc.jobs.jobs.values()) == []  # rejected before any job or launch


@pytest.mark.parametrize("change", [
    {"pin": 0, "state": "LOW"}, {"pin": 1, "state": "Hi-Z"}, {"pin": 9, "state": "LOW"},
    {"pin": 15, "state": "HIGH"}, {"pin": True, "state": "LOW"}, {"state": "high"},
    {"phase": "after_run"}, {"segment": 1}, {"segment": -1}, {"extra": 1}])
def test_run_attached_pin_phase_and_high_restrictions(svc, counter, monkeypatch, change):
    built = counter
    traffic = Traffic().install(monkeypatch)
    plan = gpio_plan(built)
    plan["actions"][0].update(change)
    with pytest.raises(RunnerError):
        prepare(svc, built, gpio=plan)
    assert traffic.gpio.writes == [] and traffic.stpg_opened == 0 and not svc.jobs.jobs


@pytest.mark.parametrize("mutate", ["order", "manifest", "too_many", "unknown_top"])
def test_action_plan_structure_rejects(svc, counter, mutate):
    built = counter
    plan = gpio_plan(built)
    if mutate == "order":
        plan["actions"].reverse()
    elif mutate == "manifest":
        plan["manifest_sha256"] = "0" * 64
    elif mutate == "too_many":
        plan["actions"] = [{"segment": 0, "phase": "before_run", "pin": 14, "state": "LOW"}] * 129
    else:
        plan["note"] = "x"
    with pytest.raises(RunnerError):
        prepare(svc, built, gpio=plan)


# ---- manual reset / set ----------------------------------------------------------------------------------------

def start_control(svc, prep, acks=None):
    return svc.start_control({"nonce": prep["nonce"], "acknowledgements": acks or {k: True for k in prep["acknowledgements"]}})


def reset(svc, target="stpg", **extra):
    request = {"operation": "reset", "controller": "gpio", "target": target,
               "target_binding": "dev" if target == "stpg" else "la", "pulse_ms": 15, "settle_ms": 0}
    request.update(extra)
    return svc.prepare_control(request)


@pytest.mark.parametrize("target,pin", [("stpg", 0), ("la", 1)])
def test_manual_reset_is_low_then_hiz_with_tagged_acks(svc, monkeypatch, target, pin):
    peer = SimulatedController()
    install_manual(monkeypatch, peer)
    prep = reset(svc, target)
    assert set(prep["acknowledgements"]) == {"wired", "interrupt", "aftermath"}
    assert any("NOT opened" in step for step in prep["summary"]["steps"])
    with pytest.raises(RunnerError, match="checkbox"):
        start_control(svc, prep, {"wired": True})
    prep = reset(svc, target)
    result = wait(start_control(svc, prep))["result"]["control"]
    assert result["outcome"] == "COMPLETED" and result["uncertain"] == [] and result["result_persisted"]
    assert peer.applied == [(pin, "LOW"), (pin, "Hi-Z")] and peer.closed == 1
    assert peer.writes[0].startswith(b"i ") and peer.writes[1].endswith(f" {pin}l\n".encode())
    assert result["electrical_release_verified"] is False
    assert read_json(Path(prep["summary"]["output_dir"]) / "control.json")["outcome"] == "COMPLETED"


def test_manual_la_reset_also_accepts_a_neutral_usb_binding(svc, monkeypatch):
    path = svc.store.root / "analyzers/la.json"
    binding = read_json(path)
    binding["kind"] = "stpg_external_device"
    write_json(path, binding)
    peer = SimulatedController()
    install_manual(monkeypatch, peer)
    prep = reset(svc, "la")
    result = wait(start_control(svc, prep))["result"]["control"]
    assert result["outcome"] == "COMPLETED" and peer.applied == [(1, "LOW"), (1, "Hi-Z")]
    assert peer.closed == 1


@pytest.mark.parametrize("changed", ["open_timeout_s", "close_timeout_s"])
def test_confirmation_displays_and_freezes_phase_limits(svc, counter, tmp_path, changed):
    path = external_config(tmp_path / "config")
    raw = read_json(path)
    raw.update(open_timeout_s=120, close_timeout_s=5)
    write_json(path, raw)
    prep = prepare(svc, counter, external=path)
    summary = prep["summary"]["external"]
    assert summary["open_timeout_s"] == 120 and summary["close_timeout_s"] == 5 and summary["timeout_s"] == 5
    raw[changed] += 1
    write_json(path, raw)
    with pytest.raises(RunnerError, match="external accessory selection changed"):
        svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS})


@pytest.mark.parametrize("request_change,match", [
    ({"pulse_ms": None}, "pulse"), ({"pulse_ms": 0}, "pulse"), ({"pulse_ms": 5001}, "pulse"),
    ({"pulse_ms": True}, "pulse"), ({"pulse_ms": "100"}, "pulse"), ({"settle_ms": 10001}, "settle"),
    ({"settle_ms": -1}, "settle"), ({"target": "fpga"}, "target"), ({"target_binding": ""}, "binding"),
    ({"allow_run_high": True}, "never drives HIGH"), ({"allow_run_high": "yes"}, "boolean"),
    ({"controller": ""}, "controller")])
def test_manual_reset_needs_declared_pulse_and_target(svc, request_change, match):
    with pytest.raises(RunnerError, match=match):
        reset(svc, **request_change)
    assert not svc.confirmations


@pytest.mark.parametrize("request_change,match", [
    ({"pin": 9}, "unsupported"), ({"pin": 0}, "binding"), ({"pin": 0, "target_binding": "la"}, "no device binding"),
    ({"pin": 0, "state": "HIGH", "target_binding": "dev"}, "HIGH"), ({"pin": 15, "state": "HIGH"}, "HIGH"),
    ({"settle_ms": 5}, "settle"), ({"state": "ON"}, "unsupported")])
def test_manual_set_restrictions(svc, request_change, match):
    request = {"operation": "set", "controller": "gpio", "pin": 26, "state": "LOW"}
    request.update(request_change)
    with pytest.raises(RunnerError, match=match):
        svc.prepare_control(request)


def test_run_line_high_needs_allow_list_and_explicit_override(svc, monkeypatch):
    request = {"operation": "set", "controller": "gpio", "pin": 0, "state": "HIGH", "target_binding": "dev",
               "allow_run_high": True}
    with pytest.raises(RunnerError, match="HIGH"):  # GPIO0 not in this binding's allow-list
        svc.prepare_control(request)
    svc.store.save("controllers", "gpio", replace(CONTROLLER, allow_high=(0, 14, 26)).to_dict(), replace=True)
    with pytest.raises(RunnerError, match="HIGH"):
        svc.prepare_control({**request, "allow_run_high": False})
    prep = svc.prepare_control(request)
    assert {"run_line", "run_high"} <= set(prep["acknowledgements"])
    peer = SimulatedController()
    install_manual(monkeypatch, peer, config=replace(CONTROLLER, allow_high=(0, 14, 26)))
    assert wait(start_control(svc, prep))["result"]["control"]["outcome"] == "COMPLETED"
    assert peer.applied == [(0, "HIGH")]


def test_legacy_controller_uses_only_the_old_commands(svc, counter, monkeypatch):
    built = counter
    svc.store.save("controllers", "xiao", LEGACY.to_dict())
    peer = SimulatedController(legacy=True)
    install_manual(monkeypatch, peer, config=LEGACY)
    prep = svc.prepare_control({"operation": "set", "controller": "xiao", "pin": 2, "state": "HIGH"})
    result = wait(start_control(svc, prep))["result"]["control"]
    assert result["outcome"] == "COMPLETED" and result["legacy_uncorrelated"] is True
    assert peer.writes == [b"s\n", b"2h\n"]
    with pytest.raises(RunnerError):  # a legacy run action can only be channel 2 (0/1 are RUN)
        prepare(svc, built, controller="xiao", gpio=gpio_plan(built, [{"segment": 0, "phase": "before_run",
                                                                         "pin": 3, "state": "LOW"}]))
    prep = prepare(svc, built, controller="xiao", gpio=gpio_plan(built, [{"segment": 0, "phase": "before_run",
                                                                           "pin": 2, "state": "LOW"}]))
    assert prep["summary"]["control"]["actions"][0]["physical"] == "legacy channel 2 -> pin 2"


@pytest.mark.parametrize("owner", ["stpg", "la", "controller"])
def test_manual_reset_rejects_while_another_process_owns_a_device(svc, monkeypatch, owner):
    peer = SimulatedController()
    install_manual(monkeypatch, peer)
    prep = reset(svc, "la" if owner == "la" else "stpg")
    with DeviceLease({"stpg": SIMULATED_DEVICE, "la": ANALYZER, "controller": CONTROLLER}[owner]):
        job = wait(start_control(svc, prep))
    if owner == "controller":  # the SDK records its own lease refusal before any controller byte
        result = job["result"]["control"]
        assert result["outcome"] == "REJECT" and any("leased" in e for e in result["errors"])
    else:  # the target lease is taken before the controller is touched
        assert job["status"] == "failed" and "leased" in job["error"]
    assert peer.writes == [] and peer.closed == 0


def test_failed_reset_reports_that_run_may_stay_low(svc, monkeypatch):
    peer = FaultyController("bad", at=3)  # identity, LOW (acknowledged), Hi-Z (garbled ACK)
    install_manual(monkeypatch, peer)
    result = wait(start_control(svc, reset(svc)))["result"]["control"]
    assert result["outcome"] == "REJECT" and len(peer.writes) == 3 and peer.closed == 1
    assert [r["acknowledged"] for r in result["requests"]] == [True, False]
    assert any("may still be held LOW" in text for text in result["uncertain"])
    assert result["last_acknowledged_states"] == {"0": "LOW"}


def test_partial_ack_is_reported_unacknowledged_without_resend(svc, monkeypatch):
    peer = FaultyController("partial", at=2)
    ticks = iter(range(10**6))

    def wrapped(*args, **kwargs):  # a clock that advances on every read, so the 5 s reply bound expires at once
        return MANUAL_CONTROL(*args, **kwargs, stream_factory=lambda _config: peer,
                              port_provider=lambda: controller_ports(CONTROLLER), clock=lambda: next(ticks) * 0.5)

    monkeypatch.setattr(service_module, "manual_control", wrapped)
    prep = svc.prepare_control({"operation": "set", "controller": "gpio", "pin": 26, "state": "HIGH"})
    result = wait(start_control(svc, prep))["result"]["control"]
    assert result["outcome"] == "REJECT" and len(peer.writes) == 2 and peer.closed == 1
    assert result["requests"] == [{"pin": 26, "state": "HIGH", "written_bytes": len(peer.writes[1]), "error": None,
                                   "acknowledged": False}]
    assert any("may or may not have been applied" in text for text in result["uncertain"])
    assert any("timeout" in error for error in result["errors"])


def test_manual_confirmation_is_frozen(svc, monkeypatch):
    peer = SimulatedController()
    install_manual(monkeypatch, peer)
    prep = reset(svc)
    svc.store.save("controllers", "gpio", replace(CONTROLLER, allow_high=(14,)).to_dict(), replace=True)
    with pytest.raises(RunnerError, match="controller binding changed"):
        start_control(svc, prep)
    svc.store.save("controllers", "gpio", CONTROLLER.to_dict(), replace=True)
    prep = reset(svc)
    svc.store.save("devices", "dev", replace(SIMULATED_DEVICE, firmware_version=1).to_dict(), replace=True)
    with pytest.raises(RunnerError, match="target binding changed"):
        start_control(svc, prep)
    svc.store.save("devices", "dev", SIMULATED_DEVICE.to_dict(), replace=True)
    prep = reset(svc)
    svc.store.delete("devices", "dev")
    with pytest.raises(RunnerError, match="no device binding"):
        start_control(svc, prep)
    with pytest.raises(RunnerError, match="already used"):
        start_control(svc, prep)
    run_confirmation = svc.prepare_control({"operation": "set", "controller": "gpio", "pin": 26, "state": "LOW"})
    with pytest.raises(RunnerError, match="already used"):  # a GPIO confirmation never starts a run, and back
        svc.start_run({"nonce": run_confirmation["nonce"], "acknowledgements": ACKS})
    assert peer.writes == [] and not svc.jobs.jobs


def test_cancelled_reset_sends_no_corrective_command(svc, monkeypatch):
    peer = SimulatedController()

    def wrapped(*args, **kwargs):
        token = kwargs["cancel"]
        return MANUAL_CONTROL(*args, **kwargs, stream_factory=lambda _config: peer,
                        port_provider=lambda: controller_ports(CONTROLLER), sleep=lambda seconds: token.cancel())

    monkeypatch.setattr(service_module, "manual_control", wrapped)
    job = wait(start_control(svc, reset(svc)))
    result = job["result"]["control"]
    assert result["outcome"] == "CANCELLED" and job["status"] == "done"
    assert peer.applied == [(0, "LOW")] and any("may still be held LOW" in t for t in result["uncertain"])


# ---- failures inside a run seal later GPIO and STPG requests ----------------------------------------------------

@pytest.mark.parametrize("fault", ["bad", "write", "short"])
def test_controller_failure_before_run_sends_no_run(svc, counter, monkeypatch, fault):
    built = counter
    traffic = Traffic(gpio=FaultyController(fault, at=2)).install(monkeypatch)
    view = run(svc, prepare(svc, built, gpio=gpio_plan(built)))["result"]
    assert view["control"]["outcome"] == "REJECT" and view["run"]["outcome"] == "REJECT"
    assert view["overall"]["exit_code"] == 2 and traffic.stpg.runs == 0
    assert len(traffic.gpio.writes) == 2 and traffic.gpio.closed == 1
    assert view["control"]["uncertain"] and view["control"]["requests"][0]["acknowledged"] is False
    if fault == "write":
        assert view["control"]["requests"][0]["written_bytes"] is None


def test_controller_close_failure_keeps_the_completed_core(svc, counter, monkeypatch):
    built = counter
    Traffic(gpio=FaultyController("close")).install(monkeypatch)
    view = run(svc, prepare(svc, built, gpio=gpio_plan(built)))["result"]
    assert view["run"]["outcome"] == "COMPLETED" and view["control"]["outcome"] == "REJECT"
    assert view["overall"]["exit_code"] == 2


def test_control_checkpoint_failure_blocks_gpio_and_run(svc, counter, monkeypatch):
    built = counter
    traffic = Traffic().install(monkeypatch)

    def unavailable(*args, **kwargs):
        raise OSError("synthetic evidence storage failure")

    monkeypatch.setattr(accessories, "write_json", unavailable)
    view = run(svc, prepare(svc, built, gpio=gpio_plan(built)))["result"]
    assert view["control"]["outcome"] == "REJECT" and not view["control"]["result_persisted"]
    assert traffic.gpio.writes == [] and traffic.stpg.runs == 0 and view["overall"]["exit_code"] == 2


def test_cancel_during_a_before_run_action_stops_before_run(svc, counter, monkeypatch):
    built = counter

    def cancel(data):
        if data.endswith(b"14h\n"):
            svc.cancel(svc.jobs.active_device_job().id)

    traffic = Traffic(gpio=SimulatedController(on_write=cancel)).install(monkeypatch)
    job = run(svc, prepare(svc, built, gpio=gpio_plan(built)))
    view = job["result"]
    assert view["run"]["outcome"] == "CANCELLED" and view["control"]["outcome"] == "CANCELLED"
    assert view["overall"]["exit_code"] == 130 and job["status"] == "cancelled"
    assert traffic.stpg.runs == 0 and traffic.gpio.applied == [(14, "HIGH")]


def test_after_terminal_failure_stops_the_next_segment(svc, segmented, monkeypatch):
    traffic = Traffic(gpio=FaultyController("bad", at=2)).install(monkeypatch)
    plan = gpio_plan(segmented, [{"segment": 0, "phase": "after_terminal", "pin": 26, "state": "LOW"},
                                 {"segment": 1, "phase": "before_run", "pin": 26, "state": "Hi-Z"}])
    view = run(svc, prepare(svc, segmented, gpio=plan))["result"]
    assert view["control"]["outcome"] == "REJECT" and traffic.stpg.runs == 1
    assert len(traffic.gpio.writes) == 2, "an action after the rejected one was sent"
    assert view["overall"]["exit_code"] == 2


# ---- demo mode, plan files and HTTP routes -------------------------------------------------------------------

def test_demo_controller_run_and_manual_reset_are_simulated(tmp_path, counter):
    built = counter
    demo = RunnerService(config_dir=tmp_path / "cfg", device_run=False, demo=True, results_dir=tmp_path / "runs",
                         session=Session(tmp_path / "work"))
    prep = prepare(demo, built, device="simulated", controller="simulated", gpio=gpio_plan(built))
    view = run(demo, prep)["result"]
    assert view["run"]["outcome"] == "COMPLETED" and view["control"]["outcome"] == "COMPLETED"
    assert view["simulated"] is True
    prep = demo.prepare_control({"operation": "reset", "controller": "simulated", "target": "la",
                                 "target_binding": "simulated", "pulse_ms": 20})
    assert prep["summary"]["simulated"] is True
    assert wait(start_control(demo, prep))["result"]["control"]["outcome"] == "COMPLETED"
    with pytest.raises(RunnerError, match="mode 'device'"):  # a device-mode accessory never runs with the demo STPG
        prep = demo.prepare_run({"bundle": str(built.bundle_dir), "manifest_sha256": built.manifest_sha256,
                                 "device": "simulated", "external": {"config": str(external_config(tmp_path / "dev"))}})
    config = external_config(tmp_path / "accessory", mode="simulated")
    prep = demo.prepare_run({"bundle": str(built.bundle_dir), "manifest_sha256": built.manifest_sha256,
                             "device": "simulated", "external": {"config": str(config)},
                             "control": {"controller": "simulated", "plan": gpio_plan(built)}})
    view = run(demo, prep)["result"]
    assert view["overall"]["exit_code"] == 0 and view["control"]["outcome"] == "COMPLETED"
    assert view["external"]["mode"] == "simulated" and view["external"]["outcome"] == "COMPLETED"
    assert view["simulated"] is True


@pytest.mark.parametrize("scenario", ["open-status", "open-eof", "open-timeout"])
def test_an_accessory_that_is_not_ready_stops_before_the_stpg_run(svc, counter, tmp_path, monkeypatch, scenario):
    # e.g. the accessory found that its device's firmware differs from its binding: it never answers READY
    built = counter
    traffic = Traffic().install(monkeypatch)
    prep = prepare(svc, built, external=external_config(tmp_path / "accessory", scenario=scenario, timeout=1))
    view = run(svc, prep)["result"]
    assert view["external"]["outcome"] not in ("COMPLETED", "PENDING") and view["external"]["errors"]
    assert view["external"]["hardware_effects_possible"] is True and view["overall"]["exit_code"] == 2
    assert view["run"]["outcome"] == "NOT_STARTED" and traffic.stpg_opened == 0
    output = Path(prep["summary"]["output_dir"])
    assert (output / "accessory" / "session.json").is_file() and not (output / "stpg").exists()
    assert [m for k, m in traffic.events if k == "accessory"] == ["open"]  # no cleanup request after the failure


def test_confirmation_freezes_the_external_accessory_selection(svc, counter, tmp_path, monkeypatch):
    built = counter
    traffic = Traffic().install(monkeypatch)
    config = external_config(tmp_path / "accessory")
    prep = prepare(svc, built, external=config)
    raw = read_json(config)
    write_json(config, {**raw, "timeout_s": 3})  # edited after the confirmation
    with pytest.raises(RunnerError, match="changed after this run was confirmed"):
        svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS})
    write_json(config, raw)
    prep = prepare(svc, built, external=config)
    (tmp_path / "accessory" / "plan.json").write_text("{}")  # an input it names, changed after the confirmation
    job = run(svc, prep)  # the SDK re-hashes every named input before launching anything
    assert job["status"] == "failed" and "hash changed" in job["error"], job
    assert traffic.launched == 0 and traffic.stpg_opened == 0


def test_external_inspect_checks_files_only(svc, counter, tmp_path, monkeypatch):
    built = counter
    traffic = Traffic().install(monkeypatch)
    config = external_config(tmp_path / "accessory")
    request = {"bundle": str(built.bundle_dir), "manifest_sha256": built.manifest_sha256, "device": "dev",
               "config": str(config)}
    summary = svc.inspect_external(request)
    assert summary["identifier"] == "public-peer" and summary["segment"] == 0 and summary["mode"] == "device"
    assert summary["device"] == ANALYZER.to_dict() and len(summary["notes"]) >= 4
    with pytest.raises(RunnerError):
        svc.inspect_external({**request, "manifest_sha256": "1" * 64})
    with pytest.raises(RunnerError, match="mode 'device'"):
        svc.inspect_external({**request, "device": "simulated"})
    stpg_alias = external_config(tmp_path / "alias", binding=ExternalDeviceBinding.from_dict(
        {**SIMULATED_DEVICE.to_dict(), "kind": "stpg_external_device"}))
    with pytest.raises(RunnerError):
        svc.inspect_external({**request, "config": str(stpg_alias)})
    assert traffic.launched == 0 and traffic.stpg_opened == 0 and list(svc.jobs.jobs.values()) == []


def test_the_view_names_unadmitted_bytes_of_a_cancelled_exchange(svc, counter, tmp_path):
    built = counter
    session = {"outcome": "CANCELLED", "cancel_requested": True, "unadmitted_stdout_bytes": 57, "errors": [],
               "mode": "device", "prepared": {"spec": {"identifier": "public-peer"}}, "stderr_hex": b"bye\n".hex()}
    result = {"run": None, "external": session, "control": None, "run_result_file": None}
    view = svc._accessory_view(result, tmp_path, Path(built.bundle_dir), False)
    x = view["external"]
    assert (x["outcome"], x["cancel_requested"], x["unadmitted_stdout_bytes"], x["stderr_text"]) == (
        "CANCELLED", True, 57, "bye")
    assert view["overall"]["exit_code"] == 130


@pytest.mark.parametrize("reserved, outcome", [(False, "NOT_STARTED"), (True, "UNKNOWN")])
def test_a_missing_stpg_result_is_not_started_only_without_the_sdk_run_folder(svc, counter, tmp_path, reserved,
                                                                             outcome):
    # run_bundle reserves output/run before any port access; a reserved folder without a result is unknown
    built = counter
    if reserved:
        (tmp_path / "run").mkdir()
    result = {"run": None, "external": None, "control": None, "run_result_file": None}
    view = svc._accessory_view(result, tmp_path, Path(built.bundle_dir), True)
    assert view["run"]["outcome"] == outcome and view["run"]["result_persisted"] is False


def test_plan_files_round_trip_and_never_overwrite(svc, counter, tmp_path):
    built = counter
    saved = svc.save_plan({"plan": gpio_plan(built), "path": str(tmp_path / "plans" / "control-plan.json")})
    loaded = svc.control_plan_load({"path": saved["path"], "bundle": str(built.bundle_dir),
                                    "manifest_sha256": built.manifest_sha256, "controller": "gpio"})
    assert loaded["plan"] == gpio_plan(built)
    with pytest.raises(RunnerError, match="exists"):
        svc.save_plan({"plan": gpio_plan(built), "path": saved["path"]})
    with pytest.raises(RunnerError, match="immutable bundle"):
        svc.save_plan({"plan": gpio_plan(built), "path": str(built.bundle_dir / "x.json")})
    with pytest.raises(RunnerError, match="different bundle"):
        svc.control_plan_load({"path": saved["path"], "bundle": str(built.bundle_dir), "manifest_sha256": "1" * 64})
    with pytest.raises(RunnerError):  # analyzer plans belong to the logic-analyzer tool
        svc.save_plan({"plan": {"kind": "stpg_capture_plan", "schema_version": 1},
                       "path": str(tmp_path / "plans" / "capture-plan.json")})


def test_http_control_routes_and_no_analyzer_routes(server):  # noqa: F811 - pytest fixture
    t = {"X-STPG-Token": "tok"}
    status, state = call(server, "GET", "/api/runner/state", headers=t)
    assert status == 200 and state["controller"]["pins"] == list(SUPPORTED_PINS)
    assert state["external_protocol"] == "stpg_external_accessory_v1" and "wiring_presets" not in state
    status, items = call(server, "GET", "/api/runner/bindings?kind=controllers", headers=t)
    assert status == 200 and items["items"][0]["config"] == SIMULATED_CONTROLLER.to_dict()
    assert call(server, "GET", "/api/runner/control/start", headers=t)[0] == 405
    status, error = call(server, "POST", "/api/runner/control/prepare", {"operation": "reset", "controller": "simulated",
                                                                         "target": "stpg", "target_binding": "simulated"}, t)
    assert status == 400 and "pulse" in error["error"]
    status, prep = call(server, "POST", "/api/runner/control/prepare", {
        "operation": "reset", "controller": "simulated", "target": "stpg", "target_binding": "simulated", "pulse_ms": 5}, t)
    assert status == 200
    status, job = call(server, "POST", "/api/runner/control/start", {"nonce": prep["nonce"], "acknowledgements": {}}, t)
    assert status == 400 and "checkbox" in job["error"]
    status, prep = call(server, "POST", "/api/runner/control/prepare", {
        "operation": "reset", "controller": "simulated", "target": "stpg", "target_binding": "simulated", "pulse_ms": 5}, t)
    acks = {key: True for key in prep["acknowledgements"]}
    status, job = call(server, "POST", "/api/runner/control/start", {"nonce": prep["nonce"], "acknowledgements": acks}, t)
    assert status == 200 and job["device"] is True
    for _ in range(500):
        status, job = call(server, "GET", f"/api/runner/job?id={job['id']}", headers=t)
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["result"]["control"]["outcome"] == "COMPLETED"
    status, again = call(server, "POST", "/api/runner/control/start", {"nonce": prep["nonce"], "acknowledgements": acks}, t)
    assert status == 400 and "already used" in again["error"]
    for path in ("/api/runner/wiring", "/api/capture/plan", "/api/capture/mock", "/api/overlay"):
        assert call(server, "POST", path, {}, t)[0] == 404, path  # the analyzer belongs to its own program
    status, error = call(server, "POST", "/api/runner/external/inspect", {"bundle": "/nonexistent", "device": "simulated",
                                                                          "config": "/nonexistent.json"}, t)
    assert status == 400 and error["error"]


# ---- a run the STPG refuses at the start check (its first INFO), with and without GPIO actions -------------------
class _StartsIn(Traffic):
    """The simulated STPG starts in a given state instead of IDLE; nothing else changes."""

    def __init__(self, state, **kwargs):
        super().__init__(**kwargs)
        self.initial = state

    def stpg_factory(self, _config):
        peer = super().stpg_factory(_config)
        peer.last = replace(peer.last, state=self.initial)
        return peer


@pytest.mark.parametrize("with_gpio", [True, False])
def test_a_run_refused_at_the_start_check_says_so_and_no_gpio_action_was_started(svc, counter, monkeypatch, with_gpio):
    from stpg import constants as c

    traffic = _StartsIn(c.STATE_READY).install(monkeypatch)
    view = run(svc, prepare(svc, counter, gpio=gpio_plan(counter) if with_gpio else None))["result"]
    assert view["run"]["outcome"] == "REJECT" and traffic.stpg.commands == [c.CMD_INFO]  # INFO only: no upload
    assert view["start_check"] == {"request": "INFO", "answer": "INFO · READY", "state": "READY",
                                   "allowed_states": ["IDLE", "DONE"], "error_code": 0}
    assert traffic.gpio.writes == [] and view["overall"]["exit_code"] == 2
    if with_gpio:
        control = view["control"]
        assert control["outcome"] == "INCOMPLETE" and control["requests"] == []
        assert control["not_started"].startswith("the STPG refused the run at its start check")
        assert control["open_attempted"] is False  # the controller's port was never opened
    else:
        assert view["control"] is None


def test_a_controller_that_fails_after_the_start_check_is_not_called_not_started(svc, counter, monkeypatch):
    from stpg import constants as c

    class NoAck(SimulatedController):
        def _command(self, text):
            return None  # never acknowledges: a real controller failure, after the STPG start check passed

    traffic = _StartsIn(c.STATE_IDLE, gpio=NoAck()).install(monkeypatch)
    view = run(svc, prepare(svc, counter, gpio=gpio_plan(counter)))["result"]
    assert view["start_check"] is None and c.CMD_INFO in traffic.stpg.commands
    control = view["control"]
    assert control["outcome"] != "COMPLETED" and "not_started" not in control
    assert control["open_attempted"] is True and control["errors"]  # it was contacted, and it failed


def test_the_start_check_is_read_only_from_what_was_recorded():
    from stpg import constants as c
    from stpgview.runner.service import start_check_refusal

    def record(command, rsp, state, error=0, **extra):
        return {"command": command, "decoded": {"rsp": rsp, "state": state, "error_code": error}, **extra}

    info_ready = record(c.CMD_INFO, c.RSP_INFO, c.STATE_READY)
    assert start_check_refusal({"outcome": "REJECT", "transactions": [info_ready]})["state"] == "READY"
    assert start_check_refusal({"outcome": "COMPLETED", "transactions": [info_ready]}) is None
    idle = record(c.CMD_INFO, c.RSP_INFO, c.STATE_IDLE)
    assert start_check_refusal({"outcome": "REJECT", "transactions": [idle]}) is None  # passed: another reason
    upload = {"command": c.CMD_UPLOAD_BEGIN}
    assert start_check_refusal({"outcome": "REJECT", "transactions": [idle, upload]}) is None  # past the check
    identity = record(c.CMD_INFO, c.RSP_INFO, c.STATE_IDLE, read_error="RunnerError: response sequence/firmware/"
                      "capability mismatch")
    assert start_check_refusal({"outcome": "REJECT", "transactions": [identity]}) is None  # not a state refusal
    clock = record(c.CMD_GET_CLOCK_INFO, c.RSP_CLOCK_INFO, c.STATE_DONE)
    found = start_check_refusal({"outcome": "REJECT", "transactions": [idle, clock]})
    assert found["request"] == "GET_CLOCK_INFO" and found["allowed_states"] == ["IDLE"]


# ---- a binding whose firmware version or capability differs from what the STPG reports ---------------------------
def test_an_identity_difference_shows_both_values_and_the_binding_is_never_changed(svc, counter, monkeypatch):
    from stpg import constants as c

    expected = replace(SIMULATED_DEVICE, firmware_version=SIMULATED_DEVICE.firmware_version ^ 0x100,
                       capability=SIMULATED_DEVICE.capability | c.CAP_OVERCLOCK_200MHZ)
    if expected.capability == SIMULATED_DEVICE.capability:
        expected = replace(expected, capability=SIMULATED_DEVICE.capability & ~c.CAP_SAMPLE_CAPTURE)
    svc.store.save("devices", "dev", expected.to_dict(), replace=True)
    saved = (svc.store.root / "devices" / "dev.json").read_bytes()
    traffic = Traffic().install(monkeypatch)
    view = run(svc, prepare(svc, counter))["result"]
    assert view["run"]["outcome"] == "REJECT" and traffic.stpg.commands == [c.CMD_INFO]
    mismatch = view["identity_mismatch"]
    assert mismatch["request"] == "INFO" and mismatch["uploaded_before"] is False
    assert mismatch["fields"] == [
        {"name": "firmware_version", "expected": expected.firmware_version, "reported": SIMULATED_DEVICE.firmware_version},
        {"name": "capability", "expected": expected.capability, "reported": SIMULATED_DEVICE.capability}]
    assert mismatch["only_expected"] or mismatch["only_reported"]
    assert view["start_check"] is None  # an identity difference, not the state check
    assert (svc.store.root / "devices" / "dev.json").read_bytes() == saved  # nothing copied into the binding


def test_an_identity_failure_with_the_same_values_is_not_called_a_different_firmware():
    from stpg import constants as c
    from stpgview.runner.service import identity_mismatch

    device = {"firmware_version": 0x1234, "capability": 0x5}
    sequence_only = {"command": c.CMD_INFO, "read_failure_stage": "identity",
                     "decoded": {"firmware_version": 0x1234, "capability": 0x5, "seq": 9}}
    assert identity_mismatch({"device": device, "transactions": [sequence_only]}) is None
    assert identity_mismatch({"device": device, "transactions": [{"command": c.CMD_INFO, "decoded": {}}]}) is None
    other = {**sequence_only, "decoded": {"firmware_version": 0x1235, "capability": 0x5}}
    found = identity_mismatch({"device": device, "transactions": [other]})
    assert [f["expected"] == f["reported"] for f in found["fields"]] == [False, True]


# ---- RUN accepted, then the first STATUS gets no reply: show what was answered, never a completed run -------------
class _SilentAfterRun(SimulatedStpg):
    """Answers RUN, then no STATUS: the bounded host read ends without a byte."""

    def _handle(self, command):
        from stpg import constants as c

        if command.cmd == c.CMD_GET_STATUS and self.run_args:
            return  # nothing is queued
        super()._handle(command)


class _SilentTraffic(Traffic):
    def stpg_factory(self, _config):
        self.stpg_opened += 1
        self.stpg = _SilentAfterRun(on_run=lambda index: self.events.append(("run", index)))
        return self.stpg


def test_a_run_whose_first_status_gets_no_reply_says_what_was_answered(svc, counter, monkeypatch):
    from stpg import constants as c

    traffic = _SilentTraffic().install(monkeypatch)
    view = run(svc, prepare(svc, counter))["result"]
    assert view["run"]["outcome"] == "REJECT" and view["run"]["completed_segments"] == 0
    commands = traffic.stpg.commands
    assert commands[-3:] == [c.CMD_UPLOAD_END, c.CMD_RUN, c.CMD_GET_STATUS]
    assert commands.count(c.CMD_RUN) == 1 and commands[commands.index(c.CMD_RUN):].count(c.CMD_GET_STATUS) == 1  # no retry
    assert commands.count(c.CMD_RESET) == 1 and traffic.stpg.closed == 1
    assert not view["run"]["release_command_attempted"]
    stop = view["stopped_at"]
    assert (stop["upload_end"], stop["run"]) == ("OK · READY", "OK · RUNNING")
    assert (stop["failed_request"], stop["response_bytes"]) == ("GET_STATUS", 0)
    assert stop["failure_stage"] in ("deadline", "read_limit")  # as the SDK recorded it: no reply either way
    assert stop["segment"] == 0 and view["start_check"] is None and view["identity_mismatch"] is None


def _post_run_status_record(segment=0, stage="deadline", response_bytes=0):
    from stpg import constants as c

    def command(code, seq):
        return {"kind": "command", "command": code, "seq": seq, "written_bytes": c.STPG_CMD_SIZE}

    def ok(code, seq, state):
        return {**command(code, seq), "decoded": {"seq": seq, "rsp": c.RSP_OK, "state": state, "error_code": 0}}

    upload_state = c.STATE_READY if segment == 0 else c.STATE_HOLD_AWAITING_NEXT
    failed = {**command(c.CMD_GET_STATUS, 23), "read_error": "RunnerError: incomplete response",
              "read_failure_stage": stage, "raw_response_hex": "00" * response_bytes,
              "write_begin_s": 1.0, "read_end_s": 3.0}
    return {"outcome": "REJECT", "completed_segments": segment, "planned_segments": 2,
            "transactions": [ok(c.CMD_UPLOAD_END, 21, upload_state), ok(c.CMD_RUN, 22, c.STATE_RUNNING), failed]}


@pytest.mark.parametrize("segment", [0, 1])
@pytest.mark.parametrize("stage,byte_count", [("deadline", 0), ("deadline", 12), ("read_limit", 0)])
def test_the_stop_point_names_only_answered_steps(segment, stage, byte_count):
    from stpgview.runner.service import run_stop_point

    found = run_stop_point(_post_run_status_record(segment, stage, byte_count))
    assert found == {"segment": segment, "planned_segments": 2,
                     "upload_end": "OK · READY" if segment == 0 else "OK · HOLD_AWAITING_NEXT",
                     "run": "OK · RUNNING", "run_took_s": None, "failed_request": "GET_STATUS",
                     "response_bytes": byte_count, "failure_stage": stage, "failed_took_s": 2.0}


@pytest.mark.parametrize("case", [
    "completed", "cancelled", "wrong-upload-state", "run-done", "run-draining", "run-error", "run-timeout",
    "wrong-sequence", "short-write", "driver-read-error", "identity", "full-frame-after-deadline", "unknown-bytes",
    "malformed-bytes", "later-status", "next-upload", "release-status", "release-info",
])
def test_the_first_status_notice_is_not_used_for_other_boundaries(case):
    from stpg import constants as c
    from stpgview.runner.service import run_stop_point

    report = _post_run_status_record()
    records = report["transactions"]
    upload, run_reply, failed = records
    if case in ("completed", "cancelled"):
        report["outcome"] = case.upper()
    elif case == "wrong-upload-state":
        upload["decoded"]["state"] = c.STATE_HOLD_AWAITING_NEXT
    elif case in ("run-done", "run-draining", "run-error"):
        run_reply["decoded"]["state"] = {"run-done": c.STATE_DONE, "run-draining": c.STATE_RUN_DRAINING,
                                         "run-error": c.STATE_ERROR}[case]
    elif case == "run-timeout":
        records[:] = [upload, {**failed, "command": c.CMD_RUN, "seq": 22}]
    elif case == "wrong-sequence":
        run_reply["decoded"]["seq"] = 21
    elif case == "short-write":
        failed.update(written_bytes=1, write_error="RunnerError: short write")
    elif case == "driver-read-error":
        failed["read_failure_stage"] = "read"
    elif case == "identity":
        failed.update(read_failure_stage="identity", decoded={"rsp": c.RSP_STATUS, "state": c.STATE_DONE})
    elif case == "full-frame-after-deadline":
        failed["raw_response_hex"] = "00" * c.STPG_RSP_SIZE
    elif case in ("unknown-bytes", "malformed-bytes"):
        failed["raw_response_hex"] = None if case == "unknown-bytes" else " "
    else:
        assert case in ("later-status", "next-upload", "release-status", "release-info")
        state = c.STATE_RUNNING if case == "later-status" else c.STATE_DONE
        observed = {"kind": "command", "command": c.CMD_GET_STATUS, "seq": 23,
                    "written_bytes": c.STPG_CMD_SIZE,
                    "decoded": {"seq": 23, "rsp": c.RSP_STATUS, "state": state, "error_code": 0}}
        records.insert(-1, observed)
        failed["seq"] = 24
        if case == "next-upload":
            observed["decoded"]["state"] = c.STATE_HOLD_AWAITING_NEXT
            failed["command"] = c.CMD_UPLOAD_BEGIN
            report["completed_segments"] = 1
        elif case.startswith("release-"):
            records.insert(-1, {"kind": "command", "command": c.CMD_RESET, "seq": 24,
                                "decoded": {"rsp": c.RSP_OK, "state": c.STATE_IDLE}})
            failed["seq"] = 25
            failed["command"] = c.CMD_INFO if case == "release-info" else c.CMD_GET_STATUS
            report.update(completed_segments=2, release_command_attempted=True)
    assert run_stop_point(report) is None
