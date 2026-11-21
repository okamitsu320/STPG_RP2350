"""Write a Trace as a VCD file for Surfer, GTKWave and other waveform viewers.

Scopes:
    drive.<signal>   tester output: 0 / 1, or z while the tester does not drive
    expect.<signal>  expected DUT level from a compare strobe until the end of
                     that vector: 1 (H), 0 (L), x (sampled, not compared); z otherwise
    info.vector      logical vector index (integer)
    info.stil_line   STIL source line of the vector (0 when unknown)
    info.rate        rate / waveform-table index
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import IO

from .model import KIND_COMPARE, KIND_DRIVE, KIND_RELEASE, Trace

_EXPECT_VALUE = {"H": "1", "L": "0", "X": "x"}


def _identifiers():
    alphabet = [chr(c) for c in range(33, 127)]
    n = 0
    while True:
        value = n
        ident = ""
        while True:
            ident = alphabet[value % len(alphabet)] + ident
            value //= len(alphabet)
            if value == 0:
                break
        yield ident
        n += 1


def _vcd_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\[\]\.$]", "_", name) or "_"


def write_vcd(trace: Trace, out: IO[str], *, start: int = 0, end: int | None = None) -> None:
    stop = trace.vector_count if end is None else min(end, trace.vector_count)
    ids = _identifiers()
    drive_ids = [next(ids) for _ in trace.signals]
    expect_ids = [next(ids) for _ in trace.signals]
    vec_id, line_id, rate_id = next(ids), next(ids), next(ids)

    out.write(f"$date {datetime.now(timezone.utc).isoformat(timespec='seconds')} $end\n")
    out.write("$version stpg-view (STPG pattern viewer) $end\n")
    sources = ", ".join(f"{s.get('kind')}={Path(s.get('path', '')).name}" for s in trace.sources)
    out.write(f"$comment source: {sources} $end\n")
    out.write("$timescale 1ps $end\n")
    out.write("$scope module drive $end\n")
    for signal, ident in zip(trace.signals, drive_ids):
        out.write(f"$var wire 1 {ident} {_vcd_name(signal.name)} $end\n")
    out.write("$upscope $end\n$scope module expect $end\n")
    for signal, ident in zip(trace.signals, expect_ids):
        out.write(f"$var wire 1 {ident} {_vcd_name(signal.name)} $end\n")
    out.write("$upscope $end\n$scope module info $end\n")
    out.write(f"$var integer 32 {vec_id} vector $end\n")
    out.write(f"$var integer 32 {line_id} stil_line $end\n")
    out.write(f"$var integer 16 {rate_id} rate $end\n")
    out.write("$upscope $end\n$enddefinitions $end\n")

    if stop <= start:
        return
    value = trace.start_values[start]
    enable = trace.start_enables[start]
    drive_state = []
    for sig in range(len(trace.signals)):
        mask = 1 << sig
        drive_state.append(("1" if value & mask else "0") if enable & mask else "z")
    expect_state = ["z"] * len(trace.signals)

    t0 = trace.starts[start]
    out.write("#0\n$dumpvars\n")
    for sig, ident in enumerate(drive_ids):
        out.write(f"{drive_state[sig]}{ident}\n")
    for ident in expect_ids:
        out.write(f"z{ident}\n")
    out.write(f"b0 {vec_id}\nb0 {line_id}\nb0 {rate_id}\n$end\n")

    current_time = 0
    pending: dict[int, list[str]] = {}

    def emit(t: int, text: str) -> None:
        pending.setdefault(t, []).append(text)

    def flush_until(limit: int | None) -> None:
        nonlocal current_time
        for t in sorted(pending):
            if limit is not None and t >= limit:
                break
            if t != current_time:
                out.write(f"#{t}\n")
                current_time = t
            out.write("".join(pending.pop(t)))

    for index in range(start, stop):
        base = trace.starts[index] - t0
        flush_until(base)
        changes = []
        for sig in range(len(trace.signals)):
            if expect_state[sig] != "z":
                expect_state[sig] = "z"
                changes.append(f"z{expect_ids[sig]}\n")
        line = trace.lines[index]
        changes.append(f"b{index:b} {vec_id}\n")
        changes.append(f"b{max(line, 0):b} {line_id}\n")
        changes.append(f"b{trace.rate_ids[index]:b} {rate_id}\n")
        emit(base, "".join(changes))
        for event in sorted(trace.events(index), key=lambda e: (e.t, e.signal)):
            t = base + event.t
            sig = event.signal
            if event.kind == KIND_DRIVE:
                new = event.value
                if drive_state[sig] != new:
                    drive_state[sig] = new
                    emit(t, f"{new}{drive_ids[sig]}\n")
            elif event.kind == KIND_RELEASE:
                if drive_state[sig] != "z":
                    drive_state[sig] = "z"
                    emit(t, f"z{drive_ids[sig]}\n")
            elif event.kind == KIND_COMPARE:
                new = _EXPECT_VALUE.get(event.value, "x")
                if expect_state[sig] != new:
                    expect_state[sig] = new
                    emit(t, f"{new}{expect_ids[sig]}\n")
    end_time = trace.starts[stop - 1] + trace.periods[stop - 1] - t0
    flush_until(None)
    if end_time != current_time:
        out.write(f"#{end_time}\n")


def export_vcd(trace: Trace, path: str | Path, **kwargs) -> Path:
    path = Path(path)
    with path.open("w", newline="\n") as stream:
        write_vcd(trace, stream, **kwargs)
    return path
