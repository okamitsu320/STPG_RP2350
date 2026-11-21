from __future__ import annotations

import gzip
import hashlib
import json
from io import StringIO
from pathlib import Path

import pytest
import stil.json_artifacts as artifacts

from stil.json_artifacts import (
    COMPACT_REPORT_MARKER,
    COMPACT_REPORT_ENCODING,
    COMPACT_VECTOR_MAP_MARKER,
    COMPACT_VECTOR_MAP_ENCODING,
    JsonArtifactError,
    json_artifact_identity,
    read_json_artifact,
    read_json_bytes,
    resolve_json_artifact,
    write_json_artifact,
    write_json_gzip_chunks,
)


def test_direct_gzip_chunks_are_deterministic_and_atomic(tmp_path):
    path = tmp_path / "direct.json"
    path.write_text('{"old": true}')
    expected = '{"values": [1, 2], "unicode": "\\u3042"}\n'
    output = write_json_gzip_chunks(path, [expected[:8], expected[8:]])
    first = output.read_bytes()
    assert gzip.decompress(first).decode() == expected
    assert not path.exists()
    assert write_json_gzip_chunks(path, [expected]).read_bytes() == first
    def interrupted():
        yield '{"partial": '
        raise OSError("simulated writer failure")
    with pytest.raises(OSError, match="simulated"):
        write_json_gzip_chunks(path, interrupted())
    assert output.read_bytes() == first
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("compression", ["none", "gzip"])
def test_compact_compile_report_restores_all_legacy_fields_without_duplicates(tmp_path, compression):
    summary = [f"line {index}: CLK P->R" for index in range(100)]
    value = {"normalized_transition_summary": summary, "warnings": list(summary),
             "normalized_transition_count": 100,
             "source_analysis": {"normalization": {"transition_count": 100,
                                                    "transition_summary": list(summary)}}}
    path = write_json_artifact(tmp_path / "compile.json", value, compression=compression,
                               compact_compile_report=True)
    raw = json.loads(read_json_bytes(path))
    assert "warnings" not in raw
    assert "transition_summary" not in raw["source_analysis"]["normalization"]
    assert raw[COMPACT_REPORT_MARKER]["encoding"] == COMPACT_REPORT_ENCODING
    decoded = read_json_artifact(path)
    assert decoded == value
    assert decoded["warnings"][0] is decoded["normalized_transition_summary"][0]
    decoded["warnings"].append("extra")
    assert len(decoded["normalized_transition_summary"]) == 100
    assert value["warnings"] == summary  # writing must not mutate the caller


def test_nonduplicate_report_warnings_and_default_storage_are_preserved(tmp_path):
    value = {"normalized_transition_summary": ["P->R"], "warnings": ["another warning"]}
    for compact in (False, True):
        path = write_json_artifact(tmp_path / "report.json", value,
                                   compression="none", compact_compile_report=compact)
        assert json.loads(path.read_text()) == value


@pytest.mark.parametrize("aliases,extra", [
    (["warnings", "warnings"], {}), (["unknown"], {}), ([], {}),
    (["warnings"], {"warnings": ["collision"]}),
    (["source_analysis.normalization.transition_summary"], {"source_analysis": None}),
])
def test_malformed_compact_report_aliases_are_rejected(tmp_path, aliases, extra):
    value = {"normalized_transition_summary": ["P->R"],
             COMPACT_REPORT_MARKER: {"encoding": COMPACT_REPORT_ENCODING, "aliases": aliases}, **extra}
    path = write_json_artifact(tmp_path / "bad.json", value, compression="none")
    with pytest.raises(JsonArtifactError):
        read_json_artifact(path)


def test_auto_compression_is_deterministic_and_transparently_readable(tmp_path: Path) -> None:
    output = tmp_path / "large.json"
    value = {"records": [{"kind": "vector", "drive": "00000000"} for _ in range(400)]}

    first_path = write_json_artifact(output, value)
    first_bytes = first_path.read_bytes()
    second_path = write_json_artifact(output, value)

    assert first_path == second_path == tmp_path / "large.json.gz"
    assert first_path.read_bytes() == first_bytes
    assert read_json_artifact(output) == value
    identity = json_artifact_identity(output)
    assert identity["encoding"] == "gzip"
    assert identity["size_bytes"] == len(first_bytes)
    assert identity["uncompressed_sha256"] == hashlib.sha256(
        gzip.decompress(first_bytes)
    ).hexdigest()


def test_auto_keeps_small_json_plain_and_replaces_stale_counterpart(tmp_path: Path) -> None:
    output = tmp_path / "small.json"
    compressed = write_json_artifact(output, {"ok": True}, compression="gzip")
    assert compressed == tmp_path / "small.json.gz"

    plain = write_json_artifact(output, {"ok": True}, compression="auto")
    assert plain == output
    assert plain.is_file()
    assert not compressed.exists()
    assert read_json_artifact(output) == {"ok": True}


def test_resolver_does_not_accept_corrupt_gzip_json(tmp_path: Path) -> None:
    output = tmp_path / "broken.json"
    stored = write_json_artifact(output, {"valid": True}, compression="gzip")
    stored.write_bytes(b"\x1f\x8btruncated")

    assert resolve_json_artifact(output) == stored
    with pytest.raises(JsonArtifactError):
        read_json_artifact(output)


@pytest.mark.parametrize(
    "reader", [read_json_bytes, read_json_artifact], ids=["bytes", "json"]
)
@pytest.mark.parametrize("suffix", [".json", ".json.gz"], ids=["plain", "gzip"])
def test_missing_json_artifact_preserves_file_not_found(
    tmp_path: Path, reader, suffix: str
) -> None:
    missing = tmp_path / f"absent{suffix}"

    with pytest.raises(FileNotFoundError):
        reader(missing)


def test_json_artifact_manifest_identity_binds_stored_and_uncompressed_bytes(
    tmp_path: Path,
) -> None:
    output = write_json_artifact(
        tmp_path / "report.json",
        {"items": ["same"] * 300},
        compression="gzip",
    )
    descriptor = json_artifact_identity(output)
    payload = gzip.decompress(output.read_bytes())

    assert descriptor["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert descriptor["uncompressed_sha256"] == hashlib.sha256(payload).hexdigest()
    assert json.loads(payload) == {"items": ["same"] * 300}


def test_compact_vector_map_is_versioned_on_disk_and_expanded_for_readers(tmp_path: Path) -> None:
    value = {
        "mode_emitted": "rate-vector",
        "vectors": {
            "0": {"source_map": [{"global_vector_index": 0}]},
            "1": {"source_map": [{"global_vector_index": 1}]},
        },
    }
    output = write_json_artifact(
        tmp_path / "pattern.map.json",
        value,
        compression="gzip",
        compact_vector_map=True,
    )
    serialized = json.loads(gzip.decompress(output.read_bytes()))

    assert serialized[COMPACT_VECTOR_MAP_MARKER] == COMPACT_VECTOR_MAP_ENCODING
    assert serialized["vectors"] == list(value["vectors"].values())
    assert read_json_artifact(output) == value


def test_compact_vector_map_does_not_rewrite_sparse_keys(tmp_path: Path) -> None:
    value = {"vectors": {"0": {"x": 1}, "2": {"x": 2}}}
    output = write_json_artifact(
        tmp_path / "pattern.map.json",
        value,
        compression="none",
        compact_vector_map=True,
    )

    assert json.loads(output.read_text(encoding="utf-8")) == value


def test_compact_vector_map_rejects_unknown_encoding(tmp_path: Path) -> None:
    output = tmp_path / "bad-map.json"
    output.write_text(
        json.dumps({COMPACT_VECTOR_MAP_MARKER: "future-format", "vectors": []}),
        encoding="utf-8",
    )

    with pytest.raises(JsonArtifactError, match="unsupported or malformed"):
        read_json_artifact(output)


@pytest.mark.parametrize("sort_keys", [False, True])
@pytest.mark.parametrize("value", [
    {"rows": [{"z": index, "a": [None, True, False, "日本語\n\\\"", 1.25]} for index in range(600)]},
    [{"small": [], "tuple": (1, "x", {}), "empty": {}}] * 600,
    {"single_large_repeat": {"source_map": [{"line": index} for index in range(2000)]}},
    {"numbers": [float("nan"), float("inf"), -float("inf"), -0.0] * 200},
    {None: [0] * 600}, {1.5: [0] * 600}, {False: [0] * 600},
    {}, [], None, "scalar", 123,
])
def test_native_batches_match_legacy_streaming_json_bytes(tmp_path, sort_keys, value):
    legacy = StringIO()
    json.dump(value, legacy, indent=None, sort_keys=sort_keys)
    actual = write_json_artifact(tmp_path / "native.json", value, compression="none",
                                 indent=None, sort_keys=sort_keys)
    assert actual.read_bytes() == (legacy.getvalue() + "\n").encode("utf-8")


def test_compact_encoding_uses_bounded_subobjects_not_whole_map(tmp_path, monkeypatch):
    value = {"rows": [{"line": index, "bits": [0, 1]} for index in range(2000)]}
    original = artifacts.json.dumps
    calls = []

    def dumps(item, **kwargs):
        calls.append(item)
        assert item is not value
        assert item is not value["rows"]
        return original(item, **kwargs)

    monkeypatch.setattr(artifacts.json, "dumps", dumps)
    actual = write_json_artifact(tmp_path / "map.json", value, compression="gzip", indent=None)
    legacy = StringIO()
    json.dump(value, legacy, sort_keys=True)
    assert gzip.decompress(actual.read_bytes()) == (legacy.getvalue() + "\n").encode()
    assert len(calls) == 2001  # one native call per row, plus the root key


@pytest.mark.parametrize("invalid", ["cycle", "unsupported", "invalid_key", "mixed_sorted_keys"])
def test_native_batch_errors_preserve_previous_artifact_and_cleanup(tmp_path, invalid):
    output = write_json_artifact(tmp_path / "preserve.json", {"ok": True}, compression="gzip")
    previous = output.read_bytes()
    if invalid == "cycle":
        value = [0] * 600
        value.append(value)
    elif invalid == "unsupported":
        value = {"rows": [{"ok": index} for index in range(600)], "bad": {1, 2}}
    elif invalid == "invalid_key":
        value = {(1, 2): [0] * 600}
    else:
        value = {1: [0] * 600, "str": 0}
    with pytest.raises((ValueError, TypeError)) as expected:
        json.dump(value, StringIO(), sort_keys=True)
    with pytest.raises(type(expected.value)):
        write_json_artifact(output, value, compression="gzip", indent=None)
    assert output.read_bytes() == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == [output.name]


def test_native_json_path_remains_portable_without_c_encoder(tmp_path, monkeypatch):
    value = {"rows": [{"bits": [0, 1], "line": index} for index in range(600)]}
    expected = write_json_artifact(tmp_path / "a.json", value, compression="gzip", indent=None).read_bytes()
    monkeypatch.setattr(json.encoder, "c_make_encoder", None)
    actual = write_json_artifact(tmp_path / "b.json", value, compression="gzip", indent=None)
    assert actual.read_bytes() == expected


@pytest.mark.parametrize("kind", ["dict", "list", "tuple"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("sort_keys", [False, True])
def test_container_subclass_overrides_keep_legacy_streaming_semantics(tmp_path, kind, nested, sort_keys):
    class SpecialDict(dict):
        def items(self):
            return [("special", 42)]

    class SpecialList(list):
        def __iter__(self):
            return iter([42])

    class SpecialTuple(tuple):
        def __iter__(self):
            return iter([42])

    value = {"dict": lambda: SpecialDict({str(index): index for index in range(600)}),
             "list": lambda: SpecialList(range(600)), "tuple": lambda: SpecialTuple(range(600))}[kind]()
    if nested:
        value = {"before": 0, "nested": value, "after": 1}
    legacy = StringIO()
    json.dump(value, legacy, sort_keys=sort_keys)
    actual = write_json_artifact(tmp_path / "subclass.json", value, compression="gzip",
                                 indent=None, sort_keys=sort_keys)
    assert gzip.decompress(actual.read_bytes()) == (legacy.getvalue() + "\n").encode()
