from __future__ import annotations

import re
from pathlib import Path

from .ast import (
    Diagnostic,
    Document,
    EventDecl,
    LoopDecl,
    MacroDecl,
    PatternBurstDecl,
    PatternDecl,
    PatternItem,
    ProcedureDecl,
    SignalDecl,
    SignalGroupDecl,
    SourceLocation,
    VectorDecl,
    WaveformCharDecl,
    WaveformDecl,
    WaveformTableDecl,
)
from .errors import StilError
from .source_artifacts import read_source_text
from .vector_data import WAVEFORM_CHAR_CLASS
from .pattern_syntax import (
    materialize_legacy_pattern,
    parse_definition_units,
    parse_patlist_names,
    parse_pattern_statements,
)

_SIGNAL_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+(In|Out|InOut)\s*;\s*$")
_GROUP_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'([^']+)'\s*;\s*$")
_WFT_RE = re.compile(r'^\s*WaveformTable\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\s*\{\s*$')
_PATTERNBURST_RE = re.compile(r"^\s*PatternBurst\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
_PATTERN_RE = re.compile(r"^\s*Pattern\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
_MACRODEFS_RE = re.compile(r"^\s*MacroDefs(?:\s+\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)|\s+([A-Za-z_][A-Za-z0-9_.]*))?\s*\{")
_PROCEDURES_RE = re.compile(r"^\s*Procedures(?:\s+\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)|\s+([A-Za-z_][A-Za-z0-9_.]*))?\s*\{")
_LOOP_RE = re.compile(r"^\s*Loop\s+([0-9]+)\s*\{\s*$")
_PERIOD_RE = re.compile(r"^\s*Period\s+'([^']+)'\s*;\s*$")
_LABEL_PREFIX = r"(?:[A-Za-z_][A-Za-z0-9_.]*:\s*)?"
_W_RE = re.compile(r'^\s*' + _LABEL_PREFIX + r'W\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\s*;\s*$')
_VECTOR_RE = re.compile(r"^\s*" + _LABEL_PREFIX + r"V\s*\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(" + WAVEFORM_CHAR_CLASS + r"+)\s*;\s*\}\s*$")
_WAVEFORM_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\{\s*(.+)\s*\}\s*$")
_CHAR_DEF_RE = re.compile(r"\s*(" + WAVEFORM_CHAR_CLASS + r")\s*:\s*([^;]+)\s*;?")
_EVENT_RE = re.compile(r"\s*([^,\s]+)\s+([A-Za-z]+)(?:\(([^)]*)\))?\s*")

_NONSUPPORTED_KEYWORDS = ("Subroutine", "Scan", "Parallel")
_COMMENT_OR_QUOTE = re.compile(r"[\"'/]")
_QUOTE_END = {quote: re.compile(r"\\[\s\S]|" + re.escape(quote)) for quote in ("'", '"')}


def _strip_line_comment(line: str) -> str:
    """Remove a STIL ``//`` comment without touching quoted text.

    STIL generators commonly emit comments inside Pattern and Waveforms
    blocks, and may append them to otherwise valid declarations.  Parsing
    the comment-free line also keeps braces in comments from affecting block
    matching.  STIL strings use both single and double quotes, so do not
    treat ``//`` inside either quoted form as a comment delimiter.
    """

    quote: str | None = None
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "/" and index + 1 < len(line) and line[index + 1] == "/":
            return line[:index]
        index += 1
    return line


def _strip_comments(text: str, *, source_file: str) -> str:
    """Remove // and C-style block comments while preserving source offsets."""

    if "/" not in text:
        return text
    output: list[str] = []
    index = 0
    copied_through = 0
    while index < len(text):
        marker = _COMMENT_OR_QUOTE.search(text, index)
        if marker is None:
            break
        index = marker.start()
        char = text[index]
        if char in {"'", '"'}:
            # Jump across quoted spans, including escapes and newlines. A
            # missing closing quote is left for the existing Pattern lexer.
            closing = next((match for match in _QUOTE_END[char].finditer(text, index + 1)
                            if match.group(0) == char), None)
            if closing is None:
                break
            index = closing.end()
            continue
        following = text[index + 1:index + 2]
        if following not in {"*", "/"}:
            index += 1
            continue
        if following == "*":
            closing_index = text.find("*/", index + 2)
            if closing_index < 0:
                line = 1 + text.count("\n", 0, index)
                raise StilError("ERR_UNCLOSED_COMMENT", f"{source_file}:{line}: unclosed block comment")
            end = closing_index + 2
        else:
            end = text.find("\n", index + 2)
            if end < 0:
                end = len(text)
        output.append(text[copied_through:index])
        output.append(re.sub(r"[^\n]", " ", text[index:end]))
        index = copied_through = end
    if not output:
        return text
    output.append(text[copied_through:])
    return "".join(output)


def parse_stil_text(text: str, *, source_file: str, loop_policy: str = "expand") -> Document:
    if loop_policy not in {"expand", "preserve"}:
        raise ValueError(f"unsupported loop_policy: {loop_policy}")
    lines = _strip_comments(text, source_file=source_file).splitlines()
    signals: list[SignalDecl] = []
    groups: list[SignalGroupDecl] = []
    waveform_tables: list[WaveformTableDecl] = []
    pattern_bursts: list[PatternBurstDecl] = []
    patterns: list[PatternDecl] = []
    macros: list[MacroDecl] = []
    procedures: list[ProcedureDecl] = []
    diagnostics: list[Diagnostic] = []
    index = 0

    def loc(line_no: int) -> SourceLocation:
        return SourceLocation(source_file=source_file, line=line_no, column=1)

    def reject(line_no: int, code: str, message: str) -> None:
        raise StilError(code, f"{source_file}:{line_no}: {message}")

    def vector_widths() -> dict[str, int]:
        return {**{signal.name: 1 for signal in signals},
                **{group.name: len(group.signals) for group in groups}}

    while index < len(lines):
        line_no = index + 1
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("//"):
            continue
        if any(line.startswith(keyword) for keyword in _NONSUPPORTED_KEYWORDS) or line.startswith("Procedure "):
            reject(line_no, "ERR_UNSUPPORTED_BLOCK", f"unsupported STIL block: {line.split()[0]}")
        if line.startswith("Header"):
            _consume_brace_block(lines, index - 1)
            index = _find_block_end(lines, index - 1) + 1
            continue
        if line.startswith("Signals"):
            body_start = index
            end = _find_block_end(lines, index - 1)
            for body_line_no in range(body_start + 1, end + 1):
                raw = lines[body_line_no - 1]
                stripped = raw.strip()
                if not stripped or stripped == "}":
                    continue
                match = _SIGNAL_RE.match(raw)
                if not match:
                    reject(body_line_no, "ERR_BAD_SIGNALS", f"bad Signals declaration: {raw.strip()}")
                name, direction = match.groups()
                signals.append(SignalDecl(name=name, direction=direction, location=loc(body_line_no)))
            index = end + 1
            continue
        if line.startswith("SignalGroups"):
            end = _find_block_end(lines, index - 1)
            for body_line_no in range(index + 1, end + 1):
                raw = lines[body_line_no - 1]
                stripped = raw.strip()
                if not stripped or stripped == "}":
                    continue
                match = _GROUP_RE.match(raw)
                if not match:
                    reject(body_line_no, "ERR_BAD_SIGNAL_GROUP", f"bad SignalGroups declaration: {raw.strip()}")
                name, payload = match.groups()
                group_signals = tuple(part.strip() for part in payload.split("+"))
                groups.append(SignalGroupDecl(name=name, signals=group_signals, location=loc(body_line_no)))
            index = end + 1
            continue
        macrodefs_match = _MACRODEFS_RE.match(lines[index - 1])
        if macrodefs_match:
            outer_domain = macrodefs_match.group(1) or macrodefs_match.group(2)
            body, end = _extract_brace_block_body(lines, index - 1, source_file=source_file)
            units = parse_definition_units(
                body,
                source_file=source_file,
                start_line=line_no,
                kind="Macro",
                outer_domain=outer_domain,
                vector_widths=vector_widths(),
            )
            macros.extend(MacroDecl(name=name, domain=domain, statements=body, location=unit_loc) for domain, name, body, unit_loc in units)
            index = end + 1
            continue
        procedures_match = _PROCEDURES_RE.match(lines[index - 1])
        if procedures_match:
            outer_domain = procedures_match.group(1) or procedures_match.group(2)
            body, end = _extract_brace_block_body(lines, index - 1, source_file=source_file)
            units = parse_definition_units(
                body,
                source_file=source_file,
                start_line=line_no,
                kind="Procedure",
                outer_domain=outer_domain,
                vector_widths=vector_widths(),
            )
            procedures.extend(
                ProcedureDecl(name=name, domain=domain, statements=body, location=unit_loc)
                for domain, name, body, unit_loc in units
            )
            index = end + 1
            continue
        wft_match = _WFT_RE.match(lines[index - 1])
        if wft_match:
            name = wft_match.group(1) or wft_match.group(2)
            end = _find_block_end(lines, index - 1)
            period_text = ""
            waveforms: list[WaveformDecl] = []
            waveform_indices: dict[str, int] = {}
            cursor = index
            while cursor < end:
                raw = lines[cursor]
                stripped = raw.strip()
                if not stripped or stripped == "}":
                    cursor += 1
                    continue
                period_match = _PERIOD_RE.match(raw)
                if period_match:
                    period_text = period_match.group(1)
                    cursor += 1
                    continue
                if stripped.startswith("Waveforms"):
                    waveforms_end = _find_block_end(lines, cursor)
                    for wf_line_no in range(cursor + 2, waveforms_end + 1):
                        wf_raw = lines[wf_line_no - 1]
                        wf_stripped = wf_raw.strip()
                        if not wf_stripped or wf_stripped == "}":
                            continue
                        wf_match = _WAVEFORM_LINE_RE.match(wf_raw)
                        if not wf_match:
                            reject(wf_line_no, "ERR_BAD_WAVEFORM", f"bad waveform line: {wf_raw.strip()}")
                        signal_or_group, payload = wf_match.groups()
                        char_defs: list[WaveformCharDecl] = []
                        for char_match in _CHAR_DEF_RE.finditer(payload):
                            waveform_char = char_match.group(1)
                            event_blob = char_match.group(2)
                            events: list[EventDecl] = []
                            for part in event_blob.split(","):
                                event_match = _EVENT_RE.match(part)
                                if not event_match:
                                    reject(wf_line_no, "ERR_BAD_WAVEFORM_EVENT", f"bad waveform event: {part.strip()}")
                                time_text, action_name, action_arg = event_match.groups()
                                events.append(
                                    EventDecl(
                                        time_text=time_text,
                                        action_name=action_name,
                                        action_arg=action_arg,
                                        location=loc(wf_line_no),
                                    )
                                )
                            char_defs.append(WaveformCharDecl(char=waveform_char, events=tuple(events), location=loc(wf_line_no)))
                        if not char_defs:
                            reject(wf_line_no, "ERR_BAD_WAVEFORM", f"missing waveform-char definitions: {wf_raw.strip()}")
                        waveform = WaveformDecl(
                            signal_or_group=signal_or_group,
                            chars=tuple(char_defs),
                            location=loc(wf_line_no),
                        )
                        previous_index = waveform_indices.get(signal_or_group)
                        if previous_index is None:
                            waveform_indices[signal_or_group] = len(waveforms)
                            waveforms.append(waveform)
                        else:
                            previous = waveforms[previous_index]
                            waveforms[previous_index] = WaveformDecl(
                                signal_or_group=signal_or_group,
                                chars=previous.chars + waveform.chars,
                                location=previous.location,
                            )
                    cursor = waveforms_end + 1
                    continue
                cursor += 1
            if not period_text:
                reject(line_no, "ERR_BAD_WFT", f"WaveformTable {name} missing Period")
            waveform_tables.append(WaveformTableDecl(name=name, period_text=period_text, waveforms=tuple(waveforms), location=loc(line_no)))
            index = end + 1
            continue
        pb_match = _PATTERNBURST_RE.match(lines[index - 1])
        if pb_match:
            name = pb_match.group(1)
            patternburst_content, end = _extract_brace_block_body(lines, index - 1, source_file=source_file)
            patlist_matches = list(
                re.finditer(r"\bPatList\s*\{([^{}]*)\}", patternburst_content, flags=re.DOTALL)
            )
            has_patlist_keyword = re.search(r"\bPatList\b", patternburst_content) is not None
            if has_patlist_keyword and not patlist_matches:
                reject(line_no, "ERR_BAD_PATTERNBURST", f"malformed PatList in PatternBurst {name}")
            if len(patlist_matches) > 1:
                reject(line_no, "ERR_BAD_PATTERNBURST", f"PatternBurst {name} has more than one PatList")
            patterns_found: tuple[str, ...] = ()
            if patlist_matches:
                patlist_match = patlist_matches[0]
                patterns_found = parse_patlist_names(
                    patlist_match.group(1),
                    source_file=source_file,
                    start_line=line_no + patternburst_content.count("\n", 0, patlist_match.start(1)),
                )
            patternburst_without_patlist = (
                patternburst_content[: patlist_matches[0].start()]
                + " "
                + patternburst_content[patlist_matches[0].end() :]
                if patlist_matches
                else patternburst_content
            )
            macro_domains = list(
                dict.fromkeys(
                    match.group(1)
                    for match in re.finditer(
                        r"\bMacroDefs\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;",
                        patternburst_without_patlist,
                    )
                )
            )
            procedure_domains = list(
                dict.fromkeys(
                    match.group(1)
                    for match in re.finditer(
                        r"\bProcedures\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;",
                        patternburst_without_patlist,
                    )
                )
            )
            pattern_bursts.append(
                PatternBurstDecl(
                    name=name,
                    patterns=patterns_found,
                    location=loc(line_no),
                    macro_domains=tuple(macro_domains),
                    procedure_domains=tuple(procedure_domains),
                )
            )
            index = end + 1
            continue
        pattern_match = _PATTERN_RE.match(lines[index - 1])
        if pattern_match:
            name = pattern_match.group(1)
            body, end = _extract_brace_block_body(lines, index - 1, source_file=source_file)
            statements = parse_pattern_statements(
                body,
                source_file=source_file,
                start_line=line_no,
                vector_widths=vector_widths(),
            )
            materialized = materialize_legacy_pattern(statements, loop_policy=loop_policy)
            vectors: tuple[VectorDecl, ...] = ()
            items: tuple[PatternItem, ...] = ()
            if materialized is not None:
                legacy_vectors, legacy_items, _current_wft_name = materialized
                if _current_wft_name is None:
                    reject(line_no, "ERR_BAD_PATTERN", f"Pattern {name} missing W statement")
                if loop_policy == "preserve":
                    items = legacy_items
                else:
                    vectors = legacy_vectors
            patterns.append(
                PatternDecl(
                    name=name,
                    vectors=vectors,
                    location=loc(line_no),
                    items=items,
                    statements=statements,
                )
            )
            index = end + 1
            continue
        diagnostics.append(Diagnostic("warning", "WARN_IGNORED_TOPLEVEL", f"ignored top-level line: {line}", line_no, 1))

    return Document(
        source_file=source_file,
        signals=tuple(signals),
        signal_groups=tuple(groups),
        waveform_tables=tuple(waveform_tables),
        pattern_bursts=tuple(pattern_bursts),
        patterns=tuple(patterns),
        macros=tuple(macros),
        procedures=tuple(procedures),
        diagnostics=tuple(diagnostics),
    )


def _parse_pattern_body(
    lines: list[str],
    *,
    start_index: int,
    end_index: int,
    pattern_name: str,
    loc,
    reject,
    inherited_wft_name: str | None = None,
) -> tuple[list[VectorDecl], str | None]:
    """Parse a Pattern body and expand STIL Loop blocks host-side.

    The compiler IR currently represents an explicit vector sequence, while
    ATEGen commonly emits ``Loop <count> { ... }`` in a Pattern.  Expanding
    those loops here keeps source locations and the existing compiler/backends
    unchanged.  Nested loops are supported and comments have already been
    removed by :func:`_strip_line_comment`.
    """

    current_wft_name = inherited_wft_name
    vectors: list[VectorDecl] = []
    cursor = start_index
    while cursor < end_index:
        body_line_no = cursor + 1
        raw = lines[cursor]
        stripped = raw.strip()
        if not stripped or stripped == "}":
            cursor += 1
            continue

        w_match = _W_RE.match(raw)
        if w_match:
            current_wft_name = w_match.group(1) or w_match.group(2)
            cursor += 1
            continue

        v_match = _VECTOR_RE.match(raw)
        if v_match:
            if current_wft_name is None:
                reject(body_line_no, "ERR_BAD_PATTERN", f"Vector before W statement in Pattern {pattern_name}")
            target, chars = v_match.groups()
            vectors.append(
                VectorDecl(
                    target=target,
                    chars=chars,
                    waveform_table_name=current_wft_name,
                    location=loc(body_line_no),
                )
            )
            cursor += 1
            continue

        loop_match = _LOOP_RE.match(raw)
        if loop_match:
            loop_count = int(loop_match.group(1))
            if loop_count < 1:
                reject(body_line_no, "ERR_BAD_PATTERN", f"Loop count must be positive in Pattern {pattern_name}")
            loop_end = _find_block_end(lines, cursor)
            if loop_end >= end_index:
                reject(body_line_no, "ERR_BAD_PATTERN", f"Loop escapes Pattern {pattern_name}")
            body_vectors, body_wft_name = _parse_pattern_body(
                lines,
                start_index=cursor + 1,
                end_index=loop_end,
                pattern_name=pattern_name,
                loc=loc,
                reject=reject,
                inherited_wft_name=current_wft_name,
            )
            if not body_vectors:
                reject(body_line_no, "ERR_BAD_PATTERN", f"Loop in Pattern {pattern_name} has no vectors")
            for _ in range(loop_count):
                vectors.extend(body_vectors)
            current_wft_name = body_wft_name
            cursor = loop_end + 1
            continue

        reject(body_line_no, "ERR_BAD_PATTERN", f"bad Pattern line: {stripped}")

    return vectors, current_wft_name


def _parse_pattern_items(
    lines: list[str],
    *,
    start_index: int,
    end_index: int,
    pattern_name: str,
    loc,
    reject,
    inherited_wft_name: str | None = None,
) -> tuple[list[PatternItem], str | None]:
    """Parse a Pattern while retaining Loop nesting and repetition counts."""

    current_wft_name = inherited_wft_name
    items: list[PatternItem] = []
    cursor = start_index
    while cursor < end_index:
        body_line_no = cursor + 1
        raw = lines[cursor]
        stripped = raw.strip()
        if not stripped or stripped == "}":
            cursor += 1
            continue

        w_match = _W_RE.match(raw)
        if w_match:
            current_wft_name = w_match.group(1) or w_match.group(2)
            cursor += 1
            continue

        v_match = _VECTOR_RE.match(raw)
        if v_match:
            if current_wft_name is None:
                reject(body_line_no, "ERR_BAD_PATTERN", f"Vector before W statement in Pattern {pattern_name}")
            target, chars = v_match.groups()
            items.append(
                VectorDecl(
                    target=target,
                    chars=chars,
                    waveform_table_name=current_wft_name,
                    location=loc(body_line_no),
                )
            )
            cursor += 1
            continue

        loop_match = _LOOP_RE.match(raw)
        if loop_match:
            loop_count = int(loop_match.group(1))
            if loop_count < 1:
                reject(body_line_no, "ERR_BAD_PATTERN", f"Loop count must be positive in Pattern {pattern_name}")
            loop_end = _find_block_end(lines, cursor)
            if loop_end >= end_index:
                reject(body_line_no, "ERR_BAD_PATTERN", f"Loop escapes Pattern {pattern_name}")
            body_items, body_wft_name = _parse_pattern_items(
                lines,
                start_index=cursor + 1,
                end_index=loop_end,
                pattern_name=pattern_name,
                loc=loc,
                reject=reject,
                inherited_wft_name=current_wft_name,
            )
            if not body_items:
                reject(body_line_no, "ERR_BAD_PATTERN", f"Loop in Pattern {pattern_name} has no vectors")
            items.append(LoopDecl(loop_count=loop_count, items=tuple(body_items), location=loc(body_line_no)))
            current_wft_name = body_wft_name
            cursor = loop_end + 1
            continue

        reject(body_line_no, "ERR_BAD_PATTERN", f"bad Pattern line: {stripped}")

    return items, current_wft_name


def parse_stil_file(path: str | Path, *, loop_policy: str = "expand") -> Document:
    path_obj = Path(path)
    return parse_stil_text(
        read_source_text(path_obj),
        source_file=str(path_obj),
        loop_policy=loop_policy,
    )


def _consume_brace_block(lines: list[str], start_index: int) -> None:
    _find_block_end(lines, start_index)


def _extract_brace_block_body(
    lines: list[str],
    start_index: int,
    *,
    source_file: str,
) -> tuple[str, int]:
    """Return the text inside a top-level brace block and its closing line.

    Unlike the legacy line-range extraction, this retains content that shares
    the header or closing line. Comments have already been replaced with
    whitespace by :func:`_strip_comments`, so the original line offsets remain
    stable and braces inside comments cannot affect the scan.
    """

    opening = lines[start_index].find("{")
    if opening < 0:
        raise StilError("ERR_UNCLOSED_BLOCK", f"{source_file}:{start_index + 1}: block has no opening brace")

    depth = 1
    quote: str | None = None
    escaped = False
    body: list[str] = []
    for line_index in range(start_index, len(lines)):
        raw = lines[line_index]
        begin = opening + 1 if line_index == start_index else 0
        for offset in range(begin, len(raw)):
            char = raw[offset]
            if quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in {"'", '"'}:
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    if raw[offset + 1:].strip():
                        raise StilError(
                            "ERR_TRAILING_BLOCK_CONTENT",
                            f"{source_file}:{line_index + 1}: unexpected content after closing brace",
                        )
                    body.append(raw[begin:offset])
                    return "\n".join(body), line_index
        body.append(raw[begin:])
        # Consume the implicit newline exactly as the former joined scanner
        # did, including a backslash escaping that newline inside a string.
        if quote is not None:
            escaped = False
    raise StilError("ERR_UNCLOSED_BLOCK", f"{source_file}:{start_index + 1}: unclosed block")


def _find_block_end(lines: list[str], start_index: int) -> int:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start_index, len(lines)):
        raw = lines[index]
        for char in raw:
            if quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in {"'", '"'}:
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
        if depth == 0:
            return index
    raise StilError("ERR_UNCLOSED_BLOCK", f"unclosed block starting at line {start_index + 1}")
