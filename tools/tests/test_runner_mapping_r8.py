"""Compiler-owned runtime-event mapping, including compressed loop provenance."""
from pathlib import Path

import pytest

from stpg.client import FailLogEntry
from stil.json_artifacts import read_json_artifact
from stil.report import describe_fail_sources, describe_source_span, render_fail_report_text
from stpg_runner import build_project, describe_result, init_project
from stpg_runner.artifacts import read_json
from test_runner_fail_log_r8 import Peer, compared, execute

pytestmark = pytest.mark.stpg_host_only
FIXTURE = Path(__file__).parent / "fixtures/runner/pattern.stil"


@pytest.fixture(scope="module", params=[("preserve", None), ("expand", None), ("preserve", 4)])
def looped(request, tmp_path_factory):
    policy, cap = request.param
    root = tmp_path_factory.mktemp("r8-source-map")
    source = root / "public.stil"
    text = FIXTURE.read_text().replace("X: 100ns Compare(X);", "X: 100ns Compare(X); L: 100ns Compare(0);")
    text = text[:text.index("Pattern MAIN")] + "Pattern MAIN { W NORMAL; Loop 3 { V { ALL = 1L; } V { ALL = 0L; } } }\n"
    source.write_text(text)
    project = init_project(source, root / "project", wait_preset="none", completion="release",
                           loop_policy=policy, max_segment_vectors=cap, compile_mode="rate-vector")
    return build_project(project)


def test_repeat_loop_and_segment_spans_have_global_occurrences(looped, tmp_path):
    manifest = read_json(looped.bundle_dir / "runner.bundle.json")
    start = 0
    for item in manifest["patterns"]:
        pattern = looped.bundle_dir / item["path"]
        data = read_json_artifact(pattern.parent / "pattern.map.json")
        span = describe_source_span(data, pattern_bytes=pattern.read_bytes())
        assert span["first"]["global_vector_index"] == start
        logical_count = item["expected"]["compares"]
        assert span["last"]["global_vector_index"] == start + logical_count - 1
        entries = [FailLogEntry(1, 2, 0, 2), FailLogEntry(item["expected"]["events"] - 1, 2, 0, 2)]
        mapped = describe_fail_sources(entries, data, pattern_bytes=pattern.read_bytes())
        assert mapped[0]["source"]["global_vector_index"] == start
        assert mapped[-1]["source"]["global_vector_index"] == start + logical_count - 1
        assert mapped[0]["signals"] == [{"name": "D1", "bit": 1, "expected": 0, "actual": 1}]
        text = render_fail_report_text(entries, data, pattern_bytes=pattern.read_bytes())
        assert f"vector={start}" in text and f"vector={start + logical_count - 1}" in text
        start += logical_count
    assert start == 6
    report = execute(looped, tmp_path / "run", Peer(fails=1))
    assert report["outcome"] == "COMPLETED", report["errors"]
    described = describe_result(looped.bundle_dir, manifest_sha256=looped.manifest_sha256, result=report)
    assert len(described["segments"]) == len(manifest["patterns"])
    commands = [cmd.cmd for cmd in Peer().commands]  # no real I/O is possible
    assert commands == []


def test_folded_single_vector_loop_span_and_failure_occurrence(compared):
    manifest = read_json(compared.bundle_dir / "runner.bundle.json")
    pattern = compared.bundle_dir / manifest["patterns"][0]["path"]
    data = read_json_artifact(pattern.parent / "pattern.map.json")
    span = describe_source_span(data, pattern_bytes=pattern.read_bytes())
    assert span["first"]["global_vector_index"] == 0
    assert span["last"]["global_vector_index"] == 499
    result = describe_fail_sources([FailLogEntry(999, 2, 0, 2)], data, pattern_bytes=pattern.read_bytes())
    assert result[0]["source"]["global_vector_index"] == 499
    assert result[0]["unmapped_fail_bits"] == 0


@pytest.mark.parametrize("mode", ["raw-event", "rate-vector"])
def test_raw_sources_and_wide_rate_wait_sources(mode, tmp_path):
    source = tmp_path / "public.stil"
    text = FIXTURE.read_text()
    if mode == "rate-vector":
        text = text.replace("400ns", "400us")
    source.write_text(text)
    project = init_project(source, tmp_path / "project", wait_preset="none", completion="release", compile_mode=mode,
                           loop_policy="expand" if mode == "raw-event" else "preserve")
    built = build_project(project)
    manifest = read_json(built.bundle_dir / "runner.bundle.json")
    item = manifest["patterns"][0]
    pattern = built.bundle_dir / item["path"]
    data = read_json_artifact(pattern.parent / "pattern.map.json")
    span = describe_source_span(data, pattern_bytes=pattern.read_bytes())
    assert span["first"]["global_vector_index"] == 0 and span["last"]["global_vector_index"] == 3
    mapped = describe_fail_sources([FailLogEntry(0, 1, 0, 1)], data, pattern_bytes=pattern.read_bytes())
    assert mapped[0]["source"]["stil_line"] == 19
    assert mapped[0]["source"]["global_vector_index"] == 0
    with pytest.raises(ValueError):
        describe_fail_sources([FailLogEntry(0xFFFFFFFF, 1, 0, 1)], data, pattern_bytes=pattern.read_bytes())
