"""Public byte-level peers and actual pinned pyserial code; no native device calls."""
from __future__ import annotations

import ast
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
import serial
from serial.serialutil import SerialBase

from stpg import constants as c
from stpg.protocol import StpgStatusResponse
from stpg_runner import CancellationToken
from stpg_runner.artifacts import RunnerError
from stpg_runner.execution import _Wire, RunCancelled
from stpg_runner.execution_gate import check_execution_contract
from test_stpg_runner_execution import Clock, DEVICE, FirmwarePeer, bundle, execute

pytestmark = pytest.mark.stpg_host_only


class ReadPeer:
    """Empty reads take a modeled 50 ms; any setting write is a test failure."""
    def __init__(self, clock, *, empty=0, prefix=64, fragment=64, fault=False, delay=0.0):
        self.clock, self.empty, self.fragment = clock, empty, fragment
        self.fault, self.delay = fault, delay
        self.rx = bytearray(StpgStatusResponse(c.RSP_OK, 1, c.STATE_IDLE, DEVICE.capability, 0, 0,
                                             firmware_version=DEVICE.firmware_version).pack()[:prefix])
        self.writes, self.reads = [], 0

    @property
    def timeout(self):
        return 0.05

    @timeout.setter
    def timeout(self, value):
        raise AssertionError("setting written after open")

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def read(self, size):
        self.reads += 1
        if self.empty:
            self.empty -= 1
            self.clock.now += 0.05
            return b""
        if not self.rx:
            if self.fault:
                raise OSError("synthetic ReadFile failure")
            self.clock.now += 0.05
            return b""
        self.clock.now += self.delay
        count = min(size, self.fragment)
        part = bytes(self.rx[:count])
        del self.rx[:count]
        return part


def exchange(peer, *, timeout=2.0, check_cancel=lambda: None):
    journal = []
    wire = _Wire(peer, DEVICE, journal, check_cancel, peer.clock)
    return wire, journal


@pytest.mark.parametrize("empty,fragment,budget", [(0, 64, 2.0), (1, 1, 2.0), (39, 7, 2.0), (60, 3, 20.0)])
def test_fixed_configuration_accepts_empty_slices_then_fragmented_response(empty, fragment, budget):
    peer = ReadPeer(Clock(), empty=empty, fragment=fragment)
    wire, journal = exchange(peer)
    assert wire.exchange(c.CMD_INFO, timeout=budget).seq == 1
    assert len(peer.writes) == 1 and peer.reads == empty + math.ceil(64 / fragment)
    assert len(bytes.fromhex(journal[0]["raw_response_hex"])) == 64
    assert sum(part["size"] == 0 for part in journal[0]["read_parts"]) == empty


@pytest.mark.parametrize("prefix", range(64))
def test_each_partial_prefix_is_preserved_to_deadline_without_resend(prefix):
    peer = ReadPeer(Clock(), prefix=prefix, fragment=7)
    wire, journal = exchange(peer)
    with pytest.raises(RunnerError, match="deadline"):
        wire.exchange(c.CMD_INFO)
    assert len(peer.writes) == 1 and 2.0 <= peer.clock.now < 2.051
    assert len(bytes.fromhex(journal[0]["raw_response_hex"])) == prefix
    assert journal[0]["read_failure_stage"] == "deadline"


@pytest.mark.parametrize("prefix", [0, 1, 63])
def test_driver_error_journals_the_failed_read_call_and_partial_bytes(prefix):
    peer = ReadPeer(Clock(), prefix=prefix, fault=True)
    wire, journal = exchange(peer)
    with pytest.raises(OSError, match="ReadFile"):
        wire.exchange(c.CMD_INFO)
    record = journal[0]
    assert record["read_failure_stage"] == "read" and "ReadFile" in record["read_error"]
    failed = record["read_parts"][-1]
    assert failed["end_s"] >= failed["begin_s"] and failed["size"] is None and "ReadFile" in failed["error"]
    assert len(bytes.fromhex(record["raw_response_hex"])) == prefix and len(peer.writes) == 1


def test_late_complete_response_is_retained_but_not_accepted():
    peer = ReadPeer(Clock(), delay=2.01)
    wire, journal = exchange(peer)
    with pytest.raises(RunnerError, match="deadline"):
        wire.exchange(c.CMD_INFO)
    assert len(bytes.fromhex(journal[0]["raw_response_hex"])) == 64
    assert "decoded" not in journal[0] and len(peer.writes) == 1


def test_admission_silence_keeps_its_twenty_second_budget_not_a_slice_budget():
    peer = ReadPeer(Clock(), prefix=0)
    wire, journal = exchange(peer)
    with pytest.raises(RunnerError, match="deadline"):
        wire.exchange(c.CMD_RUN, timeout=20.0)
    assert 20.0 <= peer.clock.now < 20.051 and len(peer.writes) == 1
    assert journal[0]["response_deadline_s"] == 20.0


@pytest.mark.parametrize("fault,stage", [("crc", "decode"), ("identity", "identity")])
def test_decoding_and_identity_errors_keep_distinct_failure_stages(fault, stage):
    peer = ReadPeer(Clock())
    if fault == "crc":
        peer.rx[-1] ^= 1
    else:
        peer.rx = bytearray(StpgStatusResponse(c.RSP_OK, 2, c.STATE_IDLE, DEVICE.capability, 0, 0,
                                             firmware_version=DEVICE.firmware_version).pack())
    wire, journal = exchange(peer)
    with pytest.raises(ValueError):
        wire.exchange(c.CMD_INFO)
    assert journal[0]["read_failure_stage"] == stage
    assert len(bytes.fromhex(journal[0]["raw_response_hex"])) == 64 and len(peer.writes) == 1


def test_empty_stream_with_nonadvancing_clock_has_a_finite_attempt_bound():
    peer = ReadPeer(Clock(), prefix=0)
    peer.read = lambda size: b""
    wire, journal = exchange(peer)
    with pytest.raises(RunnerError, match="read limit"):
        wire.exchange(c.CMD_INFO)
    assert len(journal[0]["read_parts"]) == math.ceil(2.0 / 0.05) + 64
    assert journal[0]["read_failure_stage"] == "read_limit" and len(peer.writes) == 1


def test_cancel_between_empty_slices_does_not_read_or_write_again():
    token, clock = CancellationToken(), Clock()
    peer = ReadPeer(clock, prefix=0)
    original = peer.read

    def read(size):
        result = original(size)
        token.cancel()
        return result

    peer.read = read

    def check():
        if token.cancelled:
            raise RunCancelled("synthetic cancellation")

    wire, journal = exchange(peer, check_cancel=check)
    with pytest.raises(RunCancelled):
        wire.exchange(c.CMD_INFO)
    assert peer.reads == 1 and len(peer.writes) == 1
    assert journal[0]["read_failure_stage"] == "cancel"


def test_payload_ack_stall_seals_real_executor_without_run_or_release(bundle, tmp_path):
    clock = Clock()

    class Stalled(FirmwarePeer):
        def read(self, size):
            if self.payloads:
                clock.now += 0.05
                return b""
            return super().read(size)

        @property
        def timeout(self):
            return 0.05

        @timeout.setter
        def timeout(self, value):
            raise AssertionError("setting written after open")

    report, peer = execute(bundle, tmp_path, Stalled(), clock=clock)
    assert report["outcome"] == "REJECT" and peer.close_count == 1
    assert peer.commands[-1].cmd == c.CMD_UPLOAD_DATA and len(peer.payloads) == 1
    assert not report["release_command_attempted"]
    assert report["transactions"][-1]["read_failure_stage"] == "deadline"
    assert report["transactions"][-1]["written_bytes"] == len(peer.payloads[0])


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_production_opener_settings_are_explicit_and_only_set_while_closed(monkeypatch, platform):
    import stpg_runner.device as device
    trace = []

    class SerialStub:
        def __init__(self, **kwargs):
            trace.append(("construct", kwargs))
            self.opened = False

        def __setattr__(self, key, value):
            if key in {"port", "rts", "dtr", "timeout", "write_timeout"}:
                assert not self.opened
                trace.append((key, value))
            object.__setattr__(self, key, value)

        def open(self):
            trace.append(("open",))
            self.opened = True

    monkeypatch.setattr(device, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(serial, "Serial", SerialStub)
    assert device.open_serial(DEVICE).opened
    assert trace[0] == ("construct", dict(port=None, baudrate=115200, timeout=0.05, write_timeout=2.0,
                                         dsrdtr=platform != "win32", rtscts=False, xonxoff=False))
    assert [entry[0] for entry in trace] == ["construct", "rts", "port", "open"]


def test_actual_pyserial_windows_configuration_disables_dsr_flow(monkeypatch):
    """Execute the installed 3.5 backend function with every Windows API stubbed."""
    import stpg_runner.device as device
    assert serial.VERSION == "3.5", "review a different dependency before changing this host model"
    path = Path(serial.__file__).with_name("serialwin32.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Serial")
    function = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_reconfigure_port")
    calls, states = [], []
    win32 = SimpleNamespace(COMMTIMEOUTS=SimpleNamespace, DCB=SimpleNamespace, MAXDWORD=0xffffffff,
        EV_ERR=128, NOPARITY=0, ODDPARITY=1, EVENPARITY=2, MARKPARITY=3, SPACEPARITY=4,
        ONESTOPBIT=0, ONE5STOPBITS=1, TWOSTOPBITS=2, RTS_CONTROL_HANDSHAKE=2,
        RTS_CONTROL_ENABLE=1, RTS_CONTROL_DISABLE=0, DTR_CONTROL_HANDSHAKE=2,
        DTR_CONTROL_ENABLE=1, DTR_CONTROL_DISABLE=0)
    for name in ("SetCommTimeouts", "SetCommMask", "GetCommState"):
        setattr(win32, name, lambda *args, _name=name: calls.append(_name) or True)
    win32.SetCommState = lambda handle, state: calls.append("SetCommState") or states.append(vars(state)) or True
    scope = {"serial": serial, "win32": win32, "ctypes": SimpleNamespace(byref=lambda value: value),
             "SerialException": serial.SerialException}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), scope)

    class WindowsModel(SerialBase):
        def open(self):
            self._port_handle = "HOST MODEL ONLY"
            scope["_reconfigure_port"](self)
            self.is_open = True

    monkeypatch.setattr(device, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(serial, "Serial", WindowsModel)
    stream = device.open_serial(DEVICE)
    assert stream.is_open and calls == ["SetCommTimeouts", "SetCommMask", "GetCommState", "SetCommState"]
    assert len(states) == 1 and states[0]["fDtrControl"] == win32.DTR_CONTROL_ENABLE
    assert states[0]["fRtsControl"] == win32.RTS_CONTROL_ENABLE
    assert not any(states[0][key] for key in ("fOutxDsrFlow", "fOutxCtsFlow", "fOutX", "fInX"))


@pytest.mark.parametrize("before,after", [
    ("range(max_reads)", "range(99999)"),
    ("math.ceil(timeout / SERIAL_READ_TIMEOUT_S) + c.STPG_RSP_SIZE", "c.STPG_RSP_SIZE"),
    ("self.clock() >= deadline", "self.clock() > deadline"),
    ("call[\"end_s\"] > deadline", "False"),
    ('stage = "read"', 'stage = "read"; self.stream.timeout = 1.0'),
    ("                self.check_cancel()", "                pass"),
])
def test_gate_rejects_serial_read_mutations(before, after):
    import stpg_runner.execution as execution
    source = Path(execution.__file__).read_text(encoding="utf-8")
    assert before in source
    assert check_execution_contract(execution_source=source.replace(before, after))["status"] == "REJECT"
