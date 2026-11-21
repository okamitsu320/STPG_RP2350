"""Self-contained source snapshots with both stored and decoded bindings."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from stil.source_artifacts import decoded_source_identity, source_encoding, open_source_binary
from .artifacts import RunnerError, confined_path, identity, read_json

SOURCE_STORAGE_NAME = "source.storage.json"
SOURCE_STORAGE_KIND = "stpg_runner_source_storage"


def bundle_source_paths(root: Path, *, manifest: dict[str, Any] | None = None) -> dict[str, Path]:
    """Verify source storage/bindings, not the rest of the bundle or Run intent.

    Legacy bundles have no descriptor: their snapshots must match stored input
    bytes exactly. New snapshots may compress a plain input but may not change
    its decoded bytes. A compressed input is copied byte-for-byte, not repinned.
    """
    root = Path(root).resolve()
    if manifest is None:
        manifest = read_json(confined_path(root, "runner.bundle.json"))
    inputs, files = manifest["binding"]["inputs"], manifest["files"]
    declared = manifest["binding"]["project"]["input"]
    required_roles = {"stil", "pin_map"} if declared.get("pin_map") is not None else {"stil"}
    if not isinstance(inputs, dict) or set(inputs) != required_roles:
        raise RunnerError("bundle source roles differ from the project")
    source_root = confined_path(root, "sources")
    storage = None
    if SOURCE_STORAGE_NAME in files:
        descriptor_path = confined_path(root, SOURCE_STORAGE_NAME)
        if identity(descriptor_path) != files[SOURCE_STORAGE_NAME]:
            raise RunnerError("source storage descriptor differs from the bundle inventory")
        storage = read_json(descriptor_path)
        if (set(storage) != {"kind", "schema_version", "inputs"}
                or storage["kind"] != SOURCE_STORAGE_KIND or type(storage["schema_version"]) is not int
                or storage["schema_version"] != 1 or not isinstance(storage["inputs"], dict)
                or set(storage["inputs"]) != set(inputs)):
            raise RunnerError("invalid source storage descriptor/roles")
    result = {}
    for role, bound in inputs.items():
        if (not isinstance(bound, dict) or set(bound) != {"path", "size_bytes", "sha256"}
                or type(bound["size_bytes"]) is not int or bound["size_bytes"] < 0
                or not isinstance(bound["sha256"], str) or len(bound["sha256"]) != 64
                or any(ch not in "0123456789abcdef" for ch in bound["sha256"])):
            raise RunnerError(f"invalid source binding: {role}")
        if bound["path"] != declared[role]:
            raise RunnerError(f"source binding path differs from the project: {role}")
        # Validate the bound logical project path even when storage has a
        # distinct compressed path. Neither path can supply an external file.
        confined_path(source_root, bound["path"])
        entry = storage["inputs"][role] if storage is not None else None
        if storage is not None:
            if (not isinstance(entry, dict) or set(entry) != {"path", "input_encoding", "encoding", "decoded"}
                    or not isinstance(entry["input_encoding"], str) or entry["input_encoding"] not in {"identity", "gzip"}
                    or not isinstance(entry["encoding"], str) or entry["encoding"] not in {"identity", "gzip"}
                    or not isinstance(entry["decoded"], dict) or set(entry["decoded"]) != {"size_bytes", "sha256"}
                    or type(entry["decoded"]["size_bytes"]) is not int or entry["decoded"]["size_bytes"] < 0
                    or not isinstance(entry["decoded"]["sha256"], str) or len(entry["decoded"]["sha256"]) != 64
                    or any(ch not in "0123456789abcdef" for ch in entry["decoded"]["sha256"])):
                raise RunnerError(f"invalid source storage entry: {role}")
            path = confined_path(source_root, entry["path"])
        else:
            path = confined_path(source_root, bound["path"])
        name = path.relative_to(root).as_posix()
        if name not in files:
            raise RunnerError(f"source snapshot is missing/unlisted/changed: {role}")
        actual = identity(path)
        if actual != files[name]:
            raise RunnerError(f"source snapshot is missing/unlisted/changed: {role}")
        expected = {key: bound[key] for key in ("size_bytes", "sha256")}
        if entry is None:
            if actual != expected:
                raise RunnerError(f"bundle source mismatch: {role}")
            if source_encoding(path) == "gzip":
                decoded_source_identity(path)  # consume the complete member/CRC, even for a legacy layout
        else:
            decoded = decoded_source_identity(path)
            if source_encoding(path) != entry["encoding"] or decoded != entry["decoded"]:
                raise RunnerError(f"source snapshot encoding/decoded identity mismatch: {role}")
            if entry["input_encoding"] == "gzip":
                if entry["encoding"] != "gzip" or actual != expected:
                    raise RunnerError(f"compressed source differs from its input binding: {role}")
            else:
                if str(bound["path"]).endswith(".gz"):
                    raise RunnerError(f"gzip input cannot be declared plain: {role}")
                with open_source_binary(path) as plain_input:
                    if plain_input.read(2) == b"\x1f\x8b":
                        raise RunnerError(f"source snapshot must not disguise nested gzip as a plain input: {role}")
                if decoded != expected:
                    raise RunnerError(f"decoded source differs from its input binding: {role}")
        result[role] = path
    if len(set(result.values())) != len(result):
        raise RunnerError("source roles must not alias a snapshot")
    return result
