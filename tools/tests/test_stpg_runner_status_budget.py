"""Public delayed-byte models; no device I/O or physical timing proof.

The default-policy startup/draining cases can also run against the preceding
SDK unchanged. They must fail there by a real two-second deadline rejection,
not by an import/setup error. No timeout override is used in those two cases.
"""

from dataclasses import replace
import math
from pathlib import Path

import pytest

from stpg import constants as c
from stpg_runner import CancellationToken
import stpg_runner.execution as execution
from stpg_runner.execution_gate import check_execution_contract
from test_stpg_runner_execution import Clock, FirmwarePeer, bundle, execute

pytestmark = pytest.mark.stpg_host_only


class DelayedPeer(FirmwarePeer):
    """Empty 50ms slices before a selected response; no wall-clock sleeps."""

    def __init__(self, clock, boundary, seconds, **kwargs):
        super().__init__(**kwargs)
        self.clock, self.boundary = clock, boundary
        self.empty_reads = math.ceil(seconds / 0.05)

    def read(self, size):
        if self.response_count == self.boundary and self.empty_reads:
            self.empty_reads -= 1
            self.clock.now += 0.05
            return b""
        if not self.rx:
            self.clock.now += 0.05
        return super().read(size)


def draining_status(index, response):
    if index == 12:
        return replace(response, state=c.STATE_RUN_DRAINING)
    if index == 13:
        return replace(response, state=c.STATE_DONE)
    return response


@pytest.mark.parametrize("phase,boundary", [("startup", 11), ("draining", 12)])
def test_post_run_delayed_status_completes_on_default_policy(bundle, tmp_path, phase, boundary):
    clock = Clock()
    peer = DelayedPeer(clock, boundary, 4.2, nonterminal=1,
                       mutate=draining_status if phase == "draining" else None)
    report, peer = execute(bundle, tmp_path, peer, clock=clock)
    last = report["transactions"][-1]
    assert report["outcome"] == "COMPLETED", {
        "outcome": report["outcome"], "errors": report["errors"],
        "last_command": last.get("command"), "read_failure_stage": last.get("read_failure_stage"),
        "raw_response_bytes": len(bytes.fromhex(last["raw_response_hex"])),
    }
    delayed = report["transactions"][boundary - 1]
    assert 4.19 < delayed["read_end_s"] - delayed["read_begin_s"] < 4.26
    assert delayed["decoded"]["state"] == (c.STATE_RUNNING if phase == "startup" else c.STATE_RUN_DRAINING)
    assert report["completed_segments"] == 1 and report["release_sequence_complete"]
    assert peer.close_count == report["close_attempts"] == report["close_successes"] == 1


@pytest.mark.parametrize("phase,boundary", [("startup", 11), ("draining", 12)])
def test_two_second_counterfactual_still_rejects(bundle, tmp_path, monkeypatch, phase, boundary):
    # In addition to the actual-old-source check, isolate the receive-policy
    # mechanism here. This model alone is not an old executable comparison.
    monkeypatch.setattr(execution, "STATUS_RESPONSE_TIMEOUT_S", 2.0)
    clock = Clock()
    peer = DelayedPeer(clock, boundary, 4.2, nonterminal=1,
                       mutate=draining_status if phase == "draining" else None)
    report, peer = execute(bundle, tmp_path, peer, clock=clock)
    last = report["transactions"][-1]
    assert report["outcome"] == "REJECT" and last["read_failure_stage"] == "deadline"
    assert last["raw_response_hex"] == ""
    assert peer.commands[-1].cmd == c.CMD_GET_STATUS and not report["release_command_attempted"]
    assert peer.close_count == 1


def test_only_terminal_observation_status_uses_twenty_seconds(bundle, tmp_path):
    report, _ = execute(bundle, tmp_path)
    assert report["outcome"] == "COMPLETED"
    assert report["plan"]["observation"]["response_timeout_s"] == 20.0
    seen_run = False
    observation_count = 0
    for record in report["transactions"]:
        command = record.get("command")
        expected = 20.0 if command in (c.CMD_UPLOAD_END, c.CMD_RUN) else 2.0
        if command == c.CMD_GET_STATUS and seen_run:
            expected = 20.0
            observation_count += 1
        assert math.isclose(record["response_deadline_s"] - record["read_begin_s"], expected)
        if command == c.CMD_RUN:
            seen_run = True
        elif command == c.CMD_RESET:
            seen_run = False
    assert observation_count == 1


@pytest.mark.parametrize("boundary", [1, 2, 3, 4, 5, 6, 7, 8, 12, 13, 14])
def test_ordinary_response_deadline_is_not_extended(bundle, tmp_path, boundary):
    clock = Clock()
    report, peer = execute(bundle, tmp_path, DelayedPeer(clock, boundary, 4.2), clock=clock)
    last = report["transactions"][-1]
    assert report["outcome"] == "REJECT" and last["read_failure_stage"] == "deadline"
    assert math.isclose(last["response_deadline_s"] - last["read_begin_s"], 2.0)
    assert peer.response_count == boundary and peer.close_count == 1


@pytest.mark.parametrize("boundary", [9, 10])
def test_upload_end_and_run_keep_their_admission_budget(bundle, tmp_path, boundary):
    clock = Clock()
    report, _ = execute(bundle, tmp_path, DelayedPeer(clock, boundary, 4.2), clock=clock)
    assert report["outcome"] == "COMPLETED"


@pytest.mark.parametrize("prefix", [0, 1, 31, 63])
def test_silence_or_partial_status_still_seals_at_twenty_seconds(bundle, tmp_path, prefix):
    clock = Clock()
    peer = DelayedPeer(clock, 0, 0, partial_at=11, prefix=prefix)
    report, peer = execute(bundle, tmp_path, peer, clock=clock)
    last = report["transactions"][-1]
    assert report["outcome"] == "REJECT" and last["read_failure_stage"] == "deadline"
    assert 20.0 <= last["read_end_s"] - last["read_begin_s"] < 20.051
    assert len(bytes.fromhex(last["raw_response_hex"])) == prefix
    assert sum(command.cmd == c.CMD_RUN for command in peer.commands) == 1
    assert peer.commands[-1].cmd == c.CMD_GET_STATUS and not report["release_command_attempted"]
    assert peer.close_count == 1


def test_delayed_valid_device_error_is_not_a_success_or_release(bundle, tmp_path):
    clock = Clock()
    report, peer = execute(bundle, tmp_path, DelayedPeer(clock, 11, 4.2, terminal_error=9), clock=clock)
    assert report["outcome"] == "DEVICE_ERROR" and report["comparison"] == "UNOBSERVED"
    assert report["segments"][0]["terminal"]["error_code"] == 9
    assert not report["release_command_attempted"] and peer.commands[-1].cmd == c.CMD_GET_STATUS
    assert peer.close_count == 1


@pytest.mark.parametrize("field,value", [("seq", 999), ("event_count", 999),
                                         ("firmware_version", 0), ("capability", 0)])
def test_delayed_invalid_frame_is_not_adopted(bundle, tmp_path, field, value):
    clock = Clock()
    peer = DelayedPeer(clock, 11, 4.2, mutate=lambda i, r: replace(r, **{field: value}) if i == 11 else r)
    report, peer = execute(bundle, tmp_path, peer, clock=clock)
    assert report["outcome"] == "REJECT" and not report["release_command_attempted"]
    assert len(bytes.fromhex(report["transactions"][-1]["raw_response_hex"])) == 64
    assert peer.commands[-1].cmd == c.CMD_GET_STATUS and peer.close_count == 1


def test_delayed_bad_crc_is_not_adopted(bundle, tmp_path):
    clock = Clock()
    report, peer = execute(bundle, tmp_path, DelayedPeer(clock, 11, 4.2, corrupt_at=11), clock=clock)
    assert report["outcome"] == "REJECT" and "bad response crc" in report["errors"][-1]
    assert not report["release_command_attempted"] and peer.close_count == 1


def test_complete_response_after_twenty_seconds_is_retained_not_decoded(bundle, tmp_path):
    clock = Clock()

    def delay(boundary):
        if boundary == 11:
            clock.now += 20.1

    report, peer = execute(bundle, tmp_path, FirmwarePeer(read_delay=delay), clock=clock)
    last = report["transactions"][-1]
    assert report["outcome"] == "REJECT" and last["read_failure_stage"] == "deadline"
    assert len(bytes.fromhex(last["raw_response_hex"])) == 64 and "decoded" not in last
    assert not report["release_command_attempted"] and peer.close_count == 1


def test_cancel_during_long_observation_sends_no_later_command(bundle, tmp_path):
    clock, token = Clock(), CancellationToken()

    class CancelPeer(DelayedPeer):
        def read(self, size):
            part = super().read(size)
            if self.response_count == 11 and self.clock.now >= 3.0:
                token.cancel()
            return part

    report, peer = execute(bundle, tmp_path, CancelPeer(clock, 11, 4.2), clock=clock, cancel=token)
    assert report["outcome"] == "CANCELLED" and report["transactions"][-1]["read_failure_stage"] == "cancel"
    assert not report["release_command_attempted"] and peer.commands[-1].cmd == c.CMD_GET_STATUS
    assert peer.close_count == 1


def test_read_admitted_before_global_deadline_may_finish_after_it(bundle, tmp_path):
    clock = Clock()

    def delay(boundary):
        if boundary >= 11:
            clock.now += 19.5

    report, peer = execute(bundle, tmp_path, FirmwarePeer(nonterminal=1000, read_delay=delay), clock=clock)
    assert report["outcome"] == "REJECT" and "terminal observation deadline" in report["errors"][-1]
    observed = report["transactions"][10:]
    assert len(observed) == 4 and observed[-1]["read_end_s"] > 60.0
    assert all("decoded" in record for record in observed)
    assert not report["release_command_attempted"] and peer.close_count == 1


@pytest.mark.parametrize("before,after", [
    ("STATUS_RESPONSE_TIMEOUT_S = 20.0", "STATUS_RESPONSE_TIMEOUT_S = 2.0"),
    ("STATUS_RESPONSE_TIMEOUT_S = 20.0", "STATUS_RESPONSE_TIMEOUT_S = 20.1"),
    ("RESPONSE_TIMEOUT_S = 2.0", "RESPONSE_TIMEOUT_S = 20.0"),
    ("timeout=STATUS_RESPONSE_TIMEOUT_S", "timeout=RESPONSE_TIMEOUT_S"),
    ("c.CMD_GET_STATUS, timeout=STATUS_RESPONSE_TIMEOUT_S", "c.CMD_GET_STATUS"),
    ("initial = self.wire.exchange(c.CMD_GET_STATUS)",
     "initial = self.wire.exchange(c.CMD_GET_STATUS, timeout=STATUS_RESPONSE_TIMEOUT_S)"),
    ("response = self.wire.exchange(c.CMD_GET_STATUS)\n        self._expect(response, c.RSP_STATUS, (c.STATE_IDLE,)",
     "response = self.wire.exchange(c.CMD_GET_STATUS, timeout=STATUS_RESPONSE_TIMEOUT_S)\n        self._expect(response, c.RSP_STATUS, (c.STATE_IDLE,)"),
    ("timeout: float = RESPONSE_TIMEOUT_S", "timeout: float = STATUS_RESPONSE_TIMEOUT_S"),
    ("return self._read(record, seq, RESPONSE_TIMEOUT_S)", "return self._read(record, seq, STATUS_RESPONSE_TIMEOUT_S)"),
])
def test_gate_rejects_budget_or_scope_mutations(before, after):
    source = Path(execution.__file__).read_text(encoding="utf-8")
    assert before in source
    assert check_execution_contract(execution_source=source.replace(before, after))["status"] == "REJECT"
