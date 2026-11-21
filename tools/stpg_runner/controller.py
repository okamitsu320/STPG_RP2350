"""Optional RP2040/XIAO GPIO client. Import/validation never contacts a device.

GPIO0/1 are RUN: ordinary pattern invocations must not reset them. Manual
reset is a separate explicit operation. ACKs are software states, not voltage.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable

from .artifacts import RunnerError, read_json, write_json
from .device import DeviceLease, physical_identity_key, validate_port, enumerate_ports
from .progress import CancellationToken
from .execution import RunCancelled

SUPPORTED_PINS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 14, 15, 26)
STATES = {"LOW": "l", "Hi-Z": "z", "HIGH": "h"}
REPLY_TIMEOUT_S = 5.0
MAX_READ_BYTES = 16384
MAX_LINE_BYTES = 512


@dataclass(frozen=True)
class ControllerConfig:
    port: str
    vid: int
    pid: int
    serial_number: str
    protocol: str
    allow_high: tuple[int, ...] = ()
    legacy_pins: tuple[int, ...] = ()

    @classmethod
    def from_dict(cls, value: dict) -> "ControllerConfig":
        fields = {"kind", "schema_version", "port", "vid", "pid", "serial_number", "protocol", "allow_high", "legacy_pins"}
        if not isinstance(value, dict) or set(value) != fields:
            raise RunnerError("controller config has missing/unknown fields")
        if value["kind"] != "stpg_gpio_controller" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise RunnerError("unsupported controller schema")
        if value["protocol"] not in ("rp2040-gpio-v1", "legacy-3"):
            raise RunnerError("controller protocol must be explicitly selected")
        for name in ("port", "serial_number"):
            item = value[name]
            if not isinstance(item, str) or not item.strip() or item != item.strip() or any(ord(x) < 32 for x in item):
                raise RunnerError(f"invalid controller {name}")
        for name in ("allow_high", "legacy_pins"):
            item = value[name]
            if (not isinstance(item, list) or any(type(x) is not int or not 0 <= x <= 48 for x in item)
                    or len(item) != len(set(item))):
                raise RunnerError(f"invalid controller {name}")
        allowed = set(SUPPORTED_PINS if value["protocol"] == "rp2040-gpio-v1" else (0, 1, 2))
        if not set(value["allow_high"]) <= allowed:
            raise RunnerError("HIGH allow-list contains an unsupported pin")
        if (value["protocol"] == "legacy-3" and len(value["legacy_pins"]) != 3
                or value["protocol"] != "legacy-3" and value["legacy_pins"]):
            raise RunnerError("legacy-3 requires exactly three expected physical pin numbers; v1 requires []")
        result = cls(value["port"], value["vid"], value["pid"], value["serial_number"], value["protocol"],
                     tuple(value["allow_high"]), tuple(value["legacy_pins"]))
        physical_identity_key(result)
        return result

    def to_dict(self) -> dict:
        result = {"kind": "stpg_gpio_controller", "schema_version": 1, **asdict(self)}
        result["allow_high"], result["legacy_pins"] = list(self.allow_high), list(self.legacy_pins)
        return result

    def validate_action(self, pin: int, state: str, *, run_attached: bool = False, allow_run_high: bool = False) -> None:
        if type(run_attached) is not bool or type(allow_run_high) is not bool:
            raise RunnerError("GPIO policy overrides must be explicit booleans")
        allowed = SUPPORTED_PINS if self.protocol == "rp2040-gpio-v1" else (0, 1, 2)
        if type(pin) is not int or pin not in allowed or not isinstance(state, str) or state not in STATES:
            raise RunnerError("unsupported GPIO pin/state")
        if run_attached and pin in (0, 1):
            raise RunnerError("RUN pins cannot be changed by a pattern-run action; use explicit manual control/reset")
        if state == "HIGH" and (pin not in self.allow_high or pin in (0, 1) and not allow_run_high):
            raise RunnerError("HIGH requires its allow-list and, for RUN, explicit allow_run_high=True")


def load_controller_config(path: Path) -> ControllerConfig:
    return ControllerConfig.from_dict(read_json(path))


def ensure_separate(controller: ControllerConfig, identities: list[Any]) -> None:
    import os
    endpoint = lambda p: os.path.realpath(p) if os.name != "nt" else p.casefold()
    for other in identities:
        if (physical_identity_key(controller) == physical_identity_key(other)
                or endpoint(controller.port) == endpoint(other.port)):
            raise RunnerError("controller aliases an STPG/LA endpoint or identity")


def open_controller_serial(config: ControllerConfig):  # pragma: no cover - hardware only
    import serial
    # USB/OS and DTR/RTS effects of open/close are unmeasured; no later line-control.
    return serial.Serial(port=config.port, baudrate=115200, timeout=0.05, write_timeout=2.0)


class ControllerSession:
    def __init__(self, config: ControllerConfig, *, stream_factory=None, port_provider=None,
                 cancel: CancellationToken | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = ControllerConfig.from_dict(config.to_dict())
        self.factory, self.ports, self.clock = stream_factory, port_provider, clock
        self.token = cancel or CancellationToken()
        self.stream = self.lease = None
        self.buffer = bytearray()
        self.received = 0
        self.sequence = 0
        self.nonce = secrets.token_hex(8)
        self.journal: list[dict] = []
        self.close_attempts = self.close_successes = 0
        self.closed = self.failed = False
        self.states: dict[int, str] = {}

    def check(self) -> None:
        if self.token.cancelled:
            raise RunCancelled("controller cancelled; last levels may remain, no corrective write")
        if self.failed or self.closed:
            raise RunnerError("controller session is sealed")

    def _log(self, event: str, **fields) -> dict:
        record = {"event": event, "time_s": self.clock(), **fields}
        self.journal.append(record)
        return record

    def _write(self, data: bytes) -> None:
        self.check()
        record = self._log("write.begin", planned_hex=data.hex(), written_bytes=None)
        try:
            count = self.stream.write(data)
            record["written_bytes"] = count
            if type(count) is not int or count != len(data):
                raise RunnerError("short controller write; not resent")
        except BaseException as exc:
            self.failed = True
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["end_s"] = self.clock()

    def _line(self, deadline: float) -> str:
        while True:
            self.check()
            if self.clock() >= deadline:
                raise RunnerError("controller response timeout; prefix retained")
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_LINE_BYTES:
                    raise RunnerError("controller line limit exceeded")
                raw = bytes(self.buffer[:newline]); del self.buffer[:newline + 1]
                return raw.decode("ascii").rstrip("\r")
            if len(self.buffer) > MAX_LINE_BYTES:
                raise RunnerError("controller unterminated line limit exceeded")
            record = self._log("read.begin")
            try:
                part = self.stream.read(256)
                record["raw_hex"] = bytes(part).hex()
                self.received += len(part)
                self.buffer.extend(part)
            finally:
                record["end_s"] = self.clock()
            if len(part) > 256 or self.received > MAX_READ_BYTES:
                raise RunnerError("controller receive limit exceeded")

    @staticmethod
    def _startup(line: str) -> bool:
        return (line == "" or line in ("Commands:", "Status:",
                "RP2040 Zero multi GPIO 3-state controller", "XIAO ESP32-C3 multi GPIO 3-state controller")
                or re.fullmatch(r"  [012][lzhns?].*|  [s?]  :.*|  Extended GPIO:.*|  a  :.*|  i TOKEN :.*", line) is not None
                or re.fullmatch(r"CH[012] (?:GPIO\d+|pin=\d+) state=(?:LOW|Hi-Z|HIGH)", line) is not None)

    def open(self) -> None:
        self.check()
        if self.stream is not None or self.lease is not None:
            raise RunnerError("controller can be opened only once")
        self.lease = DeviceLease(self.config)
        self.lease.__enter__()
        try:
            self._log("metadata", value=validate_port(self.config, (self.ports or enumerate_ports)()))
            self.check()
            self._log("open.begin")
            self.stream = (self.factory or open_controller_serial)(self.config)
            self._log("open.end")
            deadline = self.clock() + REPLY_TIMEOUT_S
            if self.config.protocol == "rp2040-gpio-v1":
                self._write(f"i {self.nonce}\n".encode("ascii"))
                wanted = f"STPG_GPIO_V1 {self.nonce} GPIO=" + ",".join(map(str, SUPPORTED_PINS))
                for _ in range(128):
                    line = self._line(deadline)
                    if line == wanted:
                        if self.buffer:
                            self._log("identity.line", line=line, disposition="REJECT_TRAILING_DATA")
                            raise RunnerError("unexpected data after controller identity")
                        self._log("identity.line", line=line, disposition="MATCH")
                        return
                    startup = self._startup(line)
                    self._log("identity.line", line=line, disposition="STARTUP" if startup else "REJECT")
                    if not startup:
                        raise RunnerError("unexpected controller identity/startup line")
                raise RunnerError("controller startup-line count exceeded")
            self._write(b"s\n")
            # Legacy responses are uncorrelated. Reject ambiguity; never probe pins or fallback.
            for _ in range(128):
                line = self._line(deadline)
                if line == "Status:":
                    self._log("identity.line", line=line, disposition="LEGACY_STATUS_HEADER")
                    break
                startup = self._startup(line)
                self._log("identity.line", line=line, disposition="STARTUP" if startup else "REJECT")
                if not startup:
                    raise RunnerError("unexpected legacy controller startup")
            else:
                raise RunnerError("legacy status header missing")
            for channel, pin in enumerate(self.config.legacy_pins):
                line = self._line(deadline)
                match = re.fullmatch(rf"CH{channel} (?:GPIO{pin}|pin={pin}) state=(LOW|Hi-Z|HIGH)", line)
                self._log("identity.line", line=line, disposition="LEGACY_STATUS" if match else "REJECT")
                if match is None:
                    raise RunnerError("legacy controller map/status mismatch")
                self.states[channel] = match[1]
        except BaseException:
            self.failed = True
            raise

    def set(self, pin: int, state: str, *, allow_run_high: bool = False) -> None:
        self.config.validate_action(pin, state, allow_run_high=allow_run_high)
        try:
            self.check()
            if self.stream is None:
                raise RunnerError("controller is not open")
            self.sequence += 1
            prefix = f"@{self.nonce}:{self.sequence} " if self.config.protocol == "rp2040-gpio-v1" else ""
            request = f"{prefix}{pin}{STATES[state]}\n".encode("ascii")
            self._write(request)
            line = self._line(self.clock() + REPLY_TIMEOUT_S)
            if line != f"{prefix}CH{pin} = {state}":
                raise RunnerError("controller ACK/tag/pin/state mismatch; not retried")
            self.states[pin] = state
            self._log("state.acknowledged", pin=pin, state=state, physical_readback=False)
        except BaseException:
            self.failed = True
            raise

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.stream is not None:
                self.close_attempts += 1
                self._log("close.begin")
                try:
                    self.stream.close()
                    self.close_successes += 1
                finally:
                    self._log("close.end")
        finally:
            if self.lease is not None and self.lease.fd is not None:
                self.lease.__exit__(None, None, None)

    def evidence(self) -> dict:
        return {"journal": self.journal, "close_attempts": self.close_attempts,
                "close_successes": self.close_successes, "last_acknowledged_states": {str(k): v for k, v in self.states.items()},
                "pending_hex": bytes(self.buffer).hex(), "physical_readback": False,
                "legacy_uncorrelated": self.config.protocol == "legacy-3"}


def identify_controller(config: ControllerConfig, *, output_dir: Path, confirm_identify: bool = False,
                        stream_factory=None, port_provider=None, cancel=None,
                        clock=time.monotonic) -> dict:
    """One explicit identity/status exchange, no GPIO command or protocol fallback.

    USB open/close and the v1 session-token change are not electrically read-only.
    A close/checkpoint failure keeps the consumed prefix in the returned report.
    """
    if confirm_identify is not True:
        raise RunnerError("explicit confirm_identify=True is required")
    from .controller_gate import check_controller_contract
    gate = check_controller_contract()
    if gate["status"] != "PASS":
        raise RunnerError(f"controller structural gate rejected: {gate['errors']}")
    config = ControllerConfig.from_dict(config.to_dict())
    output = Path(output_dir).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()  # never overwrite a previous invocation, including failed evidence
    session = ControllerSession(config, stream_factory=stream_factory, port_provider=port_provider,
                                cancel=cancel, clock=clock)
    report = {"kind": "stpg_gpio_identify_result", "schema_version": 1, "status": "PENDING",
              "config": config.to_dict(), "protocol": config.protocol, "errors": [],
              "pin_commands_written": 0, "physical_readback": False, "result_persisted": False,
              "gate": gate, "result_file": str(output / "identify.json")}
    write_json(output / "identify.json", report)  # fail before contact if evidence cannot be reserved
    try:
        session.open()
        session.check()
        report["status"] = "IDENTIFIED"
    except (RunCancelled, KeyboardInterrupt) as exc:
        report["status"] = "CANCELLED"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    except BaseException as exc:
        report["status"] = "REJECT"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            session.close()
        except BaseException as exc:
            report["status"] = "REJECT"
            report["errors"].append(f"close: {type(exc).__name__}: {exc}")
    report["transport"] = session.evidence()
    journal = report["transport"]["journal"]
    report["sent"] = [bytes.fromhex(item["planned_hex"]).decode("ascii", "replace").rstrip("\n")
                      for item in journal if item["event"] == "write.begin"]
    received = b"".join(bytes.fromhex(item.get("raw_hex", "")) for item in journal if item["event"] == "read.begin")
    report["received_lines"] = [line.rstrip("\r") for line in received.decode("ascii", "replace").split("\n")
                                if line.strip()][-24:]  # display only; the full raw prefix stays in transport
    report["legacy_states"] = dict(report["transport"]["last_acknowledged_states"])
    try:
        report["result_persisted"] = True
        write_json(output / "identify.json", report)
    except BaseException as exc:
        report["status"] = "REJECT"; report["result_persisted"] = False
        report["errors"].append(f"checkpoint: {type(exc).__name__}: {exc}")
    return report


def manual_control(config: ControllerConfig, *, output_dir: Path, pin: int, state: str | None = None,
                   pulse_ms: int | None = None, target=None, settle_ms: int = 0,
                   confirm_control: bool = False, allow_run_high: bool = False,
                   stream_factory=None, port_provider=None, cancel=None,
                   clock=time.monotonic, sleep=time.sleep) -> dict:
    """Separate explicit operation. No STPG/LA open, auto-RUN, retry or failure cleanup."""
    if confirm_control is not True:
        raise RunnerError("explicit confirm_control=True is required")
    from .controller_gate import check_controller_contract
    gate = check_controller_contract()
    if gate["status"] != "PASS":
        raise RunnerError(f"controller structural gate rejected: {gate['errors']}")
    config = ControllerConfig.from_dict(config.to_dict())
    if type(settle_ms) is not int or not 0 <= settle_ms <= 10000:
        raise RunnerError("settle_ms must be 0..10000")
    if pulse_ms is not None:
        if pin not in (0, 1) or state is not None or type(pulse_ms) is not int or not 1 <= pulse_ms <= 5000 or target is None:
            raise RunnerError("manual reset requires RUN pin, target identity and pulse_ms=1..5000")
    elif state is None:
        raise RunnerError("manual set requires a state")
    requested_state = "LOW" if pulse_ms is not None else state
    config.validate_action(pin, requested_state, allow_run_high=allow_run_high)
    if pin in (0, 1) and target is None:
        raise RunnerError("RUN control requires the target's separate identity binding")
    if pin in (0, 1):
        from .external_binding import target_binding
        target = target_binding(target.to_dict())
        target_kinds = ("stpg_runner_device",) if pin == 0 else ("stpg_capture_analyzer", "stpg_external_device")
        if target.to_dict().get("kind") not in target_kinds:
            raise RunnerError("RUN pin and target binding kind differ")
    if target is not None:
        ensure_separate(config, [target])
    output = Path(output_dir).resolve()
    # The target lease is obtained before controller open; active SDK/LA owners reject.
    lease = DeviceLease(target) if target is not None else None
    session = ControllerSession(config, stream_factory=stream_factory, port_provider=port_provider, cancel=cancel, clock=clock)
    report = {"kind": "stpg_gpio_control_result", "schema_version": 1, "outcome": "PENDING",
              "config": config.to_dict(), "pin": pin, "state": state, "pulse_ms": pulse_ms,
              "settle_ms": settle_ms, "result_persisted": False, "errors": [], "electrical_release_verified": False}
    report["gate"] = gate
    if lease is not None:
        lease.__enter__()
    try:
        output.parent.mkdir(parents=True, exist_ok=True); output.mkdir()
        write_json(output / "control.json", report)
        try:
            session.open()
            session.set(pin, requested_state, allow_run_high=allow_run_high)
            if pulse_ms is not None:
                sleep(pulse_ms / 1000)
                session.check()
                session.set(pin, "Hi-Z")
            if settle_ms:
                sleep(settle_ms / 1000)
                session.check()
            report["outcome"] = "COMPLETED"
        except (RunCancelled, KeyboardInterrupt) as exc:
            report["outcome"] = "CANCELLED"; report["errors"].append(f"{type(exc).__name__}: {exc}")
        except BaseException as exc:
            report["outcome"] = "REJECT"; report["errors"].append(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                session.close()
            except BaseException as exc:
                report["outcome"] = "REJECT"; report["errors"].append(f"close: {type(exc).__name__}: {exc}")
        report["transport"] = session.evidence()
        try:
            report["result_persisted"] = True
            write_json(output / "control.json", report)
        except BaseException as exc:
            report["result_persisted"] = False; report["outcome"] = "REJECT"
            report["errors"].append(f"checkpoint: {type(exc).__name__}: {exc}")
        return report
    finally:
        if lease is not None:
            lease.__exit__(None, None, None)
