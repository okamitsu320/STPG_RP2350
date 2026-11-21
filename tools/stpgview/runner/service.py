"""GUI runner: the HTTP-independent half of the runner API.

Everything goes through the shared SDK (``stpg_runner`` for projects, builds,
bundles, plans, execution, the optional GPIO controller, manual reset and the
optional external accessory process). This module adds only GUI concerns:
background jobs, local binding storage and one-time frozen confirmations. It
contains no analyzer code and imports no accessory package: an accessory is a
separately installed program that the SDK starts for one run.

Accessories are optional. The STPG alone is the main path; a GPIO controller or
external accessory is touched only when the user selects it for one confirmed
invocation (no enumeration, query, launch or automatic reset otherwise). A
selected accessory that is missing or invalid rejects; nothing falls back
silently.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from importlib import metadata
import platform
import secrets
import sys
import tempfile
import threading
import time
from typing import Any

from stil.json_artifacts import read_json_artifact, resolve_json_artifact
from stil.source_artifacts import decoded_source_identity, open_source_binary
from stil.runtime_capacity import get_runtime_capacity_profile
from stpg import constants as c
from stpg_runner import (BuildCancelled, BuildResult, ControllerConfig, DeviceConfig, ExternalAccessorySpec,
                         ExternalDeviceBinding, accessory_exit, build_project, identify_controller, init_project,
                         load_external_accessory, load_project, manual_control, physical_identity_key,
                         prepare_external_accessory, prepare_run, public_summary, run_with_accessories,
                         run_with_external_accessory, sdk_info, verify_bundle)
from stpg_runner.accessories import validate_control_plan
from stpg_runner.artifacts import RunnerError, canonical_json, confined_path, identity, read_json, write_json
from stpg_runner.build import BUNDLE_NAME
from stpg_runner.controller import STATES as GPIO_STATES, SUPPORTED_PINS, ensure_separate
import stpg_runner.device as sdk_device
from stpg_runner.device import validate_port
from stpg_runner.project import CUSTOM_WAIT_PRESET, WAIT_MODES, WAIT_PRESETS, Project, project_template
from stpg_runner.segmentation import capacity_reasons, source_rejection
from stpg_runner.source_storage import bundle_source_paths
from stpg_runner.results import describe_result, read_run_result, run_result_path
from stpg_runner.rebinding import rebind_bundle
from stpg_runner.build import tool_fingerprint

from ..config_location import OPTION, ConfigLocation, describe_config_dir, state_text
from .inputs import INPUT_FORMS, PREPROCESS_RECORD, check_toolchain_form, names, preprocess_ate_input
from .jobs import Job, RunnerJobs
from .description import (bundle_candidates, describe_text, fail_log_stop, fail_log_summary, find_bundle,
                          run_folder_of)
from .simulated import (SIMULATED_CONTROLLER, SIMULATED_DEVICE, SimulatedController, SimulatedStpg, controller_ports,
                        simulated_ports)
from .store import BindingStore

SIMULATED = "simulated"
CONFIRM_TTL_S = 600
SHA256 = re.compile(r"[0-9a-f]{64}")
ACKNOWLEDGEMENTS = {
    "compatible_hardware": "Board, pin map, Flash/PSRAM and clock match this bundle's target",
    "safe_loads": "DUT/loads are isolated and safe for the planned drive levels (the SDK cannot check this)",
    "preview_limits": "This is the unvalidated execution preview: STATUS counters and, after compare failures, the "
                      "STPG's retained fail log only (no samples); no STOP/release on cancel",
    # shown only for a plan with compares (see run_acknowledgements); its own words, not the ones above
    "fail_log_hold": "After a segment that ends with compare failures, the SDK reads that segment's retained fail log "
                     "before anything else: the levels stay driven that much longer before the next segment (HOLD) or "
                     "the release",
}
FAIL_LOG_ACK = "fail_log_hold"


def run_acknowledgements(plan: dict) -> dict:
    """The confirmation boxes for one plan: the fail-log box only when some segment has compares."""

    compares = any(segment["expected"]["compares"] for segment in plan["segments"])
    return {key: text for key, text in ACKNOWLEDGEMENTS.items() if key != FAIL_LOG_ACK or compares}


CONTROL_PLAN_KIND = "stpg_gpio_action_plan"
PIN_ROLES = {0: "STPG RUN (manual reset only)", 1: "LA RUN (manual reset only)", 2: "current bench FPGA enable"}
EXIT_TEXT = {
    0: "completed; every selected accessory completed and its evidence was saved (not a DUT PASS)",
    2: "error, or incomplete / unsaved evidence on some axis",
    3: "completed with a comparison mismatch (external data adopted, or no external accessory selected)",
    4: "the external accessory completed, but reported its data as not adopted",
    130: "cancelled",
}
# The analyzer's RUN line (controller GPIO1) is reset with the analyzer's USB identity as the leased target. Analyzer
# bindings are written by the separately installed logic-analyzer tool; the core reads only their USB identity.
SIMULATED_LA_TARGET = ExternalDeviceBinding.from_dict({
    "kind": "stpg_capture_analyzer", "schema_version": 1, "port": "SIMULATED-LA", "vid": 0x2E8A, "pid": 0x000A,
    "serial_number": "SIMULATED-LA-0001"})
# A project made by the separate logic-analyzer tool for a marker pattern carries this record; the core only reads
# that it exists (its settings belong to that tool).
DERIVED_PROJECT_FILE = "marker.derivation.json"
# The separately distributed logic-analyzer tool, named only to guide the operator (see accessory_tool).
LA_DISTRIBUTION = "stpg-rp2350-la"
LA_COMMAND = "stpg-la"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def rejection_text(report: dict) -> str:
    """The SDK's ``source_rejection`` of a retained analysis, also for one without backend candidates.

    A structure error (an unknown WaveformTable, no mapped signal) stops the analysis before the backends
    are tried and leaves ``capability`` null, which the SDK's renderer does not handle. The reasons are the same
    error anomalies.
    """

    return source_rejection({**report, "capability": report.get("capability") or {}})


def _endpoint(name: str) -> str:
    """A port name as the OS resolves it (symlinks on POSIX, case on Windows), for comparisons only."""

    return os.path.realpath(name) if os.name != "nt" else name.casefold()


def port_mismatch(device: DeviceConfig, ports: list[Any]) -> str | None:
    """Why the SDK's preflight would refuse this port list for ``device``; None when it accepts it.

    The rule is the SDK's own ``validate_port`` (exact port, unique USB identity, matching metadata). The SDK
    names the case too, with a code and JSON lists; this says the same in plain words, before the GUI's
    confirmation. Metadata only.
    """

    try:
        validate_port(device, ports)
        return None
    except RunnerError as exc:
        rule = str(exc)

    def usb(port: Any) -> str:
        if port.vid is None:
            return f"{port.device} (no USB metadata)"
        return f"{port.device} ({port.vid:04x}:{port.pid or 0:04x} serial {port.serial_number or '-'})"

    listed = ", ".join(usb(port) for port in ports) or "none"
    if any(not isinstance(getattr(port, "device", None), str) or not port.device for port in ports):
        return f"the USB port list has an entry without a port name. Ports listed now: {listed}"

    expected = f"{device.vid:04x}:{device.pid:04x} serial {device.serial_number}"
    identity = (device.vid, device.pid, device.serial_number)
    selected = [port for port in ports if _endpoint(port.device) == _endpoint(device.port)]
    owners = [port for port in ports if (port.vid, port.pid, port.serial_number) == identity]
    names = " and ".join(sorted({port.device for port in owners}))
    if not selected:
        reason = (f"{device.port} is not in the USB port list; the binding's USB identity ({expected}) is on "
                  f"{names} now, so the port name may have changed" if owners else
                  f"{device.port} is not in the USB port list and no port has the binding's USB identity "
                  f"({expected}): check that the STPG is connected and runs its firmware")
    elif len({port.device for port in selected}) != len(selected):
        reason = f"{device.port} is listed more than once"
    elif any((port.vid, port.pid, port.serial_number) != identity for port in selected):
        port = next(port for port in selected if (port.vid, port.pid, port.serial_number) != identity)
        reason = f"{device.port} is listed as {usb(port)}, but the binding expects {expected}"
        if ((port.vid, port.pid) == identity[:2] and isinstance(port.serial_number, str)
                and port.serial_number.casefold() == device.serial_number.casefold()):
            reason += " (only the letter case of the serial differs)"
    elif len({_endpoint(port.device) for port in owners}) > 1:
        reason = f"the binding's USB identity ({expected}) is on {names}; the SDK refuses an ambiguous identity"
    else:
        reason = rule
    return f"{reason}. Ports listed now: {listed}"


EXCHANGES_SHOWN = 6
_CMD_NAMES = {value: name[4:] for name, value in vars(c).items() if name.startswith("CMD_") and type(value) is int}
_RSP_NAMES = {value: name[4:] for name, value in vars(c).items() if name.startswith("RSP_") and type(value) is int}
_STATE_NAMES = {value: name[6:] for name, value in vars(c).items()
                if name.startswith("STATE_") and type(value) is int}
# The firmware's own error codes (a STATUS answer's error_code), named as in stpg_error.h.
_ERR_NAMES = {value: name for name, value in vars(c).items() if name.startswith("ERR_") and type(value) is int}
# Journal records that are only read (no request is written for them): a FAIL_LOG page and its end frame.
_RECEIVE_ONLY = {"fail_log_payload": "FAIL_LOG page data", "fail_log_trailer": "FAIL_LOG page end"}
# Errors of the serial link itself, as the SDK's journal records them (pyserial raises SerialException, an OSError,
# also for a Windows driver timeout); a wrong or semantic answer is not one of them.
_LINK_ERRORS = re.compile(r"^(SerialException|SerialTimeoutException|OSError|TimeoutError|PermissionError):"
                          r"|^RunnerError: (response timeout/EOF|response deadline exhausted|response read limit exhausted|short/invalid write)")


def device_exchanges(report: dict) -> dict | None:
    """The last STPG exchanges of a run, from the SDK's local transaction journal in ``run.json``.

    Only command names, byte counts, host times, response codes and errors: no arguments, CRCs or payload hashes.
    ``link_lost`` is a legacy name for an I/O failure, not physical disconnect proof.
    Write attempt/completion are separate; neither proves target consumption or execution.
    """

    records = [t for t in report.get("transactions") or [] if isinstance(t, dict)]
    if not records:
        return None

    def number(value: Any) -> float | None:
        return float(value) if type(value) in (int, float) else None

    def write_completed(record: dict) -> bool:
        if record.get("kind") in _RECEIVE_ONLY:
            # a FAIL_LOG page or its end frame is only read: what was written is its READ_FAIL_LOG request
            request = next((item for item in reversed(records) if item.get("kind") == "command"
                            and item.get("seq") == record.get("seq")), None)
            return request is not None and write_completed(request)
        expected = record.get("size") if record.get("kind") == "payload" else c.STPG_CMD_SIZE
        count = record.get("written_bytes")
        return type(count) is int and count == expected and not record.get("write_error")

    rows = []
    for record in records[-EXCHANGES_SHOWN:]:
        command, decoded = record.get("command"), record.get("decoded") or {}
        begin = number(record.get("write_begin_s", record.get("read_begin_s")))
        end = number(record.get("read_end_s", record.get("write_end_s")))
        answer = None
        if decoded:
            answer = (f"{_RSP_NAMES.get(decoded.get('rsp'), decoded.get('rsp'))} · "
                      f"{_STATE_NAMES.get(decoded.get('state'), decoded.get('state'))}")
        raw = record.get("raw_response_hex")
        try:
            response_bytes = len(bytes.fromhex(raw)) if isinstance(raw, str) else 0
        except ValueError:
            response_bytes = None
        if record.get("kind") == "fail_log_payload" and not record.get("read_error") and response_bytes:
            answer = f"{response_bytes} bytes of retained fail-log entries"
        rows.append({
            "seq": record.get("seq"),
            "request": _RECEIVE_ONLY.get(record.get("kind")) or (
                "payload" if record.get("kind") == "payload"
                else _CMD_NAMES.get(command, f"0x{command:04x}" if type(command) is int else "?")),
            "sent_bytes": None if record.get("kind") in _RECEIVE_ONLY else record.get("written_bytes"),
            "answer": answer,
            "took_s": end - begin if begin is not None and end is not None else None,
            "error": record.get("write_error") or record.get("read_error"),
            "response_bytes": response_bytes,
            "failure_stage": "write" if record.get("write_error") else record.get("read_failure_stage"),
        })
    events = {item.get("event"): number(item.get("time_s"))
              for item in report.get("lifecycle") or [] if isinstance(item, dict)}
    close = events.get("close.begin"), events.get("close.end")
    last_error = rows[-1]["error"]
    return {"total": len(records), "rows": rows,
            "link_lost": isinstance(last_error, str) and bool(_LINK_ERRORS.match(last_error)),
            "run_write_attempted": any(t.get("command") == c.CMD_RUN and "write_begin_s" in t for t in records),
            "run_write_completed": any(t.get("command") == c.CMD_RUN and write_completed(t) for t in records),
            "run_sent": any(t.get("command") == c.CMD_RUN and write_completed(t) for t in records),  # legacy alias
            "last_write_completed": write_completed(records[-1]),
            "close_s": close[1] - close[0] if None not in close else None}


_START_CHECK = (c.CMD_INFO, c.CMD_GET_CLOCK_INFO, c.CMD_GET_STATUS)
IDENTITY_LINES_SHOWN = 8
LINE_SHOWN = 200


def _bounded(value: Any) -> str:
    text = str(value)
    return text if len(text) <= LINE_SHOWN else text[:LINE_SHOWN] + "…"


def upload_without_run(report: dict) -> dict | None:
    """A finished rejected/cancelled record ending in accepted UPLOAD_END; else None.

    A PENDING checkpoint can precede a RUN sent after that checkpoint; do not
    interpret it as a finished before-RUN stop. Even a finished record describes
    historical replies, not current device or pin state, nor why it stopped.
    """

    if report.get("outcome") not in ("REJECT", "CANCELLED"):
        return None
    records = report.get("transactions") or []
    if not isinstance(records, list) or any(not isinstance(t, dict) for t in records):
        return None
    last = records[-1] if records else None
    if (last is None or last.get("kind") != "command" or last.get("command") != c.CMD_UPLOAD_END
            or last.get("write_error") or last.get("read_error")):
        return None
    decoded = last.get("decoded")
    if (not isinstance(decoded, dict) or decoded.get("rsp") != c.RSP_OK or decoded.get("error_code") != 0
            or decoded.get("state") not in (c.STATE_READY, c.STATE_HOLD_AWAITING_NEXT)):
        return None
    return {"segment": report.get("completed_segments"), "planned_segments": report.get("planned_segments"),
            "state": _STATE_NAMES[decoded["state"]]}


def start_check_refusal(report: dict) -> dict | None:
    """Where the SDK's start check stopped a run, read from ``run.json``; None when the run got past it.

    The start check is the run's first exchanges (INFO, then clock information and STATUS), before any upload. A run
    starts only when INFO reports IDLE or DONE and the next two answers keep that state. This only reads what was
    recorded: it sends nothing, and does not change what the SDK decided.
    """

    if report.get("outcome") != "REJECT":
        return None
    records = [t for t in report.get("transactions") or [] if isinstance(t, dict)]
    if not records or [t.get("command") for t in records] != list(_START_CHECK[:len(records)]):
        return None
    last, first = records[-1], records[0].get("decoded")
    decoded = last.get("decoded")
    if not isinstance(decoded, dict) or not isinstance(first, dict) or last.get("write_error") or last.get("read_error"):
        return None  # a transport or identity failure, not the state check
    allowed = ([c.STATE_IDLE, c.STATE_DONE] if last.get("command") == c.CMD_INFO else [first.get("state")])
    expected_rsp = {c.CMD_INFO: c.RSP_INFO, c.CMD_GET_CLOCK_INFO: c.RSP_CLOCK_INFO, c.CMD_GET_STATUS: c.RSP_STATUS}
    if (decoded.get("rsp") == expected_rsp[last["command"]] and decoded.get("state") in allowed
            and decoded.get("error_code") == 0):
        return None  # the answer passed: the run stopped for another reason
    return {"request": _CMD_NAMES.get(last["command"], "?"),
            "answer": f"{_RSP_NAMES.get(decoded.get('rsp'), decoded.get('rsp'))} · "
                      f"{_STATE_NAMES.get(decoded.get('state'), decoded.get('state'))}",
            "state": _STATE_NAMES.get(decoded.get("state"), decoded.get("state")),
            "allowed_states": [_STATE_NAMES.get(state, state) for state in allowed],
            "error_code": decoded.get("error_code")}


def run_stop_point(report: dict) -> dict | None:
    """Explain a missing or partial first STATUS reply after an accepted RUN.

    Only the adjacent journal tail UPLOAD_END, RUN, GET_STATUS supports this
    diagnosis. Later polls, another segment's upload, final release, write or
    frame-validation failures and cancellation keep their ordinary diagnostics.
    """

    if report.get("outcome") != "REJECT" or report.get("release_command_attempted"):
        return None
    segment, planned = report.get("completed_segments"), report.get("planned_segments")
    if type(segment) is not int or type(planned) is not int or not 0 <= segment < planned:
        return None
    records = report.get("transactions")
    if not isinstance(records, list) or len(records) < 3:
        return None
    upload, started, failed = records[-3:]
    for record, command in zip((upload, started, failed), (c.CMD_UPLOAD_END, c.CMD_RUN, c.CMD_GET_STATUS)):
        if (not isinstance(record, dict) or record.get("kind") != "command" or record.get("command") != command
                or type(record.get("written_bytes")) is not int or record["written_bytes"] != c.STPG_CMD_SIZE
                or record.get("write_error")):
            return None
    seq = upload.get("seq")
    if type(seq) is not int or seq < 1 or started.get("seq") != seq + 1 or failed.get("seq") != seq + 2:
        return None

    def accepted(record: dict, state: int) -> bool:
        decoded = record.get("decoded")
        return (isinstance(decoded, dict) and not record.get("read_error") and not record.get("read_failure_stage")
                and decoded.get("seq") == record["seq"] and decoded.get("rsp") == c.RSP_OK
                and decoded.get("error_code") == 0 and decoded.get("state") == state)

    upload_state = c.STATE_READY if segment == 0 else c.STATE_HOLD_AWAITING_NEXT
    if not accepted(upload, upload_state) or not accepted(started, c.STATE_RUNNING):
        return None
    stage = failed.get("read_failure_stage")
    if stage not in ("deadline", "read_limit") or not failed.get("read_error") or failed.get("decoded") is not None:
        return None
    raw = failed.get("raw_response_hex")
    if not isinstance(raw, str) or len(raw) >= 2 * c.STPG_RSP_SIZE or re.fullmatch(r"(?:[0-9a-f]{2})*", raw) is None:
        return None  # unknown byte count or a complete frame is not this case

    def took(record: dict) -> float | None:
        begin, end = record.get("write_begin_s"), record.get("read_end_s")
        if type(begin) in (int, float) and type(end) in (int, float) and 0 <= begin <= end < float("inf"):
            return end - begin
        return None

    return {"segment": segment, "planned_segments": planned,
            "upload_end": f"OK · {_STATE_NAMES[upload_state]}", "run": "OK · RUNNING", "run_took_s": took(started),
            "failed_request": "GET_STATUS", "response_bytes": len(raw) // 2,
            "failure_stage": stage, "failed_took_s": took(failed)}


def identity_mismatch(report: dict) -> dict | None:
    """What the binding expects and what the STPG reported, when a reply's firmware version or capability differed
    from the binding (read from ``run.json``; nothing is asked again and the binding is never changed for you)."""

    device = report.get("device") if isinstance(report.get("device"), dict) else {}
    records = [t for t in report.get("transactions") or [] if isinstance(t, dict)]
    for index, record in enumerate(records):
        if record.get("read_failure_stage") != "identity" or not isinstance(record.get("decoded"), dict):
            continue
        decoded = record["decoded"]
        fields = [{"name": name, "expected": device.get(name), "reported": decoded.get(name)}
                  for name in ("firmware_version", "capability")]
        if all(field["expected"] == field["reported"] for field in fields):
            return None  # for example a sequence mismatch: not a different firmware
        expected, reported = device.get("capability"), decoded.get("capability")
        both = type(expected) is int and type(reported) is int
        return {"request": _CMD_NAMES.get(record.get("command"), "?"), "fields": fields,
                "uploaded_before": any(t.get("command") == c.CMD_UPLOAD_BEGIN for t in records[:index]),
                "only_expected": _capability_names(expected & ~reported) if both else [],
                "only_reported": _capability_names(reported & ~expected) if both else []}
    return None


def retained_analysis(folder: Path) -> tuple[Path, dict] | None:
    """The whole-source analysis a build reservation keeps: ``source.analysis.json`` (projects with a
    segmentation mode) or ``base-package/source.analysis.json`` (older projects), possibly ``.gz``."""

    try:
        candidate = folder / "source.analysis.json"
        if not candidate.exists():
            candidate = folder / "base-package" / "source.analysis.json"
        analysis = resolve_json_artifact(candidate)
        data = read_json_artifact(analysis)
    except (OSError, ValueError):
        return None
    return (analysis, data) if isinstance(data, dict) else None


def analysis_rejection(folder: Path, *, sdk_message: str = "", segmentation: dict | None = None) -> str | None:
    """Where the source analysis of a failed build reservation is, its reasons, and what to do.

    The SDK keeps a failed reservation as evidence; its ``source.analysis.json`` (projects with a
    segmentation mode) or ``base-package/source.analysis.json`` (older projects) says why no backend
    could take the STIL. The reasons are the SDK's own text (``source_rejection``); they are not
    repeated when the SDK message already carries them. Only when the SDK's capacity-only classifier
    (``capacity_reasons``) finds the cause, say that an automatic copy builds it in segments.
    """

    found = retained_analysis(folder)
    if found is None:
        return None
    analysis, data = found
    reasons = rejection_text(data)
    # Whole-source and legacy base-package analyses can differ in detail.
    # Modern SDK errors already carry their own causes; do not repeat them
    # merely because the other retained report is not byte-identical.
    carries_reasons = "source analysis is REJECT; package generation is blocked: " in sdk_message
    text = f"Source analysis: {analysis}" + ("." if carries_reasons else f": {reasons}.") + streaming_explanation(data)
    mode = (data.get("selection") or {}).get("mode_requested", "auto")
    if capacity_reasons(data, mode):
        policy = segmentation if isinstance(segmentation, dict) else {}
        cap = policy.get("max_vectors")
        setting = ("" if not policy else
                   "This project is set to one package (Segmentation = single). " if policy.get("mode") == "single" else
                   "This project was created before automatic segmentation (its segmentation has no mode) and keeps the "
                   "old meaning: " + (f"segments only when it has more than {cap} vectors. " if cap else "always one package. ")
                   if "mode" not in policy else "")
        count = (data.get("vectors") or {}).get("logical_vector_count")
        text += (f" The pattern does not fit in one package{f' ({count} vectors)' if isinstance(count, int) else ''}. "
                 + setting + "The failed reservation is kept as evidence. To build it in segments, set Segmentation = "
                 "automatic with Edit settings… (this project has no successful build) or make a copy of this "
                 "project with Segmentation = automatic: no vector count is needed; the builder fills each segment "
                 "up to the first STPG limit it meets (pattern bytes, events, samples, compares or Loop blocks).")
    return text


def streaming_explanation(data: dict, what: str = "pattern") -> str:
    """What ``ERR_STREAMING_POC_TIMING`` in a retained analysis means, and which WaveformTable a rate is.

    Rates are numbered by the compiler; a period that only one WaveformTable has names it. Empty when the
    analysis has no such error.
    """

    timing = [item.get("context") or {} for item in data.get("anomalies", [])
              if item.get("severity") == "error" and item.get("code") == "ERR_STREAMING_POC_TIMING"]
    if not timing:
        return ""
    periods: dict[int, list[str]] = {}
    for table, ticks in ((data.get("timing") or {}).get("period_ticks_by_waveform_table") or {}).items():
        periods.setdefault(ticks, []).append(table)
    named = sorted({f"rate[{item['rate_id']}] = {periods[item['period_ticks']][0]} (period {item['period_ticks']} ticks)"
                    for item in timing if "rate_id" in item and len(periods.get(item.get("period_ticks"), [])) == 1})
    text = f" By period: {'; '.join(named)}." if named else ""
    drive = timing[0].get("streaming_poc_required_drive_gap_ticks", 10)
    sample = timing[0].get("streaming_poc_required_sample_gap_ticks", 11)
    return text + (f" ERR_STREAMING_POC_TIMING: this {what} is played by streaming, and streaming needs at least "
                   f"{drive} ticks before each drive-only event time and {sample} ticks before each compare "
                   "(sample) time, counted from the previous event time in the vector or, for the first one "
                   "(cycle-wrap), from the last event time of the previous vector. The named rates are "
                   "WaveformTables whose edges are closer than that. Chunk waits, the loop policy and Timing = "
                   "Approx do not change this; the STIL WaveformTable timing has to change.")


_SEGMENT_REJECT = re.compile(r"variant (?P<variant>[A-Za-z0-9_.-]+) segment (?P<index>\d+) failed: "
                             r"source analysis is REJECT")


def segment_rejection(folder: Path, error: str) -> str | None:
    """Why one segment of a segmented build was rejected: the SDK keeps that segment's source analysis.

    The SDK's message for a segment names only "source analysis is REJECT"; the reasons are in
    ``segmented/<variant>/segment-NNNN/<dir>/source.analysis.json(.gz)``, where ``<dir>`` is
    ``.source-package`` when a wait is applied afterwards and ``package`` otherwise
    (``segmented_package.build_segmented_packages``). They are given in the SDK's own words
    (``source_rejection``). The whole-source analysis (capacity) does not apply here: the pattern is
    already being built in segments.
    """

    match = _SEGMENT_REJECT.search(error)
    if match is None:
        return None
    index = int(match["index"])
    segment = folder / "segmented" / match["variant"] / f"segment-{index:04d}"
    data = None
    for name in (".source-package", "package"):
        try:
            analysis = resolve_json_artifact(segment / name / "source.analysis.json")
            data = read_json_artifact(analysis)
            break
        except (OSError, ValueError):
            continue
    if data is None:
        return None
    if not isinstance(data, dict):
        return None
    return f"Segment {index} source analysis: {analysis}: {rejection_text(data)}." + streaming_explanation(data, "segment")


_SEGMENT_SCOPE = re.compile(r"segment (?P<index>\d+) wait materialization failed: .*service profile scope "
                            r"'(?P<scope>[a-z-]+)' does not match artifact scope '(?P<artifact>[a-z-]+)'")


def segment_scope_hint(error: str) -> str | None:
    """A segmented build stopped at one segment's wait scope check.

    Current SDKs validate the sequence union and its local projections. Do not
    recommend dropping loops/waits as a fallback for a local admission error.
    """

    match = _SEGMENT_SCOPE.search(error)
    if match is None:
        return None
    has = "has no Loop" if match["artifact"] == "non-loop" else "keeps a Loop"
    return (f"Segment {match['index']} {has} after splitting, and the project's scope is {match['scope']}. "
            "The current SDK checks the whole selected sequence and then each local projection. "
            "Keep the rejected evidence and check SDK identity/scope linkage; do not retry with "
            "different loops, waits or firmware implicitly.")


_SCOPE_MISMATCH = re.compile(r"service profile scope '(?P<scope>[a-z-]+)' does not match "
                             r"(?P<where>whole segmented sequence|artifact) scope '(?P<found>[a-z-]+)'")


def scope_mismatch_hint(error: str) -> str | None:
    """The build found the scope the compiled pattern needs; say it in the words of the settings dialog.

    The SDK checks a segmented build's declared scope against the whole sequence (loop-generic when any
    segment keeps Loop provenance) and a one-package build against its artifact. A segment's own check
    after that (``segment_scope_hint``) is handled first.
    """

    match = _SCOPE_MISMATCH.search(error)
    if match is None or match["scope"] == match["found"]:
        return None
    whole = match["where"] == "whole segmented sequence"
    if match["found"] == "loop-generic":
        what = "At least one segment keeps a Loop block" if whole else "The compiled pattern keeps a Loop block"
        budget = (" The service budget you declare then has to hold for the whole pattern, including the parts "
                  "without a Loop.")
    else:
        what = "No segment keeps a Loop block" if whole else "The compiled pattern keeps no Loop block"
        budget = ""
    return (f"{what}, so this build needs Scope = {match['found']}; the project declares {match['scope']}. "
            f"Choose Scope = {match['found']} with Edit settings… (this project has no successful build) or in a "
            f"copy of the project.{budget}")


_QUANTIZED = re.compile(r"quantized to (?P<ticks>\d+) ticks")
APPROXIMATION_REASONS_SHOWN = 8


def _ns(ticks: int, tick_hz: int) -> str:
    return f"{ticks * 1e9 / tick_hz:.3f}".rstrip("0").rstrip(".")


def approximation_hint(folder: Path) -> str | None:
    """Which times are off the target tick grid, from the retained analysis of an exact project's build."""

    found = retained_analysis(folder)
    if found is None:
        return None
    analysis, data = found
    timing = data.get("timing") or {}
    reasons = list(dict.fromkeys(timing.get("approximation_reasons") or []))
    if not reasons:
        return None
    tick_hz = int(timing.get("tick_hz") or 0)

    def emitted(reason: str) -> str:
        match = _QUANTIZED.search(reason)
        return f"{reason} (= {_ns(int(match['ticks']), tick_hz)} ns)" if match and tick_hz else reason

    shown = "; ".join(emitted(reason) for reason in reasons[:APPROXIMATION_REASONS_SHOWN])
    more = (f"; and {len(reasons) - APPROXIMATION_REASONS_SHOWN} more"
            if len(reasons) > APPROXIMATION_REASONS_SHOWN else "")
    grid = f" ({_ns(1, tick_hz)} ns per tick)" if tick_hz else ""
    return (f"Off the target tick grid{grid}, from {analysis}: {shown}{more}. The failed reservation is kept as "
            "evidence. To build this STIL as it is, choose Timing = Approx with Edit settings… (this project has no "
            "successful build) or in a copy: each listed time is rounded to the nearest tick (ties up) and the "
            "compile reports keep every change. Rounding does not change the STIL, and it does not repair "
            "streaming gaps, capacity or an edge at the period.")


def newest_reservation(workspace: Path, since: float) -> Path | None:
    """The reservation folder a build that started at ``since`` made (the newest one)."""

    if not workspace.is_dir():
        return None
    folders = [p for p in workspace.iterdir() if p.is_dir() and p.stat().st_mtime >= since - 2]
    return max(folders, key=lambda p: p.stat().st_mtime) if folders else None


def rejected_reservation(workspace: Path, since: float, error: str) -> Path | None:
    """The reservation a build that just raised ``error`` left behind, if its source analysis rejected it.

    A first build leaves a new FAILED reservation (the newest folder); building the same inputs again names
    the kept reservation in the SDK's "existing cache is incomplete/corrupt" message.
    """

    marker = "existing cache is incomplete/corrupt; not overwritten: "
    if marker in error:
        folder = Path(error.split(marker, 1)[1].strip())
    elif "source analysis is REJECT" in error:
        folder = newest_reservation(workspace, since)
    else:
        return None
    try:
        state = read_json(folder / "build.state.json") if folder is not None else {}
    except RunnerError:
        return None
    return folder if "source analysis is REJECT" in str(state.get("error", "")) else None


def copy_blockers(settings: dict) -> list[str]:
    """Why "Copy to a new project" could not reproduce these settings (New project sets only the init options)."""

    template = project_template(stil="inputs/pattern.stil", wait_preset="none", completion="hold").to_dict()
    out = []
    if settings["input"]["pattern"] is not None or settings["input"]["pattern_burst"] is not None:
        out.append("it selects a Pattern/PatternBurst, which New project cannot set")
    if settings["target"] != template["target"]:
        out.append(f"its target differs from the SDK's default target {template['target']}")
    return out


OPTION_KEYS = ("wait_preset", "completion", "storage", "compile_mode", "loop_policy")
# The longest Custom length whose ticks fit uint32 at the SDK's default target (200 MHz: 21,474,836 µs); the SDK
# checks the project's own target.
CUSTOM_WAIT_MAX_US = 0xFFFFFFFF * 1_000_000 // project_template(
    stil="inputs/pattern.stil", wait_preset="none", completion="hold").to_dict()["target"]["tick_hz"]
_WAIT_MS = re.compile(r"([0-9]{1,9})(?:\.([0-9]{1,12}))?")


def custom_wait_us(value: Any) -> int:
    """A Custom chunk-wait length typed in milliseconds, as exact whole microseconds (the SDK's ``chunk_wait_us``).

    The text is read as a decimal, never as a binary float: 1 µs steps (3 decimals; further decimals only as zeros).
    Nothing is rounded, truncated or clamped. The SDK then checks the target tick grid and the uint32 limit.
    """

    if type(value) is not str:
        raise RunnerError("Custom chunk wait: type the length in milliseconds, for example 6.5")
    text = value.strip()
    match = _WAIT_MS.fullmatch(text)
    if match is None:
        raise RunnerError(f"Custom chunk wait {text!r}: a positive number of milliseconds such as 6.5 or 0.001 "
                          "(digits and one decimal point only); nothing is rounded")
    fraction = match[2] or ""
    if fraction[3:].strip("0"):
        raise RunnerError(f"Custom chunk wait {text} ms is finer than 1 µs (3 decimals); nothing is rounded")
    microseconds = int(match[1]) * 1000 + int(fraction[:3].ljust(3, "0"))
    if microseconds < 1:
        raise RunnerError("Custom chunk wait must be longer than 0 ms")
    return microseconds


def project_options(request: dict) -> dict:
    """The settings of a New-project / Edit-settings form as ``init_project`` / ``project_template`` options."""

    missing = [key for key in OPTION_KEYS if request.get(key) in (None, "")]
    if missing:
        raise RunnerError(f"choose explicitly: {missing} (the GUI applies no silent defaults)")
    approx = request.get("allow_approx", False)
    if type(approx) is not bool:
        if isinstance(approx, str) and approx in {"true", "false"}:
            approx = approx == "true"  # HTML form values only; no truthiness coercion
        else:
            raise RunnerError("allow_approx must be a boolean or the form value true/false")
    segmentation = request.get("segmentation") or (
        "split" if request.get("max_segment_vectors") not in (None, "") else "single")
    number = lambda key: float(request[key]) if request.get(key) not in (None, "") else None  # noqa: E731
    if "chunk_wait_us" in request:
        raise RunnerError("give the Custom chunk wait as chunk_wait_ms (milliseconds, 1 µs steps)")
    if request["wait_preset"] == CUSTOM_WAIT_PRESET:
        if request.get("chunk_wait_ms") in (None, ""):
            raise RunnerError("Custom chunk wait needs its length in milliseconds, for example 6.5")
        chunk_wait = custom_wait_us(request["chunk_wait_ms"])
    elif request.get("chunk_wait_ms") not in (None, ""):
        # never stored or silently dropped: the form removes it when another preset is chosen
        raise RunnerError(f"a chunk wait length is used only with {CUSTOM_WAIT_PRESET}; "
                          f"wait preset {request['wait_preset']} has its own")
    else:
        chunk_wait = None
    if segmentation in {"single", "auto"}:
        if request.get("max_segment_vectors") not in (None, ""):
            if segmentation == "single":
                raise RunnerError("max vectors per segment is used only with segmentation = auto or split")
            text = str(request["max_segment_vectors"]).strip()
            if not text.isascii() or not text.isdigit() or int(text) < 1:
                raise RunnerError("max vectors per segment must be a positive integer")
            max_vectors = int(text)
        else:
            max_vectors = None
    elif segmentation == "split":
        text = str(request.get("max_segment_vectors") or "").strip()
        if not text.isdigit() or int(text) < 1:
            raise RunnerError("segmentation = split needs max vectors per segment (a positive integer)")
        max_vectors = int(text)
    else:
        raise RunnerError("segmentation must be auto, single or split")
    return dict(
        wait_preset=request["wait_preset"], completion=request["completion"], chunk_wait_us=chunk_wait,
        service_us=number("service_us"), service_source=request.get("service_source") or "",
        service_scope=request.get("service_scope") or None, max_total_wait_us=number("max_total_wait_us"),
        forbidden_stil_rates=tuple(names(request.get("forbidden_stil_rates"))), max_segment_vectors=max_vectors,
        segmentation_mode=(None if not request.get("segmentation") else
                           "auto" if segmentation in {"auto", "split"} else "single"),
        storage=request["storage"], compile_mode=request["compile_mode"], loop_policy=request["loop_policy"],
        allow_approx=approx)


# The parts of a project file that Edit settings may change: the same settings New project sets. The inputs,
# Pattern/PatternBurst selection, target and capture are kept as they are.
EDITABLE_SECTIONS = ("compile", "wait", "segmentation", "execution")


_LOOP_STATEMENT = re.compile(rb"(?m)^[ \t]*Loop[ \t]+\d")
_LOOP_WORD = re.compile(rb"(?<![A-Za-z0-9_])Loop(?![A-Za-z0-9_])")
_STIL_COMMENTS = re.compile(rb"//[^\n]*|/\*.*?\*/", re.S)


def stil_has_loop(path: Path) -> bool:
    """Whether a STIL has the word ``Loop`` outside comments.

    Without it no statement can be a Loop (the parser needs the keyword; preprocessing adds none), so the compiled
    pattern keeps no Loop block and a wait preset needs Scope = non-loop whatever the loop policy: the build would
    reject loop-generic. With it, the build decides (the Loop may be in a Pattern that is not selected, a string...).
    """

    with open_source_binary(path) as stream:
        data = stream.read()
    return _LOOP_WORD.search(_STIL_COMMENTS.sub(b" ", data)) is not None


def stil_loop_count(path: Path) -> int:
    """Loop statements in a STIL (lines that start with ``Loop N``; ``//`` comment lines do not).

    Only for the wait-scope hint of the settings dialog: loop policy preserve on a STIL with Loop gives a
    Loop artifact (loop-generic), anything else non-loop. The build stays the authority.
    """

    count, tail = 0, b""
    with open_source_binary(path) as stream:
        while chunk := stream.read(8 << 20):
            data = tail + chunk
            cut = data.rfind(b"\n") + 1
            count += len(_LOOP_STATEMENT.findall(data, 0, cut))
            tail = data[cut:]
    return count + len(_LOOP_STATEMENT.findall(tail))


def settings_summary(settings: dict) -> dict:
    """The settings a build reservation used, in the words of the New-project form (for the reservation list)."""

    wait, seg = settings.get("wait") or {}, settings.get("segmentation") or {}
    return {"compile_mode": (settings.get("compile") or {}).get("mode"),
            "loop_policy": (settings.get("compile") or {}).get("loop_policy"),
            "allow_approx": (settings.get("compile") or {}).get("allow_approx", False),
            "wait_preset": wait.get("preset"), "chunk_wait_us": wait.get("chunk_wait_us"),
            "service_scope": wait.get("service_scope"),
            "service_us": wait.get("service_us"), "max_total_wait_us": wait.get("max_total_wait_us"),
            "segmentation": seg.get("mode"), "max_vectors": seg.get("max_vectors"),
            "storage": (settings.get("execution") or {}).get("storage"),
            "completion": (settings.get("execution") or {}).get("completion")}


TIMING_GROUPS_SHOWN = 50


def timing_summary(root: Path, patterns: list[dict]) -> dict:
    """The bundle's timing choice and the rounding its compile reports record, over all packages.

    ``compile.policy.json`` binds the choice; an older bundle has none and was built exact. Each package's ``pattern.compile_report.json`` lists one record per rounded time and
    vector; they are grouped by (kind, WaveformTable, signal, source time, emitted time, delta).
    """

    policy_path = root / "compile.policy.json"
    policy = read_json(policy_path) if policy_path.is_file() else None
    groups: dict[tuple, dict] = {}
    packages = []
    for index, item in enumerate(patterns):
        folder = confined_path(root, item["path"]).parent
        try:
            report = read_json_artifact(resolve_json_artifact(folder / "pattern.compile_report.json"))
        except (OSError, ValueError) as exc:
            packages.append({"index": index, "path": item["path"], "records": None, "error": str(exc)})
            continue
        records = report.get("approximations") or []
        packages.append({"index": index, "path": item["path"], "records": len(records),
                         "approximate_vectors": int(report.get("approximate_count") or 0)})
        for record in records:
            key = tuple(record.get(name) for name in ("kind", "waveform_table_name", "affected_signal",
                                                      "original_time", "emitted_time_ns", "delta_ns"))
            group = groups.get(key)
            if group is None:
                group = groups[key] = {"kind": key[0], "waveform_table": key[1], "signal": key[2],
                                       "original_time": key[3], "emitted_time_ns": key[4], "delta_ns": key[5],
                                       "records": 0, "first_stil_line": record.get("stil_line"), "packages": []}
            group["records"] += 1
            if not group["packages"] or group["packages"][-1] != index:
                group["packages"].append(index)

    def magnitude(value: Any) -> float:
        try:
            return abs(float(value))
        except (TypeError, ValueError):
            return 0.0

    ordered = sorted(groups.values(), key=lambda g: (-magnitude(g["delta_ns"]), str(g["waveform_table"]),
                                                    str(g["signal"]), str(g["kind"]), str(g["original_time"])))
    return {"policy": policy, "allow_approx": bool(policy and policy.get("allow_approx") is True),
            "records": sum(p["records"] or 0 for p in packages), "group_count": len(groups),
            "groups": ordered[:TIMING_GROUPS_SHOWN],
            "max_abs_delta_ns": max((magnitude(g["delta_ns"]) for g in ordered), default=0.0),
            "packages": packages, "unreadable": [p["path"] for p in packages if p["records"] is None]}


def role_pins(bundle: Path) -> list[dict]:
    """Signals that the bundle's pin map gives a non-signal role (marker, fail).

    A pattern derived by another tool can drive such a pin (for example a trigger marker); the run confirmation
    names them so that the operator checks the pin is spare and safe to drive. Read-only; [] without a pin map.
    """

    try:
        pin_map = bundle_source_paths(Path(bundle)).get("pin_map")
        if pin_map is None:
            return []
        with open_source_binary(pin_map) as stream:
            data = json.loads(stream.read().decode("utf-8"))
    except (RunnerError, OSError, ValueError, KeyError, TypeError):
        return []
    out = []
    for name, entry in sorted((data.get("signals") or {}).items()):
        if isinstance(entry, dict) and entry.get("role", "signal") != "signal":
            out.append({"signal": name, "role": entry.get("role"), "bit": entry.get("bit"),
                        "logical_pin": entry.get("logical_pin")})
    return out


def default_results_root(bundle_dir: Path | None, fallback: Path) -> Path:
    if bundle_dir is not None and bundle_dir.parent.name == "build":
        return bundle_dir.parent.parent / "results"
    return fallback


PROJECT_LOG = "stpg-view.log.jsonl"
PROJECT_FILE = "project.stpg-project.json"


def _result_entry(run_file: Path) -> Path:
    """The result folder of a run record: ``<entry>/run.json.gz``, ``<entry>/stpg/…`` or (older) ``<entry>/run/…``."""

    folder = Path(run_file).parent
    return folder.parent if folder.name in ("stpg", "run") else folder


def project_log(project_dir: Path, record: dict) -> None:
    """Append one JSON line to the project's log; stpg-view writes it, the SDK never reads it.

    It is not part of any build identity (the cache key binds the settings and ``inputs/``), and nothing
    reads it back. A log that cannot be written never stops a build.
    """

    line = json.dumps({"time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record},
                      ensure_ascii=False)
    try:
        with (Path(project_dir) / PROJECT_LOG).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
    except OSError:
        pass


def _sdk_info() -> dict:
    try:
        return sdk_info()
    except OSError as exc:  # an SDK file replaced while it is hashed (an update in progress)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _relative(path: Path, base: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return str(path)


def logged_build(project_path: Path, workspace: Path, **options: Any) -> BuildResult:
    """``build_project`` with a start and an end line in the project log (``PROJECT_LOG``).

    The start line names the SDK files and the Python that build (``stpg_runner.sdk_info``: the fingerprint of
    the installed files now and when stpg-view imported the SDK, so an update without a restart shows). The end
    line gives the reservation and the result, or the SDK's own error text (not the GUI's hints).
    """

    folder, started = project_path.parent, time.time()
    project_log(folder, {"event": "build", "status": "started", "project": project_path.name,
                         "workspace": _relative(workspace, folder), "sdk": _sdk_info(),
                         "stpg_view_directory": str(Path(__file__).resolve().parents[1]),
                         "platform": f"{platform.system()} {platform.release()}"})
    end: dict[str, Any] = {"event": "build"}
    try:
        result = build_project(project_path, **options)
    except BaseException as exc:
        marker = "existing cache is incomplete/corrupt; not overwritten: "
        reservation = (Path(str(exc).split(marker, 1)[1].strip()) if marker in str(exc)
                       else newest_reservation(workspace, started))
        end.update(status="CANCELLED" if isinstance(exc, BuildCancelled) else "FAILED",
                   reservation=_relative(reservation, folder) if reservation is not None else None,
                   error=f"{type(exc).__name__}: {exc}"[:4000])
        raise
    else:
        end.update(status="HOST_ONLY_VERIFIED", reservation=_relative(result.bundle_dir, folder),
                   cache_hit=result.cache_hit, package_kind=result.package_kind,
                   segment_count=result.segment_count, manifest_sha256=result.manifest_sha256)
        return result
    finally:
        info = _sdk_info()
        end.update(elapsed_s=round(time.time() - started, 1), sdk_tool_sha256=info.get("tool_sha256"),
                   sdk_code_changed_since_import=info.get("code_changed_since_import"))
        project_log(folder, end)


def _flatten(value: Any, prefix: str = "") -> set[str]:
    if isinstance(value, dict):
        out = set()
        for key, item in value.items():
            out |= _flatten(item, f"{prefix}{key}.")
        return out
    return {prefix[:-1]}


def _frozen(value: Any) -> bytes:
    """Canonical bytes of a confirmed value: what is executed is decoded from these, never re-read."""

    return canonical_json(value)


def _thaw(value: bytes) -> Any:
    return json.loads(value)


def _capability_names(capability: int) -> list[str]:
    return [name for name in dir(c) if name.startswith("CAP_") and name != "CAPABILITY_V01"
            and getattr(c, name) and capability & getattr(c, name) == getattr(c, name)
            and bin(getattr(c, name)).count("1") == 1]


def accessory_tool() -> dict:
    """Whether the separately distributed logic-analyzer tool is installed in this Python environment.

    Package metadata only: nothing of it is imported or started. "Not found" says nothing about other environments;
    an accessory config can name a program anywhere.
    """

    try:
        version = metadata.version(LA_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        version = None
    return {"distribution": LA_DISTRIBUTION, "command": LA_COMMAND, "installed": version is not None,
            "version": version}


class RunnerService:
    def __init__(self, *, config_dir: Path, device_run: bool, demo: bool, results_dir: Path,
                 session: Any = None, config_location: ConfigLocation | None = None) -> None:
        self.store = BindingStore(config_dir)
        self.location = config_location or ConfigLocation(Path(config_dir), OPTION)
        self.accessory_tool = accessory_tool()  # looked up once: installing it needs a restart anyway
        self.jobs = RunnerJobs()
        self.device_run = device_run
        self.demo = demo
        self.results_dir = Path(results_dir)
        self.session = session
        self.lock = threading.Lock()
        # Confirmation -> job transitions of every tab are serialized: a frozen confirmation is consumed once.
        self.transition = threading.Lock()
        self.confirmations: dict[str, dict] = {}
        self.loop_counts: dict[str, int] = {}  # STIL sha256 -> Loop statements (the scope hint)
        self.loop_words: dict[str, bool] = {}  # STIL sha256 -> any Loop keyword outside comments
        self.timing: dict[str, dict] = {}  # bundle manifest sha256 -> timing_summary
        self.port_provider: Any = None  # None: the SDK's own enumeration (host tests inject a port list)

    # ---- state -----------------------------------------------------------------
    def config_info(self) -> dict:
        """The settings folder of this start, why it was chosen and what is in it now (names only)."""

        info = describe_config_dir(self.store.root)
        return {"path": str(self.store.root), "source": self.location.source,
                "source_text": self.location.source_text, "notes": list(self.location.notes),
                "text": state_text(info), **info}

    def state(self) -> dict:
        active = self.jobs.active_device_job()
        return {
            "device_run": self.device_run, "demo": self.demo, "config_dir": str(self.store.root),
            "config": self.config_info(), "accessory_tool": self.accessory_tool,
            "results_dir": str(self.results_dir), "platform": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(), "jobs": self.jobs.summary(),
            "active_device_job": active.id if active else None,
            "wait_presets": list(WAIT_PRESETS), "wait_preset_us": dict(WAIT_PRESETS), "input_forms": INPUT_FORMS,
            # Custom is a separate mode with its own length (the numeric presets stay as they were)
            "wait_modes": list(WAIT_MODES), "custom_wait_preset": CUSTOM_WAIT_PRESET,
            "custom_wait_max_us": CUSTOM_WAIT_MAX_US,
            "acknowledgements": ACKNOWLEDGEMENTS,
            "controller": {"pins": list(SUPPORTED_PINS), "legacy_pins": [0, 1, 2], "states": list(GPIO_STATES),
                           "roles": {str(k): v for k, v in PIN_ROLES.items()}, "pulse_ms": [1, 5000],
                           "settle_ms": [0, 10000]},
            "external_protocol": "stpg_external_accessory_v1",
        }

    # ---- projects and bundles ------------------------------------------------------
    def open_project(self, path: str) -> dict:
        project_path = Path(path).expanduser().resolve()
        if project_path.is_dir():
            project_path = project_path / "project.stpg-project.json"
        raw = read_json(project_path)
        project = load_project(project_path)
        settings = project.to_dict()
        defaults = sorted(_flatten(settings) - _flatten(raw))
        inputs: dict[str, Any] = {}
        input_errors = []
        try:
            for role, item in project.input_paths(project_path.parent).items():
                inputs[role] = {"path": str(item), **identity(item)}
        except RunnerError as exc:
            input_errors.append(str(exc))
        stil_loops = stil_has_loop_word = None
        if "stil" in inputs:
            sha = inputs["stil"]["sha256"]
            if sha not in self.loop_counts:
                self.loop_counts[sha] = stil_loop_count(Path(inputs["stil"]["path"]))
                self.loop_words[sha] = stil_has_loop(Path(inputs["stil"]["path"]))
            stil_loops = self.loop_counts[sha]
            stil_has_loop_word = self.loop_words[sha]
        reservations = self.reservations(project_path.parent / "build")
        for entry in reservations:
            entry["current_settings"] = entry.get("settings_full") == settings
            entry.pop("settings_full", None)
        return {"kind": "project", "project_path": str(project_path), "project_dir": str(project_path.parent),
                "project_sha256": identity(project_path)["sha256"],
                "settings": settings, "defaults": defaults, "inputs": inputs, "input_errors": input_errors,
                "stil_loops": stil_loops, "stil_has_loop": stil_has_loop_word, "reservations": reservations,
                "results_dir": str(project_path.parent / "results"),
                "derivation": self.derivation_of(project_path.parent),
                "preprocess": (read_json(project_path.parent / PREPROCESS_RECORD)
                               if (project_path.parent / PREPROCESS_RECORD).is_file() else None),
                "copy_blockers": copy_blockers(settings),
                "edit_blockers": self.edit_blockers(project_path)}

    def edit_blockers(self, project_path: Path) -> list[str]:
        """Why "Edit settings" is not offered: only a project that never built successfully is edited in place.

        A successful bundle carries its own copy of the settings, but the project would then no longer
        describe it; such a project is copied instead. A marker-derived project belongs to its derivation
        record. A build that is running reads the project file.
        """

        out = []
        folder = project_path.parent
        if (folder / DERIVED_PROJECT_FILE).is_file():
            out.append("it is a marker project derived by the logic-analyzer tool; its settings belong to that tool")
        workspace = folder / "build"
        built = []
        if workspace.is_dir():
            for item in workspace.iterdir():
                try:
                    if item.is_dir() and read_json(item / "build.state.json").get("status") == "HOST_ONLY_VERIFIED":
                        built.append(item.name)
                except RunnerError:
                    continue
        if built:
            out.append(f"it has a successful build ({built[0][:12]}…); make a copy to try other settings")
        if self.jobs.running(f"build:{project_path}"):
            out.append("a build of it is running")
        return out

    def edit_project(self, request: dict) -> dict:
        """Change the settings of a project that has no successful build, in place.

        Only the settings New project sets change (``EDITABLE_SECTIONS``); inputs, the Pattern/PatternBurst
        selection, the target and capture stay. The SDK loader validates the result before it is written,
        and ``project_sha256`` must still match the file the page shows. The failed reservations stay in
        ``build/`` as evidence with the settings they used (each has its own ``project.json``); a build
        with the new settings gets a new cache key.
        """

        project_path = Path(request.get("project") or "").expanduser().resolve()
        if project_path.is_dir():
            project_path = project_path / "project.stpg-project.json"
        options = project_options(request)
        with self.lock:
            current = load_project(project_path).to_dict()
            if identity(project_path)["sha256"] != request.get("project_sha256"):
                raise RunnerError("the project file changed since it was opened; open it again before editing")
            blockers = self.edit_blockers(project_path)
            if blockers:
                raise RunnerError("settings are edited only in a project without a successful build: "
                                  + "; ".join(blockers))
            template = project_template(stil=current["input"]["stil"], pin_map=current["input"]["pin_map"],
                                        **options).to_dict()
            updated = {**current, **{key: template[key] for key in EDITABLE_SECTIONS}}
            if "allow_approx" not in current["compile"] and options["allow_approx"] is False:
                # Absent means exact in an older project; saving exact again does not add the new key.
                updated["compile"] = {k: v for k, v in updated["compile"].items() if k != "allow_approx"}
            write_json(project_path, Project.from_dict(updated).to_dict())
        return self.open_project(str(project_path))

    @staticmethod
    def derivation_of(project_dir: Path) -> dict | None:
        """Whether the separate logic-analyzer tool derived this project (its record is that tool's business)."""

        path = Path(project_dir) / DERIVED_PROJECT_FILE
        return {"record": str(path)} if path.is_file() else None

    @staticmethod
    def reservations(workspace: Path) -> list[dict]:
        out = []
        if not workspace.is_dir():
            return out
        for folder in sorted(workspace.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not folder.is_dir():
                continue
            entry: dict[str, Any] = {"path": str(folder), "key": folder.name}
            try:
                state = read_json(folder / "build.state.json")
                entry["status"] = state.get("status")
                if state.get("error"):
                    entry["error"] = state["error"]
            except RunnerError:
                entry["status"] = "UNREADABLE"
            try:  # the settings this attempt used: the SDK writes them first into every reservation
                used = read_json(folder / "project.json")
                entry["settings"] = settings_summary(used)
                entry["settings_full"] = used
            except (RunnerError, AttributeError):
                entry["settings"] = None
            if (folder / BUNDLE_NAME).is_file():
                entry["manifest_sha256"] = identity(folder / BUNDLE_NAME)["sha256"]
            out.append(entry)
        return out[:50]

    def stil_info(self, path: str) -> dict:
        """Loop statements of a STIL the New-project dialog names (read only): the wait-scope hint."""

        source = Path(path).expanduser()
        if not source.is_file():
            raise ValueError(f"{source}: not a file")
        return {"path": str(source), "has_loop": stil_has_loop(source), "loop_lines": stil_loop_count(source)}

    def init_project(self, request: dict) -> dict:
        """Create a new project through the SDK's ``init_project``.

        ATE-generator input (``input_form`` ``ate-pair``/``ate-single``) is first
        converted with the compiler's own preprocessor (the code of
        ``stpg-stil-preprocess``); the project then holds the converted STIL as
        its STIL input and ``preprocess.json`` records the original files. That
        record is not part of the build identity.
        """

        missing = [key for key in ("stil", "directory", *OPTION_KEYS) if request.get(key) in (None, "")]
        if missing:
            raise RunnerError(f"choose explicitly: {missing} (the GUI applies no silent defaults)")
        # The page always sends both; an API caller without them gets the earlier meaning (STIL as is, and a
        # split exactly when max_segment_vectors is given).
        form = request.get("input_form") or "stil"
        if form not in INPUT_FORMS:
            raise RunnerError(f"input_form must be one of {sorted(INPUT_FORMS)}")
        options = project_options(request)
        stil = Path(request["stil"]).expanduser()
        directory = Path(request["directory"]).expanduser()
        if directory.exists() or directory.is_symlink():
            raise RunnerError(f"{directory} already exists; type the name of a new folder "
                              "(a project is created in a new folder, an existing one is never reused)")
        pin_map = Path(request["pin_map"]).expanduser() if request.get("pin_map") else None
        ignore = names(request.get("ignore_signals"))
        record = None
        if form == "stil":
            if ignore or request.get("ate_project"):
                raise RunnerError("ignore signals and an ATE project file apply only to ATE-generator input")
            check_toolchain_form(stil)
            path = init_project(stil, directory, pin_map=pin_map, **options)
            copy_of = Path(request["copy_of"]).expanduser().resolve().parent if request.get("copy_of") else None
            if copy_of is not None and (copy_of / PREPROCESS_RECORD).is_file():
                # A copy of a preprocessed project keeps the record of where its STIL came from, but only
                # while its inputs are byte-identical to the ones that record describes.
                original = read_json(copy_of / PREPROCESS_RECORD)
                stored_inputs = load_project(path).input_paths(path.parent)
                pins = stored_inputs.get("pin_map")
                if (original.get("output", {}).get("inputs/pattern.stil") == decoded_source_identity(stored_inputs["stil"])
                        and original.get("pin_map", {}).get("inputs/pins.json") == (decoded_source_identity(pins) if pins else None)):
                    record = dict(original, copied_from=str(copy_of))
                    record["output_identity"] = "decoded_bytes; logical input names"
                    record["stored_inputs"] = {
                        role: {"path": item.relative_to(path.parent).as_posix(), **identity(item)}
                        for role, item in stored_inputs.items()
                    }
        else:
            if form == "ate-pair" and not request.get("ate_project"):
                raise RunnerError("choose the ATE project file (Signals/Timing) that belongs to this pattern")
            project_file = Path(request["ate_project"]).expanduser() if form == "ate-pair" else stil
            sources = {"project": {"path": str(project_file.resolve()), **identity(project_file)},
                       "pattern": {"path": str(stil.resolve()), **identity(stil)}}
            with tempfile.TemporaryDirectory(prefix="stpg-view-preprocess-") as tmp:
                converted, generated = preprocess_ate_input(project_file, stil, Path(tmp), ignore=ignore,
                                                            pin_map=pin_map is None)
                current = {"project": {"path": str(project_file.resolve()), **identity(project_file)},
                           "pattern": {"path": str(stil.resolve()), **identity(stil)}}
                if current != sources:
                    raise RunnerError("ATE source changed during preprocessing; no project created")
                path = init_project(converted, directory, pin_map=pin_map or generated, **options)
            stored_inputs = load_project(path).input_paths(path.parent)
            stored_pins = stored_inputs.get("pin_map")
            record = {
                "kind": "stpgview_preprocess_record", "schema_version": 1,
                "note": ("written by stpg-view when it created this project; not part of the build identity "
                         "(a build binds inputs/ by content)"),
                "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tool": "stil.preprocess.preprocess_ategen_stil (the code of stpg-stil-preprocess)",
                "form": form,
                "sources": sources,
                "ignore_signals": ignore,
                "output": {"inputs/pattern.stil": decoded_source_identity(stored_inputs["stil"])},
                "output_identity": "decoded_bytes; logical input names",
                "stored_inputs": {
                    role: {"path": item.relative_to(path.parent).as_posix(), **identity(item)}
                    for role, item in stored_inputs.items()
                },
                "pin_map": {"source": "given", "path": str(pin_map.resolve())} if pin_map is not None else {
                    "source": ("generated: the preprocessor's default pin map (stpg-stil-preprocess --pin-map), "
                               "declaration order without the ignored signals")},
            }
            record["pin_map"]["inputs/pins.json"] = decoded_source_identity(stored_pins) if stored_pins else None
        if record is not None:
            write_json(path.parent / PREPROCESS_RECORD, record)
        return self.open_project(str(path))

    def open_bundle(self, path: str, manifest_sha256: str | None = None) -> dict:
        root = Path(path).expanduser().resolve()
        if root.name == BUNDLE_NAME:
            root = root.parent
        # The SDK's process receipt: after one full check of these bytes in this process (a build, Open
        # or run), it re-hashes every file instead; check_mode says which it did.
        started = time.monotonic()
        verification = verify_bundle(root, manifest_sha256=manifest_sha256 or None, reuse=True)
        info: dict[str, Any] = {"kind": "bundle", "bundle_dir": str(root), "verification": verification,
                                "check": {"mode": verification.get("check_mode"),
                                          "seconds": round(time.monotonic() - started, 2)}}
        if (root / BUNDLE_NAME).is_file():
            info["manifest_sha256"] = identity(root / BUNDLE_NAME)["sha256"]
            if verification["status"] == "PASS":
                manifest = read_json(root / BUNDLE_NAME)
                info.update(settings=manifest["binding"]["project"], package_kind=manifest["package_kind"],
                            patterns=manifest["patterns"], tool_sha256=manifest["binding"]["tool_sha256"],
                            inputs=manifest["binding"]["inputs"], python=manifest["binding"]["python"])
                # Run refuses a bundle bound to another SDK; the page says so in words (the hashes go to the
                # project log). A bundle updated without recompiling says that too (the SDK's own warning).
                info["sdk_matches"] = manifest["binding"]["tool_sha256"] == tool_fingerprint()
                rebound = manifest["binding"].get("rebind")
                info["rebound"] = {"warning": rebound["warning"]} if isinstance(rebound, dict) else None
                target = manifest["binding"]["project"]["target"]
                if manifest["binding"]["project"]["execution"]["storage"] == "psram":
                    try:  # how much of the PSRAM pattern region each segment fills (the bundle table)
                        info["psram_pattern_bytes"] = get_runtime_capacity_profile(
                            target["runtime_profile"]).psram_pattern_bytes
                    except (KeyError, ValueError):
                        pass
                sha = info["manifest_sha256"]
                if sha not in self.timing:  # a verified manifest's hash fixes every compile report it lists
                    self.timing[sha] = timing_summary(root, manifest["patterns"])
                info["timing"] = self.timing[sha]
        info["results_dir"] = str(default_results_root(root, self.results_dir))
        if verification["status"] == "PASS":
            info["role_pins"] = role_pins(root)
        return info


    # ---- offline jobs ----------------------------------------------------------------
    def start_build(self, project_path: str, work_dir: str | None = None) -> Job:
        path = Path(project_path).expanduser().resolve()
        workspace = Path(work_dir).expanduser().resolve() if work_dir else path.parent / "build"

        def work(job: Job) -> dict:
            started = time.time()
            try:
                result = logged_build(path, workspace, work_dir=Path(work_dir).expanduser() if work_dir else None,
                                      on_progress=job.progress, cancel=job.token)
            except AttributeError as exc:
                # The SDK's rejection renderer fails on an analysis without backend candidates (D1);
                # the reservation it just kept has the analysis, so give its reasons instead.
                folder = newest_reservation(workspace, started)
                found = retained_analysis(folder) if folder is not None else None
                if found is None or found[1].get("status") != "REJECT":
                    raise
                raise RunnerError(f"{rejection_text(found[1])}. Source analysis: {found[0]}. (The SDK raised "
                                  f"AttributeError: {exc} while it worded this rejection; the reasons above are "
                                  "read from the analysis it kept.)") from exc
            except ValueError as exc:
                inner = "" if type(exc) is RunnerError else f"{type(exc).__name__}: "
                hint = segment_scope_hint(str(exc)) or scope_mismatch_hint(str(exc))
                if hint is None and "ERR_APPROXIMATION_REQUIRED: source timing is off" in str(exc):
                    folder = newest_reservation(workspace, started)
                    hint = approximation_hint(folder) if folder is not None else None
                if hint is not None:
                    raise RunnerError(f"{inner}{exc}. {hint}") from exc
                folder = rejected_reservation(workspace, started, str(exc))
                try:  # the project's setting names why it was one package (old projects have no mode)
                    segmentation = read_json(path).get("segmentation", {"max_vectors": None})
                except (OSError, RunnerError, AttributeError):
                    segmentation = None
                if folder is None:
                    detail = None
                elif _SEGMENT_REJECT.search(str(exc)):  # not the whole-source analysis: it is built in segments
                    detail = segment_rejection(folder, str(exc))
                else:
                    detail = analysis_rejection(folder, sdk_message=str(exc), segmentation=segmentation)
                if detail is None:
                    raise
                # The job shows the type of the raised error; name the SDK's own only when it differs.
                raise RunnerError(f"{inner}{exc}. {detail}") from exc
            return {**result.to_dict(), "bundle": self.open_bundle(str(result.bundle_dir), result.manifest_sha256)}

        return self.jobs.start("build", f"build {path.parent.name}", work, key=f"build:{path}")

    def start_verify(self, bundle: str, manifest_sha256: str | None) -> Job:
        return self.jobs.start("verify", "verify bundle",
                               lambda job: verify_bundle(Path(bundle), manifest_sha256=manifest_sha256 or None))

    def start_plan(self, bundle: str, manifest_sha256: str) -> Job:
        return self.jobs.start("plan", "plan run (offline)",
                               lambda job: prepare_run(Path(bundle), manifest_sha256=manifest_sha256).to_dict())

    # ---- bindings ----------------------------------------------------------------------
    def bindings(self, kind: str) -> dict:
        """Local bindings of one kind. Analyzer bindings belong to the separate logic-analyzer tool: listed here only
        by their USB identity (the RUN target of a manual analyzer reset), never written by this GUI."""

        items = self.store.list(kind)
        if self.demo:
            config = {"devices": SIMULATED_DEVICE, "analyzers": SIMULATED_LA_TARGET,
                      "controllers": SIMULATED_CONTROLLER}[kind].to_dict()
            items.insert(0, {"name": SIMULATED, "path": None, "config": config, "simulated": True})
        return {"kind": kind, "items": items, "read_only": kind == "analyzers"}

    def scan_ports(self) -> dict:
        """Metadata enumeration only (no port is opened); nothing is selected automatically."""

        ports = []
        if self.demo:
            for port in simulated_ports():
                ports.append({"device": port.device, "vid": port.vid, "pid": port.pid,
                              "serial_number": port.serial_number, "description": "simulated STPG (demo)"})
            ports.append({"device": SIMULATED_LA_TARGET.port, "vid": SIMULATED_LA_TARGET.vid,
                          "pid": SIMULATED_LA_TARGET.pid, "serial_number": SIMULATED_LA_TARGET.serial_number,
                          "description": "simulated analyzer (demo; RUN-line target only)"})
        error = None
        if not self.device_run:
            return {"ports": ports, "error": None, "real_scan_performed": False}
        try:
            from serial.tools import list_ports

            for port in list_ports.comports(include_links=True):
                ports.append({"device": port.device, "vid": port.vid, "pid": port.pid,
                              "serial_number": port.serial_number, "description": port.description,
                              "manufacturer": port.manufacturer, "product": port.product,
                              "location": port.location})
        except Exception as exc:  # noqa: BLE001 - shown to the user
            error = f"{type(exc).__name__}: {exc}"
        return {"ports": ports, "error": error, "real_scan_performed": True}

    @staticmethod
    def capability_names(capability: int) -> list[str]:
        return _capability_names(capability)

    # ---- run -------------------------------------------------------------------------------
    def _check_ports(self, device: DeviceConfig, simulated: bool, then: str) -> None:
        """The SDK's port rule on the current USB port list, before its long bundle verification.

        Keep this GUI-side diagnostic before confirmation as well as Start. The updated SDK independently
        checks metadata before its semantic verification and again immediately before open. Nothing is
        opened here; this GUI check never replaces authoritative SDK admission.
        """

        ports = simulated_ports() if simulated else (self.port_provider or sdk_device.enumerate_ports)()
        mismatch = port_mismatch(device, ports)
        if mismatch:
            raise RunnerError(f"STPG port check (USB metadata only, no port opened): {mismatch}. {then}")

    def _device(self, name: str):
        if name == SIMULATED:
            if not self.demo:
                raise RunnerError("the simulated device exists only with --demo-device")
            return SIMULATED_DEVICE, True
        if not self.device_run:
            raise RunnerError("device execution is disabled (stpg-view was started with --no-device-run)")
        return self.store.load("devices", name), False

    def _controller(self, name: str, simulated_device: bool | None) -> tuple[ControllerConfig, bool]:
        """A selected controller binding; ``simulated_device`` None = manual control (no STPG in the job)."""

        if name == SIMULATED:
            if not self.demo or simulated_device is False:
                raise RunnerError("the simulated GPIO controller works only in demo mode, with the simulated STPG")
            return SIMULATED_CONTROLLER, True
        if simulated_device:
            raise RunnerError("a real GPIO controller cannot be combined with the simulated STPG")
        if not self.device_run:
            raise RunnerError("device access is disabled (stpg-view was started with --no-device-run)")
        if not name:
            raise RunnerError("choose a GPIO controller binding")
        return self.store.load("controllers", name), False

    def _target(self, kind: str, name: str, simulated: bool):
        """RUN target binding of a manual operation: leased by the SDK, never opened or queried."""

        if kind not in ("stpg", "la"):
            raise RunnerError("the RUN target is the STPG (GPIO0) or the analyzer (GPIO1)")
        if name == SIMULATED:
            if not simulated:
                raise RunnerError("the simulated STPG/analyzer can be reset only by the simulated controller")
            return SIMULATED_DEVICE if kind == "stpg" else SIMULATED_LA_TARGET
        if simulated:
            raise RunnerError("the simulated controller can reset only the simulated STPG/analyzer")
        if not name:
            raise RunnerError(f"choose the {'STPG device' if kind == 'stpg' else 'analyzer'} binding wired to this RUN line")
        return self.store.load("devices" if kind == "stpg" else "analyzers", name)

    def _external(self, selection: dict, bundle: Path, sha: str, device: DeviceConfig, simulated: bool) -> dict:
        """A selected external accessory: its local config, checked file by file (no launch, no USB enumeration)."""

        path = Path(str(selection.get("config") or "")).expanduser()
        if not str(selection.get("config") or "").strip():
            raise RunnerError("choose the external accessory's local config file (stpg_external_accessory JSON)")
        path = path.resolve()
        spec = load_external_accessory(path)
        if spec.mode != ("simulated" if simulated else "device"):
            raise RunnerError(f"the accessory config is for mode {spec.mode!r}; the "
                              f"{'simulated (demo) STPG needs a simulated' if simulated else 'real STPG needs a device'}"
                              " accessory config")
        prepared = prepare_external_accessory(bundle, manifest_sha256=sha, device=device, spec=spec)
        summary = {
            "config": str(path), "config_sha256": identity(path)["sha256"], "identifier": spec.identifier,
            "command": list(spec.command), "executable_sha256": spec.executable_sha256,
            "binding_file": str(spec.binding_file), "binding_sha256": spec.binding_sha256,
            "plan_file": str(spec.plan_file), "plan_sha256": spec.plan_sha256, "segment": spec.segment,
            "mode": spec.mode, "timeout_s": spec.timeout_s,
            "open_timeout_s": spec.open_timeout_s if spec.open_timeout_s is not None else spec.timeout_s,
            "close_timeout_s": spec.close_timeout_s, "device": prepared.binding.to_dict(),
            "lease_key": physical_identity_key(prepared.binding),
            "target_pattern_sha256": prepared.target_pattern_sha256,
            "notes": [
                "The core checked only the config, the launcher and input hashes, the device's USB identity (it must "
                "differ from the STPG and the controller) and the segment. The accessory validates its own binding "
                "and plan, leases and opens its device before it answers READY.",
                "The launcher hash does not attest the code it runs: select only an accessory program you trust.",
                f"Order at segment {spec.segment}: GPIO before-run actions, the accessory's ARMED, then RUN. After a "
                "valid DONE/HOLD of that segment: the accessory's COMPLETE. No other segment involves it.",
                "On any failure or cancel no cleanup command is sent to the accessory: its stdin is closed once.",
                "Its detailed results stay with the accessory program; the core keeps only a neutral result.",
            ]}
        return {"config": str(path), "spec_json": _frozen(spec.to_dict()), "binding": prepared.binding,
                "summary": summary}

    def inspect_external(self, request: dict) -> dict:
        """What a selected accessory config would use with this bundle and STPG (files only; nothing launched)."""

        bundle = Path(request["bundle"]).expanduser().resolve()
        device, simulated = self._device(request.get("device") or "")
        return self._external({"config": request.get("config")}, bundle, request.get("manifest_sha256") or "",
                              device, simulated)["summary"]

    def prepare_run(self, request: dict) -> dict:
        bundle = Path(request["bundle"]).expanduser().resolve()
        sha = request.get("manifest_sha256") or ""
        device, simulated = self._device(request.get("device") or "")
        self._check_ports(device, simulated, "Fix the binding or the connection, then press Run… again "
                                             "(the bundle was not checked).")
        started = time.monotonic()
        plan = prepare_run(bundle, manifest_sha256=sha)  # a full check or the SDK's receipt, a snapshot; no device
        check = {"mode": plan.verification_mode, "seconds": round(time.monotonic() - started, 2)}
        external = self._external(request["external"], bundle, sha, device, simulated) \
            if request.get("external") else None
        control = None
        controller = None
        if request.get("control"):
            spec = request["control"]
            controller, _ = self._controller(spec.get("controller") or "", simulated)
            actions = validate_control_plan(spec.get("plan"), controller, plan)  # SDK rules: pins, phases, HIGH
            ensure_separate(controller, [device] + ([external["binding"]] if external is not None else []))
            control = {"controller": spec.get("controller"), "controller_json": _frozen(controller.to_dict()),
                       "plan_json": _frozen(actions)}
        root = default_results_root(bundle, self.results_dir)
        output = Path(request.get("output_dir") or root / f"{_stamp()}-{sha[:8]}-{secrets.token_hex(2)}").expanduser().resolve()
        if output.exists():
            raise RunnerError(f"{output} exists; every run needs a new result directory")
        if output == bundle or bundle in output.parents:
            raise RunnerError("results must be outside the immutable bundle")
        plan_dict = plan.to_dict()
        summary = {
            "bundle_dir": str(bundle), "manifest_sha256": sha, "bundle_check": check,
            "runtime_profile": plan.runtime_profile,
            "storage": {c.STORAGE_SRAM: "sram", c.STORAGE_PSRAM: "psram"}.get(plan.storage, plan.storage),
            "completion": plan.completion,
            "completion_text": ("hold: final output levels stay driven after the last segment (no release command)"
                                if plan.completion == "hold" else
                                "release: RESET(1) + STATUS + INFO after all segments; electrical release is not "
                                "verified"),
            "segments": [{"index": s["index"], "path": s["path"], "sha256": s["sha256"],
                          "size_bytes": s["expected"]["size_bytes"], "mode": s["expected"]["mode"],
                          "active_pins": s["expected"]["active_pins"], "events": s["expected"]["events"],
                          "samples": s["expected"]["samples"], "compares": s["expected"]["compares"],
                          "run_flags": s["run_args"][0]} for s in plan_dict["segments"]],
            "observation": plan_dict["observation"],
            "device": {"name": request.get("device"), **device.to_dict(),
                       "capabilities": _capability_names(device.capability),
                       "lease_key": physical_identity_key(device)},
            "transport": "simulated (demo; no hardware)" if simulated else "serial (USB CDC)",
            "collection": ("STATUS counters; after a segment ends with compare failures, also that segment's "
                           "retained fail log (up to 341 entries); sample payloads are not collected"),
            "fail_log_collection": plan_dict.get("fail_log_collection"),
            "hold_segments": [s["index"] for s in plan_dict["segments"]
                              if s["terminal_state"] == c.STATE_HOLD_AWAITING_NEXT],
            "output_dir": str(output),
            "role_pins": role_pins(bundle),
            "limitations": [
                "Execution preview: these checks do not verify wiring or waveform timing. "
                "See the firmware release notes for the hardware tests performed.",
                "Cancel stops further host requests and closes the port; no STOP/release is sent and outputs "
                "or acquisition may continue.",
                "No automatic retry, reconnect or recovery; a failed run keeps its result directory.",
                "Completion and comparison are separate: COMPLETED is not a DUT PASS.",
            ],
            "external": external["summary"] if external else None,
            "control": None,
            "accessories": {"controller": control["controller"] if control else None,
                            "external": external["summary"]["identifier"] if external else None},
        }
        if control:
            summary["control"] = self._control_summary(control["controller"], controller,
                                                       _thaw(control["plan_json"]), external is not None)
        nonce = secrets.token_urlsafe(18)
        acknowledgements = run_acknowledgements(plan_dict)
        with self.lock:
            now = time.monotonic()
            self.confirmations = {k: v for k, v in self.confirmations.items() if v["expires"] > now}
            self.confirmations[nonce] = {"expires": now + CONFIRM_TTL_S, "bundle": str(bundle), "sha": sha,
                                         "acknowledgements": sorted(acknowledgements),
                                         "device": request.get("device"), "simulated": simulated,
                                         "device_json": _frozen(device.to_dict()),
                                         "output": str(output),
                                         "external": ({"config": external["config"], "spec_json": external["spec_json"],
                                                       "config_sha256": external["summary"]["config_sha256"]}
                                                      if external else None),
                                         "control": control, "summary": summary}
        return {"nonce": nonce, "expires_in_s": CONFIRM_TTL_S, "summary": summary,
                "acknowledgements": acknowledgements}

    def start_run(self, request: dict) -> Job:
        acks = request.get("acknowledgements") or {}
        if any(acks.get(key) is not True for key in ACKNOWLEDGEMENTS if key != FAIL_LOG_ACK):
            raise RunnerError("every confirmation checkbox must be ticked")
        nonce = request.get("nonce") or ""
        with self.transition:  # one confirmation -> at most one job, across tabs
            with self.lock:
                confirmation = self.confirmations.pop(nonce, None)  # single use, even if the start fails below
            if confirmation is None or confirmation["expires"] < time.monotonic() or confirmation.get("operation"):
                raise RunnerError("this confirmation was already used or has expired; prepare the run again")
            if any(acks.get(key) is not True for key in confirmation["acknowledgements"]):
                raise RunnerError("every confirmation checkbox must be ticked; prepare the run again")
            work, title = self._run_work(confirmation)
            return self.jobs.start("run", title, work, device=True)

    def _run_work(self, confirmation: dict):
        if Path(confirmation["output"]).exists():
            raise RunnerError("the result directory appeared meanwhile; prepare the run again")
        # Execute exactly what was confirmed: the frozen snapshots. A binding or accessory selection that was
        # replaced, deleted or recreated differently since then rejects here, before any job, launch or port open.
        current, simulated = self._device(confirmation["device"] or "")
        if simulated != confirmation["simulated"] or _frozen(current.to_dict()) != confirmation["device_json"]:
            raise RunnerError("the device binding changed after this run was confirmed; prepare the run again")
        device = DeviceConfig.from_dict(_thaw(confirmation["device_json"]))
        bundle, sha, output = Path(confirmation["bundle"]), confirmation["sha"], Path(confirmation["output"])
        spec = None
        if confirmation["external"]:
            frozen = confirmation["external"]
            path = Path(frozen["config"])
            try:
                live = load_external_accessory(path)
                live_sha = identity(path)["sha256"]
            except (RunnerError, OSError, ValueError) as exc:
                raise RunnerError(f"the external accessory config cannot be read again ({exc}); prepare the run "
                                  "again") from exc
            if _frozen(live.to_dict()) != frozen["spec_json"] or live_sha != frozen["config_sha256"]:
                raise RunnerError("the external accessory selection changed after this run was confirmed; prepare "
                                  "the run again")
            spec = ExternalAccessorySpec.from_dict(_thaw(frozen["spec_json"]))
        control = None
        if confirmation["control"]:
            frozen = confirmation["control"]
            current_controller, _ = self._controller(frozen["controller"] or "", simulated)
            if _frozen(current_controller.to_dict()) != frozen["controller_json"]:
                raise RunnerError("the GPIO controller binding changed after this run was confirmed; prepare again")
            control = {"controller": ControllerConfig.from_dict(_thaw(frozen["controller_json"])),
                       "plan": _thaw(frozen["plan_json"])}
        self._check_ports(device, simulated, "Nothing was started; reconnect or fix the binding, then prepare "
                                             "the run again.")

        def work(job: Job) -> dict:
            # This GUI step is only handoff to the SDK, not proof that bundle
            # verification succeeded. The SDK now reports its own preparation
            # stages, file counts and heartbeats before opening the STPG port.
            job.progress({"stage": "run_verify", "event": "start", "elapsed_s": 0.0,
                          "segments": len(confirmation["summary"]["segments"])})
            started = [False]

            def progress(event: Any) -> None:
                if not started[0]:
                    started[0] = True
                    job.progress({"stage": "run_verify", "event": "done", "elapsed_s": 0.0})
                job.progress(event)

            injected: dict[str, Any] = {}
            if simulated:
                injected = {"stream_factory": lambda _config: SimulatedStpg(), "port_provider": simulated_ports,
                            "sleep": lambda seconds: time.sleep(min(seconds, 0.05))}
                if control is not None:
                    injected.update(controller_stream_factory=lambda _config: SimulatedController(),
                                    controller_port_provider=lambda: controller_ports(control["controller"]))
            options = dict(manifest_sha256=sha, device=device, output_dir=output, confirm_run=True,
                           controller=control["controller"] if control else None,
                           control_plan=control["plan"] if control else None,
                           on_progress=progress, cancel=job.token, **injected)
            if spec is not None:
                result = run_with_external_accessory(bundle, spec=spec, **options)
            else:
                result = run_with_accessories(bundle, **options)
            return self._accessory_view(result, output, bundle, simulated)

        return work, f"run {bundle.name[:12]} on {confirmation['device']}"

    def _accessory_view(self, result: dict, output: Path, bundle: Path, simulated: bool) -> dict:
        """STPG, controller and external accessory shown as separate axes, plus the CLI-equivalent overall status."""

        run_file = Path(result["run_result_file"]) if result.get("run_result_file") else None
        if run_file is not None and run_file.is_file():
            view = self._run_view(read_run_result(run_file), run_file, simulated)
        else:
            view = {"run": result["run"], "segments": [], "result_file": None, "simulated": simulated,
                    "completion": None}
        external = result.get("external")
        stpg_dir = output / "stpg" if external is not None else output
        if view["run"] is None:
            # No STPG result came back. run_bundle reserves its run folder before any port access, so without that
            # folder the SDK never reached the STPG (e.g. the accessory did not answer READY); with it, it is unknown.
            reached = any((stpg_dir / name).exists() for name in ("run.json", "run.json.gz", "run"))
            view["run"] = {"outcome": "UNKNOWN" if reached else "NOT_STARTED", "comparison": "UNOBSERVED",
                           "completed_segments": 0, "planned_segments": None, "errors": [], "result_persisted": False}
        view["control"] = (self._gpio_view(result["control"], stpg_dir / "control.json", run_attached=True)
                           if result.get("control") else None)
        if view["control"] is not None and view.get("start_check"):
            # The STPG refused before its first upload, so no segment boundary (and no GPIO action) was reached.
            view["control"]["not_started"] = "the STPG refused the run at its start check, before the first GPIO action"
        if external is not None:
            spec = (external.get("prepared") or {}).get("spec") or {}
            view["external"] = {
                "identifier": spec.get("identifier"), "mode": external.get("mode"),
                "outcome": external.get("outcome"), "adopted": external.get("adopted") is True,
                "result_persisted": external.get("result_persisted") is True,
                "cancel_requested": external.get("cancel_requested") is True,
                "errors": list(external.get("errors") or []), "returncode": external.get("returncode"),
                "result_file": external.get("result_file"),
                "session_file": str(output / "accessory" / "session.json"),
                "output_dir": str(output / "accessory"),
                "hardware_effects_possible": external.get("hardware_effects_possible") is True,
                # the accessory's own diagnostic text (a bounded prefix the SDK kept); shown, never interpreted
                "stderr_text": bytes.fromhex(external.get("stderr_hex") or "")[:4000].decode("utf-8", "replace").strip(),
                # Unadmitted bytes of a cancelled exchange, including buffered bytes received before the cancel.
                "unadmitted_stdout_bytes": int(external.get("unadmitted_stdout_bytes") or 0),
            }
        code = accessory_exit(result)
        view["overall"] = {"exit_code": code, "text": EXIT_TEXT.get(code, "unknown")}
        return view



    @staticmethod
    def _gpio_view(report: dict, path: Path, *, run_attached: bool) -> dict:
        """Controller evidence for display: software ACK states only, with every uncertainty spelled out."""

        transport = report.get("transport") or {}
        acknowledged = transport.get("last_acknowledged_states") or {}
        requests, acks = [], 0
        for record in transport.get("journal") or []:
            if record.get("event") == "state.acknowledged":
                acks += 1
            if record.get("event") != "write.begin":
                continue
            text = bytes.fromhex(record.get("planned_hex") or "").decode("ascii", errors="replace").strip()
            match = re.fullmatch(r"(?:@[0-9a-f]{16}:\d+ )?(\d+)([lzh])", text)
            if match:
                requests.append({"pin": int(match[1]), "state": {"l": "LOW", "z": "Hi-Z", "h": "HIGH"}[match[2]],
                                 "written_bytes": record.get("written_bytes"), "error": record.get("error")})
        for index, item in enumerate(requests):  # each set waits for its ACK before the next request
            item["acknowledged"] = index < acks
        uncertain = []
        if report.get("outcome") != "COMPLETED":
            for item in requests[acks:]:
                uncertain.append(f"GPIO{item['pin']} {item['state']} was requested but not acknowledged: it may "
                                 "or may not have been applied")
            if not run_attached and report.get("pulse_ms") is not None:
                pin = report.get("pin")
                if acknowledged.get(str(pin)) == "LOW" or any(r["state"] == "LOW" for r in requests[acks:]):
                    uncertain.append(f"RUN (GPIO{pin}) may still be held LOW: the target can stay in reset. "
                                     "Nothing releases it automatically; decide the next step yourself.")
            if not requests[acks:] and requests:
                uncertain.append("Every request was acknowledged, but the operation did not complete cleanly "
                                 "(see errors); acknowledged levels stay as they are.")
        # The identification lines the SDK had already read (SDK R9). Shown on this computer only: a line can hold
        # the controller's identity or token. They say what was received, not why it was refused.
        journal = transport.get("journal") or []
        lines = [record for record in journal if isinstance(record, dict) and record.get("event") == "identity.line"]
        identity_lines = [{"line": _bounded(record.get("line")), "disposition": str(record.get("disposition"))}
                          for record in lines[-IDENTITY_LINES_SHOWN:]]
        return {"outcome": report.get("outcome"), "result_persisted": report.get("result_persisted") is True,
                "identity_lines": identity_lines, "identity_lines_total": len(lines),
                "errors": report.get("errors") or [], "acknowledged_actions": report.get("actions"),
                "planned_actions": (report.get("plan") or {}).get("actions") if run_attached else None,
                "pin": report.get("pin"), "state": report.get("state"), "pulse_ms": report.get("pulse_ms"),
                "settle_ms": report.get("settle_ms"), "requests": requests, "last_acknowledged_states": acknowledged,
                "close_attempts": transport.get("close_attempts"), "close_successes": transport.get("close_successes"),
                "open_attempted": any(record.get("event") == "open.begin" for record in transport.get("journal") or []),
                "legacy_uncorrelated": transport.get("legacy_uncorrelated"),
                "electrical_release_verified": False, "physical_readback": False, "uncertain": uncertain,
                "protocol": (report.get("config") or {}).get("protocol"),
                "result_file": str(path) if path.is_file() else None}

    def _control_summary(self, name: str, controller: ControllerConfig, actions: dict, with_external: bool) -> dict:
        legacy = controller.protocol == "legacy-3"
        rows = []
        for action in actions["actions"]:
            pin = action["pin"]
            rows.append({**action, "role": PIN_ROLES.get(pin, "user-defined"),
                         "physical": f"legacy channel {pin} -> pin {controller.legacy_pins[pin]}" if legacy else
                         f"GPIO{pin}", "high_permitted": pin in controller.allow_high})
        notes = [
            "before_run actions run after that segment's upload is accepted and checkpointed, immediately before "
            "RUN; after_terminal actions run only after a valid saved DONE/HOLD, never after ERROR.",
            "A failed or cancelled action stops every later GPIO and STPG request. No level is restored or "
            "released automatically, including in cleanup; the controller port is closed once.",
            "ACKs are software states reported by the controller, not measured voltages.",
            "GPIO0/GPIO1 (RUN) are never touched by a run; reset uses the separate manual controls.",
        ]
        if with_external:
            notes.append("At the external accessory's segment the order is: GPIO before_run actions, then the "
                         "accessory's ARMED, then RUN.")
        if legacy:
            notes.append("legacy-3: replies carry no request tag; an unexpected line rejects without retry.")
        return {"controller": name, "port": controller.port, "vid": controller.vid, "pid": controller.pid,
                "serial_number": controller.serial_number, "protocol": controller.protocol,
                "allow_high": list(controller.allow_high), "legacy_pins": list(controller.legacy_pins),
                "lease_key": physical_identity_key(controller), "actions": rows, "notes": notes}

    @staticmethod
    def _run_view(report: dict, path: Path, simulated: bool) -> dict:
        summary = public_summary(report)
        segments = [{"index": item["index"], "outcome": item["outcome"], "comparison": item.get("comparison"),
                     "expected": {k: item["expected"][k] for k in ("events", "samples", "compares")},
                     "terminal": {k: item["terminal"][k] for k in ("state", "run_progress", "sample_count",
                                                                   "fail_count", "result_flags", "error_code")},
                     "error_name": _ERR_NAMES.get(item["terminal"]["error_code"]) if item["terminal"]["error_code"]
                     else None,
                     "fail_log": fail_log_summary(item)}
                    for item in report.get("segments", [])]
        link = (report.get("capture") or {}).get("metadata")
        return {"run": summary, "segments": segments, "result_file": str(path),
                "completion": (report.get("plan") or {}).get("completion"),
                "transport_kind": report.get("transport_kind"), "simulated": simulated,
                "exchanges": device_exchanges(report), "start_check": start_check_refusal(report),
                "left_loaded": upload_without_run(report),
                "identity_mismatch": identity_mismatch(report), "stopped_at": run_stop_point(report),
                "fail_log_stop": fail_log_stop(report), "result_dir": str(_result_entry(path)),
                "capture_link": {"capture_dir": link["capture_dir"], "plan_sha256": link["plan_sha256"]}
                if link else None}

    # ---- manual GPIO set / HW reset (separate, explicit; never part of a run, cleanup or retry) --------------
    def prepare_control(self, request: dict) -> dict:
        operation = request.get("operation")
        if operation not in ("reset", "set"):
            raise RunnerError("operation must be reset or set")
        controller, simulated = self._controller(request.get("controller") or "", None)
        allow_run_high = request.get("allow_run_high", False)
        if type(allow_run_high) is not bool:
            raise RunnerError("allow_run_high must be an explicit boolean")
        settle = request.get("settle_ms")
        settle = 0 if settle is None else settle  # optional; shown explicitly in the confirmation
        pulse = None
        state = None
        if operation == "reset":
            kind = request.get("target")
            if kind not in ("stpg", "la"):
                raise RunnerError("reset target must be stpg (GPIO0) or la (GPIO1)")
            pin = 0 if kind == "stpg" else 1
            pulse = request.get("pulse_ms")
            if type(pulse) is not int or not 1 <= pulse <= 5000:
                raise RunnerError("declare the LOW pulse width explicitly: an integer 1..5000 ms (no default)")
            if allow_run_high:
                raise RunnerError("a reset is LOW then Hi-Z; it never drives HIGH")
        else:
            pin, state = request.get("pin"), request.get("state")
            if type(pin) is not int:
                raise RunnerError("choose a GPIO number")
            kind = {0: "stpg", 1: "la"}.get(pin)
            if settle != 0:
                raise RunnerError("a settle time belongs to a reset only")
        if type(settle) is not int or not 0 <= settle <= 10000:
            raise RunnerError("settle time must be an integer 0..10000 ms")
        controller.validate_action(pin, "LOW" if operation == "reset" else state,
                                   allow_run_high=allow_run_high)  # never default an invalid set state
        target = None
        target_name = request.get("target_binding") or ""
        if kind is not None:
            target = self._target(kind, target_name, simulated)
            ensure_separate(controller, [target])
        root = self.results_dir
        output = (root / f"{_stamp()}-control-{operation}-gpio{pin}-{secrets.token_hex(2)}").resolve()
        if output.exists():
            raise RunnerError(f"{output} exists; every operation needs a new result directory")
        target_text = None
        if target is not None:
            target_text = (f"{'STPG' if kind == 'stpg' else 'analyzer'} {target_name} · {target.port} · "
                           f"{target.vid:04x}:{target.pid:04x} serial {target.serial_number}")
        steps = [f"lease the controller {controller.port} ({controller.vid:04x}:{controller.pid:04x} serial "
                 f"{controller.serial_number}, protocol {controller.protocol}) and check its USB metadata"]
        if target is not None:
            steps.insert(0, f"lease the target {target_text} (it is NOT opened, identified or queried; an active "
                            "owner rejects before the controller is touched)")
        if operation == "reset":
            steps += [f"set GPIO{pin} LOW", f"wait {pulse} ms (a host delay, not a calibrated pulse width)",
                      f"set GPIO{pin} Hi-Z (the board pull-up is expected to release RUN; not measured)"]
            if settle:
                steps.append(f"wait {settle} ms settle time (not an enumeration wait)")
        else:
            steps.append(f"set GPIO{pin} {state}")
        steps.append("close the controller port once; no further pin command in any case")
        warnings = ["ACKs are software states; voltage, release and target CPU state are not measured.",
                    "Nothing is run, re-identified, released or retried automatically afterwards."]
        acks = {}
        if operation == "reset":
            name = "STPG" if kind == "stpg" else "logic analyzer"
            warnings.insert(0, f"A failure or cancel after the LOW can leave RUN LOW: the {name} stays in reset.")
            acks["wired"] = (f"Controller GPIO{pin} is wired to the RUN input of exactly this {name}; the program "
                             "cannot check wiring (the current debug board keeps RUN disconnected).")
            acks["interrupt"] = f"Resetting interrupts whatever the {name} is doing; no other program is using it."
        else:
            role = PIN_ROLES.get(pin, "user-defined")
            acks["load"] = f"The circuit on GPIO{pin} ({role}) tolerates {state}; the program cannot check loads."
            warnings.insert(0, f"GPIO{pin} keeps {state} until changed explicitly; nothing restores it.")
            if pin in (0, 1):
                acks["run_line"] = (f"GPIO{pin} is a RUN line: I set it directly instead of using the reset "
                                    "operation, knowing what the target does at this level.")
            if pin in (0, 1) and state == "HIGH":
                acks["run_high"] = ("Exceptional: I drive the RUN line actively HIGH (allowed by this binding's "
                                    "allow_high list and this explicit override).")
        acks["aftermath"] = "I will inspect the result and decide any next step myself."
        summary = {"operation": operation, "controller": request.get("controller"), "simulated": simulated,
                   "pin": pin, "role": PIN_ROLES.get(pin, "user-defined"), "state": state, "pulse_ms": pulse,
                   "settle_ms": settle, "allow_run_high": allow_run_high, "target": target_text,
                   "protocol": controller.protocol, "allow_high": list(controller.allow_high),
                   "lease_keys": [physical_identity_key(controller)] + ([physical_identity_key(target)] if target else []),
                   "steps": steps, "warnings": warnings, "output_dir": str(output)}
        nonce = secrets.token_urlsafe(18)
        with self.lock:
            now = time.monotonic()
            self.confirmations = {k: v for k, v in self.confirmations.items() if v["expires"] > now}
            self.confirmations[nonce] = {
                "expires": now + CONFIRM_TTL_S, "operation": operation, "simulated": simulated,
                "controller": request.get("controller"), "controller_json": _frozen(controller.to_dict()),
                "target_kind": kind, "target": target_name if target is not None else None,
                "target_json": _frozen(target.to_dict()) if target is not None else None,
                "pin": pin, "state": state, "pulse_ms": pulse, "settle_ms": settle,
                "allow_run_high": allow_run_high, "acks": sorted(acks), "output": str(output), "summary": summary}
        return {"nonce": nonce, "expires_in_s": CONFIRM_TTL_S, "summary": summary, "acknowledgements": acks}

    def start_control(self, request: dict) -> Job:
        nonce = request.get("nonce") or ""
        with self.transition:
            with self.lock:
                confirmation = self.confirmations.pop(nonce, None)
            if (confirmation is None or confirmation["expires"] < time.monotonic()
                    or not confirmation.get("operation")):
                raise RunnerError("this confirmation was already used or has expired; prepare the operation again")
            acks = request.get("acknowledgements") or {}
            if any(acks.get(key) is not True for key in confirmation["acks"]):
                raise RunnerError("every confirmation checkbox must be ticked")
            output = Path(confirmation["output"])
            if output.exists():
                raise RunnerError("the result directory appeared meanwhile; prepare the operation again")
            current, simulated = self._controller(confirmation["controller"] or "", None)
            if simulated != confirmation["simulated"] or _frozen(current.to_dict()) != confirmation["controller_json"]:
                raise RunnerError("the GPIO controller binding changed after confirmation; prepare again")
            controller = ControllerConfig.from_dict(_thaw(confirmation["controller_json"]))
            target = None
            if confirmation["target_json"] is not None:
                kind = confirmation["target_kind"]
                live = self._target(kind, confirmation["target"], simulated)
                if _frozen(live.to_dict()) != confirmation["target_json"]:
                    raise RunnerError("the target binding changed after confirmation; prepare again")
                frozen = _thaw(confirmation["target_json"])
                target = DeviceConfig.from_dict(frozen) if kind == "stpg" else ExternalDeviceBinding.from_dict(frozen)
            options = {key: confirmation[key] for key in ("pin", "state", "pulse_ms", "settle_ms", "allow_run_high")}

            def work(job: Job) -> dict:
                injected: dict[str, Any] = {}
                if simulated:
                    peer = SimulatedController()
                    injected = {"stream_factory": lambda _config: peer,
                                "port_provider": lambda: controller_ports(controller),
                                "sleep": lambda seconds: time.sleep(min(seconds, 0.05))}
                report = manual_control(controller, output_dir=output, target=target, confirm_control=True,
                                        cancel=job.token, **options, **injected)
                return {"control": self._gpio_view(report, output / "control.json", run_attached=False),
                        "operation": confirmation["operation"], "simulated": simulated,
                        "summary": confirmation["summary"]}

            title = (f"reset {confirmation['target_kind'].upper()} via GPIO{options['pin']}"
                     if confirmation["operation"] == "reset" else f"set GPIO{options['pin']} {options['state']}")
            return self.jobs.start("control", title, work, device=True)

    # ---- plan files shared with the CUI (same schemas as stpg-runner / stpg-capture) -----------------------
    def control_plan_load(self, request: dict) -> dict:
        plan = read_json(Path(request.get("path") or "").expanduser())
        sha = request.get("manifest_sha256") or ""
        if not isinstance(plan, dict) or plan.get("kind") != CONTROL_PLAN_KIND:
            raise RunnerError(f"not a {CONTROL_PLAN_KIND} file")
        if plan.get("manifest_sha256") != sha:
            raise RunnerError("this GPIO action plan is bound to a different bundle manifest")
        if request.get("controller"):
            controller, _ = self._controller(request["controller"], request["controller"] == SIMULATED or None)
            run_plan = prepare_run(Path(request["bundle"]).expanduser().resolve(), manifest_sha256=sha)
            plan = validate_control_plan(plan, controller, run_plan)
        return {"plan": plan}

    def save_plan(self, request: dict) -> dict:
        """Write a GPIO action plan for the CUI; never overwrites, never inside a bundle."""

        value = request.get("plan")
        if not isinstance(value, dict) or value.get("kind") != CONTROL_PLAN_KIND:
            raise RunnerError("nothing to save: a GPIO action plan is required")
        path = Path(request.get("path") or "").expanduser().resolve()
        if not path.name.endswith(".json"):
            raise RunnerError("save the plan as a .json file")
        if path.exists():
            raise RunnerError(f"{path} exists; choose a new file name")
        if any((parent / BUNDLE_NAME).is_file() for parent in (path.parent, *path.parent.parents)):
            raise RunnerError("plans are local files; do not write them inside an immutable bundle")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, value)
        return {"path": str(path), **identity(path)}

    def cancel(self, job_id: str) -> Job:
        return self.jobs.cancel(job_id)

    # ---- results ----------------------------------------------------------------------------
    def results(self, root: str | None) -> dict:
        folder = Path(root).expanduser() if root else self.results_dir
        items = []
        if folder.is_dir():
            for entry in sorted(folder.iterdir(), key=lambda p: p.name, reverse=True)[:100]:
                # STPG-only and controller runs: run.json at the top; with an external accessory: stpg/run.json and
                # accessory/session.json. run/run.json is the layout of results from earlier versions.
                run_folder = next((folder for folder in (entry, entry / "stpg", entry / "run")
                                   if any((folder / name).exists() for name in ("run.json", "run.json.gz"))), None)
                item: dict[str, Any] = {"path": str(entry), "name": entry.name}
                if run_folder is not None:
                    try:
                        report = read_run_result(run_folder)
                        item["run"] = public_summary(report)
                        item["transport_kind"] = report.get("transport_kind")
                    except (RunnerError, KeyError) as exc:
                        item["error"] = str(exc)
                session_json = entry / "accessory" / "session.json"
                if session_json.is_file():
                    try:
                        session = read_json(session_json)
                        item["external"] = {"outcome": session.get("outcome"), "mode": session.get("mode"),
                                            "adopted": session.get("adopted") is True,
                                            "identifier": ((session.get("prepared") or {}).get("spec") or {}).get(
                                                "identifier"), "path": str(session_json)}
                    except RunnerError as exc:
                        item["external_error"] = str(exc)
                control_json = next((path for path in (entry / "control.json", entry / "stpg" / "control.json")
                                     if path.is_file()), entry / "control.json")
                if control_json.is_file():
                    try:
                        control = read_json(control_json)
                        item["control"] = {"kind": control.get("kind"), "outcome": control.get("outcome"),
                                           "result_persisted": control.get("result_persisted") is True,
                                           "pin": control.get("pin"), "state": control.get("state"),
                                           "pulse_ms": control.get("pulse_ms"), "path": str(control_json)}
                    except RunnerError as exc:
                        item["control_error"] = str(exc)
                if "run" in item or "external" in item or "control" in item or "error" in item:
                    items.append(item)
        return {"root": str(folder), "items": items}


    # ---- readable results (offline; the SDK's STIL description, never a device) -----------------------------
    def describe_run(self, request: dict) -> dict:
        """The STIL details of one result: the SDK's ``describe_result`` with the bundle of that exact manifest.

        The bundle is looked for only where it belongs (the open bundle, the builds of the results' project) and
        selected by its exact manifest hash; ``describe_result`` verifies it. Nothing is guessed: a result without a
        plan, a missing bundle or a map the SDK cannot use is reported as unavailable, with the reason.
        """

        entry = Path(request.get("result") or "").expanduser().resolve()
        folder = run_folder_of(entry)
        if folder is None:
            raise RunnerError(f"{entry} holds no STPG run result")
        report = read_run_result(folder)
        base: dict[str, Any] = {"result": str(entry), "name": entry.name, "available": False}
        plan = report.get("plan")
        sha = plan.get("manifest_sha256") if isinstance(plan, dict) else None
        if not isinstance(sha, str) or not SHA256.fullmatch(sha):
            return {**base, "reason": "This run stopped before its execution plan was made: it has no segments to "
                                      "match with the STIL."}
        base["manifest_sha256"] = sha
        bundle = find_bundle(sha, bundle_candidates(entry, request.get("bundle")))
        if bundle is None:
            return {**base, "reason": f"The bundle this run used (manifest {sha[:12]}…) is not open and is not among "
                                      "the builds of the project next to these results. Open that bundle, then show "
                                      "the STIL details again."}
        try:
            description = describe_result(bundle, manifest_sha256=sha, result=report)
            sources = bundle_source_paths(bundle)
        except (RunnerError, ValueError, KeyError, OSError) as exc:
            return {**base, "bundle_dir": str(bundle),
                    "reason": f"The SDK could not match this result with the STIL ({exc}). Nothing is guessed."}
        return {**base, "available": True, "bundle_dir": str(bundle), "description": description,
                "text": describe_text(description, result_name=entry.name),
                "stil": str(sources["stil"]), "pin_map": str(sources["pin_map"]) if sources.get("pin_map") else None}

    def save_description(self, request: dict) -> dict:
        """Write STIL details to a new file; rehash inputs, reuse only unchanged matching."""

        target = Path(request.get("path") or "").expanduser().resolve()
        if not target.name.endswith(".txt"):
            raise RunnerError("save the STIL details as a .txt file")
        if target.exists():
            raise RunnerError(f"{target} exists; choose a new file name")
        if any((parent / BUNDLE_NAME).is_file() for parent in (target.parent, *target.parent.parents)):
            raise RunnerError("do not write into an immutable bundle; choose a folder outside it")
        described = self.describe_run(request)
        if not described["available"]:
            raise RunnerError(described["reason"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as stream:  # an existing file is never replaced
            stream.write(described["text"])
        return {"path": str(target), "bytes": target.stat().st_size}

    # ---- explicit update of a bundle for the installed SDK, without recompiling --------------------------------
    def start_rebind(self, bundle: str, manifest_sha256: str) -> Job:
        """The SDK's ``rebind_bundle``: the old bundle stays as it is; a new one is written beside it and checked.

        Never automatic and never a fallback of Run: the user starts it for one bundle. The SDK fingerprints and
        Pythons it records go to the project log, not the page.
        """

        root = Path(bundle).expanduser().resolve()
        if not isinstance(manifest_sha256, str) or not SHA256.fullmatch(manifest_sha256):
            raise RunnerError("updating a bundle needs its exact manifest SHA-256")
        in_project = root.parent.name == "build" and (root.parent.parent / PROJECT_FILE).is_file()
        project_dir = root.parent.parent if in_project else None

        def work(job: Job) -> dict:
            if project_dir is not None:
                project_log(project_dir, {"event": "rebind.start", "bundle": _relative(root, project_dir),
                                          "manifest_sha256": manifest_sha256, "sdk": _sdk_info()})
            try:
                result = rebind_bundle(root, manifest_sha256=manifest_sha256, on_progress=job.progress,
                                       cancel=job.token)
            except BaseException as exc:
                if project_dir is not None:
                    project_log(project_dir, {"event": "rebind.end", "error": f"{type(exc).__name__}: {exc}"})
                raise
            manifest = read_json(Path(result.bundle_dir) / BUNDLE_NAME)
            if project_dir is not None:
                project_log(project_dir, {"event": "rebind.end", "bundle": _relative(result.bundle_dir, project_dir),
                                          "manifest_sha256": result.manifest_sha256, "no_op": result.cache_hit,
                                          "rebind": manifest["binding"].get("rebind")})
            return {**result.to_dict(), "no_op": result.cache_hit,
                    "bundle": self.open_bundle(str(result.bundle_dir), result.manifest_sha256)}

        return self.jobs.start("rebind", f"update bundle {root.name[:12]} for this SDK", work, key=f"rebind:{root}")

    def start_controller_identify(self, name: str) -> Job:
        """Open the GPIO controller for the SDK's identity/status exchange only: no pin command is written.

        Not electrically read-only: the port is opened and closed and a v1 sketch takes a new session token.
        The SDK saves identify.json under the results folder before contact and keeps it on close failure.

        rp2040-gpio-v1 sends ``i <token>`` and needs the STPG_GPIO_V1 line; legacy-3 sends ``s`` and needs the
        three expected pins. Another sketch is reported, never adapted to: the protocol is not guessed.
        """

        controller, simulated = self._controller(name, None)

        def work(job: Job) -> dict:
            injected: dict[str, Any] = {}
            if simulated:
                peer = SimulatedController()
                injected = {"stream_factory": lambda _config: peer, "port_provider": lambda: controller_ports(controller)}
            output = self.results_dir / f"{_stamp()}-controller-identify-{secrets.token_hex(4)}"
            report = identify_controller(controller, output_dir=output, confirm_identify=True,
                                         cancel=job.token, **injected)
            lines = report["received_lines"]
            hint = None
            if report["status"] == "REJECT" and controller.protocol == "rp2040-gpio-v1" and any(
                    line.startswith(("Invalid channel", "Invalid command")) for line in lines):
                hint = ("the selected protocol did not match. This reply resembles the old three-channel sketch; "
                        "if independently confirmed, register legacy-3 with its known physical mapping. "
                        "No GPIO command or automatic fallback was sent; USB open/close effects are unmeasured.")
            return {**report, "simulated": simulated, "error": "; ".join(report["errors"]) or None,
                    "hint": hint, "outcome": "CANCELLED" if report["status"] == "CANCELLED" else
                    "COMPLETED" if report["status"] == "IDENTIFIED" else "REJECT"}

        return self.jobs.start("controller_identify", f"identify GPIO controller {name}", work, device=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def python_info() -> str:
    return f"{sys.executable} {platform.python_version()}"
