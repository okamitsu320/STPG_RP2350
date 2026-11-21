"""Explicit offline migration, not a compiler, run fallback or hardware admission."""

from copy import deepcopy
import hashlib
from pathlib import Path
import platform
import shutil

from .artifacts import RunnerError, canonical_json, confined_path, identity, read_json, write_json
from .build import BUNDLE_NAME, BuildResult, _verify_inventory, tool_fingerprint, verify_bundle
from .progress import Reporter

REBIND_WARNING = "Compiled with an older SDK; later compiler fixes are not included."
_BASE_BINDING_FIELDS = {"project", "inputs", "tool_sha256", "python"}
_FIELDS = {"kind", "schema_version", "source_manifest", "source_manifest_sha256",
           "source_cache_key", "source_tool_sha256", "source_python",
           "compile_tool_sha256", "compile_python", "warning"}


def _digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise RunnerError("rebind requires exact lowercase SHA-256 identities")
    return value


def validate_rebind_provenance(root: Path, manifest: dict, *, depth: int = 0) -> None:
    """Check the hash-bound origin chain and unchanged artifact inventory."""
    binding = manifest["binding"]
    if "rebind" not in binding:
        return
    if depth >= 32:
        raise RunnerError("rebind origin chain exceeds 32 migrations; recompile the source")
    value = binding["rebind"]
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or value["kind"] != "stpg_bundle_rebind" or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["warning"] != REBIND_WARNING):
        raise RunnerError("invalid rebind provenance fields")
    pin = _digest(value["source_manifest_sha256"])
    name = f"provenance/bundle-{pin}.json"
    if value["source_manifest"] != name or name not in manifest["files"]:
        raise RunnerError("rebind origin is not hash-named and inventoried")
    origin = read_json(confined_path(root, name), expected_sha256=pin)
    if set(origin) != set(manifest):
        raise RunnerError("rebind origin schema differs")
    old = origin["binding"]
    if not isinstance(old, dict) or set(old) not in (_BASE_BINDING_FIELDS, _BASE_BINDING_FIELDS | {"rebind"}):
        raise RunnerError("invalid rebind origin binding")
    if not isinstance(old["python"], str) or not old["python"].strip() or old["tool_sha256"] == binding["tool_sha256"]:
        raise RunnerError("invalid or unnecessary rebind origin environment")
    old_key = hashlib.sha256(canonical_json(old)).hexdigest()
    compiled = old.get("rebind", {})
    if (origin["cache_key"] != old_key or value["source_cache_key"] != old_key
            or value["source_tool_sha256"] != _digest(old["tool_sha256"])
            or value["source_python"] != old["python"]
            or value["compile_tool_sha256"] != compiled.get("compile_tool_sha256", old["tool_sha256"])
            or value["compile_python"] != compiled.get("compile_python", old["python"])
            or binding["project"] != old["project"] or binding["inputs"] != old["inputs"]):
        raise RunnerError("rebind source/compile identity differs")
    for field in set(origin) - {"binding", "cache_key", "files"}:
        if canonical_json(origin[field]) != canonical_json(manifest[field]):
            raise RunnerError("rebind changed package/pattern metadata")
    if set(manifest["files"]) != set(origin["files"]) | {name} or name in origin["files"]:
        raise RunnerError("rebind artifact inventory differs from origin")
    for path, descriptor in origin["files"].items():
        if path == "build.state.json":
            expected = canonical_json({"status": "HOST_ONLY_VERIFIED", "cache_key": old_key})
            if descriptor != {"size_bytes": len(expected), "sha256": hashlib.sha256(expected).hexdigest()}:
                raise RunnerError("rebind origin build state differs")
        elif manifest["files"][path] != descriptor:
            raise RunnerError("rebind changed an original artifact")
    validate_rebind_provenance(root, origin, depth=depth + 1)


def rebind_bundle(bundle_dir: Path, *, manifest_sha256: str, output_dir: Path | None = None,
                  on_progress=None, cancel=None) -> BuildResult:
    """Verify and copy unchanged artifacts to a new current-SDK binding, without compilation.

    The original stays immutable. A matching SDK is a verified no-op. Existing
    destinations and failed prefixes are never overwritten or reused.
    """
    pin = _digest(manifest_sha256)
    root = Path(bundle_dir).resolve()
    progress = Reporter(on_progress, cancel)
    fingerprint = tool_fingerprint()
    origin = read_json(confined_path(root, BUNDLE_NAME), expected_sha256=pin)
    checked = verify_bundle(root, manifest_sha256=pin, on_progress=on_progress, cancel=progress.cancel)
    if checked["status"] != "PASS":
        raise RunnerError(f"old bundle failed current semantic verification: {checked['errors']}")
    if tool_fingerprint() != fingerprint:
        raise RunnerError("SDK changed during old-bundle verification")
    old = origin["binding"]
    _digest(old["tool_sha256"])
    if old["tool_sha256"] == fingerprint:
        return BuildResult(root, pin, True, origin["package_kind"], len(origin["patterns"]))
    binding = deepcopy(old)
    inherited = old.get("rebind", {})
    origin_name = f"provenance/bundle-{pin}.json"
    binding.update(tool_sha256=fingerprint, python=platform.python_version(), rebind={
        "kind": "stpg_bundle_rebind", "schema_version": 1,
        "source_manifest": origin_name, "source_manifest_sha256": pin,
        "source_cache_key": origin["cache_key"], "source_tool_sha256": old["tool_sha256"],
        "source_python": old["python"],
        "compile_tool_sha256": inherited.get("compile_tool_sha256", old["tool_sha256"]),
        "compile_python": inherited.get("compile_python", old["python"]), "warning": REBIND_WARNING,
    })
    key = hashlib.sha256(canonical_json(binding)).hexdigest()
    requested = Path(output_dir) if output_dir is not None else root.parent / key
    if requested.exists() or requested.is_symlink():
        raise FileExistsError("rebind destination already exists; never overwritten")
    destination = requested.resolve()
    if destination == root or destination.is_relative_to(root) or root.is_relative_to(destination):
        raise RunnerError("rebind destination must be separate from the immutable old bundle")
    progress.cancel.check()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()  # exclusive reservation, also after a crash
    try:
        progress.emit("rebind_copy", "start", total=len(origin["files"]))
        for index, (name, descriptor) in enumerate(origin["files"].items(), 1):
            progress.cancel.check()
            source, target = confined_path(root, name), confined_path(destination, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if identity(source) != descriptor or identity(target) != descriptor:
                raise RunnerError("old bundle changed during rebind copy")
            progress.emit("rebind_copy", "progress", completed=index, total=len(origin["files"]))
        if origin_name in origin["files"]:
            raise RunnerError("rebind origin path collision")
        source_manifest = confined_path(destination, origin_name)
        source_manifest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(confined_path(root, BUNDLE_NAME), source_manifest)
        if identity(source_manifest)["sha256"] != pin:
            raise RunnerError("old manifest changed during rebind")
        manifest = deepcopy(origin)
        manifest.update(binding=binding, cache_key=key)
        write_json(destination / "build.state.json", {"status": "HOST_ONLY_VERIFIED", "cache_key": key})
        manifest["files"]["build.state.json"] = identity(destination / "build.state.json")
        manifest["files"][origin_name] = identity(source_manifest)
        write_json(destination / BUNDLE_NAME, manifest)
        _verify_inventory(root, origin, manifest_sha256=pin, progress=progress)
        result = verify_bundle(destination, on_progress=on_progress, cancel=progress.cancel)
        if result["status"] != "PASS":
            raise RunnerError(f"rebound bundle failed current semantic verification: {result['errors']}")
        from .execution import prepare_run
        new_pin = identity(destination / BUNDLE_NAME)["sha256"]
        prepare_run(destination, manifest_sha256=new_pin, cancel=progress.cancel, on_progress=on_progress)
        if tool_fingerprint() != fingerprint:
            raise RunnerError("SDK changed during rebind")
        progress.emit("rebind_copy", "done", completed=len(origin["files"]), total=len(origin["files"]))
        return BuildResult(destination, new_pin, False, manifest["package_kind"], len(manifest["patterns"]))
    except BaseException as exc:
        try:
            write_json(destination / "build.state.json", {"status": "FAILED", "cache_key": key,
                                                         "error": f"{type(exc).__name__}: {exc}"})
        except OSError:
            pass
        raise
