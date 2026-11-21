"""Readable run results for people: the SDK's offline STIL description, worded, plus the fail-log stop point.

Nothing here opens a device or asks one again. The STIL correspondence (segment ranges, which vector, line and
signal failed) comes only from ``stpg_runner.describe_result``: runtime events are never turned into vectors here,
and a result is described only with the bundle whose exact manifest it recorded.

The shared text is built from that description only: no raw journal, port, USB identity, result or bundle path,
GUI job label or SDK error text. Pattern, label and signal names can still be confidential; the text says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stpg import constants as c
from stpg_runner.artifacts import RunnerError, identity
from stpg_runner.build import BUNDLE_NAME
from stpg_runner.results import LEGACY_RUN_RESULT_NAME, RUN_RESULT_NAME

_STATE_NAMES = {value: name[6:] for name, value in vars(c).items() if name.startswith("STATE_") and type(value) is int}
_RSP_NAMES = {value: name[4:] for name, value in vars(c).items() if name.startswith("RSP_") and type(value) is int}
_CMD_NAMES = {value: name[4:] for name, value in vars(c).items() if name.startswith("CMD_") and type(value) is int}
FAIL_LOG_STATUS_TEXT = {
    "NOT_NEEDED": "not needed (no compare failure reported)",
    "PENDING": "not read: the run stopped before the read",
    "READ": "read",
    "READ_ERROR": "reading stopped",
    "NOT_COLLECTED_ERROR": "not read (the segment ended in ERROR)",
    "UNAVAILABLE_LEGACY": "not in this result (recorded by an older version)",
}
MAX_TEXT_ENTRIES = 2000  # per description; the result file keeps every entry


def run_folder_of(entry: Path) -> Path | None:
    """The folder holding the STPG run record of one result: the result itself, ``stpg/`` or (older) ``run/``."""

    for folder in (entry, entry / "stpg", entry / "run"):
        if any((folder / name).exists() for name in (RUN_RESULT_NAME, LEGACY_RUN_RESULT_NAME)):
            return folder
    return None


def bundle_candidates(entry: Path, open_bundle: str | None) -> list[Path]:
    """Where the bundle of a result can be: the open bundle, then the builds of the project the results belong to.

    Results of a project bundle are in ``<project>/results`` next to ``<project>/build``.
    """

    found: list[Path] = []
    if open_bundle:
        found.append(Path(open_bundle).expanduser().resolve())
    results = entry.parent
    if results.name == "results" and (results.parent / "build").is_dir():
        found.extend(sorted(path for path in (results.parent / "build").iterdir() if path.is_dir()))
    return found


def find_bundle(manifest_sha256: str, candidates: list[Path]) -> Path | None:
    """The candidate whose manifest has exactly this SHA-256 (the hash only selects; the SDK verifies)."""

    for root in candidates:
        manifest = root / BUNDLE_NAME
        try:
            if manifest.is_file() and not manifest.is_symlink() and identity(manifest)["sha256"] == manifest_sha256:
                return root
        except (OSError, RunnerError):
            continue
    return None


def fail_log_summary(record: dict) -> dict | None:
    """One segment's retained fail-log state as the result file recorded it (None for older results)."""

    log = record.get("fail_log")
    if not isinstance(log, dict):
        return None
    terminal = record.get("terminal") if isinstance(record.get("terminal"), dict) else {}
    reported = log.get("reported_fail_count", terminal.get("fail_count"))
    to_read = min(reported, c.STPG_FAIL_LOG_MAX) if type(reported) is int else None
    return {"status": log.get("status"), "entries_read": log.get("entries_read"), "to_read": to_read,
            "reported": reported, "overflow": log.get("overflow") is True}


def fail_log_stop(report: dict) -> dict | None:
    """Where reading a segment's retained fail log stopped, from the result file.

    Only a segment whose fail log is ``READ_ERROR`` qualifies. Its comparison (a validated mismatching DONE/HOLD)
    stands; the request that failed is named from the journal only when the journal's tail belongs to the read.
    """

    segments = [item for item in report.get("segments") or [] if isinstance(item, dict)]
    record = next((item for item in reversed(segments)
                   if isinstance(item.get("fail_log"), dict) and item["fail_log"].get("status") == "READ_ERROR"), None)
    if record is None:
        return None
    summary = fail_log_summary(record) or {}
    terminal = record.get("terminal") if isinstance(record.get("terminal"), dict) else {}
    result = {"segment": record.get("index"), "planned_segments": report.get("planned_segments"),
              "terminal": _STATE_NAMES.get(terminal.get("state"), terminal.get("state")),
              "fail_count": terminal.get("fail_count"), "entries_read": summary.get("entries_read"),
              "to_read": summary.get("to_read"), "request": None, "failure_stage": None,
              "response_bytes": None, "expected_bytes": None, "answer": None}
    records = [item for item in report.get("transactions") or [] if isinstance(item, dict)]
    last = records[-1] if records else None
    names = {"fail_log_payload": "FAIL_LOG page data", "fail_log_trailer": "FAIL_LOG page end"}
    if last is not None and (last.get("kind") in names
                             or (last.get("kind") == "command" and last.get("command") == c.CMD_READ_FAIL_LOG)):
        result["request"] = names.get(last.get("kind"), "READ_FAIL_LOG")
        raw = last.get("raw_response_hex")
        result["response_bytes"] = len(raw) // 2 if isinstance(raw, str) else None
        result["expected_bytes"] = last.get("size") if last.get("kind") == "fail_log_payload" else c.STPG_RSP_SIZE
        if last.get("write_error"):
            result["failure_stage"] = "write"
        elif last.get("read_error"):
            result["failure_stage"] = last.get("read_failure_stage")
        decoded = last.get("decoded")
        if isinstance(decoded, dict) and not last.get("read_error"):
            # a complete frame that the SDK did not accept (another answer or state, or a page it rejected)
            result["answer"] = (f"{_RSP_NAMES.get(decoded.get('rsp'), decoded.get('rsp'))} · "
                                f"{_STATE_NAMES.get(decoded.get('state'), decoded.get('state'))}")
    return result


def _bits(value: int) -> str:
    return "H" if value else "L"


def _where(source: dict | None) -> str:
    if not source:
        return "—"
    parts = [f"line {source['stil_line']}", f"vector {source['global_vector_index']}"]
    if source.get("pattern_name"):
        parts.append(f"pattern {source['pattern_name']}")
    if source.get("label"):
        parts.append(f"label {source['label']}")
    return ", ".join(parts)


def describe_text(description: dict, *, result_name: str) -> str:
    """Plain text for people and for sharing, built from ``describe_result`` output only."""

    # The result-folder basename is not safe to export: users may name it after
    # a port, serial, customer or private path. Keep result_name API-compatible,
    # but derive exported content only from the SDK's whitelisted description.
    lines = ["STPG run: STIL details", "",
             f"bundle manifest sha256: {description['manifest_sha256']}",
             f"outcome: {description['outcome']} / comparison: {description['comparison']}",
             "Acquisition outcome and comparison are separate; neither is a DUT pass. The STPG's compare result "
             "is not a physical or timing validation."]
    rebind = description.get("rebind")
    if rebind:
        lines += ["", "This bundle was updated for a newer SDK without recompiling:",
                  f"  {rebind['warning']}",
                  f"  compiled by SDK {rebind['compile_tool_sha256']} (Python {rebind['compile_python']})",
                  f"  updated from SDK {rebind['source_tool_sha256']}"]
    shown = 0
    for segment in description["segments"]:
        span = segment.get("source_range") or {}
        log = segment["fail_log"]
        lines += ["", f"segment {segment['index']}: {segment['outcome']}, "
                      f"comparison {segment.get('comparison') or 'not recorded'}, "
                      f"terminal {_STATE_NAMES.get(segment['terminal_state'], segment['terminal_state'])}, "
                      f"fails reported {segment['fail_count']}",
                  f"  STIL from {_where(span.get('first'))}",
                  f"  STIL to   {_where(span.get('last'))}",
                  f"  fail log: {FAIL_LOG_STATUS_TEXT.get(log['status'], log['status'])}, "
                  f"{log['entries_read']} entries" + (" (the STPG keeps only the first 341 failures)"
                                                      if log.get("overflow") else "")]
        for number, entry in enumerate(log["entries"], 1):
            if shown >= MAX_TEXT_ENTRIES:
                break
            shown += 1
            signals = ", ".join(f"{item['name']} expected {_bits(item['expected'])} got {_bits(item['actual'])}"
                                for item in entry["signals"]) or "—"
            extra = f"; unmapped fail bits 0x{entry['unmapped_fail_bits']:x}" if entry["unmapped_fail_bits"] else ""
            lines.append(f"  fail {number}: {_where(entry['source'])}: {signals}{extra}")
    if shown >= MAX_TEXT_ENTRIES:
        lines += ["", f"Only the first {MAX_TEXT_ENTRIES} fail entries are listed here; the result file keeps them all."]
    stop = description.get("stop")
    if stop:
        request = (_CMD_NAMES.get(stop.get("command"), stop.get("command")) if stop.get("kind") == "command"
                   else {"payload": "upload payload", "fail_log_payload": "FAIL_LOG page data",
                         "fail_log_trailer": "FAIL_LOG page end"}.get(stop.get("kind"), stop.get("kind")))
        lines += ["", f"stopped at: {request} (seq {stop.get('seq')}, failing stage "
                      f"{stop.get('failure_stage') or 'not recorded'})"]
    lines += ["", "Vectors are zero-based STIL vector occurrences (Loop iterations counted); lines are one-based.",
              "Pattern, signal and label names may be confidential: check this text before you share it."]
    return "\n".join(lines) + "\n"
