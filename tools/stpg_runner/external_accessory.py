"""Explicit subprocess accessories using file references and flat lifecycle facts.

No plug-in imports/discovery, shell, LA protocol, wire constants or device opener.
The executable is trusted user-selected software, not a sandbox. Process
separation is NOT a legal determination or a guarantee against hardware effects.
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import queue
import secrets
import subprocess
import threading
import time
from typing import Any

from .artifacts import (RunnerError, canonical_json, confined_path, identity, write_json,
                        _unique_object, _bad_constant)
from .controller import ControllerConfig, ensure_separate
from .device import DeviceConfig, enumerate_ports, validate_port
from .execution import RunHooks, prepare_run
from .external_binding import ExternalDeviceBinding
from .preparation import _invocation_scope
from .progress import BuildCancelled, CancellationToken

PROTOCOL = "stpg_external_accessory_v1"
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_PACKET_BYTES = 32 * 1024
MAX_STDOUT_BYTES = 256 * 1024
MAX_STDERR_BYTES = 64 * 1024


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise RunnerError("expected an exact lowercase SHA-256")
    return value


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_bad_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RunnerError("invalid external accessory JSON") from exc
    if not isinstance(value, dict):
        raise RunnerError("external accessory JSON must be an object")
    return value


def _input(path: Path, expected: str | None = None) -> tuple[bytes, dict]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise RunnerError("external accessory input exceeds the file limit")
    if expected is not None and _sha(raw) != _digest(expected):
        raise RunnerError("external accessory input hash changed")
    decoded = raw
    if path.suffix == ".gz":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                decoded = stream.read(MAX_INPUT_BYTES + 1)
        except (OSError, EOFError) as exc:
            raise RunnerError("invalid compressed external accessory input") from exc
        if len(decoded) > MAX_INPUT_BYTES:
            raise RunnerError("decoded external accessory input exceeds the file limit")
    return raw, _json(decoded)


def _text(value: Any, maximum: int = 4096) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or value != value.strip() or any(ord(c) < 32 for c in value)):
        raise RunnerError("invalid external accessory text/path")
    return value


@dataclass(frozen=True)
class ExternalAccessorySpec:
    identifier: str
    command: tuple[str, ...]
    executable_sha256: str
    binding_file: Path
    binding_sha256: str
    plan_file: Path
    plan_sha256: str
    segment: int
    mode: str
    timeout_s: float
    open_timeout_s: float | None = None
    close_timeout_s: float = 1.0

    @classmethod
    def from_dict(cls, value: dict, *, base: Path = Path(".")) -> "ExternalAccessorySpec":
        fields = {"kind", "schema_version", "identifier", "command", "executable_sha256",
                  "binding_file", "binding_sha256", "plan_file", "plan_sha256", "segment", "mode", "timeout_s"}
        optional = {"open_timeout_s", "close_timeout_s"}
        if (not isinstance(value, dict) or fields - value.keys() or value.keys() - fields - optional
                or value["kind"] != "stpg_external_accessory" or type(value["schema_version"]) is not int
                or value["schema_version"] != 1):
            raise RunnerError("invalid external accessory config kind/schema/fields")
        identifier = _text(value["identifier"], 64)
        if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in identifier):
            raise RunnerError("external accessory identifier must be lowercase ASCII")
        command = value["command"]
        if not isinstance(command, list) or not 1 <= len(command) <= 32:
            raise RunnerError("external accessory command requires 1..32 literal arguments")
        command = tuple(_text(part) for part in command)
        if not Path(command[0]).is_absolute():
            raise RunnerError("external executable must be an explicit absolute path, not a PATH lookup")
        if type(value["segment"]) is not int or not 0 <= value["segment"] < 256:
            raise RunnerError("external accessory segment must be 0..255")
        if value["mode"] not in ("device", "simulated"):
            raise RunnerError("external accessory mode must be device or simulated")
        timeout = value["timeout_s"]
        if (type(timeout) not in (int, float) or not 0.1 <= timeout <= 20 or not math.isfinite(timeout)):
            raise RunnerError("external accessory timeout_s must be finite and 0.1..20")
        opened = value.get("open_timeout_s")
        closed = value.get("close_timeout_s", 1.0)
        for name, limit, maximum in (("open_timeout_s", opened, 3600), ("close_timeout_s", closed, 20)):
            if name == "open_timeout_s" and limit is None:
                continue
            if type(limit) not in (int, float) or not 0.1 <= limit <= maximum or not math.isfinite(limit):
                raise RunnerError(f"external accessory {name} must be finite and 0.1..{maximum}")
        return cls(identifier, command, _digest(value["executable_sha256"]),
                   (base / _text(value["binding_file"])).resolve(), _digest(value["binding_sha256"]),
                   (base / _text(value["plan_file"])).resolve(), _digest(value["plan_sha256"]),
                   value["segment"], value["mode"], float(timeout),
                   None if opened is None else float(opened), float(closed))

    def to_dict(self) -> dict:
        return {"kind": "stpg_external_accessory", "schema_version": 1, "identifier": self.identifier,
                "command": list(self.command), "executable_sha256": self.executable_sha256,
                "binding_file": str(self.binding_file), "binding_sha256": self.binding_sha256,
                "plan_file": str(self.plan_file), "plan_sha256": self.plan_sha256,
                "segment": self.segment, "mode": self.mode, "timeout_s": self.timeout_s,
                "open_timeout_s": self.open_timeout_s, "close_timeout_s": self.close_timeout_s}


def load_external_accessory(path: Path) -> ExternalAccessorySpec:
    path = Path(path).resolve()
    _, value = _input(path)
    return ExternalAccessorySpec.from_dict(value, base=path.parent)


@dataclass(frozen=True)
class PreparedExternalAccessory:
    spec: ExternalAccessorySpec
    manifest_sha256: str
    target_pattern_sha256: str
    binding: ExternalDeviceBinding
    binding_bytes: bytes
    plan_bytes: bytes

    def to_dict(self) -> dict:
        return {"kind": "stpg_prepared_external_accessory", "schema_version": 1,
                "spec": self.spec.to_dict(), "manifest_sha256": self.manifest_sha256,
                "target_pattern_sha256": self.target_pattern_sha256,
                "device_identity": self.binding.to_dict(), "device_io_performed": False,
                "process_started": False, "physical_execution_authorized": False,
                "plan_semantics_validated_by_core": False}


def _launcher(spec: ExternalAccessorySpec) -> None:
    executable = Path(spec.command[0])
    if not executable.is_file() or executable.stat().st_size > 64 * 1024 * 1024:
        raise RunnerError("external executable is missing or too large")
    if identity(executable)["sha256"] != spec.executable_sha256:
        raise RunnerError("external executable hash changed")


def prepare_external_accessory(bundle_dir: Path, *, manifest_sha256: str, device: DeviceConfig,
                               spec: ExternalAccessorySpec, cancel=None, on_progress=None) -> PreparedExternalAccessory:
    """File-only preparation; no launch, registry discovery or USB enumeration.

    Hashes bind files/launcher, NOT its transitive code, dependencies or behavior.
    The selected companion must independently validate its opaque plan before I/O.
    """
    token = cancel or CancellationToken()
    token.check()
    spec = ExternalAccessorySpec.from_dict(spec.to_dict())
    device = DeviceConfig.from_dict(device.to_dict())
    _launcher(spec)
    binding_bytes, raw_binding = _input(spec.binding_file, spec.binding_sha256)
    plan_bytes, _ = _input(spec.plan_file, spec.plan_sha256)
    binding = ExternalDeviceBinding.from_dict(raw_binding)
    ensure_separate(device, [binding])
    plan = prepare_run(bundle_dir, manifest_sha256=manifest_sha256, cancel=token, on_progress=on_progress)
    if spec.segment >= len(plan.segments):
        raise RunnerError("external accessory target segment is outside this bundle")
    token.check()
    return PreparedExternalAccessory(spec, manifest_sha256, plan.segments[spec.segment].to_dict()["sha256"],
                                     binding, binding_bytes, plan_bytes)


class _ProcessSession:
    def __init__(self, prepared: PreparedExternalAccessory, root: Path, bundle: Path, *, token, factory=None):
        self.prepared, self.root, self.bundle, self.token = prepared, root, bundle, token
        self.factory = factory or subprocess.Popen
        self.process = None
        self.nonce = secrets.token_hex(16)
        self.events: queue.Queue = queue.Queue(maxsize=128)
        self.stop = threading.Event()
        self.readers: list[threading.Thread] = []
        self.writer = None
        self.sealed = False
        self.completed = False
        self.owned_output = False
        self.pending = bytearray()
        self.report = {"kind": "stpg_external_accessory_session", "schema_version": 1,
                       "outcome": "PENDING", "adopted": False, "mode": prepared.spec.mode,
                       "prepared": prepared.to_dict(), "requests": [], "stdout_hex": "", "stderr_hex": "",
                       "errors": [], "spawn_attempts": 0, "stdin_close_attempts": 0,
                       "terminate_attempts": 0, "kill_attempts": 0, "returncode": None,
                       "pipe_eof_seen": [], "output_drain_complete": False,
                       "result_persisted": False, "cancel_requested": False, "unadmitted_stdout_bytes": 0,
                       "stdin_eof_grace_s": prepared.spec.close_timeout_s, "firmware_validation_pass": False,
                       "waveform_validation_pass": False, "hardware_effects_possible": False}

    def checkpoint(self):
        if not self.owned_output:
            raise RunnerError("external accessory has no owned evidence directory")
        self.report["pending_stdout_hex"] = bytes(self.pending).hex()
        write_json(self.root / "session.json", self.report)

    def _pump(self, name, stream):
        try:
            while not self.stop.is_set():
                raw = stream.read(4096)
                item = (name, raw, None)
                while not self.stop.is_set():
                    try:
                        self.events.put(item, timeout=0.05)
                        break
                    except queue.Full:
                        pass
                if not raw:
                    return
        except BaseException as exc:
            try:
                self.events.put((name, b"", f"{type(exc).__name__}: {exc}"), timeout=0.05)
            except queue.Full:
                pass

    def _consume(self, item, *, idle=False):
        name, raw, error = item
        if not raw and error is None:
            self.report["pipe_eof_seen"] = sorted(set(self.report["pipe_eof_seen"]) | {name})
        field = f"{name}_hex"
        limit = MAX_STDOUT_BYTES if name == "stdout" else MAX_STDERR_BYTES
        remaining = max(0, limit - len(self.report[field]) // 2)
        self.report[field] += raw[:remaining].hex()
        if name == "stdout":
            self.pending.extend(raw[:remaining])
        if error or len(raw) > remaining:
            raise RunnerError(error or "external accessory output exceeds the bounded journal")
        if name == "stdout" and ((not raw and not self.completed) or (raw and idle)):
            raise RunnerError("external accessory EOF/unsolicited stdout")

    def check_idle(self):
        self.token.check()
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            self._consume(item, idle=True)
        code = self.process.poll() if self.process is not None else None
        if code is not None and (not self.completed or code != 0):
            raise RunnerError("external accessory exited before completing its lifecycle")

    def _error(self, message):
        if message not in self.report["errors"]:
            self.report["errors"].append(message)

    def reject(self, exc):
        self.sealed = True
        self.report["outcome"] = "CANCELLED" if isinstance(exc, (BuildCancelled, KeyboardInterrupt)) else "REJECT"
        self.report["cancel_requested"] |= isinstance(exc, (BuildCancelled, KeyboardInterrupt)) or self.token.cancelled
        self._error(f"{type(exc).__name__}: {exc}")

    def guarded(self, callback, *args):
        try:
            return callback(*args)
        except BaseException as exc:
            self.reject(exc)
            raise

    def request(self, method, expected_status, **facts):
        if self.sealed:
            raise RunnerError("external accessory transport is sealed")
        lifecycle = (("open", "READY"), ("before_run", "ARMED"), ("after_terminal", "COMPLETE"))
        index = len(self.report["requests"])
        if index >= len(lifecycle) or (method, expected_status) != lifecycle[index]:
            error = RunnerError("external accessory lifecycle request order rejected")
            self.reject(error)
            raise error
        self.guarded(self.check_idle)
        sequence = len(self.report["requests"]) + 1
        packet = canonical_json({"protocol": PROTOCOL, "nonce": self.nonce,
                                 "seq": sequence, "method": method, **facts})
        if len(packet) > MAX_PACKET_BYTES:
            raise RunnerError("external accessory request exceeds the packet limit")
        timeout = (self.prepared.spec.open_timeout_s if method == "open"
                   and self.prepared.spec.open_timeout_s is not None else self.prepared.spec.timeout_s)
        record = {"seq": sequence, "method": method, "timeout_s": timeout, "planned_hex": packet.hex(),
                  "write_begin_s": time.monotonic(), "written_bytes": None}
        self.report["requests"].append(record)
        self.checkpoint()  # durable intent before the single write attempt
        deadline = time.monotonic() + timeout
        sent: queue.Queue = queue.Queue(maxsize=1)

        def send():
            try:
                count = self.process.stdin.write(packet)
                self.process.stdin.flush()
                sent.put((count, None))
            except BaseException as exc:
                sent.put((None, f"{type(exc).__name__}: {exc}"))

        self.writer = threading.Thread(target=send, daemon=True)
        self.writer.start()
        try:
            while True:
                self.token.check()
                if time.monotonic() >= deadline:
                    raise RunnerError("external accessory write deadline exhausted")
                try:
                    count, error = sent.get(timeout=0.02)
                    break
                except queue.Empty:
                    pass
            record["written_bytes"] = count
            record["write_end_s"] = time.monotonic()
            if error or count != len(packet):
                raise RunnerError(error or "external accessory short write; not resent")
            while b"\n" not in self.pending:
                self.token.check()
                if time.monotonic() >= deadline:
                    raise RunnerError("external accessory response deadline exhausted")
                try:
                    item = self.events.get(timeout=min(0.02, max(0.001, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                self._consume(item)
                if len(self.pending) > MAX_PACKET_BYTES:
                    raise RunnerError("external accessory response exceeds the packet limit")
            self.token.check()
            if time.monotonic() >= deadline:
                raise RunnerError("external accessory response arrived after deadline")
            line, _, remainder = self.pending.partition(b"\n")
            if remainder:
                raise RunnerError("external accessory response carries unsolicited bytes")
            self.pending.clear()
            value = _json(line.rstrip(b"\r"))
            fields = {"protocol", "nonce", "seq", "method", "status", "result_file", "result_sha256"}
            if (set(value) != fields or value["protocol"] != PROTOCOL or value["nonce"] != self.nonce
                    or type(value["seq"]) is not int or value["seq"] != sequence or value["method"] != method
                    or value["status"] != expected_status):
                raise RunnerError("external accessory response identity/sequence/status rejected")
            if method != "after_terminal":
                if value["result_file"] is not None or value["result_sha256"] is not None:
                    raise RunnerError("external accessory supplied a premature result")
            record["response"] = value
            record["read_end_s"] = time.monotonic()
            self.checkpoint()
            self.token.check()
            if time.monotonic() >= deadline:
                raise RunnerError("external accessory admission completed after deadline")
            return value
        except BaseException as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            self.reject(exc)
            raise

    def start(self):
        spec = self.prepared.spec
        self.root.mkdir()
        self.owned_output = True
        inputs = self.root / "inputs"
        inputs.mkdir()
        paths = []
        for label, source, raw in (("binding", spec.binding_file, self.prepared.binding_bytes),
                                   ("plan", spec.plan_file, self.prepared.plan_bytes)):
            path = inputs / (label + ".json" + (".gz" if source.suffix == ".gz" else ""))
            with path.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            paths.append(path)
        self.checkpoint()
        self.token.check()
        _launcher(spec)
        _input(spec.binding_file, spec.binding_sha256)
        _input(spec.plan_file, spec.plan_sha256)
        self.report["spawn_attempts"] = 1
        self.report["hardware_effects_possible"] = True
        self.checkpoint()
        self.process = self.factory(list(spec.command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, shell=False, bufsize=0)
        for name in ("stdout", "stderr"):
            thread = threading.Thread(target=self._pump, args=(name, getattr(self.process, name)), daemon=True)
            self.readers.append(thread)
            thread.start()
        self.request("open", "READY", bundle_dir=str(self.bundle), manifest_sha256=self.prepared.manifest_sha256,
                     binding_file=str(paths[0]), binding_sha256=spec.binding_sha256,
                     plan_file=str(paths[1]), plan_sha256=spec.plan_sha256,
                     output_dir=str(self.root), segment=spec.segment, mode=spec.mode)

    def before(self, context):
        return self.guarded(self._before, context)

    def _before(self, context):
        # COMPLETE ends requests, not evidence validation. Normal early EOF/
        # exit0 is allowed; already queued faults must still block a later RUN.
        self.check_idle()
        if self.completed:
            return
        if context.segment_index == self.prepared.spec.segment:
            if context.pattern_sha256 != self.prepared.target_pattern_sha256:
                raise RunnerError("external accessory pattern snapshot changed")
            self.request("before_run", "ARMED", segment=context.segment_index,
                         pattern_sha256=context.pattern_sha256)

    def after(self, context, terminal):
        return self.guarded(self._after, context, terminal)

    def _after(self, context, terminal):
        if context.segment_index != self.prepared.spec.segment or self.completed:
            return
        response = self.request("after_terminal", "COMPLETE", segment=context.segment_index)
        path = confined_path(self.root, response["result_file"])
        if path == self.root / "session.json" or path.is_relative_to(self.root / "inputs"):
            raise RunnerError("external accessory result collides with host-owned evidence")
        _, result = _input(path, _digest(response["result_sha256"]))
        if (set(result) != {"kind", "schema_version", "mode", "outcome", "adopted", "errors"}
                or result["kind"] != "stpg_external_accessory_result" or type(result["schema_version"]) is not int
                or result["schema_version"] != 1 or result["mode"] != self.prepared.spec.mode
                or result["outcome"] != "COMPLETED" or type(result["adopted"]) is not bool
                or not isinstance(result["errors"], list) or result["errors"]):
            raise RunnerError("external accessory result semantics rejected")
        self.token.check()
        self.report.update(outcome="COMPLETED", adopted=result["adopted"],
                           result_file=str(path), result_sha256=response["result_sha256"])
        self.checkpoint()
        self.completed = True

    def finish(self, failure=None):
        # No IPC cleanup request, retry, device cancel/reset or recovery.
        if failure is not None:
            self.reject(failure)
        self.report["cancel_requested"] |= self.token.cancelled or isinstance(failure, (BuildCancelled, KeyboardInterrupt))
        if self.report["cancel_requested"]:
            self.report["unadmitted_stdout_bytes"] += len(self.pending)
        process = self.process
        if process is not None:
            try:
                if self.writer is not None and self.writer.is_alive() and process.poll() is None:
                    self.report["terminate_attempts"] += 1
                    process.terminate()
            except BaseException as exc:
                self.report["outcome"] = "REJECT"
                self._error(f"process terminate: {type(exc).__name__}: {exc}")
            try:
                self.report["stdin_close_attempts"] += 1
                process.stdin.close()  # EOF only, not an accessory operation
            except BaseException as exc:
                self.report["outcome"] = "REJECT"
                self._error(f"stdin close: {type(exc).__name__}: {exc}")
            try:
                try:
                    process.wait(timeout=self.prepared.spec.close_timeout_s)
                except subprocess.TimeoutExpired:
                    if self.report["terminate_attempts"] == 0:
                        self.report["terminate_attempts"] += 1
                        process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.report["kill_attempts"] += 1
                        process.kill()
                        process.wait(timeout=1)
                self.report["returncode"] = process.returncode
                if process.returncode != 0 or self.report["terminate_attempts"] or self.report["kill_attempts"]:
                    raise RunnerError("external accessory process did not exit normally")
            except BaseException as exc:
                self.report["outcome"] = "REJECT"
                self._error(f"process close: {type(exc).__name__}: {exc}")
            finally:
                # wait() observes process exit, not completion of the reader
                # threads. Drain through BOTH EOFs before stopping the pumps;
                # otherwise a just-read late stdout chunk could be discarded
                # by stop.set() and a malformed child falsely accepted.
                eof = set(self.report["pipe_eof_seen"])
                drain_deadline = time.monotonic() + 1
                while len(eof) < 2 and time.monotonic() < drain_deadline:
                    try:
                        name, raw, error = self.events.get(timeout=0.02)
                    except queue.Empty:
                        continue
                    if not raw and error is None:
                        eof.add(name)
                    field = f"{name}_hex"
                    limit = MAX_STDOUT_BYTES if name == "stdout" else MAX_STDERR_BYTES
                    remaining = max(0, limit - len(self.report[field]) // 2)
                    self.report[field] += raw[:remaining].hex()
                    if name == "stdout" and self.report["cancel_requested"]:
                        self.report["unadmitted_stdout_bytes"] += len(raw)
                    if error or len(raw) > remaining or (name == "stdout" and raw and not self.report["cancel_requested"]):
                        self.report["outcome"] = "REJECT"
                        self._error("late/overflow external accessory output after exchange")
                self.report["pipe_eof_seen"] = sorted(eof)
                self.report["output_drain_complete"] = len(eof) == 2
                if len(eof) != 2:
                    self.report["outcome"] = "REJECT"
                    self._error("external accessory output drainage incomplete")
                self.stop.set()
                for thread in self.readers:
                    thread.join(timeout=0.2)
                for name in ("stdout", "stderr"):
                    try:
                        getattr(process, name).close()
                    except BaseException as exc:
                        self.report["outcome"] = "REJECT"
                        self._error(f"pipe close: {type(exc).__name__}: {exc}")
        if self.report["outcome"] == "PENDING":
            self.report["outcome"] = "INCOMPLETE"
        if self.report["outcome"] != "COMPLETED":
            self.report["adopted"] = False
        try:
            if not self.owned_output:
                return self.report
            self.report["result_persisted"] = True
            self.checkpoint()
        except BaseException as exc:
            self.report.update(outcome="REJECT", adopted=False, result_persisted=False)
            self._error(f"checkpoint: {type(exc).__name__}: {exc}")
        return self.report


@_invocation_scope
def run_with_external_accessory(bundle_dir: Path, *, manifest_sha256: str, device: DeviceConfig,
                                spec: ExternalAccessorySpec, output_dir: Path, confirm_run=False,
                                controller: ControllerConfig | None = None, control_plan=None,
                                cancel=None, on_progress=None, stream_factory=None, port_provider=None,
                                controller_stream_factory=None, controller_port_provider=None,
                                process_factory=None, clock=time.monotonic, sleep=time.sleep) -> dict:
    """One explicit companion, then ordinary STPG/controller execution.

    Output has separate `stpg/` and `accessory/` evidence. The companion must
    validate/lease/identify its own selected USB target before replying READY.
    Nothing here can attest that an arbitrary trusted executable follows that rule.
    """
    if confirm_run is not True:
        raise RunnerError("explicit confirm_run=True is required")
    from .accessories import run_with_accessories, validate_control_plan
    token = cancel or CancellationToken()
    token.check()
    device = DeviceConfig.from_dict(device.to_dict())
    prepared = prepare_external_accessory(bundle_dir, manifest_sha256=manifest_sha256, device=device,
                                          spec=spec, cancel=token, on_progress=on_progress)
    spec = prepared.spec
    if spec.mode == "simulated":
        injected = [stream_factory, port_provider]
        if controller is not None:
            injected += [controller_stream_factory, controller_port_provider]
        if not all(callable(item) for item in injected):
            raise RunnerError("simulated accessory requires every selected core device's injected I/O and metadata")
    if (controller is None) != (control_plan is None):
        raise RunnerError("selected controller requires its binding and action plan")
    if controller is not None:
        from .controller_gate import check_controller_contract
        controller = ControllerConfig.from_dict(controller.to_dict())
        ensure_separate(controller, [device, prepared.binding])
        if check_controller_contract()["status"] != "PASS":
            raise RunnerError("controller structural gate rejected")
        plan = prepare_run(bundle_dir, manifest_sha256=manifest_sha256, cancel=token, on_progress=on_progress)
        validate_control_plan(control_plan, controller, plan)
    token.check()
    validate_port(device, (port_provider or enumerate_ports)())
    root, bundle = Path(output_dir).resolve(), Path(bundle_dir).resolve()
    if root == bundle or bundle in root.parents:
        raise RunnerError("external accessory results must be outside the immutable bundle")
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir()  # exclusive one-shot parent; no replacement after failure/crash
    session = _ProcessSession(prepared, root / "accessory", bundle, token=token, factory=process_factory)
    result = {"run": None, "control": None, "capture": None, "external": None, "run_result_file": None}
    failure = None
    try:
        session.start()
        core = run_with_accessories(bundle, manifest_sha256=manifest_sha256, device=device,
                                    output_dir=root / "stpg", confirm_run=True, controller=controller,
                                    control_plan=control_plan, run_hooks=RunHooks(session.before, session.after),
                                    cancel=token, on_progress=on_progress, stream_factory=stream_factory,
                                    port_provider=port_provider, controller_stream_factory=controller_stream_factory,
                                    controller_port_provider=controller_port_provider, clock=clock, sleep=sleep)
        result.update(core)
        if core["run"]["outcome"] == "CANCELLED":
            raise BuildCancelled("core run cancelled; no accessory cleanup request")
    except BaseException as exc:
        failure = exc
    finally:
        result["external"] = session.finish(failure)
    return result
