"""Source-independent pattern model shared by the loaders, the server and VCD export.

All times are integer picoseconds. Each logical (expanded) vector has a start
time, a period, one display character per signal, and a list of timed events.
Events are produced on demand so that large patterns do not keep one Python
object per event in memory.

Event kinds:
    "D"  drive: the tester drives ``value`` ("0" or "1") from ``t`` onward
    "Z"  release: the tester stops driving from ``t`` onward
    "C"  compare strobe at ``t``; ``value`` is "H", "L" or "X" (sampled, not compared)
"""

from __future__ import annotations

from array import array
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

KIND_DRIVE = "D"
KIND_RELEASE = "Z"
KIND_COMPARE = "C"

# Character classes used by the UI and by tests.
CLASS_DRIVE = "drive"
CLASS_COMPARE = "compare"
CLASS_MASKED = "masked"  # sampled but not compared (X)
CLASS_RELEASE = "release"  # tester output off (Z)
CLASS_NONE = "none"  # no action in this vector (T, hold, unknown)


@dataclass(frozen=True)
class Signal:
    name: str
    bit: int | None = None
    pin: str | None = None
    declared: str = ""  # STIL direction (In/Out/InOut/...) or pin-map direction


@dataclass(frozen=True)
class Event:
    signal: int
    t: int  # ps, relative to the vector start
    kind: str
    value: str = ""


@dataclass
class Rate:
    name: str
    period_ps: int
    detail: dict = field(default_factory=dict)


class RowChars(Sequence):
    """Per-vector display characters, stored once per distinct row.

    Generated patterns repeat the same rows (and Loops repeat whole bodies), so a long pattern keeps one
    string per distinct row and a 4-byte row id per vector.
    """

    def __init__(self, rows: list[str] | None = None, ids: array | None = None) -> None:
        self.rows: list[str] = rows if rows is not None else []
        self.ids = ids if ids is not None else array("i")
        self._index = {row: i for i, row in enumerate(self.rows)}
        self.summary_bits: dict[int, list[int]] = {}  # row id -> per-signal bits (session.wave)

    def append(self, row: str) -> None:
        row_id = self._index.get(row)
        if row_id is None:
            row_id = self._index[row] = len(self.rows)
            self.rows.append(row)
        self.ids.append(row_id)

    def __len__(self) -> int:
        return len(self.ids)

    def __getitem__(self, index):  # type: ignore[no-untyped-def]
        if isinstance(index, slice):
            return [self.rows[i] for i in self.ids[index]]
        return self.rows[self.ids[index]]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (RowChars, list, tuple)):
            return len(self) == len(other) and all(a == b for a, b in zip(self, other))
        return NotImplemented

    __hash__ = None  # type: ignore[assignment] - mutable, like a list


def mask_store(signal_count: int):  # type: ignore[no-untyped-def]
    """Storage for one signal bitmask per vector: 8 bytes each up to 64 signals."""

    return array("Q") if signal_count <= 64 else []


class Trace:
    """Expanded vectors of one source (STIL or binary)."""

    def __init__(self, *, kind: str, title: str, signals: list[Signal]) -> None:
        self.kind = kind
        self.title = title
        self.signals = signals
        self.sources: list[dict] = []
        self.rates: list[Rate] = []
        self.notes: list[str] = []
        self.starts = array("q")
        self.periods = array("q")
        self.rate_ids = array("i")
        self.lines = array("i")  # STIL line or -1
        self.extra_ps = array("q")  # inserted wait time inside the vector (0 = none)
        self.wait_at = array("q")  # offset of that wait from the vector start
        self.wait_source = ""  # "patch" (pattern.wait.patch.json), "inferred", or "stil"
        self.chars = RowChars()
        self.labels: dict[int, str] = {}
        self.start_values = mask_store(len(signals))  # driven level bitmask (signal index) at vector start
        self.start_enables = mask_store(len(signals))  # drive-enable bitmask at vector start
        self._events_fn: Callable[[int], tuple[Event, ...]] | None = None
        self._detail_fn: Callable[[int], dict] | None = None
        self.total_ps = 0
        self._wait_index: list[int] | None = None
        self.edit_of: str | None = None  # trace id of the STIL this edited preview was made from
        self.origin_path: Path | None = None  # original file when the shown STIL was preprocessed
        self.preprocessed_from: dict | None = None  # {"project": ..., "pattern": ...} for preprocessed input
        self.fail_marks: dict | None = None  # a run's fails placed on this trace (Session.set_marks)

    # ---- building -------------------------------------------------------
    def append_vector(self, *, period_ps: int, rate_id: int, chars: str, line: int | None,
                      start_value: int, start_enable: int, extra_ps: int = 0, wait_at_ps: int = 0,
                      label: str | None = None) -> None:
        index = len(self.chars)
        self.starts.append(self.total_ps)
        self.periods.append(period_ps)
        self.rate_ids.append(rate_id)
        self.lines.append(-1 if line is None else int(line))
        self.extra_ps.append(extra_ps)
        self.wait_at.append(wait_at_ps)
        self.chars.append(chars)
        self.start_values.append(start_value)
        self.start_enables.append(start_enable)
        if label:
            self.labels[index] = label
        self.total_ps += period_ps

    def set_providers(self, events: Callable[[int], tuple[Event, ...]],
                      detail: Callable[[int], dict] | None = None) -> None:
        self._events_fn = events
        self._detail_fn = detail

    # ---- queries --------------------------------------------------------
    @property
    def vector_count(self) -> int:
        return len(self.chars)

    def events(self, index: int) -> tuple[Event, ...]:
        if self._events_fn is None:
            return ()
        return self._events_fn(index)

    def wait_vectors(self, first: int, last: int) -> list[int]:
        """Indices in [first, last] whose period includes inserted wait time."""

        if self._wait_index is None:
            self._wait_index = [i for i, extra in enumerate(self.extra_ps) if extra]
        lo = bisect_right(self._wait_index, first - 1)
        hi = bisect_right(self._wait_index, last)
        return self._wait_index[lo:hi]

    def vector_at(self, t_ps: int) -> int:
        if not self.chars:
            return 0
        return max(0, min(len(self.chars) - 1, bisect_right(self.starts, t_ps) - 1))

    def row(self, index: int) -> dict:
        rate_id = self.rate_ids[index]
        line = self.lines[index]
        return {
            "i": index,
            "start": self.starts[index],
            "period": self.periods[index],
            "extra": self.extra_ps[index],
            "wait_at": self.wait_at[index],
            "rate": rate_id,
            "line": None if line < 0 else line,
            "label": self.labels.get(index),
            "chars": self.chars[index],
        }

    def detail(self, index: int) -> dict:
        info = self.row(index)
        info["rate_name"] = self.rates[info["rate"]].name if 0 <= info["rate"] < len(self.rates) else None
        info["events"] = [
            {"signal": e.signal, "t": e.t, "kind": e.kind, "value": e.value} for e in self.events(index)
        ]
        if self._detail_fn is not None:
            info.update(self._detail_fn(index))
        return info

    def iter_changes(self, start: int = 0, end: int | None = None) -> Iterator[tuple[int, int, Event]]:
        """Yield (vector index, absolute time, event) in time order."""

        stop = len(self.chars) if end is None else min(end, len(self.chars))
        for index in range(start, stop):
            base = self.starts[index]
            for event in sorted(self.events(index), key=lambda e: (e.t, e.signal)):
                yield index, base + event.t, event

    def meta(self) -> dict:
        return {
            "kind": self.kind,
            "title": self.title,
            "sources": self.sources,
            "signals": [
                {"name": s.name, "bit": s.bit, "pin": s.pin, "declared": s.declared} for s in self.signals
            ],
            "vector_count": self.vector_count,
            "total_ps": self.total_ps,
            "rates": [{"name": r.name, "period_ps": r.period_ps, "detail": r.detail} for r in self.rates],
            "notes": self.notes,
            "wait_source": self.wait_source,
            "edit_of": self.edit_of,
            "preprocessed_from": self.preprocessed_from,
            "fail_marks": len(self.fail_marks["marks"]) + len(self.fail_marks["unplaced"]) if self.fail_marks else 0,
        }


def format_ps(ps: int) -> str:
    """Human-readable time for labels (ps, ns, us, ms, s)."""

    value = abs(ps)
    for unit, scale in (("s", 10**12), ("ms", 10**9), ("us", 10**6), ("ns", 10**3)):
        if value >= scale:
            return f"{ps / scale:.6g} {unit}"
    return f"{ps} ps"


def char_class(char: str) -> str:
    """Display class of a tester character (the selected WaveformChar decides the real action)."""

    if char in ("H", "L", "h", "l"):
        return CLASS_COMPARE
    if char == "X":
        return CLASS_MASKED
    if char == "Z":
        return CLASS_RELEASE
    if char in ("0", "1", "P", "N", "U", "D", "R", "F", "u", "d", "p", "n"):
        return CLASS_DRIVE
    return CLASS_NONE


def apply_events(value: int, enable: int, events: tuple[Event, ...]) -> tuple[int, int]:
    """Return the drive state after one vector's events."""

    for event in sorted(events, key=lambda e: e.t):
        mask = 1 << event.signal
        if event.kind == KIND_DRIVE:
            enable |= mask
            if event.value == "1":
                value |= mask
            else:
                value &= ~mask
        elif event.kind == KIND_RELEASE:
            enable &= ~mask
    return value, enable
