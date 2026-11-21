"""Loaded traces and the data queries used by the web UI."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import threading

from .load_bin import load_bin, read_map
from .load_stil import load_stil  # noqa: F401 - re-exported for callers
from .stil_inputs import load_stil_input, stil_role
from .model import Trace, char_class

EVENT_WINDOW_LIMIT = 4000  # vectors returned with full events; larger windows are summarized
SUMMARY_SCAN_LIMIT = 200_000  # vectors scanned for one summary request (evenly sampled)

_CLASS_BITS = {"drive": 1, "compare": 2, "masked": 4, "release": 8, "none": 16}


def classify_json(path: Path) -> str:
    """Return 'map', 'pin_map', 'package' or 'unknown' for a JSON input."""

    try:
        data = read_map(path) if path.name.endswith((".json", ".json.gz")) else {}
    except Exception:  # noqa: BLE001 - classification only
        return "unknown"
    if not isinstance(data, dict):
        return "unknown"
    if "signal_map" in data and "vectors" in data:
        return "map"
    if "signals" in data and "active_pins" in data:
        return "pin_map"
    if "artifacts" in data or data.get("kind", "").endswith("package_manifest"):
        return "package"
    if data.get("kind") == "stpg_view_edits":
        return "edits"
    return "unknown"


@dataclass
class LoadRequest:
    stil: list[Path] = field(default_factory=list)
    binaries: list[Path] = field(default_factory=list)
    map_path: Path | None = None
    pin_map: Path | None = None
    edit_lists: list[Path] = field(default_factory=list)


def plan_inputs(paths: list[str | Path], *, pin_map: str | Path | None = None,
                map_path: str | Path | None = None) -> LoadRequest:
    request = LoadRequest(pin_map=Path(pin_map) if pin_map else None, map_path=Path(map_path) if map_path else None)
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            candidate = path / "pattern.bin"
            if candidate.is_file():
                request.binaries.append(candidate)
                continue
            raise ValueError(f"{path}: directory has no pattern.bin")
        if not path.is_file():
            raise ValueError(f"{path}: not found")
        name = path.name.lower()
        if name.endswith((".stil", ".stl", ".stil.gz", ".stl.gz")):
            request.stil.append(path)
        elif name.endswith(".bin"):
            request.binaries.append(path)
        elif name.endswith((".json", ".json.gz")):
            kind = classify_json(path)
            if kind == "map":
                request.map_path = path
            elif kind == "pin_map":
                request.pin_map = path
            elif kind == "edits":
                request.edit_lists.append(path)
            elif kind == "package":
                bin_path = path.parent / "pattern.bin"
                if bin_path.is_file():
                    request.binaries.append(bin_path)
                else:
                    raise ValueError(f"{path}: package manifest without pattern.bin")
            else:
                raise ValueError(f"{path}: unrecognized JSON (expected map.json, pin map or manifest)")
        else:
            raise ValueError(f"{path}: unsupported file type")
    if not request.stil and not request.binaries and not request.edit_lists:
        raise ValueError("nothing to show: give a .stil, a pattern .bin or a package directory")
    return request


MAX_MARKS = 100_000


def plain_stil(path: Path, work_dir: Path) -> Path:
    """A decompressed copy of a gzip STIL in the session's work folder (made once per content), for viewing."""

    import shutil
    from stil.source_artifacts import decoded_source_identity, open_source_binary

    digest = decoded_source_identity(path)["sha256"]
    name = path.name[:-3] or "pattern.stil"
    target = work_dir / "gunzip" / digest[:16] / name
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        with open_source_binary(path) as source, partial.open("wb") as copy:
            shutil.copyfileobj(source, copy, 1 << 20)
        partial.replace(target)
    return target


class Session:
    def __init__(self, work_dir: Path | None = None) -> None:
        self.traces: dict[str, Trace] = {}
        self.order: list[str] = []
        self.lock = threading.Lock()
        import tempfile

        from .editing import EditManager

        self.work_dir = work_dir or Path(tempfile.mkdtemp(prefix="stpg-view-work-"))
        self.edits = EditManager(self, self.work_dir)

    # ---- loading --------------------------------------------------------
    def load(self, paths: list[str | Path], *, pin_map: str | Path | None = None,
             map_path: str | Path | None = None, replace: bool = True) -> list[str]:
        request = plan_inputs(paths, pin_map=pin_map, map_path=map_path)
        compressed = {}
        for index, stil_path in enumerate(request.stil):
            if stil_path.name.lower().endswith(".gz"):  # for example a bundle's compressed source snapshot
                plain = plain_stil(stil_path, self.work_dir)
                compressed[plain] = stil_path
                request.stil[index] = plain
        loaded: list[tuple[str, Trace]] = []
        roles = {path: stil_role(path) for path in request.stil}
        projects = [path for path, role in roles.items() if role == "project"]
        patterns = [path for path, role in roles.items() if role == "pattern"]
        if len(projects) > 1 and patterns:
            raise ValueError("several ATE-generator project files were given; open one project with its patterns")
        project = projects[0] if len(projects) == 1 and patterns else None
        for stil_path in request.stil:
            if project is not None and stil_path == project:
                continue
            trace = load_stil_input(stil_path, pin_map=request.pin_map, work_dir=self.work_dir,
                                    project=project if roles[stil_path] == "pattern" else None)
            if stil_path in compressed:
                trace.sources.append({"kind": "stil_gzip", "path": str(compressed[stil_path])})
            loaded.append((f"stil:{stil_path.name}", trace))
        for bin_path in request.binaries:
            map_for_bin = request.map_path if len(request.binaries) == 1 else None
            trace = load_bin(bin_path, map_path=map_for_bin, pin_map=request.pin_map)
            loaded.append((f"bin:{bin_path.parent.name}/{bin_path.name}", trace))
        with self.lock:
            if replace:
                self.traces.clear()
                self.order.clear()
                self.edits.states.clear()
            ids = []
            for base_id, trace in loaded:
                trace_id = base_id
                n = 2
                while trace_id in self.traces:
                    trace_id = f"{base_id}#{n}"
                    n += 1
                self.traces[trace_id] = trace
                self.order.append(trace_id)
                ids.append(trace_id)
        for edit_list in request.edit_lists:
            ids.append(self.edits.open_edit_list(edit_list))
        return ids

    # ---- fail marks of a run (placed by the runner page; checked against this trace) ----------------------
    def set_marks(self, trace_id: str | None, marks: object, title: str) -> dict:
        """Place a run's fail entries on one trace: a vector is marked only when this trace's vector at that index
        is on the STIL line the SDK named. Anything else is listed as not placed, with the reason; nothing is
        moved to a nearby vector."""

        trace = self.get(trace_id)
        if not isinstance(marks, list) or len(marks) > MAX_MARKS:
            raise ValueError(f"marks must be a list of at most {MAX_MARKS}")
        lanes = {signal.name: index for index, signal in enumerate(trace.signals)}
        placed, unplaced = [], []
        for number, mark in enumerate(marks):
            mark = mark if isinstance(mark, dict) else {}
            vector, line, text = mark.get("vector"), mark.get("line"), str(mark.get("text") or "")[:300]
            signals = [name for name in mark.get("signals") or [] if isinstance(name, str)]
            if type(vector) is not int or type(line) is not int:
                reason = "the result gives no STIL vector and line for this entry"
            elif not 0 <= vector < trace.vector_count:
                reason = f"vector {vector} is outside this view ({trace.vector_count} vectors)"
            elif trace.lines[vector] != line:
                shown = trace.lines[vector]
                reason = (f"vector {vector} of this view is on line {shown if shown >= 0 else 'unknown'}, the result "
                          f"names line {line}: not the same STIL occurrence")
            else:
                placed.append({"n": number, "vector": vector, "line": line, "start_ps": trace.starts[vector],
                               "period_ps": trace.periods[vector], "lanes": [lanes[n] for n in signals if n in lanes],
                               "missing_signals": [n for n in signals if n not in lanes], "text": text})
                continue
            unplaced.append({"n": number, "text": text, "reason": reason})
        placed.sort(key=lambda item: (item["start_ps"], item["n"]))
        with self.lock:
            trace.fail_marks = {"title": str(title)[:200], "marks": placed, "unplaced": unplaced,
                                "total": len(marks)}
        return {"placed": len(placed), "unplaced": len(unplaced), "total": len(marks),
                "reasons": [item["reason"] for item in unplaced[:5]]}

    def marks(self, trace: Trace) -> dict:
        with self.lock:
            return trace.fail_marks or {"title": None, "marks": [], "unplaced": [], "total": 0}

    def get(self, trace_id: str | None) -> Trace:
        with self.lock:
            if trace_id is None and self.order:
                trace_id = self.order[0]
            if trace_id not in self.traces:
                raise KeyError(f"unknown trace {trace_id!r}")
            return self.traces[trace_id]

    # ---- queries --------------------------------------------------------
    def state(self) -> dict:
        with self.lock:
            return {"traces": [{"id": trace_id, **self.traces[trace_id].meta()} for trace_id in self.order]}

    @staticmethod
    def rows(trace: Trace, start: int, count: int) -> dict:
        start = max(0, start)
        stop = min(trace.vector_count, start + max(0, min(count, 2000)))
        return {"start": start, "rows": [trace.row(i) for i in range(start, stop)]}

    @staticmethod
    def wave(trace: Trace, t0: int, t1: int, buckets: int) -> dict:
        if trace.vector_count == 0:
            return {"mode": "empty"}
        first = trace.vector_at(t0)
        last = trace.vector_at(max(t0, t1))
        count = last - first + 1
        if count <= EVENT_WINDOW_LIMIT:
            events = []
            vectors = []
            for index in range(first, last + 1):
                base = trace.starts[index]
                vectors.append([index, base, trace.periods[index], trace.chars[index], trace.extra_ps[index],
                                trace.wait_at[index], trace.rate_ids[index]])
                for event in trace.events(index):
                    events.append([event.signal, base + event.t, event.kind, event.value])
            return {
                "mode": "events",
                "first": first,
                "init_value": trace.start_values[first],
                "init_enable": trace.start_enables[first],
                "vectors": vectors,
                "events": events,
            }
        waits = Session._wait_spans(trace, first, last)
        buckets = max(1, min(buckets, 4000))
        span_start = trace.starts[first]
        span_end = trace.starts[last] + trace.periods[last]
        width = max(1, (span_end - span_start) / buckets)
        stride = max(1, count // SUMMARY_SCAN_LIMIT)
        n_signals = len(trace.signals)
        cells: list[list[int]] = [[0] * n_signals for _ in range(buckets)]
        # Each bucket is summarised from its distinct rows (in scan order): a signal is "changing" when
        # its character differs between them or from the previous bucket's last row.
        starts, ids, rows, rate_ids = trace.starts, trace.chars.ids, trace.chars.rows, trace.rate_ids
        in_bucket: list[dict[int, None]] = [{} for _ in range(buckets)]
        bucket_rates: list[dict[int, None]] = [{} for _ in range(buckets)]
        last_row = [-1] * buckets
        for index in range(first, last + 1, stride):
            bucket = min(buckets - 1, int((starts[index] - span_start) / width))
            row_id = ids[index]
            in_bucket[bucket][row_id] = None
            bucket_rates[bucket][rate_ids[index]] = None
            last_row[bucket] = row_id
        previous: str | None = None
        for bucket, distinct in enumerate(in_bucket):
            if not distinct:
                continue
            target = cells[bucket]
            texts = [rows[row_id] for row_id in distinct]
            for row_id in distinct:
                bits = _row_bits(trace, row_id)
                for sig in range(n_signals):
                    target[sig] |= bits[sig]
            for sig in range(n_signals):
                char = texts[0][sig]
                if (previous is not None and previous[sig] != char) or any(t[sig] != char for t in texts[1:]):
                    target[sig] |= 128
            previous = rows[last_row[bucket]]
        return {
            "mode": "summary",
            "first": first,
            "last": last,
            "span_start": span_start,
            "bucket_ps": width,
            "stride": stride,
            "cells": cells,
            "rates": [list(found)[:3] for found in bucket_rates],  # the first rates seen in each bucket
            "waits": waits,
        }

    @staticmethod
    def _wait_spans(trace: Trace, first: int, last: int, limit: int = 5000) -> list[list[int]]:
        """[start, end] of the inserted wait time of each vector in the range."""

        spans = []
        for index in trace.wait_vectors(first, last):
            start = trace.starts[index] + trace.wait_at[index]
            spans.append([start, start + trace.extra_ps[index]])
            if len(spans) >= limit:
                break
        return spans

    @staticmethod
    def find(trace: Trace, *, line: int | None = None, time_ps: int | None = None,
             vector: int | None = None) -> dict:
        if vector is not None:
            index = max(0, min(trace.vector_count - 1, vector))
        elif time_ps is not None:
            index = trace.vector_at(time_ps)
        elif line is not None:
            index = next((i for i, value in enumerate(trace.lines) if value == line), -1)
            if index < 0:
                later = [(value, i) for i, value in enumerate(trace.lines) if value > line]
                index = min(later)[1] if later else -1
        else:
            index = -1
        return {"index": index}


def _row_bits(trace: Trace, row_id: int) -> list[int]:
    """Summary bits of each signal of one distinct row: its class, and 32 high / 64 low."""

    cache = trace.chars.summary_bits
    bits = cache.get(row_id)
    if bits is None:
        bits = []
        for char in trace.chars.rows[row_id]:
            value = _CLASS_BITS[char_class(char)]
            if char in ("1", "H", "h", "U", "u"):
                value |= 32
            elif char in ("0", "L", "l", "D", "d"):
                value |= 64
            bits.append(value)
        cache[row_id] = bits
    return bits


def dumps(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()
