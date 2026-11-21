from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Iterator

from .errors import StilError
from .pattern_syntax import parse_definition_units, parse_patlist_names
from .parser import _strip_comments
from .vector_data import WAVEFORM_CHAR_CLASS, decode_vector_data

_SIGNAL_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+(In|Out|InOut)\s*;\s*$")
_GROUP_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'([^']+)'\s*;\s*$")
_PATTERN_RE = re.compile(r"\bPattern\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
_PATTERNBURST_HEADER_RE = re.compile(r"(?m)^\s*PatternBurst\s+[A-Za-z_][A-Za-z0-9_.]*\s*\{")
_DEFINITION_HEADER_RE = re.compile(
    r"(?m)^\s*(MacroDefs|Procedures)(?:\s+\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)|\s+([A-Za-z_][A-Za-z0-9_.]*))?\s*\{"
)
_VECTOR_STMT_RE = re.compile(
    r"\b(?:V|Vector)\s*\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;{}]*?)(\s*;\s*\})"
)
_WFT_HEADER_RE = re.compile(r'^\s*WaveformTable\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\s*\{\s*$')
_PERIOD_RE = re.compile(r"^\s*Period\s+'([^']+)'\s*;\s*$")
_ATE_WAVE_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\{\s*(" + WAVEFORM_CHAR_CLASS + r")\s*\{\s*(.*?)\s*\}\s*\}\s*$"
)
_ATE_EVENT_RE = re.compile(r"'([^']+)'\s*([UDZHLXT])\s*;\s*")


@dataclass(frozen=True)
class SignalDecl:
    name: str
    direction: str


@dataclass(frozen=True)
class SignalGroupDecl:
    name: str
    signals: tuple[str, ...]


@dataclass(frozen=True)
class PreprocessResult:
    output_text: str
    signals: tuple[SignalDecl, ...]
    signal_groups: tuple[SignalGroupDecl, ...]


def read_ategen_signals(project_path: str | Path) -> tuple[SignalDecl, ...]:
    """Read metadata without converting or retaining the large Pattern file."""

    return tuple(_extract_signals(Path(project_path).read_text(encoding="utf-8")))


def default_stimulus_only_ignore_signals(result: PreprocessResult) -> set[str]:
    return {signal.name for signal in result.signals if signal.direction == "Out"}


def preprocess_ategen_stil(
    project_path: str | Path,
    pattern_path: str | Path,
    *,
    ignore_signals: set[str] | None = None,
    drop_compare_signals: set[str] | None = None,
    shift_compare_signals: set[str] | None = None,
    shift_compare_vectors: int = 0,
) -> PreprocessResult:
    ignore = ignore_signals or set()
    drop_compare = drop_compare_signals or set()
    project_text = Path(project_path).read_text(encoding="utf-8")
    pattern_text = Path(pattern_path).read_text(encoding="utf-8")

    signals = _extract_signals(project_text)
    signal_names = {signal.name for signal in signals}
    signal_groups = _filter_signal_groups(_extract_signal_groups(project_text), signal_names)
    timing_tables = _extract_timing(project_text)
    pattern_block = _extract_pattern_block(pattern_text)
    pattern_match = _PATTERN_RE.search(_strip_comments(pattern_text, source_file=str(pattern_path)))
    assert pattern_match is not None
    pattern_name = pattern_match.group(1)
    definition_blocks = _extract_definition_blocks(project_text, source_file=str(project_path))
    definition_blocks.extend(_extract_definition_blocks(pattern_text, source_file=str(pattern_path)))
    definition_blocks = _deduplicate_definition_blocks(definition_blocks)
    if shift_compare_vectors > 0 and shift_compare_signals and re.search(r"\b(?:Macro|Call)\s+[A-Za-z_][A-Za-z0-9_.]*\s*;", pattern_block):
        raise StilError(
            "ERR_SHIFT_WITH_MACRO_CALL_UNSUPPORTED",
            "compare-vector shifting across Macro/Procedure expansion is not supported by the split-file preprocessor",
        )
    macro_domains, procedure_domains, selected_patternburst = _extract_patternburst_context(
        project_text + "\n" + pattern_text,
        source_file=f"{project_path} + {pattern_path}",
        selected_pattern=pattern_name,
    )
    # Source strings are no longer used. Do not retain them during the vector
    # rewrite/shift and final output allocation.
    del project_text, pattern_text

    signal_index = {signal.name: idx for idx, signal in enumerate(signals)}
    group_map = {group.name: group.signals for group in signal_groups}
    pattern_block = _rewrite_pattern_vectors(pattern_block, signal_index, group_map, ignore, drop_compare)
    definition_blocks = [
        _rewrite_pattern_vectors(block, signal_index, group_map, ignore, drop_compare)
        for block in definition_blocks
    ]
    if shift_compare_vectors > 0 and shift_compare_signals:
        pattern_block = _shift_pattern_compare_vectors(
            pattern_block,
            signal_index,
            group_map,
            shift_compare_signals,
            shift_compare_vectors,
        )

    lines: list[str] = []
    lines.append(f'Header {{ Title "{Path(project_path).stem} preprocessed"; }}')
    lines.append("Signals {")
    for signal in signals:
        lines.append(f"  {signal.name} {signal.direction};")
    lines.append("}")
    lines.append("SignalGroups {")
    for group in signal_groups:
        lines.append(f"  {group.name} = '" + " + ".join(group.signals) + "';")
    lines.append("}")
    lines.append("Timing {")
    for wft_name, period_text, wave_lines in timing_tables:
        lines.append(f"  WaveformTable {wft_name} {{")
        lines.append(f"    Period '{period_text}';")
        lines.append("    Waveforms {")
        lines.extend(f"      {line}" for line in wave_lines)
        lines.append("    }")
        lines.append("  }")
    lines.append("}")
    lines.extend(block.rstrip() for block in definition_blocks)
    if selected_patternburst or macro_domains or procedure_domains:
        lines.append("PatternBurst STPG_PREPROCESSED {")
        lines.extend(f"  MacroDefs {domain};" for domain in macro_domains)
        lines.extend(f"  Procedures {domain};" for domain in procedure_domains)
        lines.append("  PatList {")
        lines.append(f"    {pattern_name};")
        lines.append("  }")
        lines.append("}")
    lines.append(pattern_block.rstrip())
    lines.append("")

    return PreprocessResult(
        output_text="\n".join(lines),
        signals=tuple(signals),
        signal_groups=tuple(signal_groups),
    )


def build_default_pin_map(
    result: PreprocessResult,
    *,
    ignored_signals: set[str] | None = None,
) -> dict[str, object]:
    ignored = ignored_signals or set()
    kept = [signal for signal in result.signals if signal.name not in ignored]
    role_map = {"In": "input", "Out": "output", "InOut": "inout"}
    active_pins = _firmware_compatible_active_pins(len(kept))
    return {
        "active_pins": active_pins,
        "signals": {
            signal.name: {
                "bit": index,
                "logical_pin": f"DUT{index}",
                "direction": role_map[signal.direction],
                "role": "signal",
            }
            for index, signal in enumerate(kept)
        },
    }


def _firmware_compatible_active_pins(signal_count: int) -> int:
    if signal_count <= 0 or signal_count > 24:
        raise StilError("ERR_BAD_PIN_MAP", f"stimulus-only signal count out of supported range: {signal_count}")
    if signal_count <= 16:
        return 16
    return 24


def _filter_signal_groups(groups: list[SignalGroupDecl], signal_names: set[str]) -> list[SignalGroupDecl]:
    filtered: list[SignalGroupDecl] = []
    for group in groups:
        members = tuple(signal for signal in group.signals if signal in signal_names)
        filtered.append(SignalGroupDecl(name=group.name, signals=members))
    return filtered


def _extract_named_block(text: str, keyword: str) -> str:
    start = text.find(f"{keyword} ")
    if start < 0:
        start = text.find(f"{keyword}{{")
    if start < 0:
        start = text.find(f"{keyword}\n")
    if start < 0:
        raise StilError("ERR_UNSUPPORTED_BLOCK", f"{keyword} block not found")
    brace = text.find("{", start)
    if brace < 0:
        raise StilError("ERR_UNCLOSED_BLOCK", f"{keyword} block missing '{{'")
    depth = 0
    for index in range(brace, len(text)):
        ch = text[index]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise StilError("ERR_UNCLOSED_BLOCK", f"{keyword} block is not closed")


def _extract_signals(project_text: str) -> list[SignalDecl]:
    block = _extract_named_block(project_text, "Signals")
    signals: list[SignalDecl] = []
    for raw in block.splitlines()[1:-1]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("//"):
            continue
        match = _SIGNAL_RE.match(stripped)
        if not match:
            raise StilError("ERR_BAD_SIGNALS", f"bad Signals declaration: {stripped}")
        name, direction = match.groups()
        signals.append(SignalDecl(name=name, direction=direction))
    return signals


def _extract_signal_groups(project_text: str) -> list[SignalGroupDecl]:
    block = _extract_named_block(project_text, "SignalGroups")
    groups: list[SignalGroupDecl] = []
    for raw in block.splitlines()[1:-1]:
        stripped = raw.strip()
        if stripped.startswith("//"):
            stripped = stripped[2:].strip()
        if not stripped or stripped.startswith("//"):
            continue
        match = _GROUP_RE.match(stripped)
        if not match:
            raise StilError("ERR_BAD_SIGNAL_GROUP", f"bad SignalGroups declaration: {stripped}")
        name, payload = match.groups()
        groups.append(SignalGroupDecl(name=name, signals=tuple(part.strip() for part in payload.split("+"))))
    return groups


def _extract_timing(project_text: str) -> list[tuple[str, str, list[str]]]:
    timing_block = _extract_named_block(project_text, "Timing")
    lines = [_strip_line_comment(raw) for raw in timing_block.splitlines()]
    tables: list[tuple[str, str, list[str]]] = []
    cursor = 0
    while cursor < len(lines):
        match = _WFT_HEADER_RE.match(lines[cursor])
        if not match:
            cursor += 1
            continue

        wft_name = match.group(1) or match.group(2)
        table_end = _find_line_block_end(lines, cursor)
        period_text = ""
        converted: list[str] = []
        for raw in lines[cursor + 1 : table_end]:
            period_match = _PERIOD_RE.match(raw)
            if period_match:
                period_text = period_match.group(1)
                continue
            wave_match = _ATE_WAVE_RE.match(raw)
            if not wave_match:
                continue
            signal_name, waveform_char, event_blob = wave_match.groups()
            converted.append(_convert_waveform_line(signal_name, waveform_char, event_blob))

        if not period_text or not converted:
            raise StilError("ERR_BAD_WFT", f"could not extract ATEGen WaveformTable {wft_name}")
        tables.append((wft_name, period_text, converted))
        cursor = table_end + 1

    if not tables:
        raise StilError("ERR_BAD_WFT", "could not extract ATEGen WaveformTable")
    return tables


def _strip_line_comment(line: str) -> str:
    """Remove a ``//`` comment while preserving quoted STIL text."""

    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
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
            continue
        if char == "/" and index + 1 < len(line) and line[index + 1] == "/":
            return line[:index]
    return line


def _find_line_block_end(lines: list[str], start_index: int) -> int:
    depth = 0
    for index in range(start_index, len(lines)):
        raw = lines[index]
        depth += raw.count("{")
        depth -= raw.count("}")
        if depth == 0:
            return index
    raise StilError("ERR_UNCLOSED_BLOCK", f"unclosed block starting at line {start_index + 1}")


def _convert_waveform_line(signal_name: str, waveform_char: str, event_blob: str) -> str:
    events = list(_ATE_EVENT_RE.finditer(event_blob))
    cursor = 0
    for event in events:
        if event_blob[cursor : event.start()].strip():
            raise StilError("ERR_BAD_WAVEFORM_EVENT", f"unsupported ATE waveform event: {event_blob.strip()}")
        cursor = event.end()
    if not events or event_blob[cursor:].strip():
        raise StilError("ERR_BAD_WAVEFORM_EVENT", f"unsupported ATE waveform event: {event_blob.strip()}")

    converted: list[str] = []
    for event in events:
        time_text, action_code = event.groups()
        action = {
            "U": "Drive(1)",
            "D": "Drive(0)",
            "Z": "DirZ",
            "H": "Compare(1)",
            "L": "Compare(0)",
            "X": "Compare(X)",
            # ATEGen's T character is a no-compare/termination marker.  STPG
            # treats vector T as a host-side no-op, so Compare(X) is the
            # closest representable timing declaration and is never emitted
            # for a T vector by resolve.py.
            "T": "Compare(X)",
        }.get(action_code)
        if action is None:
            raise StilError("ERR_BAD_WAVEFORM_EVENT", f"unsupported ATE waveform action {action_code}")
        converted.append(f"{time_text} {action}")
    return f"{signal_name} {{ {waveform_char}: {', '.join(converted)}; }}"


def _extract_pattern_block(pattern_text: str) -> str:
    clean_text = _strip_comments(pattern_text, source_file="pattern STIL")
    match = _PATTERN_RE.search(clean_text)
    if not match:
        raise StilError("ERR_PATTERN_NOT_FOUND", "Pattern block not found in pattern file")
    start = match.start()
    brace = pattern_text.find("{", start)
    end = _find_matching_brace(pattern_text, brace)
    if end >= 0:
        block = pattern_text[start : end + 1]
        return _strip_vector_comments(block)
    raise StilError("ERR_UNCLOSED_BLOCK", "Pattern block is not closed")


def _extract_definition_blocks(text: str, *, source_file: str) -> list[str]:
    clean_text = _strip_comments(text, source_file=source_file)
    blocks: list[str] = []
    cursor = 0
    for match in _DEFINITION_HEADER_RE.finditer(clean_text):
        if match.start() < cursor:
            continue
        opening = clean_text.find("{", match.start(), match.end())
        end = _find_matching_brace(clean_text, opening)
        if end < 0:
            raise StilError("ERR_UNCLOSED_BLOCK", f"{source_file}:{clean_text.count(chr(10), 0, match.start()) + 1}: unclosed {match.group(1)} block")
        blocks.append(text[match.start() : end + 1].strip())
        cursor = end + 1
    return blocks


def _deduplicate_definition_blocks(blocks: list[str]) -> list[str]:
    seen: dict[tuple[str, str], str] = {}
    seen_global_blocks: set[tuple[str, str]] = set()
    seen_global_names: dict[tuple[str, str], str] = {}
    result: list[str] = []
    for block in blocks:
        clean = _strip_comments(block, source_file="definition block")
        match = _DEFINITION_HEADER_RE.match(clean)
        if match is None:
            raise StilError("ERR_BAD_DEFINITION_BLOCK", "could not identify MacroDefs/Procedures domain")
        kind = match.group(1)
        declared_domain = match.group(2) or match.group(3)
        opening = clean.find("{", match.start(), match.end())
        outer_end = _find_matching_brace(clean, opening)
        if outer_end < 0:
            raise StilError("ERR_UNCLOSED_BLOCK", f"unclosed {kind} definition block")

        if declared_domain is None:
            raw_body = block[opening + 1 : outer_end]
            try:
                parsed_units = parse_definition_units(
                    raw_body,
                    source_file="definition block",
                    start_line=2,
                    kind="Macro" if kind == "MacroDefs" else "Procedure",
                )
            except StilError:
                parsed_units = ()
            if parsed_units and all(domain == "" for domain, _name, _statements, _location in parsed_units):
                normalized_block = "\n".join(line.strip() for line in _strip_comments(raw_body, source_file="definition block").splitlines() if line.strip())
                block_key = (kind, normalized_block)
                if block_key in seen_global_blocks:
                    continue
                duplicate_names = [
                    name for _domain, name, _statements, _location in parsed_units
                    if (kind, name) in seen_global_names
                ]
                if duplicate_names:
                    duplicates = ", ".join(sorted(set(duplicate_names)))
                    raise StilError(
                        "ERR_DUPLICATE_DEFINITION",
                        f"duplicate global {kind} definition(s) across project/pattern STIL: {duplicates}",
                    )
                seen_global_blocks.add(block_key)
                for _domain, name, _statements, _location in parsed_units:
                    seen_global_names[(kind, name)] = normalized_block
                result.append(f"{kind} {{\n{raw_body.strip()}\n}}")
                continue

        domain_bodies: list[tuple[str, str, str]] = []
        if declared_domain is not None:
            body = block[opening + 1 : outer_end]
            clean_body = clean[opening + 1 : outer_end]
            domain_bodies.append((declared_domain, body, clean_body))
        else:
            cursor = opening + 1
            while cursor < outer_end:
                while cursor < outer_end and clean[cursor].isspace():
                    cursor += 1
                if cursor >= outer_end:
                    break
                if clean[cursor] in "'\"":
                    quote = clean[cursor]
                    domain_start = cursor + 1
                    cursor += 1
                    while cursor < outer_end and clean[cursor] != quote:
                        cursor += 1
                    if cursor >= outer_end:
                        raise StilError("ERR_BAD_DEFINITION_BLOCK", f"unclosed {kind} domain name")
                    domain = clean[domain_start:cursor]
                    cursor += 1
                else:
                    domain_match = re.match(r"[A-Za-z_][A-Za-z0-9_.]*", clean[cursor:outer_end])
                    if domain_match is None:
                        raise StilError("ERR_BAD_DEFINITION_BLOCK", f"bad {kind} domain declaration")
                    domain = domain_match.group(0)
                    cursor += domain_match.end()
                while cursor < outer_end and clean[cursor].isspace():
                    cursor += 1
                if cursor >= outer_end or clean[cursor] != "{":
                    raise StilError("ERR_BAD_DEFINITION_BLOCK", f"{kind} domain {domain!r} is missing a block")
                domain_open = cursor
                domain_end = _find_matching_brace(clean, domain_open)
                if domain_end < 0 or domain_end > outer_end:
                    raise StilError("ERR_BAD_DEFINITION_BLOCK", f"{kind} domain {domain!r} escapes its definition block")
                raw_body = block[domain_open + 1 : domain_end]
                clean_body = clean[domain_open + 1 : domain_end]
                domain_bodies.append((domain, raw_body, clean_body))
                cursor = domain_end + 1
            if not domain_bodies:
                raise StilError("ERR_BAD_DEFINITION_BLOCK", f"empty {kind} definition-domain container")

        for domain, body, clean_body in domain_bodies:
            key = (kind, domain)
            normalized = "\n".join(line.strip() for line in clean_body.splitlines() if line.strip())
            previous = seen.get(key)
            if previous is not None:
                if previous != normalized:
                    raise StilError("ERR_DUPLICATE_DEFINITION_DOMAIN", f"conflicting {kind} domain {domain!r} in project/pattern STIL")
                continue
            seen[key] = normalized
            result.append(f"{kind} ({domain}) {{\n{body.strip()}\n}}")
    return result


def _extract_patternburst_context(
    text: str,
    *,
    source_file: str,
    selected_pattern: str,
) -> tuple[list[str], list[str], bool]:
    clean_text = _strip_comments(text, source_file=source_file)
    burst_domains: list[tuple[list[str], list[str], bool]] = []
    for match in _PATTERNBURST_HEADER_RE.finditer(clean_text):
        opening = clean_text.find("{", match.start(), match.end())
        end = _find_matching_brace(clean_text, opening)
        if end < 0:
            raise StilError("ERR_UNCLOSED_BLOCK", f"{source_file}: unclosed PatternBurst block")
        block = clean_text[match.start() : end + 1]
        patlist_matches = list(re.finditer(r"\bPatList\s*\{([^{}]*)\}", block, re.DOTALL))
        if re.search(r"\bPatList\b", block) and not patlist_matches:
            raise StilError("ERR_BAD_PATTERNBURST", f"{source_file}: malformed PatList in PatternBurst")
        if len(patlist_matches) > 1:
            raise StilError("ERR_BAD_PATTERNBURST", f"{source_file}: PatternBurst has more than one PatList")
        listed_patterns: set[str] = set()
        if patlist_matches:
            patlist_match = patlist_matches[0]
            patlist_body = patlist_match.group(1)
            patlist_line = clean_text.count("\n", 0, match.start()) + 1
            patlist_line += block.count("\n", 0, patlist_match.start(1))
            listed_patterns.update(
                parse_patlist_names(patlist_body, source_file=source_file, start_line=patlist_line)
            )
        macro_domains: list[str] = []
        procedure_domains: list[str] = []
        for domain_match in re.finditer(r"\bMacroDefs\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;", block):
            if domain_match.group(1) not in macro_domains:
                macro_domains.append(domain_match.group(1))
        for domain_match in re.finditer(r"\bProcedures\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;", block):
            if domain_match.group(1) not in procedure_domains:
                procedure_domains.append(domain_match.group(1))
        burst_domains.append((macro_domains, procedure_domains, selected_pattern in listed_patterns))
    matching = [item for item in burst_domains if item[2]]
    if not matching:
        with_domains = [item for item in burst_domains if item[0] or item[1]]
        if with_domains:
            raise StilError(
                "ERR_PATTERNBURST_SELECTION_REQUIRED",
                f"no PatternBurst PatList selects {selected_pattern!r}; refusing to borrow an unrelated definition-domain binding",
            )
    if len(matching) > 1:
        bindings = {(tuple(item[0]), tuple(item[1])) for item in matching}
        if len(bindings) > 1:
            raise StilError(
                "ERR_PATTERNBURST_SELECTION_REQUIRED",
                f"multiple PatternBursts bind different definition domains for {selected_pattern!r}",
            )
    if not matching:
        return [], [], False
    return list(matching[0][0]), list(matching[0][1]), True


def _extract_patternburst_domains(
    text: str,
    *,
    source_file: str,
    selected_pattern: str,
) -> tuple[list[str], list[str]]:
    """Compatibility wrapper returning only the selected burst's bindings."""

    macro_domains, procedure_domains, _selected = _extract_patternburst_context(
        text,
        source_file=source_file,
        selected_pattern=selected_pattern,
    )
    return macro_domains, procedure_domains


def _find_matching_brace(text: str, opening_index: int) -> int:
    """Find a block terminator without counting quoted text or comments."""

    depth = 0
    quote: str | None = None
    escaped = False
    block_comment = False
    index = opening_index
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
                continue
            index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and next_char == "/":
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _strip_vector_comments(block: str) -> str:
    out = StringIO()
    for index, line in enumerate(_iter_source_lines(block)):
        if index:
            out.write("\n")
        out.write(_strip_line_comment(line).rstrip())
    return out.getvalue()


def _iter_source_lines(text: str) -> Iterator[str]:
    """The splitlines() contract without an array of every source line."""

    start = 0
    for match in re.finditer(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]", text):
        yield text[start:match.start()]
        start = match.end()
    if start < len(text):
        yield text[start:]


def _vector_chars(target: str, data: str, signal_index: dict[str, int],
                  group_map: dict[str, tuple[str, ...]]) -> str:
    width = len(group_map[target]) if target in group_map else 1 if target in signal_index else None
    chars = decode_vector_data(data, max_width=width)
    if width is not None and len(chars) != width:
        raise StilError("ERR_BAD_VECTOR_WIDTH", f"vector target {target!r} has width {len(chars)}; expected {width}")
    return chars


def _rewrite_pattern_vectors(
    pattern_block: str,
    signal_index: dict[str, int],
    group_map: dict[str, tuple[str, ...]],
    ignored_signals: set[str],
    drop_compare_signals: set[str],
) -> str:
    if not ignored_signals and not drop_compare_signals:
        return pattern_block
    clean_text = _strip_comments(pattern_block, source_file="vector rewrite")
    output = StringIO()
    cursor = 0
    for match in _VECTOR_STMT_RE.finditer(clean_text):
        target, chars, _suffix = match.groups()
        chars_list = list(_vector_chars(target, chars, signal_index, group_map))
        if target in group_map:
            group_signals = group_map[target]
            if len(chars_list) != len(group_signals):
                raise StilError(
                    "ERR_BAD_VECTOR_WIDTH",
                    f"vector target {target!r} has width {len(chars_list)}; its SignalGroup has {len(group_signals)} signals",
                )
            for idx, signal_name in enumerate(group_signals):
                if signal_name in ignored_signals:
                    chars_list[idx] = "T"
                elif signal_name in drop_compare_signals and chars_list[idx] in ("H", "L", "h", "l", "T"):
                    chars_list[idx] = "T"
        elif target in signal_index and target in ignored_signals:
            chars_list[0] = "T"
        elif target in signal_index and target in drop_compare_signals and chars_list[0] in ("H", "L", "h", "l", "T"):
            chars_list[0] = "T"
        chars_start, chars_end = match.span(2)
        output.write(pattern_block[cursor:chars_start])
        output.write("".join(chars_list))
        cursor = chars_end
    output.write(pattern_block[cursor:])
    return output.getvalue()


def _shift_pattern_compare_vectors(
    pattern_block: str,
    signal_index: dict[str, int],
    group_map: dict[str, tuple[str, ...]],
    shift_compare_signals: set[str],
    shift_compare_vectors: int,
) -> str:
    if shift_compare_vectors <= 0 or not shift_compare_signals:
        return pattern_block
    # Retain just the selected characters in the delay window for each target,
    # not every line, every pin, and two full arrays per shifted pin.
    positions: dict[str, tuple[int, ...]] = {}
    histories: dict[str, deque[str]] = {}
    output = StringIO()
    clean_text = _strip_comments(pattern_block, source_file="compare shift")
    cursor = 0
    for match in _VECTOR_STMT_RE.finditer(clean_text):
        target, data, _suffix = match.groups()
        if target not in positions:
            members = group_map.get(target, (target,) if target in signal_index else ())
            positions[target] = tuple(index for index, name in enumerate(members) if name in shift_compare_signals)
        selected = positions[target]
        if not selected:
            continue
        chars = list(_vector_chars(target, data, signal_index, group_map))
        history = histories.setdefault(target, deque())
        original = "".join(chars[index] for index in selected)
        prior = history.popleft() if len(history) >= shift_compare_vectors else "T" * len(selected)
        history.append(original)
        for index, char in zip(selected, prior):
            chars[index] = char if char in ("H", "L", "h", "l", "T") else "T"
        begin, end = match.span(2)
        output.write(pattern_block[cursor:begin])
        output.write("".join(chars))
        cursor = end
    output.write(pattern_block[cursor:])
    return output.getvalue()
