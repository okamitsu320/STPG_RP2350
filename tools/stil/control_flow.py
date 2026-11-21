from __future__ import annotations

from dataclasses import dataclass, replace

from .ast import (
    Document,
    GotoStmt,
    LoopDecl,
    LoopStmt,
    MacroCallStmt,
    MacroDecl,
    PatternDecl,
    PatternItem,
    PatternStatement,
    ProcedureCallStmt,
    ProcedureDecl,
    StopStmt,
    VectorDecl,
    VectorStmt,
    WaveformSelectStmt,
)
from .errors import StilError
from .static_limits import MAX_STATIC_STATEMENT_VISITS, MAX_STATIC_VECTOR_OCCURRENCES


@dataclass(frozen=True)
class _SequenceResult:
    items: tuple[PatternItem, ...]
    vectors: tuple[VectorDecl, ...]
    waveform_table: str | None
    stopped: bool = False


def lower_pattern_control_flow(
    document: Document,
    pattern: PatternDecl,
    *,
    pattern_burst_name: str | None,
    loop_policy: str,
) -> PatternDecl:
    """Inline the supported static STIL control flow into legacy IR nodes."""

    if loop_policy not in {"expand", "preserve"}:
        raise ValueError(f"unsupported loop_policy: {loop_policy}")
    if not pattern.statements:
        return pattern

    selected_burst = None
    if pattern_burst_name is not None:
        selected_burst = next((item for item in document.pattern_bursts if item.name == pattern_burst_name), None)
        if selected_burst is None:
            raise StilError("ERR_PATTERNBURST_NOT_FOUND", f"PatternBurst not found: {pattern_burst_name}")

    macros = _select_definitions(
        document.macros,
        allowed_domains=selected_burst.macro_domains if selected_burst else (),
        all_domains_when_unbound=selected_burst is None,
        kind="Macro",
    )
    procedures = _select_definitions(
        document.procedures,
        allowed_domains=selected_burst.procedure_domains if selected_burst else (),
        all_domains_when_unbound=selected_burst is None,
        kind="Procedure",
    )
    _validate_domain_references(document, selected_burst)

    class _Lowerer:
        def __init__(self) -> None:
            self.active_calls: list[tuple[str, str]] = []
            self.statement_visits = 0
            self.emitted_vectors = 0

        def _visit(self, statement: PatternStatement) -> None:
            self.statement_visits += 1
            if self.statement_visits > MAX_STATIC_STATEMENT_VISITS:
                raise StilError(
                    "ERR_STATIC_EXPANSION_LIMIT",
                    f"static control-flow execution exceeds {MAX_STATIC_STATEMENT_VISITS} statement visits",
                )

        def _vector(self, statement: VectorStmt, wft: str | None, trace: tuple[str, ...]) -> VectorDecl:
            if wft is None:
                raise StilError(
                    "ERR_BAD_PATTERN",
                    f"{statement.location.source_file}:{statement.location.line}: Vector before W statement",
                )
            self.emitted_vectors += 1
            if self.emitted_vectors > MAX_STATIC_VECTOR_OCCURRENCES:
                raise StilError(
                    "ERR_STATIC_EXPANSION_LIMIT",
                    f"static Pattern expansion exceeds {MAX_STATIC_VECTOR_OCCURRENCES} vector occurrences",
                )
            location = replace(
                statement.location,
                expansion_trace=statement.location.expansion_trace + trace,
            )
            return VectorDecl(statement.target, statement.chars, wft, location)

        def _labels(self, statements: tuple[PatternStatement, ...], *, owner: str) -> dict[str, int]:
            labels: dict[str, int] = {}

            def visit(sequence: tuple[PatternStatement, ...]) -> None:
                for index, statement in enumerate(sequence):
                    label = statement.location.label
                    if label is not None:
                        if label in labels:
                            raise StilError("ERR_DUPLICATE_LABEL", f"duplicate label {label!r} in {owner}")
                        labels[label] = id(statement)
                    if isinstance(statement, LoopStmt):
                        visit(statement.statements)

            visit(statements)
            return labels

        def _run_root(
            self,
            statements: tuple[PatternStatement, ...],
            *,
            owner: str,
            waveform_table: str | None,
            trace: tuple[str, ...],
            call_depth: int = 0,
        ) -> _SequenceResult:
            label_ids = self._labels(statements, owner=owner)
            return self._run_sequence(
                statements,
                owner=owner,
                waveform_table=waveform_table,
                trace=trace,
                label_ids=label_ids,
                call_depth=call_depth,
            )

        def _run_sequence(
            self,
            statements: tuple[PatternStatement, ...],
            *,
            owner: str,
            waveform_table: str | None,
            trace: tuple[str, ...],
            label_ids: dict[str, int],
            call_depth: int,
        ) -> _SequenceResult:
            local_labels = {
                statement.location.label: index
                for index, statement in enumerate(statements)
                if statement.location.label is not None
            }
            output_items: list[PatternItem] = []
            output_vectors: list[VectorDecl] = []
            cursor = 0
            current_wft = waveform_table
            while cursor < len(statements):
                statement = statements[cursor]
                self._visit(statement)
                if isinstance(statement, WaveformSelectStmt):
                    current_wft = statement.name
                elif isinstance(statement, VectorStmt):
                    vector = self._vector(statement, current_wft, trace)
                    output_items.append(vector)
                    output_vectors.append(vector)
                elif isinstance(statement, GotoStmt):
                    target_index = local_labels.get(statement.target)
                    if target_index is None:
                        if statement.target in label_ids:
                            raise StilError(
                                "ERR_GOTO_CROSS_SCOPE",
                                f"Goto {statement.target!r} crosses a Pattern/Procedure/Loop scope at line {statement.location.line}",
                            )
                        raise StilError("ERR_GOTO_NOT_FOUND", f"Goto target {statement.target!r} not found at line {statement.location.line}")
                    if target_index <= cursor:
                        raise StilError(
                            "ERR_GOTO_NOT_FORWARD",
                            f"Goto target {statement.target!r} is not forward at line {statement.location.line}",
                        )
                    cursor = target_index
                    continue
                elif isinstance(statement, StopStmt):
                    return _SequenceResult(tuple(output_items), tuple(output_vectors), current_wft, stopped=True)
                elif isinstance(statement, MacroCallStmt):
                    definitions_for_name = macros.get(statement.name, ())
                    if not definitions_for_name:
                        raise StilError("ERR_MACRO_NOT_FOUND", f"Macro not found in selected domain(s): {statement.name}")
                    if len(definitions_for_name) > 1:
                        domains = ", ".join(item.domain for item in definitions_for_name)
                        raise StilError("ERR_AMBIGUOUS_DEFINITION", f"Macro {statement.name!r} is visible in multiple domains: {domains}")
                    definition = definitions_for_name[0]
                    call_key = ("Macro", definition.name)
                    self._check_call_cycle(call_key, statement.location.line, call_depth)
                    call_trace = trace + (f"Macro {definition.name} called at line {statement.location.line}",)
                    self.active_calls.append(call_key)
                    try:
                        called = self._run_root(
                            definition.statements,
                            owner=f"Macro {definition.name}",
                            waveform_table=current_wft,
                            trace=call_trace,
                            call_depth=call_depth + 1,
                        )
                    finally:
                        self.active_calls.pop()
                    output_items.extend(called.items)
                    output_vectors.extend(called.vectors)
                    current_wft = called.waveform_table
                    if called.stopped:
                        return _SequenceResult(tuple(output_items), tuple(output_vectors), current_wft, stopped=True)
                elif isinstance(statement, ProcedureCallStmt):
                    definitions_for_name = procedures.get(statement.name, ())
                    if not definitions_for_name:
                        raise StilError("ERR_PROCEDURE_NOT_FOUND", f"Procedure not found in selected domain(s): {statement.name}")
                    if len(definitions_for_name) > 1:
                        domains = ", ".join(item.domain for item in definitions_for_name)
                        raise StilError("ERR_AMBIGUOUS_DEFINITION", f"Procedure {statement.name!r} is visible in multiple domains: {domains}")
                    definition = definitions_for_name[0]
                    call_key = ("Procedure", definition.name)
                    self._check_call_cycle(call_key, statement.location.line, call_depth)
                    call_trace = trace + (f"Procedure {definition.name} called at line {statement.location.line}",)
                    self.active_calls.append(call_key)
                    saved_wft = current_wft
                    try:
                        called = self._run_root(
                            definition.statements,
                            owner=f"Procedure {definition.name}",
                            waveform_table=current_wft,
                            trace=call_trace,
                            call_depth=call_depth + 1,
                        )
                    finally:
                        self.active_calls.pop()
                    output_items.extend(called.items)
                    output_vectors.extend(called.vectors)
                    current_wft = saved_wft
                    if called.stopped:
                        return _SequenceResult(tuple(output_items), tuple(output_vectors), current_wft, stopped=True)
                elif isinstance(statement, LoopStmt):
                    if loop_policy == "preserve":
                        loop_result = self._preserve_loop(
                            statement,
                            owner=owner,
                            waveform_table=current_wft,
                            trace=trace,
                            call_depth=call_depth,
                        )
                        output_items.extend(loop_result.items)
                        output_vectors.extend(loop_result.vectors)
                        current_wft = loop_result.waveform_table
                        if loop_result.stopped:
                            return _SequenceResult(tuple(output_items), tuple(output_vectors), current_wft, stopped=True)
                    else:
                        if statement.loop_count > MAX_STATIC_STATEMENT_VISITS - self.statement_visits:
                            raise StilError(
                                "ERR_STATIC_EXPANSION_LIMIT",
                                f"Loop at line {statement.location.line} exceeds the static execution budget",
                            )
                        for _iteration in range(statement.loop_count):
                            loop_result = self._run_root(
                                statement.statements,
                                owner=owner,
                                waveform_table=current_wft,
                                trace=trace + (f"Loop at line {statement.location.line}",),
                                call_depth=call_depth,
                            )
                            output_items.extend(loop_result.items)
                            output_vectors.extend(loop_result.vectors)
                            current_wft = loop_result.waveform_table
                            if loop_result.stopped:
                                return _SequenceResult(tuple(output_items), tuple(output_vectors), current_wft, stopped=True)
                cursor += 1
            return _SequenceResult(tuple(output_items), tuple(output_vectors), current_wft)

        def _preserve_loop(
            self,
            statement: LoopStmt,
            *,
            owner: str,
            waveform_table: str | None,
            trace: tuple[str, ...],
            call_depth: int,
        ) -> _SequenceResult:
            start_emitted = self.emitted_vectors
            first = self._run_root(
                statement.statements,
                owner=owner,
                waveform_table=waveform_table,
                trace=trace + (f"Loop at line {statement.location.line}",),
                call_depth=call_depth,
            )
            if not first.items:
                raise StilError("ERR_EMPTY_LOOP", f"Loop at line {statement.location.line} has no vectors")
            if first.stopped:
                return first
            self.emitted_vectors = start_emitted
            second = self._run_root(
                statement.statements,
                owner=owner,
                waveform_table=first.waveform_table,
                trace=trace + (f"Loop at line {statement.location.line}",),
                call_depth=call_depth,
            )
            self.emitted_vectors = start_emitted
            if _item_signature(first.items) != _item_signature(second.items) or first.stopped != second.stopped:
                raise StilError(
                    "ERR_NON_STATIONARY_PRESERVED_LOOP",
                    f"Loop at line {statement.location.line} has different vectors on its second iteration; use --loop-policy expand",
                )
            self.emitted_vectors = start_emitted + len(first.vectors)
            loop = LoopDecl(statement.loop_count, first.items, statement.location)
            return _SequenceResult((loop,), first.vectors, first.waveform_table)

        def _check_call_cycle(self, key: tuple[str, str], line: int, call_depth: int) -> None:
            if call_depth >= 64:
                raise StilError("ERR_CALL_DEPTH", f"STIL call depth exceeds 64 at line {line}")
            if key in self.active_calls:
                chain = " -> ".join(f"{kind} {name}" for kind, name in (*self.active_calls, key))
                raise StilError("ERR_RECURSIVE_CALL", f"recursive STIL call at line {line}: {chain}")

    lowerer = _Lowerer()
    lowered = lowerer._run_root(
        pattern.statements,
        owner=f"Pattern {pattern.name}",
        waveform_table=None,
        trace=(),
    )
    if not lowered.vectors:
        raise StilError("ERR_EMPTY_PATTERN", f"Pattern {pattern.name} has no vectors")
    if loop_policy == "preserve":
        vectors = tuple(lowered.vectors)
        items = lowered.items
    else:
        vectors = tuple(lowered.vectors)
        items = ()
    return replace(pattern, vectors=vectors, items=items, statements=())


def _select_definitions(
    definitions,
    *,
    allowed_domains: tuple[str, ...],
    all_domains_when_unbound: bool,
    kind: str,
):  # type: ignore[no-untyped-def]
    visible_domains = set(allowed_domains)
    if all_domains_when_unbound:
        selected = list(definitions)
    else:
        # Unnamed STIL definitions are global and remain visible alongside
        # definitions from the selected PatternBurst domains.
        selected = [item for item in definitions if item.domain == "" or item.domain in visible_domains]
    if allowed_domains:
        missing = sorted(set(allowed_domains) - {item.domain for item in definitions})
        if missing:
            raise StilError("ERR_DEFINITION_DOMAIN_NOT_FOUND", f"{kind} domain(s) not found: {', '.join(missing)}")
    result = {}
    for item in selected:
        result.setdefault(item.name, []).append(item)
    return {name: tuple(items) for name, items in result.items()}


def _validate_domain_references(document: Document, burst) -> None:  # type: ignore[no-untyped-def]
    if burst is None:
        return
    declared_macro_domains = {item.domain for item in document.macros}
    declared_procedure_domains = {item.domain for item in document.procedures}
    missing_macros = set(burst.macro_domains) - declared_macro_domains
    missing_procedures = set(burst.procedure_domains) - declared_procedure_domains
    if missing_macros or missing_procedures:
        missing = sorted(f"MacroDefs {name}" for name in missing_macros) + sorted(
            f"Procedures {name}" for name in missing_procedures
        )
        raise StilError("ERR_DEFINITION_DOMAIN_NOT_FOUND", f"PatternBurst references missing definition domain(s): {', '.join(missing)}")


def _item_signature(items: tuple[PatternItem, ...]) -> tuple[object, ...]:
    signature: list[object] = []
    for item in items:
        if isinstance(item, LoopDecl):
            signature.append(("loop", item.loop_count, _item_signature(item.items)))
        else:
            signature.append(("vector", item.target, item.chars, item.waveform_table_name))
    return tuple(signature)
