"""Build and verify reproducible host-only STIL pattern packages.

The package is an artifact boundary around the existing compiler.  It keeps
the source analysis, compiled pattern, source map, wait schedule, bounded
acceptance report, metadata, and manifest together without changing the input
STIL or pattern binary.  It deliberately contains no physical execution
authorization.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from stpg.pattern import (
    PatternValidationError,
    rate_vector_structure_metrics,
    rate_vector_decode_scope,
    resolve_max_rates,
    validate_pattern,
)
from stpg.constants import STPG_DEFAULT_TICK_HZ

from .device_precheck import inspect_pattern_requirements
from .json_artifacts import (
    JsonArtifactError,
    json_artifact_identity,
    read_json_artifact,
    resolve_json_artifact,
    write_json_artifact,
)
from .pipeline import CompileOptions, compile_stil
from .prepared_source import PreparedStilSource
from .source_analysis import analyze_stil_source
from .runtime_wait_model import _RuntimeModelCache
from .runtime_capacity import (
    DEFAULT_RUNTIME_PROFILE,
    RuntimeCapacityProfile,
    get_runtime_capacity_profile,
    runtime_capacity_contract,
    runtime_capacity_observation,
)
from .wait_acceptance import evaluate_wait_schedule
from .wait_binary import FORMAT_VALIDATION_NAME, WAIT_PATCH_NAME, WAIT_SOURCE_NAME, WaitBinaryError, apply_wait_schedule_files
from .wait_schedule import (
    _stored_map_digest,
    DEFAULT_CHUNK_EVENT_CAPACITY,
    DEFAULT_RING_SLOT_COUNT,
    WaitScheduleError,
    build_wait_schedule,
    validate_wait_schedule,
)


PACKAGE_KIND = "stpg_stil_pattern_package"
PACKAGE_SCHEMA_VERSION = 3
MANIFEST_KIND = "stpg_stil_pattern_package_manifest"
MANIFEST_SCHEMA_VERSION = 3
PACKAGE_JSON_GZIP_MIN_BYTES = 16 * 1024

PATTERN_NAME = "pattern.bin"
MAP_NAME = "pattern.map.json"
COMPILE_REPORT_NAME = "pattern.compile_report.json"
SOURCE_ANALYSIS_NAME = "source.analysis.json"
WAIT_SCHEDULE_NAME = "pattern.wait.json"
WAIT_VALIDATION_NAME = "pattern.wait.validation.json"
WAIT_ACCEPTANCE_NAME = "pattern.wait.acceptance.json"
METADATA_NAME = "pattern.metadata.json"
MANIFEST_NAME = "manifest.json"


class PatternPackageError(ValueError):
    """Raised when a host-only pattern package cannot be built or verified."""


@dataclass(frozen=True)
class PatternPackageOptions:
    input_path: Path
    output_dir: Path
    mode: str = "auto"
    tick_hz: int = STPG_DEFAULT_TICK_HZ
    pin_map_path: Path | None = None
    pattern_burst: str | None = None
    pattern: str | None = None
    strict: bool = False
    allow_approx: bool = False
    prefer_raw: bool = False
    until_vector: int | None = None
    vector_range: tuple[int, int] | None = None
    time_scale: Decimal = Decimal("1.0")
    markers: tuple[dict[str, Any], ...] = ()
    practical_chunk_shaping: bool = False
    practical_chunk_shaping_target_range: tuple[int, int] | None = None
    practical_chunk_shaping_max_run_vectors: int = 64
    practical_gate_policy: str = "error"
    wait_schedule_path: Path | None = None
    service_time_us: float | None = None
    service_time_source: str = ""
    safety_margin_us: float = 0.0
    max_wait_us: float | None = None
    max_total_wait_us: float | None = None
    max_wait_count: int | None = None
    pattern_time_limit_us: float | None = None
    runtime_profile: str | RuntimeCapacityProfile = DEFAULT_RUNTIME_PROFILE
    max_rates: int | None = None
    chunk_event_capacity: int = DEFAULT_CHUNK_EVENT_CAPACITY
    ring_slot_count: int = DEFAULT_RING_SLOT_COUNT
    forbidden_stil_rate_units: tuple[str, ...] = ()
    service_profile_scope: str = "non-loop"
    loop_policy: str = "expand"
    prepared_source: PreparedStilSource | None = field(default=None, repr=False, compare=False)


def _json_write(path: Path, value: dict[str, Any]) -> Path:
    # Sidecars are machine-oriented and usually large; compact encoding avoids
    # materializing an additional pretty-printed JSON copy before gzip. The
    # source map also uses a versioned ordinal-array encoding when its indices
    # are contiguous; readers restore the established dict API.
    return write_json_artifact(
        path,
        value,
        compression="auto",
        min_compress_bytes=PACKAGE_JSON_GZIP_MIN_BYTES,
        indent=None,
        sort_keys=True,
        compact_vector_map=Path(str(path).removesuffix(".gz")).name == MAP_NAME,
        compact_compile_report=Path(str(path).removesuffix(".gz")).name == COMPILE_REPORT_NAME,
    )


def _identity(path: Path) -> dict[str, Any]:
    path = resolve_json_artifact(path)
    if not path.is_file():
        raise PatternPackageError(f"not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}


def _artifact_identity(path: Path, *, relative_path: str) -> dict[str, Any]:
    if path.suffix == ".bin":
        result = _identity(path)
        result.pop("path", None)
        result["path"] = relative_path
        return result
    return json_artifact_identity(path, relative_path=relative_path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = read_json_artifact(path)
    except (OSError, UnicodeDecodeError, JsonArtifactError, json.JSONDecodeError) as exc:
        raise PatternPackageError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PatternPackageError(f"JSON root must be an object: {path}")
    return value


def _prepare_output_dir(output_dir: Path) -> None:
    if output_dir.exists() and not output_dir.is_dir():
        raise PatternPackageError(f"output path is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise PatternPackageError(f"output directory must be empty: {output_dir}")


def _write_not_applicable_wait_reports(
    *,
    pattern_path: Path,
    mode: str,
    schedule_path: Path,
    validation_path: Path,
    acceptance_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    pattern_identity = _identity(pattern_path)
    schedule = {
        "kind": "stpg_wait_schedule_binding",
        "schema_version": 1,
        "status": "NOT_APPLICABLE",
        "mode_emitted": mode,
        "identity": {"pattern": pattern_identity, "map": None},
        "policy": {
            "automatic_insertion": False,
            "pattern_mutation": False,
            "physical_execution_authorized": False,
        },
        "reason": "wait schedules are defined for rate-vector chunk boundaries; this mode has no such schedule",
        "limitations": [
            "no wait is automatically inserted",
            "this record is not physical approval",
        ],
    }
    validation = {
        "kind": "stpg_wait_schedule_validation",
        "schema_version": "v1",
        "status": "NOT_APPLICABLE",
        "input": {"pattern": pattern_identity, "schedule": None, "map": None},
        "errors": [],
        "warnings": [f"mode_emitted={mode} does not expose rate-vector chunk wait boundaries"],
        "physical_execution_authorized": False,
    }
    acceptance = {
        "kind": "stpg_wait_schedule_acceptance",
        "schema_version": "v1",
        "status": "NOT_APPLICABLE",
        "scope": "bounded_host_model_only",
        "input": {"pattern_path": str(pattern_path), "schedule_path": str(schedule_path)},
        "errors": [],
        "warnings": [f"mode_emitted={mode} has no wait schedule to evaluate"],
        "steady_tfill": {
            "status": "not_emitted",
            "reason": "host-only package generation has no physical trace coverage",
        },
        "physical_execution_authorized": False,
        "limitations": [
            "no service-time, Tfill, timing, throughput, or physical acceptance is performed",
        ],
    }
    _json_write(schedule_path, schedule)
    _json_write(validation_path, validation)
    _json_write(acceptance_path, acceptance)
    return schedule, validation, acceptance


def _verify_map_contract(pattern_bytes: bytes, map_data: dict[str, Any], mode: str) -> list[str]:
    errors: list[str] = []
    if map_data.get("mode_emitted") != mode:
        errors.append(f"map mode_emitted={map_data.get('mode_emitted')!r} differs from pattern mode={mode!r}")
    vectors = map_data.get("vectors")
    if not isinstance(vectors, dict):
        return ["map vectors must be an object"]
    requirements = inspect_pattern_requirements(pattern_bytes)
    if not vectors:
        errors.append("map vectors must not be empty")
    max_emitted_index: int | None = None
    if mode == "rate-vector":
        from stpg.pattern import unpack_rate_vector_full

        header, _rates, _slots, _records, _loops = unpack_rate_vector_full(pattern_bytes)
        max_emitted_index = header.vector_count - 1
    elif mode == "raw-event":
        max_emitted_index = requirements.estimated_event_count - 1
    for key, entry in vectors.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            errors.append(f"map vector key is not an integer: {key!r}")
            continue
        if index < 0 or (max_emitted_index is not None and index > max_emitted_index):
            errors.append(f"map vector index out of range: {index}")
        if not isinstance(entry, dict):
            errors.append(f"map vector {key} must be an object")
            continue
        from .map_lookup import validate_source_map_compression
        try:
            validate_source_map_compression(entry, repeat=(
                int(_records[index].repeat) if mode == "rate-vector" and 0 <= index < len(_records)
                and hasattr(_records[index], "repeat") else None))
        except ValueError as exc:
            errors.append(f"map vector {key}: {exc}")
        source_map = entry.get("source_map")
        if not isinstance(source_map, list) or not source_map:
            errors.append(f"map vector {key} has no source_map entries")
            continue
        for source_index, source_entry in enumerate(source_map):
            if not isinstance(source_entry, dict):
                errors.append(f"map vector {key} source_map[{source_index}] must be an object")
                continue
            global_index = source_entry.get("global_vector_index")
            if isinstance(global_index, bool) or not isinstance(global_index, int) or global_index < 0:
                errors.append(f"map vector {key} source_map[{source_index}] has invalid global_vector_index")
    return errors


def _verify_generated_products(
    *,
    package_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if manifest.get("kind") != MANIFEST_KIND or manifest.get("schema_version") not in {1, 2, 3}:
        errors.append("manifest kind or schema_version is unsupported")
    if manifest.get("physical_execution_authorized") is not False:
        errors.append("manifest must set physical_execution_authorized=false")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        errors.append("manifest artifacts must be a non-empty object")
        artifacts = {}
    package_root = package_dir.resolve()
    required = {
        PATTERN_NAME, MAP_NAME, COMPILE_REPORT_NAME, SOURCE_ANALYSIS_NAME,
        WAIT_SCHEDULE_NAME, WAIT_VALIDATION_NAME, WAIT_ACCEPTANCE_NAME, METADATA_NAME,
    }
    for name, descriptor in artifacts.items():
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("path"), str):
            errors.append(f"manifest artifact {name!r} has no relative path")
            continue
        relative = Path(descriptor["path"])
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"manifest artifact {name!r} path escapes package: {relative}")
            continue
        path = (package_dir / relative).resolve()
        if package_root not in path.parents and path != package_root:
            errors.append(f"manifest artifact {name!r} path escapes package: {relative}")
            continue
        if not path.is_file():
            errors.append(f"manifest artifact missing: {relative}")
            continue
        actual = _identity(path)
        if actual["size_bytes"] != descriptor.get("size_bytes"):
            errors.append(f"artifact {name} size mismatch")
        if actual["sha256"] != descriptor.get("sha256"):
            errors.append(f"artifact {name} SHA-256 mismatch")
        if descriptor.get("encoding") == "gzip":
            try:
                logical_name = str(relative).removesuffix(".gz")
                if logical_name not in required:
                    read_json_artifact(path)
                decoded_identity = json_artifact_identity(path, relative_path=str(relative))
            except (JsonArtifactError, OSError, ValueError) as exc:
                errors.append(f"artifact {name} gzip JSON is invalid: {exc}")
            else:
                for key in ("uncompressed_size_bytes", "uncompressed_sha256"):
                    if descriptor.get(key) != decoded_identity.get(key):
                        errors.append(f"artifact {name} {key} mismatch")

    listed_paths = {
        str(item.get("path"))[:-3] if str(item.get("path", "")).endswith(".gz") else str(item.get("path"))
        for item in artifacts.values()
        if isinstance(item, dict)
    }
    missing_required = sorted(required - listed_paths)
    if missing_required:
        errors.extend(f"manifest missing required artifact: {name}" for name in missing_required)

    pattern_path = package_dir / PATTERN_NAME
    map_path = resolve_json_artifact(package_dir / MAP_NAME)
    compile_report_path = resolve_json_artifact(package_dir / COMPILE_REPORT_NAME)
    source_analysis_path = resolve_json_artifact(package_dir / SOURCE_ANALYSIS_NAME)
    schedule_path = resolve_json_artifact(package_dir / WAIT_SCHEDULE_NAME)
    validation_path = resolve_json_artifact(package_dir / WAIT_VALIDATION_NAME)
    acceptance_path = resolve_json_artifact(package_dir / WAIT_ACCEPTANCE_NAME)
    metadata_path = resolve_json_artifact(package_dir / METADATA_NAME)
    try:
        pattern_bytes = pattern_path.read_bytes()
        metadata = _read_json(metadata_path)
        max_rates = resolve_max_rates(metadata.get("pattern", {}).get("max_rate_count"))
        validate_pattern(pattern_bytes, max_rates=max_rates)
        requirements = inspect_pattern_requirements(pattern_bytes)
        mode = requirements.mode
        map_digest = _stored_map_digest(map_path)
        map_data = _read_json(map_path)
        errors.extend(_verify_map_contract(pattern_bytes, map_data, mode))
        compile_mode = _read_json(compile_report_path).get("mode_emitted")
        source_analysis = _read_json(source_analysis_path)
        source_status = source_analysis.get("status")
        source_physical_authorization = source_analysis.get("decision", {}).get("physical_execution_authorized")
        del source_analysis
        schedule = _read_json(schedule_path)
        validation = _read_json(validation_path)
        acceptance = _read_json(acceptance_path)
    except (OSError, PatternValidationError, PatternPackageError, ValueError) as exc:
        errors.append(f"generated product decode/validation failed: {exc}")
    else:
        if compile_mode != mode:
            errors.append("compile report mode_emitted does not match pattern")
        if source_physical_authorization is not False:
            errors.append("source analysis must set physical_execution_authorized=false")
        if metadata.get("physical_execution_authorized") is not False:
            errors.append("metadata must set physical_execution_authorized=false")
        if metadata.get("pattern", {}).get("mode") != mode:
            errors.append("metadata pattern mode does not match pattern")
        if mode == "rate-vector":
            if validation.get("status") != "PASS":
                errors.append(f"rate-vector wait validation is not PASS: {validation.get('status')!r}")
            try:
                schedule_validation = validate_wait_schedule(
                    pattern_path, schedule_path, map_path=map_path, _verified_map=(map_digest, map_data))
            except (OSError, ValueError, WaitScheduleError) as exc:
                errors.append(f"wait schedule revalidation failed: {exc}")
            else:
                if schedule_validation.get("status") != "PASS":
                    errors.append("wait schedule does not revalidate against packaged pattern/map")
        else:
            if validation.get("status") != "NOT_APPLICABLE":
                errors.append("non-rate-vector package must mark wait validation NOT_APPLICABLE")
        if acceptance.get("physical_execution_authorized") is not False:
            errors.append("acceptance report must set physical_execution_authorized=false")
        if acceptance.get("steady_tfill", {}).get("status") != "not_emitted":
            errors.append("acceptance report must not emit steady Tfill")
        acceptance_status = acceptance.get("status")
        if acceptance_status not in {"ACCEPTED", "REQUIRES_SERVICE_PROFILE", "NOT_APPLICABLE", "REJECTED"}:
            errors.append(f"unsupported wait acceptance status: {acceptance_status!r}")
        if acceptance_status in {"REQUIRES_SERVICE_PROFILE", "REJECTED"}:
            warnings.append(f"wait acceptance status is {acceptance_status}; physical use is not authorized")
        if schedule.get("status") == "APPLIED":
            if acceptance_status != "ACCEPTED":
                errors.append("an APPLIED wait package requires an ACCEPTED source wait acceptance")
            if not resolve_json_artifact(package_dir / WAIT_PATCH_NAME).is_file():
                errors.append("an APPLIED wait package must include pattern.wait.patch.json")
        if source_status == "REVIEW":
            warnings.append("source analysis status is REVIEW")
        if source_status == "REJECT":
            errors.append("source analysis status is REJECT")
        if manifest.get("package", {}).get("pattern_mode") != mode:
            errors.append("manifest package pattern_mode does not match pattern")
    return {
        "status": "PASS" if not errors else "REJECT",
        "errors": errors,
        "warnings": warnings,
        "physical_execution_authorized": False,
    }


def verify_pattern_package(package_dir: Path) -> dict[str, Any]:
    """Verify package hashes, pattern CRC/format, map contract, and reports."""

    manifest_path = package_dir / MANIFEST_NAME
    try:
        manifest = _read_json(manifest_path)
    except PatternPackageError as exc:
        return {
            "kind": PACKAGE_KIND,
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "status": "REJECT",
            "errors": [str(exc)],
            "warnings": [],
            "physical_execution_authorized": False,
        }
    result = _verify_generated_products(package_dir=package_dir, manifest=manifest)
    return {
        "kind": PACKAGE_KIND,
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "status": result["status"],
        "package_dir": str(package_dir),
        "manifest": manifest,
        "errors": result["errors"],
        "warnings": result["warnings"],
        "physical_execution_authorized": False,
    }


def _manifest_for_package(
    package_dir: Path,
    *,
    package_metadata: dict[str, Any],
) -> dict[str, Any]:
    artifact_names = [
        PATTERN_NAME,
        MAP_NAME,
        COMPILE_REPORT_NAME,
        SOURCE_ANALYSIS_NAME,
        WAIT_SCHEDULE_NAME,
        WAIT_VALIDATION_NAME,
        WAIT_ACCEPTANCE_NAME,
        METADATA_NAME,
    ]
    for optional_name in (WAIT_PATCH_NAME, WAIT_SOURCE_NAME, FORMAT_VALIDATION_NAME):
        if resolve_json_artifact(package_dir / optional_name).is_file():
            artifact_names.append(optional_name)
    artifacts: dict[str, dict[str, Any]] = {}
    for name in artifact_names:
        actual_path = package_dir / name if name == PATTERN_NAME else resolve_json_artifact(package_dir / name)
        artifacts[name] = _artifact_identity(actual_path, relative_path=actual_path.name)
    return {
        "kind": MANIFEST_KIND,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "package": {
            "pattern_mode": package_metadata["pattern"]["mode"],
            "artifact_count": len(artifacts),
            "manifest_self_hash_excluded": True,
        },
        "artifacts": artifacts,
        "physical_execution_authorized": False,
        "limitations": [
            "manifest.json is intentionally excluded from its own artifact hash set",
            "package verification is host-only and does not authorize upload or DUT execution",
        ],
    }


@rate_vector_decode_scope()
def derive_pattern_package_with_wait(
    source_package_dir: Path,
    *,
    schedule_path: Path,
    acceptance_path: Path,
    output_dir: Path,
    _model_cache: _RuntimeModelCache | None = None,
) -> dict[str, Any]:
    """Create a new package whose binary contains an accepted wait schedule.

    The source Phase 4 package is treated as immutable context.  The derived
    package gets fresh pattern/map/wait/patch/manifest hashes and retains the
    original compile and source-analysis reports as provenance.
    """

    source_verification = verify_pattern_package(source_package_dir)
    if source_verification.get("status") != "PASS":
        raise PatternPackageError("source package verification failed")
    source_pattern = source_package_dir / PATTERN_NAME
    source_map = resolve_json_artifact(source_package_dir / MAP_NAME)
    source_metadata = _read_json(source_package_dir / METADATA_NAME)
    acceptance = _read_json(acceptance_path)
    source_requirements = inspect_pattern_requirements(source_pattern.read_bytes())
    if source_requirements.mode != "rate-vector":
        raise PatternPackageError("wait binary application currently supports rate-vector packages only")

    _prepare_output_dir(output_dir)
    for name in (SOURCE_ANALYSIS_NAME, COMPILE_REPORT_NAME):
        stored_source = resolve_json_artifact(source_package_dir / name)
        shutil.copyfile(stored_source, output_dir / stored_source.name)
    try:
        apply_result = apply_wait_schedule_files(
            source_pattern,
            schedule_path,
            map_path=source_map,
            acceptance_path=acceptance_path,
            output_dir=output_dir,
            require_empty=False,
            compact_package_sidecars=True,
            _model_cache=_model_cache,
        )
    except (WaitBinaryError, OSError, ValueError) as exc:
        raise PatternPackageError(f"cannot derive wait package: {exc}") from exc

    if apply_result.get("status") == "NOT_REQUIRED":
        raise PatternPackageError(
            "wait schedule contains no non-zero waits; no derived package is needed; "
            "use the verified source package"
        )

    derived_pattern = output_dir / PATTERN_NAME
    requirements = inspect_pattern_requirements(derived_pattern.read_bytes())
    metadata = deepcopy(source_metadata)
    metadata["kind"] = PACKAGE_KIND + ".metadata"
    metadata["physical_execution_authorized"] = False
    metadata["derived_from"] = {
        "package_dir": str(source_package_dir),
        "pattern": _identity(source_pattern),
        "map": _identity(source_map),
        "schedule": _identity(schedule_path),
        "acceptance": _identity(acceptance_path),
    }
    metadata["pattern"] = {
        **metadata.get("pattern", {}),
        "mode": requirements.mode,
        "tick_hz": requirements.tick_hz,
        "active_pins": requirements.active_pins,
        "requires_sample": requirements.requires_sample,
        "requires_compare": requirements.requires_compare,
        "estimated_event_count": requirements.estimated_event_count,
        "size_bytes": derived_pattern.stat().st_size,
        "structure_metrics": rate_vector_structure_metrics(derived_pattern.read_bytes()),
    }
    if acceptance.get("service_profile_scope") in {"non-loop", "loop-generic"}:
        metadata.setdefault("options", {})["service_profile_scope"] = acceptance[
            "service_profile_scope"
        ]
    metadata.setdefault("artifact_roles", {})["wait_patch"] = resolve_json_artifact(
        output_dir / WAIT_PATCH_NAME
    ).name
    metadata["policies"] = {
        **metadata.get("policies", {}),
        "pattern_mutation": True,
        "automatic_wait_insertion": False,
        "physical_execution_authorized": False,
    }
    metadata.setdefault("limitations", []).append(
        "this package is a derived host-only wait candidate; physical execution needs a separate approval"
    )
    for role, logical_name in tuple(metadata.get("artifact_roles", {}).items()):
        metadata["artifact_roles"][role] = resolve_json_artifact(output_dir / logical_name).name
    _json_write(output_dir / METADATA_NAME, metadata)
    manifest = _manifest_for_package(output_dir, package_metadata=metadata)
    write_json_artifact(output_dir / MANIFEST_NAME, manifest, compression="none", sort_keys=True)
    verification = verify_pattern_package(output_dir)
    if verification.get("status") != "PASS":
        raise PatternPackageError(
            "derived package verification failed: " + "; ".join(verification.get("errors", []))
        )
    return {
        "kind": PACKAGE_KIND,
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "status": "VERIFIED",
        "package_dir": str(output_dir),
        "manifest": manifest,
        "application": apply_result,
        "verification": verification,
        "physical_execution_authorized": False,
    }


@rate_vector_decode_scope()
def build_pattern_package(options: PatternPackageOptions) -> dict[str, Any]:
    """Compile and package one STIL input without physical side effects."""

    _prepare_output_dir(options.output_dir)
    runtime_profile = (
        get_runtime_capacity_profile(options.runtime_profile)
        if isinstance(options.runtime_profile, str)
        else options.runtime_profile
    )
    prepared_source = options.prepared_source or PreparedStilSource(
        options.input_path, pin_map_path=options.pin_map_path, pattern_burst=options.pattern_burst,
        pattern=options.pattern, loop_policy=options.loop_policy)
    source_analysis = analyze_stil_source(
        options.input_path,
        mode=options.mode,
        tick_hz=options.tick_hz,
        pin_map_path=options.pin_map_path,
        pattern_burst=options.pattern_burst,
        pattern=options.pattern,
        time_scale=options.time_scale,
        include_backend_candidates=True,
        runtime_profile=runtime_profile,
        max_rates=options.max_rates,
        until_vector=options.until_vector,
        vector_range=options.vector_range,
        loop_policy=options.loop_policy,
        prepared_source=prepared_source,
    )
    source_analysis_path = options.output_dir / SOURCE_ANALYSIS_NAME
    _json_write(source_analysis_path, source_analysis)
    if source_analysis.get("status") == "REJECT":
        from .source_analysis import source_rejection
        raise PatternPackageError(source_rejection(source_analysis))
    del source_analysis

    payload, map_data, compile_report = compile_stil(
        CompileOptions(
            input_path=options.input_path,
            mode=options.mode,
            tick_hz=options.tick_hz,
            pin_map_path=options.pin_map_path,
            pattern_burst=options.pattern_burst,
            pattern=options.pattern,
            strict=options.strict,
            allow_approx=options.allow_approx,
            prefer_raw=options.prefer_raw,
            until_vector=options.until_vector,
            vector_range=options.vector_range,
            loop_policy=options.loop_policy,
            time_scale=options.time_scale,
            markers=options.markers,
            practical_chunk_shaping=options.practical_chunk_shaping,
            practical_chunk_shaping_target_range=options.practical_chunk_shaping_target_range,
            practical_chunk_shaping_max_run_vectors=options.practical_chunk_shaping_max_run_vectors,
            practical_gate_policy=options.practical_gate_policy,
            runtime_profile=runtime_profile,
            max_rates=options.max_rates,
            prepared_source=prepared_source,
        )
    )
    prepared_source.clear_resolution()
    del prepared_source
    pattern_path = options.output_dir / PATTERN_NAME
    map_path = options.output_dir / MAP_NAME
    compile_report_path = options.output_dir / COMPILE_REPORT_NAME
    pattern_path.write_bytes(payload)
    _json_write(map_path, map_data)
    _json_write(compile_report_path, compile_report)
    del map_data, compile_report

    try:
        validate_pattern(payload, expected_tick_hz=options.tick_hz, max_rates=options.max_rates)
    except PatternValidationError as exc:
        raise PatternPackageError(f"compiled pattern failed validation: {exc}") from exc
    requirements = inspect_pattern_requirements(payload)
    mode = requirements.mode
    capacity_observation = runtime_capacity_observation(
        requirements,
        pattern_size_bytes=len(payload),
        profile=runtime_profile,
    )

    wait_schedule_path = options.output_dir / WAIT_SCHEDULE_NAME
    wait_validation_path = options.output_dir / WAIT_VALIDATION_NAME
    wait_acceptance_path = options.output_dir / WAIT_ACCEPTANCE_NAME
    if mode == "rate-vector":
        model_cache = _RuntimeModelCache()
        if options.wait_schedule_path is None:
            schedule = build_wait_schedule(
                pattern_path,
                map_path=map_path,
                chunk_event_capacity=options.chunk_event_capacity,
                ring_slot_count=options.ring_slot_count,
                max_rates=options.max_rates,
                forbidden_stil_rate_units=options.forbidden_stil_rate_units,
                _model_cache=model_cache,
            )
            _json_write(wait_schedule_path, schedule)
        else:
            if options.forbidden_stil_rate_units:
                raise PatternPackageError(
                    "--forbid-stil-rate cannot be combined with an external wait schedule; "
                    "put the policy on the schedule that is supplied with --wait-schedule"
                )
            try:
                _json_write(wait_schedule_path, _read_json(options.wait_schedule_path))
            except (OSError, PatternPackageError) as exc:
                raise PatternPackageError(f"cannot copy wait schedule: {exc}") from exc
        validation = validate_wait_schedule(
            pattern_path,
            wait_schedule_path,
            map_path=map_path,
            expected_chunk_event_capacity=options.chunk_event_capacity,
            expected_ring_slot_count=options.ring_slot_count,
            max_rates=options.max_rates,
            _model_cache=model_cache,
        )
        _json_write(wait_validation_path, validation)
        if validation.get("status") != "PASS":
            raise PatternPackageError("wait schedule validation failed against generated pattern/map")
        acceptance = evaluate_wait_schedule(
            pattern_path,
            wait_schedule_path,
            map_path=map_path,
            service_time_us=options.service_time_us,
            service_time_source=options.service_time_source,
            safety_margin_us=options.safety_margin_us,
            max_wait_us=options.max_wait_us,
            max_total_wait_us=options.max_total_wait_us,
            max_wait_count=options.max_wait_count,
            pattern_time_limit_us=options.pattern_time_limit_us,
            max_rates=options.max_rates,
            service_profile_scope=options.service_profile_scope,
            _model_cache=model_cache,
        )
        _json_write(wait_acceptance_path, acceptance)
        del model_cache
    else:
        _write_not_applicable_wait_reports(
            pattern_path=pattern_path,
            mode=mode,
            schedule_path=wait_schedule_path,
            validation_path=wait_validation_path,
            acceptance_path=wait_acceptance_path,
        )

    metadata = {
        "kind": PACKAGE_KIND + ".metadata",
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "physical_execution_authorized": False,
        "input": {
            "stil": _identity(options.input_path),
            "pin_map": _identity(options.pin_map_path) if options.pin_map_path is not None else None,
        },
        "generator": {
            "module": _identity(Path(__file__)),
            "cli": _identity(Path(__file__).with_name("cli_package.py")),
        },
        "options": {
            "mode": options.mode,
            "loop_policy": options.loop_policy,
            "tick_hz": options.tick_hz,
            "pattern_burst": options.pattern_burst,
            "pattern": options.pattern,
            "strict": options.strict,
            "allow_approx": options.allow_approx,
            "prefer_raw": options.prefer_raw,
            "until_vector": options.until_vector,
            "vector_range": list(options.vector_range) if options.vector_range is not None else None,
            "time_scale": str(options.time_scale),
            "practical_chunk_shaping": options.practical_chunk_shaping,
            "practical_chunk_shaping_target_range": (
                list(options.practical_chunk_shaping_target_range)
                if options.practical_chunk_shaping_target_range is not None
                else None
            ),
            "practical_chunk_shaping_max_run_vectors": options.practical_chunk_shaping_max_run_vectors,
            "practical_gate_policy": options.practical_gate_policy,
            "runtime_profile": runtime_capacity_contract(runtime_profile),
            "max_rates": options.max_rates,
            "service_profile_scope": options.service_profile_scope,
            "chunk_event_capacity": options.chunk_event_capacity,
            "ring_slot_count": options.ring_slot_count,
        },
        "pattern": {
            "mode": mode,
            "tick_hz": requirements.tick_hz,
            "active_pins": requirements.active_pins,
            "requires_sample": requirements.requires_sample,
            "requires_compare": requirements.requires_compare,
            "estimated_event_count": requirements.estimated_event_count,
            "estimated_sample_count": requirements.estimated_sample_count,
            "estimated_compare_count": requirements.estimated_compare_count,
            "size_bytes": len(payload),
            "runtime_profile": runtime_capacity_contract(runtime_profile),
            "runtime_capacity_observation": capacity_observation,
            "max_rate_count": resolve_max_rates(options.max_rates),
            "structure_metrics": (
                rate_vector_structure_metrics(payload) if mode == "rate-vector" else {}
            ),
        },
        "artifact_roles": {
            "pattern": PATTERN_NAME,
            "map": MAP_NAME,
            "compile_report": COMPILE_REPORT_NAME,
            "source_analysis": SOURCE_ANALYSIS_NAME,
            "wait_schedule": WAIT_SCHEDULE_NAME,
            "wait_validation": WAIT_VALIDATION_NAME,
            "wait_acceptance": WAIT_ACCEPTANCE_NAME,
        },
        "policies": {
            "pattern_mutation": False,
            "automatic_wait_insertion": False,
            "result_derived_fixture_creation": False,
            "physical_execution_authorized": False,
        },
        "limitations": [
            "package generation and verification are host-only",
            "wait acceptance is bounded by the supplied model and does not emit steady Tfill",
            "source analysis REVIEW or wait acceptance pending status requires human review",
        ],
    }
    metadata["artifact_roles"] = {
        "pattern": PATTERN_NAME,
        "map": resolve_json_artifact(map_path).name,
        "compile_report": resolve_json_artifact(compile_report_path).name,
        "source_analysis": resolve_json_artifact(source_analysis_path).name,
        "wait_schedule": resolve_json_artifact(wait_schedule_path).name,
        "wait_validation": resolve_json_artifact(wait_validation_path).name,
        "wait_acceptance": resolve_json_artifact(wait_acceptance_path).name,
    }
    metadata_path = options.output_dir / METADATA_NAME
    _json_write(metadata_path, metadata)
    manifest = _manifest_for_package(options.output_dir, package_metadata=metadata)
    manifest_path = options.output_dir / MANIFEST_NAME
    write_json_artifact(manifest_path, manifest, compression="none", sort_keys=True)
    verification = verify_pattern_package(options.output_dir)
    if verification["status"] != "PASS":
        raise PatternPackageError(
            "generated package verification failed: " + "; ".join(verification["errors"])
        )
    return {
        "kind": PACKAGE_KIND,
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "status": "VERIFIED",
        "package_dir": str(options.output_dir),
        "manifest": manifest,
        "verification": verification,
        "physical_execution_authorized": False,
    }
