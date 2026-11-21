"""Lossless source storage, not pattern encoding or physical device access."""
from __future__ import annotations

from contextlib import contextmanager
import gzip
import hashlib
import io
from pathlib import Path
import shutil
import tempfile
from typing import BinaryIO, Iterator
import zlib

SOURCE_GZIP_MIN_BYTES = 32 * 1024
SOURCE_GZIP_LEVEL = 6


class SourceArtifactError(ValueError):
    pass


def source_encoding(path: Path) -> str:
    with Path(path).open("rb") as stream:
        magic = stream.read(2)
    if magic == b"\x1f\x8b":
        return "gzip"
    if Path(path).suffix == ".gz":
        raise SourceArtifactError(f"source is named gzip but has no gzip header: {Path(path).name}")
    return "identity"


@contextmanager
def open_source_binary(path: Path) -> Iterator[BinaryIO]:
    """Read exact decoded source bytes; corrupt gzip never falls back to text."""
    path = Path(path)
    try:
        encoding = source_encoding(path)
        with (gzip.open(path, "rb") if encoding == "gzip" else path.open("rb")) as stream:
            yield stream
    except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
        raise SourceArtifactError(f"cannot decode source {path.name}: {exc}") from exc


def read_source_text(path: Path, *, errors: str = "strict") -> str:
    # Match Path.read_text's UTF-8 / universal-newline behavior. Storage and
    # decoded identities below retain exact bytes, including CRLF and BOM.
    with open_source_binary(path) as binary:
        with io.TextIOWrapper(binary, encoding="utf-8", errors=errors) as text:
            return text.read()


def _stream_identity(stream: BinaryIO) -> dict:
    size = 0
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        size += len(block)
        digest.update(block)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def decoded_source_identity(path: Path) -> dict:
    with open_source_binary(path) as stream:
        return _stream_identity(stream)


def _stored_identity(path: Path) -> dict:
    with path.open("rb") as stream:
        return _stream_identity(stream)


def copy_source_artifact(source: Path, destination: Path, *,
                         min_compress_bytes: int = SOURCE_GZIP_MIN_BYTES) -> tuple[Path, dict]:
    """Publish a new byte-preserving copy, compressed only if beneficial.

    The caller owns a new exclusive project/build directory. Existing members
    are never overwritten. Only this call's scratch file is removed on failure;
    an incomplete published prefix, if any, belongs to the caller's reservation.
    """
    source, destination = Path(source), Path(destination)
    before = _stored_identity(source)
    input_encoding = source_encoding(source)
    # Validate gzip through EOF/CRC before copying, rather than letting an
    # invalid compressed input become a successful project or cached snapshot.
    decoded = decoded_source_identity(source)
    plain = Path(str(destination).removesuffix(".gz"))
    compressed = Path(str(plain) + ".gz")
    if plain.exists() or compressed.exists():
        raise FileExistsError(f"source destination already exists: {plain}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        selected = source
        output = compressed if input_encoding == "gzip" else plain
        if input_encoding == "identity" and before["size_bytes"] >= min_compress_bytes:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".stpg-source-", delete=False) as scratch:
                temporary = Path(scratch.name)
                with gzip.GzipFile(fileobj=scratch, mode="wb", filename="", mtime=0,
                                   compresslevel=SOURCE_GZIP_LEVEL) as packed:
                    with source.open("rb") as original:
                        shutil.copyfileobj(original, packed, length=1024 * 1024)
            if temporary.stat().st_size < before["size_bytes"]:
                selected, output = temporary, compressed
        with selected.open("rb") as original, output.open("xb") as stored:
            shutil.copyfileobj(original, stored, length=1024 * 1024)
        if _stored_identity(source) != before or decoded_source_identity(output) != decoded:
            raise SourceArtifactError("source changed while creating its compressed copy")
        if input_encoding == "gzip" and _stored_identity(output) != before:
            raise SourceArtifactError("compressed source copy differs from its stored input bytes")
        return output, {"input_encoding": input_encoding, "encoding": source_encoding(output), "decoded": decoded}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_source_text_artifact(destination: Path, text: str) -> Path:
    """Store generated text with the same policy; no persistent raw twin."""
    with tempfile.TemporaryDirectory(prefix="stpg-generated-source-") as temporary:
        source = Path(temporary) / "source.stil"
        source.write_text(text, encoding="utf-8", newline="\n")
        stored, _ = copy_source_artifact(source, destination)
        return stored
