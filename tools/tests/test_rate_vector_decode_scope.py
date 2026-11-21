"""Actual decoder work, owned containers, byte binding and bounded fallbacks."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from stpg import pattern
from stpg_runner import build_project, verify_bundle
from stpg_runner.artifacts import read_json
from test_runtime_wait_model import _pattern
from test_stpg_runner_auto_segmentation import create, no_contact

pytestmark = pytest.mark.stpg_host_only


def spy(monkeypatch):
    original = pattern._decode_rate_vector_full
    calls = []
    def decode(raw):
        calls.append(bytes(raw))
        return original(raw)
    monkeypatch.setattr(pattern, "_decode_rate_vector_full", decode)
    return calls


def test_one_actual_decode_owned_lists_and_nested_scope(monkeypatch):
    raw = _pattern()
    expected = pattern.unpack_rate_vector_full(raw)
    calls = spy(monkeypatch)
    with pattern.rate_vector_decode_scope():
        first = pattern.unpack_rate_vector_full(raw)
        for items in first[1:]:
            items.clear()
        with pattern.rate_vector_decode_scope():
            assert pattern.unpack_rate_vector_full(bytearray(raw)) == expected
        assert pattern.unpack_rate_vector_full(raw) == expected
    assert calls == [raw]
    assert pattern.unpack_rate_vector_full(raw) == expected
    assert calls == [raw, raw]  # no global payload cache


@pytest.mark.parametrize("budget", ["_DECODE_BYTES_LIMIT", "_DECODE_RECORD_LIMIT", "_DECODE_ENTRY_LIMIT"])
def test_bounded_memo_miss_falls_back_without_skipping_validation(monkeypatch, budget):
    a, b = _pattern(), _pattern(period_ticks=40)
    expected = pattern.unpack_rate_vector_full(a)
    monkeypatch.setattr(pattern, budget, 1)
    calls = spy(monkeypatch)
    with pattern.rate_vector_decode_scope():
        assert pattern.unpack_rate_vector_full(a) == expected
        pattern.unpack_rate_vector_full(b)
        assert pattern.unpack_rate_vector_full(a) == expected
    assert calls == [a, b, a]


def test_changed_bytes_crc_and_invalid_inputs_are_not_cached_as_valid(monkeypatch):
    raw = _pattern()
    calls = spy(monkeypatch)
    with pattern.rate_vector_decode_scope():
        pattern.validate_pattern(raw)
        changed = bytearray(raw); changed[-1] ^= 1
        with pytest.raises(ValueError):
            pattern.validate_pattern(changed)
        for _ in range(2):
            with pytest.raises(Exception):
                pattern.unpack_rate_vector_full(b"bad")
        pattern.validate_pattern(raw)
    assert calls.count(raw) == 1 and calls.count(b"bad") == 2


def test_decode_scopes_are_thread_isolated(monkeypatch):
    raw = _pattern()
    calls = spy(monkeypatch)
    def work(_):
        with pattern.rate_vector_decode_scope():
            first = pattern.unpack_rate_vector_full(raw)
            assert pattern.unpack_rate_vector_full(raw) == first
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(work, range(2)))
    assert calls == [raw, raw]


def test_full_bundle_verification_decodes_once_per_exact_payload(tmp_path, monkeypatch):
    result = build_project(create(tmp_path, count=64, compile_mode="rate-vector", max_segment_vectors=25))
    calls = spy(monkeypatch)
    checked = verify_bundle(result.bundle_dir)  # explicit verify is always full
    assert checked["status"] == "PASS" and checked["check_mode"] == "semantic"
    assert calls and len(calls) == len(set(calls))
    manifest = read_json(result.bundle_dir / "runner.bundle.json")
    assert len(calls) == len({(result.bundle_dir / item["path"]).read_bytes() for item in manifest["patterns"]})


def test_package_reuses_its_verified_map_parse_but_still_revalidates_wait(tmp_path, monkeypatch):
    from stil import pattern_package as package, wait_schedule as wait
    result = build_project(create(tmp_path, compile_mode="rate-vector"))
    data = read_json(result.bundle_dir / "runner.bundle.json")
    root = (result.bundle_dir / data["patterns"][0]["path"]).parent
    reads, validations = [], []
    original_read, original_validate = package._read_json, package.validate_wait_schedule
    def read(path):
        if "pattern.map.json" in path.name:
            reads.append(path)
        return original_read(path)
    def validate(*args, **kwargs):
        validations.append(True)
        return original_validate(*args, **kwargs)
    monkeypatch.setattr(package, "_read_json", read)
    monkeypatch.setattr(package, "validate_wait_schedule", validate)
    original_wait_read = wait._load_json_object
    def no_extra_map(path):
        assert "pattern.map.json" not in path.name, "map decoded a second time"
        return original_wait_read(path)
    monkeypatch.setattr(wait, "_load_json_object", no_extra_map)
    assert package.verify_pattern_package(root)["status"] == "PASS"
    assert len(reads) == len(validations) == 1
    wrong = wait.validate_wait_schedule(root / "pattern.bin", root / "pattern.wait.json",
                                        map_path=root / "pattern.map.json", _verified_map=(("wrong",), {}))
    assert wrong["status"] == "REJECT" and "preloaded source map differs" in str(wrong["errors"])
