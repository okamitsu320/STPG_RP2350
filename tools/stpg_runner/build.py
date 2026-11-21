"""Offline project compilation with verified, content-addressed build bundles.

This module deliberately has no transport, subprocess or physical-runner imports.
The bundle is an integrity/provenance boundary, not an execution admission.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path
import platform
import shutil
from threading import RLock
from typing import Any, Callable

from stil.device_precheck import inspect_pattern_requirements
from stil.pattern_package import (
    PatternPackageOptions, build_pattern_package, derive_pattern_package_with_wait,
    verify_pattern_package,
)
from stil.prepared_source import PreparedStilSource
from stil.source_analysis import analyze_stil_source
from stil.json_artifacts import read_json_artifact, resolve_json_artifact, write_json_artifact
from stil.source_artifacts import copy_source_artifact
from stil.segmented import load_manifest, verify_segmented_manifest
from stil.segmented_package import build_segment_ranges, build_segmented_packages
from stil.wait_acceptance import evaluate_wait_schedule
from stil.wait_schedule import propose_wait_schedule
from stpg.pattern import rate_vector_decode_scope, validate_pattern

from .artifacts import RunnerError, canonical_json, confined_path, identity, read_json, write_json
from .progress import CancellationToken, ProgressEvent, Reporter
from .project import Project, chunk_wait_microseconds, load_project, project_template
from .segmentation import segmentation_decision, source_rejection
from .source_storage import SOURCE_STORAGE_KIND, SOURCE_STORAGE_NAME, bundle_source_paths

BUNDLE_NAME = "runner.bundle.json"
BUNDLE_KIND = "stpg_runner_bundle"

# Only this process can mint receipts, after a complete semantic PASS and a
# second exact inventory check. No payloads/maps or caller-provided PASS tokens
# are retained. An explicit verify is always deep; execution may reuse one.
_VERIFICATION_RECEIPT_LIMIT = 8
_VERIFICATION_RECEIPT_MANIFEST_BYTES = 1024 * 1024
_verification_receipts: OrderedDict[tuple, tuple[bytes, dict[str, Any]]] = OrderedDict()
_receipt_lock = RLock()


def _clear_verification_receipts() -> None:
    """Drop optional host optimizations (also useful to independent tests)."""
    with _receipt_lock:
        _verification_receipts.clear()


@dataclass(frozen=True)
class BuildResult:
    bundle_dir: Path
    manifest_sha256: str
    cache_hit: bool
    package_kind: str = "single"
    segment_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "stpg_runner_build_result", "schema_version": 1,
            "status": "HOST_ONLY_VERIFIED", "bundle_dir": str(self.bundle_dir),
            "manifest_sha256": self.manifest_sha256, "cache_hit": self.cache_hit,
            "package_kind": self.package_kind, "segment_count": self.segment_count,
            "device_io_performed": False, "physical_execution_authorized": False,
            "execution_available": True,
        }


def init_project(source: Path, directory: Path, *, wait_preset: str, completion: str,
                 pin_map: Path | None = None, service_us: float | None = None,
                 chunk_wait_us: int | None = None,
                 service_source: str = "", service_scope: str | None = None,
                 max_total_wait_us: float | None = None,
                 forbidden_stil_rates: tuple[str, ...] = (),
                 max_segment_vectors: int | None = None,
                 segmentation_mode: str | None = "auto",
                 storage: str = "psram", compile_mode: str = "auto",
                 loop_policy: str = "preserve", allow_approx: bool = False) -> Path:
    """Create a new portable project. Never overwrite an existing directory."""
    source = Path(source).resolve()
    pin_map = Path(pin_map).resolve() if pin_map is not None else None
    for path in (source, pin_map):
        if path is not None and not path.is_file():
            raise RunnerError(f"project input is missing/not a file: {path}")
    project = project_template(
        stil="inputs/pattern.stil", pin_map="inputs/pins.json" if pin_map else None,
        wait_preset=wait_preset, completion=completion, service_us=service_us,
        chunk_wait_us=chunk_wait_us,
        service_source=service_source, service_scope=service_scope,
        max_total_wait_us=max_total_wait_us,
        forbidden_stil_rates=forbidden_stil_rates, max_segment_vectors=max_segment_vectors,
        segmentation_mode=segmentation_mode,
        storage=storage, compile_mode=compile_mode, loop_policy=loop_policy, allow_approx=allow_approx,
    )
    directory = Path(directory).resolve()
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir()  # exclusive; failure is retained, never silently retried
    inputs = directory / "inputs"
    inputs.mkdir()
    stored, _ = copy_source_artifact(source, inputs / "pattern.stil")
    settings = project.to_dict()
    settings["input"]["stil"] = stored.relative_to(directory).as_posix()
    if pin_map is not None:
        stored, _ = copy_source_artifact(pin_map, inputs / "pins.json")
        settings["input"]["pin_map"] = stored.relative_to(directory).as_posix()
    project = Project.from_dict(settings)
    path = directory / "project.stpg-project.json"
    write_json(path, project.to_dict())
    return path


def tool_fingerprint() -> str:
    """Bind compiler, validators and this SDK by relative name and byte content."""
    tools_root = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for package in ("stpg", "stil", "stpg_runner"):
        for path in sorted((tools_root / package).rglob("*.py")):
            digest.update(path.relative_to(tools_root).as_posix().encode("utf-8") + b"\x00")
            digest.update(bytes.fromhex(identity(path)["sha256"]))
    return digest.hexdigest()


def _binding(project: Project, inputs: dict[str, Path]) -> dict[str, Any]:
    settings = project.to_dict()
    return {
        "project": settings,
        "inputs": {role: {"path": settings["input"][role], **identity(path)}
                   for role, path in inputs.items()},
        "tool_sha256": tool_fingerprint(), "python": platform.python_version(),
    }


def _require_accepted_wait_plan(report: dict[str, Any]) -> None:
    """A retained partial planner result is never an executable runner plan."""
    plan = report.get("wait_plan")
    if not isinstance(plan, dict) or plan.get("status") not in {"planned", "no_wait_needed"}:
        detail = (plan.get("reason") or plan.get("status")) if isinstance(plan, dict) else "missing plan"
        raise RunnerError("explicit wait plan rejected; retained proposal is not executable: " + str(detail))


def _pattern_contract(path: Path, *, max_rates: int = 64,
                      tick_hz: int | None = None) -> dict[str, Any]:
    payload = path.read_bytes()
    validate_pattern(payload, max_rates=max_rates, expected_tick_hz=tick_hz)
    requirements = inspect_pattern_requirements(payload)
    return {
        "size_bytes": len(payload), "mode": requirements.mode,
        "tick_hz": requirements.tick_hz, "active_pins": requirements.active_pins,
        "events": requirements.estimated_event_count,
        "samples": requirements.estimated_sample_count,
        "compares": requirements.estimated_compare_count,
    }


def _compile_policy(settings: dict[str, Any]) -> dict[str, Any]:
    requested = settings["compile"]["mode"]
    needs_rate = requested == "auto" and settings["wait"]["preset"] != "none"
    return {
        "kind": "stpg_runner_compile_policy", "schema_version": 1,
        "mode_requested": requested,
        "mode_effective": "rate-vector" if needs_rate else requested,
        "mode_reason": "explicit_chunk_wait_requires_rate_vector" if needs_rate else "project",
        "allow_approx": settings["compile"].get("allow_approx", False),
        "rounding": "nearest_tick_half_up", "physical_timing_validated": False,
    }


@rate_vector_decode_scope()
def _packages(project: Project, inputs: dict[str, Path], root: Path,
              progress: Reporter) -> tuple[str, str, list[dict[str, Any]]]:
    settings = project.to_dict()
    target, wait = settings["target"], settings["wait"]
    policy = _compile_policy(settings)
    write_json(root / "compile.policy.json", policy)
    progress.emit("compile_policy", "done", detail=(
        f"{policy['mode_requested']} -> {policy['mode_effective']}; "
        f"allow_approx={policy['allow_approx']}"))
    common = {
        "input_path": inputs["stil"], "pin_map_path": inputs.get("pin_map"),
        "pattern": settings["input"]["pattern"],
        "pattern_burst": settings["input"]["pattern_burst"],
        "mode": policy["mode_effective"], "loop_policy": settings["compile"]["loop_policy"],
        "allow_approx": policy["allow_approx"],
        "tick_hz": target["tick_hz"], "max_rates": target["max_rates"],
        "runtime_profile": target["runtime_profile"], "strict": True,
    }
    prepared = PreparedStilSource(inputs["stil"], pin_map_path=inputs.get("pin_map"),
                                  pattern=common["pattern"], pattern_burst=common["pattern_burst"],
                                  loop_policy=common["loop_policy"], retain_full_resolution=True)
    max_vectors = settings["segmentation"]["max_vectors"]
    capacity_first = False
    decision = None
    analysis = progress.perform("source_analysis", lambda: analyze_stil_source(
        **{key: value for key, value in common.items() if key not in {"strict", "allow_approx"}},
        prepared_source=prepared,
    ))
    write_json_artifact(root / "source.analysis.json", analysis, min_compress_bytes=32 * 1024, indent=None)
    if analysis.get("timing", {}).get("approximation_required") and not policy["allow_approx"]:
        raise RunnerError("ERR_APPROXIMATION_REQUIRED: source timing is off the target tick grid; "
                          "explicitly select compile.allow_approx=true / init --allow-approx to quantize; "
                          "segmentation and waits do not enable rounding")
    if "mode" in settings["segmentation"]:
        decision = segmentation_decision(settings, analysis)
        write_json(root / "segmentation.decision.json", decision)
        max_vectors = decision["planning_max_vectors"]
        capacity_first = decision.get("planning_strategy") == "capacity-first"
        progress.emit("segmentation", "done", detail=(
            f"{decision['package_kind']}; reason={decision['cause']}; "
            f"vectors={decision['source_vector_count']}; planning cap={max_vectors}; "
            f"strategy={decision['planning_strategy']}"))
    wait_us = chunk_wait_microseconds(wait)
    service = {
        "service_time_us": wait["service_us"], "service_time_source": wait["service_source"],
        "max_wait_us": wait_us if wait_us else None,
        "max_total_wait_us": wait["max_total_wait_us"],
        "service_profile_scope": wait["service_scope"] or "non-loop",
    }
    if max_vectors is not None or capacity_first:
        count = progress.perform("source_analysis", prepared.logical_vector_count)
        if capacity_first or count > max_vectors:
            ranges = (((0, count - 2), (count - 1, count - 1)) if capacity_first
                      else build_segment_ranges(count, segment_vector_count=max_vectors))
            progress.perform("segmented_compile", lambda: build_segmented_packages(
                **common, **service, output_dir=root / "segmented",
                report_path=root / "segmented.report.json", ranges=ranges, total_vectors=count,
                segment_wait_plan="every-boundary" if wait_us else None,
                forbidden_stil_rate_units=tuple(wait["forbidden_stil_rates"]),
                prepared_source=prepared, capacity_aware=True, capacity_first=capacity_first,
                compact_sidecars=True,
            ))  # capacity-aware splitting can change the final segment count
            manifest_path = root / "segmented" / "nominal" / "segmented.manifest.json"
            verification = verify_segmented_manifest(manifest_path)
            if verification["status"] != "PASS":
                raise RunnerError(f"segmented package verification rejected: {verification['errors']}")
            manifest = load_manifest(manifest_path)
            entries = [{"path": segment.path.relative_to(root).as_posix(),
                        "expected": _pattern_contract(segment.path)} for segment in manifest.segments]
            return "segmented", manifest_path.relative_to(root).as_posix(), entries
    base = root / "base-package"
    try:
        progress.perform("compile_package", lambda: build_pattern_package(
            PatternPackageOptions(**common, prepared_source=prepared, output_dir=base)))
    except ValueError as exc:
        # Enrich legacy/single failure text, without turning any failed compile
        # or wait into fallback. The prefix and original exception stay intact.
        if "source analysis is REJECT" in str(exc):
            analysis = read_json_artifact(base / "source.analysis.json")
            raise RunnerError(source_rejection(analysis)) from exc
        raise
    final = base
    if wait_us:
        progress.emit("wait_plan", "start")
        schedule, proposal = propose_wait_schedule(
            base / "pattern.bin", map_path=base / "pattern.map.json",
            max_rates=target["max_rates"], **service, wait_policy="every-boundary",
            forbidden_stil_rate_units=wait["forbidden_stil_rates"],
        )
        schedule_path = write_json_artifact(root / "wait.proposed.json", schedule, min_compress_bytes=32 * 1024, indent=None)
        write_json_artifact(root / "wait.proposal.json", proposal, min_compress_bytes=32 * 1024, indent=None)
        # A partial proposal retained for inspection is not an admitted plan.
        # A rejected every-boundary plan may contain zero waits and nevertheless
        # pass a separate low-service-cost ring simulation.
        _require_accepted_wait_plan(proposal)
        acceptance = evaluate_wait_schedule(
            base / "pattern.bin", schedule_path, map_path=base / "pattern.map.json",
            max_rates=target["max_rates"], **service,
        )
        acceptance_path = write_json_artifact(root / "wait.acceptance.json", acceptance, min_compress_bytes=32 * 1024, indent=None)
        if acceptance.get("status") != "ACCEPTED":
            raise RunnerError("explicit wait plan rejected; retained proposal is not executable")
        nonzero = sum(item.get("wait_ticks", 0) > 0 for item in schedule["boundaries"])
        if nonzero:
            final = root / "package"
            derive_pattern_package_with_wait(base, schedule_path=schedule_path,
                                             acceptance_path=acceptance_path, output_dir=final)
        progress.emit("wait_plan", "done", detail=f"applied boundaries={nonzero}; zero uses unchanged base")
    return "single", (final / "manifest.json").relative_to(root).as_posix(), [
        {"path": (final / "pattern.bin").relative_to(root).as_posix(),
         "expected": _pattern_contract(final / "pattern.bin")},
    ]


def _write_verifier(root: Path) -> None:
    (root / "verify.py").write_text(
        '"""Verify this bundle offline; this launcher cannot run a device."""\n'
        'from pathlib import Path\nfrom stpg_runner.cli import main\n'
        'if __name__ == "__main__":\n'
        '    raise SystemExit(main(["verify", str(Path(__file__).resolve().parent)]))\n',
        encoding="utf-8", newline="\n",
    )
    (root / "run.py").write_text(
        '"""Execution preview: requires device binding, manifest pin and Run intent."""\n'
        'from pathlib import Path\nimport sys\nfrom stpg_runner.cli import main\n'
        'if __name__ == "__main__":\n'
        '    raise SystemExit(main(["run", str(Path(__file__).resolve().parent), *sys.argv[1:]]))\n',
        encoding="utf-8", newline="\n",
    )


def build_project(project_path: Path, *, work_dir: Path | None = None,
                  on_progress: Callable[[ProgressEvent], None] | None = None,
                  cancel: CancellationToken | None = None) -> BuildResult:
    """Build/verify only. Identical inputs reuse a fully verified immutable bundle."""
    progress = Reporter(on_progress, cancel)
    progress.emit("preflight", "start")
    project_path = Path(project_path).resolve()
    project = load_project(project_path)
    inputs = project.input_paths(project_path.parent)
    binding = _binding(project, inputs)
    key = hashlib.sha256(canonical_json(binding)).hexdigest()
    workspace = Path(work_dir).resolve() if work_dir is not None else project_path.parent / "build"
    root = workspace / key
    progress.emit("preflight", "done")
    if root.exists():
        progress.emit("cache_verify", "start")
        verification = verify_bundle(root)
        if verification["status"] != "PASS" or verification.get("cache_key") != key:
            raise RunnerError(f"existing cache is incomplete/corrupt; not overwritten: {root}")
        progress.emit("cache_verify", "done")
        manifest = read_json(root / BUNDLE_NAME)
        return BuildResult(root, identity(root / BUNDLE_NAME)["sha256"], True,
                           manifest["package_kind"], len(manifest["patterns"]))
    workspace.mkdir(parents=True, exist_ok=True)
    required_bytes = sum(item["size_bytes"] for item in binding["inputs"].values()) + 16 * 1024 * 1024
    if shutil.disk_usage(workspace).free < required_bytes:
        raise RunnerError("insufficient workspace free space; choose a disk-backed --work-dir")
    root.mkdir()  # exclusive reservation: concurrent builds cannot overwrite one another
    try:
        write_json(root / "build.state.json", {"status": "BUILDING", "cache_key": key})
        write_json(root / "project.json", project.to_dict())
        snapshots: dict[str, Path] = {}
        storage: dict[str, Any] = {}
        progress.emit("snapshot_inputs", "start", total=len(inputs))
        for index, (role, source) in enumerate(inputs.items(), 1):
            destination = confined_path(root / "sources", binding["inputs"][role]["path"])
            destination, stored = copy_source_artifact(source, destination)
            expected = {name: binding["inputs"][role][name] for name in ("size_bytes", "sha256")}
            snapshot_identity = identity(destination) if stored["input_encoding"] == "gzip" else stored["decoded"]
            if snapshot_identity != expected or identity(source) != expected:
                raise RunnerError("project input changed while taking its build snapshot")
            snapshots[role] = destination
            storage[role] = {"path": destination.relative_to(root / "sources").as_posix(), **stored}
            progress.emit("snapshot_inputs", "progress", completed=index, total=len(inputs))
        progress.emit("snapshot_inputs", "done", completed=len(inputs), total=len(inputs))
        write_json(root / SOURCE_STORAGE_NAME, {"kind": SOURCE_STORAGE_KIND, "schema_version": 1, "inputs": storage})
        kind, package_manifest, patterns = _packages(project, snapshots, root, progress)
        progress.emit("bundle_verify", "start")
        if load_project(project_path).to_dict() != project.to_dict() or _binding(project, inputs) != binding:
            raise RunnerError("project/input/tool changed during build; immutable prefix retained")
        _write_verifier(root)
        write_json(root / "build.state.json", {"status": "HOST_ONLY_VERIFIED", "cache_key": key})
        files = {path.relative_to(root).as_posix(): identity(path)
                 for path in sorted(root.rglob("*")) if path.is_file()}
        manifest = {
            "kind": BUNDLE_KIND, "schema_version": 1, "status": "HOST_ONLY_VERIFIED",
            "physical_execution_authorized": False, "device_io_performed": False,
            "execution_available": True, "cache_key": key, "binding": binding,
            "package_kind": kind, "package_manifest": package_manifest,
            "patterns": patterns, "files": files,
        }
        write_json(root / BUNDLE_NAME, manifest)
        verification = verify_bundle(root)
        if verification["status"] != "PASS":
            raise RunnerError(f"bundle verification rejected: {verification['errors']}")
        progress.emit("bundle_verify", "done")
        return BuildResult(root, identity(root / BUNDLE_NAME)["sha256"], False, kind, len(patterns))
    except BaseException as exc:
        try:
            write_json(root / "build.state.json", {"status": "FAILED", "cache_key": key,
                                                  "error": f"{type(exc).__name__}: {exc}"})
        except OSError:
            pass  # original failure remains primary; no cleanup/retry removes the prefix
        raise


def _verify_inventory(root: Path, manifest: dict[str, Any], *, manifest_sha256: str | None,
                      progress: Reporter, stage: str = "bundle_files") -> None:
    """Exact byte/file-set guard; never a replacement for initial semantic validation."""
    manifest_path = confined_path(root, BUNDLE_NAME)
    if manifest_sha256 is not None and identity(manifest_path)["sha256"] != manifest_sha256:
        raise RunnerError("bundle manifest SHA-256 mismatch")
    files = manifest["files"]
    if not isinstance(files, dict) or not files or BUNDLE_NAME in files:
        raise RunnerError("bundle file inventory is missing/invalid")
    progress.emit(stage, "start", total=len(files))
    for index, (name, descriptor) in enumerate(files.items(), 1):
        progress.cancel.check()
        path = confined_path(root, name)
        if (not isinstance(descriptor, dict) or set(descriptor) != {"size_bytes", "sha256"}
                or type(descriptor["size_bytes"]) is not int or identity(path) != descriptor):
            raise RunnerError(f"bundle artifact mismatch: {name}")
        progress.emit(stage, "progress", completed=index, total=len(files))
    actual_files = set()
    for path in root.rglob("*"):
        progress.cancel.check()
        if path.is_symlink():
            raise RunnerError("bundle symlinks are not supported")
        if path.is_file() and path != manifest_path:
            actual_files.add(path.relative_to(root).as_posix())
    if actual_files != set(files):
        raise RunnerError("bundle has missing/unlisted files")
    if manifest_sha256 is not None and identity(manifest_path)["sha256"] != manifest_sha256:
        raise RunnerError("bundle manifest changed during inventory verification")
    progress.emit(stage, "done", completed=len(files), total=len(files))


def verify_bundle(bundle_dir: Path, *, manifest_sha256: str | None = None,
                  on_progress: Callable[[ProgressEvent], None] | None = None,
                  cancel: CancellationToken | None = None,
                  reuse: bool = False) -> dict[str, Any]:
    """Offline verification; optional reuse of this process's own semantic PASS.

    Reuse still hashes every inventoried file and checks paths/exact file set,
    manifest and installed compiler/SDK. A JSON report is never a receipt.
    Neither kind of check is permission to contact hardware.
    """
    progress = Reporter(on_progress, cancel)

    def check() -> dict[str, Any]:
        key = None
        try:
            root = Path(bundle_dir).resolve()
            pin = manifest_sha256 or identity(confined_path(root, BUNDLE_NAME))["sha256"]
            fingerprint = tool_fingerprint()
            key = (root, pin, fingerprint)
            with _receipt_lock:
                receipt = _verification_receipts.get(key) if reuse is True else None
            if receipt is not None:
                manifest = read_json(confined_path(root, BUNDLE_NAME), expected_sha256=pin)
                if canonical_json(manifest) != receipt[0]:
                    raise RunnerError("bundle manifest differs from the process receipt")
                _verify_inventory(root, manifest, manifest_sha256=pin, progress=progress)
                if tool_fingerprint() != fingerprint:
                    raise RunnerError("compiler/SDK changed during receipt verification")
                progress.emit("bundle_check", "done", detail="process_receipt: exact inventory rehashed")
                return {**deepcopy(receipt[1]), "check_mode": "process_receipt"}
            with rate_vector_decode_scope():
                result = _verify_bundle(root, manifest_sha256=pin, progress=progress)
            result["check_mode"] = "semantic"
            if result["status"] == "PASS":
                manifest = read_json(confined_path(root, BUNDLE_NAME), expected_sha256=pin)
                _verify_inventory(root, manifest, manifest_sha256=pin, progress=progress, stage="verified_files")
                if tool_fingerprint() != fingerprint:
                    raise RunnerError("compiler/SDK changed during semantic verification")
                manifest_bytes = canonical_json(manifest)
                if (manifest["binding"]["tool_sha256"] == fingerprint
                        and len(manifest_bytes) <= _VERIFICATION_RECEIPT_MANIFEST_BYTES):
                    with _receipt_lock:
                        _verification_receipts[key] = (manifest_bytes, deepcopy(result))
                        _verification_receipts.move_to_end(key)
                        while len(_verification_receipts) > _VERIFICATION_RECEIPT_LIMIT:
                            _verification_receipts.popitem(last=False)
                progress.emit("bundle_check", "done", detail="semantic: complete offline verification")
            return result
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            if key is not None:
                with _receipt_lock:
                    _verification_receipts.pop(key, None)
            return {"kind": "stpg_runner_bundle_validation", "schema_version": 1,
                    "status": "REJECT", "errors": [f"{type(exc).__name__}: {exc}"],
                    "cache_key": None, "check_mode": "rejected",
                    "device_io_performed": False, "physical_execution_authorized": False,
                    "execution_available": True}

    return progress.perform("bundle_verify", check)


def _verify_bundle(bundle_dir: Path, *, manifest_sha256: str | None,
                   progress: Reporter) -> dict[str, Any]:
    """Confined, offline integrity and semantic validation; no execution fallback."""
    root = Path(bundle_dir).resolve()
    errors: list[str] = []
    cache_key: str | None = None
    try:
        manifest_path = confined_path(root, BUNDLE_NAME)
        if manifest_sha256 is not None and identity(manifest_path)["sha256"] != manifest_sha256:
            raise RunnerError("bundle manifest SHA-256 mismatch")
        manifest = read_json(manifest_path, expected_sha256=manifest_sha256)
        fields = {"kind", "schema_version", "status", "physical_execution_authorized",
                  "device_io_performed", "execution_available", "cache_key", "binding",
                  "package_kind", "package_manifest", "patterns", "files"}
        if set(manifest) != fields:
            raise RunnerError("bundle has missing/unknown fields")
        if manifest.get("kind") != BUNDLE_KIND or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
            raise RunnerError("unsupported bundle kind/schema_version")
        if manifest.get("status") != "HOST_ONLY_VERIFIED" or any(
            manifest.get(field) is not False for field in ("physical_execution_authorized", "device_io_performed")
        ) or manifest.get("execution_available") is not True:
            raise RunnerError("bundle must be host-only and must not claim execution admission")
        binding = manifest["binding"]
        binding_fields = {"project", "inputs", "tool_sha256", "python"}
        if not isinstance(binding, dict) or set(binding) not in (binding_fields, binding_fields | {"rebind"}):
            raise RunnerError("invalid bundle binding fields")
        cache_key = hashlib.sha256(canonical_json(binding)).hexdigest()
        if manifest["cache_key"] != cache_key:
            raise RunnerError("bundle cache binding mismatch")
        project = load_project(confined_path(root, "project.json"))
        if canonical_json(project.to_dict()) != canonical_json(binding["project"]):
            raise RunnerError("bundle project differs from binding")
        if read_json(confined_path(root, "build.state.json")) != {"status": "HOST_ONLY_VERIFIED", "cache_key": cache_key}:
            raise RunnerError("build did not complete")
        _verify_inventory(root, manifest, manifest_sha256=manifest_sha256, progress=progress)
        from .rebinding import validate_rebind_provenance
        validate_rebind_provenance(root, manifest)
        files = manifest["files"]
        # Older bundles without the new field/policy remain inspectable. Every
        # new project and every policy-bearing bundle must bind the decision.
        if "allow_approx" in project.to_dict()["compile"] or "compile.policy.json" in files:
            if ("compile.policy.json" not in files or canonical_json(read_json(root / "compile.policy.json"))
                    != canonical_json(_compile_policy(project.to_dict()))):
                raise RunnerError("bundle compile policy differs from project")
        inputs = bundle_source_paths(root, manifest=manifest)
        if set(inputs) != set(binding["inputs"]):
            raise RunnerError("bundle source-role mismatch")
        for role, path in inputs.items():
            descriptor = binding["inputs"][role]
            if descriptor["path"] != project.to_dict()["input"][role]:
                raise RunnerError(f"bundle source path differs from project: {role}")
        package_manifest = confined_path(root, manifest["package_manifest"])
        if package_manifest.relative_to(root).as_posix() not in files:
            raise RunnerError("package manifest is not inventoried")
        if manifest["package_kind"] == "single":
            if package_manifest.name != "manifest.json":
                raise RunnerError("invalid single-package manifest name")
            if project.to_dict()["wait"]["preset"] != "none":
                proposal_path = resolve_json_artifact(root / "wait.proposal.json")
                proposal_path = confined_path(root, proposal_path.relative_to(root).as_posix())
                if proposal_path.relative_to(root).as_posix() not in files:
                    raise RunnerError("wait proposal is not inventoried")
                _require_accepted_wait_plan(read_json_artifact(proposal_path))
            verification = verify_pattern_package(package_manifest.parent)
            paths = [package_manifest.parent / "pattern.bin"]
        elif manifest["package_kind"] == "segmented":
            segmented = read_json(package_manifest)
            for index, segment in enumerate(segmented["segments"]):
                for field in ("path", "map_path", "package_manifest_path", "wait_schedule.path"):
                    value = (segment["wait_schedule"]["path"] if field == "wait_schedule.path"
                             else segment[field])
                    try:
                        path = confined_path(package_manifest.parent, value)
                    except RunnerError as exc:
                        raise RunnerError(f"segment[{index}].{field}: {exc}") from exc
                    if field != "wait_schedule.path" and path.relative_to(root).as_posix() not in files:
                        raise RunnerError(f"segment[{index}].{field}: reference is not inventoried: {value!r}")
            verification = verify_segmented_manifest(package_manifest)
            paths = [segment.path for segment in load_manifest(package_manifest).segments]
        else:
            raise RunnerError("unsupported bundle package_kind")
        if verification["status"] != "PASS":
            raise RunnerError(f"package validation rejected: {verification['errors']}")
        policy = _compile_policy(project.to_dict())
        for path in paths:
            metadata = read_json_artifact(path.parent / "pattern.metadata.json")
            options = metadata.get("options", {})
            if (options.get("allow_approx") is not policy["allow_approx"]
                    or options.get("mode") != policy["mode_effective"]
                    or options.get("loop_policy") != project.to_dict()["compile"]["loop_policy"]):
                raise RunnerError("package compile options differ from bound project policy")
            analysis = read_json_artifact(path.parent / "source.analysis.json")
            if not policy["allow_approx"] and analysis.get("timing", {}).get("approximation_required"):
                raise RunnerError("exact project contains a timing-approximated package")
        target = project.to_dict()["target"]
        expected_patterns = [{"path": path.relative_to(root).as_posix(),
                              "expected": _pattern_contract(path, max_rates=target["max_rates"],
                                                            tick_hz=target["tick_hz"])}
                             for path in paths]
        if canonical_json(manifest["patterns"]) != canonical_json(expected_patterns):
            raise RunnerError("bundle pattern expectations differ from decoded binaries")
        if "mode" in project.to_dict()["segmentation"]:
            decision_path = confined_path(root, "segmentation.decision.json")
            analysis = read_json_artifact(root / "source.analysis.json")
            saved_decision = read_json(decision_path)
            if type(saved_decision.get("schema_version")) is not int or saved_decision["schema_version"] not in {1, 2}:
                raise RunnerError("unsupported segmentation decision version")
            decision = segmentation_decision(project.to_dict(), analysis, legacy=saved_decision["schema_version"] == 1)
            if saved_decision != decision or manifest["package_kind"] != decision["package_kind"]:
                raise RunnerError("bundle segmentation decision differs from source analysis/project")
            if decision_path.relative_to(root).as_posix() not in files:
                raise RunnerError("bundle segmentation decision is not inventoried")
            if manifest["package_kind"] == "segmented":
                if (len(expected_patterns) < 2
                        or segmented["pattern_contract"]["source_vector_count"] != decision["source_vector_count"]
                        or (decision["planning_max_vectors"] is not None and any(
                            segment["global_vector_end"] - segment["global_vector_start"] + 1
                            > decision["planning_max_vectors"] for segment in segmented["segments"]))):
                    raise RunnerError("segment coverage/cap differs from the recorded decision")
        for item in expected_patterns:
            if item["expected"]["tick_hz"] != target["tick_hz"]:
                raise RunnerError("pattern clock differs from project clock")
        if not {"project.json", "build.state.json", "verify.py", "run.py"} <= files.keys():
            raise RunnerError("bundle inventory lacks required runner files")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return {
        "kind": "stpg_runner_bundle_validation", "schema_version": 1,
        "status": "REJECT" if errors else "PASS", "errors": errors,
        "cache_key": cache_key, "device_io_performed": False,
        "physical_execution_authorized": False, "execution_available": True,
    }
