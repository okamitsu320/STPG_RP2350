"""External-accessory negative paths that really execute.

Exercises actual run_bundle calls, with valid independently packed frames and
independent outcome/command-prefix assertions (never SDK judge as oracle).
The byte-peer's firmware contract is shared with the existing SDK tests; this
is not an independent firmware implementation or any physical evidence.
"""
from dataclasses import replace
import struct
from types import SimpleNamespace
import zlib

import pytest

from stpg import constants as c
from stpg_runner import build_project, init_project
from stpg_runner import device as device_module, execution
from stpg_runner.artifacts import RunnerError
from stpg_runner.device import DeviceLease
from test_stpg_runner_execution import DEVICE, FIXTURES, FirmwarePeer, execute

pytestmark = pytest.mark.stpg_host_only


class IndependentFramePeer(FirmwarePeer):
    def _queue(self, response):
        self.response_count += 1
        if self.mutate:
            response = self.mutate(self.response_count, response)
        self.last = response
        body = struct.pack("<IHH13I", c.STPG_CMD_MAGIC, c.STPG_PROTO_VERSION, response.rsp,
                           response.seq, response.state, response.capability, response.allowed_cmds,
                           response.active_pins, response.pattern_size, response.event_count,
                           response.run_progress, response.result_flags, response.fail_count,
                           response.sample_count, response.error_code, response.firmware_version)
        assert len(body) == 60
        self.rx.extend(body + struct.pack("<I", zlib.crc32(body)))


@pytest.fixture(autouse=True)
def no_physical_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("external negative attempted real device I/O")
    monkeypatch.setattr(execution, "open_serial", forbidden)
    monkeypatch.setattr(execution, "enumerate_ports", forbidden)


@pytest.fixture(scope="module")
def negative_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("external-negatives")
    return build_project(init_project(FIXTURES / "pattern.stil", root / "project",
                                      wait_preset="none", completion="hold"))


def _assert_incomplete_rejected(bundle, tmp_path):
    def mutate(index, response):
        if response.rsp == c.RSP_STATUS and response.state == c.STATE_DONE:
            return replace(response, run_progress=4)  # nonzero, below the six planned events
        return response
    report, peer = execute(bundle, tmp_path, IndependentFramePeer(mutate=mutate))
    assert report["outcome"] == "REJECT"
    assert peer.commands[-1].cmd == c.CMD_GET_STATUS
    assert sum(cmd.cmd == c.CMD_RUN for cmd in peer.commands) == 1
    assert peer.response_count == len(report["transactions"]) == 11
    assert report["segments"] == [] and peer.close_count == 1
    assert not report["release_command_attempted"]


def test_valid_frames_positive_control(negative_bundle, tmp_path):
    report, peer = execute(negative_bundle, tmp_path, IndependentFramePeer())
    assert report["outcome"] == "COMPLETED", report["errors"]
    assert peer.response_count == 11 and report["segments"][0]["terminal"]["run_progress"] == 6
    assert report["segments"][0]["terminal"]["sample_count"] == 2


def test_incomplete_done_is_rejected_by_actual_executor(negative_bundle, tmp_path):
    _assert_incomplete_rejected(negative_bundle, tmp_path)


def test_negative_detects_disabled_terminal_validation(negative_bundle, tmp_path, monkeypatch):
    original = execution.judge_status
    monkeypatch.setattr(execution, "judge_status", lambda response, expected, *, terminal_state:
                        "TERMINAL" if response.state == terminal_state else
                        original(response, expected, terminal_state=terminal_state))
    with pytest.raises(AssertionError):
        _assert_incomplete_rejected(negative_bundle, tmp_path)


def _assert_identity_rejected(bundle, tmp_path, field):
    def mutate(index, response):
        return replace(response, **{field: getattr(DEVICE, field) ^ 1})
    report, peer = execute(bundle, tmp_path, IndependentFramePeer(mutate=mutate))
    assert report["outcome"] == "REJECT"
    assert [cmd.cmd for cmd in peer.commands] == [c.CMD_INFO]
    assert len(report["transactions"]) == peer.response_count == peer.close_count == 1
    assert len(bytes.fromhex(report["transactions"][0]["raw_response_hex"])) == 64
    assert report["transactions"][0]["decoded"][field] != getattr(DEVICE, field)
    assert not report["release_command_attempted"]


@pytest.mark.parametrize("field", ["firmware_version", "capability"])
def test_identity_mismatch_at_actual_info_seals(negative_bundle, tmp_path, field):
    _assert_identity_rejected(negative_bundle, tmp_path, field)


def test_negative_detects_missing_identity_guard(negative_bundle, tmp_path, monkeypatch):
    original = execution._Wire._read

    def weakened(wire, record, seq, timeout):
        saved = wire.config
        wire.config = replace(saved, firmware_version=saved.firmware_version ^ 1)
        try:
            return original(wire, record, seq, timeout)
        finally:
            wire.config = saved

    monkeypatch.setattr(execution._Wire, "_read", weakened)
    with pytest.raises(AssertionError):
        _assert_identity_rejected(negative_bundle, tmp_path, "firmware_version")


def _assert_cross_role_blocked():
    fields = dict(vid=DEVICE.vid, pid=DEVICE.pid, serial_number="EXTERNAL-SYNTHETIC-LEASE")
    stpg = SimpleNamespace(**fields, lease_key="stpg-role")
    analyzer = SimpleNamespace(**fields, lease_key="analyzer-role")
    for first, second in ((stpg, analyzer), (analyzer, stpg)):
        with DeviceLease(first):
            with pytest.raises(RunnerError, match="leased|locked"):
                with DeviceLease(second):
                    pass


def test_lease_blocks_real_role_key_substitution():
    _assert_cross_role_blocked()


def test_negative_detects_restored_role_namespace_bug(monkeypatch):
    monkeypatch.setattr(device_module, "physical_identity_key", lambda config: config.lease_key)
    with pytest.raises(pytest.fail.Exception):
        _assert_cross_role_blocked()
