"""Derive an STPG-safe timing view of a preprocessed STIL source.

The source STIL is never modified in place.  The adapter changes only the
selected WaveformTable periods and event positions, preserving event order
and vector characters.  Its ``stpg-streaming-fastest`` profile computes the
shortest period whose event-group gaps satisfy the streaming PIO overhead
for every event declared by the selected table.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from stpg.pattern import (
    STREAMING_POC_WAIT_OVERHEAD_NO_SAMPLE,
    STREAMING_POC_WAIT_OVERHEAD_SAMPLE,
)

from .parser import parse_stil_text
from .resolve import _time_text_to_seconds, _time_to_ticks
from stpg.constants import STPG_DEFAULT_TICK_HZ


RATE_ADAPT_KIND = "stpg_stil_rate_adaptation"
RATE_ADAPT_SCHEMA_VERSION = 1
SUPPORTED_PROFILE = "stpg-streaming-fastest"

_PERIOD_LINE_RE = re.compile(r"^(\s*Period\s+')([^']+)('\s*;\s*)$")
_EVENT_TIME_RE = re.compile(
    r"(?P<prefix>^|[,\s])(?P<time>[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
    r"(?P<unit>ms|us|ns|ps|s)?(?P<gap>\s+)"
    r"(?P<action>[A-Za-z][A-Za-z0-9_]*)"
)


class RateAdaptError(ValueError):
    """Raised when an explicit STPG timing adaptation is unsafe or invalid."""


@dataclass(frozen=True)
class RateAdaptOptions:
    input_path: Path
    output_path: Path
    report_path: Path
    waveform_tables: tuple[str, ...]
    tick_hz: int = STPG_DEFAULT_TICK_HZ
    profile: str = SUPPORTED_PROFILE
    min_gap_ticks: int | None = None


def _identity(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RateAdaptError(f"not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}


def _split_line_comment(line: str) -> tuple[str, str]:
    """Return STIL code and an untouched ``//`` comment suffix."""

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
        elif char == "/" and index + 1 < len(line) and line[index + 1] == "/":
            return line[:index], line[index:]
    return line, ""


def _strip_comment_for_braces(line: str) -> str:
    return _split_line_comment(line)[0]


def _brace_delta(line: str) -> int:
    clean = _strip_comment_for_braces(line)
    quote: str | None = None
    escaped = False
    delta = 0
    for char in clean:
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
            delta += 1
        elif char == "}":
            delta -= 1
    return delta


def _table_ranges(lines: list[str], table_names: set[str]) -> dict[str, tuple[int, int]]:
    pattern = re.compile(r'^\s*WaveformTable\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\s*\{')
    ranges: dict[str, tuple[int, int]] = {}
    for start, line in enumerate(lines):
        match = pattern.match(_strip_comment_for_braces(line))
        if not match:
            continue
        name = match.group(1) or match.group(2)
        if name not in table_names:
            continue
        depth = 0
        end = None
        for index in range(start, len(lines)):
            depth += _brace_delta(lines[index])
            if index > start and depth == 0:
                end = index
                break
        if end is None:
            raise RateAdaptError(f"WaveformTable {name} has no closing brace")
        if name in ranges:
            raise RateAdaptError(f"WaveformTable {name} is declared more than once")
        ranges[name] = (start, end)
    missing = sorted(table_names - set(ranges))
    if missing:
        raise RateAdaptError(f"WaveformTable not found: {', '.join(missing)}")
    return ranges


def _event_action_kind(action_name: str) -> str:
    lower = action_name.lower()
    if lower.startswith("compare"):
        return "sample"
    if lower.startswith("drive") or lower.startswith("dir"):
        return "drive"
    return "other"


def _format_time_ns(ticks: int, tick_hz: int) -> str:
    if ticks == 0:
        return "0ns"
    value = (Decimal(ticks) * Decimal("1e9")) / Decimal(tick_hz)
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return f"{rendered}ns"


def _seconds_to_ticks(seconds: Decimal, tick_hz: int) -> int:
    return int((seconds * Decimal(tick_hz)).to_integral_value(rounding=ROUND_HALF_UP))


def _replacement_time(match: re.Match[str], mapping: dict[Decimal, int], tick_hz: int) -> str:
    raw = f"{match.group('time')}{match.group('unit') or ''}"
    source_time = _time_text_to_seconds(raw)
    if source_time not in mapping:
        raise RateAdaptError(f"event time {raw} was not present in adaptation map")
    return (
        f"{match.group('prefix')}{_format_time_ns(mapping[source_time], tick_hz)}"
        f"{match.group('gap')}{match.group('action')}"
    )


def _plan_table(
    table: Any,
    *,
    tick_hz: int,
    min_gap_ticks: int | None = None,
) -> dict[str, Any]:
    # Keep sub-tick source times distinct while planning.  Quantizing first
    # would collapse DELAYSCAN's 0 ns and 3 ns compare points and silently
    # change their order before the adaptation has a chance to make it legal.
    groups: dict[Decimal, dict[str, Any]] = {}
    for waveform in table.waveforms:
        for char_decl in waveform.chars:
            for event in char_decl.events:
                source_time = _time_text_to_seconds(event.time_text)
                action_kind = _event_action_kind(event.action_name)
                if action_kind == "other":
                    raise RateAdaptError(
                        f"WaveformTable {table.name} has unsupported timing action "
                        f"{event.action_name!r}; adaptation would risk underestimating the required gap"
                    )
                group = groups.setdefault(
                    source_time,
                    {"sample": False, "drive": False, "other": False, "event_count": 0},
                )
                group[action_kind] = True
                group["event_count"] += 1
    if not groups:
        raise RateAdaptError(f"WaveformTable {table.name} has no timed events")
    old_times = sorted(groups)
    if min_gap_ticks is not None:
        if min_gap_ticks <= 0:
            raise RateAdaptError("min_gap_ticks must be positive")
        requirements = [min_gap_ticks] * len(old_times)
        gap_policy = "explicit_uniform_min_gap"
    else:
        requirements = [
            STREAMING_POC_WAIT_OVERHEAD_SAMPLE if groups[tick]["sample"]
            else STREAMING_POC_WAIT_OVERHEAD_NO_SAMPLE
            for tick in old_times
        ]
        gap_policy = "streaming_action_overhead"
    new_times: list[int] = [0]
    for requirement in requirements[1:]:
        new_times.append(new_times[-1] + int(requirement))
    new_period = new_times[-1] + int(requirements[0])
    mapping = dict(zip(old_times, new_times))
    return {
        "name": table.name,
        "original_period": table.period_text,
        "original_period_ticks": int(_time_to_ticks(table.period_text, tick_hz)[0]),
        "adapted_period": _format_time_ns(new_period, tick_hz),
        "adapted_period_ticks": new_period,
        "event_group_count": len(old_times),
        "event_count": sum(int(groups[tick]["event_count"]) for tick in old_times),
        "gap_policy": gap_policy,
        "explicit_min_gap_ticks": min_gap_ticks,
        "event_group_mapping": [
            {
                "original_time_ticks": _seconds_to_ticks(old, tick_hz),
                "original_time_seconds": format(old, "f"),
                "adapted_time_ticks": new,
                "required_gap_ticks": requirement,
                "has_sample": bool(groups[old]["sample"]),
                "has_drive": bool(groups[old]["drive"]),
                "event_count": int(groups[old]["event_count"]),
            }
            for old, new, requirement in zip(old_times, new_times, requirements)
        ],
        "mapping_times": {
            format(source_time, "f"): adapted_ticks
            for source_time, adapted_ticks in mapping.items()
        },
        "source_semantics": "event order and vector characters preserved; absolute event timing changed",
    }


def adapt_stil(options: RateAdaptOptions) -> dict[str, Any]:
    if options.profile != SUPPORTED_PROFILE:
        raise RateAdaptError(f"unsupported rate adaptation profile: {options.profile}")
    if not options.waveform_tables:
        raise RateAdaptError("at least one --waveform-table is required")
    if options.tick_hz <= 0:
        raise RateAdaptError("tick_hz must be positive")

    source_text = options.input_path.read_text(encoding="utf-8")
    document = parse_stil_text(source_text, source_file=str(options.input_path))
    wanted = set(options.waveform_tables)
    tables = {table.name: table for table in document.waveform_tables}
    missing = sorted(wanted - set(tables))
    if missing:
        raise RateAdaptError(f"WaveformTable not found: {', '.join(missing)}")
    plans = {
        name: _plan_table(
            tables[name],
            tick_hz=options.tick_hz,
            min_gap_ticks=options.min_gap_ticks,
        )
        for name in options.waveform_tables
    }
    lines = source_text.splitlines(keepends=True)
    ranges = _table_ranges(lines, wanted)

    changed_event_line_count = 0
    changed_period_count = 0
    for name in options.waveform_tables:
        start, end = ranges[name]
        plan = plans[name]
        mapping = {
            Decimal(key): int(value)
            for key, value in plan["mapping_times"].items()
        }
        period_seen = False
        for index in range(start + 1, end):
            code, comment = _split_line_comment(lines[index])
            period_match = _PERIOD_LINE_RE.match(code.rstrip("\r\n"))
            if period_match:
                if period_seen:
                    raise RateAdaptError(f"WaveformTable {name} has multiple Period declarations")
                code_body = code.rstrip("\r\n")
                newline = code[len(code_body) :]
                lines[index] = (
                    f"{period_match.group(1)}{plan['adapted_period']}{period_match.group(3)}{newline}{comment}"
                )
                period_seen = True
                changed_period_count += 1
                continue
            before = lines[index]
            adapted_code = _EVENT_TIME_RE.sub(
                lambda match: _replacement_time(match, mapping, options.tick_hz),
                code,
            )
            lines[index] = adapted_code + comment
            if lines[index] != before:
                changed_event_line_count += 1
        if not period_seen:
            raise RateAdaptError(f"WaveformTable {name} has no Period declaration")

    options.output_path.parent.mkdir(parents=True, exist_ok=True)
    options.output_path.write_text("".join(lines), encoding="utf-8")
    report = {
        "kind": RATE_ADAPT_KIND,
        "schema_version": RATE_ADAPT_SCHEMA_VERSION,
        "status": "DERIVED",
        "profile": options.profile,
        "tick_hz": options.tick_hz,
        "min_gap_ticks": options.min_gap_ticks,
        "input": _identity(options.input_path),
        "output": _identity(options.output_path),
        "waveform_tables": [plans[name] for name in options.waveform_tables],
        "changed_period_count": changed_period_count,
        "changed_event_line_count": changed_event_line_count,
        "physical_execution_authorized": False,
        "device_io_performed": False,
        "limitations": [
            "this is a derived STPG timing view, not an AC-equivalent source replacement",
            "streaming overhead is a format/runtime bound, not measured refill or timing performance",
            "pattern capacity and functional compare acceptance remain separate checks",
            "the original STIL file is never modified in place",
        ],
    }
    options.report_path.parent.mkdir(parents=True, exist_ok=True)
    options.report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report
