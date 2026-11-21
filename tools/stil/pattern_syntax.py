from __future__ import annotations

import re
from collections import deque
from dataclasses import replace
from dataclasses import dataclass
from typing import Iterable, Iterator

from .ast import (
    GotoStmt,
    LoopDecl,
    LoopStmt,
    MacroCallStmt,
    PatternItem,
    PatternStatement,
    ProcedureCallStmt,
    SourceLocation,
    StopStmt,
    VectorDecl,
    VectorStmt,
    WaveformSelectStmt,
)
from .errors import StilError
from .static_limits import MAX_STATIC_VECTOR_OCCURRENCES
from .vector_data import WAVEFORM_CHAR_CLASS, decode_vector_data

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_VECTOR_DATA = re.compile(WAVEFORM_CHAR_CLASS + "+")
_PATLIST_ENTRY = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*;")
@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    kind: str
    line: int
    column: int


def _iter_tokens(text: str, *, source_file: str, start_line: int) -> Iterator[_Token]:
    """Lex sequentially, retaining neither a token array nor a newline index."""

    index = 0
    line = start_line
    line_start = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            if char == "\n":
                line += 1
                line_start = index + 1
            index += 1
            continue
        column = index - line_start + 1
        if char in "\"'":
            quote = char
            end = index + 1
            escaped = False
            while end < len(text):
                current = text[end]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == quote:
                    end += 1
                    break
                end += 1
            else:
                raise StilError("ERR_UNCLOSED_STRING", f"{source_file}:{line}:{column}: unclosed string")
            token_text = text[index:end]
            token = _Token(token_text, "STRING", line, column)
            newlines = token_text.count("\n")
            if newlines:
                line += newlines
                line_start = index + token_text.rfind("\n") + 1
            index = end
            yield token
            continue
        if char.isdigit():
            end = index + 1
            while end < len(text) and text[end].isdigit():
                end += 1
            token = _Token(text[index:end], "NUMBER", line, column)
            index = end
            yield token
            continue
        ident = _IDENT.match(text, index)
        if ident:
            token = _Token(ident.group(0), "IDENT", line, column)
            index = ident.end()
            yield token
            continue
        vector_data = _VECTOR_DATA.match(text, index)
        if vector_data:
            token = _Token(vector_data.group(0), "DATA", line, column)
            index = vector_data.end()
            yield token
            continue
        token = _Token(char, "SYMBOL", line, column)
        index += 1
        yield token


def _tokens(text: str, *, source_file: str, start_line: int) -> list[_Token]:
    # Definition-unit parsing needs slices; the large Pattern path does not.
    return list(_iter_tokens(text, source_file=source_file, start_line=start_line))


class _StatementParser:
    def __init__(self, tokens: Iterable[_Token], *, source_file: str,
                 vector_widths: dict[str, int] | None = None) -> None:
        self.tokens = iter(tokens)
        self.lookahead: deque[_Token] = deque()
        self.source_file = source_file
        self.index = 0
        self.vector_widths = vector_widths or {}

    def _peek(self, offset: int = 0) -> _Token | None:
        while len(self.lookahead) <= offset:
            token = next(self.tokens, None)
            if token is None:
                return None
            self.lookahead.append(token)
        return self.lookahead[offset]

    def _take(self) -> _Token:
        token = self._peek()
        if token is None:
            raise StilError("ERR_BAD_PATTERN", f"{self.source_file}: unexpected end of Pattern statement")
        self.index += 1
        return self.lookahead.popleft()

    def _error(self, token: _Token | None, code: str, message: str) -> None:
        if token is None:
            raise StilError(code, f"{self.source_file}: {message}")
        raise StilError(code, f"{self.source_file}:{token.line}:{token.column}: {message}")

    def _expect(self, text: str) -> _Token:
        token = self._take()
        if token.text != text:
            self._error(token, "ERR_BAD_PATTERN", f"expected {text!r}, got {token.text!r}")
        return token

    def _name(self, purpose: str) -> _Token:
        token = self._take()
        if token.kind not in {"IDENT", "STRING"}:
            self._error(token, "ERR_BAD_PATTERN", f"expected {purpose} name")
        if token.kind == "STRING":
            token = replace(token, text=token.text[1:-1])
        return token

    def parse_sequence(self, *, stop_at_brace: bool = False) -> tuple[PatternStatement, ...]:
        statements: list[PatternStatement] = []
        while self._peek() is not None:
            if self._peek().text == "}":
                if not stop_at_brace:
                    self._error(self._peek(), "ERR_BAD_PATTERN", "unexpected closing brace")
                self._take()
                return tuple(statements)

            label: str | None = None
            label_token: _Token | None = None
            if self._peek().kind == "IDENT" and self._peek(1) is not None and self._peek(1).text == ":":
                label_token = self._take()
                self._take()
                label = label_token.text

            keyword = self._take()
            if keyword.kind != "IDENT":
                self._error(keyword, "ERR_UNSUPPORTED_PATTERN_STATEMENT", f"unsupported Pattern token {keyword.text!r}")
            location = SourceLocation(
                source_file=self.source_file,
                line=label_token.line if label_token else keyword.line,
                column=label_token.column if label_token else keyword.column,
                label=label,
            )

            if keyword.text in {"W", "WaveformTable"}:
                name = self._name("WaveformTable")
                self._expect(";")
                statements.append(WaveformSelectStmt(name=name.text, location=location))
            elif keyword.text in {"V", "Vector"}:
                self._expect("{")
                target = self._name("vector target")
                self._expect("=")
                data_parts: list[str] = []
                previous: _Token | None = None
                while self._peek() is not None and self._peek().text != ";":
                    token = self._take()
                    # The lexer splits e.g. 01HL into NUMBER/IDENT tokens.
                    # Restore whitespace boundaries so \\rN affects exactly
                    # the next source group, not every remaining character.
                    if previous is not None and (
                        token.line != previous.line
                        or token.column != previous.column + len(previous.text)
                    ):
                        data_parts.append(" ")
                    data_parts.append(token.text)
                    previous = token
                if not data_parts:
                    self._error(self._peek(), "ERR_UNSUPPORTED_PATTERN_DATA", "empty vector data")
                try:
                    chars = decode_vector_data("".join(data_parts), max_width=self.vector_widths.get(target.text))
                except StilError as exc:
                    self._error(keyword, exc.code, str(exc))
                self._expect(";")
                self._expect("}")
                statements.append(VectorStmt(target=target.text, chars=chars, location=location))
            elif keyword.text == "Loop":
                count = self._take()
                if count.kind != "NUMBER":
                    self._error(count, "ERR_BAD_PATTERN", "Loop count must be a decimal integer")
                loop_count = int(count.text)
                if loop_count < 1:
                    self._error(count, "ERR_BAD_PATTERN", "Loop count must be positive")
                self._expect("{")
                body = self.parse_sequence(stop_at_brace=True)
                if not body:
                    self._error(keyword, "ERR_BAD_PATTERN", "Loop body must not be empty")
                statements.append(LoopStmt(loop_count=loop_count, statements=body, location=location))
            elif keyword.text in {"Macro", "Call"}:
                name = self._name(keyword.text)
                if self._peek() is not None and self._peek().text in {"{", "("}:
                    self._error(self._peek(), "ERR_PARAMETERIZED_CALL_UNSUPPORTED", f"{keyword.text} data arguments are not supported in this subset")
                self._expect(";")
                node = MacroCallStmt(name=name.text, location=location) if keyword.text == "Macro" else ProcedureCallStmt(name=name.text, location=location)
                statements.append(node)
            elif keyword.text == "Goto":
                target = self._name("Goto target")
                self._expect(";")
                statements.append(GotoStmt(target=target.text, location=location))
            elif keyword.text == "Stop":
                self._expect(";")
                statements.append(StopStmt(location=location))
            else:
                self._error(keyword, "ERR_UNSUPPORTED_PATTERN_STATEMENT", f"unsupported Pattern statement {keyword.text!r}")

        if stop_at_brace:
            self._error(None, "ERR_UNCLOSED_BLOCK", "unclosed Pattern statement block")
        return tuple(statements)


def parse_pattern_statements(
    text: str,
    *,
    source_file: str,
    start_line: int,
    vector_widths: dict[str, int] | None = None,
) -> tuple[PatternStatement, ...]:
    tokens = _iter_tokens(text, source_file=source_file, start_line=start_line)
    parser = _StatementParser(tokens, source_file=source_file, vector_widths=vector_widths)
    try:
        return parser.parse_sequence()
    except StilError:
        # The eager lexer used to find lexical errors before syntax errors,
        # even at EOF. Keep that diagnostic precedence without storing tokens.
        for _ in tokens:
            pass
        raise


def parse_patlist_names(text: str, *, source_file: str, start_line: int) -> tuple[str, ...]:
    """Parse the supported PatList subset, including multiple entries per line."""

    names: list[str] = []
    cursor = 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor == len(text):
            break
        match = _PATLIST_ENTRY.match(text, cursor)
        if match is None:
            line = start_line + text.count("\n", 0, cursor)
            column = cursor - text.rfind("\n", 0, cursor)
            fragment = text[cursor : text.find("\n", cursor) if "\n" in text[cursor:] else len(text)].strip()
            raise StilError(
                "ERR_BAD_PATTERNBURST",
                f"{source_file}:{line}:{column}: bad PatList entry: {fragment}",
            )
        names.append(match.group(1))
        cursor = match.end()
    return tuple(names)


def parse_named_statement_units(
    text: str,
    *,
    source_file: str,
    start_line: int,
    domain: str,
    kind: str,
) -> tuple[tuple[str, tuple[PatternStatement, ...], SourceLocation], ...]:
    tokens = _tokens(text, source_file=source_file, start_line=start_line)
    units: list[tuple[str, tuple[PatternStatement, ...], SourceLocation]] = []
    index = 0
    while index < len(tokens):
        name_token = tokens[index]
        if name_token.kind != "IDENT" or index + 1 >= len(tokens) or tokens[index + 1].text != "{":
            raise StilError(
                "ERR_BAD_DEFINITION_BLOCK",
                f"{source_file}:{name_token.line}:{name_token.column}: expected {kind} name followed by a block in domain {domain}",
            )
        depth = 1
        cursor = index + 2
        body_start = cursor
        while cursor < len(tokens) and depth:
            if tokens[cursor].text == "{":
                depth += 1
            elif tokens[cursor].text == "}":
                depth -= 1
            cursor += 1
        if depth:
            raise StilError("ERR_UNCLOSED_BLOCK", f"{source_file}:{name_token.line}: unclosed {kind} {name_token.text}")
        body_tokens = tokens[body_start : cursor - 1]
        statements = _StatementParser(body_tokens, source_file=source_file).parse_sequence()
        units.append(
            (
                name_token.text,
                statements,
                SourceLocation(source_file=source_file, line=name_token.line, column=name_token.column),
            )
        )
        index = cursor
    return tuple(units)


def parse_definition_units(
    text: str,
    *,
    source_file: str,
    start_line: int,
    kind: str,
    outer_domain: str | None = None,
    vector_widths: dict[str, int] | None = None,
) -> tuple[tuple[str, str, tuple[PatternStatement, ...], SourceLocation], ...]:
    """Parse global or domain/name blocks inside MacroDefs or Procedures.

    Standard unnamed blocks contain definitions directly, e.g.
    ``{ name { statements } }``.  Explicitly scoped blocks contain definitions
    directly when ``outer_domain`` is supplied.  For compatibility, the
    historical nested-domain form ``{ domain { name { statements } } }`` is
    also accepted when the outer block is unnamed.
    """

    tokens = _tokens(text, source_file=source_file, start_line=start_line)
    result: list[tuple[str, str, tuple[PatternStatement, ...], SourceLocation]] = []
    cursor = 0

    def matching_brace(open_index: int) -> int:
        depth = 1
        index = open_index + 1
        while index < len(tokens):
            if tokens[index].text == "{":
                depth += 1
            elif tokens[index].text == "}":
                depth -= 1
                if depth == 0:
                    return index
            index += 1
        token = tokens[open_index]
        raise StilError("ERR_UNCLOSED_BLOCK", f"{source_file}:{token.line}: unclosed {kind} block")

    def parse_units_in_domain(domain: str, begin: int, end: int) -> None:
        index = begin
        while index < end:
            name_token = tokens[index]
            if name_token.kind not in {"IDENT", "STRING"} or index + 1 >= end or tokens[index + 1].text != "{":
                raise StilError(
                    "ERR_BAD_DEFINITION_BLOCK",
                    f"{source_file}:{name_token.line}:{name_token.column}: expected {kind} name followed by a block in domain {domain}",
                )
            unit_end = matching_brace(index + 1)
            if unit_end >= end:
                raise StilError("ERR_BAD_DEFINITION_BLOCK", f"{source_file}:{name_token.line}: {kind} escapes domain {domain}")
            statements = _StatementParser(tokens[index + 2 : unit_end], source_file=source_file,
                                          vector_widths=vector_widths).parse_sequence()
            unit_name = name_token.text[1:-1] if name_token.kind == "STRING" else name_token.text
            if any(existing_domain == domain and existing_name == unit_name for existing_domain, existing_name, *_ in result):
                raise StilError("ERR_DUPLICATE_DEFINITION", f"duplicate {kind} {unit_name!r} in domain {domain!r}")
            result.append(
                (
                    domain,
                    unit_name,
                    statements,
                    SourceLocation(source_file=source_file, line=name_token.line, column=name_token.column),
                )
            )
            index = unit_end + 1

    def parse_top_level_blocks() -> list[tuple[_Token, int, int]]:
        blocks: list[tuple[_Token, int, int]] = []
        index = 0
        while index < len(tokens):
            name_token = tokens[index]
            if name_token.kind not in {"IDENT", "STRING"} or index + 1 >= len(tokens) or tokens[index + 1].text != "{":
                raise StilError(
                    "ERR_BAD_DEFINITION_BLOCK",
                    f"{source_file}:{name_token.line}:{name_token.column}: expected {kind} name followed by a block",
                )
            unit_end = matching_brace(index + 1)
            blocks.append((name_token, index + 2, unit_end))
            index = unit_end + 1
        return blocks

    if outer_domain is not None:
        parse_units_in_domain(outer_domain, 0, len(tokens))
        return tuple(result)

    top_level = parse_top_level_blocks()
    if not top_level:
        raise StilError("ERR_BAD_DEFINITION_BLOCK", f"{source_file}: empty {kind} definition block")

    # The standard unnamed form is a list of named definitions whose contents
    # are Pattern statements.  Try that exact shape first.  A failed parse is
    # only allowed to fall back to the legacy nested-domain form; if both
    # interpretations fail, report the more useful Pattern-statement error.
    global_units: list[tuple[str, str, tuple[PatternStatement, ...], SourceLocation]] = []
    global_names: set[str] = set()
    global_error: StilError | None = None
    try:
        for name_token, body_start, body_end in top_level:
            statements = _StatementParser(tokens[body_start:body_end], source_file=source_file,
                                          vector_widths=vector_widths).parse_sequence()
            name = name_token.text[1:-1] if name_token.kind == "STRING" else name_token.text
            if name in global_names:
                raise StilError("ERR_DUPLICATE_DEFINITION", f"duplicate global {kind} {name!r}")
            global_names.add(name)
            global_units.append(
                (
                    "",
                    name,
                    statements,
                    SourceLocation(source_file=source_file, line=name_token.line, column=name_token.column),
                )
            )
    except StilError as exc:
        global_error = exc

    if global_error is None:
        return tuple(global_units)

    try:
        for domain_token, domain_begin, domain_end in top_level:
            domain = domain_token.text[1:-1] if domain_token.kind == "STRING" else domain_token.text
            before = len(result)
            parse_units_in_domain(domain, domain_begin, domain_end)
            if len(result) == before:
                raise StilError(
                    "ERR_BAD_DEFINITION_BLOCK",
                    f"{source_file}:{domain_token.line}: empty {kind} domain {domain!r}",
                )
    except StilError:
        raise global_error
    return tuple(result)


def materialize_legacy_pattern(
    statements: tuple[PatternStatement, ...],
    *,
    loop_policy: str,
) -> tuple[tuple[VectorDecl, ...], tuple[PatternItem, ...], str | None] | None:
    """Build the legacy vector/loop view when no static control statements occur."""

    if _requires_control_lowering(statements):
        return None

    vectors: list[VectorDecl] = []
    items: list[PatternItem] = []
    current_wft: str | None = None

    def walk(sequence: tuple[PatternStatement, ...]) -> tuple[list[VectorDecl], list[PatternItem], str | None] | None:
        nonlocal current_wft
        local_vectors: list[VectorDecl] = []
        local_items: list[PatternItem] = []
        for statement in sequence:
            if isinstance(statement, WaveformSelectStmt):
                current_wft = statement.name
            elif isinstance(statement, VectorStmt):
                if current_wft is None:
                    raise StilError("ERR_BAD_PATTERN", f"{statement.location.source_file}:{statement.location.line}: Vector before W statement")
                if len(local_vectors) >= MAX_STATIC_VECTOR_OCCURRENCES:
                    raise StilError(
                        "ERR_STATIC_EXPANSION_LIMIT",
                        f"static Pattern expansion exceeds {MAX_STATIC_VECTOR_OCCURRENCES} vector occurrences",
                    )
                vector = VectorDecl(statement.target, statement.chars, current_wft, statement.location)
                local_vectors.append(vector)
                local_items.append(vector)
            elif isinstance(statement, LoopStmt):
                incoming_wft = current_wft
                body = walk(statement.statements)
                if body is None:
                    return None
                body_vectors, body_items, body_wft = body
                if not body_vectors:
                    raise StilError("ERR_BAD_PATTERN", f"{statement.location.source_file}:{statement.location.line}: Loop has no vectors")
                current_wft = body_wft
                if loop_policy == "preserve":
                    if len(local_vectors) + len(body_vectors) > MAX_STATIC_VECTOR_OCCURRENCES:
                        raise StilError(
                            "ERR_STATIC_EXPANSION_LIMIT",
                            f"static Pattern body exceeds {MAX_STATIC_VECTOR_OCCURRENCES} vector records",
                        )
                    local_items.append(LoopDecl(statement.loop_count, tuple(body_items), statement.location))
                    local_vectors.extend(body_vectors)
                else:
                    if statement.loop_count * len(body_vectors) + len(local_vectors) > MAX_STATIC_VECTOR_OCCURRENCES:
                        raise StilError(
                            "ERR_STATIC_EXPANSION_LIMIT",
                            f"static Pattern expansion exceeds {MAX_STATIC_VECTOR_OCCURRENCES} vector occurrences",
                        )
                    for _ in range(statement.loop_count):
                        local_vectors.extend(body_vectors)
                        local_items.extend(body_items)
                if incoming_wft is None and current_wft is None:
                    raise StilError("ERR_BAD_PATTERN", f"{statement.location.source_file}:{statement.location.line}: Loop missing W statement")
            else:
                return None
        return local_vectors, local_items, current_wft

    materialized = walk(statements)
    if materialized is None:
        return None
    vectors, items, current_wft = materialized
    if loop_policy == "preserve":
        return tuple(vectors), tuple(items), current_wft
    return tuple(vectors), (), current_wft


def _requires_control_lowering(statements: tuple[PatternStatement, ...]) -> bool:
    for statement in statements:
        if isinstance(statement, (MacroCallStmt, ProcedureCallStmt, GotoStmt, StopStmt)):
            return True
        if isinstance(statement, LoopStmt) and _requires_control_lowering(statement.statements):
            return True
    return False
