"""Small deterministic artifacts and confined portable paths; no device imports."""

from __future__ import annotations

import hashlib
import gzip
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile
import zlib
from typing import Any
from stil.source_artifacts import read_source_text


class RunnerError(ValueError):
    """An invalid project or unverified artifact must not be used."""


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RunnerError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path, *, expected_sha256: str | None = None) -> dict[str, Any]:
    try:
        if expected_sha256 is None:
            text = read_source_text(path) if path.suffix == ".gz" else path.read_text(encoding="utf-8")
        else:
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != expected_sha256:
                raise RunnerError(f"JSON SHA-256 mismatch: {path.name}")
            text = (gzip.decompress(payload) if path.suffix == ".gz" else payload).decode("utf-8")
        value = json.loads(text,
                           object_pairs_hook=_unique_object,
                           parse_constant=lambda value: _bad_constant(value))
    except (OSError, UnicodeError, ValueError, EOFError, zlib.error) as exc:
        raise RunnerError(f"cannot read JSON {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunnerError(f"{path.name}: JSON root must be an object")
    return value


def _bad_constant(value: str) -> None:
    raise RunnerError(f"non-finite JSON number: {value}")


def write_json(path: Path, value: Any) -> None:
    """Publish one JSON file atomically; not a multi-file transaction."""
    payload = canonical_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".stpg-", delete=False) as stream:
            temporary = stream.name
            if path.suffix == ".gz":
                # No temporary filename or wall clock in the gzip header.
                with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0, compresslevel=6) as compressed:
                    compressed.write(payload)
            else:
                stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def identity(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RunnerError(f"not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def portable_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RunnerError(f"path must be a non-empty relative POSIX path: {value!r}")
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if posix.is_absolute() or windows.drive or any(part in {".", "..", ""} for part in value.split("/")):
        raise RunnerError(f"path is not portable/confined: {value!r}")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if any(any(character in '<>:"|?*' or ord(character) < 32 for character in part)
           or part.endswith((".", " ")) or part.split(".")[0].upper() in reserved
           for part in posix.parts):
        raise RunnerError(f"path contains a Windows-reserved name/character: {value!r}")
    return value


def confined_path(root: Path, value: Any) -> Path:
    relative = portable_path(value)
    root = root.resolve()
    path = root.joinpath(*PurePosixPath(relative).parts).resolve()
    if not path.is_relative_to(root):
        raise RunnerError(f"path escapes root (including symlinks): {relative}")
    return path
