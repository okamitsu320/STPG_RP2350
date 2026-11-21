"""Simulated STPG, analyzer and GPIO controller for ``stpg-view --demo-device`` (host only, clearly labelled).

The STPG model answers the runner SDK's packed commands the way the firmware's
command_dispatch/pattern_buffer contracts describe (upload, HOLD between
segments, DONE counts derived from the uploaded bytes). RUN arguments are
checked like ``stpg_runner.c`` does: only the supported flag bits; without
PRESERVE_GPIO_STATE both entry arguments must be 0 (raw mode: and no HOLD may
be pending); with it the entry value/enable must equal the terminal levels the
model decoded itself from the previously uploaded bytes (``stil.hold_state``),
not from the runner's plan (raw mode: and a HOLD must be pending). A mismatch
answers RSP_ERROR (ERR_HOLD_STATE_MISMATCH / ERR_UNSUPPORTED / ERR_BAD_STATE).

It is a protocol stand-in for trying the GUI and for host tests, not a
firmware, timing or DUT model: it never reports a compare failure unless told
to, and it does not model storage targets, timing or electrical behaviour.
Runs through it are recorded by the SDK with ``transport_kind = "injected"``.

``SimulatedController`` answers the text protocol of the GPIO controller sketch
(``rp2040-gpio-v1``: tagged session and sequence; ``legacy-3``: the old three-channel
replies) so the GUI's controller path can be tried without a board. Its ACKs are
the same software states the real sketch reports: nothing about voltage.
"""

from __future__ import annotations

from dataclasses import replace
import re
import struct
import threading
from types import SimpleNamespace
from typing import Callable

from stil.device_precheck import inspect_pattern_requirements
from stil.hold_state import derive_terminal_hold
from stpg import constants as c
from stpg.crc32 import crc32
from stpg.protocol import StpgCommand, StpgStatusResponse
from stpg_runner import ControllerConfig, DeviceConfig
from stpg_runner.controller import SUPPORTED_PINS

SIMULATED_DEVICE = DeviceConfig("SIMULATED-STPG", 0x2E8A, 0x000A, "SIMULATED-STPG-0001", 0x40010204, 0x001F7BFF)
SIMULATED_TICK_HZ = 200_000_000
RUNNING_POLLS = 2  # STATUS replies that still report RUNNING before DONE/HOLD


SIMULATED_CONTROLLER = ControllerConfig("SIMULATED-GPIO", 0x2E8A, 0x000A, "SIMULATED-GPIO-0001", "rp2040-gpio-v1",
                                        allow_high=(3, 4, 5, 6, 7, 8, 14, 15, 26))


def controller_ports(config: ControllerConfig = SIMULATED_CONTROLLER) -> list[SimpleNamespace]:
    return [SimpleNamespace(device=config.port, vid=config.vid, pid=config.pid, serial_number=config.serial_number)]


def simulated_ports(*extra: SimpleNamespace) -> list[SimpleNamespace]:
    return [SimpleNamespace(device=SIMULATED_DEVICE.port, vid=SIMULATED_DEVICE.vid, pid=SIMULATED_DEVICE.pid,
                            serial_number=SIMULATED_DEVICE.serial_number), *extra]


class SimulatedStpg:
    """Byte stream with ``write``/``read``/``close``/``timeout`` like a pyserial port."""

    timeout = 2.0

    def __init__(self, *, fails: int = 0, on_run: Callable[[int], None] | None = None) -> None:
        self.fails = fails
        self.on_run = on_run
        self.rx = bytearray()
        self.pending: StpgCommand | None = None
        self.buffer = bytearray()
        self.holding = False
        self.hold = (0, 0)
        self.requirements = None
        self.run_flags = 0
        self.runs = 0
        self.polls = 0
        self.total_size = self.total_crc = 0
        self.closed = 0
        self.commands: list[int] = []
        self.run_args: list[tuple[int, int, int, int]] = []
        self.rejected_runs: list[int] = []
        self._lock = threading.Lock()
        self.last = StpgStatusResponse(c.RSP_STATUS, 0, c.STATE_IDLE, SIMULATED_DEVICE.capability, 0, 0,
                                       firmware_version=SIMULATED_DEVICE.firmware_version)

    def _queue(self, response: StpgStatusResponse) -> None:
        self.last = response
        self.rx.extend(response.pack())

    def write(self, data: bytes) -> int:
        with self._lock:
            if self.pending is not None:
                command, self.pending = self.pending, None
                if (command.arg0, command.arg1, command.arg2) != (len(self.buffer), len(data), crc32(data)):
                    self._queue(replace(self.last, rsp=c.RSP_ERROR, state=c.STATE_ERROR, error_code=1))
                    return len(data)
                self.buffer.extend(data)
                self._queue(replace(self.last, rsp=c.RSP_DATA_ACK, run_progress=len(self.buffer)))
                return len(data)
            command = StpgCommand.unpack(data)
            self.commands.append(command.cmd)
            self._handle(command)
            return len(data)

    def _handle(self, command: StpgCommand) -> None:
        response = replace(self.last, seq=command.seq, rsp=c.RSP_OK)
        if command.cmd == c.CMD_INFO:
            response = replace(response, rsp=c.RSP_INFO)
        elif command.cmd == c.CMD_GET_CLOCK_INFO:
            response = replace(response, rsp=c.RSP_CLOCK_INFO, pattern_size=SIMULATED_TICK_HZ, event_count=6,
                               run_progress=65535, result_flags=SIMULATED_TICK_HZ, fail_count=SIMULATED_TICK_HZ)
        elif command.cmd == c.CMD_RESET:
            self.holding = False
            self.hold = (0, 0)
            self.requirements = None
            self.buffer.clear()
            response = replace(response, state=c.STATE_IDLE, active_pins=0, pattern_size=0, event_count=0,
                               run_progress=0, result_flags=0, fail_count=0, sample_count=0)
        elif command.cmd == c.CMD_UPLOAD_BEGIN:
            self.buffer.clear()
            self.total_size, self.total_crc = command.arg1, command.arg2
            response = replace(response, state=c.STATE_RECEIVING, active_pins=0, pattern_size=0, event_count=0,
                               run_progress=0, result_flags=0, fail_count=0, sample_count=0)
        elif command.cmd == c.CMD_UPLOAD_DATA:
            self.pending = command
            response = replace(response, rsp=c.RSP_READY_FOR_PAYLOAD, run_progress=len(self.buffer))
        elif command.cmd == c.CMD_UPLOAD_END:
            payload = bytes(self.buffer)
            if len(payload) != self.total_size or crc32(payload) != self.total_crc:
                response = replace(response, rsp=c.RSP_ERROR, state=c.STATE_ERROR, error_code=2)
            else:
                req = self.requirements = inspect_pattern_requirements(payload)
                response = replace(response, state=c.STATE_HOLD_AWAITING_NEXT if self.holding else c.STATE_READY,
                                   active_pins=req.active_pins, pattern_size=len(payload),
                                   event_count=req.estimated_event_count, run_progress=0)
        elif command.cmd == c.CMD_RUN:
            error = self._run_error(command)
            self.run_args.append((command.arg0, command.arg1, command.arg2, command.arg3))
            if error:
                self.rejected_runs.append(error)
                self._queue(replace(response, rsp=c.RSP_ERROR, state=c.STATE_ERROR, error_code=error))
                return
            self.run_flags = command.arg0
            self.hold = derive_terminal_hold(bytes(self.buffer), *(self.hold if self.holding else (0, 0)))
            self.polls = 0
            response = replace(response, state=c.STATE_RUNNING, run_progress=0)
            index = self.runs
            self.runs += 1
            if self.on_run is not None:
                self.on_run(index)
        elif command.cmd == c.CMD_GET_STATUS:
            response = replace(response, rsp=c.RSP_STATUS)
            if self.requirements and response.state == c.STATE_RUNNING:
                req = self.requirements
                self.polls += 1
                if self.polls <= RUNNING_POLLS:
                    # Like the firmware (stpg_runner.c): run_progress is 0 from RUN until the output ends.
                    response = replace(response, run_progress=0)
                else:
                    self.holding = bool(self.run_flags & c.RUN_FLAG_SEGMENT_END_HOLD)
                    fails = min(self.fails, req.estimated_compare_count)
                    flags = ((c.STPG_RESULT_FLAG_COMPARE_MISMATCH if fails else 0)
                             | (c.STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW if fails > c.STPG_FAIL_LOG_MAX else 0))
                    response = replace(response, state=c.STATE_HOLD_AWAITING_NEXT if self.holding else c.STATE_DONE,
                                       run_progress=req.estimated_event_count,
                                       sample_count=req.estimated_sample_count, fail_count=fails,
                                       result_flags=flags)
            elif self.requirements is None:
                response = replace(response, pattern_size=0, event_count=0, run_progress=0, result_flags=0,
                                   fail_count=0, sample_count=0)
        elif command.cmd == c.CMD_READ_FAIL_LOG:
            offset, count = command.arg0, command.arg1
            if (response.state not in (c.STATE_DONE, c.STATE_HOLD_AWAITING_NEXT)
                    or command.arg2 or command.arg3 or not 1 <= count <= 2
                    or offset + count > min(response.fail_count, c.STPG_FAIL_LOG_MAX)):
                self._queue(replace(response, rsp=c.RSP_ERROR, error_code=c.ERR_UNSUPPORTED))
                return
            payload = b"".join(struct.pack("<IIII", index, 1, 0, 1) for index in range(offset, offset + count))
            terminal = self.last
            self._queue(replace(response, rsp=c.RSP_READY_FOR_READ, pattern_size=len(payload),
                                event_count=count, run_progress=offset, result_flags=c.STPG_RESULT_TYPE_FAIL_LOG))
            self.rx.extend(payload)
            self._queue(replace(response, rsp=c.RSP_DATA_END, pattern_size=len(payload),
                                event_count=crc32(payload), run_progress=offset, result_flags=c.STPG_RESULT_TYPE_FAIL_LOG))
            self.last = terminal
            return
        else:
            response = replace(response, rsp=c.RSP_ERROR, error_code=0x7F)
        self._queue(response)

    def _run_error(self, command: StpgCommand) -> int:
        """stpg_runner.c start validation (fail-stop and measurement modes are not modelled: unsupported)."""

        flags, entry = command.arg0, (command.arg1, command.arg2)
        preserve = bool(flags & c.RUN_FLAG_PRESERVE_GPIO_STATE)
        hold_flags = c.RUN_FLAG_PRESERVE_GPIO_STATE | c.RUN_FLAG_SEGMENT_END_HOLD
        if self.requirements is None:
            return c.ERR_NO_PATTERN
        if command.arg3:
            return c.ERR_UNSUPPORTED
        if flags & c.RUN_FLAG_STREAMING_POC:
            if flags & ~(c.RUN_FLAG_STREAMING_POC | hold_flags):
                return c.ERR_UNSUPPORTED
            if preserve:
                return c.ERR_HOLD_STATE_MISMATCH if entry != self.hold else 0
            return c.ERR_UNSUPPORTED if entry != (0, 0) else 0
        if flags & ~hold_flags:
            return c.ERR_UNSUPPORTED
        if preserve:
            if not self.holding:
                return c.ERR_BAD_STATE
            return c.ERR_HOLD_STATE_MISMATCH if entry != self.hold else 0
        if entry != (0, 0):
            return c.ERR_UNSUPPORTED
        return c.ERR_BAD_STATE if self.holding else 0

    def read(self, size: int) -> bytes:
        with self._lock:
            part = bytes(self.rx[:size])
            del self.rx[:size]
            return part

    def close(self) -> None:
        self.closed += 1


class SimulatedController:
    """Line protocol of the RP2040 GPIO sketch; ``legacy=True`` answers like the old three-channel sketch."""

    timeout = 0.05
    STATES = {"l": "LOW", "z": "Hi-Z", "h": "HIGH"}

    def __init__(self, *, legacy: bool = False, legacy_pins: tuple[int, ...] = (0, 1, 2),
                 on_write: Callable[[bytes], None] | None = None) -> None:
        self.legacy = legacy
        self.legacy_pins = legacy_pins
        self.on_write = on_write
        self.pins = (0, 1, 2) if legacy else SUPPORTED_PINS
        self.states = {pin: "Hi-Z" for pin in self.pins}
        self.token = ""
        self.sequence = 0
        self.rx = bytearray()
        self.writes: list[bytes] = []
        self.applied: list[tuple[int, str]] = []
        self.closed = 0
        self._line = bytearray()
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        with self._lock:
            self.writes.append(bytes(data))
            if self.on_write is not None:
                self.on_write(bytes(data))
            for byte in data:
                if byte in (10, 13):
                    if self._line:
                        self._reply(self._command(self._line.decode("ascii", errors="replace").strip()))
                        self._line.clear()
                else:
                    self._line.append(byte)
            return len(data)

    def _reply(self, line: str | None) -> None:
        if line is not None:
            self.rx.extend(line.encode("ascii") + b"\r\n")

    def _set(self, pin: int, op: str) -> str:
        state = self.STATES[op]
        self.states[pin] = state
        self.applied.append((pin, state))
        return f"CH{pin} = {state}"

    def _command(self, text: str) -> str | None:
        if not self.legacy:
            if text.startswith("i "):
                token = text[2:]
                if not re.fullmatch(r"[0-9a-f]{16}", token):
                    return "ERROR invalid session token"
                self.token, self.sequence = token, 0
                return f"STPG_GPIO_V1 {token} GPIO=" + ",".join(map(str, SUPPORTED_PINS))
            tagged = re.fullmatch(r"@([0-9a-f]{16}):([1-9][0-9]*) ([1-9][0-9]*|0)([lzhLZH])", text)
            if text.startswith("@"):
                if (tagged is None or tagged[1] != self.token or int(tagged[2]) != self.sequence + 1
                        or int(tagged[3]) not in self.pins):
                    return "ERROR stale or invalid session sequence"
                self.sequence += 1
                return f"@{self.token}:{self.sequence} " + self._set(int(tagged[3]), tagged[4].lower())
            self.token = ""  # manual/legacy access invalidates tagged ownership, as the sketch does
        if text in ("s", "S"):
            return "Status:\r\n" + "\r\n".join(f"CH{index} GPIO{pin} state={self.states[index]}"
                                                for index, pin in enumerate(self.legacy_pins[:3]))
        plain = re.fullmatch(r"([1-9][0-9]*|0)([lzhLZH])", text)
        if plain is None or int(plain[1]) not in self.pins:
            return "Invalid command"
        return self._set(int(plain[1]), plain[2].lower())

    def read(self, size: int) -> bytes:
        with self._lock:
            part = bytes(self.rx[:size])
            del self.rx[:size]
            return part

    def close(self) -> None:
        self.closed += 1
