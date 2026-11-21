"""External-accessory regressions: public stdlib child + actual injected SDK paths."""
import json
import threading
import time

import pytest

from stpg import constants as c
from stpg_runner import CancellationToken, DeviceLease
from stpg_runner import cli, controller
from stpg_runner.artifacts import RunnerError, write_json
from stpg_runner.external_accessory import ExternalAccessorySpec, run_with_external_accessory
from stpg_runner.external_binding import ExternalDeviceBinding
from test_external_accessory_process import bundle, config, execute, process_factory, request_methods
from test_stpg_runner_controller import CONFIG, Peer, controller_ports
from test_stpg_runner_execution import DEVICE, Clock, FirmwarePeer, ports

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    from stpg_runner import device, execution, external_accessory
    import serial
    from serial.tools import list_ports
    def forbidden(*a, **kw):
        pytest.fail("round2 test attempted physical I/O")
    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(list_ports, "comports", forbidden)
    for module in (device, execution, external_accessory, controller):
        monkeypatch.setattr(module, "enumerate_ports", forbidden)
    monkeypatch.setattr(controller, "open_controller_serial", forbidden)


def run_spec(tmp_path, bundle, scenario, timeout=1.0, **limits):
    # A real child and real time: 1 s leaves a busy test process room to take each prompt reply in time.
    spec, raw = config(tmp_path / "config", bundle, scenario, timeout=timeout)
    raw.update(limits)
    spec = ExternalAccessorySpec.from_dict(raw, base=spec.binding_file.parent)
    peer, clock = FirmwarePeer(), Clock()
    result = run_with_external_accessory(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256,
                                         device=DEVICE, spec=spec, output_dir=tmp_path / "result", confirm_run=True,
                                         stream_factory=lambda _: peer, port_provider=ports,
                                         clock=clock, sleep=clock.sleep)
    return result, peer


def test_old_config_keeps_its_original_limits(tmp_path, bundle):
    spec, _ = config(tmp_path, bundle, timeout=2)
    assert spec.timeout_s == 2 and spec.open_timeout_s is None and spec.close_timeout_s == 1
    assert ExternalAccessorySpec.from_dict(spec.to_dict()) == spec


@pytest.mark.parametrize("field,value", [
    ("open_timeout_s", True), ("open_timeout_s", 0), ("open_timeout_s", 3601),
    ("open_timeout_s", float("nan")), ("open_timeout_s", float("inf")),
    ("open_timeout_s", 10**500), ("open_timeout_s", "60"),
    ("close_timeout_s", False), ("close_timeout_s", 0), ("close_timeout_s", 21),
    ("close_timeout_s", float("nan")), ("close_timeout_s", 10**500), ("close_timeout_s", None),
])
def test_optional_limits_are_strict_before_process(tmp_path, bundle, field, value):
    _, raw = config(tmp_path, bundle)
    raw[field] = value
    with pytest.raises(RunnerError):
        ExternalAccessorySpec.from_dict(raw, base=tmp_path)


@pytest.mark.parametrize("extended", [False, True])
def test_initial_validation_gets_only_its_explicit_allowance(tmp_path, bundle, extended):
    # The child answers open after 1.5 s: past the 1 s reply window, inside an explicit 3 s allowance.
    result, peer = run_spec(tmp_path, bundle, "slow-open:1.5", **({"open_timeout_s": 3} if extended else {}))
    assert result["external"]["outcome"] == ("COMPLETED" if extended else "REJECT"), result["external"]["errors"]
    assert bool(peer.commands) == extended
    if extended:
        assert result["external"]["requests"][0]["timeout_s"] == 3
        assert result["external"]["requests"][1]["timeout_s"] == 1.0


@pytest.mark.parametrize("phase,runs", [("arm", 0), ("terminal", 1)])
def test_open_allowance_does_not_extend_run_boundary_waits(tmp_path, bundle, phase, runs):
    # The slow reply (1.5 s) misses the 1 s window; it would be taken if the 3 s open allowance leaked into it.
    result, peer = run_spec(tmp_path, bundle, "slow-" + phase + ":1.5", open_timeout_s=3)
    assert result["external"]["outcome"] == "REJECT"
    assert sum(x.cmd == c.CMD_RUN for x in peer.commands) == runs, result["external"]["errors"]
    assert result["external"]["requests"][-1]["timeout_s"] == 1.0


@pytest.mark.parametrize("grace,complete", [(0.1, False), (3.0, True)])
def test_eof_grace_is_explicit_and_does_not_send_cleanup_requests(tmp_path, bundle, grace, complete):
    result, _ = run_spec(tmp_path, bundle, "cleanup-slow", close_timeout_s=grace, open_timeout_s=2)
    assert result["external"]["outcome"] == ("COMPLETED" if complete else "REJECT")
    assert result["external"]["terminate_attempts"] == int(not complete)
    assert request_methods(result) == ["open", "before_run", "after_terminal"]
    assert result["external"]["stdin_close_attempts"] == 1


@pytest.mark.parametrize("close_error", [False, True])
def test_queued_response_after_cancellation_is_retained_but_never_admitted(tmp_path, bundle, close_error):
    token, timers = CancellationToken(), []
    children = []
    def observe(request):
        if request["method"] == "before_run":
            timer = threading.Timer(0.1, token.cancel)
            timers.append(timer)
            timer.start()
    result, peer = execute(tmp_path, bundle, "arm-cancel-reply", cancel=token,
                           process_factory=process_factory(observe=observe, fault="close" if close_error else None,
                                                           seen=children))
    for timer in timers:
        timer.join()
    if close_error:
        children[0].stdin.inner.close()  # release only the synthetic fault proxy's retained fd
    assert result["external"]["outcome"] == ("REJECT" if close_error else "CANCELLED")
    assert result["external"]["cancel_requested"] is True
    assert result["external"]["unadmitted_stdout_bytes"] > 0
    assert b'"ARMED"' in bytes.fromhex(result["external"]["stdout_hex"])
    assert "response" not in result["external"]["requests"][-1]
    assert request_methods(result) == ["open", "before_run"]
    assert not any(x.cmd == c.CMD_RUN for x in peer.commands)
    from stpg_runner import accessory_exit
    assert accessory_exit(result) == 130


@pytest.mark.parametrize("kind", [[], {}, None, False])
def test_invalid_external_kind_is_a_rejection_not_an_uncaught_type_error(kind):
    with pytest.raises(RunnerError):
        ExternalDeviceBinding.from_dict(neutral(kind))


@pytest.mark.parametrize("old,new", [
    ("target = target_binding(target.to_dict())", "target = target"),
    ("if target.to_dict().get('kind') not in target_kinds", "if False"),
])
def test_role_validation_cannot_be_removed_from_controller_gate(old, new):
    from pathlib import Path
    from stpg_runner.controller_gate import check_controller_contract
    import ast
    source = ast.unparse(ast.parse(Path(controller.__file__).read_text()))
    assert old in source and check_controller_contract(source=source)["status"] == "PASS"
    assert check_controller_contract(source=source.replace(old, new))["status"] == "REJECT"


def test_one_open_failure_has_one_error_summary_and_full_request_evidence(tmp_path, bundle):
    result, peer = execute(tmp_path, bundle, "open-nonce")
    errors = result["external"]["errors"]
    assert len(errors) == len(set(errors)) == 1, errors
    assert request_methods(result) == ["open"] and not peer.commands
    assert result["external"]["requests"][0]["error"] == errors[0]


@pytest.mark.parametrize("ending", ["output", "fail", "exit"])
def test_completed_child_is_still_checked_before_later_runs(tmp_path, bundle, monkeypatch, ending):
    from stpg_runner import external_accessory as module
    original = module._ProcessSession._before
    def pause(context_self, context):
        if context_self.completed:
            time.sleep(0.15)  # let the real child's post-COMPLETE action become observable
        return original(context_self, context)
    monkeypatch.setattr(module._ProcessSession, "_before", pause)
    result, peer = execute(tmp_path, bundle, "post-complete-" + ending)
    assert request_methods(result) == ["open", "before_run", "after_terminal"]
    if ending == "exit":
        assert result["run"]["outcome"] == result["external"]["outcome"] == "COMPLETED"
    else:
        assert result["run"]["outcome"] == result["external"]["outcome"] == "REJECT"
        assert sum(x.cmd == c.CMD_RUN for x in peer.commands) == 1


def neutral(kind="stpg_external_device"):
    return {"kind": kind, "schema_version": 1, "port": "SYNTHETIC-LA",
            "vid": 0x2e8a, "pid": 10, "serial_number": "SYNTHETIC-LA"}


@pytest.mark.parametrize("kind", ["stpg_external_device", "stpg_capture_analyzer"])
def test_real_cli_reset_accepts_neutral_and_legacy_targets_without_opening_them(tmp_path, monkeypatch, capsys, kind):
    control, target = tmp_path / "controller.json", tmp_path / "target.json"
    write_json(control, CONFIG.to_dict())
    write_json(target, neutral(kind))
    peer, original = Peer(), controller.manual_control
    def injected(*args, **kwargs):
        return original(*args, **kwargs, stream_factory=lambda _: peer, port_provider=controller_ports,
                        clock=peer.clock, sleep=peer.clock.sleep)
    monkeypatch.setattr(controller, "manual_control", injected)
    assert cli.main(["control", "reset", "--controller-config", str(control), "--target-device-config", str(target),
                     "--target", "la", "--pulse-ms", "10", "--output-dir", str(tmp_path / "manual"),
                     "--confirm-control"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "COMPLETED"
    assert peer.applied == [(1, "LOW"), (1, "Hi-Z")] and peer.closed == 1


def test_neutral_target_still_cannot_authorize_stpg_pin0(tmp_path):
    peer = Peer()
    with pytest.raises(RunnerError, match="binding kind"):
        controller.manual_control(CONFIG, output_dir=tmp_path / "manual", pin=0, pulse_ms=10,
                                  target=ExternalDeviceBinding.from_dict(neutral()), confirm_control=True,
                                  stream_factory=lambda _: peer, port_provider=controller_ports)
    assert not peer.writes and not (tmp_path / "manual").exists()


def test_held_neutral_target_lease_blocks_controller_open(tmp_path):
    peer, target = Peer(), ExternalDeviceBinding.from_dict(neutral())
    with DeviceLease(target), pytest.raises(RunnerError, match="leased"):
        controller.manual_control(CONFIG, output_dir=tmp_path / "manual", pin=1, pulse_ms=10,
                                  target=target, confirm_control=True, stream_factory=lambda _: peer,
                                  port_provider=controller_ports)
    assert not peer.writes and not (tmp_path / "manual").exists()
