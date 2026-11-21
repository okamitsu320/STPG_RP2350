"""Capacity-first segments and the waits the build encodes afterwards; public data.

With a wait preset, the SDK first compiles every segment, then plans one wait schedule over all of them (a wait
before every eligible chunk) and encodes those waits into each segment's binary. Encoding a wait clones a rate, a
wait above 65535 ticks (the 4 ms preset is 800,000 ticks at 200 MHz) promotes the binary to the wide format, and a
wait inside a repeat or a loop splits records. Every one of these adds bytes.

SDK 8d2c751f filled each segment up to the last vector that fit before those waits were added, so the first segment
that received a wait overflowed the PSRAM pattern region after the whole segmented compile:

    segment 0 wait materialization failed: cannot derive wait package: wide rate vector exceeds PSRAM pattern region

Since SDK 66c87369 the capacity probe measures the binary after its every-boundary waits, for a blank and an explicit
Max vectors / segment alike. The tests read the shipped binaries themselves, not the SDK's prediction.

The region is scaled down here (both encoders read ``stpg.pattern.STPG_PSRAM_PATTERN_BYTES``). The short declared
service time keeps the wait plan acceptable for this dense counter, so that the build reaches the wait encoding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import stpg.pattern as stpg_pattern
from stpg.constants import STPG_PATTERN_VERSION_WIDE_WAIT, STPG_VECTOR_RECORD_SIZE
from stpg_runner.build import build_project, init_project
from stpgview.physical.patterns import counter, counter_pins

pytestmark = pytest.mark.stpg_host_only
LIMIT = 50_000  # bytes: about 2,500 vectors of this counter (more than one 2,048-event chunk per segment)
VECTORS = 6_000


@pytest.fixture(autouse=True)
def small_region(monkeypatch):
    monkeypatch.setattr(stpg_pattern, "STPG_PSRAM_PATTERN_BYTES", LIMIT)


def project(root: Path, *, preset: str, max_vectors: int | None = None) -> Path:
    (root / "counter.stil").write_text(counter(VECTORS), encoding="utf-8")
    (root / "pins.json").write_text(counter_pins(), encoding="utf-8")
    waits = dict(service_us=50.0, service_source="declared", service_scope="non-loop", max_total_wait_us=1e11)
    return init_project(root / "counter.stil", root / "project", pin_map=root / "pins.json", wait_preset=preset,
                        completion="hold", segmentation_mode="auto", max_segment_vectors=max_vectors,
                        **(waits if preset != "none" else {}))


def segments(result) -> list[bytes]:
    return [path.read_bytes() for path in sorted(result.bundle_dir.glob("segmented/nominal/segment-*/package/pattern.bin"))]


def wide(data: bytes) -> bool:
    return int.from_bytes(data[4:6], "little") == STPG_PATTERN_VERSION_WIDE_WAIT


def test_capacity_first_leaves_room_for_the_waits_it_encodes(tmp_path):
    binaries = segments(build_project(project(tmp_path, preset="chunk-4ms")))
    assert len(binaries) == 3 and all(len(data) <= LIMIT for data in binaries)
    assert all(wide(data) for data in binaries[:-1])  # the waits were encoded, not dropped
    # Still capacity-first: a full segment has less room left than one more vector record.
    assert all(LIMIT - len(data) < STPG_VECTOR_RECORD_SIZE for data in binaries[:-1])


def test_without_waits_capacity_first_fills_each_segment(tmp_path):
    binaries = segments(build_project(project(tmp_path, preset="none")))
    assert len(binaries) == 3 and all(LIMIT - 64 < len(data) <= LIMIT for data in binaries[:-1])
    assert not any(wide(data) for data in binaries)


def test_an_explicit_cap_below_capacity_keeps_room_for_the_waits(tmp_path):
    # An explicit cap stays an upper bound: segments of at most 2,300 vectors (about 46,000 bytes, more than one
    # chunk) carry their waits.
    binaries = segments(build_project(project(tmp_path, preset="chunk-4ms", max_vectors=2_300)))
    assert len(binaries) == 3 and all(len(data) <= LIMIT for data in binaries)
    assert any(wide(data) for data in binaries)
