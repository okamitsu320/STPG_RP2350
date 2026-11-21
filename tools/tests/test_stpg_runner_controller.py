"""Optional GPIO and manual reset: packed ASCII models only, no device I/O."""
from dataclasses import replace
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from stpg import constants as c
from stpg_runner import CancellationToken
from stpg_runner import controller as module
from stpg_runner.controller import (ControllerConfig, ControllerSession, SUPPORTED_PINS,
                                    ensure_separate, manual_control)
from stpg_runner.controller_gate import check_controller_contract
from stpg_runner.device import DeviceLease
from stpg_runner.artifacts import RunnerError, read_json, write_json
from stpg_runner.accessories import run_with_accessories, accessory_exit
from test_stpg_runner_execution import DEVICE, Clock, FirmwarePeer, FIXTURES, ports
from stpg_runner import build_project, init_project

pytestmark = pytest.mark.stpg_host_only
CONFIG = ControllerConfig("MOCK-CONTROLLER", 0x2e8a, 10, "SYNTHETIC-CONTROLLER", "rp2040-gpio-v1", (0, 2, 14, 26))


class Peer:
    def __init__(self, *, clock=None, bad_at=0, fail_at=0, short_at=0, partial_at=0, prefix=0,
                 fragment=256, close_error=False, legacy=False, startup=False):
        self.clock = clock or Clock()
        self.bad_at, self.fail_at, self.short_at, self.partial_at, self.prefix = bad_at, fail_at, short_at, partial_at, prefix
        self.fragment, self.close_error, self.legacy, self.startup = fragment, close_error, legacy, startup
        self.rx = bytearray()
        self.writes, self.applied, self.closed = [], [], 0

    def write(self, raw):
        assert not self.rx, "new request before preceding response consumed"
        self.writes.append(bytes(raw))
        n = len(self.writes)
        if n == self.fail_at:
            raise OSError("synthetic unknown written-byte prefix")
        if n == self.short_at:
            return len(raw) - 1
        text = raw.decode("ascii").strip()
        if text.startswith("i "):
            token = text.split()[1]
            response = f"STPG_GPIO_V1 {token} GPIO=" + ",".join(map(str, SUPPORTED_PINS)) + "\n"
            if self.startup:
                response = "RP2040 Zero multi GPIO 3-state controller\r\nCommands:\r\n  0l : CH0 LOW\r\n" + response
        elif text == "s":
            assert self.legacy
            response = "Status:\n" + "".join(f"CH{x} GPIO{x} state=Hi-Z\n" for x in range(3))
        else:
            match = re.fullmatch(r"(@[0-9a-f]{16}:\d+ )?(\d+)([lzh])", text)
            assert match, text
            tag, pin, op = match.groups()
            state = {"l": "LOW", "z": "Hi-Z", "h": "HIGH"}[op]
            self.applied.append((int(pin), state))
            response = f"{tag or ''}CH{pin} = {state}\n"
        if n == self.bad_at:
            response = response.replace("CH", "BADCH") if not text.startswith("i ") else "WRONG IDENTITY\n"
        data = response.encode()
        self.rx.extend(data[:self.prefix] if n == self.partial_at else data)
        return len(raw)

    def read(self, size):
        self.clock.now += 0.1
        size = min(size, self.fragment)
        data = bytes(self.rx[:size]); del self.rx[:size]
        return data

    def close(self):
        self.closed += 1
        if self.close_error:
            raise OSError("synthetic controller close failure")


def controller_ports(config=CONFIG):
    return [SimpleNamespace(device=config.port, vid=config.vid, pid=config.pid, serial_number=config.serial_number)]


@pytest.fixture(autouse=True)
def no_physical_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("controller test attempted physical I/O")
    monkeypatch.setattr(module, "open_controller_serial", forbidden)
    monkeypatch.setattr(module, "enumerate_ports", forbidden)


def manual(tmp_path, peer=None, **options):
    peer = peer or Peer()
    defaults = dict(output_dir=tmp_path / "control", pin=14, state="HIGH", confirm_control=True,
                    stream_factory=lambda _: peer, port_provider=controller_ports, clock=peer.clock, sleep=peer.clock.sleep)
    defaults.update(options)
    result = manual_control(CONFIG, **defaults)
    return result, peer


def test_manual_set_and_normal_reset_are_distinct_and_once_closed(tmp_path):
    result, peer = manual(tmp_path)
    assert result["outcome"] == "COMPLETED" and peer.applied == [(14, "HIGH")]
    assert peer.closed == 1 and result["result_persisted"]
    assert read_json(tmp_path / "control" / "control.json") == result
    reset, peer = manual(tmp_path / "reset", pin=0, state=None, pulse_ms=100, settle_ms=500, target=DEVICE)
    assert reset["outcome"] == "COMPLETED" and peer.applied == [(0, "LOW"), (0, "Hi-Z")]
    assert peer.clock.sleeps == [0.1, 0.5] and peer.closed == 1
    assert reset["electrical_release_verified"] is False


def test_high_on_run_requires_both_allow_list_and_current_override(tmp_path):
    with pytest.raises(RunnerError, match="HIGH"):
        manual(tmp_path, pin=0, state="HIGH", target=DEVICE)
    result, peer = manual(tmp_path, pin=0, state="HIGH", target=DEVICE, allow_run_high=True)
    assert result["outcome"] == "COMPLETED" and peer.applied == [(0, "HIGH")]


def test_active_target_lease_blocks_reset_without_enumeration(tmp_path):
    peer = Peer()
    with DeviceLease(DEVICE):
        with pytest.raises(RunnerError, match="leased"):
            manual(tmp_path, peer, pin=0, state=None, pulse_ms=10, target=DEVICE)
    assert not peer.writes and peer.closed == 0 and not (tmp_path / "control").exists()


@pytest.mark.parametrize("options", [{"pin": 0, "state": None, "pulse_ms": 0, "target": DEVICE},
                                    {"pin": 0, "state": None, "pulse_ms": True, "target": DEVICE},
                                    {"pin": 1, "state": None, "pulse_ms": 10, "target": DEVICE},
                                    {"pin": 0, "state": "LOW"}, {"pin": 9, "state": "LOW"},
                                    {"pin": True, "state": "LOW"}, {"settle_ms": -1}, {"confirm_control": False}])
def test_invalid_manual_operations_fail_before_io(tmp_path, options):
    peer = Peer()
    with pytest.raises(RunnerError):
        manual(tmp_path, peer, **options)
    assert not peer.writes and peer.closed == 0


@pytest.mark.parametrize("kind", ["bad", "fail", "short", "close", "cancel"])
def test_failed_reset_never_adds_corrective_release(tmp_path, kind):
    peer = Peer(bad_at=2 if kind == "bad" else 0, fail_at=2 if kind == "fail" else 0,
                short_at=2 if kind == "short" else 0, close_error=kind == "close")
    token = CancellationToken()
    def sleep(seconds):
        peer.clock.sleep(seconds)
        if kind == "cancel":
            token.cancel()
    result, _ = manual(tmp_path, peer, pin=0, state=None, pulse_ms=10, target=DEVICE, cancel=token, sleep=sleep)
    assert result["outcome"] == ("CANCELLED" if kind == "cancel" else "REJECT")
    assert peer.closed == 1
    assert len(peer.writes) == (3 if kind == "close" else 2)
    if kind == "fail":
        record = next(r for r in result["transport"]["journal"] if "error" in r)
        assert record["written_bytes"] is None


@pytest.mark.parametrize("prefix", [0, 1, 8, 23])
def test_partial_ack_keeps_prefix_without_resend(tmp_path, prefix):
    peer = Peer(partial_at=2, prefix=prefix)
    result, _ = manual(tmp_path, peer)
    assert result["outcome"] == "REJECT" and peer.closed == 1 and len(peer.writes) == 2
    consumed = b"".join(bytes.fromhex(r.get("raw_hex", "")) for r in result["transport"]["journal"])
    assert consumed
    assert result["transport"]["pending_hex"] == (consumed[-prefix:].hex() if prefix else "")


def test_legacy_three_pin_protocol_has_no_new_command_or_high_fallback(tmp_path):
    config = replace(CONFIG, protocol="legacy-3", allow_high=(2,), legacy_pins=(0, 1, 2))
    peer = Peer(legacy=True)
    result = manual_control(config, output_dir=tmp_path / "legacy", pin=2, state="HIGH", confirm_control=True,
                            stream_factory=lambda _: peer, port_provider=lambda: controller_ports(config), clock=peer.clock)
    assert result["outcome"] == "COMPLETED" and peer.writes == [b"s\n", b"2h\n"]
    assert result["transport"]["legacy_uncorrelated"] is True


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("controlled-bundle")
    return build_project(init_project(FIXTURES / "pattern.stil", root / "project", wait_preset="none",
                                      completion="hold", max_segment_vectors=2))


def action_plan(bundle):
    return {"kind": "stpg_gpio_action_plan", "schema_version": 1, "manifest_sha256": bundle.manifest_sha256,
            "actions": [{"phase": "before_run", "segment": 0, "pin": 14, "state": "HIGH"},
                        {"phase": "after_terminal", "segment": 1, "pin": 14, "state": "LOW"}]}


def execute(bundle, tmp_path, *, controller=CONFIG, actions=None, gpio=None, peer=None, **options):
    gpio, peer = gpio or Peer(), peer or FirmwarePeer()
    result = run_with_accessories(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE,
                                  output_dir=tmp_path / "run", confirm_run=True, controller=controller,
                                  control_plan=actions if actions is not None else action_plan(bundle),
                                  stream_factory=lambda _: peer, port_provider=ports,
                                  controller_stream_factory=lambda _: gpio, controller_port_provider=controller_ports,
                                  clock=gpio.clock, sleep=gpio.clock.sleep, **options)
    return result, peer, gpio


def test_run_control_is_optional_and_does_not_touch_run_or_fpga_pins(bundle, tmp_path):
    result, peer, gpio = execute(bundle, tmp_path)
    assert result["run"]["outcome"] == result["control"]["outcome"] == "COMPLETED"
    assert gpio.applied == [(14, "HIGH"), (14, "LOW")]
    assert accessory_exit(result) == 0 and peer.close_count == gpio.closed == 1
    assert read_json(tmp_path / "run" / "control.json") == result["control"]


@pytest.mark.parametrize("pin", [0, 1, 9])
def test_run_pin_actions_reject_before_any_device_io(bundle, tmp_path, pin):
    plan = action_plan(bundle); plan["actions"][0]["pin"] = pin
    peer, gpio = FirmwarePeer(), Peer()
    with pytest.raises(RunnerError):
        execute(bundle, tmp_path, actions=plan, peer=peer, gpio=gpio)
    assert not peer.commands and not gpio.writes


def test_bad_controller_ack_seals_before_run_and_no_next_segment(bundle, tmp_path):
    result, peer, gpio = execute(bundle, tmp_path, gpio=Peer(bad_at=2))
    assert result["run"]["outcome"] == result["control"]["outcome"] == "REJECT"
    assert not any(cmd.cmd == c.CMD_RUN for cmd in peer.commands)
    assert peer.commands[-1].cmd == c.CMD_UPLOAD_END
    assert len(gpio.writes) == 2 and peer.close_count == gpio.closed == 1


def test_controller_close_failure_does_not_rewrite_completed_core(bundle, tmp_path):
    result, _, _ = execute(bundle, tmp_path, gpio=Peer(close_error=True))
    assert result["run"]["outcome"] == "COMPLETED" and result["control"]["outcome"] == "REJECT"
    assert accessory_exit(result) == 2


def test_endpoint_and_identity_aliases_reject():
    with pytest.raises(RunnerError):
        ensure_separate(replace(CONFIG, port=DEVICE.port), [DEVICE])
    with pytest.raises(RunnerError):
        ensure_separate(replace(CONFIG, vid=DEVICE.vid, pid=DEVICE.pid, serial_number=DEVICE.serial_number), [DEVICE])


@pytest.mark.parametrize("changes", [{"vid": True}, {"pid": 0}, {"serial_number": "bad\n"},
                                      {"allow_high": [9]}, {"legacy_pins": [0, 1, 2]},
                                      {"allow_high": [2, 2]}, {"extra": 1}])
def test_config_rejects_invalid_or_unknown(changes):
    with pytest.raises(RunnerError):
        ControllerConfig.from_dict({**CONFIG.to_dict(), **changes})


@pytest.mark.parametrize("before,after", [
    ("run_attached and pin in (0, 1)", "False"),
    ('session.set(pin, "Hi-Z")', 'session.set(pin, "HIGH")'),
    ('session.set(pin, "Hi-Z")', 'session.set(pin, "Hi-Z"); session.set(pin, "Hi-Z")'),
    ('self.clock() >= deadline', 'False'),
])
def test_controller_gate_rejects_unsafe_mutations(before, after):
    source = Path(module.__file__).read_text()
    assert before in source and check_controller_contract(source.replace(before, after))["status"] == "REJECT"


@pytest.mark.parametrize("before,after", [
    ("run_attached=True", "run_attached=False"),
    ('self.apply(context, "before_run")', 'self.apply(context, "after_terminal")'),
    ('self.session.set(action["pin"], action["state"])',
     'self.session.set(action["pin"], action["state"]); self.session.set(0, "LOW")'),
    ('self.session.close()  # no corrective', 'self.session.set(0, "Hi-Z"); self.session.close()  # no corrective'),
])
def test_controller_gate_rejects_accessory_mutations(before, after):
    import stpg_runner.accessories as accessories
    source = Path(accessories.__file__).read_text()
    assert before in source
    assert check_controller_contract(accessories_source=source.replace(before, after))["status"] == "REJECT"


def test_no_accessories_still_runs_without_importing_capture_or_opening_gpio(bundle, tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__
    def importing(name, *args, **kwargs):
        if name.startswith("stpg_capture"):
            pytest.fail("STPG-only invocation imported the analyzer")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", importing)
    peer, clock = FirmwarePeer(), Clock()
    result = run_with_accessories(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256,
                                  device=DEVICE, output_dir=tmp_path / "plain", confirm_run=True,
                                  stream_factory=lambda _: peer, port_provider=ports,
                                  clock=clock, sleep=clock.sleep)
    assert result["run"]["outcome"] == "COMPLETED" and accessory_exit(result) == 0
    assert result["control"] is result["capture"] is None
    assert not (tmp_path / "plain" / "control.json").exists()


def test_selected_missing_controller_rejects_without_run_or_fallback(bundle, tmp_path):
    peer, gpio, clock = FirmwarePeer(), Peer(), Clock()
    result = run_with_accessories(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256,
                                  device=DEVICE, output_dir=tmp_path / "missing", confirm_run=True,
                                  controller=CONFIG, control_plan=action_plan(bundle),
                                  stream_factory=lambda _: peer, port_provider=ports,
                                  controller_stream_factory=lambda _: gpio, controller_port_provider=lambda: [],
                                  clock=clock, sleep=clock.sleep)
    assert result["run"]["outcome"] == result["control"]["outcome"] == "REJECT"
    assert not gpio.writes and gpio.closed == 0 and peer.close_count == 1
    assert peer.commands[-1].cmd == c.CMD_UPLOAD_END
    assert not any(cmd.cmd == c.CMD_RUN for cmd in peer.commands)


def test_unreached_terminal_action_never_opens_controller(bundle, tmp_path):
    plan = action_plan(bundle); plan["actions"] = plan["actions"][1:]
    result, peer, gpio = execute(bundle, tmp_path, actions=plan, peer=FirmwarePeer(terminal_error=6))
    assert result["run"]["outcome"] == "DEVICE_ERROR" and result["control"]["outcome"] == "INCOMPLETE"
    assert not gpio.writes and not gpio.closed and accessory_exit(result) == 2
    assert sum(cmd.cmd == c.CMD_RUN for cmd in peer.commands) == 1


def test_terminal_action_failure_preserves_terminal_and_blocks_next_segment(bundle, tmp_path):
    plan = action_plan(bundle)
    plan["actions"] = [{"segment": 0, "phase": "after_terminal", "pin": 14, "state": "LOW"}]
    result, peer, gpio = execute(bundle, tmp_path, actions=plan, gpio=Peer(bad_at=2))
    raw = read_json(tmp_path / "run" / "run.json.gz")
    assert raw["segments"][0]["terminal"]["state"] == c.STATE_HOLD_AWAITING_NEXT
    assert raw["outcome"] == result["control"]["outcome"] == "REJECT"
    assert peer.commands[-1].cmd == c.CMD_GET_STATUS
    assert sum(cmd.cmd == c.CMD_RUN for cmd in peer.commands) == 1 and len(gpio.writes) == 2


def test_control_checkpoint_failure_before_action_blocks_gpio_and_run(bundle, tmp_path, monkeypatch):
    import stpg_runner.accessories as accessories
    def unavailable(*args, **kwargs):
        raise OSError("synthetic controller evidence storage failure")
    monkeypatch.setattr(accessories, "write_json", unavailable)
    result, peer, gpio = execute(bundle, tmp_path)
    assert result["run"]["outcome"] == result["control"]["outcome"] == "REJECT"
    assert not result["control"]["result_persisted"] and not gpio.writes and not gpio.closed
    assert peer.commands[-1].cmd == c.CMD_UPLOAD_END


def test_unowned_result_directory_is_never_overwritten(bundle, tmp_path, monkeypatch):
    import stpg_runner.accessories as accessories
    def raced(*args, output_dir, **kwargs):
        output_dir.mkdir()
        write_json(output_dir / "control.json", {"owner": "other invocation"})
        raise FileExistsError("reservation lost")
    monkeypatch.setattr(accessories, "run_bundle", raced)
    with pytest.raises(FileExistsError):
        execute(bundle, tmp_path)
    assert read_json(tmp_path / "run" / "control.json") == {"owner": "other invocation"}


def test_controller_foreign_tag_does_not_acknowledge_a_write(tmp_path):
    class Foreign(Peer):
        def write(self, raw):
            count = super().write(raw)
            if len(self.writes) == 2:
                self.rx[:] = self.rx.replace(raw.split(b":")[0], b"@0000000000000000")
            return count
    result, peer = manual(tmp_path, Foreign())
    assert result["outcome"] == "REJECT" and len(peer.writes) == 2 and peer.closed == 1
    assert not result["transport"]["last_acknowledged_states"]


def test_legacy_wrong_physical_map_rejects_before_set(tmp_path):
    config = replace(CONFIG, protocol="legacy-3", allow_high=(2,), legacy_pins=(2, 1, 0))
    peer = Peer(legacy=True)
    result = manual_control(config, output_dir=tmp_path / "legacy", pin=2, state="LOW", confirm_control=True,
                            stream_factory=lambda _: peer, port_provider=lambda: controller_ports(config), clock=peer.clock)
    assert result["outcome"] == "REJECT" and peer.writes == [b"s\n"] and peer.closed == 1


def test_buffered_controller_reply_after_deadline_is_rejected():
    clock = Clock()
    session = ControllerSession(CONFIG, clock=clock)
    session.buffer.extend(b"already complete\n")
    with pytest.raises(RunnerError, match="timeout"):
        session._line(clock.now)


def test_explicit_high_override_cannot_be_truthy_text(tmp_path):
    with pytest.raises(RunnerError, match="booleans"):
        manual(tmp_path, pin=0, state="HIGH", target=DEVICE, allow_run_high="yes")


def test_duplicate_controller_binding_fields_reject_before_contact(tmp_path):
    path = tmp_path / "controller.json"
    path.write_text(json.dumps(CONFIG.to_dict())[:-1] + ', "port": "OTHER"}')
    with pytest.raises(RunnerError, match="duplicate"):
        module.load_controller_config(path)


def test_cli_manual_reset_and_escaped_io_error_report(tmp_path, monkeypatch, capsys):
    from stpg_runner import cli
    config_path, target_path = tmp_path / "controller.json", tmp_path / "target.json"
    write_json(config_path, CONFIG.to_dict()); write_json(target_path, DEVICE.to_dict())
    peer = Peer()
    original = module.manual_control
    def injected(*args, **kwargs):
        return original(*args, **kwargs, stream_factory=lambda _: peer, port_provider=controller_ports,
                        clock=peer.clock, sleep=peer.clock.sleep)
    monkeypatch.setattr(module, "manual_control", injected)
    args = ["control", "reset", "--controller-config", str(config_path), "--target-device-config", str(target_path),
            "--target", "stpg", "--pulse-ms", "15", "--output-dir", str(tmp_path / "reset"), "--confirm-control"]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "COMPLETED"
    assert peer.applied == [(0, "LOW"), (0, "Hi-Z")]
    def escaped(*args, **kwargs):
        raise OSError("post-open outer failure")
    monkeypatch.setattr(module, "manual_control", escaped)
    assert cli.main(args) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["device_io_performed"] is None and error["device_io_possible"] is True
