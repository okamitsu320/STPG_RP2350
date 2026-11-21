"""Build independent host-only packages for explicit rate-time variants.

Phase 8 treats each timing condition as a separate artifact.  A variant
overrides one or more WaveformTable periods and scales that table's event
positions by the same ratio, preserving vector characters and event order.
The source STIL is never edited in place.  Each generated package receives a
fresh wait schedule derived from its own binary; no wait is inserted by this
module.
"""

from __future__ import annotations

from .source_artifacts import read_source_text

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .pattern_package import (
    PatternPackageError,
    PatternPackageOptions,
    build_pattern_package,
)
from .rate_adapt import (
    _EVENT_TIME_RE,
    _PERIOD_LINE_RE,
    _split_line_comment,
    _table_ranges,
    _time_text_to_seconds,
)
from .parser import parse_stil_text
from .resolve import _time_to_ticks
from stpg.constants import STPG_DEFAULT_TICK_HZ
from .wait_schedule import DEFAULT_CHUNK_EVENT_CAPACITY, DEFAULT_RING_SLOT_COUNT


RATE_SWEEP_KIND = "stpg_stil_rate_time_sweep"
RATE_SWEEP_SCHEMA_VERSION = 1
_VARIANT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TIME_LITERAL_RE = re.compile(r"^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:ps|ns|us|ms|s)$")


class RateSweepError(ValueError):
    """Raised when an explicit rate-time sweep cannot be built safely."""


@dataclass(frozen=True)
class RateSweepVariant:
    """One named set of WaveformTable period overrides."""

    name: str
    overrides: tuple[tuple[str, str], ...]


def _identity(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RateSweepError(f"not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}


def _format_ns(seconds: Decimal) -> str:
    value = seconds * Decimal("1e9")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return f"{rendered}ns"


def parse_variant_spec(value: str) -> RateSweepVariant:
    """Parse ``NAME=TABLE:PERIOD[,TABLE:PERIOD...]``."""

    name, separator, assignments = value.partition("=")
    name = name.strip()
    if not separator or not _VARIANT_NAME_RE.fullmatch(name):
        raise RateSweepError(
            "variant must use NAME=TABLE:PERIOD[,TABLE:PERIOD...] with a simple NAME"
        )
    parsed: list[tuple[str, str]] = []
    seen_tables: set[str] = set()
    for raw_assignment in assignments.split(","):
        table, table_separator, period = raw_assignment.partition(":")
        table = table.strip()
        period = period.strip()
        if not table_separator or not table or not _TIME_LITERAL_RE.fullmatch(period):
            raise RateSweepError(
                f"invalid variant assignment {raw_assignment!r}; expected TABLE:PERIOD"
            )
        if table in seen_tables:
            raise RateSweepError(f"variant {name!r} overrides table more than once: {table}")
        try:
            seconds = _time_text_to_seconds(period)
        except Exception as exc:
            raise RateSweepError(f"invalid period {period!r}") from exc
        if not seconds.is_finite() or seconds <= 0:
            raise RateSweepError(f"period must be finite and positive: {period!r}")
        seen_tables.add(table)
        parsed.append((table, period))
    if not parsed:
        raise RateSweepError(f"variant {name!r} has no table override")
    return RateSweepVariant(name=name, overrides=tuple(parsed))


def _table_event_count(table: Any) -> int:
    return sum(len(char_decl.events) for waveform in table.waveforms for char_decl in waveform.chars)


def _validate_source_timing(table: Any, *, table_name: str) -> tuple[Decimal, int]:
    try:
        period = _time_text_to_seconds(table.period_text)
    except Exception as exc:
        raise RateSweepError(f"WaveformTable {table_name} has invalid Period {table.period_text!r}") from exc
    if not period.is_finite() or period <= 0:
        raise RateSweepError(f"WaveformTable {table_name} period must be positive")
    event_count = _table_event_count(table)
    for waveform in table.waveforms:
        for char_decl in waveform.chars:
            for event in char_decl.events:
                event_time = _time_text_to_seconds(event.time_text)
                if event_time < 0 or event_time >= period:
                    raise RateSweepError(
                        f"WaveformTable {table_name} event {event.time_text} is outside its source period"
                    )
    return period, event_count


def derive_rate_variant(
    input_path: Path,
    output_path: Path,
    variant: RateSweepVariant,
    *,
    tick_hz: int,
) -> dict[str, Any]:
    """Write one derived STIL timing variant and its host-only report."""

    if tick_hz <= 0:
        raise RateSweepError("tick_hz must be positive")
    source_text = read_source_text(input_path)
    document = parse_stil_text(source_text, source_file=str(input_path))
    tables = {table.name: table for table in document.waveform_tables}
    missing = sorted(table for table, _period in variant.overrides if table not in tables)
    if missing:
        raise RateSweepError(f"WaveformTable not found: {', '.join(missing)}")

    plans: list[dict[str, Any]] = []
    for table_name, target_period_text in variant.overrides:
        table = tables[table_name]
        source_period, event_count = _validate_source_timing(table, table_name=table_name)
        target_period = _time_text_to_seconds(target_period_text)
        ratio = target_period / source_period
        target_ticks, target_exact = _time_to_ticks(target_period_text, tick_hz)
        if target_ticks <= 0:
            raise RateSweepError(f"target period quantizes to zero ticks: {table_name}={target_period_text}")
        plans.append(
            {
                "name": table_name,
                "original_period": table.period_text,
                "original_period_seconds": format(source_period, "f"),
                "target_period": target_period_text,
                "target_period_seconds": format(target_period, "f"),
                "target_period_ticks": target_ticks,
                "target_period_exact": target_exact,
                "scale_ratio": format(ratio, "f"),
                "event_count": event_count,
            }
        )

    lines = source_text.splitlines(keepends=True)
    ranges = _table_ranges(lines, {name for name, _period in variant.overrides})
    changed_event_count = 0
    changed_period_count = 0
    for plan in plans:
        table_name = plan["name"]
        target_period = _time_text_to_seconds(plan["target_period"])
        source_period = _time_text_to_seconds(plan["original_period"])
        ratio = target_period / source_period
        start, end = ranges[table_name]
        period_seen = False
        table_event_matches = 0
        for index in range(start + 1, end):
            code, comment = _split_line_comment(lines[index])
            period_match = _PERIOD_LINE_RE.match(code.rstrip("\r\n"))
            if period_match:
                if period_seen:
                    raise RateSweepError(f"WaveformTable {table_name} has multiple Period declarations")
                code_body = code.rstrip("\r\n")
                newline = code[len(code_body) :]
                lines[index] = (
                    f"{period_match.group(1)}{plan['target_period']}{period_match.group(3)}"
                    f"{newline}{comment}"
                )
                period_seen = True
                changed_period_count += 1
                continue

            def replace_event(match: re.Match[str]) -> str:
                nonlocal table_event_matches
                table_event_matches += 1
                raw_time = f"{match.group('time')}{match.group('unit') or ''}"
                source_seconds = _time_text_to_seconds(raw_time)
                return (
                    f"{match.group('prefix')}{_format_ns(source_seconds * ratio)}"
                    f"{match.group('gap')}{match.group('action')}"
                )

            lines[index] = _EVENT_TIME_RE.sub(replace_event, code) + comment
        if not period_seen:
            raise RateSweepError(f"WaveformTable {table_name} has no Period declaration")
        if table_event_matches != plan["event_count"]:
            raise RateSweepError(
                f"WaveformTable {table_name} lexical event count {table_event_matches} "
                f"does not match parsed count {plan['event_count']}"
            )
        changed_event_count += table_event_matches

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(lines), encoding="utf-8")
    # Parse the derived source before any package is built, so malformed
    # lexical replacement cannot be mistaken for a package failure.
    parse_stil_text(output_path.read_text(encoding="utf-8"), source_file=str(output_path))
    return {
        "kind": RATE_SWEEP_KIND + ".variant",
        "schema_version": RATE_SWEEP_SCHEMA_VERSION,
        "status": "DERIVED",
        "variant": variant.name,
        "input": _identity(input_path),
        "output": _identity(output_path),
        "waveform_tables": plans,
        "changed_period_count": changed_period_count,
        "changed_event_count": changed_event_count,
        "semantics": {
            "vector_characters_preserved": True,
            "event_order_preserved": True,
            "selected_table_event_times_scaled_with_period": True,
            "hidden_wait_insertion": False,
            "physical_execution_authorized": False,
        },
    }


def _prepare_output_dir(path: Path) -> None:
    if path.exists() and not path.is_dir():
        raise RateSweepError(f"output path is not a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise RateSweepError(f"output directory must be empty: {path}")


def _compact_package_result(package_result: dict[str, Any]) -> dict[str, Any]:
    manifest = package_result["manifest"]
    artifacts = manifest.get("artifacts", {})
    return {
        "status": package_result.get("status"),
        "package_dir": package_result.get("package_dir"),
        "manifest": {
            "kind": manifest.get("kind"),
            "schema_version": manifest.get("schema_version"),
            "artifact_count": len(artifacts) if isinstance(artifacts, dict) else 0,
            "artifacts": artifacts,
        },
        "verification": package_result.get("verification", {}).get("status"),
        "physical_execution_authorized": False,
    }


def build_rate_sweep(
    *,
    input_path: Path,
    output_dir: Path,
    report_path: Path,
    variants: tuple[RateSweepVariant, ...],
    mode: str = "auto",
    tick_hz: int = STPG_DEFAULT_TICK_HZ,
    pin_map_path: Path | None = None,
    pattern_burst: str | None = None,
    pattern: str | None = None,
    strict: bool = False,
    allow_approx: bool = False,
    prefer_raw: bool = False,
    until_vector: int | None = None,
    vector_range: tuple[int, int] | None = None,
    runtime_profile: str = "psram-compare-sample-streaming",
    max_rates: int | None = None,
    chunk_event_capacity: int = DEFAULT_CHUNK_EVENT_CAPACITY,
    ring_slot_count: int = DEFAULT_RING_SLOT_COUNT,
    service_time_us: float | None = None,
    service_time_source: str = "",
    safety_margin_us: float = 0.0,
    max_wait_us: float | None = None,
    max_total_wait_us: float | None = None,
    max_wait_count: int | None = None,
    pattern_time_limit_us: float | None = None,
) -> dict[str, Any]:
    """Build all variants and write one matrix report."""

    if not variants:
        raise RateSweepError("at least one --variant is required")
    names = [variant.name for variant in variants]
    if len(set(names)) != len(names):
        raise RateSweepError("variant names must be unique")
    _prepare_output_dir(output_dir)
    source_identity = _identity(input_path)
    result_variants: list[dict[str, Any]] = []
    for variant in variants:
        variant_dir = output_dir / variant.name
        variant_dir.mkdir()
        derived_path = variant_dir / "source.rate-variant.stil"
        timing_report_path = variant_dir / "rate-variant.json"
        package_dir = variant_dir / "package"
        try:
            timing_report = derive_rate_variant(
                input_path,
                derived_path,
                variant,
                tick_hz=tick_hz,
            )
            package_result = build_pattern_package(
                PatternPackageOptions(
                    input_path=derived_path,
                    output_dir=package_dir,
                    mode=mode,
                    tick_hz=tick_hz,
                    pin_map_path=pin_map_path,
                    pattern_burst=pattern_burst,
                    pattern=pattern,
                    strict=strict,
                    allow_approx=allow_approx,
                    prefer_raw=prefer_raw,
                    until_vector=until_vector,
                    vector_range=vector_range,
                    runtime_profile=runtime_profile,
                    max_rates=max_rates,
                    chunk_event_capacity=chunk_event_capacity,
                    ring_slot_count=ring_slot_count,
                    service_time_us=service_time_us,
                    service_time_source=service_time_source,
                    safety_margin_us=safety_margin_us,
                    max_wait_us=max_wait_us,
                    max_total_wait_us=max_total_wait_us,
                    max_wait_count=max_wait_count,
                    pattern_time_limit_us=pattern_time_limit_us,
                )
            )
        except (OSError, PatternPackageError, RateSweepError, ValueError) as exc:
            result_variants.append(
                {
                    "name": variant.name,
                    "status": "REJECT",
                    "overrides": [{"waveform_table": table, "period": period} for table, period in variant.overrides],
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                    "physical_execution_authorized": False,
                }
            )
            continue

        timing_report_path.write_text(
            json.dumps(timing_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result_variants.append(
            {
                "name": variant.name,
                "status": "VERIFIED",
                "overrides": [{"waveform_table": table, "period": period} for table, period in variant.overrides],
                "timing_report": _identity(timing_report_path),
                "derived_stil": _identity(derived_path),
                "package": _compact_package_result(package_result),
                "wait_schedule": {
                    "recalculated_from_variant_binary": True,
                    "hidden_wait_insertion": False,
                },
                "physical_execution_authorized": False,
            }
        )

    status = "COMPLETE" if all(item["status"] == "VERIFIED" for item in result_variants) else "REJECT"
    report = {
        "kind": RATE_SWEEP_KIND,
        "schema_version": RATE_SWEEP_SCHEMA_VERSION,
        "status": status,
        "source": source_identity,
        "input_contract": {
            "mode": mode,
            "tick_hz": tick_hz,
            "max_rates": max_rates,
            "runtime_profile": runtime_profile,
            "physical_execution_authorized": False,
        },
        "variants": result_variants,
        "policies": {
            "independent_artifact_per_variant": True,
            "wait_schedule_recalculated_per_variant": True,
            "hidden_wait_insertion": False,
            "firmware_change": False,
            "physical_execution_authorized": False,
        },
        "limitations": [
            "period changes are host/compiler timing variants, not AC-equivalent source replacements",
            "each package is host-only and requires separate physical approval",
            "wait acceptance is a bounded model and does not establish physical Tfill",
            "the analyzer does not measure timing or throughput",
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report
