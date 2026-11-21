"""Load a STIL file through the existing STIL subset front end into a Trace."""

from __future__ import annotations

from array import array
from dataclasses import replace
from decimal import Decimal
import hashlib
from itertools import accumulate
from pathlib import Path

from stil.ast import ResolvedLoop
from stil.errors import StilError
from stil.parser import parse_stil_file
from stil.resolve import build_ir_with_scale, load_pin_map, resolve_document
from stil.waveform_actions import STIL_DRIVE_RELEASE, drive_level_for_char, drive_op_for_event, event_char
from stpg.constants import STPG_DRIVE_ONE, STPG_DRIVE_ZERO

from .model import KIND_COMPARE, KIND_DRIVE, KIND_RELEASE, Event, Rate, RowChars, Signal, Trace

# 1 tick = 1 ps keeps STIL timing exact for ordinary ns/ps values.
STIL_TICK_HZ = 1_000_000_000_000
# Logical (expanded) vectors one STIL may show: about 70 bytes each in the server.
MAX_VIEW_VECTORS = 20_000_000


def _file_identity(path: Path) -> dict:
    data = path.read_bytes()
    return {"path": str(path), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _defined_chars(document) -> dict[tuple[str, str], set[str]]:  # type: ignore[no-untyped-def]
    """(waveform table, signal) -> characters defined in the source."""

    groups = {group.name: group.signals for group in document.signal_groups}
    defined: dict[tuple[str, str], set[str]] = {}
    for table in document.waveform_tables:
        for waveform in table.waveforms:
            members = groups.get(waveform.signal_or_group, (waveform.signal_or_group,))
            for signal in members:
                defined.setdefault((table.name, signal), set()).update(c.char for c in waveform.chars)
    return defined


def _source_char(char: str, table: str, signal: str, defined: dict[tuple[str, str], set[str]]) -> str:
    # The resolver relabels P->R and N->F (events are kept). Show what the user wrote.
    chars = defined.get((table, signal))
    if chars is not None and char not in chars:
        if char == "R" and "P" in chars:
            return "P"
        if char == "F" and "N" in chars:
            return "N"
    return char


def _signal_events(signal_index: int, char: str, events) -> tuple[Event, ...]:  # type: ignore[no-untyped-def]
    """Interpret one signal's timed actions exactly as the rate/raw backends do.

    ``char`` is the IR character (P/N already relabelled R/F). Each event is
    first mapped with ``event_char`` (the case-sensitive selectors U/D/h/l/u/d/p/n
    take their meaning from the declared action, per event). Then: Drive actions
    win; drive characters imply their level; ``Z`` releases (any action name,
    e.g. ``DirZ``); ``H``/``L``/``X`` sample; ``T`` does nothing.
    """

    result: list[Event] = []
    for event in events:
        effective = event_char(event, char=char)
        op = drive_op_for_event(event, char=effective)
        if op is None and effective in ("1", "R", "0", "F"):
            op = STPG_DRIVE_ONE if drive_level_for_char(effective) == 1 else STPG_DRIVE_ZERO
        if op == STPG_DRIVE_ONE:
            result.append(Event(signal_index, event.time_ticks, KIND_DRIVE, "1"))
        elif op == STPG_DRIVE_ZERO:
            result.append(Event(signal_index, event.time_ticks, KIND_DRIVE, "0"))
        elif op == STIL_DRIVE_RELEASE:
            result.append(Event(signal_index, event.time_ticks, KIND_RELEASE))
        elif effective == "Z":
            result.append(Event(signal_index, event.time_ticks, KIND_RELEASE))
        elif effective in ("H", "L", "X"):
            result.append(Event(signal_index, event.time_ticks, KIND_COMPARE, effective))
    return tuple(result)


def _parse_and_resolve(path: Path, pin_map: Path | None, pattern_burst: str | None, pattern: str | None):  # type: ignore[no-untyped-def]
    """Parse and resolve with Loops kept (each body once), as the compiler's default ``preserve`` does.

    Full expansion is bounded by the compiler's static limit (``static Pattern expansion exceeds ...``),
    so a long looped pattern only loads preserved. A Loop that cannot be kept (its iterations differ)
    falls back to expansion; if that fails too, the preserved error is reported.
    """

    first_error: StilError | None = None
    for policy in ("preserve", "expand"):
        try:
            document = parse_stil_file(path, loop_policy=policy)
            pin_map_data = load_pin_map(pin_map, document=document)
            resolved = resolve_document(document, pin_map=pin_map_data, pattern_burst_name=pattern_burst,
                                        pattern_name=pattern, loop_policy=policy)
            return document, pin_map_data, resolved
        except StilError as exc:
            first_error = first_error or exc
    raise first_error  # type: ignore[misc]


def _expanded_count(items) -> int:  # type: ignore[no-untyped-def]
    total = 0
    for item in items:
        total += item.loop_count * _expanded_count(item.items) if isinstance(item, ResolvedLoop) else 1
    return total


def _expand(items, occurrence_of: dict[int, int]) -> array:  # type: ignore[no-untyped-def]
    """Occurrence id of every logical vector; a Loop repeats its body's ids."""

    out = array("i")
    for item in items:
        if isinstance(item, ResolvedLoop):
            out.extend(_expand(item.items, occurrence_of) * item.loop_count)
        else:
            out.append(occurrence_of[id(item)])
    return out


class _SourceLines:
    """One line of the source file by number, read on demand (a long STIL is not kept in memory)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offsets: array | None = None

    def line(self, number: int) -> str | None:
        if self.offsets is None:
            data = self.path.read_bytes()
            self.offsets = array("q", [0])
            find = data.find
            at = find(b"\n")
            while at >= 0:
                self.offsets.append(at + 1)
                at = find(b"\n", at + 1)
            self.offsets.append(len(data) + 1)
        if not 0 < number < len(self.offsets):
            return None
        start, end = self.offsets[number - 1], self.offsets[number] - 1
        with self.path.open("rb") as stream:
            stream.seek(start)
            return stream.read(max(0, end - start)).decode(errors="replace").strip()


def load_stil(path: str | Path, *, pin_map: str | Path | None = None,
              pattern_burst: str | None = None, pattern: str | None = None) -> Trace:
    path = Path(path)
    document, pin_map_data, resolved = _parse_and_resolve(path, Path(pin_map) if pin_map else None,
                                                          pattern_burst, pattern)
    occurrences = resolved.vectors  # each vector statement once; Loop bodies are not repeated here
    total = _expanded_count(resolved.items) if resolved.items else len(occurrences)
    if total > MAX_VIEW_VECTORS:
        raise StilError("ERR_VIEW_TOO_LONG", f"the pattern expands to {total:,} vectors; the viewer shows at most "
                                             f"{MAX_VIEW_VECTORS:,}")

    # Distinct rows (WaveformTable + characters) are interpreted once; vectors refer to them by id.
    row_of: dict[tuple, int] = {}
    distinct = []
    occurrence_row = array("i")
    for vector in occurrences:
        key = (vector.waveform_table_name, tuple(vector.signal_chars.items()))
        row = row_of.get(key)
        if row is None:
            row = row_of[key] = len(distinct)
            distinct.append(vector)
        occurrence_row.append(row)
    del row_of
    ir = build_ir_with_scale(replace(resolved, vectors=tuple(distinct), items=(), loop_preserved=False),
                             tick_hz=STIL_TICK_HZ, time_scale=Decimal(1))

    declared = {signal.name: signal.direction for signal in document.signals}
    first = ir.vectors[occurrence_row[0]] if occurrences else None
    seen = set(first.signal_chars) if first is not None else set()
    order = [s.name for s in document.signals if s.name in seen] + \
        [n for n in (first.signal_chars if first is not None else ()) if n not in declared]
    assignments = pin_map_data.signals
    signals = [
        Signal(
            name=name,
            bit=assignments[name].bit if name in assignments else None,
            pin=getattr(assignments.get(name), "logical_pin", None),
            declared=declared.get(name, ""),
        )
        for name in order
    ]
    trace = Trace(kind="stil", title=path.name, signals=signals)
    trace.sources.append({"kind": "stil", **_file_identity(path)})
    if pin_map:
        trace.sources.append({"kind": "pin_map", **_file_identity(Path(pin_map))})
    if not ir.exact:
        trace.notes.append("timing approximated: " + "; ".join(ir.approximate_reasons))
    if resolved.normalized_transition_count:
        trace.notes.append(f"{resolved.normalized_transition_count} P/N labels were normalized by the compiler; "
                           "the viewer shows the source characters")

    defined = _defined_chars(document)
    rate_index: dict[tuple[str, int], int] = {}
    templates: dict[tuple[int, int, str], tuple[Event, ...]] = {}
    row_rate = array("i")
    row_period = array("q")
    row_extra = array("q")
    row_ir_chars: list[str] = []
    row_shown: list[str] = []
    # What a row does to the held drive state: (bits it sets or clears, the bits it sets) for the level and
    # for the enable. A drive sets both; a release clears the enable and keeps the level (apply_events).
    row_change: list[tuple[int, int, int, int]] = []
    for vector in ir.vectors:
        period = vector.rate_key.period_ticks
        key = (vector.waveform_table_name, period)
        rate_id = rate_index.get(key)
        if rate_id is None:
            rate_id = rate_index[key] = len(trace.rates)
            trace.rates.append(Rate(name=vector.waveform_table_name, period_ps=period))
        ir_chars = []
        shown = []
        value_mask = value_bits = enable_mask = enable_bits = 0
        for sig, signal in enumerate(signals):
            char = vector.signal_chars.get(signal.name, "-")
            events = templates.get((rate_id, sig, char))
            if events is None:
                events = templates[(rate_id, sig, char)] = _signal_events(
                    sig, char, vector.signal_events.get(signal.name, ()))
            for event in sorted(events, key=lambda e: e.t):
                if event.kind == KIND_DRIVE:
                    value_mask |= 1 << sig
                    value_bits = value_bits | (1 << sig) if event.value == "1" else value_bits & ~(1 << sig)
                if event.kind in (KIND_DRIVE, KIND_RELEASE):
                    enable_mask |= 1 << sig
                    enable_bits = enable_bits | (1 << sig) if event.kind == KIND_DRIVE else enable_bits & ~(1 << sig)
            ir_chars.append(char)
            shown.append(_source_char(char, vector.waveform_table_name, signal.name, defined))
        is_wait = vector.waveform_table_name.startswith("STPGV_WAIT_")
        if is_wait:
            trace.wait_source = "stil"
        row_rate.append(rate_id)
        row_period.append(period)
        row_extra.append(period if is_wait else 0)
        row_ir_chars.append("".join(ir_chars))
        row_shown.append("".join(shown))
        row_change.append((value_mask, value_bits, enable_mask, enable_bits))

    occurrence_line = array("i", (v.location.line if v.location.line is not None else -1 for v in occurrences))
    labelled = {k: v.location.label for k, v in enumerate(occurrences) if v.location.label}
    if resolved.items:
        logical = _expand(resolved.items, {id(v): k for k, v in enumerate(occurrences)})
    else:
        logical = array("i", range(len(occurrences)))
    del occurrences, distinct, resolved, ir, document

    rows = array("i", map(occurrence_row.__getitem__, logical))
    trace.chars = RowChars(row_shown, rows)
    trace.lines = array("i", map(occurrence_line.__getitem__, logical))
    trace.rate_ids = array("i", map(row_rate.__getitem__, rows))
    trace.periods = array("q", map(row_period.__getitem__, rows))
    trace.extra_ps = array("q", map(row_extra.__getitem__, rows))
    trace.wait_at = array("q", bytes(8 * len(rows)))
    trace.starts = array("q", accumulate(trace.periods, initial=0))
    trace.total_ps = trace.starts.pop()
    if labelled:
        trace.labels = {i: labelled[o] for i, o in enumerate(logical) if o in labelled}
    del logical
    values, enables = trace.start_values, trace.start_enables
    value = enable = 0
    for row in rows:
        values.append(value)
        enables.append(enable)
        value_mask, value_bits, enable_mask, enable_bits = row_change[row]
        if enable_mask:
            value = (value & ~value_mask) | value_bits
            enable = (enable & ~enable_mask) | enable_bits

    def events_for(index: int) -> tuple[Event, ...]:
        row = rows[index]
        rate_id = row_rate[row]
        result: list[Event] = []
        for sig, char in enumerate(row_ir_chars[row]):
            result.extend(templates.get((rate_id, sig, char), ()))
        return tuple(result)

    source = _SourceLines(path)

    def detail_for(index: int) -> dict:
        return {"source_text": source.line(trace.lines[index]), "source": "stil"}

    trace.set_providers(events_for, detail_for)
    return trace
