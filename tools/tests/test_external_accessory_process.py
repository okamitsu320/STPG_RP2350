"""Public independent child process + injected STPG models. No hardware contact."""
from dataclasses import replace
import ast
import builtins
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from stpg import constants as c
from stpg_runner import build_project, init_project, CancellationToken
from stpg_runner.accessories import accessory_exit, run_with_accessories
from stpg_runner.artifacts import RunnerError, write_json, read_json
from stpg_runner.external_accessory import (ExternalAccessorySpec, load_external_accessory,
                                           prepare_external_accessory, run_with_external_accessory)
from stpg_runner.external_binding import ExternalDeviceBinding
from stpg_runner import external_accessory as external_module
from stpg_runner.cli import main
from test_stpg_runner_execution import DEVICE, Clock, FirmwarePeer, FIXTURES, ports

pytestmark = pytest.mark.stpg_host_only
PEER = Path(__file__).parent / "fixtures/external_accessory/peer.py"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("process-bundle")
    return build_project(init_project(FIXTURES / "pattern.stil", root / "project", wait_preset="none",
                                      completion="hold", max_segment_vectors=2))


def config(tmp_path, bundle, scenario="normal", *, segment=0, timeout=2):
    tmp_path.mkdir(parents=True, exist_ok=True)
    binding, plan = tmp_path / "binding.json", tmp_path / "plan.json"
    write_json(binding, {"kind": "stpg_external_device", "schema_version": 1, "port": "SYNTHETIC-EXTERNAL",
                         "vid": 0x2e8a, "pid": 10, "serial_number": "SYNTHETIC-EXTERNAL"})
    write_json(plan, {"kind": "public_peer_plan", "schema_version": 1})
    path = tmp_path / "external.json"
    raw = {"kind": "stpg_external_accessory", "schema_version": 1, "identifier": "public-peer",
           "command": [sys.executable, str(PEER.resolve()), scenario],
           "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
           "binding_file": "binding.json", "binding_sha256": hashlib.sha256(binding.read_bytes()).hexdigest(),
           "plan_file": "plan.json", "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
           "segment": segment, "mode": "simulated", "timeout_s": timeout}
    write_json(path, raw)
    return load_external_accessory(path), raw


def execute(tmp_path, bundle, scenario="normal", **options):
    spec, _ = config(tmp_path / "config", bundle, scenario,
                     segment=options.pop("segment", 0), timeout=options.pop("timeout", 2))
    peer = options.pop("peer", FirmwarePeer())
    clock = Clock()
    result = run_with_external_accessory(
        bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE,
        spec=spec, output_dir=tmp_path / "result", confirm_run=True,
        stream_factory=lambda _: peer, port_provider=ports, clock=clock, sleep=clock.sleep, **options)
    return result, peer


def request_methods(result):
    return [r["method"] for r in result["external"]["requests"]]


def test_positive_control_uses_one_child_three_requests_and_separate_evidence(tmp_path, bundle):
    result, peer = execute(tmp_path, bundle)
    assert result["run"]["outcome"] == result["external"]["outcome"] == "COMPLETED"
    assert result["external"]["adopted"] and accessory_exit(result) == 0
    assert request_methods(result) == ["open", "before_run", "after_terminal"]
    assert result["external"]["spawn_attempts"] == result["external"]["stdin_close_attempts"] == 1
    assert result["external"]["terminate_attempts"] == result["external"]["kill_attempts"] == 0
    assert result["external"]["mode"] == "simulated"
    assert result["external"]["firmware_validation_pass"] is False
    assert result["external"]["waveform_validation_pass"] is False
    assert read_json(tmp_path / "result/accessory/session.json") == result["external"]
    records = [json.loads(line) for line in (tmp_path / "result/accessory/peer-requests.jsonl").read_text().splitlines()]
    assert [r["method"] for r in records] == request_methods(result)
    assert peer.close_count == 1
    assert sum(command.cmd == c.CMD_RUN for command in peer.commands) == result["run"]["planned_segments"]


@pytest.mark.parametrize("phase", ["open", "arm"])
@pytest.mark.parametrize("bad", ["status", "nonce", "seq", "version", "method", "unknown", "eof",
                                 "duplicate", "nonfinite", "invalidutf8", "excess", "oversize"])
def test_bad_peer_seals_before_run_no_later_request(tmp_path, bundle, phase, bad):
    result, peer = execute(tmp_path, bundle, phase + "-" + bad)
    assert accessory_exit(result) == 2
    assert not result["external"]["adopted"]
    assert not any(command.cmd == c.CMD_RUN for command in peer.commands)
    assert len(request_methods(result)) == (1 if phase == "open" else 2)
    assert result["external"]["spawn_attempts"] == 1
    if phase == "open":
        assert result["run"] is None and not peer.commands


@pytest.mark.parametrize("bad", ["partial", "timeout", "stderr"])
def test_bounded_read_deadline_and_error_output_keep_prefix_without_resend(tmp_path, bundle, bad):
    # The real child must start and answer open inside the window; its arm reply never completes.
    result, peer = execute(tmp_path, bundle, "arm-" + bad, timeout=1.0 if bad != "stderr" else 2)
    assert accessory_exit(result) == 2 and request_methods(result) == ["open", "before_run"]
    runs = sum(command.cmd == c.CMD_RUN for command in peer.commands)
    # stdout ACK and stderr have no cross-pipe ordering. A later-observed log
    # overflow seals the next boundary; it cannot unsend an earlier valid RUN.
    assert (runs <= 1) if bad == "stderr" else (runs == 0)
    if bad == "partial":
        assert bytes.fromhex(result["external"]["pending_stdout_hex"]) == b'{"protocol":'
    if bad == "stderr":
        assert 0 < len(result["external"]["stderr_hex"]) <= 2 * 65536


@pytest.mark.parametrize("bad", ["escape", "collision", "hash", "missing", "wrongmode"])
def test_result_is_confined_hashed_and_strict_before_later_segments(tmp_path, bundle, bad):
    result, peer = execute(tmp_path, bundle, "result-" + bad)
    assert accessory_exit(result) == 2 and not result["external"]["adopted"]
    assert request_methods(result) == ["open", "before_run", "after_terminal"]
    assert sum(command.cmd == c.CMD_RUN for command in peer.commands) == 1
    assert result["run"]["outcome"] == "REJECT"


@pytest.mark.parametrize("scenario", ["late-output", "nonzero-exit", "ignore-eof"])
def test_child_cleanup_failure_never_rewrites_core_success_to_pass(tmp_path, bundle, scenario):
    result, _ = execute(tmp_path, bundle, scenario)
    assert result["run"]["outcome"] == "COMPLETED"
    assert result["external"]["outcome"] == "REJECT" and accessory_exit(result) == 2
    assert request_methods(result) == ["open", "before_run", "after_terminal"]


@pytest.mark.parametrize("late", [False, True])
def test_exit_does_not_outrun_final_output_reader(tmp_path, bundle, late):
    class DelayedReader:
        def __init__(self, inner):
            self.inner = inner

        def read(self, count):
            raw = self.inner.read(count)
            if b"UNSOLICITED-LATE" in raw:
                time.sleep(0.08)  # process has exited; its reader is not done
            return raw

        def close(self):
            self.inner.close()

    def spawn(argv, **kwargs):
        process = subprocess.Popen(argv, **kwargs)
        process.stdout = DelayedReader(process.stdout)
        return process

    result, _ = execute(tmp_path, bundle, "late-output" if late else "normal", process_factory=spawn)
    assert result["external"]["output_drain_complete"]
    assert result["external"]["pipe_eof_seen"] == ["stderr", "stdout"]
    assert accessory_exit(result) == (2 if late else 0)
    if late:
        assert b"UNSOLICITED-LATE" in bytes.fromhex(result["external"]["stdout_hex"])


def test_not_adopted_is_its_own_exit_axis(tmp_path, bundle):
    result, _ = execute(tmp_path, bundle, "not-adopted")
    assert result["run"]["outcome"] == result["external"]["outcome"] == "COMPLETED"
    assert accessory_exit(result) == 4


def test_noninitial_target_is_armed_at_only_its_own_boundary(tmp_path, bundle):
    result, _ = execute(tmp_path, bundle, segment=1)
    assert accessory_exit(result) == 0
    records = [json.loads(line) for line in (tmp_path / "result/accessory/peer-requests.jsonl").read_text().splitlines()]
    assert records[1]["segment"] == records[2]["segment"] == 1


@pytest.mark.parametrize("field,value", [("segment", True), ("segment", -1), ("mode", "auto"),
                                        ("timeout_s", True), ("timeout_s", float("inf")),
                                        ("timeout_s", 10**500), ("command", ["python", "peer.py"]),
                                        ("executable_sha256", "A"*64), ("identifier", "bad/name")])
def test_config_rejects_before_launch(tmp_path, bundle, field, value):
    _, raw = config(tmp_path, bundle)
    raw[field] = value
    with pytest.raises(RunnerError):
        ExternalAccessorySpec.from_dict(raw, base=tmp_path)


@pytest.mark.parametrize("changed", ["executable", "binding", "plan", "same-identity", "same-port"])
def test_preparation_mismatches_do_not_launch_or_create_output(tmp_path, bundle, changed):
    spec, _ = config(tmp_path / "config", bundle)
    if changed == "executable":
        spec = replace(spec, executable_sha256="0"*64)
    elif changed in ("binding", "plan"):
        getattr(spec, changed + "_file").write_text("{}")
    else:
        data = read_json(spec.binding_file)
        if changed == "same-identity":
            data.update(vid=DEVICE.vid, pid=DEVICE.pid, serial_number=DEVICE.serial_number)
        else:
            data["port"] = DEVICE.port
        write_json(spec.binding_file, data)
        spec = replace(spec, binding_sha256=hashlib.sha256(spec.binding_file.read_bytes()).hexdigest())
    with pytest.raises(RunnerError):
        prepare_external_accessory(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE, spec=spec)
    assert not (tmp_path / "result").exists()


def test_legacy_selection_rejects_before_any_metadata_process_or_core_request(tmp_path, bundle, capsys):
    assert main(["run", str(bundle.bundle_dir), "--manifest-sha256", bundle.manifest_sha256,
                 "--device-config", str(tmp_path / "not-needed"), "--output-dir", str(tmp_path / "never"),
                 "--analyzer-config", str(tmp_path / "not-needed-either"), "--confirm-run"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["device_io_performed"] is False and "separate" in report["errors"][0]
    with pytest.raises(RunnerError, match="separate external"):
        run_with_accessories(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE,
                             output_dir=tmp_path / "never", confirm_run=True, analyzer=object())
    assert not (tmp_path / "never").exists()


def test_sdk_core_has_no_la_import_edges_and_import_does_not_launch():
    root = Path(__file__).parents[1] / "stpg_runner"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert all(not a.name.startswith(("stpg_capture", "stpg_la")) for a in node.names), path.name
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("stpg_capture", "stpg_la")), path.name
    # Reloading the module in this pytest process replaces its dataclass
    # identities while earlier importers still hold the old public classes.
    # A fresh interpreter tests actual import safety without order pollution.
    previous = external_module.ExternalAccessorySpec
    script = '''
import sys, subprocess, serial
from serial.tools import list_ports
sys.path.insert(0, sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError("import attempted device enumeration/open or child launch")
subprocess.Popen = serial.Serial = list_ports.comports = forbidden
import stpg_runner.external_accessory
assert not any(name.split('.')[0] in ('stpg_capture', 'stpg_la') for name in sys.modules)
print("import-safe")
'''
    checked = subprocess.run([sys.executable, "-I", "-c", script, str(root.parent)],
                             capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0 and checked.stdout.strip() == "import-safe", checked.stderr
    assert external_module.ExternalAccessorySpec is previous


def test_usb_target_compatibility_does_not_load_analyzer_driver():
    target = ExternalDeviceBinding.from_dict({"kind": "stpg_capture_analyzer", "schema_version": 1,
                                              "port": "SYNTHETIC", "vid": 1, "pid": 2,
                                              "serial_number": "PUBLIC", "opaque_driver_setting": 123})
    assert target.to_dict()["kind"] == "stpg_capture_analyzer"
    assert "opaque_driver_setting" not in target.to_dict()


def test_without_selection_never_launches_a_process(tmp_path, bundle, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("unselected process was launched")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    peer, clock = FirmwarePeer(), Clock()
    result = run_with_accessories(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256,
                                  device=DEVICE, output_dir=tmp_path / "plain", confirm_run=True,
                                  stream_factory=lambda _: peer, port_provider=ports,
                                  clock=clock, sleep=clock.sleep)
    assert result["run"]["outcome"] == "COMPLETED" and accessory_exit(result) == 0
    assert result["capture"] is None and result.get("external") is None


def test_pre_cancelled_or_unconfirmed_never_launches_or_creates_output(tmp_path, bundle):
    spec, _ = config(tmp_path / "config", bundle)
    token = CancellationToken()
    token.cancel()
    from stpg_runner import BuildCancelled
    for options, error in (({"confirm_run": False}, RunnerError),
                           ({"confirm_run": True, "cancel": token}, BuildCancelled)):
        with pytest.raises(error):
            run_with_external_accessory(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256,
                                         device=DEVICE, spec=spec, output_dir=tmp_path / "never", **options)
    assert not (tmp_path / "never").exists()


def test_simulated_mode_cannot_fall_back_to_real_core_metadata_or_io(tmp_path, bundle):
    spec, _ = config(tmp_path / "config", bundle)
    with pytest.raises(RunnerError, match="injected"):
        run_with_external_accessory(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256,
                                     device=DEVICE, spec=spec, output_dir=tmp_path / "never", confirm_run=True)
    assert not (tmp_path / "never").exists()


class InputProxy:
    def __init__(self, inner, observe=None, *, fault=None):
        self.inner, self.observe, self.fault = inner, observe, fault
        self.close_count = 0

    def write(self, raw):
        value = json.loads(raw)
        if self.observe is not None:
            self.observe(value)
        if value["method"] == "before_run" and self.fault in {"short", "exception"}:
            count = self.inner.write(raw[:-1])
            self.inner.flush()
            if self.fault == "exception":
                raise OSError("synthetic write with unknown delivered prefix")
            return count
        return self.inner.write(raw)

    def flush(self):
        self.inner.flush()

    def close(self):
        self.close_count += 1
        if self.fault == "close":
            raise OSError("synthetic stdin close failure")
        self.inner.close()


def process_factory(*, observe=None, fault=None, seen=None):
    def spawn(argv, **kwargs):
        assert kwargs["shell"] is False
        process = subprocess.Popen(argv, **kwargs)
        process.stdin = InputProxy(process.stdin, observe, fault=fault)
        if seen is not None:
            seen.append(process)
        return process
    return spawn


@pytest.mark.parametrize("fault", ["short", "exception"])
def test_partial_child_write_is_not_retried_or_admitted_as_arm(tmp_path, bundle, fault):
    result, peer = execute(tmp_path, bundle, process_factory=process_factory(fault=fault))
    assert accessory_exit(result) == 2 and request_methods(result) == ["open", "before_run"]
    assert not any(command.cmd == c.CMD_RUN for command in peer.commands)
    record = result["external"]["requests"][1]
    assert "short write" in record["error"] if fault == "short" else record["written_bytes"] is None


def test_stdin_close_failure_still_stops_child_and_keeps_core_axis(tmp_path, bundle):
    seen = []
    result, _ = execute(tmp_path, bundle, process_factory=process_factory(fault="close", seen=seen))
    assert result["run"]["outcome"] == "COMPLETED" and accessory_exit(result) == 2
    assert result["external"]["outcome"] == "REJECT"
    assert seen[0].poll() is not None and seen[0].stdin.close_count == 1
    seen[0].stdin.inner.close()  # release this synthetic test proxy's retained fd


def test_cancellation_seals_child_before_run_and_preserves_cancel_priority(tmp_path, bundle):
    token = CancellationToken()
    timers = []
    def observe(request):
        if request["method"] == "before_run":
            timer = threading.Timer(0.05, token.cancel)
            timers.append(timer)
            timer.start()
    result, peer = execute(tmp_path, bundle, "arm-partial", cancel=token,
                           process_factory=process_factory(observe=observe))
    for timer in timers:
        timer.join()
    assert result["run"]["outcome"] == "CANCELLED" and accessory_exit(result) == 130
    assert result["external"]["cancel_requested"] is True
    assert request_methods(result) == ["open", "before_run"]
    assert not any(command.cmd == c.CMD_RUN for command in peer.commands)


def test_arm_intent_checkpoint_failure_blocks_pipe_write_and_run(tmp_path, bundle, monkeypatch):
    original = external_module.write_json
    armed_failure = []
    def persist(path, value):
        if (not armed_failure and value.get("requests")
                and value["requests"][-1]["method"] == "before_run"):
            armed_failure.append(True)
            raise OSError("synthetic journal failure before arm write")
        return original(path, value)
    monkeypatch.setattr(external_module, "write_json", persist)
    result, peer = execute(tmp_path, bundle)
    observed = (tmp_path / "result/accessory/peer-requests.jsonl").read_text().splitlines()
    assert [json.loads(line)["method"] for line in observed] == ["open"]
    assert not any(command.cmd == c.CMD_RUN for command in peer.commands)
    assert result["external"]["outcome"] == "REJECT" and accessory_exit(result) == 2


def test_controller_before_action_precedes_child_arm_and_run(tmp_path, bundle):
    from test_stpg_runner_controller import CONFIG, Peer, action_plan, controller_ports
    gpio, core = Peer(), FirmwarePeer()
    order = []
    def observe(request):
        if request["method"] == "before_run":
            assert gpio.applied == [(14, "HIGH")]
            assert not any(command.cmd == c.CMD_RUN for command in core.commands)
            order.append("gpio-then-arm-before-run")
    result, _ = execute(tmp_path, bundle, peer=core, controller=CONFIG, control_plan=action_plan(bundle),
                         controller_stream_factory=lambda _: gpio, controller_port_provider=controller_ports,
                         process_factory=process_factory(observe=observe))
    assert accessory_exit(result) == 0 and order == ["gpio-then-arm-before-run"]
    assert gpio.applied == [(14, "HIGH"), (14, "LOW")]
    assert gpio.closed == core.close_count == 1


def test_binding_mutation_between_prepare_and_spawn_is_refused_before_child(tmp_path, bundle):
    spec, _ = config(tmp_path / "config", bundle)
    def changed_metadata():
        spec.binding_file.write_text("{}")
        return ports()
    result = run_with_external_accessory(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256,
                                         device=DEVICE, spec=spec, output_dir=tmp_path / "result", confirm_run=True,
                                         stream_factory=lambda _: pytest.fail("core was opened"),
                                         port_provider=changed_metadata)
    assert result["run"] is None and result["external"]["spawn_attempts"] == 0
    assert result["external"]["outcome"] == "REJECT" and accessory_exit(result) == 2


def test_new_cli_option_routes_to_process_api_without_analyzer_import(tmp_path, bundle, monkeypatch, capsys):
    spec, _ = config(tmp_path / "config", bundle)
    device_path = tmp_path / "device.json"
    write_json(device_path, DEVICE.to_dict())
    actual = external_module.run_with_external_accessory
    core = FirmwarePeer()
    clock = Clock()
    def modeled(*args, **kwargs):
        return actual(*args, **kwargs, stream_factory=lambda _: core, port_provider=ports,
                       clock=clock, sleep=clock.sleep)
    monkeypatch.setattr(external_module, "run_with_external_accessory", modeled)
    assert main(["run", str(bundle.bundle_dir), "--manifest-sha256", bundle.manifest_sha256,
                 "--device-config", str(device_path), "--output-dir", str(tmp_path / "result"),
                 "--external-accessory", str(tmp_path / "config/external.json"), "--confirm-run", "--progress", "none"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["run"]["outcome"] == result["external"]["outcome"] == "COMPLETED"
    assert result["capture"] is None and result["external"]["mode"] == "simulated"
