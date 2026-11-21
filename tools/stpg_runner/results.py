"""Run evidence storage and offline presentation; never opens a device."""

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
from typing import Any

from .artifacts import RunnerError, canonical_json, read_json, write_json

RUN_RESULT_NAME = "run.json.gz"
LEGACY_RUN_RESULT_NAME = "run.json"

# Process-owned, successful offline descriptions only, never persisted or read
# from a bundle. Do not retain raw journals, maps or pattern binaries here.
_DESCRIPTION_LIMIT = 8
_DESCRIPTION_BYTES = 8 * 1024 * 1024
_description_lock = threading.Lock()
_descriptions: OrderedDict[tuple, tuple[bytes, bytes]] = OrderedDict()


def _remember_description(key: tuple, manifest: bytes, description: bytes) -> None:
    with _description_lock:
        _descriptions.pop(key, None)
        if len(manifest) + len(description) > _DESCRIPTION_BYTES:
            return
        _descriptions[key] = (manifest, description)
        while (len(_descriptions) > _DESCRIPTION_LIMIT
               or sum(len(m) + len(d) for m, d in _descriptions.values()) > _DESCRIPTION_BYTES):
            _descriptions.popitem(last=False)


def run_result_path(folder_or_path: Path) -> Path:
    """Resolve one legacy/new record; ambiguity is an error even for an explicit path."""
    path = Path(folder_or_path)
    folder = path if path.is_dir() else path.parent
    if not path.is_dir() and path.name not in (RUN_RESULT_NAME, LEGACY_RUN_RESULT_NAME):
        raise RunnerError("expected a result folder, run.json.gz or legacy run.json")
    candidates = [folder / name for name in (RUN_RESULT_NAME, LEGACY_RUN_RESULT_NAME)
                  if (folder / name).exists() or (folder / name).is_symlink()]
    if len(candidates) != 1:
        raise RunnerError("missing or ambiguous run result (run.json and run.json.gz must not coexist)")
    result = candidates[0]
    if result.is_symlink() or not result.is_file():
        raise RunnerError("run result must be a regular non-symlink file")
    if not path.is_dir() and path != result:
        raise RunnerError("the explicitly named run result does not exist")
    return result


def read_run_result(folder_or_path: Path, *, expected_sha256: str | None = None) -> dict[str, Any]:
    """Strict JSON reader shared by CLI, GUI and companion. Hash pins stored bytes."""
    result = read_json(run_result_path(folder_or_path), expected_sha256=expected_sha256)
    if (result.get("kind") != "stpg_runner_run_result"
            or type(result.get("schema_version")) is not int or result["schema_version"] != 1):
        raise RunnerError("unsupported run result kind/schema")
    return result


def write_run_result(folder: Path, result: dict[str, Any]) -> Path:
    """Atomic gzip-only checkpoint in an already exclusively reserved run folder."""
    folder = Path(folder)
    legacy, output = folder / LEGACY_RUN_RESULT_NAME, folder / RUN_RESULT_NAME
    if legacy.exists() or legacy.is_symlink() or output.is_symlink():
        raise RunnerError("refusing an ambiguous or symlink run checkpoint")
    write_json(output, result)
    return output


def describe_result(bundle_dir: Path, *, manifest_sha256: str,
                    result: dict[str, Any] | Path, reuse: bool = True,
                    on_progress=None, cancel=None) -> dict[str, Any]:
    """Offline STIL-correlated evidence, not sample/DUT or physical correctness proof.

    Only whitelisted source fields are returned. Pattern/signal names can still
    be confidential; this is path/USB-redacted, not permission to publish data.

    Matching is reused only within this process for the same result snapshot,
    bundle location, exact manifest and installed SDK. Every call rehashes the
    entire inventory and checks the exact file set, including on a cache hit.
    No timestamp-only shortcut or external receipt is accepted. ``reuse=False``
    forces fresh semantic validation and matching. Optional progress/cancellation
    uses host-only Reporter boundaries; it cannot interrupt a running decoder.
    """
    from .artifacts import confined_path
    from .build import BUNDLE_NAME, _verify_inventory, tool_fingerprint
    from .progress import Reporter
    from stpg.pattern import rate_vector_decode_scope

    if not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in manifest_sha256):
        raise RunnerError("description requires an exact bundle manifest SHA-256")
    progress = Reporter(on_progress, cancel)
    progress.cancel.check()
    root = Path(bundle_dir).resolve()
    # Take ownership before hashing/matching, so caller mutations cannot poison
    # a receipt. Paths are read once; the GUI may pass its already-read snapshot.
    report = deepcopy(result) if isinstance(result, dict) else read_run_result(result)
    try:
        report_sha = hashlib.sha256(canonical_json(report)).hexdigest()
    except (TypeError, ValueError, RecursionError) as exc:
        raise RunnerError("result must be a finite JSON object") from exc
    manifest = read_json(confined_path(root, BUNDLE_NAME), expected_sha256=manifest_sha256)
    manifest_bytes = canonical_json(manifest)
    fingerprint = tool_fingerprint()
    key = (root, manifest_sha256, fingerprint, report_sha)
    with _description_lock:
        cached = _descriptions.get(key) if reuse is True else None
    try:
        if cached is not None and cached[0] == manifest_bytes:
            output = progress.perform("result_description_reuse", lambda: json.loads(cached[1]))
        else:
            def match():
                # Share the existing bounded exact-byte decoder only for this
                # synchronous validation/mapping; no patterns survive the scope.
                with rate_vector_decode_scope():
                    return _describe_result(root, manifest_sha256=manifest_sha256,
                                            result=report, reuse=reuse)
            output = progress.perform("result_description", match)
        # Quiet final guard after callbacks, also on reuse. A callback is trusted
        # application code, but it must not make an edited bundle look unchanged.
        _verify_inventory(root, manifest, manifest_sha256=manifest_sha256,
                          progress=Reporter(None, progress.cancel))
        if tool_fingerprint() != fingerprint:
            raise RunnerError("compiler/SDK changed during result description")
        progress.cancel.check()
        if reuse is True:
            _remember_description(key, manifest_bytes, canonical_json(output))
        return output
    except BaseException:
        with _description_lock:
            _descriptions.pop(key, None)
        raise


def _describe_result(bundle_dir: Path, *, manifest_sha256: str,
                     result: dict[str, Any], reuse: bool) -> dict[str, Any]:
    """Initial semantic check and source matching; the caller checks final bytes."""
    from stpg.client import FailLogEntry
    from stil.json_artifacts import read_json_artifact, resolve_json_artifact
    from stil.report import describe_fail_sources, describe_source_span
    from .artifacts import confined_path, identity
    from .build import BUNDLE_NAME, verify_bundle
    root = Path(bundle_dir).resolve()
    checked = verify_bundle(root, manifest_sha256=manifest_sha256, reuse=reuse)
    if checked["status"] != "PASS":
        raise RunnerError("cannot describe an unverified bundle")
    manifest = read_json(confined_path(root, BUNDLE_NAME), expected_sha256=manifest_sha256)
    report = result
    if report.get("kind") != "stpg_runner_run_result" or type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise RunnerError("unsupported run result kind/schema")
    if (report.get("outcome") not in ("PENDING", "COMPLETED", "CANCELLED", "REJECT", "DEVICE_ERROR")
            or report.get("comparison") not in ("UNOBSERVED", "MATCH", "MISMATCH", "NOT_APPLICABLE")):
        raise RunnerError("invalid result outcome/comparison")
    plan = report.get("plan")
    if plan is not None and (not isinstance(plan, dict) or plan.get("manifest_sha256") != manifest_sha256
                             or not isinstance(plan.get("segments"), list)
                             or len(plan["segments"]) != len(manifest["patterns"])
                             or any(not isinstance(item, dict) for item in plan["segments"])):
        raise RunnerError("result belongs to another bundle manifest")
    segments = report.get("segments", [])
    if not isinstance(segments, list) or (plan is None and segments):
        raise RunnerError("invalid result segment geometry")
    output = {"kind": "stpg_result_description", "schema_version": 1,
              "manifest_sha256": manifest_sha256, "outcome": report.get("outcome"),
              "comparison": report.get("comparison"), "segments": [],
              "rebind": None, "device_io_performed": False, "physical_validation": False,
              "redaction": "NO_USB_IDENTITY_PORT_OR_INPUT_FILE_PATH; SOURCE_NAMES_CAN_BE_PRIVATE"}
    rebound = manifest["binding"].get("rebind")
    if rebound:
        output["rebind"] = {key: rebound[key] for key in (
            "compile_tool_sha256", "compile_python", "source_tool_sha256", "warning")}
    for index, record in enumerate(segments):
        if not isinstance(record, dict) or type(record.get("index")) is not int or record["index"] != index or index >= len(manifest["patterns"]):
            raise RunnerError("result segment index differs from the bundle")
        item = manifest["patterns"][index]
        if record.get("expected") != item["expected"]:
            raise RunnerError("result pattern expectations differ from the bundle")
        pattern = confined_path(root, item["path"])
        if plan["segments"][index].get("sha256") != identity(pattern)["sha256"]:
            raise RunnerError("result did not identify these exact pattern bytes")
        map_path = resolve_json_artifact(pattern.parent / "pattern.map.json")
        map_path = confined_path(root, map_path.relative_to(root).as_posix())
        if map_path.relative_to(root).as_posix() not in manifest["files"]:
            raise RunnerError("source map is not inventoried")
        data = read_json_artifact(map_path)
        payload = pattern.read_bytes()
        terminal = record.get("terminal")
        if (not isinstance(terminal, dict) or type(terminal.get("state")) is not int
                or any(type(terminal.get(key)) is not int or not 0 <= terminal[key] <= 0xFFFFFFFF
                       for key in ("fail_count", "result_flags", "sample_count", "run_progress"))):
            raise RunnerError("invalid terminal description fields")
        retained = record.get("fail_log")
        log = retained if retained is not None else {"status": "UNAVAILABLE_LEGACY", "entries": [], "entries_read": 0}
        if (not isinstance(log, dict) or log.get("status") not in (
                "PENDING", "READ", "READ_ERROR", "NOT_NEEDED", "NOT_COLLECTED_ERROR", "UNAVAILABLE_LEGACY")
                or type(log.get("entries_read")) is not int
                or type(log.get("overflow", False)) is not bool):
            raise RunnerError("invalid retained fail-log status")
        entries = log.get("entries", [])
        if not isinstance(entries, list) or len(entries) > 341 or log.get("entries_read", 0) != len(entries):
            raise RunnerError("invalid retained fail log")
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"event_index", "actual", "expected", "mask"} or any(
                    type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in entry.values()):
                raise RunnerError("invalid retained fail entry")
        if record.get("outcome") not in ("COMPLETED", "DEVICE_ERROR") or record.get("comparison") not in (
                None, "UNOBSERVED", "MATCH", "MISMATCH", "NOT_APPLICABLE"):
            raise RunnerError("invalid segment outcome/comparison")
        output["segments"].append({
            "index": index, "outcome": record.get("outcome"), "comparison": record.get("comparison"),
            "terminal_state": terminal["state"], "fail_count": terminal["fail_count"],
            "source_range": describe_source_span(data, pattern_bytes=payload),
            "fail_log": {"status": log["status"], "entries_read": len(entries),
                         "overflow": bool(log.get("overflow", False)),
                         "entries": describe_fail_sources([FailLogEntry(**entry) for entry in entries], data,
                                                          pattern_bytes=payload)},
        })
    transactions = report.get("transactions", [])
    if not isinstance(transactions, list) or any(not isinstance(item, dict) for item in transactions):
        raise RunnerError("invalid result transaction journal")
    last = next((item for item in reversed(transactions) if item.get("read_error") or item.get("write_error")), None)
    output["stop"] = ({"kind": last.get("kind"), "command": last.get("command"),
                       "seq": last.get("seq"), "failure_stage": last.get("read_failure_stage", "write")}
                      if last else None)
    return output
