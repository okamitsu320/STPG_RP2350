from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from contextlib import nullcontext
from typing import Any, Callable

from stpg.client import StpgClient
from stpg.constants import (
    ALLOW_RESULT_READ,
    CAP_STREAM_FAIL_STOP,
    ERR_COMPARE_FAIL_STOP,
    STPG_FAIL_LOG_MAX,
    STPG_MAX_EVENT_COUNT,
    RSP_OK,
    STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW,
    STPG_RESULT_FLAG_SAMPLE_OVERFLOW,
    STATE_DONE,
    STATE_ERROR,
    STATE_HOLD_AWAITING_NEXT,
    STATE_READY,
    STORAGE_PSRAM,
)
from .device_precheck import inspect_pattern_requirements
from .hold_state import derive_terminal_hold
from .json_artifacts import read_json_artifact
from .run_progress import RunProgress


@dataclass(frozen=True)
class HoldState:
    drive_value: int
    drive_enable: int


@dataclass(frozen=True)
class SegmentSpec:
    path: Path
    entry_hold_state: HoldState
    final_hold_state: HoldState | None
    segment_id: int
    global_vector_start: int | None
    global_vector_end: int | None
    global_event_start: int | None
    global_event_end: int | None
    map_path: Path | None
    expected_event_count: int | None
    expected_sample_count: int | None
    expected_compare_count: int | None


@dataclass(frozen=True)
class SegmentedManifest:
    segments: tuple[SegmentSpec, ...]


@dataclass(frozen=True)
class SegmentedArtifacts:
    merged_map: dict[str, Any] | None
    merged_fail_json: dict[str, Any]


def _file_identity(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}


def _check_pinned_identity(
    actual: dict[str, Any],
    expected: object,
    *,
    label: str,
    errors: list[str],
    require_size: bool = False,
) -> None:
    if not isinstance(expected, dict):
        errors.append(f"{label} is missing a pinned identity")
        return
    fields = ("size_bytes", "sha256") if require_size else ("sha256",)
    for field in fields:
        if expected.get(field) != actual.get(field):
            errors.append(
                f"{label} {field} mismatch: manifest={expected.get(field)!r}, "
                f"actual={actual.get(field)!r}"
            )


def verify_segmented_manifest(path: Path) -> dict[str, Any]:
    """Verify a segmented manifest and every package before device access.

    This is intentionally a host-only gate for the generic segmented runner.
    It verifies the segment binary identities recorded in the segmented
    manifest, derives each final HOLD from its binary and entry state, and
    delegates per-segment artifact/hash/format checks to
    ``verify_pattern_package``.  It never opens a transport and never turns
    ``physical_execution_authorized`` on.
    """

    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}
    try:
        data = read_json_artifact(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "kind": "stpg_stil_segmented_package_validation",
            "schema_version": "v1",
            "status": "REJECT",
            "errors": [f"cannot read segmented manifest: {exc}"],
            "warnings": [],
            "checks": {},
            "physical_execution_authorized": False,
        }
    if not isinstance(data, dict):
        errors.append("segmented manifest root must be an object")
        data = {}

    checks["kind_and_schema"] = {
        "status": "PASS"
        if data.get("kind") == "stpg_stil_segmented_package.manifest"
        and data.get("schema_version") == 1
        else "FAIL",
        "detail": f"kind={data.get('kind')!r} schema_version={data.get('schema_version')!r}",
    }
    if checks["kind_and_schema"]["status"] == "FAIL":
        errors.append("segmented manifest kind or schema_version is unsupported")
    if data.get("status") not in {"VERIFIED", "HOST_ONLY_VERIFIED"}:
        errors.append(f"segmented manifest status is not host-verified: {data.get('status')!r}")
    if data.get("physical_execution_authorized") not in {None, False}:
        errors.append("segmented manifest must not authorize physical execution")
    policies = data.get("policies")
    if not isinstance(policies, dict):
        errors.append("segmented manifest policies are required")
    elif policies.get("physical_execution_authorized") is not False:
        errors.append("segmented manifest policies must set physical_execution_authorized=false")

    raw_segments = data.get("segments")
    if not isinstance(raw_segments, list) or len(raw_segments) < 2:
        errors.append("segmented manifest must contain at least 2 segments")
        raw_segments = []
    try:
        manifest = load_manifest(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        manifest = None
        errors.append(f"segmented manifest structure is invalid: {exc}")

    segment_reports: list[dict[str, Any]] = []
    scope_semantics = policies.get("service_scope_semantics") if isinstance(policies, dict) else None
    observed_scopes: list[str] = []
    if scope_semantics not in (None, "sequence_union_v1"):
        errors.append("unsupported segmented service scope semantics")
    if manifest is not None and len(raw_segments) == len(manifest.segments):
        from .pattern_package import PATTERN_NAME, verify_pattern_package

        for index, (segment, raw) in enumerate(zip(manifest.segments, raw_segments)):
            segment_errors: list[str] = []
            try:
                pattern_identity = _file_identity(segment.path)
                _check_pinned_identity(
                    pattern_identity,
                    raw.get("pattern"),
                    label=f"segment[{index}].pattern",
                    errors=segment_errors,
                    require_size=True,
                )
            except (OSError, ValueError) as exc:
                pattern_identity = {"path": str(segment.path)}
                segment_errors.append(str(exc))

            hold_check: dict[str, Any] = {"status": "FAIL"}
            try:
                if index == 0 and segment.entry_hold_state != HoldState(0, 0):
                    raise ValueError("first segment entry HOLD must be zero for a fresh run")
                value, enable = derive_terminal_hold(
                    segment.path.read_bytes(),
                    segment.entry_hold_state.drive_value,
                    segment.entry_hold_state.drive_enable,
                )
                hold_check["derived_final"] = {"drive_value": value, "drive_enable": enable}
                if segment.final_hold_state != HoldState(value, enable):
                    raise ValueError(
                        "final_hold_state disagrees with binary operations and entry_hold_state; "
                        "regenerate the segmented package (do not patch a pinned manifest)"
                    )
                hold_check["status"] = "PASS"
            except (OSError, ValueError) as exc:
                segment_errors.append(f"segment[{index}] HOLD: {exc}")

            package_manifest_raw = raw.get("package_manifest_path")
            if not isinstance(package_manifest_raw, str) or not package_manifest_raw:
                segment_errors.append(f"segment[{index}] package_manifest_path is required")
                package_dir = segment.path.parent
                package_verification = {"status": "REJECT", "errors": ["missing package manifest"]}
            else:
                package_manifest_path = (path.parent / package_manifest_raw).resolve()
                package_dir = package_manifest_path.parent
                if (package_dir / PATTERN_NAME).resolve() != segment.path:
                    segment_errors.append(
                        f"segment[{index}] pattern path does not match its package manifest"
                    )
                package_verification = verify_pattern_package(package_dir)
                if package_verification.get("status") != "PASS":
                    segment_errors.extend(
                        f"package: {item}" for item in package_verification.get("errors", [])
                    )

            wait_descriptor = raw.get("wait_schedule")
            if scope_semantics == "sequence_union_v1":
                from .wait_acceptance import _has_loop_provenance
                try:
                    local_scope = ("loop-generic" if _has_loop_provenance(segment.path, segment.map_path)
                                   else "non-loop")
                    observed_scopes.append(local_scope)
                    expected_profile = {"declared_sequence_scope": policies.get("service_profile_scope"),
                                        "segment_scope": local_scope}
                    if raw.get("service_profile") != expected_profile:
                        raise ValueError("segment service scope differs from actual provenance/sequence")
                    acceptance = read_json_artifact(package_dir / "pattern.wait.acceptance.json")
                    if (acceptance.get("status") != "ACCEPTED" or acceptance.get("service_profile_scope") != local_scope
                            or acceptance.get("segmented_service_profile") != {**expected_profile, "segment_id": segment.segment_id}):
                        raise ValueError("segment wait acceptance lacks its sequence/local scope binding")
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    segment_errors.append(f"segment[{index}] service profile: {exc}")
            if not isinstance(wait_descriptor, dict):
                segment_errors.append(f"segment[{index}] wait_schedule is required")
            else:
                wait_raw = wait_descriptor.get("path")
                if not isinstance(wait_raw, str) or not wait_raw:
                    segment_errors.append(f"segment[{index}] wait_schedule.path is invalid")
                else:
                    wait_path = (path.parent / wait_raw).resolve()
                    try:
                        wait_identity = _file_identity(wait_path)
                        _check_pinned_identity(
                            wait_identity,
                            wait_descriptor,
                            label=f"segment[{index}].wait_schedule",
                            errors=segment_errors,
                        )
                    except (OSError, ValueError) as exc:
                        segment_errors.append(str(exc))

            if raw.get("physical_execution_authorized") is not False:
                segment_errors.append(
                    f"segment[{index}] must set physical_execution_authorized=false"
                )
            segment_reports.append(
                {
                    "segment_id": segment.segment_id,
                    "status": "PASS" if not segment_errors else "REJECT",
                    "pattern": pattern_identity,
                    "package_dir": str(package_dir),
                    "package_verification_status": package_verification.get("status"),
                    "hold_state_verification": hold_check,
                    "errors": segment_errors,
                }
            )
            errors.extend(segment_errors)

    if scope_semantics == "sequence_union_v1":
        expected_scope = "loop-generic" if "loop-generic" in observed_scopes else "non-loop"
        if policies.get("service_profile_scope") != expected_scope:
            errors.append("whole segmented sequence service scope differs from actual provenance")
    checks["segments"] = {
        "status": "PASS" if segment_reports and not errors else "FAIL",
        "segment_count": len(segment_reports),
    }
    return {
        "kind": "stpg_stil_segmented_package_validation",
        "schema_version": "v1",
        "status": "PASS" if not errors else "REJECT",
        "input": {"manifest": _file_identity(path) if path.is_file() else {"path": str(path)}},
        "checks": checks,
        "segments": segment_reports,
        "errors": errors,
        "warnings": warnings,
        "physical_execution_authorized": False,
        "limitations": [
            "this validation is host-only and does not verify electrical hold/resume behavior",
            "validation does not authorize physical execution or replace a candidate static gate",
        ],
    }


def _parse_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError(f"invalid integer field: {value!r}")


def _parse_hold_state(data: dict[str, object] | None, *, default_zero: bool = False) -> HoldState:
    if data is None:
        if default_zero:
            return HoldState(0, 0)
        raise ValueError("missing hold state")
    return HoldState(
        drive_value=_parse_int(data["drive_value"]),
        drive_enable=_parse_int(data["drive_enable"]),
    )


def _validate_global_event_ranges(segments: list[SegmentSpec]) -> None:
    """Validate the optional expanded-event mapping when a manifest carries it.

    Raw Event manifests produced before event-range metadata was introduced do
    not have these fields.  Once any segment declares an event range, every
    segment must declare one and the ranges must form one zero-based,
    inclusive frontier.  Falling back to a vector offset for an event index
    would silently corrupt compare/fail-log globalization.
    """

    has_event_ranges = any(
        segment.global_event_start is not None or segment.global_event_end is not None
        for segment in segments
    )
    if not has_event_ranges:
        return

    expected_start = 0
    for index, segment in enumerate(segments):
        if segment.global_event_start is None or segment.global_event_end is None:
            raise ValueError(f"segment {index} must define global_event_start and global_event_end")
        if segment.global_event_start != expected_start:
            raise ValueError(
                f"segment {index} global event range starts at {segment.global_event_start}, "
                f"expected {expected_start}"
            )
        if segment.global_event_end < segment.global_event_start:
            raise ValueError(f"segment {index} global event range is empty or reversed")
        expected_start = segment.global_event_end + 1


def load_manifest(path: Path) -> SegmentedManifest:
    data = read_json_artifact(path)
    raw_segments = data.get("segments")
    if not isinstance(raw_segments, list) or len(raw_segments) < 2:
        raise ValueError("segmented manifest must contain at least 2 segments")

    segments: list[SegmentSpec] = []
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            raise ValueError(f"segment {index} must be an object")
        entry = _parse_hold_state(raw.get("entry_hold_state"), default_zero=(index == 0))
        final_raw = raw.get("final_hold_state")
        final = _parse_hold_state(final_raw) if isinstance(final_raw, dict) else None
        segments.append(
            SegmentSpec(
                path=(path.parent / str(raw["path"])).resolve(),
                entry_hold_state=entry,
                final_hold_state=final,
                segment_id=int(raw.get("segment_id", index)),
                global_vector_start=_parse_int(raw["global_vector_start"]) if "global_vector_start" in raw else None,
                global_vector_end=_parse_int(raw["global_vector_end"]) if "global_vector_end" in raw else None,
                global_event_start=_parse_int(raw["global_event_start"]) if "global_event_start" in raw else None,
                global_event_end=_parse_int(raw["global_event_end"]) if "global_event_end" in raw else None,
                map_path=((path.parent / str(raw["map_path"])).resolve() if "map_path" in raw else None),
                expected_event_count=(
                    _parse_int(raw["event_count"])
                    if "event_count" in raw
                    else _parse_int(raw["pattern"]["event_count"])
                    if isinstance(raw.get("pattern"), dict) and "event_count" in raw["pattern"]
                    else None
                ),
                expected_sample_count=(
                    _parse_int(raw["estimated_sample_count"])
                    if "estimated_sample_count" in raw
                    else _parse_int(raw["pattern"]["estimated_sample_count"])
                    if isinstance(raw.get("pattern"), dict) and "estimated_sample_count" in raw["pattern"]
                    else None
                ),
                expected_compare_count=(
                    _parse_int(raw["estimated_compare_count"])
                    if "estimated_compare_count" in raw
                    else _parse_int(raw["pattern"]["estimated_compare_count"])
                    if isinstance(raw.get("pattern"), dict) and "estimated_compare_count" in raw["pattern"]
                    else None
                ),
            )
        )

    _validate_global_event_ranges(segments)
    for index in range(len(segments) - 1):
        current = segments[index]
        nxt = segments[index + 1]
        if current.final_hold_state is None:
            raise ValueError(f"segment {index} must define final_hold_state")
        if current.final_hold_state != nxt.entry_hold_state:
            raise ValueError(f"segment {index + 1} entry_hold_state must match segment {index} final_hold_state")
    return SegmentedManifest(segments=tuple(segments))


def globalize_fail_entries(entries: list[dict[str, Any]], segment: SegmentSpec) -> list[dict[str, Any]]:
    globalized: list[dict[str, Any]] = []
    for entry in entries:
        item = dict(entry)
        if "event_index" in item:
            local_event_index = int(item["event_index"])
            if segment.global_event_start is not None and segment.global_event_end is not None:
                if local_event_index < 0 or segment.global_event_start + local_event_index > segment.global_event_end:
                    raise ValueError(
                        f"event index {local_event_index} is outside segment {segment.segment_id} event range"
                    )
                item["event_index"] = segment.global_event_start + local_event_index
                item["local_event_index"] = local_event_index
            elif segment.global_vector_start is not None:
                # Legacy manifests had no event range.  Keep the old field
                # usable, but make the lack of an event mapping explicit.
                item["event_index"] = segment.global_vector_start + local_event_index
                item["local_event_index"] = local_event_index
        else:
            local_vector_index = int(item["vector_index"])
            if segment.global_vector_start is not None:
                item["vector_index"] = segment.global_vector_start + local_vector_index
                item["local_vector_index"] = local_vector_index
        item["segment_id"] = segment.segment_id
        globalized.append(item)
    return globalized


def load_segment_map_payloads(manifest: SegmentedManifest) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for segment in manifest.segments:
        if segment.map_path is None:
            raise ValueError("every segment must define map_path to build a merged map")
        payloads.append(read_json_artifact(segment.map_path))
    return payloads


def merge_segment_maps(manifest: SegmentedManifest, map_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    if len(map_payloads) != len(manifest.segments):
        raise ValueError("map payload count must match segment count")
    if not map_payloads:
        raise ValueError("at least one map payload is required")

    merged = deepcopy(map_payloads[0])
    merged["segmented"] = True
    merged["segment_count"] = len(map_payloads)
    merged["vectors"] = {}
    merged["segments"] = []

    for segment, payload in zip(manifest.segments, map_payloads):
        merged["segments"].append(
            {
                "segment_id": segment.segment_id,
                "global_vector_start": segment.global_vector_start,
                "global_vector_end": segment.global_vector_end,
            }
        )
        for vector_key, vector_payload in payload.get("vectors", {}).items():
            local_vector_index = int(vector_key)
            output_key = vector_key
            source_map = deepcopy(vector_payload.get("source_map", []))
            if segment.global_vector_start is not None:
                output_key = str(segment.global_vector_start + local_vector_index)
                for source in source_map:
                    source["local_vector_index"] = local_vector_index
                    source["segment_id"] = segment.segment_id
                    source["global_vector_index"] = segment.global_vector_start + local_vector_index
            rewritten = deepcopy(vector_payload)
            rewritten["source_map"] = source_map
            if output_key in merged["vectors"]:
                raise ValueError(f"duplicate global vector index {output_key} while merging segment maps")
            merged["vectors"][output_key] = rewritten

    return merged


def merge_segment_fail_json(manifest: SegmentedManifest, fail_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    if len(fail_payloads) != len(manifest.segments):
        raise ValueError("fail payload count must match segment count")

    merged_entries: list[dict[str, Any]] = []
    segments_meta: list[dict[str, Any]] = []
    for segment, payload in zip(manifest.segments, fail_payloads):
        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            raise ValueError("segment fail payload entries must be a list")
        merged_entries.extend(globalize_fail_entries(entries, segment))
        segments_meta.append(
            {
                "segment_id": segment.segment_id,
                "global_vector_start": segment.global_vector_start,
                "global_vector_end": segment.global_vector_end,
                "global_event_start": segment.global_event_start,
                "global_event_end": segment.global_event_end,
                "entry_count": len(entries),
            }
        )

    return {
        "segmented": True,
        "segment_count": len(manifest.segments),
        "segments": segments_meta,
        "entries": merged_entries,
    }


def build_segmented_artifacts(
    manifest: SegmentedManifest,
    *,
    map_payloads: list[dict[str, Any]] | None = None,
    fail_payloads: list[dict[str, Any]] | None = None,
    resolve_fail_sources: bool = False,
) -> SegmentedArtifacts:
    merged_map = merge_segment_maps(manifest, map_payloads) if map_payloads is not None else None
    if fail_payloads is None:
        normalized_fail_payloads = [{"entries": []} for _ in manifest.segments]
    else:
        if len(fail_payloads) > len(manifest.segments):
            raise ValueError("fail payload count cannot exceed segment count")
        # A terminal underflow may leave only a prefix of the segment results.
        # Preserve that evidence while keeping the legacy merged-fail schema
        # well-formed for its full manifest geometry.
        normalized_fail_payloads = [*fail_payloads]
        normalized_fail_payloads.extend(
            {"entries": []} for _ in range(len(manifest.segments) - len(normalized_fail_payloads))
        )
    merged_fail_json = merge_segment_fail_json(
        manifest,
        normalized_fail_payloads,
    )
    if resolve_fail_sources and merged_fail_json["entries"]:
        if map_payloads is None:
            raise ValueError("exact segment maps are required to resolve fail sources")
        for segment, map_payload in zip(manifest.segments, map_payloads):
            entries = [e for e in merged_fail_json["entries"] if e["segment_id"] == segment.segment_id]
            resolve_segment_fail_sources(segment, map_payload, entries)
    return SegmentedArtifacts(merged_map=merged_map, merged_fail_json=merged_fail_json)


def resolve_segment_fail_sources(
    segment: SegmentSpec, map_payload: dict[str, Any], entries: list[dict[str, Any]],
) -> None:
    """Resolve retained entries only; hash the executed binary once per segment."""
    if not entries:
        return
    if map_payload.get("mode_emitted") != "rate-vector":
        raise ValueError("segmented fail-source reconstruction requires rate-vector maps")
    from .report import _runtime_event_lookup, _source_map_for_runtime_event
    payload = segment.path.read_bytes()
    events = _runtime_event_lookup(payload, {e["local_event_index"] for e in entries})
    pattern_sha256 = hashlib.sha256(payload).hexdigest()
    for entry in entries:
        event = events[entry["local_event_index"]]
        source_index, source = _source_map_for_runtime_event(event, map_payload)
        entry["resolved_source_map"] = source
        entry["source_record_index"] = source_index
        entry["group_index"] = event.group_index
        entry["pattern_sha256"] = pattern_sha256


def _stage(progress: RunProgress | None, name: str, **details: Any):
    return progress.stage(name, **details) if progress is not None else nullcontext()


def _status_dict(status: object) -> dict[str, Any]:
    return {
        field: getattr(status, field)
        for field in (
            "rsp",
            "state",
            "error_code",
            "pattern_size",
            "event_count",
            "run_progress",
            "result_flags",
            "fail_count",
            "sample_count",
            "allowed_cmds",
        )
        if hasattr(status, field)
    }


def _sample_element_size(status: object) -> int:
    active_pins = int(getattr(status, "active_pins", 0))
    # Older test doubles and legacy status records omit active_pins.  The
    # current 24-pin compare/sample contract uses four-byte words; an explicit
    # 16-pin status is the only two-byte case.
    return 2 if active_pins == 16 else 4


def _validate_segment_result_read_permission(status: object, segment: SegmentSpec) -> None:
    if hasattr(status, "allowed_cmds") and not (int(status.allowed_cmds) & ALLOW_RESULT_READ):
        raise ValueError(f"segment {segment.segment_id} result reads are not allowed in this state")


def _validate_segment_result_counts(
    segment: SegmentSpec,
    sample_info: object,
    fail_summary: object,
    *,
    allow_partial_samples: bool = False,
) -> None:
    if segment.expected_sample_count is not None and not allow_partial_samples:
        observed = int(getattr(sample_info, "sample_count", 0))
        if observed != segment.expected_sample_count:
            raise ValueError(
                f"segment {segment.segment_id} sample count {observed} != "
                f"expected {segment.expected_sample_count}"
            )
    result_flags = int(getattr(sample_info, "result_flags", 0)) | int(
        getattr(fail_summary, "result_flags", 0)
    )
    if result_flags & STPG_RESULT_FLAG_SAMPLE_OVERFLOW:
        raise ValueError(f"segment {segment.segment_id} sample result overflow is set")
    if segment.expected_compare_count is not None:
        # The firmware fail summary exposes the actual mismatch count, not the
        # number of compare points.  Compare-point cardinality is retained as
        # manifest metadata and is checked by candidate-specific acceptance.
        if segment.expected_compare_count < 0:
            raise ValueError(f"segment {segment.segment_id} compare count is negative")
    if int(getattr(fail_summary, "fail_count", 0)) < 0:
        raise ValueError(f"segment {segment.segment_id} fail count is negative")


def _validate_segment_upload(response: object, payload: bytes, segment: SegmentSpec) -> None:
    if response is None:
        return
    if hasattr(response, "rsp") and int(response.rsp) != RSP_OK:
        raise ValueError(f"segment {segment.segment_id} upload response is not RSP_OK")
    if hasattr(response, "error_code") and int(response.error_code) != 0:
        raise ValueError(f"segment {segment.segment_id} upload returned error={response.error_code}")
    if hasattr(response, "pattern_size") and int(response.pattern_size) != len(payload):
        raise ValueError(
            f"segment {segment.segment_id} upload size {response.pattern_size} != {len(payload)}"
        )
    if segment.expected_event_count is not None and hasattr(response, "event_count"):
        if int(response.event_count) != segment.expected_event_count:
            raise ValueError(
                f"segment {segment.segment_id} upload event count {response.event_count} != "
                f"expected {segment.expected_event_count}"
            )


def _fail_entry_dict(entry: object) -> dict[str, int]:
    return {
        "event_index": int(getattr(entry, "event_index")),
        "actual": int(getattr(entry, "actual")),
        "expected": int(getattr(entry, "expected")),
        "mask": int(getattr(entry, "mask")),
    }


def _collect_segment_results(
    client: StpgClient,
    segment: SegmentSpec,
    status: object,
    *,
    allow_partial_samples: bool = False,
    progress: RunProgress | None = None,
) -> dict[str, Any]:
    """Read a segment's results before the next upload clears them.

    Firmware result counters and fail-log indices are segment-local.  The
    returned payload deliberately carries both the local and manifest ranges;
    ``merge_segment_fail_json`` performs the global event-index translation.
    """

    _validate_segment_result_read_permission(status, segment)
    with _stage(progress, "sample_info", segment_id=segment.segment_id):
        sample_info = client.sample_info()
    sample_count = int(getattr(sample_info, "sample_count", 0))
    sample_element_size = _sample_element_size(sample_info)
    with _stage(progress, "sample_read", segment_id=segment.segment_id,
                byte_count=sample_count * sample_element_size):
        sample_payload = client.read_samples(0, sample_count * sample_element_size) if sample_count else b""
    with _stage(progress, "fail_summary", segment_id=segment.segment_id):
        fail_summary = client.fail_summary()
    _validate_segment_result_counts(
        segment,
        sample_info,
        fail_summary,
        allow_partial_samples=allow_partial_samples,
    )
    fail_count = int(getattr(fail_summary, "fail_count", 0))
    stored_fail_count = min(fail_count, STPG_FAIL_LOG_MAX)
    with _stage(progress, "fail_log_read", segment_id=segment.segment_id,
                entry_count=stored_fail_count):
        fail_entries = client.read_fail_log(0, stored_fail_count) if stored_fail_count else []
    if len(fail_entries) != stored_fail_count:
        raise ValueError(
            f"segment {segment.segment_id} FAIL_LOG returned {len(fail_entries)} entries, "
            f"expected {stored_fail_count}"
        )
    if len(sample_payload) != sample_count * sample_element_size:
        raise ValueError(
            f"segment {segment.segment_id} sample payload length {len(sample_payload)} "
            f"does not match sample_count={sample_count} and element_size={sample_element_size}"
        )
    result_flags = int(getattr(fail_summary, "result_flags", 0)) | int(getattr(sample_info, "result_flags", 0))
    fail_log_truncated = fail_count > stored_fail_count or bool(result_flags & STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW)
    return {
        "segment_id": segment.segment_id,
        "result_reads_performed": True,
        "status": _status_dict(status),
        "sample_info": _status_dict(sample_info),
        "fail_summary": _status_dict(fail_summary),
        "sample_count": sample_count,
        "sample_element_size_bytes": sample_element_size,
        "sample_payload_hex": sample_payload.hex(),
        "fail_count": fail_count,
        "stored_fail_entry_count": len(fail_entries),
        "fail_log_truncated": fail_log_truncated,
        "fail_log_read_complete": len(fail_entries) == stored_fail_count,
        "result_flags": result_flags,
        "entries": [_fail_entry_dict(entry) for entry in fail_entries],
    }


def merge_segment_results(
    manifest: SegmentedManifest,
    segment_results: list[dict[str, Any]],
    *,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Merge segment-local result metadata without hiding truncated logs."""

    if not segment_results:
        raise ValueError("at least one segment result is required")
    if len(segment_results) > len(manifest.segments):
        raise ValueError("segment result count cannot exceed manifest segment count")
    if not allow_partial and len(segment_results) != len(manifest.segments):
        raise ValueError("segment result count must match manifest segment count")
    expected_ids = [segment.segment_id for segment in manifest.segments]
    actual_ids = [int(item.get("segment_id", -1)) for item in segment_results]
    if actual_ids != expected_ids[: len(segment_results)]:
        raise ValueError(f"segment result IDs {actual_ids} do not match manifest prefix {expected_ids}")

    for item in segment_results:
        sample_count = int(item.get("sample_count", 0))
        fail_count = int(item.get("fail_count", 0))
        stored_fail_count = int(item.get("stored_fail_entry_count", len(item.get("entries", []))))
        entries = item.get("entries", [])
        if sample_count < 0 or fail_count < 0 or stored_fail_count < 0:
            raise ValueError("segment result counts must be non-negative")
        if not isinstance(entries, list) or len(entries) != stored_fail_count:
            raise ValueError("stored fail entry count does not match segment fail entries")

    fail_payloads = [{"entries": item.get("entries", [])} for item in segment_results]
    fail_payloads.extend(
        {"entries": []} for _ in range(len(manifest.segments) - len(fail_payloads))
    )
    merged_fail = merge_segment_fail_json(manifest, fail_payloads)
    terminal_error = next(
        (
            item
            for item in segment_results
            if int(item.get("status", {}).get("state", -1)) == STATE_ERROR
        ),
        None,
    )
    global_frontier = None
    if terminal_error is not None:
        error_segment_id = int(terminal_error["segment_id"])
        error_segment = next(
            segment for segment in manifest.segments if segment.segment_id == error_segment_id
        )
        local_progress = max(0, int(terminal_error.get("status", {}).get("run_progress", 0)))
        if error_segment.global_event_start is not None:
            global_frontier = error_segment.global_event_start + local_progress
    elif len(segment_results) == len(manifest.segments) and manifest.segments[-1].global_event_end is not None:
        global_frontier = manifest.segments[-1].global_event_end + 1
    result_complete = (
        len(segment_results) == len(manifest.segments)
        and terminal_error is None
        and all(item.get("result_reads_performed", True) for item in segment_results)
    )
    global_result_flags = 0
    for item in segment_results:
        global_result_flags |= int(item.get("result_flags", 0))
    return {
        "segmented": True,
        "segment_count": len(manifest.segments),
        "completed_segment_count": len(segment_results),
        "complete": result_complete,
        "global_event_frontier": global_frontier,
        "global_sample_count": sum(int(item.get("sample_count", 0)) for item in segment_results),
        "global_fail_count": sum(int(item.get("fail_count", 0)) for item in segment_results),
        "global_stored_fail_entry_count": sum(
            int(item.get("stored_fail_entry_count", len(item.get("entries", []))))
            for item in segment_results
        ),
        "global_result_flags": global_result_flags,
        "fail_log_complete": (
            result_complete
            and not any(bool(item.get("fail_log_truncated")) for item in segment_results)
            and all(item.get("fail_log_read_complete", True) for item in segment_results)
            and all(item.get("result_reads_performed", True) for item in segment_results)
        ),
        "segments": [
            {
                "segment_id": int(item["segment_id"]),
                "global_event_start": manifest.segments[index].global_event_start,
                "global_event_end": manifest.segments[index].global_event_end,
                "sample_count": int(item.get("sample_count", 0)),
                "fail_count": int(item.get("fail_count", 0)),
                "stored_fail_entry_count": int(item.get("stored_fail_entry_count", len(item.get("entries", [])))),
                "fail_log_truncated": bool(item.get("fail_log_truncated")),
                "result_flags": int(item.get("result_flags", 0)),
            }
            for index, item in enumerate(segment_results)
        ],
        "entries": merged_fail["entries"],
    }


def run_segmented_rate_vector_compare(
    client: StpgClient,
    manifest: SegmentedManifest,
    *,
    device_capability: int,
    chunk_size: int = 4096,
    timeout_s: float = 20.0,
    upload_end_timeout_s: float | None = None,
    run_response_timeout_s: float | None = None,
    status_response_timeout_s: float | None = None,
    final_settle_s: float = 0.0,
    fail_stop: bool = False,
    fail_stop_limit: int = 0,
    on_segment_result: Callable[[dict[str, Any]], None] | None = None,
    progress: RunProgress | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run compare/sample Rate Vector segments with GPIO hold/resume.

    Results are collected at every hold boundary because ``CMD_UPLOAD_BEGIN``
    resets the firmware's per-run sample and fail state.  This function is a
    host-side execution helper; it does not authorize or perform hardware
    access by itself.  The caller must supply the capability from the current
    device INFO/STATUS response so the compare-on-fail-stop guard can run
    before the first segment is uploaded.
    """

    if len(manifest.segments) < 2:
        raise ValueError("segmented Rate Vector execution requires at least two segments")
    if not all(
        segment.global_event_start is not None and segment.global_event_end is not None
        for segment in manifest.segments
    ):
        raise ValueError("segmented Rate Vector execution requires global event ranges")
    _validate_global_event_ranges(list(manifest.segments))
    for index, segment in enumerate(manifest.segments):
        if segment.final_hold_state is None and index != len(manifest.segments) - 1:
            raise ValueError(f"segment {segment.segment_id} lacks final_hold_state")

    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if final_settle_s < 0:
        raise ValueError("final_settle_s must be non-negative")
    if fail_stop_limit < 0 or fail_stop_limit > STPG_FAIL_LOG_MAX:
        raise ValueError(f"fail_stop_limit must be in the range 0..{STPG_FAIL_LOG_MAX}")
    if fail_stop_limit != 0 and not fail_stop:
        raise ValueError("fail_stop_limit requires fail_stop=True")
    effective_fail_stop_limit = fail_stop_limit if fail_stop else None
    results: list[dict[str, Any]] = []
    payloads = [segment.path.read_bytes() for segment in manifest.segments]
    fail_stop_capable = (int(device_capability) & CAP_STREAM_FAIL_STOP) != 0
    if fail_stop and not fail_stop_capable:
        raise ValueError(
            "segmented streaming fail-stop requested but device does not advertise "
            "CAP_STREAM_FAIL_STOP"
        )
    if fail_stop_capable or fail_stop:
        requirements = [inspect_pattern_requirements(payload) for payload in payloads]
        for index, requirement in enumerate(requirements):
            segment = manifest.segments[index]
            if requirement.mode != "rate-vector":
                raise ValueError(
                    f"segment {segment.segment_id} is {requirement.mode}, not a Rate Vector pattern"
                )
            if fail_stop and requirement.estimated_sample_count > STPG_MAX_EVENT_COUNT:
                raise ValueError(
                    f"segment {segment.segment_id} estimated sample count "
                    f"{requirement.estimated_sample_count} exceeds streaming fail-stop SRAM "
                    f"capacity {STPG_MAX_EVENT_COUNT}"
                )
            if fail_stop and requirement.reduced_density_candidate:
                raise ValueError(
                    f"segment {segment.segment_id} uses a reduced-density Rate Vector "
                    "candidate unsupported by streaming fail-stop"
                )
            if fail_stop_capable and not fail_stop and requirement.requires_compare:
                raise ValueError(
                    "compare-bearing segmented streaming RUN without --fail-stop is not "
                    "checked by SRAM-sample streaming fail-stop firmware; refusing before "
                    "the first segment upload"
                )
    for index, (segment, payload) in enumerate(zip(manifest.segments, payloads)):
        with _stage(progress, "upload", segment_id=segment.segment_id, byte_count=len(payload)):
            if upload_end_timeout_s is None:
                upload = client.upload(payload, storage_target=STORAGE_PSRAM, chunk_size=chunk_size)
            else:
                upload = client.upload(
                    payload,
                    storage_target=STORAGE_PSRAM,
                    chunk_size=chunk_size,
                    end_response_timeout=upload_end_timeout_s,
                )
        _validate_segment_upload(upload, payload, segment)
        expected_upload_state = STATE_READY if index == 0 else STATE_HOLD_AWAITING_NEXT
        if hasattr(upload, "state") and int(upload.state) != expected_upload_state:
            raise ValueError(
                f"segment {segment.segment_id} upload state {upload.state} != "
                f"expected {expected_upload_state}"
            )
        with _stage(progress, "run_response", segment_id=segment.segment_id):
            run_response, target_state = _run_rate_segment(
                client, segment, index=index, segment_count=len(manifest.segments),
                fail_stop=fail_stop, effective_fail_stop_limit=effective_fail_stop_limit,
                run_response_timeout_s=run_response_timeout_s,
            )
        if hasattr(run_response, "error_code") and int(run_response.error_code) != 0:
            raise ValueError(
                f"segment {segment.segment_id} RUN returned error={run_response.error_code}"
            )
        if index == len(manifest.segments) - 1 and final_settle_s:
            time.sleep(final_settle_s)
        with _stage(progress, "terminal_wait", segment_id=segment.segment_id):
            status = wait_for_state(
                client, target_state, timeout_s=timeout_s,
                response_timeout_s=status_response_timeout_s,
            )
        if status.state == STATE_ERROR:
            if fail_stop and int(getattr(status, "error_code", -1)) == ERR_COMPARE_FAIL_STOP:
                # Firmware preserves result data for this one terminal error.
                result = _collect_segment_results(
                    client, segment, status, allow_partial_samples=True, progress=progress,
                )
                result["result_read_reason"] = "compare_fail_stop"
            else:
                # Other errors must not issue sample / FAIL_LOG commands.
                result = {
                    "segment_id": segment.segment_id,
                    "result_reads_performed": False,
                    "result_read_skipped_reason": "terminal_error_state",
                    "status": _status_dict(status),
                    "sample_count": 0, "fail_count": 0, "stored_fail_entry_count": 0,
                    "fail_log_truncated": False,
                    "result_flags": int(getattr(status, "result_flags", 0)), "entries": [],
                }
        else:
            result = _collect_segment_results(client, segment, status, progress=progress)
        result["upload_response"] = _status_dict(upload)
        result["run_response"] = _status_dict(run_response)
        results.append(result)
        if on_segment_result is not None:
            on_segment_result(result)
        if status.state == STATE_ERROR:
            return results, result

    return results, results[-1]


def _run_rate_segment(
    client: StpgClient, segment: SegmentSpec, *, index: int, segment_count: int,
    fail_stop: bool, effective_fail_stop_limit: int | None,
    run_response_timeout_s: float | None,
) -> tuple[object, int]:
    if index == 0:
        run_kwargs = {}
        if fail_stop:
            run_kwargs["fail_stop_limit"] = effective_fail_stop_limit
        if run_response_timeout_s is not None:
            run_kwargs["response_timeout"] = run_response_timeout_s
        run_response = client.run_stream_segment_hold(**run_kwargs)
        target_state = STATE_HOLD_AWAITING_NEXT
    else:
        resume_kwargs = {"hold_after": index != segment_count - 1}
        if fail_stop:
            resume_kwargs["fail_stop_limit"] = effective_fail_stop_limit
        if run_response_timeout_s is not None:
            resume_kwargs["response_timeout"] = run_response_timeout_s
        run_response = client.run_stream_resume_from_hold(
            segment.entry_hold_state.drive_value,
            segment.entry_hold_state.drive_enable,
            **resume_kwargs,
        )
        target_state = STATE_HOLD_AWAITING_NEXT if index != segment_count - 1 else STATE_DONE
    return run_response, target_state


def wait_for_state(
    client: StpgClient,
    target_state: int,
    *,
    timeout_s: float,
    response_timeout_s: float | None = None,
) -> object:
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    deadline = time.monotonic() + timeout_s
    last_status = None
    while time.monotonic() < deadline:
        if response_timeout_s is None:
            status = client.status()
        else:
            status = client.status(response_timeout=response_timeout_s)
        last_status = status
        if status.state == target_state or status.state == STATE_ERROR:
            return status
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for state {target_state}, last={last_status}")


def run_segmented_raw(
    client: StpgClient, manifest: SegmentedManifest, *, chunk_size: int = 4096,
    timeout_s: float = 5.0, progress: RunProgress | None = None,
    on_segment_result: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[object], object]:
    payloads = [segment.path.read_bytes() for segment in manifest.segments]
    hold_statuses: list[object] = []

    with _stage(progress, "upload", segment_id=manifest.segments[0].segment_id):
        client.upload(payloads[0], storage_target=STORAGE_PSRAM, chunk_size=chunk_size)
    with _stage(progress, "run_response", segment_id=manifest.segments[0].segment_id):
        client.run_segment_hold()

    for index in range(1, len(manifest.segments)):
        with _stage(progress, "terminal_wait", segment_id=manifest.segments[index - 1].segment_id):
            hold_status = wait_for_state(client, STATE_HOLD_AWAITING_NEXT, timeout_s=timeout_s)
        hold_statuses.append(hold_status)
        if on_segment_result is not None:
            on_segment_result({"segment_id": manifest.segments[index - 1].segment_id,
                               "status": _status_dict(hold_status)})
        if hold_status.state == STATE_ERROR:
            return hold_statuses, hold_status

        segment = manifest.segments[index]
        with _stage(progress, "upload", segment_id=segment.segment_id):
            client.upload(payloads[index], storage_target=STORAGE_PSRAM, chunk_size=chunk_size)
        with _stage(progress, "run_response", segment_id=segment.segment_id):
            client.run_resume_from_hold(
                segment.entry_hold_state.drive_value,
                segment.entry_hold_state.drive_enable,
                hold_after=index != (len(manifest.segments) - 1),
            )

    with _stage(progress, "terminal_wait", segment_id=manifest.segments[-1].segment_id):
        final_status = wait_for_state(client, STATE_DONE, timeout_s=timeout_s)
    if on_segment_result is not None:
        on_segment_result({"segment_id": manifest.segments[-1].segment_id,
                           "status": _status_dict(final_status)})
    return hold_statuses, final_status
