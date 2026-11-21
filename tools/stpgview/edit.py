"""STIL editing for the viewer.

Edits are recorded as JSON and applied to a *copy* of the STIL text; the
source file is never modified. Supported operations (vector indices refer to
the original, expanded vector order; signal names are the names current at
that point of the edit list, so an edit after a rename uses the new name):

``insert_wait``   ``{"op": "insert_wait", "after": i, "duration": "4ms"}``
    Insert one vector after vector ``i`` that only holds the current outputs
    (driven pins keep their level, released pins stay Z, nothing is compared),
    using a generated WaveformTable whose period is the wait duration.
``set_period``    ``{"op": "set_period", "first": i, "last": j, "period": "300ns"}``
    Run vectors ``i..j`` with a copy of their WaveformTable that has a new period
    (a "special rate").
``edge_time``     ``{"op": "edge_time", "signal": "clk", "char": "1", "event": 0, "time": "120ns",
                     "first": i, "last": j}``
    Move one timed event of one signal/character. With ``first``/``last`` the
    change applies to those vectors only (a copied WaveformTable); without them
    it changes the WaveformTable itself for every vector that uses it.
``set_char``      ``{"op": "set_char", "signal": "io_0", "char": "H", "first": i, "last": j}``
    Write another WaveformChar of the same WaveformTable for one signal in
    vectors ``i..j`` (``0`` -> ``1``, ``H`` -> ``L``, and on a bidirectional pin
    also drive <-> compare). The character must be defined for that signal in
    the vector's WaveformTable; the statement keeps its target and label.
``rename_signal`` ``{"op": "rename_signal", "signal": "old", "to": "new"}``
    Rename a signal everywhere (Signals, SignalGroups, waveforms, vector
    targets, the pin map). Refused when the name is also used for something else.
``add_signal``    ``{"op": "add_signal", "signal": "new", "direction": "In", "fill": "0"}``
    Declare a signal, give it waveforms in every WaveformTable (In: 0/1/Z
    drives; Out: H/L/X compares at the table's compare time; InOut: both) and
    write ``fill`` for it in every vector. Each vector target gets a generated
    group that adds the signal; a pin map gets the next free bit.
``remove_signal`` ``{"op": "remove_signal", "signal": "name"}``
    Delete a signal from Signals, its groups, its waveforms, every vector and
    the pin map. A vector that assigns only that signal cannot lose it.

Generated WaveformTables and groups are named ``STPGV_*``. Only top-level
vectors can be edited one by one; vectors produced by a Loop, Macro or
Procedure would change every repetition and are rejected. Signal operations
change every vector statement, also those inside Loops and Macros.

When the signal set changes, the edited copy comes with a pin map that keeps
every remaining signal on its bit (derived from the default declaration-order
map when no pin map was given), so removing a signal never moves the others.
"""

from __future__ import annotations

from array import array
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from stil.ast import Document, ResolvedLoop, WaveformTableDecl
from stil.resolve import _find_waveform_char_decl
from stil.vector_data import WAVEFORM_CHAR_ALPHABET, decode_vector_data

from .model import Trace

EDITS_SCHEMA = 1
NAME_PREFIX = "STPGV_"
_UNITS = {"s": Decimal(1), "ms": Decimal("1e-3"), "us": Decimal("1e-6"), "ns": Decimal("1e-9"),
          "ps": Decimal("1e-12"), "fs": Decimal("1e-15")}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# One vector statement: optional label, keyword, target, data. The subset has one target per statement.
_V_STMT = re.compile(r'(?:(?:"[^"]*"|[A-Za-z_][A-Za-z0-9_]*)\s*:\s*)?(?<![A-Za-z0-9_])(?:V|Vector)\s*\{\s*'
                     r'("[^"]*"|[A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;{}]*);\s*\}')
_STIL_WORDS = {"V", "Vector", "W", "WaveformTable", "Loop", "Macro", "Call", "Goto", "Stop", "Signals",
               "SignalGroups", "Timing", "Waveforms", "Period", "Pattern", "PatternBurst", "PatternExec",
               "PatList", "MacroDefs", "Procedures", "Header", "Title", "In", "Out", "InOut", "Drive",
               "Compare", "DirZ", "DirIn", "DirOut", "STIL"}
DIRECTIONS = {"In": "input", "Out": "output", "InOut": "inout"}


class EditError(ValueError):
    pass


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def parse_time(text: str) -> Decimal:
    """Seconds of a STIL-style time literal such as ``120ns`` or ``'4ms'``."""

    raw = str(text).strip().strip("'").replace("µ", "u").replace(" ", "")
    match = re.fullmatch(r"([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)(fs|ps|ns|us|ms|s)", raw)
    if not match:
        raise EditError(f"bad time {text!r} (use e.g. 120ns, 1.5us, 4ms)")
    try:
        return Decimal(match.group(1)) * _UNITS[match.group(2)]
    except InvalidOperation as exc:
        raise EditError(f"bad time {text!r}") from exc


def time_text(text: str) -> str:
    """Normalise a user time to the compact STIL form (``4ms``)."""

    parse_time(text)
    return str(text).strip().strip("'").replace("µ", "u").replace(" ", "")


def _stil_name(name: str) -> str:
    return name if _IDENT.match(name) else '"' + name.replace('"', '\\"') + '"'


def _scan_to_matching_brace(text: str, open_index: int) -> int:
    """Index just after the ``}`` that closes ``text[open_index] == '{'``."""

    depth = 0
    i = open_index
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            end = text.find(ch, i + 1)
            i = n if end < 0 else end + 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise EditError("unbalanced braces in STIL source")


def _gap_is_blank(text: str) -> bool:
    """True when a text span has only whitespace and comments (no statements)."""

    stripped = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)
    return not stripped.strip()


def _comment_spans(text: str, *, strings: bool = False) -> tuple[list[int], list[int]]:
    """Sorted (starts, ends) of the comments of a STIL text, and of its "strings" when asked."""

    starts: list[int] = []
    ends: list[int] = []
    for match in re.finditer(r'//[^\n]*|/\*.*?\*/|"[^"\n]*"', text, flags=re.S):
        if strings or match.group().startswith("/"):
            starts.append(match.start())
            ends.append(match.end())
    return starts, ends


def _in_spans(offset: int, spans: tuple[list[int], list[int]]) -> bool:
    k = bisect_right(spans[0], offset) - 1
    return k >= 0 and offset < spans[1][k]


def vector_statements(text: str):  # type: ignore[no-untyped-def]
    """Every vector statement outside comments: (start incl. label, end, label text, target, data)."""

    comments = _comment_spans(text)
    for match in _V_STMT.finditer(text):
        if _in_spans(match.start(), comments):
            continue
        keyword = re.search(r"(?<![A-Za-z0-9_])(?:V|Vector)\s*\{", match.group())
        label = match.group()[:keyword.start()] if keyword else ""
        yield match.start(), match.end(), label, match.group(1), match.group(2)


def describe_char(events: list[list[str]]) -> tuple[str, str]:
    """(kind, text) of one WaveformChar definition: drive / compare / release / other."""

    kinds = set()
    for _t, action, arg in events:
        arg = (arg or "").strip().upper()
        if action == "Drive" and arg in ("Z",):
            kinds.add("release")
        elif action == "Drive":
            kinds.add("drive")
        elif action.startswith("Dir"):
            kinds.add("release" if action == "DirZ" else "drive")
        elif action == "Compare":
            kinds.add("compare")
    kind = "drive" if kinds == {"drive"} else "compare" if kinds == {"compare"} else \
        "release" if kinds == {"release"} else "mixed" if kinds else "none"
    text = ", ".join(f"{t} {action}" + (f"({arg})" if arg is not None else "") for t, action, arg in events)
    return kind, text


@dataclass
class _Vector:
    index: int  # logical (expanded) index of a top-level vector; -1 inside a Loop
    line: int
    column: int
    table: str
    top_level: bool
    start: int = -1  # char offset of the statement (label included)
    end: int = -1  # char offset just after the closing brace


@dataclass
class EditResult:
    text: str
    inserted_after: dict[int, int]  # original vector index -> vectors inserted right after it
    generated_tables: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    pin_map: dict | None = None  # the edited copy's pin map when the signal set or names changed
    signals: dict[str, str] = field(default_factory=dict)  # original name -> final name (renames)


class PreviewMap:
    """Edited preview vector <-> original vector, with the inserted wait vectors in between."""

    def __init__(self, inserted_after: dict[int, int], count: int) -> None:
        self.count = count  # vectors in the preview
        self.positions: list[int] = []  # preview indices of inserted vectors, ascending
        shift = 0
        for source in sorted(inserted_after):
            for k in range(inserted_after[source]):
                self.positions.append(source + shift + 1 + k)
            shift += inserted_after[source]

    def to_source(self, index: int) -> int | None:
        if not 0 <= index < self.count:
            return None
        k = bisect_right(self.positions, index)
        if k and self.positions[k - 1] == index:
            return None
        return index - k

    def to_preview(self, source: int) -> int:
        index = source
        for position in self.positions:
            if position <= index:
                index += 1
        return index

    # dict-like access (preview index -> original index)
    def __getitem__(self, index: int) -> int:
        value = self.to_source(index)
        if value is None:
            raise KeyError(index)
        return value

    def __contains__(self, index: object) -> bool:
        return isinstance(index, int) and self.to_source(index) is not None

    def values(self):  # type: ignore[no-untyped-def]
        return (self[i] for i in range(self.count) if i in self)


class _Vectors(Sequence):
    """Logical vector -> its statement occurrence (one object per statement, shared by Loop repetitions)."""

    def __init__(self, occurrences: list[_Vector], logical: array) -> None:
        self.occurrences = occurrences
        self.logical = logical

    def __len__(self) -> int:
        return len(self.logical)

    def __getitem__(self, index):  # type: ignore[no-untyped-def]
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        return self.occurrences[self.logical[index]]


class StilEditor:
    """Parses one STIL source once and applies edit lists to copies of it."""

    def __init__(self, source: str | Path, *, pin_map: str | Path | None = None,
                 pattern_burst: str | None = None, pattern: str | None = None) -> None:
        from .load_stil import _expand, _parse_and_resolve

        self.source = Path(source)
        self.pin_map = Path(pin_map) if pin_map else None
        self.text = self.source.read_text()
        self.sha256 = hashlib.sha256(self.text.encode()).hexdigest()
        document, pin_map_data, resolved = _parse_and_resolve(self.source, self.pin_map, pattern_burst, pattern)
        self.document: Document = document
        self.pin_map_data = pin_map_data
        self._line_starts: list[int] | None = None
        occurrences = resolved.vectors
        top_ids = {id(item) for item in resolved.items if not isinstance(item, ResolvedLoop)} \
            if resolved.items else {id(v) for v in occurrences}
        self._occurrences: list[_Vector] = []
        for vector in occurrences:
            location = vector.location
            top = id(vector) in top_ids and not location.expansion_trace and \
                location.source_file == document.source_file
            self._occurrences.append(_Vector(index=-1, line=location.line, column=location.column,
                                             table=vector.waveform_table_name, top_level=top))
        occurrence_of = {id(v): k for k, v in enumerate(occurrences)}
        if resolved.items:
            logical = _expand(resolved.items, occurrence_of)
            index = 0  # logical index of each top-level vector statement
            for item in resolved.items:
                if isinstance(item, ResolvedLoop):
                    index += item.loop_count * _count(item.items)
                else:
                    self._occurrences[occurrence_of[id(item)]].index = index
                    index += 1
        else:
            logical = array("i", range(len(occurrences)))
            for k, entry in enumerate(self._occurrences):
                entry.index = k
        self.vectors = _Vectors(self._occurrences, logical)
        self.tables = {table.name: table for table in document.waveform_tables}
        self.groups = {group.name: group.signals for group in document.signal_groups}
        first = occurrences[0].signal_chars if occurrences else {}
        declared = [s.name for s in document.signals]
        self.signal_order = [n for n in declared if n in first] + [n for n in first if n not in set(declared)]
        self.directions = {s.name: s.direction for s in document.signals}

    # ---- source positions ------------------------------------------------
    def _offset(self, line: int, column: int) -> int:
        if self._line_starts is None:
            self._line_starts = [0] + [m.end() for m in re.finditer("\n", self.text)]
        return self._line_starts[line - 1] + max(0, column - 1)

    def _locate(self, vector: _Vector) -> None:
        if vector.start >= 0:
            return
        start = self._offset(vector.line, vector.column)
        line_start = self._offset(vector.line, 1)
        head = self.text[line_start:start]
        label = re.search(r'(?:"[^"]*"|[A-Za-z_][A-Za-z0-9_]*)\s*:\s*$', head)
        if label:
            start = line_start + label.start()
        match = re.compile(r'(?:(?:"[^"]*"|[A-Za-z_][A-Za-z0-9_]*)\s*:\s*)?(?:V|Vector)\s*\{').match(self.text, start)
        if not match:
            raise EditError(f"cannot find the vector statement at line {vector.line}")
        vector.start = start
        vector.end = _scan_to_matching_brace(self.text, match.end() - 1)

    def vector(self, index: int) -> _Vector:
        if not 0 <= index < len(self.vectors):
            raise EditError(f"vector {index} is out of range")
        vector = self.vectors[index]
        if not vector.top_level:
            raise EditError(f"vector {index} (line {vector.line}) is produced by a Loop/Macro/Procedure; "
                            "editing it would change every repetition")
        self._locate(vector)
        return vector

    def top_level_at(self, index: int) -> _Vector | None:
        """The top-level vector statement at a logical index, or None (out of range, inside a Loop)."""

        if not 0 <= index < len(self.vectors):
            return None
        vector = self.vectors[index]
        if not vector.top_level:
            return None
        self._locate(vector)
        return vector

    # ---- waveform tables ---------------------------------------------------
    def expanded_table(self, name: str, signals: list[str] | None = None,
                       added: dict[str, dict] | None = None) -> tuple[str, dict[str, dict[str, list[list[str]]]]]:
        """(period text, signal -> char -> [[time, action, arg], ...]) with group definitions resolved.

        ``signals`` is the signal list of the edited copy and ``added`` the waveforms of added signals.
        """

        table: WaveformTableDecl = self.tables[name]
        order = self.signal_order if signals is None else signals
        chars_by_signal: dict[str, set[str]] = {s: set() for s in order}
        for waveform in table.waveforms:
            members = self.groups.get(waveform.signal_or_group, (waveform.signal_or_group,))
            for signal in members:
                if signal in chars_by_signal:
                    chars_by_signal[signal].update(c.char for c in waveform.chars)
        result: dict[str, dict[str, list[list[str]]]] = {}
        for signal in order:
            if added and signal in added:
                result[signal] = json.loads(json.dumps(added[signal][name]))
                continue
            per_char: dict[str, list[list[str]]] = {}
            for char in sorted(chars_by_signal[signal]):
                decl = _find_waveform_char_decl(table, self.groups, signal, char)
                if decl is None or decl.char != char:
                    continue
                per_char[char] = [[e.time_text, e.action_name, e.action_arg] for e in decl.events]
            result[signal] = per_char
        return table.period_text, result

    def char_choices(self, index: int, signal: str) -> list[dict]:
        """The WaveformChars one signal can take in the WaveformTable of a vector, with what each does."""

        table = self.vectors[index].table
        _period, waveforms = self.expanded_table(table, [signal])
        return [{"char": char, "kind": describe_char(events)[0], "text": describe_char(events)[1]}
                for char, events in waveforms.get(signal, {}).items()]

    @staticmethod
    def table_text(name: str, period: str, waveforms: dict[str, dict[str, list[list[str]]]], indent: str) -> str:
        lines = [f"{indent}WaveformTable {name} {{", f"{indent}  Period '{period}';", f"{indent}  Waveforms {{"]
        for signal, chars in waveforms.items():
            line = StilEditor.waveform_line(signal, chars, f"{indent}    ")
            if line:
                lines.append(line)
        lines += [f"{indent}  }}", f"{indent}}}"]
        return "\n".join(lines)

    @staticmethod
    def waveform_line(signal: str, chars: dict[str, list[list[str]]], indent: str) -> str:
        parts = []
        for char, events in chars.items():
            rendered = ", ".join(f"{t} {action}" + (f"({arg})" if arg is not None else "") for t, action, arg in events)
            parts.append(f"{char}: {rendered};")
        return f"{indent}{_stil_name(signal)} {{ {' '.join(parts)} }}" if parts else ""

    def _table_block(self, name: str) -> tuple[int, int, str]:
        table = self.tables[name]
        start = self._offset(table.location.line, table.location.column)
        match = re.compile(r"WaveformTable\b[^{]*\{").search(self.text, start)
        if not match:
            raise EditError(f"cannot find WaveformTable {name}")
        end = _scan_to_matching_brace(self.text, match.end() - 1)
        line_start = self.text.rfind("\n", 0, match.start()) + 1
        indent = re.match(r"[ \t]*", self.text[line_start:]).group(0)
        return line_start, end, indent

    def _waveforms_block(self, name: str) -> tuple[int, int, str]:
        """(offset of the line of the closing brace, block start, indent of its entries) of a table's Waveforms."""

        start, end, indent = self._table_block(name)
        match = re.compile(r"Waveforms\s*\{").search(self.text, start, end)
        if not match:
            raise EditError(f"WaveformTable {name} has no Waveforms block")
        close = _scan_to_matching_brace(self.text, match.end() - 1) - 1
        close_line = self.text.rfind("\n", 0, close) + 1
        return close_line, match.end(), indent + "    "

    def _compare_time(self, name: str) -> str:
        """The first compare time used in a table (for an added output), else half the period."""

        for waveform in self.tables[name].waveforms:
            for char in waveform.chars:
                for event in char.events:
                    if event.action_name == "Compare":
                        return event.time_text
        period = parse_time(self.tables[name].period_text)
        return f"{(period / 2 / _UNITS['ns']).normalize():f}ns"

    def _added_waveforms(self, direction: str) -> dict[str, dict[str, list[list[str]]]]:
        result = {}
        for name in self.tables:
            chars: dict[str, list[list[str]]] = {}
            if direction in ("In", "InOut"):
                chars.update({"0": [["0ns", "Drive", "0"]], "1": [["0ns", "Drive", "1"]], "Z": [["0ns", "Drive", "Z"]]})
            if direction in ("Out", "InOut"):
                strobe = self._compare_time(name)
                chars.update({"H": [[strobe, "Compare", "1"]], "L": [[strobe, "Compare", "0"]],
                              "X": [[strobe, "Compare", "X"]]})
            result[name] = chars
        return result

    def current_names(self, edits: list[dict]) -> dict[str, tuple[str, str]]:
        """Signal names after an edit list: name -> ("source", source name) or ("added", direction)."""

        names: dict[str, tuple[str, str]] = {name: ("source", name) for name in self.signal_order}
        for edit in edits:
            op = edit.get("op")
            if op == "rename_signal" and edit.get("signal") in names:
                names[str(edit.get("to"))] = names.pop(edit["signal"])
            elif op == "add_signal":
                names[str(edit.get("signal"))] = ("added", edit.get("direction") or "InOut")
            elif op == "remove_signal":
                names.pop(edit.get("signal"), None)
        return names

    # ---- applying ------------------------------------------------------------
    def _used_names(self) -> set[str]:
        """Identifiers of the source outside comments (a new name must be none of them)."""

        comments = _comment_spans(self.text)
        return {m.group() for m in _WORD.finditer(self.text) if not _in_spans(m.start(), comments)}

    def _other_names(self) -> set[str]:
        d = self.document
        names = {g.name for g in d.signal_groups} | {t.name for t in d.waveform_tables} | \
            {p.name for p in d.patterns} | {b.name for b in d.pattern_bursts} | \
            {m.name for m in d.macros} | {p.name for p in d.procedures}
        names |= {m.group(1) for m in re.finditer(r'(?<![A-Za-z0-9_"])([A-Za-z_][A-Za-z0-9_]*)\s*:(?!\s*[0-9])', self.text)}
        return names | _STIL_WORDS

    def apply(self, edits: list[dict], trace: Trace) -> EditResult:
        """Return the edited STIL text. ``trace`` is the viewer trace of the source (for held states)."""

        notes: list[str] = []
        base_mods: dict[str, list[dict]] = {}  # table -> whole-table edge edits
        vector_mods: dict[int, list[int]] = {}  # vector -> edit ids changing its table
        char_mods: dict[int, dict[str, str]] = {}  # vector -> signal (original or added name) -> char
        waits_after: dict[int, list[str]] = {}
        # Names: ``current`` maps the names in use at this point of the list to the source (or added) name.
        current = {name: name for name in self.signal_order}
        added: dict[str, dict] = {}  # added name -> {"direction", "fill", "waveforms"}
        removed: list[str] = []
        renamed: dict[str, str] = {}  # source/added name -> final name
        used = None

        def signal_of(name: str) -> str:
            if name not in current:
                raise EditError(f"unknown signal {name!r}")
            return current[name]

        def check_new_name(name: str) -> None:
            nonlocal used
            if not _IDENT.match(name or ""):
                raise EditError(f"{name!r} is not a STIL name (letters, digits and _; not starting with a digit)")
            if used is None:
                used = self._used_names() | _STIL_WORDS
            if name in used or name in current or name in added or name in renamed.values() \
                    or name.startswith(NAME_PREFIX):
                raise EditError(f"the name {name!r} is already used in this STIL")

        for edit_id, edit in enumerate(edits):
            op = edit.get("op")
            if op == "insert_wait":
                vector = self.vector(int(edit["after"]))
                duration = time_text(edit["duration"])
                if parse_time(duration) <= 0:
                    raise EditError("wait duration must be positive")
                waits_after.setdefault(vector.index, []).append(duration)
            elif op in ("set_period", "edge_time"):
                first, last = edit.get("first"), edit.get("last")
                if op == "edge_time":
                    signal = signal_of(edit.get("signal"))
                    if signal in added:
                        raise EditError(f"{edit.get('signal')} was added by an edit; its waveforms are generated")
                    edit = dict(edit, signal=signal)
                if op == "edge_time" and first is None and last is None:
                    table = edit.get("table") or self.vectors[int(edit.get("at", 0))].table
                    edit = dict(edit, table=table)
                    base_mods.setdefault(table, []).append(edit)
                    continue
                first = int(first)
                last = int(first if last is None else last)
                if last < first:
                    raise EditError("vector range is empty")
                tables = {self.vector(i).table for i in range(first, last + 1)}
                if len(tables) != 1:
                    raise EditError(f"vectors {first}..{last} use several WaveformTables {sorted(tables)}; "
                                    "split the range")
                for i in range(first, last + 1):
                    vector_mods.setdefault(i, []).append(edit_id)
                edits = edits[:edit_id] + [edit] + edits[edit_id + 1:]
            elif op == "set_char":
                signal = signal_of(edit.get("signal"))
                char = str(edit.get("char", ""))
                if len(char) != 1 or char not in WAVEFORM_CHAR_ALPHABET:
                    raise EditError(f"{char!r} is not a WaveformChar")
                first = int(edit["first"])
                last = int(edit.get("last", first) if edit.get("last") is not None else first)
                if last < first:
                    raise EditError("vector range is empty")
                defined_in: dict[str, set[str]] = {}
                for i in range(first, last + 1):
                    vector = self.vector(i)
                    defined = defined_in.get(vector.table)
                    if defined is None and signal in added:
                        defined = defined_in[vector.table] = set(added[signal]["waveforms"][vector.table])
                    elif defined is None:
                        defined = defined_in[vector.table] = set(
                            self.expanded_table(vector.table, [signal])[1].get(signal, {}))
                    if char not in defined:
                        raise EditError(f"{edit.get('signal')} has no character {char!r} in WaveformTable "
                                        f"{vector.table} (vector {i}); defined: {''.join(sorted(defined)) or 'none'}")
                    char_mods.setdefault(i, {})[signal] = char
            elif op == "rename_signal":
                old = edit.get("signal")
                signal = signal_of(old)
                new = str(edit.get("to", "")).strip()
                check_new_name(new)
                if signal not in added and signal in self._other_names():
                    raise EditError(f"{signal} is also used as another name (group, table, pattern, label); "
                                    "rename it in the source")
                del current[old]
                current[new] = signal
                renamed[signal] = new
            elif op == "add_signal":
                name = str(edit.get("signal", "")).strip()
                check_new_name(name)
                direction = edit.get("direction") or "InOut"
                if direction not in DIRECTIONS:
                    raise EditError(f"direction must be In, Out or InOut, not {direction!r}")
                waveforms = self._added_waveforms(direction)
                fill = str(edit.get("fill") or {"In": "0", "Out": "X", "InOut": "Z"}[direction])
                if any(fill not in chars for chars in waveforms.values()):
                    raise EditError(f"fill {fill!r} is not a character of an added {direction} signal "
                                    f"({''.join(next(iter(waveforms.values())))})")
                added[name] = {"direction": direction, "fill": fill, "waveforms": waveforms}
                current[name] = name
            elif op == "remove_signal":
                old = edit.get("signal")
                signal = signal_of(old)
                del current[old]
                if signal in added:
                    del added[signal]
                    for mods in char_mods.values():
                        mods.pop(signal, None)
                    continue
                if len(current) == 0:
                    raise EditError("the last signal cannot be removed")
                removed.append(signal)
                renamed.pop(signal, None)
            else:
                raise EditError(f"unknown edit op {op!r}")

        final_signals = [s for s in self.signal_order if s not in removed] + list(added)
        if not final_signals:
            raise EditError("no signal would remain")
        signals_changed = bool(added or removed)
        added_waveforms = {name: info["waveforms"] for name, info in added.items()}
        if signals_changed:
            for (_vector, mods) in char_mods.items():
                for signal in list(mods):
                    if signal in removed:
                        del mods[signal]

        # Base tables (whole-table edge edits applied in place, same name).
        base: dict[str, tuple[str, dict]] = {}
        for name in self.tables:
            period, waveforms = self.expanded_table(name, final_signals, added_waveforms)
            for mod in base_mods.get(name, []):
                waveforms = self._move_edge(name, period, waveforms, mod)
            base[name] = (period, waveforms)

        # One generated table per distinct (base table, ordered edit ids).
        generated: dict[tuple[str, tuple[int, ...]], str] = {}
        generated_defs: dict[str, tuple[str, tuple[str, dict]]] = {}  # name -> (base table, definition)
        effective: dict[int, str] = {}
        for i in sorted(vector_mods):
            vector = self.vectors[i]
            key = (vector.table, tuple(vector_mods[i]))
            name = generated.get(key)
            if name is None:
                period, waveforms = base[vector.table]
                waveforms = json.loads(json.dumps(waveforms))
                for edit_id in key[1]:
                    mod = edits[edit_id]
                    if mod["op"] == "set_period":
                        period = time_text(mod["period"])
                        self._check_events_fit(vector.table, period, waveforms)
                    else:
                        waveforms = self._move_edge(vector.table, period, waveforms, mod)
                name = self._unique_name(f"{NAME_PREFIX}{vector.table}_{len(generated) + 1}")
                generated[key] = name
                generated_defs[name] = (vector.table, (period, waveforms))
            effective[i] = name

        def effective_table(i: int) -> str:
            return effective.get(i) or self.vectors[i].table

        wait_tables: dict[str, str] = {}
        for durations in waits_after.values():
            for duration in durations:
                if duration not in wait_tables:
                    wait_tables[duration] = self._unique_name(f"{NAME_PREFIX}WAIT_{_safe(duration)}")

        inserts: list[tuple[int, float, object]] = []  # (offset, order, text | ("REPLACE", end, text))
        # Table definitions: modified base tables replace their block; new tables follow their base.
        for name, mods in base_mods.items():
            start, end, indent = self._table_block(name)
            period, waveforms = base[name]
            inserts.append((start, 0, ("REPLACE", end, self.table_text(name, period, waveforms, indent))))
            notes.append(f"WaveformTable {name}: {len(mods)} edge change(s) applied to every vector")
        new_tables_after: dict[str, list[str]] = {}
        for name, (base_name, _definition) in generated_defs.items():
            new_tables_after.setdefault(base_name, []).append(name)
        for base_name, names in new_tables_after.items():
            _start, end, indent = self._table_block(base_name)
            text = "".join("\n" + self.table_text(name, *generated_defs[name][1], indent) for name in names)
            inserts.append((end, 1, text))
        group_lines: list[str] = []  # generated SignalGroups entries (without indent)
        if wait_tables:
            anchor = next(iter(self.tables))
            _start, end, indent = self._table_block(anchor)
            hold = {"0": [["0ns", "Drive", "0"]], "1": [["0ns", "Drive", "1"]], "Z": [["0ns", "Drive", "Z"]]}
            text = "".join(
                "\n" + self.table_text(name, duration, {s: hold for s in final_signals}, indent)
                for duration, name in wait_tables.items())
            inserts.append((end, 2, text))
            members = " + ".join(_stil_name(s) for s in final_signals)
            group_lines.append(f"{NAME_PREFIX}ALL = '{members}';")

        if signals_changed:
            self._signal_declarations(inserts, added, removed, base_mods)
            group_lines += self._vector_rewrites(inserts, added, removed, char_mods)
            notes.extend(f"signal {name} added ({info['direction']}, every vector '{info['fill']}')"
                         for name, info in added.items())
            notes.extend(f"signal {name} removed" for name in removed)
        else:
            for i, mods in sorted(char_mods.items()):
                self._rewrite_vector(inserts, self.vector(i), mods, {}, [])
        if char_mods:
            count = sum(len(m) for m in char_mods.values())
            notes.append(f"{count} character(s) changed in {len(char_mods)} vector(s)")
        if group_lines:
            self._insert_groups(inserts, group_lines)

        # Waveform selections around edited vectors and inserted wait vectors. Only vectors that
        # carry an edit are visited, so large patterns stay cheap.
        for i in sorted(set(vector_mods) | set(waits_after)):
            vector = self.vector(i)
            eff = effective_table(i)
            prev = self.top_level_at(i - 1)
            nxt = self.top_level_at(i + 1)
            joined_prev = prev is not None and effective_table(i - 1) == eff and \
                _gap_is_blank(self.text[prev.end:vector.start])
            joined_next = nxt is not None and effective_table(i + 1) == eff and \
                _gap_is_blank(self.text[vector.end:nxt.start])
            indent = self._indent_at(vector.start)
            if eff != vector.table and not joined_prev:
                inserts.append((vector.start, 0, f"W {eff};\n{indent}"))
            after = ""
            active_after = eff
            if eff != vector.table and not joined_next:
                after += f"\n{indent}W {vector.table};"
                active_after = vector.table
            for duration in waits_after.get(i, []):
                chars = self._held_chars(trace, i, final_signals, added)
                after += (f"\n{indent}W {wait_tables[duration]};"
                          f"\n{indent}V {{ {NAME_PREFIX}ALL = {chars}; }}"
                          f"\n{indent}W {active_after};")
            if after:
                inserts.append((vector.end, 1, after))

        text, _added_lines = _apply_inserts(self.text, inserts)
        if renamed:
            text = _rename_all(text, renamed)
            notes.extend(f"signal {old} renamed to {new}" for old, new in renamed.items())
        header = (f"// Edited by stpg-view from {self.source.name} (sha256 {self.sha256[:16]}); "
                  f"{len(edits)} edit(s). Generated tables use the {NAME_PREFIX} prefix.\n")
        pin_map = self._edited_pin_map(added, removed, renamed) if (signals_changed or renamed) else None
        return EditResult(text=header + text, inserted_after={i: len(d) for i, d in waits_after.items()},
                          generated_tables=list(generated_defs) + list(wait_tables.values()), notes=notes,
                          pin_map=pin_map, signals=dict(renamed))

    # ---- signal-set edits ------------------------------------------------------
    def _signal_declarations(self, inserts: list, added: dict[str, dict], removed: list[str],
                             replaced_tables: dict) -> None:
        """Signals block lines, and the waveform lines of every table not regenerated as a whole."""

        signal_lines = {s.name: s.location.line for s in self.document.signals}
        for name in removed:
            if name in signal_lines:
                start = self._offset(signal_lines[name], 1)
                end = self.text.find("\n", start) + 1 or len(self.text)
                inserts.append((start, 0, ("REPLACE", end, "")))
        if added:
            block = re.search(r"(?m)^[ \t]*Signals\s*\{", self.text)
            if not block:
                raise EditError("cannot find the Signals block")
            close = _scan_to_matching_brace(self.text, block.end() - 1) - 1
            close_line = self.text.rfind("\n", 0, close) + 1
            indent = re.match(r"[ \t]*", self.text[block.start():]).group(0) + "  "
            text = "".join(f"{indent}{name} {info['direction']};\n" for name, info in added.items())
            inserts.append((close_line, 0.2, text))
        for table in self.tables:
            if table in replaced_tables:
                continue
            close_line, body_start, indent = self._waveforms_block(table)
            for name in removed:
                for match in re.finditer(rf"(?m)^[ \t]*{re.escape(name)}\s*\{{[^\n]*\n?", self.text[body_start:close_line]):
                    inserts.append((body_start + match.start(), 0, ("REPLACE", body_start + match.end(), "")))
            if added:
                text = "".join(self.waveform_line(name, info["waveforms"][table], indent) + "\n"
                               for name, info in added.items())
                inserts.append((close_line, 0.2, text))
        groups_with_removed = [g for g in self.document.signal_groups if set(g.signals) & set(removed)]
        for group in groups_with_removed:
            remaining = [s for s in group.signals if s not in removed]
            if not remaining:
                raise EditError(f"group {group.name} would have no signal left")
            line_start = self._offset(group.location.line, 1)
            line_end = self.text.find("\n", line_start)
            line_end = len(self.text) if line_end < 0 else line_end
            match = re.compile(r"'[^']*'").search(self.text, line_start, line_end)
            if not match:
                raise EditError(f"cannot find the members of group {group.name}")
            inserts.append((match.start(), 0, ("REPLACE", match.end(), "'" + " + ".join(remaining) + "'")))

    def _vector_rewrites(self, inserts: list, added: dict[str, dict], removed: list[str],
                         char_mods: dict[int, dict[str, str]]) -> list[str]:
        """Rewrite every vector statement for an added/removed signal; returns generated group lines."""

        by_start = {}
        for i in char_mods:
            vector = self.vector(i)
            by_start[vector.start] = char_mods[i]
        plus_groups: dict[str, str] = {}
        for start, end, label, target, data in vector_statements(self.text):
            members = self.groups.get(target, (target,))
            mods = by_start.get(start, {})
            if not added and not (set(members) & set(removed)) and not mods:
                continue
            if added and target not in plus_groups:
                bare = target.strip('"')
                plus_groups[target] = self._unique_name(f"{NAME_PREFIX}{bare}_PLUS", taken=plus_groups.values())
            self._rewrite_statement(inserts, start, end, label, target, data, mods, added, removed,
                                    plus_groups.get(target))
        lines = []
        for target, name in plus_groups.items():
            members = [s for s in self.groups.get(target, (target,)) if s not in removed] + list(added)
            lines.append(f"{name} = '{' + '.join(members)}';")
        return lines

    def _rewrite_vector(self, inserts: list, vector: _Vector, mods: dict[str, str], added: dict, removed: list) -> None:
        match = _V_STMT.match(self.text, vector.start)
        if not match or match.end() != vector.end:
            raise EditError(f"vector at line {vector.line}: only 'V {{ target = data; }}' statements can be rewritten")
        keyword = re.search(r"(?<![A-Za-z0-9_])(?:V|Vector)\s*\{", match.group())
        label = match.group()[:keyword.start()] if keyword else ""
        self._rewrite_statement(inserts, vector.start, vector.end, label, match.group(1), match.group(2), mods,
                                added, removed, None)

    def _rewrite_statement(self, inserts: list, start: int, end: int, label: str, target: str, data: str,
                           mods: dict[str, str], added: dict, removed: list, plus_group: str | None) -> None:
        members = list(self.groups.get(target, (target,)))
        line = self.text.count("\n", 0, start) + 1
        try:
            chars = list(decode_vector_data(data.strip(), max_width=len(members)))
        except Exception as exc:  # noqa: BLE001 - the parser accepted it; report where it failed here
            raise EditError(f"line {line}: cannot decode the vector data: {exc}") from exc
        if len(chars) != len(members):
            raise EditError(f"line {line}: {len(chars)} characters for {len(members)} signals of {target}")
        for signal, char in mods.items():
            if signal in members:
                chars[members.index(signal)] = char
            elif signal not in added:
                raise EditError(f"line {line}: the vector assigns {target}, which does not contain {signal}")
        keep = [k for k, s in enumerate(members) if s not in removed]
        if not keep:
            raise EditError(f"line {line}: the vector assigns only {', '.join(members)}; "
                            "removing it would leave an empty vector")
        new_chars = "".join(chars[k] for k in keep)
        new_target = target
        if plus_group is not None:
            new_target = plus_group
            new_chars += "".join(mods.get(name, info["fill"]) for name, info in added.items())
        elif len(keep) != len(members):
            pass  # the group itself loses the signal (_signal_declarations); a lone signal target is refused above
        inserts.append((start, 0.5, ("REPLACE", end, f"{label}V {{ {new_target} = {new_chars}; }}")))

    def _edited_pin_map(self, added: dict[str, dict], removed: list[str], renamed: dict[str, str]) -> dict:
        """The pin map of the edited copy: every kept signal stays on its bit, new ones take free bits."""

        if self.pin_map is not None:
            from stil.json_artifacts import read_json_artifact

            data = json.loads(json.dumps(read_json_artifact(self.pin_map)))
        else:
            # The compiler's default map (declaration order), written out so that the bits stay.
            directions = {"in": "input", "out": "output", "inout": "inout"}
            data = {"active_pins": self.pin_map_data.active_pins, "signals": {
                name: {"bit": a.bit, "logical_pin": a.logical_pin,
                       "direction": directions.get(a.direction.lower(), a.direction.lower())}
                for name, a in self.pin_map_data.signals.items()}}
        signals = data.setdefault("signals", {})
        for name in removed:
            signals.pop(name, None)
        taken = {int(v["bit"]) for v in signals.values() if isinstance(v, dict) and "bit" in v}
        for name, info in added.items():
            limit = 24
            free = next((b for b in range(limit) if b not in taken), None)
            if free is None:
                raise EditError(f"no free pin bit for {name}: all {limit} are used")
            taken.add(free)
            signals[name] = {"bit": free, "logical_pin": f"DUT{free}", "direction": DIRECTIONS[info["direction"]]}
            if free >= int(data.get("active_pins", 16)):
                data["active_pins"] = 24
        renamed_signals = {renamed.get(name, name): value for name, value in signals.items()}
        data["signals"] = renamed_signals
        return data

    # ---- edit details ----------------------------------------------------------
    def _move_edge(self, table: str, period: str, waveforms: dict, mod: dict) -> dict:
        signal = mod.get("signal")
        char = mod.get("char")
        if signal not in waveforms:
            raise EditError(f"unknown signal {signal!r}")
        chars = waveforms[signal]
        if char not in chars:
            raise EditError(f"signal {signal} has no character {char!r} in WaveformTable {table}")
        events = chars[char]
        index = int(mod.get("event", 0))
        if not 0 <= index < len(events):
            raise EditError(f"{signal} '{char}' has {len(events)} event(s); event {index} does not exist")
        new_time = time_text(mod["time"])
        if parse_time(new_time) >= parse_time(period):
            raise EditError(f"time {new_time} is not inside the period {period}")
        updated = json.loads(json.dumps(waveforms))
        updated[signal][char][index][0] = new_time
        times = [parse_time(e[0]) for e in updated[signal][char]]
        if times != sorted(times):
            raise EditError(f"{signal} '{char}': events would be out of order")
        return updated

    @staticmethod
    def _check_events_fit(table: str, period: str, waveforms: dict) -> None:
        limit = parse_time(period)
        if limit <= 0:
            raise EditError("period must be positive")
        for signal, chars in waveforms.items():
            for char, events in chars.items():
                for t, _action, _arg in events:
                    if parse_time(t) >= limit:
                        raise EditError(f"{table}: {signal} '{char}' has an event at {t}, "
                                        f"not inside the new period {period}")

    def _held_chars(self, trace: Trace, index: int, signals: list[str] | None = None,
                    added: dict | None = None) -> str:
        """Characters that hold the outputs present at the end of vector ``index``."""

        from .model import apply_events

        value, enable = apply_events(trace.start_values[index], trace.start_enables[index], trace.events(index))
        names = [s.name for s in trace.signals]
        chars = []
        for signal in (self.signal_order if signals is None else signals):
            if added and signal in added:
                fill = added[signal]["fill"]
                chars.append(fill if fill in "01" else "Z")
                continue
            sig = names.index(signal)
            mask = 1 << sig
            chars.append(("1" if value & mask else "0") if enable & mask else "Z")
        return "".join(chars)

    def _insert_groups(self, inserts: list, lines: list[str]) -> None:
        match = re.search(r"\bSignalGroups\b[^{]*\{", self.text)
        if match:
            end = _scan_to_matching_brace(self.text, match.end() - 1)
            close = self.text.rfind("}", 0, end)
            line_start = self.text.rfind("\n", 0, match.start()) + 1
            indent = re.match(r"[ \t]*", self.text[line_start:]).group(0)
            inserts.append((close, 0, "".join(f"  {indent}{line}\n" for line in lines) + indent))
        else:
            timing = re.search(r"^[ \t]*Timing\b", self.text, flags=re.M)
            if not timing:
                raise EditError("cannot find where to add a SignalGroups block")
            inserts.append((timing.start(), 0, "SignalGroups {\n" + "".join(f"  {line}\n" for line in lines) + "}\n"))

    def _indent_at(self, offset: int) -> str:
        line_start = self.text.rfind("\n", 0, offset) + 1
        prefix = self.text[line_start:offset]
        return prefix if not prefix.strip() else re.match(r"[ \t]*", prefix).group(0)

    def _unique_name(self, wanted: str, taken=()) -> str:  # type: ignore[no-untyped-def]
        name = wanted
        n = 2
        while name in self.tables or name in taken or re.search(rf"\b{re.escape(name)}\b", self.text):
            name = f"{wanted}_{n}"
            n += 1
        return name


def _count(items) -> int:  # type: ignore[no-untyped-def]
    total = 0
    for item in items:
        total += item.loop_count * _count(item.items) if isinstance(item, ResolvedLoop) else 1
    return total


def _rename_all(text: str, renamed: dict[str, str]) -> str:
    """Rename signals in every declaration and reference; vector data and comments stay as they are."""

    comments = _comment_spans(text, strings=True)  # signal names are never quoted in the subset
    data_spans: list[tuple[int, int]] = []
    for match in _V_STMT.finditer(text):
        data_spans.append((match.start(2), match.end(2)))
    data_starts = [s for s, _e in data_spans]

    def in_data(offset: int) -> bool:
        k = bisect_right(data_starts, offset) - 1
        return k >= 0 and offset < data_spans[k][1]

    pattern = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(n) for n in sorted(renamed, key=len, reverse=True))
                         + r")(?![A-Za-z0-9_])")

    def replace(match: re.Match) -> str:
        if _in_spans(match.start(), comments) or in_data(match.start()):
            return match.group()
        return renamed[match.group()]

    return pattern.sub(replace, text)


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "p", text)


def _apply_inserts(text: str, inserts: list) -> tuple[str, object]:
    """Apply (offset, order, text | ("REPLACE", end, text)) edits.

    Returns the new text and ``added_lines(offset, before=...)``: how many lines the
    edits placed before an original offset (``before=True`` also counts edits at
    that exact offset), so line numbers map in O(log n).
    """

    from bisect import bisect_left

    ordered = sorted(inserts, key=lambda item: (item[0], item[1]))
    pieces: list[str] = []
    cursor = 0
    offsets: list[int] = []
    cumulative_lines: list[int] = []
    lines = 0
    for offset, _order, payload in ordered:
        if offset < cursor:
            raise EditError("overlapping edits")
        pieces.append(text[cursor:offset])
        if isinstance(payload, tuple) and payload[0] == "REPLACE":
            _tag, end, replacement = payload
            pieces.append(replacement)
            lines += replacement.count("\n") - text.count("\n", offset, end)
            cursor = end
        else:
            pieces.append(payload)
            lines += payload.count("\n")
            cursor = offset
        offsets.append(offset)
        cumulative_lines.append(lines)
    pieces.append(text[cursor:])

    def added_lines(offset: int, *, before: bool) -> int:
        k = bisect_right(offsets, offset) if before else bisect_left(offsets, offset)
        return cumulative_lines[k - 1] if k else 0

    return "".join(pieces), added_lines


def load_edits(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text())
    if data.get("schema") != EDITS_SCHEMA or not isinstance(data.get("edits"), list):
        raise EditError(f"{path}: not an stpg-view edit list")
    return data


def edits_document(editor: StilEditor, edits: list[dict]) -> dict:
    return {"schema": EDITS_SCHEMA, "kind": "stpg_view_edits", "source": {
        "path": str(editor.source), "sha256": editor.sha256}, "edits": edits}
