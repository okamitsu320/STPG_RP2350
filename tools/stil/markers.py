from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Sequence

from .ast import CompilerIR, IRLoop, IRVector, LoopDecl, TimedSignalEvent
from .errors import StilError
from .waveform_actions import final_drive_enable, final_drive_level


MAX_MARKER_COUNT = 4


def parse_marker_specs(marker_args: list[str] | None, marker_clear_args: list[str] | None) -> tuple[dict, ...]:
    marker_defs: dict[str, dict] = {}
    for item in marker_args or []:
        spec = _parse_marker_arg(item)
        if spec["name"] in marker_defs:
            raise StilError("ERR_DUPLICATE_MARKER", f"duplicate marker definition: {spec['name']}")
        marker_defs[spec["name"]] = spec
        if len(marker_defs) > MAX_MARKER_COUNT:
            raise StilError("ERR_MARKER_COUNT_LIMIT", f"at most {MAX_MARKER_COUNT} markers are supported")
    clear_names: set[str] = set()
    for item in marker_clear_args or []:
        clear_spec = _parse_marker_clear_arg(item)
        if clear_spec["name"] in clear_names:
            raise StilError("ERR_DUPLICATE_MARKER_CLEAR", f"duplicate marker-clear: {clear_spec['name']}")
        clear_names.add(clear_spec["name"])
        try:
            marker = marker_defs[clear_spec["name"]]
            if "vector" in clear_spec:
                marker["clear_vector"] = clear_spec["vector"]
            elif "line" in clear_spec:
                marker["clear_line"] = clear_spec["line"]
            else:
                marker["clear_occurrence"] = clear_spec["occurrence"]
        except KeyError as exc:
            raise StilError("ERR_MARKER_CLEAR_WITHOUT_SET", f"marker-clear without marker: {clear_spec['name']}") from exc
        if marker_defs[clear_spec["name"]]["mode"] != "level:set":
            raise StilError("ERR_MARKER_CLEAR_UNSUPPORTED", f"marker-clear requires level:set marker: {clear_spec['name']}")
    return tuple(marker_defs[name] for name in sorted(marker_defs))


def summarize_marker_specs(marker_specs: tuple[dict, ...] | list[dict]) -> list[dict]:
    """Return stable, report-safe marker bindings without internal event indexes."""
    fields = (
        "name",
        "address_kind",
        "line",
        "vector",
        "occurrence",
        "mode",
        "width",
        "clear_line",
        "clear_vector",
        "clear_occurrence",
        "logical_bit",
        "logical_pin",
    )
    summaries: list[dict] = []
    for marker in marker_specs:
        summary = {key: marker[key] for key in fields if key in marker and marker[key] is not None}
        summary.setdefault("address_kind", "stil_vector")
        summaries.append(summary)
    return summaries


def resolve_marker_addresses(
    marker_specs: tuple[dict, ...] | list[dict],
    *,
    line_to_vectors: Mapping[int, Sequence[int]],
) -> tuple[dict, ...]:
    """Resolve source STIL line addresses to unambiguous global vector indexes.

    A source line is accepted only when it identifies exactly one expanded
    top-level vector.  In particular, a vector inside an expanded Loop is
    intentionally rejected when the same source line occurs more than once;
    silently choosing one expansion would make a physical trigger ambiguous.
    """

    resolved: list[dict] = []
    for original in marker_specs:
        spec = dict(original)
        if "line" in spec:
            spec["vector"] = _resolve_line_address(
                spec["name"], int(spec["line"]), line_to_vectors,
                occurrence=spec.get("occurrence"), purpose="marker"
            )
            spec["address_kind"] = "stil_line"
        else:
            spec.setdefault("address_kind", "stil_vector")
        if "clear_line" in spec:
            spec["clear_vector"] = _resolve_line_address(
                spec["name"], int(spec["clear_line"]), line_to_vectors,
                occurrence=spec.get("clear_occurrence"), purpose="marker-clear"
            )
        resolved.append(spec)
    return tuple(resolved)


def _resolve_line_address(
    name: str,
    line: int,
    line_to_vectors: Mapping[int, Sequence[int]],
    *,
    occurrence: int | None = None,
    purpose: str,
) -> int:
    candidates = tuple(line_to_vectors.get(line, ()))
    if not candidates:
        raise StilError(
            "ERR_MARKER_LINE_NOT_FOUND",
            f"{purpose} STIL line is not a vector line: {name}:line={line}",
        )
    if occurrence is not None:
        if occurrence < 0 or occurrence >= len(candidates):
            raise StilError(
                "ERR_MARKER_OCCURRENCE_RANGE",
                f"{purpose} occurrence is outside the source-line occurrences: "
                f"{name}:line={line}:occurrence={occurrence} count={len(candidates)}",
            )
        return int(candidates[occurrence])
    if len(candidates) != 1:
        raise StilError(
            "ERR_MARKER_LINE_AMBIGUOUS",
            f"{purpose} STIL line maps to multiple expanded vectors: "
            f"{name}:line={line} -> {list(candidates)}",
        )
    return int(candidates[0])


def _resolve_preserved_line_address(
    ir: CompilerIR,
    line: int,
    occurrence: int | None,
    *,
    purpose: str,
) -> int:
    """Resolve a source line against a non-expanded Loop sequence.

    ``occurrence`` is ordinal among occurrences of the same source line in
    the logical expanded sequence.  The implementation walks loop metadata
    and never expands the loop count, so a marker can address a very large
    loop without first materialising all of its vectors.
    """

    total = 0
    for item in ir.sequence_items:
        if isinstance(item, IRVector):
            total += int(item.stil_line == line)
            continue
        if not isinstance(item, IRLoop):
            raise StilError("ERR_LOOP_PROVENANCE", "unsupported preserved sequence item")
        if any(isinstance(body_item, IRLoop) for body_item in item.items):
            raise StilError(
                "ERR_NESTED_LOOP_UNSUPPORTED",
                "marker resolution for nested loops is unsupported by the current Loop ABI",
            )
        matches = sum(int(body_item.stil_line == line) for body_item in item.items)
        total += matches * int(item.loop_count)
    if total == 0:
        raise StilError(
            "ERR_MARKER_LINE_NOT_FOUND",
            f"{purpose} STIL line is not a vector line: line={line}",
        )
    if occurrence is None:
        if total != 1:
            raise StilError(
                "ERR_MARKER_LINE_AMBIGUOUS",
                f"{purpose} STIL line maps to {total} logical occurrences; "
                f"specify occurrence=0..{total - 1}: line={line}",
            )
        occurrence = 0
    if occurrence < 0 or occurrence >= total:
        raise StilError(
            "ERR_MARKER_OCCURRENCE_RANGE",
            f"{purpose} occurrence is outside the source-line occurrences: "
            f"line={line}:occurrence={occurrence} count={total}",
        )

    logical_base = 0
    remaining = occurrence
    for item in ir.sequence_items:
        if isinstance(item, IRVector):
            if item.stil_line == line:
                if remaining == 0:
                    return int(item.top_level_index)
                remaining -= 1
            logical_base = max(logical_base, int(item.top_level_index) + 1)
            continue
        body = tuple(item.items)
        if any(isinstance(body_item, IRLoop) for body_item in body):
            raise StilError(
                "ERR_NESTED_LOOP_UNSUPPORTED",
                "marker resolution for nested loops is unsupported by the current Loop ABI",
            )
        matching_body_indexes = [
            index for index, body_item in enumerate(body)
            if isinstance(body_item, IRVector) and body_item.stil_line == line
        ]
        if matching_body_indexes:
            per_iteration = len(matching_body_indexes)
            loop_occurrences = per_iteration * int(item.loop_count)
            if remaining < loop_occurrences:
                iteration, rank = divmod(remaining, per_iteration)
                return (
                    int(item.logical_vector_base)
                    + iteration * len(body)
                    + matching_body_indexes[rank]
                )
            remaining -= loop_occurrences
        logical_base = max(
            logical_base,
            int(item.logical_vector_base) + int(item.loop_count) * len(body),
        )
    raise StilError("ERR_MARKER_PROVENANCE", f"could not resolve {purpose} occurrence line={line}")


def _resolve_preserved_marker_specs(
    ir: CompilerIR,
    marker_specs: tuple[dict, ...],
) -> tuple[dict, ...]:
    resolved: list[dict] = []
    for original in marker_specs:
        spec = dict(original)
        if "line" in spec and "vector" not in spec:
            spec["vector"] = _resolve_preserved_line_address(
                ir,
                int(spec["line"]),
                int(spec["occurrence"]) if "occurrence" in spec else None,
                purpose="marker",
            )
            spec["address_kind"] = "stil_line"
        elif "occurrence" in spec and "line" not in spec:
            raise StilError("ERR_BAD_MARKER_SPEC", "occurrence requires line= for a preserved loop marker")
        else:
            spec.setdefault("address_kind", "stil_vector")
        if "clear_line" in spec and "clear_vector" not in spec:
            spec["clear_vector"] = _resolve_preserved_line_address(
                ir,
                int(spec["clear_line"]),
                int(spec["clear_occurrence"]) if "clear_occurrence" in spec else None,
                purpose="marker-clear",
            )
        elif "clear_occurrence" in spec and "clear_line" not in spec:
            raise StilError("ERR_BAD_MARKER_SPEC", "clear occurrence requires clear line= for a preserved loop marker")
        resolved.append(spec)
    return tuple(resolved)


def _marker_active_range(spec: dict) -> tuple[int, int]:
    start = int(spec["vector"])
    if spec["mode"] == "pulse":
        return start, start + int(spec.get("width", 1)) - 1
    return start, int(spec["clear_vector"]) - 1


def _source_vector_count(ir: CompilerIR, fallback: int) -> int:
    """Count the unsliced source without expanding preserved Loop iterations."""

    pattern = next(
        (item for item in ir.resolved_pattern.document.patterns
         if item.name == ir.resolved_pattern.selected_pattern_name),
        None,
    )
    if pattern is None:
        return fallback
    if not pattern.items:
        return len(pattern.vectors)

    def count(items):  # type: ignore[no-untyped-def]
        return sum(item.loop_count * count(item.items) if isinstance(item, LoopDecl) else 1
                   for item in items)

    return count(pattern.items)


def _markerize_vector(
    vector: IRVector,
    *,
    global_index: int,
    marker_specs: tuple[dict, ...],
    pin_map,
    vector_index: int,
    loop_iteration: int | None = None,
    body_record_index: int | None = None,
) -> tuple[IRVector, list[dict]]:
    signal_chars = dict(vector.signal_chars)
    drive_chars = dict(vector.drive_chars)
    signal_bits = dict(vector.signal_bit_assignments)
    signal_events = dict(vector.signal_events)
    marker_events: list[dict] = []
    for spec in marker_specs:
        name = spec["name"]
        assignment = pin_map.signals[name]
        if spec["mode"] == "pulse":
            width = int(spec.get("width", 1))
            active = int(spec["vector"]) <= global_index < int(spec["vector"]) + width
        else:
            active = int(spec["vector"]) <= global_index < int(spec["clear_vector"])
        marker_char = "1" if active else "0"
        signal_bits[name] = assignment.bit
        signal_chars[name] = marker_char
        drive_chars[name] = marker_char
        signal_events[name] = (TimedSignalEvent(time_ticks=0, action_name="Drive", action_arg=marker_char),)
        if spec["mode"] == "pulse" and active:
            event = {
                "name": name,
                "mode": "pulse",
                "width": int(spec.get("width", 1)),
                "pulse_start_vector": int(spec["vector"]),
                "global_vector_index": global_index,
                "local_vector_index": vector_index,
            }
            marker_events.append(event)
        else:
            if global_index == int(spec["vector"]):
                marker_events.append(
                    {
                        "name": name,
                        "mode": "level:set",
                        "global_vector_index": global_index,
                        "local_vector_index": vector_index,
                    }
                )
            if spec["mode"] == "level:set" and global_index == int(spec["clear_vector"]):
                marker_events.append(
                    {
                        "name": name,
                        "mode": "level:clear",
                        "global_vector_index": global_index,
                        "local_vector_index": vector_index,
                    }
                )
    for event in marker_events:
        if loop_iteration is not None:
            event["loop_iteration"] = loop_iteration
        if body_record_index is not None:
            event["body_record_index"] = body_record_index
    return (
        IRVector(
            vector_index=vector_index,
            top_level_index=vector.top_level_index,
            stil_line=vector.stil_line,
            label=vector.label,
            pattern_name=vector.pattern_name,
            waveform_table_name=vector.waveform_table_name,
            rate_key=vector.rate_key,
            signal_chars=signal_chars,
            drive_chars=drive_chars,
            compare_chars=vector.compare_chars,
            signal_bit_assignments=signal_bits,
            signal_events=signal_events,
            expansion_trace=vector.expansion_trace,
            source_path=vector.source_path,
            loop_source_path=vector.loop_source_path,
            loop_iteration_index=vector.loop_iteration_index,
            loop_body_index=vector.loop_body_index,
        ),
        marker_events,
    )


def _ceil_div(value: int, divisor: int) -> int:
    return -((-value) // divisor)


def _inject_preserved_markers(ir: CompilerIR, marker_specs: tuple[dict, ...]) -> CompilerIR:
    marker_specs = _resolve_preserved_marker_specs(ir, marker_specs)
    signal_names = {signal.name for signal in ir.resolved_pattern.document.signals}
    logical_count = int(ir.logical_vector_count or 0)
    sequence_min = None
    sequence_max = None
    for sequence_item in ir.sequence_items:
        if isinstance(sequence_item, IRVector):
            item_min = item_max = int(sequence_item.top_level_index)
        elif isinstance(sequence_item, IRLoop):
            body_count = sum(1 for body_item in sequence_item.items if isinstance(body_item, IRVector))
            if body_count <= 0:
                continue
            item_min = int(sequence_item.logical_vector_base)
            item_max = item_min + int(sequence_item.loop_count) * body_count - 1
        else:
            continue
        sequence_min = item_min if sequence_min is None else min(sequence_min, item_min)
        sequence_max = item_max if sequence_max is None else max(sequence_max, item_max)
    if sequence_min is None or sequence_max is None:
        sequence_min = 0
        sequence_max = logical_count - 1
    source_vector_count = _source_vector_count(ir, sequence_max + 1)
    pin_map = ir.resolved_pattern.pin_map
    for spec in marker_specs:
        name = spec["name"]
        if name in signal_names:
            raise StilError("ERR_MARKER_NAME_CONFLICT", f"marker name conflicts with document signal: {name}")
        if name not in pin_map.signals:
            raise StilError("ERR_MARKER_NOT_IN_PIN_MAP", f"marker not present in pin-map: {name}")
        assignment = pin_map.signals[name]
        if getattr(assignment, "allow_shared", False) is not True and getattr(assignment, "role", "signal") not in ("marker",):
            raise StilError("ERR_MARKER_ROLE_INVALID", f"marker {name} must be declared with role=marker or allow_shared=true")
        if assignment.bit == 20 and pin_map.active_pins == 24:
            raise StilError("ERR_MARKER_UNOBSERVABLE_BIT", f"marker {name} cannot use logical bit20 on current 24pin board")
        clear_vector = spec.get("clear_vector")
        if spec["mode"] == "level:set" and clear_vector is None:
            raise StilError("ERR_MARKER_CLEAR_REQUIRED", f"level marker requires --marker-clear: {name}")
        if spec["mode"] == "level:set" and int(clear_vector) <= int(spec["vector"]):
            raise StilError("ERR_MARKER_CLEAR_ORDER", f"marker-clear must follow marker set: {name}@{clear_vector}")
        start, end = _marker_active_range(spec)
        if start < 0 or end >= source_vector_count or end < start:
            raise StilError("ERR_MARKER_VECTOR_RANGE", f"marker range is outside logical pattern: {name}@{start}:{end}")
        # A clear/falling edge may be the only marker effect in this slice.
        # A pulse ending at the final source vector has no following edge.
        effect_end = min(end + 1, source_vector_count - 1)
        if spec["mode"] == "level:set" and int(clear_vector) >= source_vector_count:
            raise StilError("ERR_MARKER_VECTOR_RANGE", f"marker-clear is outside logical pattern: {name}@{clear_vector}")
        if start > sequence_max or effect_end < sequence_min:
            raise StilError("ERR_MARKER_VECTOR_RANGE", f"marker has no effect in selected source range: {name}")

    next_vector_index = max((vector.vector_index for vector in ir.vectors), default=-1) + 1
    marker_events_by_vector: dict[int, list[dict]] = {}
    transformed_items: list[IRVector | IRLoop] = []
    logical_base = 0
    for item in ir.sequence_items:
        if isinstance(item, IRVector):
            logical_base = int(item.top_level_index)
            transformed, events = _markerize_vector(
                item,
                global_index=logical_base,
                marker_specs=marker_specs,
                pin_map=pin_map,
                vector_index=next_vector_index,
            )
            next_vector_index += 1
            transformed_items.append(transformed)
            if events:
                marker_events_by_vector[transformed.vector_index] = events
            logical_base = int(item.top_level_index) + 1
            continue
        if not isinstance(item, IRLoop) or any(isinstance(body_item, IRLoop) for body_item in item.items):
            raise StilError(
                "ERR_NESTED_LOOP_UNSUPPORTED",
                "marker injection supports only preserved loops with VectorRecord bodies",
            )
        logical_base = int(item.logical_vector_base)
        body = tuple(item.items)
        body_len = len(body)
        if body_len == 0 or any(not isinstance(body_item, IRVector) for body_item in body):
            raise StilError("ERR_LOOP_PROVENANCE", "preserved loop body is not a vector sequence")
        cut_points = {0, int(item.loop_count)}
        for spec in marker_specs:
            start, end = _marker_active_range(spec)
            for body_offset in range(body_len):
                first = _ceil_div(start - logical_base - body_offset, body_len)
                last = (end - logical_base - body_offset) // body_len
                first = max(0, first)
                last = min(int(item.loop_count) - 1, last)
                if first <= last:
                    cut_points.add(first)
                    cut_points.add(last + 1)
        cuts = sorted(cut_points)
        for iteration_start, iteration_end in zip(cuts, cuts[1:]):
            representative_base = logical_base + iteration_start * body_len
            transformed_body: list[IRVector] = []
            for body_index, body_vector in enumerate(body):
                transformed, events = _markerize_vector(
                    body_vector,
                    global_index=representative_base + body_index,
                    marker_specs=marker_specs,
                    pin_map=pin_map,
                    vector_index=next_vector_index,
                    loop_iteration=iteration_start,
                    body_record_index=body_index,
                )
                next_vector_index += 1
                transformed_body.append(transformed)
                if events:
                    marker_events_by_vector[transformed.vector_index] = events
            transformed_items.append(
                IRLoop(
                    loop_id=item.loop_id,
                    loop_count=iteration_end - iteration_start,
                    items=tuple(transformed_body),
                    source_path=item.source_path,
                    stil_line=item.stil_line,
                    logical_vector_base=representative_base,
                    iteration_base=iteration_start,
                )
            )
        logical_base = int(item.logical_vector_base) + int(item.loop_count) * body_len

    enriched_specs = []
    for spec in marker_specs:
        assignment = pin_map.signals[spec["name"]]
        enriched_specs.append(
            {
                **spec,
                "logical_bit": assignment.bit,
                "logical_pin": assignment.logical_pin,
                "marker_events_by_vector": marker_events_by_vector,
            }
        )
    sequence_items = tuple(transformed_items)
    all_vectors = tuple(_sequence_vectors(sequence_items))
    terminal_value, terminal_enable = _compute_terminal_state_sequence(sequence_items)
    return replace(
        ir,
        vectors=all_vectors,
        sequence_items=sequence_items,
        markers=tuple(enriched_specs),
        terminal_drive_value=terminal_value,
        terminal_drive_enable=terminal_enable,
    )


def _sequence_vectors(items) -> list[IRVector]:  # type: ignore[no-untyped-def]
    result: list[IRVector] = []
    for item in items:
        if isinstance(item, IRVector):
            result.append(item)
        elif isinstance(item, IRLoop):
            result.extend(_sequence_vectors(item.items))
    return result


def inject_markers(ir: CompilerIR, marker_specs: tuple[dict, ...]) -> CompilerIR:
    if not marker_specs:
        if ir.loop_preserved or ir.sequence_items:
            terminal_value, terminal_enable = _compute_terminal_state_sequence(ir.sequence_items)
        else:
            terminal_value, terminal_enable = _compute_terminal_state(ir.vectors)
        return replace(ir, terminal_drive_value=terminal_value, terminal_drive_enable=terminal_enable)

    if ir.loop_preserved or ir.sequence_items:
        return _inject_preserved_markers(ir, marker_specs)

    pin_map = ir.resolved_pattern.pin_map
    signal_names = {signal.name for signal in ir.resolved_pattern.document.signals}
    available_global_indices = {vector.top_level_index for vector in ir.vectors}
    max_global_index = max(available_global_indices, default=-1)
    selected_pattern = next(
        (pattern for pattern in ir.resolved_pattern.document.patterns
         if pattern.name == ir.resolved_pattern.selected_pattern_name),
        None,
    )
    source_vector_count = _source_vector_count(ir, max_global_index + 1)
    line_to_vectors: dict[int, list[int]] = {}
    if selected_pattern is not None:
        for global_index, vector in enumerate(selected_pattern.vectors):
            line_to_vectors.setdefault(vector.location.line, []).append(global_index)
    else:
        for vector in ir.vectors:
            line_to_vectors.setdefault(vector.stil_line, []).append(vector.top_level_index)
    marker_specs = resolve_marker_addresses(marker_specs, line_to_vectors=line_to_vectors)
    for spec in marker_specs:
        name = spec["name"]
        if name in signal_names:
            raise StilError("ERR_MARKER_NAME_CONFLICT", f"marker name conflicts with document signal: {name}")
        if name not in pin_map.signals:
            raise StilError("ERR_MARKER_NOT_IN_PIN_MAP", f"marker not present in pin-map: {name}")
        assignment = pin_map.signals[name]
        if getattr(assignment, "allow_shared", False) is not True and getattr(assignment, "role", "signal") not in ("marker",):
            raise StilError("ERR_MARKER_ROLE_INVALID", f"marker {name} must be declared with role=marker or allow_shared=true")
        if assignment.bit == 20 and pin_map.active_pins == 24:
            raise StilError("ERR_MARKER_UNOBSERVABLE_BIT", f"marker {name} cannot use logical bit20 on current 24pin board")
        if not 0 <= spec["vector"] < source_vector_count:
            raise StilError(
                "ERR_MARKER_VECTOR_RANGE",
                f"marker STIL vector address is outside the source pattern: {name}@{spec['vector']}",
            )
        clear_vector = spec.get("clear_vector")
        if clear_vector is not None and not 0 <= clear_vector < source_vector_count:
            raise StilError(
                "ERR_MARKER_VECTOR_RANGE",
                f"marker-clear STIL vector address is outside the source pattern: {name}@{clear_vector}",
            )
        if spec["mode"] == "level:set" and clear_vector is None:
            raise StilError("ERR_MARKER_CLEAR_REQUIRED", f"level marker requires --marker-clear: {name}")
        if spec["mode"] == "level:set" and clear_vector is not None and clear_vector <= spec["vector"]:
            raise StilError("ERR_MARKER_CLEAR_ORDER", f"marker-clear must follow marker set: {name}@{clear_vector}")
        if spec["mode"] == "pulse":
            width = spec.get("width", 1)
            if width <= 0:
                raise StilError("ERR_BAD_MARKER_SPEC", f"pulse marker width must be >= 1: {name}")
            pulse_end = spec["vector"] + width - 1
            if pulse_end >= source_vector_count:
                raise StilError("ERR_MARKER_VECTOR_RANGE", f"marker pulse exceeds source pattern: {name}@{pulse_end}")

        if spec["mode"] == "pulse":
            width = spec.get("width", 1)
            has_selected_effect = any(
                spec["vector"] <= global_index <= spec["vector"] + width
                for global_index in available_global_indices
            )
        else:
            has_selected_effect = any(
                spec["vector"] <= global_index <= spec["clear_vector"]
                for global_index in available_global_indices
            )
        if not has_selected_effect:
            raise StilError("ERR_MARKER_VECTOR_RANGE", f"marker has no effect in selected source range: {name}")

    marker_events_by_vector: dict[int, list[dict]] = {}
    injected_vectors: list[IRVector] = []
    for vector in ir.vectors:
        global_index = vector.top_level_index
        vector_marker_events: list[dict] = []
        for spec in marker_specs:
            if spec["mode"] == "pulse":
                width = spec.get("width", 1)
                within_pulse = spec["vector"] <= global_index < (spec["vector"] + width)
                if within_pulse:
                    vector_marker_events.append(
                        {
                            "name": spec["name"],
                            "mode": "pulse",
                            "width": width,
                            "pulse_start_vector": spec["vector"],
                            "global_vector_index": global_index,
                            "local_vector_index": vector.vector_index,
                        }
                    )
            else:
                clear_vector = spec["clear_vector"]
                if global_index == spec["vector"]:
                    vector_marker_events.append(
                        {
                            "name": spec["name"],
                            "mode": "level:set",
                            "global_vector_index": global_index,
                            "local_vector_index": vector.vector_index,
                        }
                    )
                if clear_vector == global_index:
                    vector_marker_events.append(
                        {
                            "name": spec["name"],
                            "mode": "level:clear",
                            "global_vector_index": global_index,
                            "local_vector_index": vector.vector_index,
                        }
                    )
        signal_chars = dict(vector.signal_chars)
        drive_chars = dict(vector.drive_chars)
        signal_bits = dict(vector.signal_bit_assignments)
        signal_events = dict(vector.signal_events)
        for spec in marker_specs:
            assignment = pin_map.signals[spec["name"]]
            signal_bits[spec["name"]] = assignment.bit
            if spec["mode"] == "pulse":
                width = spec.get("width", 1)
                marker_char = "1" if spec["vector"] <= global_index < (spec["vector"] + width) else "0"
            else:
                marker_char = "1" if spec["vector"] <= global_index < spec["clear_vector"] else "0"
            signal_chars[spec["name"]] = marker_char
            drive_chars[spec["name"]] = marker_char
            signal_events[spec["name"]] = (
                TimedSignalEvent(
                    time_ticks=0,
                    action_name="Drive",
                    action_arg=marker_char,
                ),
            )
        injected_vectors.append(
            IRVector(
                vector_index=vector.vector_index,
                top_level_index=vector.top_level_index,
                stil_line=vector.stil_line,
                label=vector.label,
                pattern_name=vector.pattern_name,
                waveform_table_name=vector.waveform_table_name,
                rate_key=vector.rate_key,
                signal_chars=signal_chars,
                drive_chars=drive_chars,
                compare_chars=vector.compare_chars,
                signal_bit_assignments=signal_bits,
                signal_events=signal_events,
                expansion_trace=vector.expansion_trace,
                source_path=vector.source_path,
                loop_source_path=vector.loop_source_path,
                loop_iteration_index=vector.loop_iteration_index,
                loop_body_index=vector.loop_body_index,
            )
        )
        if vector_marker_events:
            marker_events_by_vector[vector.vector_index] = vector_marker_events

    enriched_specs = []
    for spec in marker_specs:
        assignment = pin_map.signals[spec["name"]]
        enriched_specs.append(
            {
                **spec,
                "address_kind": spec.get("address_kind", "stil_vector"),
                "logical_bit": assignment.bit,
                "logical_pin": assignment.logical_pin,
                "marker_events_by_vector": marker_events_by_vector,
            }
        )
    terminal_value, terminal_enable = _compute_terminal_state(tuple(injected_vectors))
    return replace(
        ir,
        vectors=tuple(injected_vectors),
        markers=tuple(enriched_specs),
        terminal_drive_value=terminal_value,
        terminal_drive_enable=terminal_enable,
    )


def _compute_terminal_state(vectors: tuple[IRVector, ...]) -> tuple[int, int]:
    value = 0
    enable = 0
    for vector in vectors:
        for signal_name, char in vector.drive_chars.items():
            bit = vector.signal_bit_assignments[signal_name]
            mask = 1 << bit
            level = final_drive_level(char, vector.signal_events.get(signal_name, ()))
            enabled = final_drive_enable(char, vector.signal_events.get(signal_name, ()))
            if level == 1:
                value |= mask
            elif level == 0:
                value &= ~mask
            if enabled is True:
                enable |= mask
            elif enabled is False:
                enable &= ~mask
    return value, enable


def _compute_terminal_state_sequence(items) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """Compute terminal drive state without expanding loop iterations."""

    vectors: list[IRVector] = []
    for item in items:
        if isinstance(item, IRVector):
            vectors.append(item)
        elif isinstance(item, IRLoop):
            if any(isinstance(body_item, IRLoop) for body_item in item.items):
                raise StilError("ERR_NESTED_LOOP_UNSUPPORTED", "terminal-state calculation cannot represent nested loops")
            # A loop body is deterministic at the STPG level; applying it
            # once gives the same final drive state as applying it count times.
            vectors.extend(body_item for body_item in item.items if isinstance(body_item, IRVector))
    return _compute_terminal_state(tuple(vectors))


def _parse_marker_arg(text: str) -> dict:
    name, sep, payload_text = text.partition(":")
    if not sep or not payload_text or not name or name.strip() != name or "=" in name:
        raise StilError("ERR_BAD_MARKER_SPEC", f"invalid marker spec: {text}")
    payload: dict[str, str] = {"name": name}
    allowed = {"vector", "line", "occurrence", "mode", "width"}
    current_key: str | None = None
    for item in payload_text.split(":"):
        if "=" not in item:
            if current_key == "mode" and item == "set" and payload.get("mode") == "level":
                payload["mode"] = "level:set"
                current_key = None
                continue
            raise StilError("ERR_BAD_MARKER_SPEC", f"invalid marker field: {text}")
        key, value = item.split("=", 1)
        if key not in allowed or key in payload or not value:
            raise StilError("ERR_BAD_MARKER_SPEC", f"invalid or duplicate marker field: {text}")
        payload[key] = value
        current_key = key
    address_fields = [key for key in ("vector", "line") if key in payload]
    if len(address_fields) != 1 or "mode" not in payload:
        raise StilError(
            "ERR_BAD_MARKER_SPEC",
            f"marker requires exactly one of vector or line, and mode: {text}",
        )
    if payload["mode"] not in ("pulse", "level:set"):
        raise StilError("ERR_BAD_MARKER_SPEC", f"unsupported marker mode: {payload['mode']}")
    address_key = address_fields[0]
    if "occurrence" in payload and address_key != "line":
        raise StilError("ERR_BAD_MARKER_SPEC", f"occurrence is only valid with line= for loop markers: {text}")
    try:
        address = int(payload[address_key], 10)
    except ValueError as exc:
        raise StilError(
            "ERR_BAD_MARKER_SPEC",
            f"marker {address_key} must be an integer: {text}",
        ) from exc
    if address < 0 or (address_key == "line" and address == 0):
        raise StilError("ERR_BAD_MARKER_SPEC", f"marker {address_key} is out of range: {text}")
    occurrence = None
    if "occurrence" in payload:
        try:
            occurrence = int(payload["occurrence"], 10)
        except ValueError as exc:
            raise StilError("ERR_BAD_MARKER_SPEC", f"marker occurrence must be an integer: {text}") from exc
        if occurrence < 0:
            raise StilError("ERR_BAD_MARKER_SPEC", f"marker occurrence is out of range: {text}")
    result = {"name": name, address_key: address, "mode": payload["mode"]}
    if occurrence is not None:
        result["occurrence"] = occurrence
    if "width" in payload:
        if payload["mode"] != "pulse":
            raise StilError("ERR_BAD_MARKER_SPEC", f"width is only valid for pulse markers: {text}")
        try:
            result["width"] = int(payload["width"], 10)
        except ValueError as exc:
            raise StilError("ERR_BAD_MARKER_SPEC", f"marker width must be an integer: {text}") from exc
    return result


def _parse_marker_clear_arg(text: str) -> dict:
    name, sep, payload_text = text.partition(":")
    if not sep or not payload_text or not name or name.strip() != name or "=" in name:
        raise StilError("ERR_BAD_MARKER_SPEC", f"invalid marker-clear spec: {text}")
    payload: dict[str, str] = {"name": name}
    for item in payload_text.split(":"):
        if "=" not in item:
            raise StilError("ERR_BAD_MARKER_SPEC", f"invalid marker-clear field: {text}")
        key, value = item.split("=", 1)
        if key not in {"vector", "line", "occurrence"} or key in payload or not value:
            raise StilError("ERR_BAD_MARKER_SPEC", f"invalid or duplicate marker-clear field: {text}")
        payload[key] = value
    address_fields = [key for key in ("vector", "line") if key in payload]
    if len(address_fields) != 1:
        raise StilError(
            "ERR_BAD_MARKER_SPEC",
            f"marker-clear requires exactly one of vector or line: {text}",
        )
    address_key = address_fields[0]
    if "occurrence" in payload and address_key != "line":
        raise StilError("ERR_BAD_MARKER_SPEC", f"marker-clear occurrence is only valid with line=: {text}")
    try:
        address = int(payload[address_key], 10)
    except ValueError as exc:
        raise StilError(
            "ERR_BAD_MARKER_SPEC",
            f"marker-clear {address_key} must be an integer: {text}",
        ) from exc
    if address < 0 or (address_key == "line" and address == 0):
        raise StilError("ERR_BAD_MARKER_SPEC", f"marker-clear {address_key} is out of range: {text}")
    result = {"name": name, address_key: address}
    if "occurrence" in payload:
        try:
            occurrence = int(payload["occurrence"], 10)
        except ValueError as exc:
            raise StilError("ERR_BAD_MARKER_SPEC", f"marker-clear occurrence must be an integer: {text}") from exc
        if occurrence < 0:
            raise StilError("ERR_BAD_MARKER_SPEC", f"marker-clear occurrence is out of range: {text}")
        result["occurrence"] = occurrence
    return result
