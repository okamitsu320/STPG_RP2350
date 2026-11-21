from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

from .ast import (
    ApproximationRecord,
    CompilerIR,
    Document,
    IRLoop,
    IRVector,
    LoopDecl,
    PinAssignment,
    PinMap,
    RateKey,
    ResolvedPattern,
    ResolvedItem,
    ResolvedLoop,
    ResolvedVector,
    TimedSignalEvent,
)
from .errors import StilError
from .control_flow import lower_pattern_control_flow
from .json_artifacts import read_json_artifact
from .waveform_actions import ADDED_WAVEFORM_CHARS


def load_pin_map(path: str | Path | None, *, document: Document) -> PinMap:
    if path is None:
        assignments = {
            signal.name: PinAssignment(bit=index, logical_pin=f"DUT{index}", gpio=None, direction=signal.direction.lower(), role="signal", allow_shared=False)
            for index, signal in enumerate(document.signals)
        }
        return PinMap(active_pins=_firmware_compatible_active_pins(len(assignments)), signals=assignments)
    data = read_json_artifact(Path(path))
    raw_signals = data.get("signals", {})
    assignments: dict[str, PinAssignment] = {}
    for name, payload in raw_signals.items():
        assignments[name] = PinAssignment(
            bit=int(payload["bit"]),
            logical_pin=payload.get("logical_pin", f"DUT{int(payload['bit'])}"),
            gpio=int(payload["gpio"]) if "gpio" in payload and payload["gpio"] is not None else None,
            direction=payload.get("direction", "inout"),
            role=payload.get("role", "signal"),
            allow_shared=bool(payload.get("allow_shared", False)),
        )
    pin_map = PinMap(active_pins=int(data["active_pins"]), signals=assignments)
    _validate_pin_map(pin_map)
    return pin_map


def _validate_pin_map(pin_map: PinMap) -> None:
    if pin_map.active_pins <= 0 or pin_map.active_pins > 24:
        raise StilError("ERR_BAD_PIN_MAP", f"active_pins out of supported range: {pin_map.active_pins}")

    valid_roles = {"signal", "marker", "fail"}
    valid_directions = {"input", "output", "inout"}
    bit_owners: dict[int, list[tuple[str, PinAssignment]]] = {}

    for name, assignment in pin_map.signals.items():
        if assignment.bit < 0 or assignment.bit >= pin_map.active_pins:
            raise StilError("ERR_BAD_PIN_MAP", f"signal {name} uses bit {assignment.bit} outside active_pins={pin_map.active_pins}")
        if assignment.role not in valid_roles:
            raise StilError("ERR_BAD_PIN_MAP", f"signal {name} uses unsupported role {assignment.role}")
        if assignment.direction.lower() not in valid_directions:
            raise StilError("ERR_BAD_PIN_MAP", f"signal {name} uses unsupported direction {assignment.direction}")
        bit_owners.setdefault(assignment.bit, []).append((name, assignment))

    for bit, owners in bit_owners.items():
        if len(owners) <= 1:
            continue
        if any(owner.role in {"marker", "fail"} and not owner.allow_shared for _, owner in owners):
            names = ", ".join(name for name, _ in owners)
            raise StilError("ERR_BAD_PIN_MAP", f"marker/fail bit sharing requires allow_shared=true on bit {bit}: {names}")


def _firmware_compatible_active_pins(signal_count: int) -> int:
    if signal_count <= 0 or signal_count > 24:
        raise StilError("ERR_BAD_PIN_MAP", f"active_pins out of supported range: {signal_count}")
    if signal_count <= 16:
        return 16
    return 24


def resolve_document(
    document: Document,
    *,
    pin_map: PinMap,
    pattern_burst_name: str | None,
    pattern_name: str | None,
    loop_policy: str = "expand",
    vector_range: tuple[int, int] | None = None,
    _source_count: list[int] | None = None,
) -> ResolvedPattern:
    waveform_tables = {table.name: table for table in document.waveform_tables}
    patterns = {pattern.name: pattern for pattern in document.patterns}
    groups = {group.name: group for group in document.signal_groups}
    signal_names = {signal.name for signal in document.signals}

    if pattern_burst_name is not None:
        try:
            burst = next(item for item in document.pattern_bursts if item.name == pattern_burst_name)
        except StopIteration as exc:
            raise StilError("ERR_PATTERNBURST_NOT_FOUND", f"PatternBurst not found: {pattern_burst_name}") from exc
        selected_name = _select_pattern_from_burst(burst.patterns, pattern_name)
    else:
        if pattern_name is None:
            if len(document.patterns) != 1:
                raise StilError("ERR_MULTIPLE_PATTERNS_UNSUPPORTED", "multiple Pattern declarations require --pattern or --pattern-burst")
            selected_name = document.patterns[0].name
        else:
            selected_name = pattern_name

    if selected_name not in patterns:
        raise StilError("ERR_PATTERN_NOT_FOUND", f"Pattern not found: {selected_name}")
    control_flow_burst_name = pattern_burst_name
    if control_flow_burst_name is None:
        containing_bursts = [item for item in document.pattern_bursts if selected_name in item.patterns]
        if len(containing_bursts) == 1:
            control_flow_burst_name = containing_bursts[0].name
        elif len(containing_bursts) > 1:
            raise StilError(
                "ERR_PATTERNBURST_SELECTION_REQUIRED",
                f"Pattern {selected_name!r} is listed in multiple PatternBursts; select one explicitly",
            )
        elif any(item.macro_domains or item.procedure_domains for item in document.pattern_bursts):
            raise StilError(
                "ERR_PATTERNBURST_SELECTION_REQUIRED",
                f"Pattern {selected_name!r} is not listed in a PatternBurst that binds definition domains; refusing unscoped definitions",
            )
    selected_pattern = lower_pattern_control_flow(
        document,
        patterns[selected_name],
        pattern_burst_name=control_flow_burst_name,
        loop_policy=loop_policy,
    )
    if not selected_pattern.vectors and not selected_pattern.items:
        raise StilError("ERR_EMPTY_PATTERN", f"Pattern {selected_name} has no vectors")
    first_vector = _first_pattern_vector(selected_pattern.items) if selected_pattern.items else selected_pattern.vectors[0]
    default_wft_name = first_vector.waveform_table_name
    if default_wft_name not in waveform_tables:
        raise StilError("ERR_WFT_NOT_FOUND", f"WaveformTable not found: {default_wft_name}")
    waveform_table = waveform_tables[default_wft_name]

    if selected_pattern.items:
        normalized_count = 0
        normalized_summary: list[str] = []
        resolved_vectors: list[ResolvedVector] = []
        source_index = 0

        def resolve_one(vector, *, expanded_index: int, source_path: tuple[int, ...]) -> ResolvedVector:  # type: ignore[no-untyped-def]
            nonlocal normalized_count
            nonlocal source_index
            if vector.target in groups:
                target_signals = groups[vector.target].signals
                if len(vector.chars) != len(target_signals):
                    raise StilError(
                        "ERR_VECTOR_LENGTH_MISMATCH",
                        f"vector chars length mismatch for group {vector.target} at line {vector.location.line}",
                    )
                signal_chars = dict(zip(target_signals, vector.chars))
            elif vector.target in signal_names:
                if len(vector.chars) != 1:
                    raise StilError("ERR_VECTOR_LENGTH_MISMATCH", f"single signal vector must have length 1 at line {vector.location.line}")
                signal_chars = {vector.target: vector.chars}
            else:
                raise StilError("ERR_SIGNAL_NOT_FOUND", f"unknown signal/group {vector.target} at line {vector.location.line}")

            bit_assignments: dict[str, int] = {}
            normalized_chars: dict[str, str] = {}
            for signal_name, char in signal_chars.items():
                if signal_name not in pin_map.signals:
                    if char in ("X", "T"):
                        continue
                    raise StilError("ERR_UNMAPPED_ACTIVE_SIGNAL", f"unmapped active signal {signal_name} with char {char} at line {vector.location.line}")
                if char == "P":
                    normalized_count += 1
                    normalized_summary.append(f"line {vector.location.line}: {signal_name} P->R (label only; Drive arguments retained)")
                    char = "R"
                elif char == "N":
                    normalized_count += 1
                    normalized_summary.append(f"line {vector.location.line}: {signal_name} N->F (label only; Drive arguments retained)")
                    char = "F"
                normalized_chars[signal_name] = char
                bit_assignments[signal_name] = pin_map.signals[signal_name].bit
            result = ResolvedVector(
                vector_index=source_index,
                top_level_index=expanded_index,
                location=vector.location,
                pattern_name=selected_pattern.name,
                waveform_table_name=vector.waveform_table_name,
                signal_chars=normalized_chars,
                signal_bits=bit_assignments,
                source_path=source_path,
            )
            source_index += 1
            resolved_vectors.append(result)
            return result

        def walk(items: tuple, *, expanded_index: int, source_path: tuple[int, ...]) -> tuple[tuple[ResolvedItem, ...], int]:  # type: ignore[no-untyped-def]
            start_expanded_index = expanded_index
            resolved_items: list[ResolvedItem] = []
            for item_index, item in enumerate(items):
                item_path = source_path + (item_index,)
                if isinstance(item, LoopDecl):
                    loop_logical_base = expanded_index
                    body, body_size = walk(item.items, expanded_index=expanded_index, source_path=item_path)
                    if not body:
                        raise StilError("ERR_EMPTY_PATTERN", f"Loop in Pattern {selected_pattern.name} has no vectors")
                    resolved_items.append(
                        ResolvedLoop(
                            loop_count=item.loop_count,
                            items=body,
                            location=item.location,
                            source_path=item_path,
                            logical_vector_base=loop_logical_base,
                            iteration_base=0,
                        )
                    )
                    expanded_index += body_size * item.loop_count
                else:
                    resolved_items.append(resolve_one(item, expanded_index=expanded_index, source_path=item_path))
                    expanded_index += 1
            return tuple(resolved_items), expanded_index - start_expanded_index

        structural_items, expanded_count = walk(tuple(selected_pattern.items), expanded_index=0, source_path=())
        result = ResolvedPattern(
            document=document,
            selected_pattern_burst=pattern_burst_name,
            selected_pattern_name=selected_name,
            waveform_table=waveform_table,
            vectors=tuple(resolved_vectors),
            pin_map=pin_map,
            normalized_transition_count=normalized_count,
            normalized_transition_summary=tuple(normalized_summary),
            items=structural_items,
            loop_preserved=True,
        )
        return (slice_resolved_pattern(result, start=vector_range[0], end=vector_range[1])
                if vector_range is not None else result)

    normalized_count = 0
    normalized_summary: list[str] = []
    resolved_vectors: list[ResolvedVector] = []
    for vector_index, vector in enumerate(selected_pattern.vectors):
        retain = vector_range is None or vector_range[0] <= vector_index <= vector_range[1]
        if vector.target in groups:
            target_signals = groups[vector.target].signals
            if len(vector.chars) != len(target_signals):
                raise StilError(
                    "ERR_VECTOR_LENGTH_MISMATCH",
                    f"vector chars length mismatch for group {vector.target} at line {vector.location.line}",
                )
            signal_chars = dict(zip(target_signals, vector.chars))
        elif vector.target in signal_names:
            if len(vector.chars) != 1:
                raise StilError("ERR_VECTOR_LENGTH_MISMATCH", f"single signal vector must have length 1 at line {vector.location.line}")
            signal_chars = {vector.target: vector.chars}
        else:
            raise StilError("ERR_SIGNAL_NOT_FOUND", f"unknown signal/group {vector.target} at line {vector.location.line}")

        bit_assignments: dict[str, int] = {}
        normalized_chars: dict[str, str] = {}
        for signal_name, char in signal_chars.items():
            if signal_name not in pin_map.signals:
                if char in ("X", "T"):
                    continue
                raise StilError("ERR_UNMAPPED_ACTIVE_SIGNAL", f"unmapped active signal {signal_name} with char {char} at line {vector.location.line}")
            if char == "P":
                normalized_count += 1
                normalized_summary.append(
                    f"line {vector.location.line}: {signal_name} P->R (label only; Drive arguments retained)"
                )
                char = "R"
            elif char == "N":
                normalized_count += 1
                normalized_summary.append(
                    f"line {vector.location.line}: {signal_name} N->F (label only; Drive arguments retained)"
                )
                char = "F"
            if retain:
                normalized_chars[signal_name] = char
                bit_assignments[signal_name] = pin_map.signals[signal_name].bit
        # Validate/normalize every source row, including rows outside a
        # segment, but retain only the selected resolution. This is exactly
        # resolve-then-slice semantics without materializing all per-row dicts.
        if not retain:
            continue
        resolved_vectors.append(
            ResolvedVector(
                vector_index=vector_index if vector_range is None else vector_index - vector_range[0],
                top_level_index=vector_index,
                location=vector.location,
                pattern_name=selected_pattern.name,
                waveform_table_name=vector.waveform_table_name,
                signal_chars=normalized_chars,
                signal_bits=bit_assignments,
            )
        )

    if vector_range is not None:
        start, end = vector_range
        if start < 0 or end < start:
            raise StilError("ERR_BAD_VECTOR_RANGE", f"invalid vector range {start}:{end}")
        if end >= len(selected_pattern.vectors):
            raise StilError("ERR_BAD_VECTOR_RANGE", f"vector range {start}:{end} exceeds pattern size {len(selected_pattern.vectors)}")
    if _source_count is not None:
        _source_count.append(len(selected_pattern.vectors))

    return ResolvedPattern(
        document=document,
        selected_pattern_burst=pattern_burst_name,
        selected_pattern_name=selected_name,
        waveform_table=waveform_table,
        vectors=tuple(resolved_vectors),
        pin_map=pin_map,
        normalized_transition_count=normalized_count,
        normalized_transition_summary=tuple(normalized_summary),
    )


def build_ir(resolved: ResolvedPattern, *, tick_hz: int) -> CompilerIR:
    return build_ir_with_scale(resolved, tick_hz=tick_hz, time_scale=Decimal("1.0"))


def build_ir_with_scale(resolved: ResolvedPattern, *, tick_hz: int, time_scale: Decimal) -> CompilerIR:
    waveform_tables = {table.name: table for table in resolved.document.waveform_tables}
    groups = {group.name: group.signals for group in resolved.document.signal_groups}
    # Resolve waveform definitions once per WaveformTable.  The previous
    # implementation scanned every waveform and character declaration for
    # every signal of every vector, which made large simulation-derived STIL
    # inputs unnecessarily quadratic in practice.
    waveform_topologies = {
        name: tuple(sorted(_waveform_rate_topology(table).items()))
        for name, table in waveform_tables.items()
    }
    waveform_char_caches = {
        name: _build_waveform_char_cache(table, groups)
        for name, table in waveform_tables.items()
    }
    waveform_event_caches: dict[str, dict[tuple[str, str], tuple[TimedSignalEvent, ...]]] = {}
    waveform_event_conversions: dict[str, dict[int, tuple[tuple[int, bool], ...]]] = {}
    for name, cache in waveform_char_caches.items():
        resolved_cache: dict[tuple[str, str], tuple[TimedSignalEvent, ...]] = {}
        conversion_cache: dict[int, tuple[tuple[int, bool], ...]] = {}
        for key, char_decl in cache.items():
            declaration_id = id(char_decl)
            converted = conversion_cache.get(declaration_id)
            if converted is None:
                converted = tuple(
                    _time_to_ticks(event.time_text, tick_hz, time_scale=time_scale)
                    for event in char_decl.events
                )
                conversion_cache[declaration_id] = converted
            resolved_cache[key] = tuple(
                TimedSignalEvent(
                    time_ticks=event_ticks,
                    action_name=event.action_name,
                    action_arg=event.action_arg,
                )
                for event, (event_ticks, _event_exact) in zip(char_decl.events, converted)
            )
        waveform_event_caches[name] = resolved_cache
        waveform_event_conversions[name] = conversion_cache
    used_chars_by_wft: dict[str, dict[str, set[str]]] = {}
    for item in resolved.vectors:
        table_map = used_chars_by_wft.setdefault(item.waveform_table_name, {})
        for signal_name, char in item.signal_chars.items():
            table_map.setdefault(signal_name, set()).add(char)
    vectors: list[IRVector] = []
    approximate_reasons: list[str] = []
    approximations: list[ApproximationRecord] = []
    warnings: list[str] = []
    period_conversions = {
        name: _time_to_ticks(table.period_text, tick_hz, time_scale=time_scale)
        for name, table in waveform_tables.items()
    }
    # All vectors using one WaveformTable have the same immutable key. In a
    # long source, rebuilding its topology strings per vector dominated IR
    # memory even after tokenization became bounded.
    rate_keys = {
        name: RateKey(
            waveform_table_name=name,
            period_ticks=period_conversions[name][0],
            normalized_edge_topology=tuple(f"{signal}:{value}" for signal, value in topology),
        )
        for name, topology in waveform_topologies.items()
    }
    for item in resolved.vectors:
        waveform_table = waveform_tables[item.waveform_table_name]
        char_cache = waveform_char_caches[item.waveform_table_name]
        event_cache = waveform_event_caches[item.waveform_table_name]
        event_conversions = waveform_event_conversions[item.waveform_table_name]
        period_ticks, period_exact = period_conversions[item.waveform_table_name]
        if not period_exact:
            approximate_reasons.append(
                f"WFT {waveform_table.name} period {waveform_table.period_text} quantized to {period_ticks} ticks"
            )
            approximations.append(
                _build_approximation_record(
                    kind="period_quantization",
                    original_time=waveform_table.period_text,
                    emitted_time_ticks=period_ticks,
                    tick_hz=tick_hz,
                    affected_signal=None,
                    affected_vector_index=item.vector_index,
                    stil_line=item.location.line,
                    waveform_table_name=waveform_table.name,
                )
            )
        rate_key = rate_keys[item.waveform_table_name]
        drive_chars: dict[str, str] = {}
        compare_chars: dict[str, str] = {}
        signal_events: dict[str, tuple[TimedSignalEvent, ...]] = {}
        for signal_name, char in item.signal_chars.items():
            if char == "T":
                continue
            source_decl = char_cache.get((signal_name, char))
            if (signal_name, char) in event_cache:
                signal_events[signal_name] = event_cache[(signal_name, char)]
            else:
                signal_events[signal_name] = _resolve_signal_events(
                    waveform_table,
                    groups,
                    signal_name,
                    char,
                    tick_hz=tick_hz,
                    time_scale=time_scale,
                    char_decl=source_decl,
                )
            if char in ADDED_WAVEFORM_CHARS:
                # Classify by the selected declaration, not by spelling:
                # a D selector may contain a D->U pulse, and h/l have their
                # own timing/expectation rather than borrowing H/L's table.
                events = signal_events[signal_name]
                if any(event.action_name == "Drive" or event.action_name.startswith("Dir") for event in events):
                    drive_chars[signal_name] = char
                if any(event.action_name == "Compare" for event in events):
                    compare_chars[signal_name] = char
            elif char in ("0", "1", "Z", "R", "F"):
                drive_chars[signal_name] = char
            else:
                compare_chars[signal_name] = char
            if source_decl is None:
                continue
            for event, (event_ticks, event_exact) in zip(
                source_decl.events,
                event_conversions[id(source_decl)],
            ):
                if not event_exact:
                    approximate_reasons.append(
                        f"WFT {waveform_table.name} {signal_name}.{char} "
                        f"time {event.time_text} quantized to {event_ticks} ticks"
                    )
                    approximations.append(
                        _build_approximation_record(
                            kind="event_quantization",
                            original_time=event.time_text,
                            emitted_time_ticks=event_ticks,
                            tick_hz=tick_hz,
                            affected_signal=signal_name,
                            affected_vector_index=item.vector_index,
                            stil_line=item.location.line,
                            waveform_table_name=waveform_table.name,
                        )
                    )
        vectors.append(
            IRVector(
                vector_index=item.vector_index,
                top_level_index=item.top_level_index,
                stil_line=item.location.line,
                label=item.location.label,
                pattern_name=item.pattern_name,
                waveform_table_name=item.waveform_table_name,
                rate_key=rate_key,
                signal_chars=item.signal_chars,
                drive_chars=drive_chars,
                compare_chars=compare_chars,
                signal_bit_assignments=item.signal_bits,
                signal_events=signal_events,
                source_path=item.source_path,
                loop_source_path=item.loop_source_path,
                loop_iteration_index=item.loop_iteration_index,
                loop_body_index=item.loop_body_index,
                expansion_trace=item.location.expansion_trace,
            )
        )
    if resolved.normalized_transition_count:
        warnings.extend(resolved.normalized_transition_summary)
    return CompilerIR(
        resolved_pattern=resolved,
        vectors=tuple(vectors),
        exact=not approximate_reasons,
        approximate_reasons=tuple(dict.fromkeys(approximate_reasons)),
        approximations=tuple(approximations),
        warnings=tuple(dict.fromkeys(warnings)),
        sequence_items=_build_ir_sequence_items(resolved, tuple(vectors)),
        logical_vector_count=_resolved_logical_vector_count(resolved),
        loop_preserved=resolved.loop_preserved,
        tick_hz=tick_hz,
    )


def slice_resolved_pattern(resolved: ResolvedPattern, *, start: int, end: int) -> ResolvedPattern:
    if start < 0 or end < start:
        raise StilError("ERR_BAD_VECTOR_RANGE", f"invalid vector range {start}:{end}")
    if resolved.loop_preserved:
        logical_count = _resolved_logical_vector_count(resolved)
        if end >= logical_count:
            raise StilError("ERR_BAD_VECTOR_RANGE", f"vector range {start}:{end} exceeds pattern size {logical_count}")
        return _slice_preserved_pattern(resolved, start=start, end=end)
    if end >= len(resolved.vectors):
        raise StilError("ERR_BAD_VECTOR_RANGE", f"vector range {start}:{end} exceeds pattern size {len(resolved.vectors)}")
    sliced_vectors = tuple(
        ResolvedVector(
            vector_index=local_index,
            top_level_index=vector.top_level_index,
            location=vector.location,
            pattern_name=vector.pattern_name,
            waveform_table_name=vector.waveform_table_name,
            signal_chars=vector.signal_chars,
            signal_bits=vector.signal_bits,
        )
        for local_index, vector in enumerate(resolved.vectors[start : end + 1])
    )
    return ResolvedPattern(
        document=resolved.document,
        selected_pattern_burst=resolved.selected_pattern_burst,
        selected_pattern_name=resolved.selected_pattern_name,
        waveform_table=resolved.waveform_table,
        vectors=sliced_vectors,
        pin_map=resolved.pin_map,
        normalized_transition_count=resolved.normalized_transition_count,
        normalized_transition_summary=resolved.normalized_transition_summary,
    )


def _clone_resolved_vector(vector: ResolvedVector, *, logical_index: int, source_path: tuple[int, ...]) -> ResolvedVector:
    return ResolvedVector(
        vector_index=logical_index,
        top_level_index=logical_index,
        location=vector.location,
        pattern_name=vector.pattern_name,
        waveform_table_name=vector.waveform_table_name,
        signal_chars=vector.signal_chars,
        signal_bits=vector.signal_bits,
        source_path=source_path,
        loop_source_path=vector.loop_source_path,
        loop_iteration_index=vector.loop_iteration_index,
        loop_body_index=vector.loop_body_index,
    )


def _slice_preserved_pattern(
    resolved: ResolvedPattern,
    *,
    start: int,
    end: int,
) -> ResolvedPattern:
    """Clip a preserved, non-nested loop sequence to an inclusive range.

    Full loop iterations stay compressed.  Only boundary iterations that are
    cut by the requested range are lowered to ordinary vectors; this keeps a
    segment proportional to its requested logical range while retaining the
    existing LoopBlock ABI where possible.
    """

    selected_items: list[ResolvedItem] = []
    selected_vectors: list[ResolvedVector] = []
    logical_base = 0

    def add_vector(
        vector: ResolvedVector,
        logical_index: int,
        *,
        occurrence_path: tuple[int, ...] | None = None,
        loop_source_path: tuple[int, ...] | None = None,
        loop_iteration_index: int | None = None,
        loop_body_index: int | None = None,
    ) -> None:
        cloned = _clone_resolved_vector(
            vector,
            logical_index=logical_index,
            source_path=occurrence_path if occurrence_path is not None else vector.source_path,
        )
        if loop_source_path is not None or loop_iteration_index is not None or loop_body_index is not None:
            cloned = replace(
                cloned,
                loop_source_path=loop_source_path,
                loop_iteration_index=loop_iteration_index,
                loop_body_index=loop_body_index,
            )
        selected_items.append(cloned)
        selected_vectors.append(cloned)

    for item_index, item in enumerate(resolved.items):
        if isinstance(item, ResolvedVector):
            if start <= logical_base <= end:
                add_vector(item, logical_base)
            logical_base += 1
            continue
        if any(isinstance(body_item, ResolvedLoop) for body_item in item.items):
            raise StilError(
                "ERR_NESTED_LOOP_UNSUPPORTED",
                "vector-range selection for preserved input does not support nested loops",
            )
        body = tuple(body_item for body_item in item.items if isinstance(body_item, ResolvedVector))
        if len(body) != len(item.items) or not body:
            raise StilError("ERR_LOOP_PROVENANCE", "preserved loop body is not a vector sequence")
        body_len = len(body)
        loop_count = int(item.loop_count)
        loop_start = logical_base
        loop_end = logical_base + loop_count * body_len - 1
        overlap_start = max(start, loop_start)
        overlap_end = min(end, loop_end)
        if overlap_start <= overlap_end:
            first_iteration = (overlap_start - loop_start) // body_len
            last_iteration = (overlap_end - loop_start) // body_len
            first_iteration_start = loop_start + first_iteration * body_len
            first_local_start = max(0, overlap_start - first_iteration_start)
            last_iteration_start = loop_start + last_iteration * body_len
            last_local_end = min(body_len - 1, overlap_end - last_iteration_start)
            first_is_full = first_local_start == 0
            last_is_full = last_local_end == body_len - 1

            # At most two boundary iterations are lowered.  The contiguous
            # interior stays one compressed LoopBlock, even for a million-
            # iteration source loop.
            if not first_is_full:
                for body_index in range(first_local_start, body_len if first_iteration != last_iteration else last_local_end + 1):
                    logical_index = first_iteration_start + body_index
                    add_vector(
                        body[body_index],
                        logical_index,
                        occurrence_path=item.source_path + (first_iteration, body_index),
                        loop_source_path=item.source_path,
                        loop_iteration_index=first_iteration,
                        loop_body_index=body_index,
                    )
            full_start = first_iteration if first_is_full else first_iteration + 1
            full_end = last_iteration if last_is_full else last_iteration - 1
            if full_start <= full_end:
                selected_items.append(
                    ResolvedLoop(
                        loop_count=full_end - full_start + 1,
                        items=tuple(body),
                        location=item.location,
                        source_path=item.source_path + (full_start,),
                        logical_vector_base=loop_start + full_start * body_len,
                        iteration_base=full_start,
                    )
                )
                selected_vectors.extend(body)
            if not last_is_full and (last_iteration != first_iteration or first_is_full):
                for body_index in range(0, last_local_end + 1):
                    logical_index = last_iteration_start + body_index
                    add_vector(
                        body[body_index],
                        logical_index,
                        occurrence_path=item.source_path + (last_iteration, body_index),
                        loop_source_path=item.source_path,
                        loop_iteration_index=last_iteration,
                        loop_body_index=body_index,
                    )
        logical_base = loop_end + 1

    return ResolvedPattern(
        document=resolved.document,
        selected_pattern_burst=resolved.selected_pattern_burst,
        selected_pattern_name=resolved.selected_pattern_name,
        waveform_table=resolved.waveform_table,
        vectors=tuple(selected_vectors),
        pin_map=resolved.pin_map,
        normalized_transition_count=resolved.normalized_transition_count,
        normalized_transition_summary=resolved.normalized_transition_summary,
        items=tuple(selected_items),
        loop_preserved=True,
    )
def _first_pattern_vector(items: tuple) -> object:  # type: ignore[no-untyped-def]
    for item in items:
        if isinstance(item, LoopDecl):
            return _first_pattern_vector(item.items)
        return item
    raise StilError("ERR_EMPTY_PATTERN", "pattern has no vectors")


def _resolved_logical_vector_count(resolved: ResolvedPattern) -> int:
    if not resolved.items:
        return len(resolved.vectors)

    def count(items: tuple[ResolvedItem, ...]) -> int:
        total = 0
        for item in items:
            if isinstance(item, ResolvedLoop):
                total += item.loop_count * count(item.items)
            else:
                total += 1
        return total

    return count(resolved.items)


def _build_ir_sequence_items(resolved: ResolvedPattern, vectors: tuple[IRVector, ...]) -> tuple:
    if not resolved.items:
        return ()
    by_path = {vector.source_path: vector for vector in vectors}
    next_loop_id = 0

    def build(items: tuple[ResolvedItem, ...]) -> tuple:
        nonlocal next_loop_id
        result = []
        for item in items:
            if isinstance(item, ResolvedLoop):
                nested = build(item.items)
                result.append(
                    IRLoop(
                        loop_id=next_loop_id,
                        loop_count=item.loop_count,
                        items=nested,
                        source_path=item.source_path,
                        stil_line=item.location.line,
                        logical_vector_base=item.logical_vector_base,
                        iteration_base=item.iteration_base,
                    )
                )
                next_loop_id += 1
            else:
                try:
                    result.append(by_path[item.source_path])
                except KeyError as exc:
                    raise StilError("ERR_LOOP_PROVENANCE", f"missing IR vector for source path {item.source_path}") from exc
        return tuple(result)

    return build(resolved.items)


def _select_pattern_from_burst(patterns: tuple[str, ...], pattern_name: str | None) -> str:
    if pattern_name is not None:
        if pattern_name not in patterns:
            raise StilError("ERR_PATTERN_NOT_FOUND", f"Pattern {pattern_name} is not in selected PatternBurst")
        return pattern_name
    if len(patterns) == 1:
        return patterns[0]
    if not patterns:
        raise StilError("ERR_PATTERN_NOT_FOUND", "selected PatternBurst has an empty PatList")
    raise StilError("ERR_MULTIPLE_PATTERNS_UNSUPPORTED", "multiple patterns in PatternBurst require --pattern")


def _waveform_rate_topology(waveform_table) -> dict[str, int]:  # type: ignore[no-untyped-def]
    topology: dict[str, int] = {}
    for waveform in waveform_table.waveforms:
        for char_decl in waveform.chars:
            topology[f"{waveform.signal_or_group}.{char_decl.char}"] = len(char_decl.events)
    return topology


def _resolve_signal_events(
    waveform_table,
    groups: dict[str, tuple[str, ...]],
    signal_name: str,
    char: str,
    *,
    tick_hz: int,
    time_scale: Decimal,
    char_decl=None,
) -> tuple[TimedSignalEvent, ...]:
    if char_decl is None:
        char_decl = _find_waveform_char_decl(waveform_table, groups, signal_name, char)
    if char_decl is None:
        raise StilError(
            "ERR_WAVEFORM_CHAR_NOT_FOUND",
            f"Waveform definition not found for {waveform_table.name} {signal_name}.{char}",
        )
    resolved_events: list[TimedSignalEvent] = []
    for event in char_decl.events:
        event_ticks, _event_exact = _time_to_ticks(event.time_text, tick_hz, time_scale=time_scale)
        resolved_events.append(
            TimedSignalEvent(
                time_ticks=event_ticks,
                action_name=event.action_name,
                action_arg=event.action_arg,
            )
        )
    return tuple(resolved_events)


def _find_waveform_char_decl(waveform_table, groups: dict[str, tuple[str, ...]], signal_name: str, char: str):
    candidate_chars = (char,)
    if char == "R":
        candidate_chars = ("R", "P")
    elif char == "F":
        candidate_chars = ("F", "N")
    for waveform in waveform_table.waveforms:
        if waveform.signal_or_group == signal_name:
            for char_decl in waveform.chars:
                if char_decl.char in candidate_chars:
                    return char_decl
    for waveform in waveform_table.waveforms:
        members = groups.get(waveform.signal_or_group)
        if not members or signal_name not in members:
            continue
        for char_decl in waveform.chars:
            if char_decl.char in candidate_chars:
                return char_decl
    return None


def _build_waveform_char_cache(
    waveform_table,
    groups: dict[str, tuple[str, ...]],
) -> dict[tuple[str, str], object]:
    """Index the declarations used by ``_find_waveform_char_decl``.

    Direct signal waveform declarations retain precedence over group
    declarations, and declaration order is retained within each category.
    The R/P and F/N aliases are resolved with the same first-match semantics
    as the original helper.
    """
    direct: dict[str, list[object]] = {}
    grouped: dict[str, list[object]] = {}
    for waveform in waveform_table.waveforms:
        members = groups.get(waveform.signal_or_group)
        if members is None:
            direct.setdefault(waveform.signal_or_group, []).extend(waveform.chars)
        else:
            for signal_name in members:
                grouped.setdefault(signal_name, []).extend(waveform.chars)

    cache: dict[tuple[str, str], object] = {}
    for signal_name in set(direct) | set(grouped):
        declarations = [*direct.get(signal_name, ()), *grouped.get(signal_name, ())]
        requested_chars = {str(declaration.char) for declaration in declarations}
        requested_chars.update({"R", "F"})
        for char in requested_chars:
            candidates = ("R", "P") if char == "R" else ("F", "N") if char == "F" else (char,)
            declaration = next(
                (item for item in declarations if str(item.char) in candidates),
                None,
            )
            if declaration is not None:
                cache[(signal_name, char)] = declaration
    return cache


def _time_to_ticks(time_text: str, tick_hz: int, *, time_scale: Decimal = Decimal("1.0")) -> tuple[int, bool]:
    units = [
        ("ms", 1e-3),
        ("us", 1e-6),
        ("ns", 1e-9),
        ("ps", 1e-12),
        ("s", 1.0),
    ]
    for suffix, scale in units:
        if time_text.endswith(suffix):
            value = Decimal(time_text[: -len(suffix)])
            ticks_decimal = value * time_scale * Decimal(str(scale)) * Decimal(tick_hz)
            ticks = int(ticks_decimal.to_integral_value(rounding=ROUND_HALF_UP))
            return ticks, ticks_decimal == ticks_decimal.to_integral_value()
    value = Decimal(time_text)
    ticks_decimal = value * time_scale * Decimal(tick_hz)
    ticks = int(ticks_decimal.to_integral_value(rounding=ROUND_HALF_UP))
    return ticks, ticks_decimal == ticks_decimal.to_integral_value()


def _time_text_to_seconds(time_text: str, *, time_scale: Decimal = Decimal("1.0")) -> Decimal:
    units = {
        "ms": Decimal("1e-3"),
        "us": Decimal("1e-6"),
        "ns": Decimal("1e-9"),
        "ps": Decimal("1e-12"),
        "s": Decimal("1"),
    }
    for suffix, scale in units.items():
        if time_text.endswith(suffix):
            return Decimal(time_text[: -len(suffix)]) * time_scale * scale
    return Decimal(time_text) * time_scale


def _format_ns_from_seconds(seconds: Decimal) -> str:
    return format(seconds * Decimal("1e9"), "f")


def _build_approximation_record(
    *,
    kind: str,
    original_time: str,
    emitted_time_ticks: int,
    tick_hz: int,
    affected_signal: str | None,
    affected_vector_index: int | None,
    stil_line: int | None,
    waveform_table_name: str,
) -> ApproximationRecord:
    original_seconds = _time_text_to_seconds(original_time)
    emitted_seconds = Decimal(emitted_time_ticks) / Decimal(tick_hz)
    delta_seconds = emitted_seconds - original_seconds
    return ApproximationRecord(
        kind=kind,
        original_time=original_time,
        emitted_time_ticks=emitted_time_ticks,
        emitted_time_ns=_format_ns_from_seconds(emitted_seconds),
        delta_ticks=emitted_time_ticks - int((original_seconds * Decimal(tick_hz)).to_integral_value(rounding=ROUND_HALF_UP)),
        delta_ns=_format_ns_from_seconds(delta_seconds),
        affected_signal=affected_signal,
        affected_vector_index=affected_vector_index,
        stil_line=stil_line,
        waveform_table_name=waveform_table_name,
    )
