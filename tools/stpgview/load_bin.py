"""Load an STPG pattern binary (rate-vector, raw-event or macro) into a Trace.

Signal names come from, in order: an explicit or sibling ``*.map.json``
(compiler output), an explicit pin map, or generic ``DUT<n>`` names.
The rate-vector decoding follows the firmware's group semantics: slots with
the same time form one output event; drive operations, direction updates and
sample masks of a group take effect together.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct

from stpg.constants import (
    STPG_DRIVE_COMPLEMENT,
    STPG_DRIVE_NONE,
    STPG_DRIVE_ONE,
    STPG_DRIVE_VECTOR,
    STPG_DRIVE_ZERO,
    STPG_EVF_COMPARE_POINT,
    STPG_EVF_SAMPLE_POINT,
    STPG_MACROP2VEC_MAGIC,
    STPG_MACROVEC_MAGIC,
    STPG_PATTERN_MAGIC,
    STPG_RATE_SLOT_DIR_UPDATE,
    STPG_RATE_SLOT_DRIVE_OP_MASK,
    STPG_RATE_SLOT_SAMPLE,
    STPG_RATEVEC_MAGIC,
    STPG_T_MIN_TICKS,
)
from stpg.pattern import LoopRefRecord, expand_macro_pattern, unpack_pattern, unpack_rate_vector_full

from .model import KIND_COMPARE, KIND_DRIVE, KIND_RELEASE, Event, Rate, Signal, Trace, format_ps

PS_PER_S = 1_000_000_000_000


def _file_identity(path: Path) -> dict:
    data = path.read_bytes()
    return {"path": str(path), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def find_map(pattern_path: Path) -> Path | None:
    """Return the compiler map stored next to a pattern binary, if any."""

    candidates = [
        pattern_path.with_name(pattern_path.stem + ".map.json"),
        pattern_path.with_name(pattern_path.stem + ".map.json.gz"),
        pattern_path.with_name("pattern.map.json"),
        pattern_path.with_name("pattern.map.json.gz"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def read_map(path: Path) -> dict:
    try:
        from stil.json_artifacts import read_json_artifact
    except ImportError:  # pragma: no cover - the package always ships it
        return json.loads(path.read_text())
    plain = path.with_name(path.name[:-3]) if path.name.endswith(".gz") else path
    return read_json_artifact(plain)


def _signals_from_map(map_data: dict) -> list[Signal]:
    entries = map_data.get("signal_map") or {}
    signals = [
        Signal(name=name, bit=int(info["bit"]), pin=info.get("logical_pin"), declared=info.get("direction", ""))
        for name, info in entries.items()
        if isinstance(info, dict) and "bit" in info
    ]
    return sorted(signals, key=lambda s: s.bit)


def _signals_from_pin_map(path: Path) -> list[Signal]:
    data = json.loads(path.read_text())
    entries = data.get("signals", {})
    signals = [
        Signal(name=name, bit=int(info["bit"]), pin=info.get("logical_pin"), declared=info.get("direction", ""))
        for name, info in entries.items()
    ]
    return sorted(signals, key=lambda s: s.bit)


def _ticks_to_ps(ticks: int, tick_hz: int) -> int:
    return (ticks * PS_PER_S + tick_hz // 2) // tick_hz


def _bits(mask: int):
    bit = 0
    while mask:
        if mask & 1:
            yield bit
        mask >>= 1
        bit += 1


def load_bin(path: str | Path, *, map_path: str | Path | None = None,
             pin_map: str | Path | None = None) -> Trace:
    path = Path(path)
    data = path.read_bytes()
    if len(data) < 4:
        raise ValueError(f"{path}: file too short for an STPG pattern")
    magic = struct.unpack_from("<I", data, 0)[0]

    map_data: dict | None = None
    chosen_map = Path(map_path) if map_path else find_map(path)
    if chosen_map is not None:
        map_data = read_map(chosen_map)

    if map_data and map_data.get("signal_map"):
        signals = _signals_from_map(map_data)
    elif pin_map:
        signals = _signals_from_pin_map(Path(pin_map))
    else:
        signals = []

    if magic == STPG_RATEVEC_MAGIC:
        trace = _load_rate_vector(path, data, signals, map_data)
    elif magic == STPG_PATTERN_MAGIC:
        header, events = unpack_pattern(data)
        trace = _load_events(path, header.tick_hz, header.active_pins, events, signals, kind="raw-event")
    elif magic == STPG_MACROVEC_MAGIC:
        header, events = expand_macro_pattern(data)
        trace = _load_events(path, header.tick_hz, header.active_pins, events, signals, kind="macro-stream")
    elif magic == STPG_MACROP2VEC_MAGIC:
        raise ValueError("macro P2 binaries are not supported by the viewer yet")
    else:
        raise ValueError(f"{path}: unknown pattern magic 0x{magic:08X}")

    trace.sources.insert(0, {"kind": "pattern_bin", **_file_identity(path)})
    if chosen_map is not None:
        trace.sources.append({"kind": "map", **_file_identity(chosen_map)})
    elif pin_map:
        trace.sources.append({"kind": "pin_map", **_file_identity(Path(pin_map))})
    else:
        trace.notes.append("no map.json or pin map: signals are shown as DUT<bit>")
    return trace


def _read_wait_patch(pattern_path: Path) -> dict | None:
    """The wait patch report written next to a waited package binary, if present."""

    for name in ("pattern.wait.patch.json", "pattern.wait.patch.json.gz"):
        candidate = pattern_path.with_name(name)
        if candidate.is_file():
            try:
                data = read_map(candidate)
            except Exception:  # noqa: BLE001 - optional provenance
                return None
            if isinstance(data, dict) and data.get("kind") == "stpg_wait_binary_patch":
                return data
    return None


def _default_signals(active_pins: int) -> list[Signal]:
    return [Signal(name=f"DUT{bit}", bit=bit, pin=f"DUT{bit}") for bit in range(active_pins)]


def _rate_groups(rate, slots):  # type: ignore[no-untyped-def]
    own = list(slots[rate.slot_offset : rate.slot_offset + rate.slot_count])
    own.sort(key=lambda slot: slot.time_ticks)
    groups: list[tuple[int, list]] = []
    for slot in own:
        if groups and groups[-1][0] == slot.time_ticks:
            groups[-1][1].append(slot)
        else:
            groups.append((slot.time_ticks, [slot]))
    return groups


def _load_rate_vector(path: Path, data: bytes, signals: list[Signal], map_data: dict | None) -> Trace:
    header, rates, slots, entries, loop_blocks = unpack_rate_vector_full(data)
    tick_hz = int(header.tick_hz)
    if not signals:
        signals = _default_signals(int(header.active_pins))
    sig_of_bit = {s.bit: i for i, s in enumerate(signals) if s.bit is not None}
    trace = Trace(kind="rate-vector", title=path.name, signals=signals)

    # A wait is a cloned rate with the same slot structure and a longer period.
    structure: dict[tuple, list[int]] = {}
    groups_by_rate = []
    for rate_id, rate in enumerate(rates):
        groups = _rate_groups(rate, slots)
        groups_by_rate.append(groups)
        shape = tuple((int(s.flags), int(s.drive_mask), int(s.dir_mask), int(s.sample_mask))
                      for _t, group in groups for s in group)
        structure.setdefault(shape, []).append(rate_id)
    nominal_period = {}
    nominal_rate = {}
    for members in structure.values():
        base_id = min(members, key=lambda r: (int(rates[r].period_ticks), r))
        for r in members:
            nominal_period[r] = int(rates[base_id].period_ticks)
            nominal_rate[r] = base_id
    # Where the added time sits: before the first group whose time moved, else at the end.
    rate_wait_at = {}
    for rate_id in range(len(rates)):
        base_groups = groups_by_rate[nominal_rate[rate_id]]
        own_groups = groups_by_rate[rate_id]
        rate_wait_at[rate_id] = nominal_period[rate_id]
        for (base_time, _g), (own_time, _h) in zip(base_groups, own_groups):
            if own_time != base_time:
                rate_wait_at[rate_id] = int(base_time)
                break
    patch = _read_wait_patch(path)
    patched_vectors = None
    if patch is not None:
        patched_vectors = {int(b["target_logical_vector_index"]): int(b["wait_ticks"])
                           for b in patch.get("applied_boundaries", []) if b.get("target_logical_vector_index") is not None}
        trace.wait_source = "patch"
        trace.notes.append(f"{len(patched_vectors)} wait(s) from pattern.wait.patch.json")
    for rate_id, rate in enumerate(rates):
        extra = int(rate.period_ticks) - nominal_period[rate_id]
        name = f"rate{rate_id}" + (f" = rate{nominal_rate[rate_id]} +{format_ps(_ticks_to_ps(extra, tick_hz))}"
                                   if extra else "")
        trace.rates.append(Rate(name=name, period_ps=_ticks_to_ps(int(rate.period_ticks), tick_hz), detail={
            "period_ticks": int(rate.period_ticks),
            "nominal_period_ticks": nominal_period[rate_id],
            "groups": [{"time_ticks": int(t), "slots": [
                {"flags": int(s.flags), "drive_mask": int(s.drive_mask), "dir_mask": int(s.dir_mask),
                 "sample_mask": int(s.sample_mask)} for s in group]} for t, group in groups_by_rate[rate_id]],
        }))

    # Expand records to logical vectors: (record, repeat, top-level index, loop info).
    occurrences: list[tuple] = []
    for top_index, entry in enumerate(entries):
        if isinstance(entry, LoopRefRecord):
            block = loop_blocks[entry.loop_id]
            for iteration in range(block.loop_count):
                for body_index, record in enumerate(block.body_records):
                    for repeat in range(record.repeat):
                        occurrences.append((record, repeat, top_index, iteration, body_index))
        else:
            for repeat in range(entry.repeat):
                occurrences.append((entry, repeat, top_index, None, None))

    resolver = None
    if map_data is not None and isinstance(map_data.get("vectors"), dict):
        from stil.map_lookup import SourceMapOccurrenceResolver

        resolver = SourceMapOccurrenceResolver(map_data)

    def vector_events(record, rate_id: int, value: int, enable: int):  # type: ignore[no-untyped-def]
        """Return (events, chars, end value, end enable) for one logical vector (bit space)."""

        events: list[Event] = []
        compare_char: dict[int, str] = {}
        drive_levels: dict[int, list[str]] = {}
        for time_ticks, group in groups_by_rate[rate_id]:
            t_ps = _ticks_to_ps(int(time_ticks), tick_hz)
            new_value, new_enable = value, enable
            driven: set[int] = set()
            for slot in group:
                flags = int(slot.flags)
                op = flags & STPG_RATE_SLOT_DRIVE_OP_MASK
                if op != STPG_DRIVE_NONE:
                    for bit in _bits(int(slot.drive_mask)):
                        if op == STPG_DRIVE_VECTOR:
                            level = (record.drive_value >> bit) & 1
                        elif op == STPG_DRIVE_COMPLEMENT:
                            level = ((record.drive_value >> bit) & 1) ^ 1
                        elif op == STPG_DRIVE_ONE:
                            level = 1
                        elif op == STPG_DRIVE_ZERO:
                            level = 0
                        else:
                            continue
                        new_value = (new_value | (1 << bit)) if level else (new_value & ~(1 << bit))
                        driven.add(bit)
                if flags & STPG_RATE_SLOT_DIR_UPDATE:
                    dir_mask = int(slot.dir_mask)
                    new_enable = (new_enable & ~dir_mask) | (record.drive_enable & dir_mask)
                if flags & STPG_RATE_SLOT_SAMPLE:
                    for bit in _bits(int(slot.sample_mask)):
                        if (record.expect_mask >> bit) & 1:
                            compare_char[bit] = "H" if (record.expect_value >> bit) & 1 else "L"
                        else:
                            compare_char.setdefault(bit, "X")
                        sig = sig_of_bit.get(bit)
                        if sig is not None:
                            events.append(Event(sig, t_ps, KIND_COMPARE, compare_char[bit]))
            for bit in set(_bits(new_enable ^ enable)) | driven:
                sig = sig_of_bit.get(bit)
                on = (new_enable >> bit) & 1
                level = (new_value >> bit) & 1
                if on:
                    drive_levels.setdefault(bit, []).append(str(level))
                    was_on = (enable >> bit) & 1
                    if sig is not None and (not was_on or bit in driven or ((value >> bit) & 1) != level):
                        events.append(Event(sig, t_ps, KIND_DRIVE, str(level)))
                elif (enable >> bit) & 1 and sig is not None:
                    events.append(Event(sig, t_ps, KIND_RELEASE))
            value, enable = new_value, new_enable
        chars = []
        for signal in signals:
            bit = signal.bit
            if bit in compare_char:
                chars.append(compare_char[bit])
            elif bit in drive_levels:
                levels = drive_levels[bit]
                if len(set(levels)) > 1:
                    chars.append("R" if levels[-1] == "1" else "F")
                else:
                    chars.append(levels[-1])
            elif bit is not None and (enable >> bit) & 1:
                chars.append("-")  # still driven from an earlier vector
            else:
                chars.append("Z")
        return tuple(events), "".join(chars), value, enable

    def to_signal_mask(bit_mask: int) -> int:
        result = 0
        for bit in _bits(bit_mask):
            sig = sig_of_bit.get(bit)
            if sig is not None:
                result |= 1 << sig
        return result

    # Source provenance first: it decides whether a longer cloned rate is a wait.
    provenance: list[tuple[int | None, str | None, str]] = []
    tables_by_rate: dict[int, set[str]] = {}
    for logical, (record, repeat, top_index, iteration, body_index) in enumerate(occurrences):
        line = label = None
        source_table = ""
        if resolver is not None:
            try:
                if iteration is None:
                    info = resolver.resolve_readonly(top_index, repeat_index=repeat, logical_vector_index=logical)
                else:
                    info = resolver.resolve_readonly(top_index, loop_iteration=iteration,
                                                     body_record_index=body_index, body_repeat_index=repeat,
                                                     logical_vector_index=logical)
                line = info.get("stil_line")
                label = info.get("label")
                source_table = str(info.get("waveform_table") or "")
            except Exception:  # noqa: BLE001 - provenance is optional for display
                line = None
        provenance.append((line, label, source_table))
        if source_table:
            tables_by_rate.setdefault(int(record.rate_id), set()).add(source_table)

    def is_wait_rate(rate_id: int) -> bool:
        """A longer clone of a rate. With a map, it must come from the same WaveformTable
        (a wait patch keeps the table; a STIL special rate uses another table)."""

        if int(rates[rate_id].period_ticks) == nominal_period[rate_id]:
            return False
        own = tables_by_rate.get(rate_id)
        base = tables_by_rate.get(nominal_rate[rate_id])
        return not own or not base or bool(own & base)

    value = enable = 0
    occurrence_rate: list[int] = []
    start_bits: list[tuple[int, int]] = []
    inferred = 0
    for logical, (record, _repeat, _top, _iteration, _body) in enumerate(occurrences):
        rate_id = int(record.rate_id)
        _events, chars, end_value, end_enable = vector_events(record, rate_id, value, enable)
        line, label, source_table = provenance[logical]
        period_ticks = int(rates[rate_id].period_ticks)
        if source_table.startswith("STPGV_WAIT_"):  # a hold vector inserted by stpg-view editing
            extra, wait_at = period_ticks, 0
        elif patched_vectors is not None:
            extra = patched_vectors.get(logical, 0) if period_ticks != nominal_period[rate_id] else 0
            wait_at = rate_wait_at[rate_id]
        elif is_wait_rate(rate_id):
            extra, wait_at = period_ticks - nominal_period[rate_id], rate_wait_at[rate_id]
            inferred += 1
        else:
            extra, wait_at = 0, 0
        trace.append_vector(period_ps=_ticks_to_ps(period_ticks, tick_hz), rate_id=rate_id,
                            chars=chars, line=line, start_value=to_signal_mask(value & enable),
                            start_enable=to_signal_mask(enable), extra_ps=_ticks_to_ps(extra, tick_hz),
                            wait_at_ps=_ticks_to_ps(wait_at, tick_hz) if extra else 0, label=label)
        occurrence_rate.append(rate_id)
        start_bits.append((value, enable))
        value, enable = end_value, end_enable
    if inferred:
        trace.wait_source = "inferred"
        trace.notes.append(f"{inferred} wait(s) inferred from the rate structure (no pattern.wait.patch.json "
                           "next to the binary)" + ("" if map_data else "; without a map a special rate with a "
                                                    "longer period looks the same"))
    elif trace.wait_source == "" and any(trace.extra_ps):
        trace.wait_source = "stil"

    def events_for(index: int) -> tuple[Event, ...]:
        record = occurrences[index][0]
        start_value, start_enable = start_bits[index]
        events, _chars, _v, _e = vector_events(record, occurrence_rate[index], start_value, start_enable)
        return events

    def detail_for(index: int) -> dict:
        record, repeat, top_index, iteration, body_index = occurrences[index]
        return {
            "source": "pattern.bin",
            "record_index": top_index,
            "repeat_iteration": repeat,
            "loop_iteration": iteration,
            "loop_body_index": body_index,
            "record": {
                "rate_id": int(record.rate_id), "repeat": int(record.repeat),
                "drive_value": f"0x{record.drive_value:06X}", "drive_enable": f"0x{record.drive_enable:06X}",
                "expect_value": f"0x{record.expect_value:06X}", "expect_mask": f"0x{record.expect_mask:06X}",
            },
        }

    trace.set_providers(events_for, detail_for)
    trace.notes.append(f"tick {tick_hz} Hz, {len(rates)} rates, {len(entries)} records, {len(loop_blocks)} loop blocks")
    return trace


def _load_events(path: Path, tick_hz: int, active_pins: int, exec_events, signals: list[Signal], *,
                 kind: str) -> Trace:
    """Raw/macro event streams: one row per output event."""

    if not signals:
        signals = _default_signals(int(active_pins))
    sig_of_bit = {s.bit: i for i, s in enumerate(signals) if s.bit is not None}
    trace = Trace(kind=kind, title=path.name, signals=signals)
    trace.rates.append(Rate(name="event", period_ps=0))
    value = enable = 0
    rows = []
    for exec_event in exec_events:
        duration_ps = _ticks_to_ps(int(exec_event.wait_ticks) + STPG_T_MIN_TICKS, tick_hz)
        events: list[Event] = []
        chars = []
        sampled = bool(exec_event.flags & STPG_EVF_SAMPLE_POINT)
        # firmware compare.c: with COMPARE_POINT every sample_mask bit is compared
        # against expect_value; without it the event is sampled only.
        compared = bool(exec_event.flags & STPG_EVF_COMPARE_POINT)
        for sig, signal in enumerate(signals):
            bit = signal.bit
            mask = 1 << bit
            on = bool(exec_event.out_enable & mask)
            level = (exec_event.out_value >> bit) & 1
            if sampled and exec_event.sample_mask & mask:
                if compared:
                    char = "H" if exec_event.expect_value & mask else "L"
                else:
                    char = "X"
                events.append(Event(sig, 0, KIND_COMPARE, char))
            elif on:
                char = str(level)
            else:
                char = "Z"
            chars.append(char)
            if on and (not (enable & mask) or ((value >> bit) & 1) != level):
                events.append(Event(sig, 0, KIND_DRIVE, str(level)))
            elif not on and enable & mask:
                events.append(Event(sig, 0, KIND_RELEASE))
        sv = sum(1 << sig_of_bit[b] for b in _bits(value & enable) if b in sig_of_bit)
        se = sum(1 << sig_of_bit[b] for b in _bits(enable) if b in sig_of_bit)
        trace.append_vector(period_ps=duration_ps, rate_id=0, chars="".join(chars), line=None,
                            start_value=sv, start_enable=se)
        rows.append(tuple(events))
        value, enable = int(exec_event.out_value), int(exec_event.out_enable)
    trace.set_providers(lambda index: rows[index], lambda index: {"source": "pattern.bin", "event_index": index})
    trace.notes.append(f"{kind}: one row per output event")
    return trace
