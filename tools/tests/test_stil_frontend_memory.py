"""Bounded front-end working sets; public synthetic sources only."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace as NS
import random

import pytest

from stil import compareprep, family_analyze, stimulus
from stil.preprocess import _iter_source_lines, _rewrite_pattern_vectors, _shift_pattern_compare_vectors


def vector(index: int, drive_times: tuple, sample_times: tuple):
    return NS(vector_index=index, top_level_index=index, stil_line=index + 1, label=None,
              pattern_name="PUBLIC", waveform_table_name="W",
              signal_events={"A": tuple(NS(action_name="Drive", time_ticks=time) for time in drive_times),
                             "B": tuple(NS(action_name="Compare", time_ticks=time) for time in sample_times)},
              drive_chars={"A": "0"} if drive_times else {},
              compare_chars={"B": "H"} if sample_times else {})


def test_family_summary_matches_eager_reference_and_keeps_bounded_examples(monkeypatch):
    shapes = [((0,), (16,)), ((16,), (0,)), ((0,), ()), ((), (0,)),
              ((0,), (0,)), ((), ()), ((0, 32), (16, 48))]
    vectors = tuple(vector(index, *shapes[index % len(shapes)]) for index in range(700))
    classify = family_analyze.classify_micro_op_family
    eager = [classify(item) for item in vectors]
    histogram = Counter(item["family"] for item in eager)
    unsupported = [item for item in eager if not item["supported"]]
    examples = {name: [item for item in eager if item["family"] == name][:8]
                for name in family_analyze.SUPPORTED_FAMILIES}

    class Tracked(dict):
        live = peak = calls = 0

        def __init__(self, *args):
            super().__init__(*args)
            Tracked.live += 1
            Tracked.calls += 1
            Tracked.peak = max(Tracked.peak, Tracked.live)

        def __del__(self):
            Tracked.live -= 1

    monkeypatch.setattr(family_analyze, "classify_micro_op_family", lambda item: Tracked(classify(item)))
    report = family_analyze.analyze_micro_op_families(NS(vectors=iter(vectors)))
    assert report == {
        "summary": {"total_vectors": len(vectors), "supported_vectors": len(vectors) - len(unsupported),
                    "unsupported_vectors": len(unsupported),
                    "micro_op_candidate_ratio": (len(vectors) - len(unsupported)) / len(vectors),
                    "family_histogram": dict(sorted(histogram.items())),
                    "supported_families": list(family_analyze.SUPPORTED_FAMILIES),
                    "optimization_scope": "canonical-micro-op-families-only", "generic_rate_vector_fallback": True},
        "unsupported_examples": unsupported[:16], "family_examples": examples,
    }
    assert Tracked.calls == len(vectors)
    assert Tracked.peak <= 5 * 8 + 16 + 2


def test_empty_family_summary():
    report = family_analyze.analyze_micro_op_families(NS(vectors=()))
    assert report["summary"]["total_vectors"] == 0
    assert report["summary"]["micro_op_candidate_ratio"] == 0.0
    assert not report["unsupported_examples"]


@pytest.mark.parametrize("shift", [1, 3, 200])
def test_streamed_shift_matches_independent_per_target_reference(shift):
    rng = random.Random(20261008)
    groups = {"G0": ("A", "B", "C"), "G1": ("D", "E", "F")}
    selected = {"B", "C", "D"}
    rows = [("G0" if index % 3 else "G1", "".join(rng.choice("01HLXTZhl") for _ in range(3)))
            for index in range(120)]
    history = {target: [] for target in groups}
    expected = []
    for target, data in rows:
        previous = history[target][-shift] if len(history[target]) >= shift else "TTT"
        changed = list(data)
        for index, name in enumerate(groups[target]):
            if name in selected:
                char = previous[index]
                changed[index] = char if char in "HLhlT" else "T"
        expected.append(f"  V {{ {target} = {''.join(changed)}; }}")
        history[target].append(data)
    text = "\n".join(["Pattern PUBLIC {", "W W;", *(f"  V {{ {target} = {data}; }}" for target, data in rows), "}"])
    result = _shift_pattern_compare_vectors(text, {}, groups, selected, shift)
    assert result == "\n".join(["Pattern PUBLIC {", "W W;", *expected, "}"])


def test_shift_handles_repeats_multiple_statements_and_comments():
    text = "V { ALL = \\r2 hl; } /* V { ALL = LLLL; } */ Vector { ALL = \\r4 h; }"
    result = _shift_pattern_compare_vectors(text, {}, {"ALL": ("A", "B", "C", "D")}, {"A", "B"}, 1)
    assert result == "V { ALL = TThl; } /* V { ALL = LLLL; } */ Vector { ALL = hlhh; }"


def test_ignore_and_drop_apply_after_character_repetition_including_definitions():
    text = "MacroDefs { M { V { ALL = \\r2 hl; } } }"
    assert _rewrite_pattern_vectors(text, {}, {"ALL": ("A", "B", "C", "D")}, {"A"}, {"D"}) == (
        "MacroDefs { M { V { ALL = TlhT; } } }")


@pytest.mark.parametrize("text", ["", "a", "a\n", "a\r\nb\r", "\n\n", "a\vb\fc\x85d\u2028e\u2029"])
def test_incremental_source_lines_match_splitlines(text):
    assert list(_iter_source_lines(text)) == text.splitlines()


def write_split_source(tmp_path: Path) -> tuple[Path, Path]:
    project, pattern = tmp_path / "public_project.stil", tmp_path / "public.stil"
    project.write_text("\n".join([
        "Signals {", "  A In;", "  B Out;", "}",
        "SignalGroups {", "  ALL = 'A + B';", "}",
        "Timing {", "  WaveformTable W {", "    Period '400ns';", "    Waveforms {",
        "      A { 0 { '0ns' D; } }", "      A { 1 { '0ns' U; } }",
        "      B { H { '100ns' H; } }", "      B { L { '100ns' L; } }",
        "    }", "  }", "}",
    ]), encoding="utf-8")
    pattern.write_text("Pattern PUBLIC {\nW W;\nV { ALL = 0H; }\nV { ALL = 1L; }\n}\n", encoding="utf-8")
    return project, pattern


@pytest.mark.parametrize("module", [compareprep, stimulus])
def test_preparation_converts_large_pattern_only_once(tmp_path, monkeypatch, module):
    project, pattern = write_split_source(tmp_path)
    original = module.preprocess_ategen_stil
    calls = []

    def convert(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "preprocess_ategen_stil", convert)
    if module is compareprep:
        result = module.build_compare_ready_outputs(project_path=project, pattern_path=pattern,
                                                   output_dir=tmp_path / "out", keep_compare_signals={"B"})
    else:
        result = module.build_stimulus_only_outputs(project_path=project, pattern_path=pattern,
                                                  output_dir=tmp_path / "out")
    assert len(calls) == 1
    assert result.preprocessed_stil_path.exists()
    assert result.pin_map_path.exists()
