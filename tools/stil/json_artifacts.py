"""Deterministic JSON sidecar storage with transparent gzip reads.

Package v3 uses gzip for sufficiently large sidecars when it reduces the
stored size. JSON objects are still ordinary UTF-8 JSON after decompression;
binary pattern payloads are unchanged. The reader accepts legacy uncompressed
package v1/v2 files.
"""

from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterable, Iterator, TextIO
import zlib


JSON_GZIP_MIN_BYTES = 1024
JSON_GZIP_ENCODING = "gzip"
JSON_GZIP_LEVEL = 6
COMPACT_VECTOR_MAP_MARKER = "_stpg_vectors_encoding"
COMPACT_VECTOR_MAP_ENCODING = "implicit-ordinal-array-v1"
COMPACT_REPORT_MARKER = "_stpg_report_encoding"
COMPACT_REPORT_ENCODING = "normalized-transition-aliases-v1"
_REPORT_ALIASES = ("warnings", "source_analysis.normalization.transition_summary")
_JSON_BATCH_NODES = 512
_JSON_BATCH_CHARS = 16 * 1024


def _fits_native_json_batch(value: Any) -> bool:
    """Bound a native encoding to a small subtree, not the whole document.

    This walk has a fixed node/character budget even for cyclic input; cycle
    rejection remains the encoder's job. A single unusually large scalar is
    encoded on its own, as with the standard streaming encoder.
    """

    pending = [value]
    nodes = chars = 0
    while pending:
        item = pending.pop()
        nodes += 1
        if nodes > _JSON_BATCH_NODES:
            return False
        if isinstance(item, str):
            chars += len(item)
        elif isinstance(item, dict):
            if type(item) is not dict:
                return False
            if len(item) + len(pending) > _JSON_BATCH_NODES - nodes:
                return False
            chars += sum(len(key) for key in item if isinstance(key, str))
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            if type(item) not in (list, tuple):
                return False
            if len(item) + len(pending) > _JSON_BATCH_NODES - nodes:
                return False
            pending.extend(item)
        if chars > _JSON_BATCH_CHARS:
            return False
    return True


def _iter_native_compact_json(value: Any, *, sort_keys: bool, active: set[int]) -> Iterator[str]:
    """Match default compact json.dump bytes using small one-shot encodings."""

    if isinstance(value, (dict, list, tuple)) and type(value) not in (dict, list, tuple):
        # Subclasses can override items/iteration. Keep their established
        # streaming semantics rather than reconstructing them by key lookup.
        yield from json.JSONEncoder(sort_keys=sort_keys).iterencode(value)
        return
    if _fits_native_json_batch(value) or not isinstance(value, (dict, list, tuple)):
        # indent=None takes the native one-shot path on supported CPython
        # versions. Other interpreters retain standard-library semantics.
        yield json.dumps(value, sort_keys=sort_keys)
        return
    identity = id(value)
    if identity in active:
        raise ValueError("Circular reference detected")
    active.add(identity)
    try:
        if isinstance(value, dict):
            yield "{"
            keys = sorted(value) if sort_keys else value
            for index, key in enumerate(keys):
                if index:
                    yield ", "
                # Let the standard encoder handle int/float/bool/None keys,
                # escaping and invalid-key errors, not a new coercion rule.
                yield json.dumps({key: None}, sort_keys=sort_keys)[1:-5]
                yield from _iter_native_compact_json(value[key], sort_keys=sort_keys, active=active)
            yield "}"
        else:
            yield "["
            for index, item in enumerate(value):
                if index:
                    yield ", "
                yield from _iter_native_compact_json(item, sort_keys=sort_keys, active=active)
            yield "]"
    finally:
        active.remove(identity)


def _write_compact_json(stream: TextIO, value: Any, *, sort_keys: bool) -> None:
    parts: list[str] = []
    size = 0
    for part in _iter_native_compact_json(value, sort_keys=sort_keys, active=set()):
        parts.append(part)
        size += len(part)
        if size >= 64 * 1024:
            stream.write("".join(parts))
            parts.clear()
            size = 0
    if parts:
        stream.write("".join(parts))


class JsonArtifactError(ValueError):
    """An artifact cannot be safely decoded as a JSON object."""


@dataclass(frozen=True)
class JsonArtifactIdentity:
    path: str
    stored_size_bytes: int
    stored_sha256: str
    encoding: str
    uncompressed_size_bytes: int
    uncompressed_sha256: str


def write_json_gzip_chunks(path: Path, chunks: Iterable[str]) -> Path:
    """Atomically write already-encoded JSON chunks directly to deterministic gzip.

    Unlike the auto-size writer, this explicitly gzip-only path never creates
    a large plain JSON temporary. The producer is responsible for JSON syntax.
    An incomplete producer/write leaves the previous published artifact intact.
    """
    plain = Path(str(path)[:-3]) if str(path).endswith(".gz") else path
    output = Path(str(plain) + ".gz")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output.parent, prefix=f".{output.name}.",
            suffix=".tmp", delete=False,
        ) as stored:
            temporary = Path(stored.name)
            with gzip.GzipFile(fileobj=stored, mode="wb", filename="", mtime=0,
                               compresslevel=JSON_GZIP_LEVEL) as compressed:
                parts: list[str] = []
                size = 0
                for part in chunks:
                    parts.append(part)
                    size += len(part)
                    if size >= 64 * 1024:
                        compressed.write("".join(parts).encode("utf-8"))
                        parts.clear()
                        size = 0
                if parts:
                    compressed.write("".join(parts).encode("utf-8"))
        os.replace(temporary, output)
        temporary = None
        plain.unlink(missing_ok=True)
        return output
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json_artifact(
    path: Path,
    value: Any,
    *,
    compression: str = "auto",
    min_compress_bytes: int = JSON_GZIP_MIN_BYTES,
    indent: int | None = 2,
    sort_keys: bool = True,
    compact_vector_map: bool = False,
    compact_compile_report: bool = False,
) -> Path:
    """Write JSON and return its actual path.

    ``auto`` compresses only when the uncompressed payload is large enough and
    the gzip member is smaller. ``gzip`` always writes a ``.gz`` member, while
    ``none`` always writes plain JSON. The exact counterpart is removed so
    future readers never have to guess which generation is current.
    """

    if compression not in {"auto", "gzip", "none"}:
        raise ValueError("compression must be auto, gzip, or none")
    if compact_vector_map:
        value = _encode_compact_vector_map(value)
    if compact_compile_report:
        value = _encode_compact_compile_report(value)
    plain_path = Path(str(path)[:-3]) if str(path).endswith(".gz") else path
    gzip_path = Path(str(plain_path) + ".gz")
    plain_path.parent.mkdir(parents=True, exist_ok=True)
    plain_temp_name: str | None = None
    gzip_temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=plain_path.parent,
            prefix=f".{plain_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as text_stream:
            plain_temp_name = text_stream.name
            if indent is None:
                _write_compact_json(text_stream, value, sort_keys=sort_keys)
            else:
                json.dump(value, text_stream, indent=indent, sort_keys=sort_keys)
            text_stream.write("\n")

        plain_temp = Path(plain_temp_name)
        raw_size = plain_temp.stat().st_size
        use_gzip = compression == "gzip"
        if compression == "auto" and raw_size >= min_compress_bytes:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=plain_path.parent,
                prefix=f".{gzip_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as binary_stream:
                gzip_temp_name = binary_stream.name
                with gzip.GzipFile(
                    fileobj=binary_stream,
                    mode="wb",
                    filename="",
                    mtime=0,
                    compresslevel=JSON_GZIP_LEVEL,
                ) as gzip_stream:
                    with plain_temp.open("rb") as source_stream:
                        shutil.copyfileobj(source_stream, gzip_stream, length=1024 * 1024)
            use_gzip = Path(gzip_temp_name).stat().st_size < raw_size

        if use_gzip:
            if gzip_temp_name is None:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=plain_path.parent,
                    prefix=f".{gzip_path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as binary_stream:
                    gzip_temp_name = binary_stream.name
                    with gzip.GzipFile(
                        fileobj=binary_stream,
                        mode="wb",
                        filename="",
                        mtime=0,
                        compresslevel=JSON_GZIP_LEVEL,
                    ) as gzip_stream:
                        with plain_temp.open("rb") as source_stream:
                            shutil.copyfileobj(source_stream, gzip_stream, length=1024 * 1024)
            os.replace(gzip_temp_name, gzip_path)
            gzip_temp_name = None
            selected = gzip_path
            stale = plain_path
        else:
            os.replace(plain_temp_name, plain_path)
            plain_temp_name = None
            selected = plain_path
            stale = gzip_path

        if stale != selected and stale.exists():
            stale.unlink()
        return selected
    finally:
        for temporary in (plain_temp_name, gzip_temp_name):
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass


def resolve_json_artifact(path: Path) -> Path:
    """Resolve a logical .json path to its stored .json or .json.gz member."""

    path = Path(path)
    if path.is_file():
        return path
    if str(path).endswith(".gz"):
        plain = Path(str(path)[:-3])
        if plain.is_file():
            return plain
    else:
        compressed = Path(str(path) + ".gz")
        if compressed.is_file():
            return compressed
    return path


def read_json_artifact(path: Path) -> Any:
    """Decode a JSON artifact from plain UTF-8 or gzip-compressed UTF-8."""

    actual = resolve_json_artifact(path)
    try:
        with actual.open("rb") as probe:
            compressed = probe.read(2) == b"\x1f\x8b"
        stream = gzip.open(actual, "rt", encoding="utf-8") if compressed else actual.open("r", encoding="utf-8")
        with stream:
            value = json.load(stream)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, gzip.BadGzipFile, EOFError, zlib.error) as exc:
        raise JsonArtifactError(f"cannot read JSON artifact {actual}: {exc}") from exc
    return _decode_compact_compile_report(_decode_compact_vector_map(value))


def _encode_compact_compile_report(value: Any) -> Any:
    """Store repeated normalization descriptions once, with explicit aliases.

    Only the package writer opts into this format; ordinary CLI reports keep
    their established schema. No diagnostics, counts or strings are dropped.
    """

    if not isinstance(value, dict) or COMPACT_REPORT_MARKER in value:
        return value
    summary = value.get("normalized_transition_summary")
    if not isinstance(summary, list) or not summary or not all(isinstance(item, str) for item in summary):
        return value
    compact = dict(value)
    aliases = []
    if value.get("warnings") == summary:
        compact.pop("warnings")
        aliases.append("warnings")
    analysis = value.get("source_analysis")
    normalization = analysis.get("normalization") if isinstance(analysis, dict) else None
    if isinstance(normalization, dict) and normalization.get("transition_summary") == summary:
        compact["source_analysis"] = dict(analysis)
        compact["source_analysis"]["normalization"] = dict(normalization)
        compact["source_analysis"]["normalization"].pop("transition_summary")
        aliases.append("source_analysis.normalization.transition_summary")
    if not aliases:
        return value
    compact[COMPACT_REPORT_MARKER] = {"encoding": COMPACT_REPORT_ENCODING, "aliases": aliases}
    return compact


def _decode_compact_compile_report(value: Any) -> Any:
    """Restore legacy field values; reject unknown or ambiguous references."""

    if not isinstance(value, dict) or COMPACT_REPORT_MARKER not in value:
        return value
    marker = value[COMPACT_REPORT_MARKER]
    summary = value.get("normalized_transition_summary")
    if (not isinstance(marker, dict) or set(marker) != {"encoding", "aliases"}
            or marker.get("encoding") != COMPACT_REPORT_ENCODING
            or not isinstance(summary, list) or not all(isinstance(item, str) for item in summary)):
        raise JsonArtifactError("unsupported or malformed compact compile-report encoding")
    aliases = marker.get("aliases")
    if (not isinstance(aliases, list) or not aliases or len(aliases) > len(_REPORT_ALIASES)
            or any(not isinstance(alias, str) or alias not in _REPORT_ALIASES for alias in aliases)
            or len(set(aliases)) != len(aliases)):
        raise JsonArtifactError("invalid compact compile-report aliases")
    expanded = dict(value)
    expanded.pop(COMPACT_REPORT_MARKER)
    for alias in aliases:
        parent = expanded
        parts = alias.split(".")
        for key in parts[:-1]:
            if not isinstance(parent.get(key), dict):
                raise JsonArtifactError("compact compile-report alias has no object parent")
            parent[key] = dict(parent[key])
            parent = parent[key]
        if parts[-1] in parent:
            raise JsonArtifactError("compact compile-report alias collides with an existing field")
        # Containers remain independent, as in legacy decoded JSON. Immutable
        # strings can safely share storage instead of being decoded three times.
        parent[parts[-1]] = list(summary)
    return expanded


def _encode_compact_vector_map(value: Any) -> Any:
    """Encode a sequential source-map vector object as an ordinal array.

    The compact on-disk representation is explicitly tagged. Non-map values,
    sparse maps, and maps with non-canonical keys are left unchanged.
    """

    if not isinstance(value, dict):
        return value
    vectors = value.get("vectors")
    if not isinstance(vectors, dict):
        return value
    try:
        ordered_keys = sorted(vectors, key=lambda key: int(key))
    except (TypeError, ValueError):
        return value
    if ordered_keys != [str(index) for index in range(len(ordered_keys))]:
        return value
    compact = dict(value)
    compact[COMPACT_VECTOR_MAP_MARKER] = COMPACT_VECTOR_MAP_ENCODING
    compact["vectors"] = [vectors[key] for key in ordered_keys]
    return compact


def _decode_compact_vector_map(value: Any) -> Any:
    """Restore the established in-memory map API from a tagged compact map."""

    if not isinstance(value, dict) or COMPACT_VECTOR_MAP_MARKER not in value:
        return value
    encoding = value.get(COMPACT_VECTOR_MAP_MARKER)
    vectors = value.get("vectors")
    if encoding != COMPACT_VECTOR_MAP_ENCODING or not isinstance(vectors, list):
        raise JsonArtifactError("unsupported or malformed compact vector-map encoding")
    expanded = dict(value)
    expanded.pop(COMPACT_VECTOR_MAP_MARKER, None)
    expanded["vectors"] = {str(index): entry for index, entry in enumerate(vectors)}
    return expanded


def read_json_bytes(path: Path) -> bytes:
    """Return the uncompressed bytes of a JSON artifact."""

    actual = resolve_json_artifact(path)
    try:
        payload = actual.read_bytes()
        return gzip.decompress(payload) if payload.startswith(b"\x1f\x8b") else payload
    except FileNotFoundError:
        raise
    except (OSError, gzip.BadGzipFile, EOFError, zlib.error) as exc:
        raise JsonArtifactError(f"cannot decompress JSON artifact {actual}: {exc}") from exc


def json_artifact_identity(path: Path, *, relative_path: str | None = None) -> dict[str, Any]:
    """Return stored and decoded byte identities suitable for a package manifest."""

    actual = resolve_json_artifact(path)
    stored_digest = hashlib.sha256()
    stored_size = 0
    with actual.open("rb") as stored_stream:
        prefix = stored_stream.read(2)
        stored_digest.update(prefix)
        stored_size += len(prefix)
        for block in iter(lambda: stored_stream.read(1024 * 1024), b""):
            stored_digest.update(block)
            stored_size += len(block)

    decoded_digest = hashlib.sha256()
    decoded_size = 0
    open_stream = gzip.open(actual, "rb") if prefix == b"\x1f\x8b" else actual.open("rb")
    with open_stream as decoded_stream:
        for block in iter(lambda: decoded_stream.read(1024 * 1024), b""):
            decoded_digest.update(block)
            decoded_size += len(block)
    return {
        "path": relative_path if relative_path is not None else actual.name,
        "size_bytes": stored_size,
        "sha256": stored_digest.hexdigest(),
        "encoding": JSON_GZIP_ENCODING if prefix == b"\x1f\x8b" else "identity",
        "uncompressed_size_bytes": decoded_size,
        "uncompressed_sha256": decoded_digest.hexdigest(),
    }
