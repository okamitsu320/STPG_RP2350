"""More than 128 multi-record Loops split instead of rejecting; GUI builds of public data.

A rate-vector package holds at most ``STPG_MAX_LOOP_BLOCKS`` (128) Loop blocks. A preserved Loop whose body packs to
two or more records takes one block; a one-record body folds into a repeated record and takes none. Up to SDK
5207eaf9 source analysis rejected a ``preserve`` pattern with more than 128 such Loops (``ERR_TOO_MANY_LOOP_BLOCKS``
was not a segmentable cause), even with a cap that gave small segments. SDK 5981dbb4 splits it.

Every build goes through the GUI's own service (``RunnerService.start_build``) and is checked on the real output,
not on the job status: each segment's decoded ``pattern.bin`` keeps between 1 and 128 Loop blocks, the segments
cover the source's vectors once and in order, and an explicit cap still bounds each segment.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from stil.json_artifacts import read_json_artifact, resolve_json_artifact
from stpg.constants import STPG_MAX_LOOP_BLOCKS
from stpg.pattern import rate_vector_structure_metrics
from stpg_runner import verify_bundle
from stpgview.physical.patterns import counter
from stpgview.runner.service import RunnerService
from stpgview.session import Session

from stpg_hardware_guard import forbid_hardware

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture
def svc(tmp_path):
    return RunnerService(config_dir=tmp_path / "cfg", device_run=False, demo=False, results_dir=tmp_path / "runs",
                         session=Session(tmp_path / "work"))


def loops(blocks: int, *, body: int = 2, period: str = "400ns") -> tuple[str, int]:
    """The counter's header; each block is one vector, then ``Loop 3`` of ``body`` distinct vectors."""

    head = counter(1).split("Pattern MAIN {")[0].replace("Period '400ns'", f"Period '{period}'")
    text = []
    for i in range(blocks):
        inner = "".join(f"    V {{ ALL = {(i + k * 37) % 128:07b}{k % 2}; }}\n" for k in range(body))
        text.append(f"  V {{ ALL = {i % 128:07b}0; }}\n  Loop 3 {{\n{inner}  }}\n")
    return head + "Pattern MAIN {\n  W NORMAL;\n" + "".join(text) + "}\n", blocks * (1 + 3 * body)


def build(svc, root: Path, text: str, **options) -> Path:
    (root / "source.stil").write_text(text, encoding="utf-8")
    request = {"input_form": "stil", "stil": str(root / "source.stil"), "directory": str(root / "p"),
               "wait_preset": "none", "completion": "hold", "storage": "psram", "compile_mode": "auto",
               "loop_policy": "preserve", "segmentation": "auto"}
    request.update(options)
    info = svc.init_project(request)
    job = svc.start_build(info["project_path"])
    end = time.monotonic() + 300
    while job.status == "running":
        assert time.monotonic() < end, "build did not finish"
        time.sleep(0.05)
    public = job.public(0)
    assert public["status"] == "done", public["error"]
    bundle = Path(public["result"]["bundle_dir"])
    assert verify_bundle(bundle, manifest_sha256=public["result"]["manifest_sha256"])["status"] == "PASS"
    return bundle


def segments(bundle: Path) -> tuple[list[dict], list[list[int]] | None]:
    """Decoded structure of each package, in order, and the planned ranges (None for one package)."""

    paths = sorted(bundle.glob("segmented/nominal/segment-*/package/pattern.bin")) or [bundle / "base-package/pattern.bin"]
    metrics = [rate_vector_structure_metrics(path.read_bytes()) for path in paths]
    manifest = bundle / "segmented" / "nominal" / "segmented.manifest.json"
    ranges = None
    if paths[0].parent.parent.name.startswith("segment-"):
        ranges = read_json_artifact(resolve_json_artifact(manifest))["capacity_plan"]["ranges"]
    return metrics, ranges


def assert_split(metrics, ranges, vectors, cap=None):
    assert ranges is not None and len(metrics) == len(ranges) >= 2
    assert ranges[0][0] == 0 and ranges[-1][1] == vectors - 1
    assert all(b[0] == a[1] + 1 for a, b in zip(ranges, ranges[1:]))  # contiguous, each vector once
    assert all(1 <= m["loop_block_count"] <= STPG_MAX_LOOP_BLOCKS for m in metrics)  # Loops kept, never expanded
    assert sum(m["expanded_logical_vector_count"] for m in metrics) == vectors
    for (start, end), m in zip(ranges, metrics):
        assert m["expanded_logical_vector_count"] == end - start + 1
        assert cap is None or end - start + 1 <= cap


def test_128_multi_record_loops_still_fit_one_package(svc, tmp_path):
    text, vectors = loops(128)
    metrics, ranges = segments(build(svc, tmp_path, text))
    assert ranges is None and metrics[0]["loop_block_count"] == 128
    assert metrics[0]["expanded_logical_vector_count"] == vectors


@pytest.mark.parametrize(("blocks", "cap"), [(129, None), (200, None), (200, 400), (200, 100_000)])
def test_more_than_128_multi_record_loops_split_in_preserve(svc, tmp_path, blocks, cap):
    text, vectors = loops(blocks)
    options = {} if cap is None else {"max_segment_vectors": str(cap)}
    metrics, ranges = segments(build(svc, tmp_path, text, **options))
    assert_split(metrics, ranges, vectors, cap)
    if cap == 400:
        assert len(ranges) >= vectors // 400  # the cap, not the block limit, sets the count here


def test_wide_periods_with_encoded_waits_split_too(svc, tmp_path):
    text, vectors = loops(200, period="400us")
    bundle = build(svc, tmp_path, text, wait_preset="chunk-4ms", service_us="50",
                   service_source="declared, not measured", service_scope="loop-generic",
                   max_total_wait_us="100000000000")
    metrics, ranges = segments(bundle)
    assert_split(metrics, ranges, vectors)


def test_one_record_loop_bodies_use_repeats_and_stay_one_package(svc, tmp_path):
    text, vectors = loops(200, body=1)
    metrics, ranges = segments(build(svc, tmp_path, text))
    assert ranges is None and metrics[0]["loop_block_count"] == 0
    assert metrics[0]["expanded_logical_vector_count"] == vectors


def test_expand_still_builds_200_multi_record_loops_as_one_package(svc, tmp_path):
    text, vectors = loops(200)
    bundle = build(svc, tmp_path, text, loop_policy="expand")
    assert not list(bundle.glob("segmented/nominal/segment-*"))
    report = read_json_artifact(resolve_json_artifact(bundle / "base-package" / "pattern.compile_report.json"))
    assert report["loop_preserved"] is False and report["logical_vector_count"] == vectors


def test_one_package_only_names_the_automatic_copy_for_the_loop_block_limit(svc, tmp_path):
    # The GUI asks the SDK's capacity classifier, so the Loop-block limit offers the same automatic copy as the
    # byte limit; the advice names the limits that end a segment, not a fixed vector count.
    text, _vectors = loops(129)
    (tmp_path / "source.stil").write_text(text, encoding="utf-8")
    info = svc.init_project({"input_form": "stil", "stil": str(tmp_path / "source.stil"), "directory": str(tmp_path / "p"),
                             "wait_preset": "none", "completion": "hold", "storage": "psram", "compile_mode": "auto",
                             "loop_policy": "preserve", "segmentation": "single"})
    job = svc.start_build(info["project_path"])
    end = time.monotonic() + 120
    while job.status == "running":
        assert time.monotonic() < end, "build did not finish"
        time.sleep(0.05)
    failed = job.public(0)
    assert failed["status"] == "failed"
    assert "STPG_MAX_LOOP_BLOCKS=128" in failed["error"]
    assert "does not fit in one package" in failed["error"] and "Segmentation = automatic" in failed["error"]
    assert "Loop blocks" in failed["error"] and "vectors per segment" not in failed["error"]
