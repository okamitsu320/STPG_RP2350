"""Capacity-first planning with two encoder capacity errors; public data.

SDK 8f698155 compared ``str(exc)`` of a failed growth probe with a fixed set of capacity messages and rejected the
whole build for any other error. Two capacity errors of the rate-vector backend missed that set:

- In compile mode ``auto`` with loop policy ``preserve`` (the New-project defaults), the pipeline re-raises the
  encoder's "rate vector exceeds PSRAM pattern region" as ``ERR_LOOP_BACKEND_UNSUPPORTED`` ("preserved loop could
  not be compiled by the rate-vector backend; refusing raw-event fallback"), even for a source without Loops.
  A 300,000-vector public counter (6 MB of rate vectors) built with the SDK defaults rejected at probe 0..299999.
- A pattern with a period above 65535 ticks is encoded in the wide format, whose size error reads "wide rate vector
  exceeds PSRAM pattern region", also in mode ``rate-vector``.

SDK 8d2c751f recognizes both (the known wrapper only, with the encoder's exact message and code). The PSRAM byte
limit is simulated with a smaller threshold on the real compiler's output, raising the encoder's own message for
the format.
"""

from __future__ import annotations

import pytest

from stil import pipeline
from stil import segmented_package as planner
from stil.runtime_capacity import DEFAULT_RUNTIME_PROFILE
from stpg.constants import ERR_BAD_SIZE, STPG_PATTERN_VERSION_WIDE_WAIT
from stpg.pattern import PatternValidationError
from stpgview.physical.patterns import counter, counter_pins

pytestmark = pytest.mark.stpg_host_only
LIMIT = 6_000  # bytes: about 300 vectors of this counter; stands in for the 5 MB PSRAM pattern region


def write_source(root, period="400ns"):
    (root / "counter.stil").write_text(counter(1200).replace("Period '400ns'", f"Period '{period}'"), encoding="utf-8")
    (root / "pins.json").write_text(counter_pins(), encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def limited_region(monkeypatch):
    original = pipeline._compile_prepared_rate_vector

    def limited(ir, **kwargs):  # the encoder's own size check and message, with a smaller region
        result = original(ir, **kwargs)
        if len(result.pattern_bytes) > LIMIT:
            wide = int.from_bytes(result.pattern_bytes[4:6], "little") == STPG_PATTERN_VERSION_WIDE_WAIT
            raise PatternValidationError(f"{'wide ' if wide else ''}rate vector exceeds PSRAM pattern region",
                                         ERR_BAD_SIZE)
        return result

    monkeypatch.setattr(pipeline, "_compile_prepared_rate_vector", limited)


def plan(source, mode):
    return planner.plan_capacity_aware_segment_ranges(
        input_path=source / "counter.stil", total_vectors=1200, max_segment_vector_count=None,
        initial_probe_vector_count=100, mode=mode, tick_hz=200_000_000, pin_map_path=source / "pins.json",
        pattern_burst=None, pattern=None, strict=True, allow_approx=False, prefer_raw=False,
        runtime_profile=DEFAULT_RUNTIME_PROFILE, max_rates=64, loop_policy="preserve")


def test_rate_vector_mode_fills_each_segment_to_the_byte_limit(tmp_path):
    ranges, report = plan(write_source(tmp_path), "rate-vector")
    assert len(ranges) >= 3 and ranges[0][0] == 0 and ranges[-1][1] == 1199
    assert all(d["metrics"]["pattern_bytes"] <= LIMIT for d in report["decisions"])
    assert report["planning_strategy"] == "capacity-first"


def test_auto_mode_with_preserved_loops_plans_the_same_segments(tmp_path):
    source = write_source(tmp_path)
    expected, _ = plan(source, "rate-vector")
    ranges, _ = plan(source, "auto")
    assert ranges == expected


def test_a_wide_format_pattern_fills_each_segment_to_the_byte_limit(tmp_path):
    ranges, report = plan(write_source(tmp_path, period="400us"), "rate-vector")  # 80,000 ticks: wide format
    assert len(ranges) >= 2 and ranges[-1][1] == 1199
    assert all(d["metrics"]["pattern_bytes"] <= LIMIT for d in report["decisions"])
