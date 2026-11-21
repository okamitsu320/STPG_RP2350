"""Strict ordinary/segmented execution preview; no I/O at import or planning."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import math
import sys
from pathlib import Path
import time
from typing import Any, Callable

from stpg import constants as c
from stpg.crc32 import crc32
from stpg.client import FailLogEntry
from stpg.pattern import rate_vector_decode_scope, unpack_rate_vector_full
from stpg.protocol import StpgCommand, StpgStatusResponse
from stil.device_precheck import inspect_pattern_requirements, validate_device_compatibility
from stil.segmented import load_manifest

from .artifacts import RunnerError, canonical_json, confined_path, read_json, write_json
from .build import BUNDLE_NAME, _verify_inventory, tool_fingerprint, verify_bundle
from .device import (ByteStream, DeviceConfig, DeviceLease, PortBindingError,
                     SERIAL_READ_TIMEOUT_S, enumerate_ports, open_serial, port_metadata, validate_port)
from .progress import BuildCancelled, CancellationToken, ProgressEvent, Reporter
from .preparation import _current_invocation, _invocation_scope
from .results import write_run_result

MAX_SEGMENTS = 256
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
MAX_POLLS = 300
POLL_INTERVAL_S = 0.2
OBSERVATION_TIMEOUT_S = 60.0
RESPONSE_TIMEOUT_S = 2.0
ADMISSION_TIMEOUT_S = 20.0
# RUN is acknowledged before synchronous firmware startup/replay preparation.
# Only subsequent observation STATUS reads use this longer, finite budget;
# ordinary/preflight/reset checks and payload ACKs retain RESPONSE_TIMEOUT_S.
# This is a host receive policy, not a worst-case firmware latency guarantee.
STATUS_RESPONSE_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class SegmentRunContext:
    """Immutable callback facts, not a stream, mutable plan, or RUN authority."""

    segment_index: int
    manifest_sha256: str
    pattern_sha256: str
    run_args: tuple[int, int, int, int]
    terminal_state: int
    size_bytes: int
    active_pins: int
    tick_hz: int
    events: int
    samples: int
    compares: int


@dataclass(frozen=True)
class RunHooks:
    """Trusted Python callbacks; never deserialized from a project or bundle.

    Both callbacks must return None. They may have external side effects and
    unbounded runtime: this API is not a sandbox or hardware authorization.
    ERROR never invokes after_terminal. Cancellation/failure sends no cleanup.
    """

    before_run: Callable[[SegmentRunContext], None] | None = None
    after_terminal: Callable[[SegmentRunContext, dict[str, Any]], None] | None = None


@dataclass(frozen=True)
class CaptureMetadata:
    """Local evidence link only; no file is opened or permission inferred."""

    plan_sha256: str
    capture_dir: str


def _validate_hooks(hooks: RunHooks | None, metadata: CaptureMetadata | None) -> None:
    if hooks is not None:
        if type(hooks) is not RunHooks or all(callback is None for callback in (hooks.before_run, hooks.after_terminal)):
            raise RunnerError("capture_hook must be RunHooks with at least one callback")
        if any(callback is not None and not callable(callback)
               for callback in (hooks.before_run, hooks.after_terminal)):
            raise RunnerError("hook callbacks must be callable or None")
    if metadata is not None:
        if hooks is None or type(metadata) is not CaptureMetadata:
            raise RunnerError("capture_metadata requires CaptureMetadata and capture_hook")
        digest, directory = metadata.plan_sha256, metadata.capture_dir
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or not isinstance(directory, str) or not directory.strip()
                or directory != directory.strip() or len(directory) > 4096
                or any(ord(char) < 32 for char in directory)):
            raise RunnerError("invalid local capture metadata")


@dataclass(frozen=True)
class SegmentPlan:
    index: int
    path: str
    payload: bytes
    expected: dict[str, Any]
    run_args: tuple[int, int, int, int]
    terminal_state: int
    required_capability: int

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "path": self.path,
                "sha256": hashlib.sha256(self.payload).hexdigest(),
                "expected": deepcopy(self.expected), "run_args": list(self.run_args),
                "terminal_state": self.terminal_state,
                "required_capability": self.required_capability,
                "payload_acks": (len(self.payload) + 4095) // 4096}


@dataclass(frozen=True)
class RunPlan:
    bundle_dir: Path
    manifest_sha256: str
    runtime_profile: str
    storage: int
    completion: str
    segments: tuple[SegmentPlan, ...]
    verification_mode: str = "semantic"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "stpg_runner_run_plan", "schema_version": 1,
                "status": "HOST_ONLY_PLANNED", "device_io_performed": False,
                "physical_execution_authorized": False,
                "manifest_sha256": self.manifest_sha256, "runtime_profile": self.runtime_profile,
                "storage": self.storage, "completion": self.completion,
                "bundle_check_mode": self.verification_mode,
                "segments": [segment.to_dict() for segment in self.segments],
                "observation": {"max_status_per_segment": MAX_POLLS,
                                "interval_s": POLL_INTERVAL_S, "deadline_s": OBSERVATION_TIMEOUT_S,
                                "response_timeout_s": STATUS_RESPONSE_TIMEOUT_S},
                "fail_log_collection": {"when": "validated mismatching DONE/HOLD only",
                                        "max_entries_per_segment": c.STPG_FAIL_LOG_MAX,
                                        "page_entries": c.STPG_FAIL_LOG_READ_PAGE_ENTRIES,
                                        "max_pages_per_segment": 171,
                                        "response_timeout_s": RESPONSE_TIMEOUT_S,
                                        "max_nominal_receive_budget_s": 1026.0,
                                        "extends_hold_residence": True, "retry": False},
                "sample_payload_collection": False}


def _validate_manifest_pin(manifest_sha256: str) -> None:
    if (not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64
            or any(char not in "0123456789abcdef" for char in manifest_sha256)):
        raise RunnerError("execution requires the exact lowercase manifest SHA-256")


def _plan_seal(plan: RunPlan) -> bytes:
    return canonical_json({"root": str(plan.bundle_dir), "plan": plan.to_dict()})


def _reuse_invocation_plan(root: Path, manifest_sha256: str, progress: Reporter) -> RunPlan | None:
    """Only our own sealed snapshot; every reuse rehashes the exact inventory."""
    state = _current_invocation()
    record = state.plans.get((root, manifest_sha256)) if state is not None else None
    if record is None:
        return None
    plan, seal, manifest_bytes, fingerprint = record
    if type(plan) is not RunPlan or _plan_seal(plan) != seal:
        raise RunnerError("shared execution snapshot was modified")
    manifest = read_json(confined_path(root, BUNDLE_NAME), expected_sha256=manifest_sha256)
    if (canonical_json(manifest) != manifest_bytes or tool_fingerprint() != fingerprint
            or manifest["binding"]["tool_sha256"] != fingerprint):
        raise RunnerError("shared execution snapshot binding changed")
    _verify_inventory(root, manifest, manifest_sha256=manifest_sha256, progress=progress, stage="shared_files")
    if _plan_seal(plan) != seal or tool_fingerprint() != fingerprint:
        raise RunnerError("shared execution snapshot changed during inventory verification")
    progress.emit("bundle_check", "done", detail="shared_snapshot: exact inventory rehashed")
    return plan


@rate_vector_decode_scope()
def prepare_run(bundle_dir: Path, *, manifest_sha256: str,
                on_progress: Callable[[ProgressEvent], None] | None = None,
                cancel: CancellationToken | None = None,
                clock: Callable[[], float] = time.monotonic) -> RunPlan:
    """Verify once and snapshot bytes, then rehash the exact inventory. No device I/O."""
    _validate_manifest_pin(manifest_sha256)
    progress = Reporter(on_progress, cancel, clock=clock)

    def forward(event: ProgressEvent) -> None:
        # One enclosing start/done pair; keep inner file counts and heartbeats.
        if event.stage != "bundle_verify" or event.event == "heartbeat":
            progress.emit(event.stage, event.event, completed=event.completed,
                          total=event.total, detail=event.detail)

    root = Path(bundle_dir).resolve()
    shared = _reuse_invocation_plan(root, manifest_sha256, progress)
    if shared is not None:
        return shared
    progress.emit("bundle_verify", "start")
    manifest = read_json(root / BUNDLE_NAME, expected_sha256=manifest_sha256)
    binding = manifest.get("binding")
    if not isinstance(binding, dict):
        raise RunnerError("unverified bundle binding")
    if binding.get("tool_sha256") != tool_fingerprint():
        raise RunnerError("installed compiler/SDK differs from the bundle; build a new bundle")
    verified = verify_bundle(root, manifest_sha256=manifest_sha256,
                             on_progress=forward if on_progress is not None else None,
                             cancel=progress.cancel, reuse=True)
    if verified["status"] != "PASS":
        raise RunnerError(f"unverified bundle: {verified['errors']}")
    progress.emit("bundle_verify", "done")
    project = manifest["binding"]["project"]
    storage = {"sram": c.STORAGE_SRAM, "psram": c.STORAGE_PSRAM}[project["execution"]["storage"]]
    items = manifest["patterns"]
    if not 1 <= len(items) <= MAX_SEGMENTS:
        raise RunnerError("execution preview supports 1..256 segments")
    if sum(item["expected"]["size_bytes"] for item in items) > MAX_SNAPSHOT_BYTES:
        raise RunnerError("execution snapshot exceeds 128 MiB")
    segmented = len(items) > 1
    if segmented and storage != c.STORAGE_PSRAM:
        raise RunnerError("segmented execution requires PSRAM")
    specs = (load_manifest(confined_path(root, manifest["package_manifest"])).segments
             if segmented else ())
    segments = []
    progress.emit("snapshot", "start", total=len(items))
    for index, item in enumerate(items):
        progress.cancel.check()
        expected = deepcopy(item["expected"])
        path = confined_path(root, item["path"])
        payload = path.read_bytes()
        if {"size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()} != manifest["files"][item["path"]]:
            raise RunnerError("pattern changed during execution snapshot")
        requirements = inspect_pattern_requirements(payload)
        mode = expected["mode"]
        if segmented and (mode not in {"raw-event", "rate-vector"}
                          or (mode == "raw-event" and expected["samples"] != 0)):
            raise RunnerError("segmented execution supports raw drive-only or RateVector")
        flags = (c.RUN_FLAG_STREAMING_POC if mode != "raw-event"
                 and (expected["events"] > c.STPG_MAX_EVENT_COUNT or segmented) else 0)
        required_caps = 0
        version = int.from_bytes(payload[4:6], "little")
        if version == c.STPG_PATTERN_VERSION_WIDE_WAIT:
            required_caps |= (c.CAP_WIDE_RATE_VECTOR_32 if mode == "rate-vector" else c.CAP_WIDE_WAIT_32)
        if mode == "rate-vector":
            header, _, _, vectors, loops = unpack_rate_vector_full(payload)
            if loops:
                required_caps |= c.CAP_RATE_LOOP_LIMITED
            if (any(getattr(record, "repeat", 1) > 1 for record in vectors)
                    or any(record.repeat > 1 for loop in loops for record in loop.body_records)):
                required_caps |= c.CAP_RATE_REPEAT
            # Explicit P4 packages need an explicit execution mode; ordinary
            # wait schedules are not P4 packages and must not silently enable it.
            if header.flags & c.STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4:
                raise RunnerError("explicit P4 execution is outside the v1 preview")
        if index < len(items) - 1:
            flags |= c.RUN_FLAG_SEGMENT_END_HOLD
        entry_value = entry_enable = 0
        if index:
            flags |= c.RUN_FLAG_PRESERVE_GPIO_STATE
            entry_value = specs[index].entry_hold_state.drive_value
            entry_enable = specs[index].entry_hold_state.drive_enable
        # Reject structural/capacity incompatibilities before serial contact.
        validate_device_compatibility(
            requirements, device_clock_tick_hz=expected["tick_hz"], device_active_pins=0,
            device_capability=0xFFFFFFFF & ~c.CAP_STREAM_FAIL_STOP, storage_target=storage,
            run_flags=flags, pattern_size_bytes=len(payload), runtime_profile=project["target"]["runtime_profile"],
        )
        segments.append(SegmentPlan(index, item["path"], payload, expected,
                                    (flags, entry_value, entry_enable, 0),
                                    c.STATE_HOLD_AWAITING_NEXT if index < len(items) - 1 else c.STATE_DONE,
                                    required_caps))
        progress.emit("snapshot", "progress", completed=index + 1, total=len(items))
    progress.emit("snapshot", "done", completed=len(items), total=len(items))
    # Detect edits to HOLD metadata/manifest or sources during the snapshot too.
    progress.perform("snapshot_verify", lambda: _verify_inventory(
        root, manifest, manifest_sha256=manifest_sha256, progress=progress, stage="snapshot_files"))
    plan = RunPlan(root, manifest_sha256, project["target"]["runtime_profile"], storage,
                   project["execution"]["completion"], tuple(segments), verified["check_mode"])
    fingerprint = tool_fingerprint()
    if binding["tool_sha256"] != fingerprint:
        raise RunnerError("compiler/SDK changed during execution snapshot")
    state = _current_invocation()
    if state is not None:
        state.plans[(root, manifest_sha256)] = (plan, _plan_seal(plan), canonical_json(manifest), fingerprint)
    return plan


def judge_status(response: StpgStatusResponse, expected: dict[str, Any], *, terminal_state: int) -> str:
    """Pure strict status judge. Identity/sequence/CRC are checked by the wire layer."""
    fields = {"pattern_size": expected["size_bytes"], "active_pins": expected["active_pins"],
              "event_count": expected["events"]}
    if response.rsp != c.RSP_STATUS or any(getattr(response, key) != value for key, value in fields.items()):
        raise RunnerError("terminal observation response/tags/counts mismatch")
    if response.run_progress > expected["events"]:
        raise RunnerError("run progress exceeds expected events")
    if response.state == c.STATE_ERROR:
        if response.error_code == 0:
            raise RunnerError("ERROR with error_code=0")
        return "DEVICE_ERROR"  # remaining result fields are deliberately record-only
    if response.error_code != 0:
        raise RunnerError("non-ERROR response has nonzero error_code")
    if response.state in (c.STATE_RUNNING, c.STATE_RUN_DRAINING):
        return "NONTERMINAL"
    if response.state != terminal_state:
        raise RunnerError("unexpected terminal state")
    if response.run_progress != expected["events"] or response.sample_count != expected["samples"]:
        raise RunnerError("incomplete terminal progress/samples")
    if response.fail_count > expected["compares"]:
        raise RunnerError("fail_count exceeds expected comparisons")
    flags = ((c.STPG_RESULT_FLAG_COMPARE_MISMATCH if response.fail_count else 0)
             | (c.STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW if response.fail_count > c.STPG_FAIL_LOG_MAX else 0))
    if response.result_flags != flags:
        raise RunnerError("terminal flags inconsistent with fail_count")
    return "TERMINAL"


class RunCancelled(RunnerError):
    pass


class _DeviceError(RunnerError):
    pass


class _Wire:
    def __init__(self, stream: ByteStream, config: DeviceConfig, journal: list[dict[str, Any]],
                 check_cancel: Callable[[], None], clock: Callable[[], float]) -> None:
        self.stream, self.config, self.journal = stream, config, journal
        self.check_cancel, self.clock = check_cancel, clock
        self.seq = 0

    def _write(self, payload: bytes, record: dict[str, Any]) -> None:
        self.check_cancel()
        record["write_begin_s"] = self.clock()
        try:
            count = self.stream.write(payload)
            record["written_bytes"] = count
            if type(count) is not int or count != len(payload):
                raise RunnerError("short/invalid write; not resent")
        except BaseException as exc:
            record.setdefault("written_bytes", None)  # a failed write may have sent a prefix
            record["write_error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["write_end_s"] = self.clock()

    def _read(self, record: dict[str, Any], seq: int, timeout: float,
              *, payload_length: int | None = None) -> StpgStatusResponse | bytes:
        if payload_length is not None and not 1 <= payload_length <= 32:
            raise RunnerError("FAIL_LOG payload must be one bounded page")
        size = c.STPG_RSP_SIZE if payload_length is None else payload_length
        raw = bytearray()
        record["read_begin_s"] = self.clock()
        deadline = record["read_begin_s"] + timeout
        record["read_parts"] = []
        max_reads = math.ceil(timeout / SERIAL_READ_TIMEOUT_S) + c.STPG_RSP_SIZE
        record["response_deadline_s"] = deadline
        record["max_read_calls"] = max_reads
        stage = "cancel"
        try:
            # Empty timed slices are not EOF and do not resend the request.
            # The count also bounds a broken/injected clock or immediate-empty backend.
            for _ in range(max_reads):
                stage = "cancel"
                self.check_cancel()
                stage = "deadline"
                if self.clock() >= deadline:
                    raise RunnerError("response deadline exhausted; partial bytes retained")
                stage = "read"
                call = {"begin_s": self.clock(), "size": None}
                record["read_parts"].append(call)
                try:
                    part = self.stream.read(size - len(raw))
                    call["size"] = len(part)
                except BaseException as exc:
                    call["error"] = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    call["end_s"] = self.clock()
                raw.extend(part)
                record["raw_response_hex"] = raw.hex()  # before decode/semantic rejection
                stage = "deadline"
                if call["end_s"] > deadline:
                    raise RunnerError("response deadline exhausted; partial bytes retained")
                if len(raw) >= size:
                    break
            else:
                stage = "read_limit"
                raise RunnerError("response read limit exhausted; partial bytes retained")
            if len(raw) != size:
                raise RunnerError("response length mismatch")
            if payload_length is not None:
                return bytes(raw)
            stage = "decode"
            response = StpgStatusResponse.unpack(bytes(raw))
            record["decoded"] = asdict(response)
            stage = "identity"
            if (response.seq != seq or response.firmware_version != self.config.firmware_version
                    or response.capability != self.config.capability):
                raise RunnerError("response sequence/firmware/capability mismatch")
            return response
        except BaseException as exc:
            record["raw_response_hex"] = raw.hex()
            record["read_failure_stage"] = stage
            record["read_error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["read_end_s"] = self.clock()

    def exchange(self, command: int, args: tuple[int, int, int, int] = (0, 0, 0, 0),
                 *, timeout: float = RESPONSE_TIMEOUT_S) -> StpgStatusResponse:
        self.check_cancel()
        self.seq += 1
        packet = StpgCommand(command, self.seq, *args).pack()
        record = {"kind": "command", "command": command, "seq": self.seq,
                  "args": list(args), "planned_write_hex": packet.hex()}
        self.journal.append(record)
        self._write(packet, record)
        return self._read(record, self.seq, timeout)

    def payload(self, data: bytes, seq: int, offset: int) -> StpgStatusResponse:
        record = {"kind": "payload", "seq": seq, "offset": offset, "size": len(data),
                  "planned_write_sha256": hashlib.sha256(data).hexdigest()}
        self.journal.append(record)
        self._write(data, record)
        return self._read(record, seq, RESPONSE_TIMEOUT_S)

    def fail_data(self, seq: int, length: int) -> tuple[bytes, StpgStatusResponse]:
        record = {"kind": "fail_log_payload", "seq": seq, "size": length}
        self.journal.append(record)
        payload = self._read(record, seq, RESPONSE_TIMEOUT_S, payload_length=length)
        trailer = {"kind": "fail_log_trailer", "seq": seq}
        self.journal.append(trailer)
        return payload, self._read(trailer, seq, RESPONSE_TIMEOUT_S)


class _Session:
    def __init__(self, wire: _Wire, plan: RunPlan, report: dict[str, Any], checkpoint: Callable[[], None],
                 emit: Callable[..., None], clock: Callable[[], float], sleep: Callable[[float], None],
                 hooks: RunHooks | None) -> None:
        self.wire, self.plan, self.report = wire, plan, report
        self.checkpoint, self.emit, self.clock, self.sleep = checkpoint, emit, clock, sleep
        self.hooks = hooks

    def _invoke_hook(self, phase: str, segment: SegmentPlan,
                     terminal: StpgStatusResponse | None = None) -> None:
        callback = getattr(self.hooks, phase) if self.hooks is not None else None
        if callback is None:
            return
        context = SegmentRunContext(
            segment.index, self.plan.manifest_sha256, hashlib.sha256(segment.payload).hexdigest(),
            segment.run_args, segment.terminal_state, segment.expected["size_bytes"],
            segment.expected["active_pins"], segment.expected["tick_hz"], segment.expected["events"],
            segment.expected["samples"], segment.expected["compares"],
        )
        record = {"phase": phase, "segment_index": segment.index,
                  "begin_s": self.clock(), "outcome": "PENDING"}
        self.report["capture"]["hook_events"].append(record)
        try:
            self.wire.check_cancel()
            result = callback(context) if terminal is None else callback(context, asdict(terminal))
            if result is not None:
                raise RunnerError("hook callback must return None")
            self.wire.check_cancel()
            record["outcome"] = "COMPLETED"
        except BaseException as exc:
            record["outcome"] = "REJECT"
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["end_s"] = self.clock()

    def _expect(self, response: StpgStatusResponse, code: int, states: tuple[int, ...],
                **fields: int) -> StpgStatusResponse:
        if response.rsp != code or response.state not in states or response.error_code != 0:
            raise RunnerError("response code/state/error mismatch")
        if any(getattr(response, key) != value for key, value in fields.items()):
            raise RunnerError(f"response fields mismatch: {tuple(fields)}")
        return response

    @staticmethod
    def _tags(segment: SegmentPlan) -> dict[str, int]:
        return {"active_pins": segment.expected["active_pins"],
                "pattern_size": segment.expected["size_bytes"], "event_count": segment.expected["events"]}

    def _reset(self, release: bool) -> None:
        if release:
            self.report["release_command_attempted"] = True
        response = self.wire.exchange(c.CMD_RESET, (1 if release else 0, 0, 0, 0))
        self._expect(response, c.RSP_OK, (c.STATE_IDLE,), active_pins=0, pattern_size=0,
                     event_count=0, run_progress=0, sample_count=0, fail_count=0, result_flags=0)
        if release:
            self.report["release_acknowledged"] = True
        response = self.wire.exchange(c.CMD_GET_STATUS)
        self._expect(response, c.RSP_STATUS, (c.STATE_IDLE,), active_pins=0, pattern_size=0,
                     event_count=0, run_progress=0, sample_count=0, fail_count=0, result_flags=0)

    def _upload(self, segment: SegmentPlan) -> None:
        payload = segment.payload
        total_crc = crc32(payload)
        response = self.wire.exchange(c.CMD_UPLOAD_BEGIN, (self.plan.storage, len(payload), total_crc, 0))
        self._expect(response, c.RSP_OK, (c.STATE_RECEIVING,), active_pins=0, pattern_size=0,
                     event_count=0, run_progress=0, sample_count=0, fail_count=0, result_flags=0)
        for offset in range(0, len(payload), c.STPG_UPLOAD_CHUNK_SIZE):
            chunk = payload[offset:offset + c.STPG_UPLOAD_CHUNK_SIZE]
            response = self.wire.exchange(c.CMD_UPLOAD_DATA, (offset, len(chunk), crc32(chunk), 0))
            self._expect(response, c.RSP_READY_FOR_PAYLOAD, (c.STATE_RECEIVING,), active_pins=0,
                         pattern_size=0, event_count=0, run_progress=offset,
                         sample_count=0, fail_count=0, result_flags=0)
            response = self.wire.payload(chunk, response.seq, offset)
            self._expect(response, c.RSP_DATA_ACK, (c.STATE_RECEIVING,), active_pins=0,
                         pattern_size=0, event_count=0, run_progress=offset + len(chunk),
                         sample_count=0, fail_count=0, result_flags=0)
            self.emit("upload", "progress", completed=offset + len(chunk), total=len(payload))
        response = self.wire.exchange(c.CMD_UPLOAD_END, (len(payload), total_crc, 0, 0),
                                      timeout=ADMISSION_TIMEOUT_S)
        self._expect(response, c.RSP_OK, (c.STATE_HOLD_AWAITING_NEXT if segment.index else c.STATE_READY,),
                     **self._tags(segment), run_progress=0, sample_count=0, fail_count=0, result_flags=0)
        self.emit("upload", "done", completed=len(payload), total=len(payload),
                  detail=f"segment={segment.index}; UPLOAD_END accepted")

    def _start(self, segment: SegmentPlan) -> None:
        response = self.wire.exchange(c.CMD_RUN, segment.run_args, timeout=ADMISSION_TIMEOUT_S)
        self._expect(response, c.RSP_OK, (c.STATE_RUNNING,), **self._tags(segment),
                     run_progress=0, sample_count=0, fail_count=0, result_flags=0)
        self.emit("run", "accepted", detail=f"segment={segment.index}; RUN accepted; waiting for STATUS; "
                  "no waveform or terminal completion observed")

    def _observe(self, segment: SegmentPlan) -> StpgStatusResponse:
        deadline = self.clock() + OBSERVATION_TIMEOUT_S
        for poll in range(MAX_POLLS):
            self.wire.check_cancel()
            if self.clock() >= deadline:
                raise RunnerError("terminal observation deadline exhausted")
            self.sleep(POLL_INTERVAL_S)
            if self.clock() >= deadline:
                raise RunnerError("terminal observation deadline exhausted after sleep")
            response = self.wire.exchange(c.CMD_GET_STATUS, timeout=STATUS_RESPONSE_TIMEOUT_S)
            outcome = judge_status(response, segment.expected, terminal_state=segment.terminal_state)
            if outcome != "NONTERMINAL":
                return response
            self.emit("observe", "progress", completed=response.run_progress,
                      total=segment.expected["events"], detail=f"segment={segment.index}; poll={poll + 1}")
        raise RunnerError("terminal observation poll limit exhausted")

    def _fail_log(self, segment: SegmentPlan, terminal: StpgStatusResponse, record: dict[str, Any]) -> None:
        log = record["fail_log"]
        if terminal.fail_count == 0:
            return
        count = min(terminal.fail_count, c.STPG_FAIL_LOG_MAX)
        try:
            self.emit("fail_log", "start", completed=0, total=count, detail=f"segment={segment.index}; terminal mismatch; reading retained entries")
            for offset in range(0, count, c.STPG_FAIL_LOG_READ_PAGE_ENTRIES):
                page_count = min(count - offset, c.STPG_FAIL_LOG_READ_PAGE_ENTRIES)
                length = page_count * c.STPG_FAIL_LOG_ENTRY_SIZE
                ready = self.wire.exchange(c.CMD_READ_FAIL_LOG, (offset, page_count, 0, 0))
                tags = {"active_pins": terminal.active_pins, "pattern_size": length,
                        "run_progress": offset, "sample_count": terminal.sample_count,
                        "fail_count": terminal.fail_count, "result_flags": c.STPG_RESULT_TYPE_FAIL_LOG}
                self._expect(ready, c.RSP_READY_FOR_READ, (terminal.state,), event_count=page_count, **tags)
                payload, trailer = self.wire.fail_data(ready.seq, length)
                self._expect(trailer, c.RSP_DATA_END, (terminal.state,), event_count=crc32(payload), **tags)
                entries = FailLogEntry.unpack_many(payload)
                previous = log["entries"][-1]["event_index"] if log["entries"] else -1
                for entry in entries:
                    logical_mask = (1 << segment.expected["active_pins"]) - 1
                    if (not previous < entry.event_index < segment.expected["events"]
                            or (entry.actual | entry.expected | entry.mask) & ~logical_mask
                            or not (entry.actual ^ entry.expected) & entry.mask):
                        raise RunnerError("invalid FAIL_LOG entry range/order/bits")
                    previous = entry.event_index
                log["entries"].extend(asdict(entry) for entry in entries)
                log["entries_read"] = len(log["entries"])
                self.checkpoint()  # preserve each fully validated page before the next request
                self.emit("fail_log", "progress", completed=log["entries_read"], total=count,
                          detail=f"segment={segment.index}; validated retained entries, not waveform progress")
            log["status"] = "READ"
        except BaseException as exc:
            log["status"] = "READ_ERROR"
            log["error"] = f"{type(exc).__name__}: {exc}"
            raise
        self.emit("fail_log", "done", completed=log["entries_read"], total=count,
                  detail=f"segment={segment.index}; retained fail log received")

    def execute(self) -> None:
        info = self.wire.exchange(c.CMD_INFO)
        self._expect(info, c.RSP_INFO, (c.STATE_IDLE, c.STATE_DONE))
        clock = self.wire.exchange(c.CMD_GET_CLOCK_INFO)
        self._expect(clock, c.RSP_CLOCK_INFO, (info.state,))
        initial = self.wire.exchange(c.CMD_GET_STATUS)
        self._expect(initial, c.RSP_STATUS, (info.state,))
        for segment in self.plan.segments:
            if info.capability & segment.required_capability != segment.required_capability:
                raise RunnerError("device lacks required wide/Loop capabilities")
            if (segment.expected["mode"] == "rate-vector"
                    and (info.firmware_version >> 24) < int.from_bytes(segment.payload[20:24], "little")):
                raise RunnerError("device maximum rate table smaller than the pattern")
            validate_device_compatibility(
                inspect_pattern_requirements(segment.payload), device_clock_tick_hz=clock.pattern_size,
                device_active_pins=initial.active_pins, device_capability=info.capability,
                storage_target=self.plan.storage, run_flags=segment.run_args[0],
                pattern_size_bytes=len(segment.payload), runtime_profile=self.plan.runtime_profile,
            )
        self.report["preflight_responses"] = {"info": asdict(info), "clock": asdict(clock), "status": asdict(initial)}
        self.checkpoint()
        self._reset(False)
        self.checkpoint()
        for segment in self.plan.segments:
            self.emit("upload", "start", completed=0, total=len(segment.payload))
            self._upload(segment)
            self.checkpoint()
            self._invoke_hook("before_run", segment)
            self._start(segment)
            terminal = self._observe(segment)
            record = {"index": segment.index, "expected": deepcopy(segment.expected),
                                             "terminal": asdict(terminal),
                      "outcome": "DEVICE_ERROR" if terminal.state == c.STATE_ERROR else "COMPLETED",
                      "comparison": ("UNOBSERVED" if terminal.state == c.STATE_ERROR else
                                     "MISMATCH" if terminal.fail_count else
                                     "MATCH" if segment.expected["compares"] else "NOT_APPLICABLE"),
                      "fail_log": {"status": ("NOT_COLLECTED_ERROR" if terminal.state == c.STATE_ERROR else
                                              "PENDING" if terminal.fail_count else "NOT_NEEDED"),
                                   "reported_fail_count": terminal.fail_count, "entries_read": 0,
                                   "overflow": bool(terminal.result_flags & c.STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW),
                                   "entries": []}}
            self.report["segments"].append(record)
            if record["comparison"] == "MISMATCH":
                self.report["comparison"] = "MISMATCH"
            self.checkpoint()  # persist the terminal before another upload resets results
            if terminal.state == c.STATE_ERROR:
                raise _DeviceError(f"terminal device error {terminal.error_code}; no continuation")
            self._invoke_hook("after_terminal", segment, terminal)
            if self.hooks is not None and self.hooks.after_terminal is not None:
                self.checkpoint()
            self._fail_log(segment, terminal, record)
            self.checkpoint()
            self.emit("segment", "done", completed=segment.index + 1, total=len(self.plan.segments))
        if self.plan.completion == "release":
            self._reset(True)
            response = self.wire.exchange(c.CMD_INFO)
            self._expect(response, c.RSP_INFO, (c.STATE_IDLE,), active_pins=0, pattern_size=0,
                         event_count=0, run_progress=0, sample_count=0, fail_count=0, result_flags=0)
            self.report["release_sequence_complete"] = True


def public_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Small GUI-safe summary, intentionally excluding raw/local device evidence."""
    return {key: deepcopy(report[key]) for key in (
        "kind", "schema_version", "outcome", "comparison", "completed_segments", "planned_segments",
        "firmware_validation_pass", "sample_payload_collection", "release_command_attempted",
        "release_acknowledged", "release_sequence_complete", "electrical_release_verified", "errors",
        "result_persisted",
        "stream_open_attempted", "stream_opened", "close_attempts", "close_successes",
    )}


@_invocation_scope
def run_bundle(bundle_dir: Path, *, manifest_sha256: str, device: DeviceConfig,
               output_dir: Path, confirm_run: bool = False,
               on_progress: Callable[[ProgressEvent], None] | None = None,
               cancel: CancellationToken | None = None,
               capture_hook: RunHooks | None = None,
               capture_metadata: CaptureMetadata | None = None,
               stream_factory: Callable[[DeviceConfig], ByteStream] | None = None,
               port_provider: Callable[[], list[Any]] | None = None,
               clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """One invocation, no retries. Injected factories are trusted host-test code.

    Real use is an unvalidated execution preview. This intent flag is not the
    repository's exact candidate-specific direct physical-test approval.
    """
    if confirm_run is not True:
        raise RunnerError("explicit confirm_run=True is required; no device opened")
    _validate_hooks(capture_hook, capture_metadata)
    device = DeviceConfig.from_dict(device.to_dict())
    from .execution_gate import check_execution_contract
    gate = check_execution_contract()
    if gate["status"] != "PASS":
        raise RunnerError(f"execution structural gate rejected: {gate['errors']}")
    _validate_manifest_pin(manifest_sha256)
    root = Path(bundle_dir).resolve()
    output = Path(output_dir).resolve()
    if output == root or root in output.parents:
        raise RunnerError("results must not modify the immutable bundle")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()  # exclusive one-shot reservation, including after a crash
    report: dict[str, Any] = {
        "kind": "stpg_runner_run_result", "schema_version": 1, "outcome": "PENDING",
        "comparison": "UNOBSERVED", "planned_segments": 0, "completed_segments": 0,
        "firmware_validation_pass": False, "sample_payload_collection": False,
        "release_command_attempted": False, "release_acknowledged": False,
        "release_sequence_complete": False, "electrical_release_verified": False,
        "operator_confirmed": True, "transport_kind": "serial" if stream_factory is None else "injected",
        "plan": None, "device": device.to_dict(), "gate": gate, "segments": [], "port_checks": [],
        "stream_open_attempted": False, "stream_opened": False, "close_attempts": 0,
        "close_successes": 0, "errors": [], "transactions": [], "lifecycle": [],
        "result_persisted": False,
        "serial_contract": {"platform": sys.platform, "read_timeout_s": SERIAL_READ_TIMEOUT_S,
                            "write_timeout_s": 2.0, "dsrdtr": sys.platform != "win32",
                            "rtscts": False, "xonxoff": False, "initial_dtr": True, "cached_rts": True,
                            "applies_to": "production opener" if stream_factory is None else "injected backend not enforced"},
    }
    if capture_hook is not None:
        report["capture"] = {"metadata": asdict(capture_metadata) if capture_metadata else None,
                             "hook_events": [], "callback_runtime_bounded": False}
    token = cancel or CancellationToken()
    started = clock()
    plan = None

    def check_cancel() -> None:
        if token.cancelled:
            raise RunCancelled("host requests cancelled; no STOP/release or CPU/output-state inference")

    def checkpoint() -> None:
        write_run_result(output, report)

    def emit(stage: str, event: str, **fields: Any) -> None:
        check_cancel()
        if on_progress is not None:
            on_progress(ProgressEvent(stage, event, clock() - started, **fields))
        check_cancel()

    def host_progress(event: ProgressEvent) -> None:
        emit(event.stage, event.event, completed=event.completed, total=event.total, detail=event.detail)

    def check_port(phase: str) -> None:
        emit("port_check", "start", detail=phase)
        record = {"phase": phase, "begin_s": clock(), "status": "PENDING", "ports": None}
        report["port_checks"].append(record)
        checkpoint()
        try:
            listed = list((port_provider or enumerate_ports)())
            record["ports"] = port_metadata(listed)
            metadata = validate_port(device, listed)
            report["port_metadata"] = metadata
            record.update(status="PASS", selected=metadata)
        except PortBindingError as exc:
            record.update(exc.details)
            raise
        except BaseException as exc:
            record.update(status="REJECT", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            record["end_s"] = clock()
        check_cancel()
        checkpoint()
        emit("port_check", "done", detail=phase)

    stream = None
    try:
        checkpoint()
        emit("workspace", "reserved", detail=str(output))
        with DeviceLease(device):
            check_port("before_bundle_verify")
            plan = prepare_run(root, manifest_sha256=manifest_sha256, on_progress=host_progress,
                               cancel=token, clock=clock)
            report["plan"] = plan.to_dict()
            report["planned_segments"] = len(plan.segments)
            checkpoint()
            check_port("before_open")
            emit("device_preflight", "start")
            check_cancel()
            checkpoint()
            if _reuse_invocation_plan(root, manifest_sha256, Reporter(None, token, clock=clock)) is not plan:
                raise RunnerError("missing owned execution snapshot before open")
            report["stream_open_attempted"] = True
            report["lifecycle"].append({"event": "open.begin", "time_s": clock()})
            try:
                stream = (stream_factory or open_serial)(device)
                report["stream_opened"] = True
                report["lifecycle"].append({"event": "open.end", "time_s": clock()})
                wire = _Wire(stream, device, report["transactions"], check_cancel, clock)
                _Session(wire, plan, report, checkpoint, emit, clock, sleep, capture_hook).execute()
                check_cancel()
                report["outcome"] = "COMPLETED"
            except BaseException as exc:
                report["execution_error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                if stream is not None:
                    report["close_attempts"] += 1
                    report["lifecycle"].append({"event": "close.begin", "time_s": clock()})
                    try:
                        stream.close()
                        report["close_successes"] += 1
                    except BaseException as exc:
                        report["errors"].append(f"close: {type(exc).__name__}: {exc}")
                        raise
                    finally:
                        report["lifecycle"].append({"event": "close.end", "time_s": clock()})
    except _DeviceError as exc:
        report["outcome"] = "DEVICE_ERROR"
        report["errors"].append(str(exc))
    except (RunCancelled, BuildCancelled, KeyboardInterrupt) as exc:
        report["outcome"] = "CANCELLED"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    except BaseException as exc:
        report["outcome"] = "REJECT"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    if report["close_attempts"] != report["close_successes"]:
        report["outcome"] = "REJECT"
    report["release_command_attempted"] = any(
        record.get("command") == c.CMD_RESET and record.get("args") == [1, 0, 0, 0]
        and "write_begin_s" in record for record in report["transactions"])
    report["completed_segments"] = sum(item["outcome"] == "COMPLETED" for item in report["segments"])
    if report["outcome"] == "COMPLETED":
        compares = sum(segment.expected["compares"] for segment in plan.segments)
        fails = sum(item["terminal"]["fail_count"] for item in report["segments"])
        report["comparison"] = "MISMATCH" if fails else "MATCH" if compares else "NOT_APPLICABLE"
    try:
        report["result_persisted"] = True
        checkpoint()
    except BaseException as exc:
        report["result_persisted"] = False
        report["outcome"] = "REJECT"
        if report["comparison"] != "MISMATCH":
            report["comparison"] = "UNOBSERVED"
        report["errors"].append(f"final checkpoint: {type(exc).__name__}: {exc}")
        # Return the in-memory prefix; no further device operation or retry.
    return report
