"""Capacity-first planning must not compile the pattern many times over; public data.

To place each segment boundary, the SDK compiles candidate ranges ("probes"). Up to SDK 9bf78b6c two costs added up:

- every probe resolved the whole pattern again (``resolve_document`` lowers the control flow of the entire pattern
  and slices the range at the end), so even a one-vector probe cost as much as resolving the whole source;
- the search doubled the range from 100,000 vectors, then bisected down to the last vector that fits: about
  log2(vectors) probes per segment, each compiling nearly a full segment.

SDK 5207eaf9 keeps one complete preserved resolution per conversion and slices each range from it, and for a flat
preserved rate-vector source (no Loop blocks, like this counter) interpolates from the sizes of the probes that fit.
Sources with Loop blocks keep the bisection; only the kept resolution helps them.

The tests keep the result and bound the work, without prescribing how: segments filled as before (less room left
than one more vector record), at most five probes per segment, and at most one whole-pattern lowering per segment
plus three. 9bf78b6c needed 61 probes and 67 lowerings here. A public source with Loop blocks (the shape of
large generated ATE patterns) still bisects, so it gets no probe bound, but it must be resolved once too.

The region is scaled down (both encoders read ``stpg.pattern.STPG_PSRAM_PATTERN_BYTES``) so that five segments
build in seconds; without a wait preset the planner is the same.
"""

from __future__ import annotations

import pytest

import stil.resolve as resolve_module
import stpg.pattern as stpg_pattern
from stil.json_artifacts import read_json_artifact, resolve_json_artifact
from stpg.constants import STPG_VECTOR_RECORD_SIZE
from stpg_runner.build import build_project, init_project
from stpgview.physical.patterns import counter, counter_pins

pytestmark = pytest.mark.stpg_host_only
LIMIT = 100_000  # bytes: about 5,000 vectors of this counter per segment
VECTORS = 20_000
SEGMENTS = 5
LOOPED_BLOCKS = 5_000  # 25,000 vectors, 4 segments


def looped(blocks: int) -> str:
    """The counter's header; each block is two vectors, then ``Loop 3`` of one vector (five vectors per block).

    A one-vector Loop body packs as a repeated record, so no segment meets the 128 Loop blocks of a package,
    but the source keeps its Loop structure and the planner bisects.
    """

    head = counter(1).split("Pattern MAIN {")[0]
    body = []
    for i in range(blocks):
        a, b = format(i % 128, "07b"), format(i * 7 % 128, "07b")
        body.append(f"  V {{ ALL = {a}0; }}\n  V {{ ALL = {b}1; }}\n  Loop 3 {{\n    V {{ ALL = {a}1; }}\n  }}\n")
    return head + "Pattern MAIN {\n  W NORMAL;\n" + "".join(body) + "}\n"


def _build(root, text):
    """One segmented build; counts how often the whole pattern's control flow is lowered."""

    (root / "source.stil").write_text(text, encoding="utf-8")
    (root / "pins.json").write_text(counter_pins(), encoding="utf-8")
    lowerings = []
    plain = resolve_module.lower_pattern_control_flow
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(stpg_pattern, "STPG_PSRAM_PATTERN_BYTES", LIMIT)
        patch.setattr(resolve_module, "lower_pattern_control_flow",
                      lambda *args, **kwargs: lowerings.append(1) or plain(*args, **kwargs))
        project = init_project(root / "source.stil", root / "project", pin_map=root / "pins.json",
                               wait_preset="none", completion="hold", segmentation_mode="auto")
        result = build_project(project)
    binaries = [path.read_bytes() for path in
                sorted(result.bundle_dir.glob("segmented/nominal/segment-*/package/pattern.bin"))]
    plan = read_json_artifact(resolve_json_artifact(
        result.bundle_dir / "segmented" / "nominal" / "segmented.manifest.json"))["capacity_plan"]
    return binaries, plan, len(lowerings)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("planning"), counter(VECTORS))


@pytest.fixture(scope="module")
def built_looped(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("planning-looped"), looped(LOOPED_BLOCKS))

def test_capacity_first_still_fills_every_segment(built):
    binaries, _plan, lowerings = built
    assert lowerings >= 1  # the counter sees the SDK's resolution (a renamed function would read 0)
    assert len(binaries) == SEGMENTS
    assert all(LIMIT - STPG_VECTOR_RECORD_SIZE < len(data) <= LIMIT for data in binaries[:-1])


def test_planning_needs_few_probes_per_segment(built):
    _binaries, plan, _lowerings = built
    assert plan["probe_count"] <= 5 * SEGMENTS, plan["probe_count"]


def test_the_whole_pattern_is_not_resolved_again_for_every_probe(built):
    _binaries, _plan, lowerings = built
    assert lowerings <= SEGMENTS + 3, lowerings


def test_a_source_with_loop_blocks_is_resolved_once_too(built_looped):
    # Loop blocks keep the bisection (more probes); the kept resolution must still serve every probe.
    binaries, plan, lowerings = built_looped
    assert len(binaries) >= 3 and all(len(data) <= LIMIT for data in binaries)
    assert plan["probe_count"] > 5 * len(binaries)  # still the bisection, so the bound below is meaningful
    assert lowerings <= len(binaries) + 3, lowerings
