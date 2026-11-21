"""Prepare host-only segmented STIL packages.

The segment planner is deliberately separate from the physical hold/resume
runner.  It compiles each inclusive global-vector range into an independent
package, records the terminal drive state as the next segment's entry state,
and emits a manifest that can later be consumed by ``stil.segmented``.

Rate-time variants are applied before slicing.  Consequently every variant
gets its own segment packages, maps, wait records, and hashes; no timing or
wait state is copied between variants.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path, PurePath, PureWindowsPath
import shutil
from typing import Any

from stpg.constants import ERR_BAD_SIZE, ERR_PATTERN_INVALID, STPG_DEFAULT_TICK_HZ
from stpg.pattern import (PatternValidationError, rate_vector_structure_metrics,
                         unpack_rate_vector_full, rate_vector_decode_scope)
from stpg import pattern as pattern_format

from .device_precheck import inspect_pattern_requirements
from .errors import StilError
from .backend_rate import _is_loop_block_capacity_error
from .hold_state import derive_terminal_hold
from .markers import resolve_marker_addresses
from .pattern_package import (
    PatternPackageError,
    PatternPackageOptions,
    build_pattern_package,
    derive_pattern_package_with_wait,
    _json_write,
    _manifest_for_package,
    verify_pattern_package,
)
from .rate_sweep import RateSweepError, RateSweepVariant, derive_rate_variant
from .parser import parse_stil_file
from .prepared_source import PreparedStilSource
from .resolve import load_pin_map, resolve_document
from .runtime_wait_model import (
    _RuntimeModelCache,
    RuntimeWaitModelError,
    build_runtime_model,
    build_runtime_chunks,
    iter_runtime_events,
    _every_boundary_wait_ticks,
    _ticks_from_us,
    plan_waits,
    runtime_chunk_source_contexts,
)
from .runtime_capacity import DEFAULT_RUNTIME_PROFILE
from .runtime_capacity import assess_runtime_capacity, get_runtime_capacity_profile
from .pipeline import CompileOptions, _compile_stil_binary_probe, _compile_stil_wait_probe
from .wait_binary import inspect_wait_schedule_encoding
from .wait_acceptance import evaluate_wait_schedule, _has_loop_provenance
from .json_artifacts import read_json_artifact, resolve_json_artifact, write_json_artifact
from .wait_schedule import (
    DEFAULT_CHUNK_EVENT_CAPACITY,
    DEFAULT_RING_SLOT_COUNT,
    FORBIDDEN_STIL_RATE_MATCH_NEXT_CHUNK_FIRST_EVENT,
    validate_wait_schedule,
)


SEGMENTED_PACKAGE_KIND = "stpg_stil_segmented_package"
SEGMENTED_PACKAGE_SCHEMA_VERSION = 1


class SegmentedPackageError(ValueError):
    """Raised when a segmented host-only package cannot be prepared."""


def _relative_posix(child: PurePath, base: PurePath, *, field: str) -> str:
    """Serialize generated artifact references independently of the host OS.

    This checks writer syntax, not symlink confinement or SDK admission. Native
    absolute paths in local provenance reports deliberately remain unchanged.
    """
    try:
        relative = child.relative_to(base)
    except ValueError as exc:
        raise SegmentedPackageError(
            f"{field}: artifact path is outside its package root: {str(child)!r}"
        ) from exc
    value = relative.as_posix()
    if (not value or "\\" in value or "\x00" in value or relative.is_absolute()
            or PureWindowsPath(value).drive
            or any(part in {".", "..", ""} for part in value.split("/"))):
        raise SegmentedPackageError(
            f"{field}: path must be a non-empty confined relative POSIX path: {value!r}"
        )
    return value


def _normalize_stil_rate_units(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    result: list[str] = []
    for value in values or ():
        if not isinstance(value, str) or not value.strip():
            raise SegmentedPackageError(
                "forbidden STIL rate units must be non-empty WaveformTable names"
            )
        result.append(value.strip())
    return tuple(sorted(set(result)))


def _resolve_segment_marker_specs(
    input_path: Path,
    *,
    pin_map_path: Path | None,
    pattern_burst: str | None,
    pattern: str | None,
    marker_specs: tuple[dict[str, Any], ...],
    prepared_source: PreparedStilSource | None = None,
) -> tuple[dict[str, Any], ...]:
    """Resolve STIL line markers once against the unsliced source pattern."""

    if not marker_specs or not any(
        "line" in marker or "clear_line" in marker for marker in marker_specs
    ):
        return tuple(dict(marker) for marker in marker_specs)
    if prepared_source is not None:
        resolved = prepared_source.resolve()
    else:
        document = parse_stil_file(input_path)
        pin_map = load_pin_map(pin_map_path, document=document)
        resolved = resolve_document(document, pin_map=pin_map,
                                    pattern_burst_name=pattern_burst, pattern_name=pattern)
    line_to_vectors: dict[int, list[int]] = {}
    for vector in resolved.vectors:
        line_to_vectors.setdefault(vector.location.line, []).append(vector.top_level_index)
    if prepared_source is not None:
        prepared_source.clear_resolution()
    return resolve_marker_addresses(marker_specs, line_to_vectors=line_to_vectors)


def _marker_intersects_segment(
    marker: dict[str, Any], *, global_start: int, global_end: int
) -> bool:
    start = int(marker["vector"])
    if marker["mode"] == "pulse":
        # Include the first LOW vector, even when it starts the next segment.
        end = start + int(marker.get("width", 1))
    else:
        # The clear vector itself must be included so an independently
        # uploaded segment can emit the falling edge at its first vector.
        end = int(marker["clear_vector"])
    return start <= global_end and end >= global_start


def _identity(path: Path) -> dict[str, Any]:
    if path.suffix in {".json", ".gz"}:
        path = resolve_json_artifact(path)
    if not path.is_file():
        raise SegmentedPackageError(f"not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}


def _parse_boundary(value: int | str) -> int:
    try:
        boundary = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise SegmentedPackageError(f"invalid inclusive segment boundary: {value!r}") from exc
    if boundary < 0:
        raise SegmentedPackageError(f"segment boundary must be non-negative: {boundary}")
    return boundary


def build_segment_ranges(
    total_vectors: int,
    *,
    segment_vector_count: int | None = None,
    split_at: tuple[int, ...] = (),
) -> tuple[tuple[int, int], ...]:
    """Return contiguous inclusive ``(start, end)`` global vector ranges."""

    if total_vectors < 2:
        raise SegmentedPackageError("segmented packaging requires at least two source vectors")
    if (segment_vector_count is None) == (not split_at):
        raise SegmentedPackageError("provide exactly one of segment_vector_count or split_at")

    if segment_vector_count is not None:
        if segment_vector_count < 1:
            raise SegmentedPackageError("segment_vector_count must be positive")
        ends = list(range(segment_vector_count - 1, total_vectors - 1, segment_vector_count))
        if not ends or ends[-1] != total_vectors - 1:
            ends.append(total_vectors - 1)
    else:
        ends = [_parse_boundary(value) for value in split_at]
        if ends != sorted(set(ends)):
            raise SegmentedPackageError("split_at boundaries must be strictly increasing")
        if any(end >= total_vectors - 1 for end in ends):
            raise SegmentedPackageError(
                f"split_at boundaries must be before the final vector ({total_vectors - 1})"
            )
        ends.append(total_vectors - 1)

    ranges: list[tuple[int, int]] = []
    start = 0
    for end in ends:
        if end < start:
            raise SegmentedPackageError(f"segment range is not contiguous: {start}:{end}")
        ranges.append((start, end))
        start = end + 1
    if len(ranges) < 2:
        raise SegmentedPackageError("segmented packaging requires at least two segments")
    return tuple(ranges)


_CAPACITY_PROBE_ERRORS = {
    "rate vector exceeds PSRAM pattern region": ERR_BAD_SIZE,
    "wide rate vector exceeds PSRAM pattern region": ERR_BAD_SIZE,
    "rate vector exceeds the runtime event limit": ERR_BAD_SIZE,
    "rate vector exceeds the PSRAM result region": ERR_BAD_SIZE,
    "rate vector exceeds the runtime compare limit": ERR_BAD_SIZE,
    "event_count out of range": ERR_PATTERN_INVALID,
    "macro-stream exceeds Stage A size": ERR_BAD_SIZE,
    "macro-stream pattern exceeds Stage A size": ERR_BAD_SIZE,
}
_PRESERVED_RATE_FAILURE = (
    "preserved loop could not be compiled by the rate-vector backend; refusing raw-event fallback"
)


def _capacity_failure(exc: BaseException) -> bool:
    """Unwrap only the known auto/preserve wrapper, never arbitrary causes.

    The encoder's exact message, exception class and numeric/symbolic code must match. Parser,
    timing, I/O and unknown wrappers are not a reason to shorten a range.
    Explicit causes only; bounded traversal rejects cycles/missing causes.
    """
    seen: set[int] = set()
    for _ in range(8):
        if id(exc) in seen:
            return False
        seen.add(id(exc))
        if isinstance(exc, PatternValidationError):
            return (exc.__cause__ is None and str(exc) in _CAPACITY_PROBE_ERRORS
                    and exc.error_code == _CAPACITY_PROBE_ERRORS[str(exc)])
        if (isinstance(exc, StilError) and exc.__cause__ is None
                and _is_loop_block_capacity_error(exc.code, str(exc))):
            return True
        if (not isinstance(exc, StilError) or exc.code != "ERR_LOOP_BACKEND_UNSUPPORTED"
                or str(exc) != _PRESERVED_RATE_FAILURE or exc.__cause__ is None):
            return False
        exc = exc.__cause__
    return False


def _probe_every_boundary_encoding(
    payload: bytes, *, rate_waveform_tables: tuple[str, ...], chunk_event_capacity: int,
    max_wait_us: float, forbidden_stil_rate_units: tuple[str, ...], max_rates: int | None,
) -> dict[str, Any]:
    """Exact local placement/encoding cost, not service-time acceptance."""
    cache = _RuntimeModelCache()
    header, rates, _slots, _entries, _loops = cache.decode(payload)
    if len(rate_waveform_tables) != len(rates):
        raise SegmentedPackageError("wait probe source-rate binding does not match the emitted table")
    _total, chunks = build_runtime_chunks(payload, chunk_event_capacity=chunk_event_capacity, _model_cache=cache)
    forbidden_chunks: set[int] = set()
    if forbidden_stil_rate_units:
        first_events = {chunk.event_start: chunk.chunk_index for chunk in chunks}
        for event in iter_runtime_events(payload, _model_cache=cache):
            index = first_events.get(event.event_index)
            if index is not None and rate_waveform_tables[event.rate_id] in forbidden_stil_rate_units:
                forbidden_chunks.add(index)
    chunk_dicts = [chunk.to_dict(int(header.tick_hz)) for chunk in chunks]
    placements = _every_boundary_wait_ticks(
        chunk_dicts, max_wait_ticks=_ticks_from_us(max_wait_us, int(header.tick_hz)),
        forbidden_chunks=forbidden_chunks)
    schedule = {
        "model": {"chunk_event_capacity": chunk_event_capacity},
        "boundaries": [{**chunk, "before_chunk_index": index, "wait_ticks": placements.get(index, 0)}
                       for index, chunk in enumerate(chunk_dicts)],
    }
    result = inspect_wait_schedule_encoding(payload, schedule=schedule, max_rates=max_rates, _model_cache=cache)
    # Only explicit encoding-resource pressure can shorten a candidate. A
    # malformed model/uint32 timing/source binding never becomes a fit search.
    if (result.get("status") != "PASS"
            and len(result.get("errors", [])) != len(result.get("capacity_errors", []))):
        raise SegmentedPackageError(f"wait capacity inspection rejected: {result.get('errors')}")
    return result


def plan_capacity_aware_segment_ranges(
    *,
    input_path: Path,
    total_vectors: int,
    max_segment_vector_count: int | None,
    mode: str,
    tick_hz: int,
    pin_map_path: Path | None,
    pattern_burst: str | None,
    pattern: str | None,
    strict: bool,
    allow_approx: bool,
    prefer_raw: bool,
    runtime_profile: str,
    max_rates: int | None,
    loop_policy: str,
    markers: tuple[dict[str, Any], ...] = (),
    prepared_source: PreparedStilSource | None = None,
    initial_probe_vector_count: int = 100_000,
    segment_wait_plan: str | None = None,
    max_wait_us: float | None = None,
    chunk_event_capacity: int = DEFAULT_CHUNK_EVENT_CAPACITY,
    forbidden_stil_rate_units: tuple[str, ...] = (),
) -> tuple[tuple[tuple[int, int], ...], dict[str, Any]]:
    """Choose ranges whose compiled artifacts fit every runtime capacity.

    ``max_segment_vector_count`` is an upper bound, not the capacity model.
    Each candidate range uses the same encoding/backend selection as package
    generation, without constructing unused maps/reports, and is checked for encoded bytes,
    expanded events, samples, and compares.  The search assumes the usual
    monotonic capacity metrics (adding logical vectors does not reduce any
    required resource) and performs a final per-range check in the caller.
    """

    if total_vectors < 2:
        raise SegmentedPackageError("capacity-aware segmentation requires at least two vectors")
    if max_segment_vector_count is not None and max_segment_vector_count < 1:
        raise SegmentedPackageError("max_segment_vector_count must be positive")
    if type(initial_probe_vector_count) is not int or initial_probe_vector_count < 1:
        raise SegmentedPackageError("initial_probe_vector_count must be positive")
    if mode not in {"auto", "rate-vector", "raw-event", "macro-stream"}:
        raise SegmentedPackageError(f"unsupported capacity-aware mode: {mode!r}")
    profile = get_runtime_capacity_profile(runtime_profile)
    wait_aware = segment_wait_plan == "every-boundary"
    if wait_aware and (mode != "rate-vector" or max_wait_us is None or max_wait_us <= 0):
        raise SegmentedPackageError("every-boundary capacity planning requires rate-vector and positive max_wait_us")
    prepared_source = prepared_source or PreparedStilSource(
        input_path, pin_map_path=pin_map_path, pattern_burst=pattern_burst,
        pattern=pattern, loop_policy=loop_policy, retain_full_resolution=True)
    probe_cache: dict[tuple[int, int], tuple[bool, dict[str, Any]]] = {}

    def probe(start: int, end: int) -> tuple[bool, dict[str, Any]]:
        key = (start, end)
        cached = probe_cache.get(key)
        if cached is not None:
            return cached
        segment_markers = tuple(
            marker
            for marker in markers
            if _marker_intersects_segment(marker, global_start=start, global_end=end)
        )
        try:
            options = CompileOptions(
                input_path=input_path,
                mode=mode,
                tick_hz=tick_hz,
                pin_map_path=pin_map_path,
                pattern_burst=pattern_burst,
                pattern=pattern,
                strict=strict,
                allow_approx=allow_approx,
                prefer_raw=prefer_raw,
                vector_range=(start, end),
                loop_policy=loop_policy,
                runtime_profile=profile,
                max_rates=max_rates,
                practical_gate_policy="report",
                markers=segment_markers,
                prepared_source=prepared_source,
            )
            encoding = None
            if wait_aware:
                compiled = _compile_stil_wait_probe(options)
                payload = compiled.payload
                encoding = _probe_every_boundary_encoding(
                    payload, rate_waveform_tables=compiled.rate_waveform_tables,
                    chunk_event_capacity=chunk_event_capacity, max_wait_us=max_wait_us,
                    forbidden_stil_rate_units=forbidden_stil_rate_units, max_rates=max_rates)
            else:
                payload, _compile_strategy = _compile_stil_binary_probe(options)
            prepared_source.clear_resolution()
            shipped_bytes = encoding["capacity"]["pattern_bytes_after_waits"] if encoding else len(payload)
            requirements = inspect_pattern_requirements(payload)
            diagnostics = assess_runtime_capacity(
                requirements,
                pattern_size_bytes=shipped_bytes,
                profile=profile,
            )
            if encoding:
                # The encoder's hard format limit and the selected runtime
                # region must BOTH hold, even in small-limit host tests.
                limit = min(profile.psram_pattern_bytes, pattern_format.STPG_PSRAM_PATTERN_BYTES)
                if shipped_bytes > limit:
                    diagnostics.append({"severity": "error", "code": "ERR_WAIT_ENCODED_PATTERN_BYTES_LIMIT",
                                        "message": f"post-wait pattern bytes {shipped_bytes} exceed region {limit}"})
                diagnostics.extend({"severity": "error", "code": "ERR_WAIT_ENCODING_CAPACITY", "message": error}
                                   for error in encoding.get("errors", []))
            errors = [item for item in diagnostics if item.get("severity") == "error"]
            result = (
                not errors,
                {
                    "start": start,
                    "end": end,
                    "vector_count": end - start + 1,
                    "fit": not errors,
                    "pattern_bytes": shipped_bytes,
                    **({"wait_encoding": {**encoding["capacity"], **encoding["encoding"],
                                          "rate_budget": encoding["rate_budget"], "pattern_region_bytes": limit}}
                       if encoding else {}),
                    "estimated_event_count": int(requirements.estimated_event_count),
                    "estimated_sample_count": int(requirements.estimated_sample_count),
                    "estimated_compare_count": int(requirements.estimated_compare_count),
                    "diagnostics": diagnostics,
                    # Historical capacity plans read a top-level report key,
                    # while the strategy lives under capability_precheck.
                    # Keep their null field byte-compatible; complete compile
                    # reports continue to expose the actual backend strategy.
                    "compile_strategy": None,
                },
            )
        except (OSError, StilError, ValueError) as exc:
            if (max_segment_vector_count is None or wait_aware) and not _capacity_failure(exc):
                raise SegmentedPackageError(f"capacity-first probe rejected at {start}..{end}: {exc}") from exc
            result = (
                False,
                {
                    "start": start,
                    "end": end,
                    "vector_count": end - start + 1,
                    "fit": False,
                    "compile_error": str(exc),
                    "compile_error_code": getattr(exc, "code", None),
                },
            )
        probe_cache[key] = result
        return result

    ranges: list[tuple[int, int]] = []
    decisions: list[dict[str, Any]] = []
    last_span = None
    start = 0
    while start < total_vectors:
        capacity_first = max_segment_vector_count is None
        ceiling = total_vectors - 1 if capacity_first else min(total_vectors - 1, start + max_segment_vector_count - 1)
        initial_span = last_span or initial_probe_vector_count
        upper = min(ceiling, start + initial_span - 1) if capacity_first else ceiling
        fits, upper_detail = probe(start, upper)
        eligible = (isinstance(prepared_source, PreparedStilSource)
                    and prepared_source._flat_preserved_source()
                    and mode in {"auto", "rate-vector"} and not prefer_raw)
        fit_points = []
        estimates_left = 8

        def interpolated_end(end: int, detail: dict[str, Any], high: int) -> int | None:
            # A hint only: every proposed range is really encoded and checked.
            # Bound interpolation attempts; unhelpful/nonlinear metrics revert
            # to bisection. Loops/ambiguous auto backends never enter this path.
            nonlocal estimates_left
            if not eligible or estimates_left <= 0:
                return None
            size = detail.get("pattern_bytes")
            if type(size) is not int or size <= 0:
                return None
            limit = min(profile.psram_pattern_bytes, pattern_format.STPG_PSRAM_PATTERN_BYTES)
            width, growth = end - start + 1, size
            for old_end, old_size in reversed(fit_points):
                if old_end < end and old_size < size:
                    width, growth = end - old_end, size - old_size
                    break
            candidate = end + max(1, (limit - size) * width // growth)
            if not end < candidate <= high:
                return None
            estimates_left -= 1
            return candidate

        if fits and type(upper_detail.get("pattern_bytes")) is int:
            fit_points.append((upper, upper_detail.get("pattern_bytes")))
        previous_fit = None
        while capacity_first and fits and upper < ceiling:
            previous_fit = (upper, upper_detail)
            estimate = interpolated_end(upper, upper_detail, ceiling)
            upper = estimate if estimate is not None else min(ceiling, start + 2 * (upper - start + 1) - 1)
            fits, upper_detail = probe(start, upper)
            if fits and type(upper_detail.get("pattern_bytes")) is int:
                fit_points.append((upper, upper_detail.get("pattern_bytes")))
        if fits:
            best_end = upper
            best_detail = upper_detail
        else:
            single_fits, single_detail = (True, previous_fit[1]) if previous_fit else probe(start, start)
            if not single_fits:
                raise SegmentedPackageError(
                    "capacity-aware segmentation cannot fit one logical vector at "
                    f"{start}: {single_detail.get('compile_error') or single_detail.get('diagnostics')}; "
                    f"wait encoding={single_detail.get('wait_encoding')}"
                )
            best_end = previous_fit[0] if previous_fit else start
            low = best_end + 1
            high = upper - 1
            best_detail = single_detail
            while low <= high:
                estimate = interpolated_end(best_end, best_detail, high)
                middle = estimate if estimate is not None else (low + high) // 2
                middle_fits, middle_detail = probe(start, middle)
                if middle_fits:
                    if type(best_detail.get("pattern_bytes")) is int:
                        fit_points.append((best_end, best_detail["pattern_bytes"]))
                    best_end = middle
                    best_detail = middle_detail
                    low = middle + 1
                else:
                    high = middle - 1
        ranges.append((start, best_end))
        decisions.append(
            {
                "global_start": start,
                "global_end": best_end,
                "max_requested_end": upper,
                "metrics": best_detail,
            }
        )
        last_span = best_end - start + 1 if eligible else None
        start = best_end + 1

    if len(ranges) < 2:
        raise SegmentedPackageError(
            "capacity-aware segmentation found one fitting range; use a non-segmented package"
        )
    return (
        tuple(ranges),
        {
            "kind": "stpg_capacity_aware_segment_plan",
            "schema_version": 2 if max_segment_vector_count is None else 1,
            "status": "PLANNED",
            "runtime_profile": profile.name,
            "mode": mode,
            "loop_policy": loop_policy,
            "max_segment_vector_count": max_segment_vector_count,
            **({"planning_strategy": "capacity-first", "initial_probe_vector_count": initial_probe_vector_count}
               if max_segment_vector_count is None else {}),
            "source_vector_count": total_vectors,
            **({"capacity_basis": "every-boundary-encoded-waits",
                "max_wait_us": max_wait_us, "chunk_event_capacity": chunk_event_capacity,
                "forbidden_stil_rate_units": list(forbidden_stil_rate_units)} if wait_aware else {}),
            "ranges": [list(item) for item in ranges],
            "decisions": decisions,
            "probe_count": len(probe_cache),
            "physical_execution_authorized": False,
            "limitations": [
                "planning is host-only and uses the same compiler/capacity contract as package generation",
                "service timing, electrical continuity, and physical execution are not evaluated",
            ],
        },
    )


def _hold_state(value: Any, *, field: str) -> dict[str, Any]:
    try:
        numeric = int(value)
    except (TypeError, ValueError) as exc:
        raise SegmentedPackageError(f"invalid HOLD {field}: {value!r}") from exc
    if numeric < 0 or numeric >= (1 << 24):
        raise SegmentedPackageError(f"HOLD {field} is outside 24 logical bits: {numeric}")
    payload = {field: f"0x{numeric:06X}"}
    return payload


def _hold_pair(drive_value: int, drive_enable: int) -> dict[str, Any]:
    value = _hold_state(drive_value, field="drive_value")
    enable = _hold_state(drive_enable, field="drive_enable")
    drive_value = value["drive_value"]
    drive_enable = enable["drive_enable"]
    canonical = {"drive_value": drive_value, "drive_enable": drive_enable}
    canonical_bytes = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    canonical["hash"] = hashlib.sha256(canonical_bytes).hexdigest()
    return canonical


def _zero_hold() -> dict[str, Any]:
    return _hold_pair(0, 0)


def _segment_pattern_contract(
    package_dir: Path, map_data: dict[str, Any], *, pattern_bytes: bytes | None = None
) -> dict[str, Any]:
    if pattern_bytes is None:
        pattern_bytes = (package_dir / "pattern.bin").read_bytes()
    requirements = inspect_pattern_requirements(pattern_bytes)
    contract: dict[str, Any] = {
        "mode": requirements.mode,
        "tick_hz": requirements.tick_hz,
        "active_pins": requirements.active_pins,
        "estimated_event_count": requirements.estimated_event_count,
        "estimated_sample_count": requirements.estimated_sample_count,
        "estimated_compare_count": requirements.estimated_compare_count,
    }
    if requirements.mode == "rate-vector":
        header, _rates, _slots, _vectors, loops = unpack_rate_vector_full(pattern_bytes)
        structure = rate_vector_structure_metrics(pattern_bytes)
        contract.update(
            {
                "rate_count": header.rate_count,
                "slot_count": header.slot_count,
                "vector_count": header.vector_count,
                "top_level_entry_count": structure["top_level_entry_count"],
                "encoded_record_count": structure["encoded_record_count"],
                "loop_block_count": structure["loop_block_count"],
                "loop_body_record_count": structure["loop_body_record_count"],
                "expanded_logical_vector_count": structure["expanded_logical_vector_count"],
                "loop_count": len(loops),
                "emitted_map_vector_count": len(map_data.get("vectors", {})),
            }
        )
    else:
        contract["vector_count"] = len(map_data.get("vectors", {}))
    return contract


def _identity_variant_source(input_path: Path) -> dict[str, Any]:
    # A nominal variant has no transformed source to materialize. Reuse the
    # original input directly instead of copying a potentially very large STIL
    # file into every segmented-package output directory.
    identity = _identity(input_path)
    return {
        "kind": "stpg_stil_segmented_package.identity_variant",
        "schema_version": SEGMENTED_PACKAGE_SCHEMA_VERSION,
        "status": "DERIVED",
        "variant": "nominal",
        "input": identity,
        "output": identity,
        "waveform_tables": [],
        "changed_period_count": 0,
        "changed_event_count": 0,
        "semantics": {
            "vector_characters_preserved": True,
            "event_order_preserved": True,
            "selected_table_event_times_scaled_with_period": False,
            "hidden_wait_insertion": False,
            "physical_execution_authorized": False,
        },
    }


def _write_json(path: Path, value: dict[str, Any], *, compact: bool = False) -> Path:
    if compact:
        return write_json_artifact(path, value, min_compress_bytes=32 * 1024, indent=None)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _file_identity(path: Path) -> dict[str, Any]:
    return _identity(path)


def _aggregate_segment_runtime_model(
    segment_inputs: list[dict[str, Any]],
    *,
    chunk_event_capacity: int,
    ring_slot_count: int,
) -> tuple[dict[str, Any], list[dict[str, int]]]:
    """Build one logical runtime model from independently stored segments.

    The input binaries remain separate.  Only their host-side emitted chunk
    geometry is concatenated for planning; no full-size pattern binary is
    materialized.  The returned ranges map logical planner chunks back to the
    segment that will receive the encoded wait.
    """

    aggregate_chunks: list[dict[str, Any]] = []
    segment_chunk_ranges: list[dict[str, int]] = []
    event_cursor = 0
    total_consume_ticks = 0
    tick_hz: int | None = None
    versions: set[int] = set()
    rate_count = 0
    slot_count = 0
    model_cache = _RuntimeModelCache()

    for item in segment_inputs:
        package_dir = Path(item["source_package_dir"])
        pattern_path = package_dir / "pattern.bin"
        try:
            runtime = build_runtime_model(
                pattern_path.read_bytes(),
                chunk_event_capacity=chunk_event_capacity,
                ring_slot_count=ring_slot_count,
                _model_cache=model_cache,
            )
        except (OSError, RuntimeWaitModelError, ValueError) as exc:
            raise SegmentedPackageError(
                f"segment {item['segment_id']} cannot be modeled for automatic waits: {exc}"
            ) from exc

        model_data = runtime["model"]
        pattern_data = runtime["pattern"]
        segment_tick_hz = int(model_data["tick_hz"])
        if tick_hz is None:
            tick_hz = segment_tick_hz
        elif tick_hz != segment_tick_hz:
            raise SegmentedPackageError(
                "automatic segmented wait planning requires one tick_hz across all segments"
            )
        # Rate Vector v1 and v2 have different field widths, but their
        # decoded event/chunk geometry has the same units. The encoder and
        # capacity checks below still work on each individual segment.
        versions.add(int(pattern_data["version"]))

        local_chunks = list(pattern_data.get("chunks", []))
        if not local_chunks:
            raise SegmentedPackageError(
                f"segment {item['segment_id']} has no runtime chunks for automatic wait planning"
            )
        map_data = read_json_artifact(package_dir / "pattern.map.json")
        local_source_contexts = runtime_chunk_source_contexts(
            pattern_path.read_bytes(),
            local_chunks,
            map_data,
            _model_cache=model_cache,
        )
        # Contexts contain only rate-unit strings. Do not keep the entire
        # previous map alive while the next segment's JSON is decoded.
        del map_data
        if len(local_source_contexts) != len(local_chunks):
            raise SegmentedPackageError(
                f"segment {item['segment_id']} source rate-unit mapping does not match its chunks"
            )
        first_chunk = len(aggregate_chunks)
        segment_event_start = event_cursor
        vector_offset = int(item["global_vector_start"])
        for local_index, local_chunk in enumerate(local_chunks):
            chunk = dict(local_chunk)
            chunk["chunk_index"] = len(aggregate_chunks)
            chunk["event_start"] = int(local_chunk["event_start"]) + event_cursor
            chunk["event_end_exclusive"] = (
                int(local_chunk["event_end_exclusive"]) + event_cursor
            )
            chunk["logical_vector_start"] = (
                int(local_chunk["logical_vector_start"]) + vector_offset
            )
            chunk["logical_vector_end_exclusive"] = (
                int(local_chunk["logical_vector_end_exclusive"]) + vector_offset
            )
            chunk["rate_units"] = list(
                local_source_contexts[local_index]["rate_units"]
            )
            chunk["boundary_rate_unit"] = local_source_contexts[local_index][
                "boundary_rate_unit"
            ]
            aggregate_chunks.append(chunk)
        segment_event_count = int(pattern_data["expanded_event_count"])
        event_cursor += segment_event_count
        total_consume_ticks += int(pattern_data["total_consume_ticks"])
        rate_count = max(rate_count, int(pattern_data.get("rate_count", 0)))
        slot_count = max(slot_count, int(pattern_data.get("slot_count", 0)))
        segment_chunk_ranges.append(
            {
                "segment_id": int(item["segment_id"]),
                "global_vector_start": int(item["global_vector_start"]),
                "global_vector_end": int(item["global_vector_end"]),
                "global_event_start": segment_event_start,
                "global_event_end": event_cursor - 1,
                "first_chunk_index": first_chunk,
                "last_chunk_index": len(aggregate_chunks) - 1,
            }
        )

    if tick_hz is None or not aggregate_chunks:
        raise SegmentedPackageError("automatic segmented wait planning produced no runtime model")
    return (
        {
            "model": {
                "version": "runtime-wait-model-v1",
                "tick_hz": tick_hz,
                "chunk_event_capacity": chunk_event_capacity,
                "ring_slot_count": ring_slot_count,
                "steady_state_lead_slots": ring_slot_count - 1,
                "steady_state_lead_chunks": ring_slot_count - 1,
                "service_time_assumption": "supplied by automatic segmented wait plan",
            },
            "pattern": {
                "version": next(iter(versions)) if len(versions) == 1 else None,
                **({"versions": sorted(versions)} if len(versions) > 1 else {}),
                "rate_count": rate_count,
                "slot_count": slot_count,
                "expanded_event_count": event_cursor,
                "total_consume_ticks": total_consume_ticks,
                "total_consume_us": total_consume_ticks * 1_000_000.0 / tick_hz,
                "chunk_count": len(aggregate_chunks),
                "chunks": aggregate_chunks,
            },
            "assumptions": [
                "the planner consumes the emitted chunk windows of each stored segment in order",
                "segment-local binaries remain independent artifacts; no full pattern binary is created",
                "segment upload boundaries are forbidden wait locations and are handled by the constrained planner",
                "startup fill, DMA scheduling, Core1 request timing, and USB timing are not simulated",
                "this report is host-only and does not modify or authorize physical execution",
            ],
        },
        segment_chunk_ranges,
    )


def _build_segmented_wait_schedule(
    model: dict[str, Any],
    *,
    source_identity: dict[str, Any],
    segment_chunk_ranges: list[dict[str, int]],
    max_rates: int | None,
    service_time_us: float,
    service_time_source: str,
    safety_margin_us: float,
    max_wait_us: float,
    max_total_wait_us: float,
    max_wait_count: int | None,
    pattern_time_limit_us: float | None,
    wait_policy: str = "pressure",
    forbidden_stil_rate_units: tuple[str, ...] = (),
    service_profile_scope: str = "non-loop",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Plan one logical schedule and acceptance report for all segments."""

    if service_profile_scope not in {"non-loop", "loop-generic"}:
        raise SegmentedPackageError(
            "service_profile_scope must be one of: non-loop, loop-generic"
        )

    forbidden_wait_chunks = {
        int(segment_range["first_chunk_index"])
        for segment_range in segment_chunk_ranges[1:]
    }
    forbidden_stil_rate_units = _normalize_stil_rate_units(forbidden_stil_rate_units)
    if forbidden_stil_rate_units:
        available_units = {
            str(rate_unit)
            for chunk in model["pattern"]["chunks"]
            for rate_unit in chunk.get("rate_units", [])
        }
        unknown_units = sorted(set(forbidden_stil_rate_units) - available_units)
        if unknown_units:
            raise SegmentedPackageError(
                f"forbidden STIL rate unit(s) are not used by this pattern: {unknown_units}"
            )
        forbidden_wait_chunks.update(
            int(chunk["chunk_index"])
            for chunk in model["pattern"]["chunks"]
            if (
                not isinstance(chunk.get("boundary_rate_unit"), str)
                or chunk.get("boundary_rate_unit") in forbidden_stil_rate_units
            )
        )
    planner_policy = "pressure" if wait_policy == "auto" else wait_policy
    plan = plan_waits(
        model,
        service_time_us=service_time_us,
        service_time_source=service_time_source,
        safety_margin_us=safety_margin_us,
        max_wait_us=max_wait_us,
        max_total_wait_us=max_total_wait_us,
        max_wait_count=max_wait_count,
        pattern_time_limit_us=pattern_time_limit_us,
        forbidden_wait_chunks=forbidden_wait_chunks,
        wait_policy=planner_policy,
    )
    tick_hz = int(model["model"]["tick_hz"])
    waits_by_chunk: dict[int, int] = {}
    for wait in plan.get("schedule", {}).get("waits", []):
        if not isinstance(wait, dict):
            continue
        chunk_index = int(wait["before_chunk_index"])
        waits_by_chunk[chunk_index] = waits_by_chunk.get(chunk_index, 0) + int(wait["wait_ticks"])

    for segment_range in segment_chunk_ranges[1:]:
        first_chunk = int(segment_range["first_chunk_index"])
        if waits_by_chunk.get(first_chunk, 0) > 0:
            raise SegmentedPackageError(
                "constrained automatic segmented wait planner emitted a forbidden "
                "segment upload boundary wait; "
                f"segment {segment_range['segment_id']} first chunk {first_chunk}"
            )

    boundaries: list[dict[str, Any]] = []
    for chunk in model["pattern"]["chunks"]:
        chunk_index = int(chunk["chunk_index"])
        boundary = dict(chunk)
        wait_ticks = waits_by_chunk.get(chunk_index, 0)
        boundary.update(
            {
                "before_chunk_index": chunk_index,
                "wait_ticks": wait_ticks,
                "wait_us": wait_ticks * 1_000_000.0 / tick_hz,
                "source_context": {"scope": "logical_segmented_pattern"},
            }
        )
        boundaries.append(boundary)

    schedule = {
        "kind": "stpg_wait_schedule",
        "schema_version": 1,
        "status": "PROPOSED",
        "scope": "logical_segmented_pattern",
        "identity": {
            "source": source_identity,
            "segments": segment_chunk_ranges,
        },
        "model": {
            **model["model"],
            "max_rate_count": max_rates,
            "expanded_event_count": model["pattern"]["expanded_event_count"],
            "chunk_count": model["pattern"]["chunk_count"],
            "total_consume_ticks": model["pattern"]["total_consume_ticks"],
            "total_consume_us": model["pattern"]["total_consume_us"],
        },
        "policy": {
            "placement": "before_segment_local_chunk",
            "wait_planning_policy": wait_policy,
            "service_profile_scope": service_profile_scope,
            "canonical_wait_unit": "ticks",
            "automatic_insertion": False,
            "pattern_mutation": False,
            "segment_boundary_first_wait_supported": False,
            "forbidden_wait_chunks": sorted(forbidden_wait_chunks),
            "forbidden_stil_rate_units": list(forbidden_stil_rate_units),
            "forbidden_stil_rate_match": (
                FORBIDDEN_STIL_RATE_MATCH_NEXT_CHUNK_FIRST_EVENT
            ),
            "relocation_from_forbidden_boundary_supported": True,
            "physical_execution_authorized": False,
        },
        "boundaries": boundaries,
        "proposal": plan,
        "limitations": [
            "the schedule is planned from concatenated host-side segment geometry",
            (
                "every-boundary places the configured wait before each eligible chunk; "
                "segment first and forbidden-rate boundaries remain zero"
                if wait_policy == "every-boundary"
                else "a requested wait before a segment's first local chunk is relocated to an earlier legal chunk when the host model can clear or reduce the pressure"
            ),
            (
                "the constrained planner rejects the candidate if no legal relocation satisfies the declared limits"
                if wait_policy in {"auto", "pressure"}
                else "the every-boundary planner rejects the candidate if its explicit waits violate the declared limits or leave modeled pressure"
            ),
            "service time is a supplied bounded model input, not a physical measurement",
            "this schedule does not authorize physical execution",
        ],
    }
    acceptance_status = "ACCEPTED" if plan.get("status") in {"planned", "no_wait_needed"} else "REJECTED"
    acceptance = {
        "kind": "stpg_wait_schedule_acceptance",
        "schema_version": "v1",
        "status": acceptance_status,
        "scope": "bounded_host_model_only",
        "input": {
            "source": source_identity,
            "segments": segment_chunk_ranges,
        },
        "service_profile_scope": service_profile_scope,
        "model": model,
        "wait_plan": plan,
        "wait_decision": {
            "status": "WAIT_ACCEPTED" if acceptance_status == "ACCEPTED" else "WAIT_REJECTED",
            "reason": plan.get("reason") or plan.get("status"),
        },
        "steady_tfill": {
            "status": "not_emitted",
            "reason": "host-only acceptance has no physical trace coverage or steady-data measurement",
        },
        "errors": [] if acceptance_status == "ACCEPTED" else [str(plan.get("reason", "wait plan rejected"))],
        "warnings": [],
        "physical_execution_authorized": False,
        "limitations": [
            "service time is a supplied bounded model input, not a universal worst case",
            "segment-local application and package verification are still required",
            "this report is not timing sign-off or physical approval",
        ],
    }
    return schedule, acceptance


def _materialize_projected_segment_wait(
    *,
    global_schedule_path: Path,
    segment_input: dict[str, Any],
    service_time_us: float | None,
    service_time_source: str,
    safety_margin_us: float,
    max_wait_us: float | None,
    max_total_wait_us: float | None,
    max_wait_count: int | None,
    pattern_time_limit_us: float | None,
    max_rates: int | None,
    service_profile_scope: str,
    compact_sidecars: bool = False,
) -> None:
    segment_dir = Path(segment_input["segment_dir"])
    source_package_dir = Path(segment_input["source_package_dir"])
    package_dir = Path(segment_input["package_dir"])
    base_map_path = source_package_dir / "pattern.map.json"
    base_schedule_path = source_package_dir / "pattern.wait.json"
    projected_schedule = derive_segment_wait_schedule(
        global_schedule_path,
        base_schedule_path,
        global_event_start=int(segment_input["global_event_start"]),
        global_event_end=int(segment_input["global_event_end"]),
        segment_id=int(segment_input["segment_id"]),
    )
    local_scope = segment_input["service_profile_scope"]
    scope_binding = {"declared_sequence_scope": service_profile_scope,
                     "segment_scope": local_scope, "segment_id": int(segment_input["segment_id"])}
    projected_schedule["policy"].update({"service_profile_scope": local_scope,
                                          "segmented_service_profile": scope_binding})
    projected_schedule_path = segment_dir / "projected.wait.json"
    projected_schedule_path = _write_json(projected_schedule_path, projected_schedule, compact=compact_sidecars)
    model_cache = _RuntimeModelCache()
    source_bytes = (source_package_dir / "pattern.bin").read_bytes()
    encoding = inspect_wait_schedule_encoding(
        source_bytes, schedule=projected_schedule, max_rates=max_rates, _model_cache=model_cache)
    source_metadata = read_json_artifact(source_package_dir / "pattern.metadata.json")
    profile = get_runtime_capacity_profile(source_metadata["options"]["runtime_profile"]["name"])
    limit = min(profile.psram_pattern_bytes, pattern_format.STPG_PSRAM_PATTERN_BYTES)
    cost = encoding.get("capacity", {})
    if (encoding.get("status") != "PASS" or cost.get("pattern_bytes_after_waits", limit + 1) > limit):
        raise PatternPackageError(
            f"wait encoding capacity rejected: {cost.get('pattern_bytes_before_waits')} bytes before "
            f"{cost.get('nonzero_wait_count')} waits, {cost.get('pattern_bytes_after_waits')} bytes after; "
            f"PSRAM pattern region {limit}; {encoding.get('errors')}")
    witness = segment_input.get("wait_capacity")
    if witness is not None and any(cost.get(key) != witness.get(key) for key in cost):
        raise PatternPackageError("projected wait encoding differs from the capacity-plan witness")
    projected_acceptance = evaluate_wait_schedule(
        source_package_dir / "pattern.bin",
        projected_schedule_path,
        map_path=base_map_path,
        service_time_us=service_time_us,
        service_time_source=service_time_source,
        safety_margin_us=safety_margin_us,
        max_wait_us=max_wait_us,
        max_total_wait_us=max_total_wait_us,
        max_wait_count=max_wait_count,
        pattern_time_limit_us=pattern_time_limit_us,
        max_rates=max_rates,
        service_profile_scope=local_scope,
        _model_cache=model_cache,
    )
    projected_acceptance["segmented_service_profile"] = scope_binding
    projected_acceptance_path = segment_dir / "projected.wait.acceptance.json"
    projected_acceptance_path = _write_json(projected_acceptance_path, projected_acceptance, compact=compact_sidecars)
    if projected_acceptance.get("status") != "ACCEPTED":
        raise PatternPackageError(
            "projected wait schedule acceptance failed: "
            + "; ".join(projected_acceptance.get("errors", []))
        )
    has_wait = any(
        isinstance(boundary, dict) and int(boundary.get("wait_ticks", 0)) > 0
        for boundary in projected_schedule.get("boundaries", [])
    )
    if has_wait:
        package_result = derive_pattern_package_with_wait(
            source_package_dir,
            schedule_path=projected_schedule_path,
            acceptance_path=projected_acceptance_path,
            output_dir=package_dir,
            _model_cache=model_cache,
        )
        if package_result.get("status") != "VERIFIED":
            raise PatternPackageError(
                f"derived package status is not VERIFIED: {package_result.get('status')!r}"
            )
    else:
        shutil.copytree(source_package_dir, package_dir)
        # The staging package deliberately had no service admission. Even
        # a zero-wait projection needs its actual local scope and the now
        # accepted budget, rather than copying REQUIRES_SERVICE_PROFILE.
        _json_write(package_dir / "pattern.wait.json", projected_schedule)
        validation = validate_wait_schedule(package_dir / "pattern.bin", package_dir / "pattern.wait.json",
                                             map_path=package_dir / "pattern.map.json", max_rates=max_rates)
        if validation["status"] != "PASS":
            raise PatternPackageError("zero-wait projection failed validation")
        _json_write(package_dir / "pattern.wait.validation.json", validation)
        _json_write(package_dir / "pattern.wait.acceptance.json", projected_acceptance)
        metadata = read_json_artifact(package_dir / "pattern.metadata.json")
        metadata["options"].update({"service_time_us": service_time_us,
                                    "service_time_source": service_time_source,
                                    "service_profile_scope": local_scope})
        metadata["segmented_service_profile"] = scope_binding
        _json_write(package_dir / "pattern.metadata.json", metadata)
        _write_json(package_dir / "manifest.json", _manifest_for_package(package_dir, package_metadata=metadata))
        if verify_pattern_package(package_dir)["status"] != "PASS":
            raise PatternPackageError("zero-wait projection package failed verification")
    shutil.rmtree(source_package_dir)


def derive_segment_wait_schedule(
    global_schedule_path: Path,
    base_schedule_path: Path,
    *,
    global_event_start: int,
    global_event_end: int,
    segment_id: int,
) -> dict[str, Any]:
    """Project a global wait schedule onto one independently compiled segment.

    Wait schedules are keyed by the event boundary before a runtime chunk, not
    by source-vector number.  A segment therefore inherits a wait by matching
    ``global_event_start + local_event_start``.  A positive wait at local
    boundary zero is rejected: the current binary encoder cannot represent a
    pause before the first chunk of an independently uploaded package.  The
    caller must choose a split where that global boundary has zero wait.
    """

    try:
        global_schedule = read_json_artifact(global_schedule_path)
        base_schedule = read_json_artifact(base_schedule_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SegmentedPackageError(f"cannot read wait schedule: {exc}") from exc
    if not isinstance(global_schedule, dict) or not isinstance(base_schedule, dict):
        raise SegmentedPackageError("wait schedules must have JSON object roots")
    for label, schedule in (("global", global_schedule), ("segment", base_schedule)):
        if schedule.get("kind") != "stpg_wait_schedule":
            raise SegmentedPackageError(f"{label} wait schedule kind is unsupported")
        if not isinstance(schedule.get("boundaries"), list):
            raise SegmentedPackageError(f"{label} wait schedule has no boundaries")
    if global_event_start < 0 or global_event_end < global_event_start:
        raise SegmentedPackageError("segment global event range is invalid")

    global_by_event: dict[int, dict[str, Any]] = {}
    for boundary in global_schedule["boundaries"]:
        if not isinstance(boundary, dict):
            raise SegmentedPackageError("global wait schedule boundary must be an object")
        try:
            event_start = int(boundary["event_start"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SegmentedPackageError("global wait boundary lacks integer event_start") from exc
        if event_start in global_by_event:
            raise SegmentedPackageError(f"duplicate global wait boundary at event {event_start}")
        global_by_event[event_start] = boundary

    projected = deepcopy(base_schedule)
    projected["status"] = "DERIVED"
    projected_boundaries: list[dict[str, Any]] = []
    selected_global_boundaries: list[int] = []
    for local_index, local_boundary in enumerate(base_schedule["boundaries"]):
        if not isinstance(local_boundary, dict):
            raise SegmentedPackageError(f"segment wait boundary {local_index} must be an object")
        try:
            local_event_start = int(local_boundary["event_start"])
            local_event_count = int(local_boundary["event_count"])
            local_event_end = int(local_boundary["event_end_exclusive"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SegmentedPackageError(
                f"segment wait boundary {local_index} has invalid geometry"
            ) from exc
        global_boundary_event = global_event_start + local_event_start
        if global_boundary_event < global_event_start or global_boundary_event > global_event_end:
            raise SegmentedPackageError(
                f"segment boundary {local_index} maps outside global event range: "
                f"{global_boundary_event} not in [{global_event_start}, {global_event_end}]"
            )
        source_boundary = global_by_event.get(global_boundary_event)
        if source_boundary is None:
            raise SegmentedPackageError(
                f"global wait schedule has no boundary at event {global_boundary_event} "
                f"for segment boundary {local_index}"
            )
        if int(source_boundary.get("event_count", -1)) != local_event_count:
            raise SegmentedPackageError(
                f"wait boundary event geometry mismatch at global event {global_boundary_event}"
            )
        if int(source_boundary.get("event_end_exclusive", -1)) != global_boundary_event + local_event_count:
            raise SegmentedPackageError(
                f"wait boundary end mismatch at global event {global_boundary_event}"
            )
        wait_ticks = int(source_boundary.get("wait_ticks", 0))
        if wait_ticks < 0:
            raise SegmentedPackageError(f"negative wait at global event {global_boundary_event}")
        if local_index == 0 and wait_ticks:
            raise SegmentedPackageError(
                f"split at global event {global_boundary_event} loses a non-zero first-boundary wait"
            )
        projected_boundary = deepcopy(local_boundary)
        projected_boundary["wait_ticks"] = wait_ticks
        tick_hz = int(projected.get("model", {}).get("tick_hz", 0))
        if tick_hz <= 0:
            raise SegmentedPackageError("segment wait schedule has invalid model.tick_hz")
        projected_boundary["wait_us"] = wait_ticks * 1_000_000.0 / tick_hz
        projected_boundary["reason"] = (
            f"projected from global boundary {int(source_boundary['before_chunk_index'])}"
        )
        projected_boundaries.append(projected_boundary)
        if wait_ticks:
            selected_global_boundaries.append(int(source_boundary["before_chunk_index"]))

    projected["boundaries"] = projected_boundaries
    projected["identity"] = deepcopy(base_schedule.get("identity"))
    global_policy = (
        global_schedule.get("policy")
        if isinstance(global_schedule.get("policy"), dict)
        else {}
    )
    projected_policy = (
        base_schedule.get("policy")
        if isinstance(base_schedule.get("policy"), dict)
        else {}
    )
    projected_policy = deepcopy(projected_policy)
    for key in ("forbidden_stil_rate_units", "forbidden_stil_rate_match"):
        if key in global_policy:
            projected_policy[key] = deepcopy(global_policy[key])
    projected["policy"] = {
        **projected_policy,
        "derived_from_global_schedule": True,
        "automatic_insertion": False,
        "pattern_mutation": False,
        "physical_execution_authorized": False,
    }
    projected["provenance"] = {
        "global_schedule": _file_identity(global_schedule_path),
        "global_event_range": [global_event_start, global_event_end],
        "segment_id": segment_id,
        "selected_global_boundary_indices": selected_global_boundaries,
        "split_boundary_first_wait_must_be_zero": True,
    }
    projected.setdefault("limitations", []).append(
        "derived from a global schedule by exact event-boundary projection; no wait is inserted at a segment first boundary"
    )
    return projected


def _segment_manifest(
    *,
    path: Path,
    variant_name: str,
    source_identity: dict[str, Any],
    segments: list[dict[str, Any]],
    pattern_contract: dict[str, Any],
    global_wait_schedule: dict[str, Any] | None = None,
    segment_wait_plan: dict[str, Any] | None = None,
    segment_wait_plan_acceptance: dict[str, Any] | None = None,
    capacity_plan: dict[str, Any] | None = None,
    service_profile_scope: str | None = None,
) -> dict[str, Any]:
    manifest = {
        "kind": SEGMENTED_PACKAGE_KIND + ".manifest",
        "schema_version": SEGMENTED_PACKAGE_SCHEMA_VERSION,
        "status": "VERIFIED",
        "variant": variant_name,
        "source": source_identity,
        "pattern_contract": pattern_contract,
        "segments": segments,
        "policies": {
            "global_vector_index_is_report_authority": True,
            "segment_ranges_are_inclusive": True,
            "entry_hold_matches_previous_final_hold": True,
            "terminal_hold_derived_from_binary_and_entry": True,
            "fresh_wait_records_per_segment": True,
            "hidden_wait_insertion": False,
            "physical_execution_authorized": False,
            "global_wait_schedule_projected": global_wait_schedule is not None,
            "segment_wait_plan_auto": segment_wait_plan is not None,
            "segment_wait_plan_mode": (
                "external-schedule" if global_wait_schedule is not None else segment_wait_plan
            ),
            "service_profile_scope": service_profile_scope,
            "capacity_aware_segment_plan": capacity_plan is not None,
        },
        "limitations": [
            "host-only package preparation does not prove electrical hold continuity",
            "firmware hold/resume support is implemented only by a separately admitted compatible build",
            "compare/sample results are segment-local and must be collected before the next upload",
            "Rate Vector segmented execution requires a separately approved compatible firmware candidate",
            "split boundaries are structural vector boundaries; AC stop-safety remains a user decision",
        ],
        "manifest_path": str(path),
    }
    if global_wait_schedule is not None:
        manifest["global_wait_schedule"] = global_wait_schedule
    if segment_wait_plan is not None:
        manifest["segment_wait_plan"] = segment_wait_plan
    if segment_wait_plan_acceptance is not None:
        manifest["segment_wait_plan_acceptance"] = segment_wait_plan_acceptance
    if capacity_plan is not None:
        manifest["capacity_plan"] = capacity_plan
    if service_profile_scope is not None:
        manifest["policies"]["service_scope_semantics"] = "sequence_union_v1"
    return manifest


@rate_vector_decode_scope()
def build_segmented_packages(
    *,
    input_path: Path,
    output_dir: Path,
    report_path: Path,
    ranges: tuple[tuple[int, int], ...],
    total_vectors: int | None = None,
    variants: tuple[RateSweepVariant, ...] = (),
    mode: str = "auto",
    tick_hz: int = STPG_DEFAULT_TICK_HZ,
    pin_map_path: Path | None = None,
    pattern_burst: str | None = None,
    pattern: str | None = None,
    strict: bool = False,
    allow_approx: bool = False,
    prefer_raw: bool = False,
    runtime_profile: str = DEFAULT_RUNTIME_PROFILE,
    max_rates: int | None = None,
    chunk_event_capacity: int = DEFAULT_CHUNK_EVENT_CAPACITY,
    ring_slot_count: int = DEFAULT_RING_SLOT_COUNT,
    service_time_us: float | None = None,
    service_time_source: str = "",
    service_profile_scope: str = "non-loop",
    safety_margin_us: float = 0.0,
    max_wait_us: float | None = None,
    max_total_wait_us: float | None = None,
    max_wait_count: int | None = None,
    pattern_time_limit_us: float | None = None,
    loop_policy: str = "expand",
    global_wait_schedule_path: Path | None = None,
    segment_wait_plan: str | None = None,
    forbidden_stil_rate_units: tuple[str, ...] = (),
    markers: tuple[dict[str, Any], ...] = (),
    capacity_aware: bool = False,
    capacity_first: bool = False,
    prepared_source: PreparedStilSource | None = None,
    compact_sidecars: bool = False,
) -> dict[str, Any]:
    """Build one or more independent segmented package manifests.

    ``segment_wait_plan="auto"`` keeps the binaries separate while planning
    one pressure-based wait schedule over their concatenated runtime chunks.
    ``segment_wait_plan="every-boundary"`` instead places the configured
    wait before every eligible chunk boundary.  Both schedules are projected
    and applied internally; callers do not repeat wait operations per segment.
    """

    if global_wait_schedule_path is not None and segment_wait_plan is not None:
        raise SegmentedPackageError(
            "provide only one of global_wait_schedule_path or segment_wait_plan"
        )
    if segment_wait_plan not in {None, "auto", "every-boundary"}:
        raise SegmentedPackageError(f"unsupported segment_wait_plan: {segment_wait_plan!r}")
    if global_wait_schedule_path is not None or segment_wait_plan is not None:
        if prefer_raw:
            raise SegmentedPackageError("segmented waits require rate-vector, not prefer_raw")
        if mode == "auto":
            mode = "rate-vector"  # deterministic format requirement, never a fallback
    forbidden_stil_rate_units = _normalize_stil_rate_units(forbidden_stil_rate_units)
    if (
        forbidden_stil_rate_units
        and segment_wait_plan not in {"auto", "every-boundary"}
        and global_wait_schedule_path is None
    ):
        raise SegmentedPackageError(
            "--forbid-stil-rate requires --segment-wait-plan auto/every-boundary or a prebuilt schedule "
            "that already carries the exclusion policy"
        )
    if segment_wait_plan in {"auto", "every-boundary"}:
        if mode not in {"auto", "rate-vector"}:
            raise SegmentedPackageError(
                "--segment-wait-plan auto/every-boundary requires --mode auto or --mode rate-vector"
            )
        if service_time_us is None:
            raise SegmentedPackageError(
                "--service-us is required with --segment-wait-plan auto/every-boundary"
            )
        if not service_time_source.strip():
            raise SegmentedPackageError(
                "--service-source is required with --segment-wait-plan auto/every-boundary"
            )
        if max_wait_us is None or max_wait_us <= 0:
            raise SegmentedPackageError(
                "--max-wait-us is required with --segment-wait-plan auto/every-boundary"
            )
        if max_total_wait_us is None or max_total_wait_us <= 0:
            raise SegmentedPackageError(
                "--max-total-wait-us is required with --segment-wait-plan auto/every-boundary"
            )

    if not ranges or ranges[0][0] != 0:
        raise SegmentedPackageError("segment ranges must start at global vector 0")
    if any(start < 0 or start > end for start, end in ranges):
        raise SegmentedPackageError("segment ranges must be inclusive and non-empty")
    if any(ranges[index][1] + 1 != ranges[index + 1][0] for index in range(len(ranges) - 1)):
        raise SegmentedPackageError("segment ranges must be contiguous")
    inferred_total_vectors = ranges[-1][1] + 1
    if total_vectors is None:
        total_vectors = inferred_total_vectors
    if total_vectors != inferred_total_vectors:
        raise SegmentedPackageError(
            f"segment ranges end at {inferred_total_vectors - 1}, expected source end {total_vectors - 1}"
        )
    if total_vectors < 2:
        raise SegmentedPackageError("segmented packaging requires at least two source vectors")
    if not variants:
        variants = (RateSweepVariant(name="nominal", overrides=()),)
    names = [variant.name for variant in variants]
    if len(set(names)) != len(names):
        raise SegmentedPackageError("variant names must be unique")
    if any(not name or Path(name).name != name or name in {".", ".."} for name in names):
        raise SegmentedPackageError("variant names must be safe single directory names")
    if output_dir.exists() and not output_dir.is_dir():
        raise SegmentedPackageError(f"output path is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise SegmentedPackageError(f"output directory must be empty: {output_dir}")

    source_identity = _identity(input_path)
    prepared_source = prepared_source or PreparedStilSource(
        input_path, pin_map_path=pin_map_path, pattern_burst=pattern_burst,
        pattern=pattern, loop_policy=loop_policy, retain_full_resolution=True)
    prepared_source.validate_binding(input_path, pin_map_path=pin_map_path,
                                      pattern_burst=pattern_burst, pattern=pattern, loop_policy=loop_policy)
    resolved_marker_specs = _resolve_segment_marker_specs(
        input_path,
        pin_map_path=pin_map_path,
        pattern_burst=pattern_burst,
        pattern=pattern,
        marker_specs=markers,
        prepared_source=prepared_source if loop_policy == "expand" else None,
    )
    capacity_plan: dict[str, Any] | None = None
    if capacity_first and not capacity_aware:
        raise SegmentedPackageError("capacity_first requires capacity_aware planning")
    if capacity_aware:
        max_segment_vector_count = None if capacity_first else max(end - start + 1 for start, end in ranges)
        ranges, capacity_plan = plan_capacity_aware_segment_ranges(
            input_path=input_path,
            total_vectors=total_vectors,
            max_segment_vector_count=max_segment_vector_count,
            mode=mode,
            tick_hz=tick_hz,
            pin_map_path=pin_map_path,
            pattern_burst=pattern_burst,
            pattern=pattern,
            strict=strict,
            allow_approx=allow_approx,
            prefer_raw=prefer_raw,
            runtime_profile=runtime_profile,
            max_rates=max_rates,
            loop_policy=loop_policy,
            markers=resolved_marker_specs,
            prepared_source=prepared_source,
            segment_wait_plan=segment_wait_plan,
            max_wait_us=max_wait_us,
            chunk_event_capacity=chunk_event_capacity,
            forbidden_stil_rate_units=forbidden_stil_rate_units,
        )
    prepared_source.release()
    global_wait_schedule_identity: dict[str, Any] | None = None
    if global_wait_schedule_path is not None:
        global_wait_schedule_identity = _file_identity(global_wait_schedule_path)
    report_variants: list[dict[str, Any]] = []
    for variant in variants:
        variant_dir = output_dir / variant.name
        variant_dir.mkdir()
        source_variant_path = input_path
        timing_report_path = variant_dir / "rate-variant.json"
        if variant.overrides:
            source_variant_path = variant_dir / "source.rate-variant.stil"
            timing_report = derive_rate_variant(
                input_path,
                source_variant_path,
                variant,
                tick_hz=tick_hz,
            )
        else:
            timing_report = _identity_variant_source(input_path)
        _write_json(timing_report_path, timing_report)
        # Prepare from this variant's selected source. A rate-modified variant
        # must not inherit preparation from the original or another variant.
        variant_prepared = PreparedStilSource(
            source_variant_path, pin_map_path=pin_map_path, pattern_burst=pattern_burst,
            pattern=pattern, loop_policy=loop_policy, retain_full_resolution=True)

        deferred_wait = global_wait_schedule_path is not None or segment_wait_plan in {
            "auto",
            "every-boundary",
        }
        segment_inputs: list[dict[str, Any]] = []
        global_event_cursor = 0
        for segment_id, (global_start, global_end) in enumerate(ranges):
            segment_dir = variant_dir / f"segment-{segment_id:04d}"
            package_dir = segment_dir / "package"
            source_package_dir = segment_dir / ".source-package"
            build_dir = source_package_dir if deferred_wait else package_dir
            try:
                segment_markers = tuple(
                    marker
                    for marker in resolved_marker_specs
                    if _marker_intersects_segment(
                        marker,
                        global_start=global_start,
                        global_end=global_end,
                    )
                )
                package_result = build_pattern_package(
                    PatternPackageOptions(
                        input_path=source_variant_path,
                        output_dir=build_dir,
                        mode=mode,
                        tick_hz=tick_hz,
                        pin_map_path=pin_map_path,
                        pattern_burst=pattern_burst,
                        pattern=pattern,
                        strict=strict,
                        allow_approx=allow_approx,
                        prefer_raw=prefer_raw,
                        vector_range=(global_start, global_end),
                        runtime_profile=runtime_profile,
                        max_rates=max_rates,
                        chunk_event_capacity=chunk_event_capacity,
                        ring_slot_count=ring_slot_count,
                        service_time_us=None if deferred_wait else service_time_us,
                        service_time_source=service_time_source,
                        safety_margin_us=safety_margin_us,
                        max_wait_us=max_wait_us,
                        max_total_wait_us=max_total_wait_us,
                        max_wait_count=max_wait_count,
                        pattern_time_limit_us=pattern_time_limit_us,
                        service_profile_scope=service_profile_scope,
                        loop_policy=loop_policy,
                        markers=segment_markers,
                        prepared_source=variant_prepared,
                    )
                )
                if package_result.get("status") != "VERIFIED":
                    raise PatternPackageError(
                        f"package status is not VERIFIED: {package_result.get('status')!r}"
                    )
                map_data = read_json_artifact(build_dir / "pattern.map.json")
                base_contract = _segment_pattern_contract(build_dir, map_data)
                del map_data
                segment_event_count = int(base_contract["estimated_event_count"])
                if segment_event_count <= 0:
                    raise SegmentedPackageError(
                        f"variant {variant.name} segment {segment_id} has no emitted events"
                    )
                segment_inputs.append(
                    {
                        "segment_id": segment_id,
                        "global_vector_start": global_start,
                        "global_vector_end": global_end,
                        "global_event_start": global_event_cursor,
                        "global_event_end": global_event_cursor + segment_event_count - 1,
                        "segment_dir": str(segment_dir),
                        "package_dir": str(package_dir),
                        "source_package_dir": str(build_dir),
                        **({"wait_capacity": capacity_plan["decisions"][segment_id]["metrics"]["wait_encoding"]}
                           if capacity_plan and capacity_plan.get("capacity_basis") == "every-boundary-encoded-waits"
                           and not variant.overrides else {}),
                    }
                )
                global_event_cursor += segment_event_count
            except (OSError, PatternPackageError, RateSweepError, ValueError) as exc:
                raise SegmentedPackageError(
                    f"variant {variant.name} segment {segment_id} failed: {exc}"
                ) from exc

        variant_prepared.release()
        if deferred_wait:
            for segment_input in segment_inputs:
                base_dir = Path(segment_input["source_package_dir"])
                segment_input["service_profile_scope"] = (
                    "loop-generic" if _has_loop_provenance(base_dir / "pattern.bin", base_dir / "pattern.map.json")
                    else "non-loop")
            expected_scope = ("loop-generic" if any(item["service_profile_scope"] == "loop-generic"
                                                   for item in segment_inputs) else "non-loop")
            if service_profile_scope != expected_scope:
                raise SegmentedPackageError(
                    f"service profile scope {service_profile_scope!r} does not match "
                    f"whole segmented sequence scope {expected_scope!r}")
        segment_wait_plan_identity: dict[str, Any] | None = None
        segment_wait_acceptance_identity: dict[str, Any] | None = None
        if global_wait_schedule_path is not None:
            for segment_input in segment_inputs:
                try:
                    _materialize_projected_segment_wait(
                        global_schedule_path=global_wait_schedule_path,
                        segment_input=segment_input,
                        service_time_us=service_time_us,
                        service_time_source=service_time_source,
                        safety_margin_us=safety_margin_us,
                        max_wait_us=max_wait_us,
                        max_total_wait_us=max_total_wait_us,
                        max_wait_count=max_wait_count,
                        pattern_time_limit_us=pattern_time_limit_us,
                        max_rates=max_rates,
                        service_profile_scope=service_profile_scope,
                        compact_sidecars=compact_sidecars,
                    )
                except (OSError, PatternPackageError, ValueError) as exc:
                    raise SegmentedPackageError(
                        f"segment {segment_input['segment_id']} wait materialization failed: {exc}"
                    ) from exc
        elif segment_wait_plan in {"auto", "every-boundary"}:
            try:
                aggregate_model, segment_chunk_ranges = _aggregate_segment_runtime_model(
                    segment_inputs,
                    chunk_event_capacity=chunk_event_capacity,
                    ring_slot_count=ring_slot_count,
                )
                schedule, acceptance = _build_segmented_wait_schedule(
                    aggregate_model,
                    source_identity=source_identity,
                    segment_chunk_ranges=segment_chunk_ranges,
                    max_rates=max_rates,
                    service_time_us=float(service_time_us),
                    service_time_source=service_time_source,
                    safety_margin_us=safety_margin_us,
                    max_wait_us=float(max_wait_us),
                    max_total_wait_us=float(max_total_wait_us),
                    max_wait_count=max_wait_count,
                    pattern_time_limit_us=pattern_time_limit_us,
                    wait_policy=segment_wait_plan,
                    forbidden_stil_rate_units=forbidden_stil_rate_units,
                    service_profile_scope=service_profile_scope,
                )
            except (OSError, RuntimeWaitModelError, ValueError) as exc:
                raise SegmentedPackageError(f"automatic segmented wait planning failed: {exc}") from exc
            plan_path = variant_dir / "segment.wait.plan.json"
            acceptance_path = variant_dir / "segment.wait.plan.acceptance.json"
            plan_path = _write_json(plan_path, schedule, compact=compact_sidecars)
            acceptance_path = _write_json(acceptance_path, acceptance, compact=compact_sidecars)
            if acceptance.get("status") != "ACCEPTED":
                raise SegmentedPackageError(
                    "automatic segmented wait plan was not accepted: "
                    + "; ".join(acceptance.get("errors", []))
                )
            segment_wait_plan_identity = _identity(plan_path)
            segment_wait_acceptance_identity = _identity(acceptance_path)
            for segment_input, segment_chunk_range in zip(segment_inputs, segment_chunk_ranges):
                segment_input["global_event_start"] = int(segment_chunk_range["global_event_start"])
                segment_input["global_event_end"] = int(segment_chunk_range["global_event_end"])
                try:
                    _materialize_projected_segment_wait(
                        global_schedule_path=plan_path,
                        segment_input=segment_input,
                        service_time_us=service_time_us,
                        service_time_source=service_time_source,
                        safety_margin_us=safety_margin_us,
                        max_wait_us=max_wait_us,
                        max_total_wait_us=max_total_wait_us,
                        max_wait_count=max_wait_count,
                        pattern_time_limit_us=pattern_time_limit_us,
                        max_rates=max_rates,
                        service_profile_scope=service_profile_scope,
                        compact_sidecars=compact_sidecars,
                    )
                except (OSError, PatternPackageError, ValueError) as exc:
                    raise SegmentedPackageError(
                        f"segment {segment_input['segment_id']} wait materialization failed: {exc}"
                    ) from exc

        segment_records: list[dict[str, Any]] = []
        global_event_cursor = 0
        previous_final: dict[str, Any] | None = None
        for segment_input in segment_inputs:
            segment_id = int(segment_input["segment_id"])
            global_start = int(segment_input["global_vector_start"])
            global_end = int(segment_input["global_vector_end"])
            package_dir = Path(segment_input["package_dir"])
            map_path = resolve_json_artifact(package_dir / "pattern.map.json")
            wait_path = resolve_json_artifact(package_dir / "pattern.wait.json")
            map_data = read_json_artifact(map_path)
            entry_hold = _zero_hold() if previous_final is None else previous_final
            pattern_bytes = (package_dir / "pattern.bin").read_bytes()
            final_hold = _hold_pair(*derive_terminal_hold(
                pattern_bytes,
                int(entry_hold["drive_value"], 0),
                int(entry_hold["drive_enable"], 0),
            ))
            pattern_contract = _segment_pattern_contract(
                package_dir, map_data, pattern_bytes=pattern_bytes
            )
            segment_event_count = int(pattern_contract["estimated_event_count"])
            if segment_event_count <= 0:
                raise SegmentedPackageError(
                    f"variant {variant.name} segment {segment_id} has no emitted events"
                )
            segment_records.append(
                {
                    "segment_id": segment_id,
                    "global_vector_start": global_start,
                    "global_vector_end": global_end,
                    "global_event_start": global_event_cursor,
                    "global_event_end": global_event_cursor + segment_event_count - 1,
                    "path": _relative_posix(package_dir / "pattern.bin", variant_dir,
                                            field=f"segment[{segment_id}].path"),
                    "map_path": _relative_posix(map_path, variant_dir,
                                                field=f"segment[{segment_id}].map_path"),
                    "package_manifest_path": _relative_posix(
                        package_dir / "manifest.json", variant_dir,
                        field=f"segment[{segment_id}].package_manifest_path"),
                    "pattern": {
                        "sha256": _identity(package_dir / "pattern.bin")["sha256"],
                        "size_bytes": _identity(package_dir / "pattern.bin")["size_bytes"],
                        **pattern_contract,
                    },
                    "entry_hold_state": entry_hold,
                    "final_hold_state": final_hold,
                    "wait_schedule": {
                        "path": _relative_posix(wait_path, variant_dir,
                                                field=f"segment[{segment_id}].wait_schedule.path"),
                        "sha256": _identity(wait_path)["sha256"],
                        "recalculated_from_segment_binary": True,
                        "hidden_wait_insertion": False,
                        "derived_from_global_schedule": global_wait_schedule_identity,
                        "derived_from_segment_wait_plan": segment_wait_plan_identity,
                    },
                    "physical_execution_authorized": False,
                    **({"service_profile": {"declared_sequence_scope": service_profile_scope,
                        "segment_scope": segment_input["service_profile_scope"]}} if deferred_wait else {}),
                }
            )
            global_event_cursor += segment_event_count
            previous_final = final_hold

        contract_fields = ("mode", "tick_hz", "active_pins")
        for field in contract_fields:
            values = {segment["pattern"].get(field) for segment in segment_records}
            if len(values) != 1:
                raise SegmentedPackageError(
                    f"variant {variant.name} has inconsistent segment pattern {field}: {sorted(values, key=str)}"
                )

        manifest_path = variant_dir / "segmented.manifest.json"
        segment_patterns = [segment["pattern"] for segment in segment_records]
        max_encoded_bytes = max(int(segment["size_bytes"]) for segment in segment_patterns)
        pattern_contract = {
            "segment_count": len(segment_records),
            "source_vector_count": total_vectors,
            "logical_vector_count": sum(
                int(segment.get("expanded_logical_vector_count", segment.get("vector_count", 0)))
                for segment in segment_patterns
            ),
            "expanded_logical_vector_count": sum(
                int(segment.get("expanded_logical_vector_count", segment.get("vector_count", 0)))
                for segment in segment_patterns
            ),
            "expanded_event_count": sum(
                int(segment.get("estimated_event_count", 0)) for segment in segment_patterns
            ),
            "expanded_sample_count": sum(
                int(segment.get("estimated_sample_count", 0)) for segment in segment_patterns
            ),
            "expanded_compare_count": sum(
                int(segment.get("estimated_compare_count", 0)) for segment in segment_patterns
            ),
            "encoded_pattern_bytes": max_encoded_bytes,
            "total_encoded_pattern_bytes": sum(
                int(segment["size_bytes"]) for segment in segment_patterns
            ),
            "max_segment_encoded_pattern_bytes": max_encoded_bytes,
            "encoded_record_count": sum(
                int(segment.get("encoded_record_count", 0)) for segment in segment_patterns
            ),
            "top_level_entry_count": sum(
                int(segment.get("top_level_entry_count", 0)) for segment in segment_patterns
            ),
            "loop_block_count": sum(
                int(segment.get("loop_block_count", 0)) for segment in segment_patterns
            ),
            "loop_body_record_count": sum(
                int(segment.get("loop_body_record_count", 0)) for segment in segment_patterns
            ),
            "loop_count": sum(int(segment.get("loop_count", 0)) for segment in segment_patterns),
            "global_vector_start": ranges[0][0],
            "global_vector_end": ranges[-1][1],
            "global_event_end": global_event_cursor - 1,
            "split_ranges": [list(item) for item in ranges],
            "mode": segment_records[0]["pattern"]["mode"],
            "tick_hz": segment_records[0]["pattern"]["tick_hz"],
            "active_pins": segment_records[0]["pattern"]["active_pins"],
        }
        manifest = _segment_manifest(
            path=manifest_path,
            variant_name=variant.name,
            source_identity=_identity(source_variant_path),
            segments=segment_records,
            pattern_contract=pattern_contract,
            global_wait_schedule=global_wait_schedule_identity,
            segment_wait_plan=segment_wait_plan_identity,
            segment_wait_plan_acceptance=segment_wait_acceptance_identity,
            capacity_plan=capacity_plan,
            service_profile_scope=(
                service_profile_scope
                if global_wait_schedule_path is not None or segment_wait_plan is not None
                else None
            ),
        )
        _write_json(manifest_path, manifest)
        report_variants.append(
            {
                "name": variant.name,
                "status": "VERIFIED",
                "timing_report": _identity(timing_report_path),
                "derived_source": _identity(source_variant_path),
                "manifest": _identity(manifest_path),
                "segments": segment_records,
                "segment_wait_plan": segment_wait_plan_identity,
                "segment_wait_plan_acceptance": segment_wait_acceptance_identity,
                "physical_execution_authorized": False,
            }
        )

    report = {
        "kind": SEGMENTED_PACKAGE_KIND,
        "schema_version": SEGMENTED_PACKAGE_SCHEMA_VERSION,
        "status": "COMPLETE",
        "source": source_identity,
        "source_vector_count": total_vectors,
        "segment_ranges": [list(item) for item in ranges],
        "capacity_plan": capacity_plan,
        "variants": report_variants,
        "policies": {
            "variant_then_segment_order": True,
            "global_vector_index_is_report_authority": True,
            "independent_segment_package": True,
            "segment_wait_plan": segment_wait_plan,
            "segment_wait_plan_mode": (
                "external-schedule" if global_wait_schedule_path is not None else segment_wait_plan
            ),
            "service_profile_scope": service_profile_scope,
            "physical_execution_authorized": False,
        },
        "limitations": [
            "host-only preparation does not validate electrical hold/resume behavior",
            "AC-safe stopping at a boundary requires user confirmation",
            "firmware hold/resume support is implemented only by a separately admitted compatible build",
            "compare/sample results are segment-local and must be collected before the next upload",
            "Rate Vector segmented physical execution requires a separately approved compatible firmware candidate",
            "automatic segmented wait planning is a bounded host-only model and is not physical approval",
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(report_path, report, compact=compact_sidecars)
    return report
