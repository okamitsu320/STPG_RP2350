"""STIL input forms of a new runner project (GUI only; the SDK takes one normalized STIL).

``stpg_runner.init_project`` copies one STIL in the compiler's subset form. ATE
generator output (tester event codes such as ``'0ns' D;``, often a project file
plus a pattern file) is converted first with the compiler's own preprocessor,
``stil.preprocess.preprocess_ategen_stil`` -- the code behind
``stpg-stil-preprocess``. The checks here only route the input and give a clear
message early; the build remains the authority on whether a STIL compiles.
"""

from __future__ import annotations

import json
from pathlib import Path

from stpg_runner.artifacts import RunnerError
from stil.source_artifacts import open_source_binary

from ..stil_inputs import _ATE_EVENT, _PATTERN, _SIGNALS, _TIMING, _strip_comments, preprocessor_reads_signals

INPUT_FORMS = {
    "stil": "STIL as is (toolchain form, or already preprocessed)",
    "ate-pair": "ATE generator: project file + pattern file (preprocessed at Create)",
    "ate-single": "ATE generator: one file with both (preprocessed at Create)",
}
PREPROCESS_RECORD = "preprocess.json"
HEAD_BYTES = 8 * 1024 * 1024  # Signals/Timing precede the Pattern block; the vectors are not needed here


def names(value) -> list[str]:
    """A comma-separated text (or a list) of names, in order, without blanks."""

    items = value.split(",") if isinstance(value, str) else list(value or [])
    return [item.strip() for item in items if isinstance(item, str) and item.strip()]


def _head(path: Path) -> tuple[str, bool]:
    """Comment-free text of the first HEAD_BYTES of a file and whether that is the whole file."""

    if not path.is_file():
        raise RunnerError(f"STIL input is missing/not a file: {path}")
    with open_source_binary(path) as stream:
        data = stream.read(HEAD_BYTES + 1)
    return _strip_comments(data[:HEAD_BYTES].decode("utf-8", errors="replace")), len(data) <= HEAD_BYTES


def _shape(path: Path) -> dict:
    text, whole = _head(path)
    has_pattern = _PATTERN.search(text) is not None
    timing = _TIMING.search(text) is not None
    signals = _SIGNALS.search(text) is not None
    # A project file whose Signals block is commented out: the preprocessor still reads it there.
    commented = not signals and timing and not has_pattern and whole and preprocessor_reads_signals(path)
    return {"pattern": has_pattern, "pattern_unknown": not has_pattern and not whole,
            "signals": signals or commented, "timing": timing,
            "ate_events": _ATE_EVENT.search(text) is not None}


def check_toolchain_form(path: Path) -> None:
    """Reject input that is evidently ATE-generator output and name the form to choose instead."""

    shape = _shape(path)
    if shape["signals"] and shape["timing"] and not shape["pattern"] and not shape["pattern_unknown"]:
        raise RunnerError(f"{path.name} looks like an ATE project file (Signals/Timing, no Pattern): choose "
                          "'ATE generator: project file + pattern file' and give the pattern file as STIL input")
    if shape["pattern"] and not shape["signals"]:
        raise RunnerError(f"{path.name} has a Pattern but no Signals: if it is an ATE pattern file, choose "
                          "'ATE generator: project file + pattern file' and add its project file")
    if shape["ate_events"]:
        raise RunnerError(f"{path.name} uses tester event codes ('0ns' D;): choose 'ATE generator: one file with "
                          "both' so that it is preprocessed")


def preprocess_ate_input(project: Path, pattern: Path, out_dir: Path, *, ignore: list[str],
                         pin_map: bool) -> tuple[Path, Path | None]:
    """Convert ATE-generator input into ``out_dir``; returns the STIL and, if asked, the default pin map."""

    from stil.preprocess import build_default_pin_map, preprocess_ategen_stil

    project, pattern = Path(project), Path(pattern)
    head = _shape(project)
    body = head if project == pattern else _shape(pattern)
    if not (head["signals"] and head["timing"]):
        swapped = project != pattern and body["signals"] and body["timing"] and head["pattern"]
        raise RunnerError(f"{project.name} has no Signals/Timing block"
                          + ("; the project and pattern files look swapped" if swapped else
                             ": choose the ATE project file of this pattern"))
    if not body["pattern"] and not body["pattern_unknown"]:
        raise RunnerError(f"{pattern.name} has no Pattern block: give the ATE pattern file as STIL input")
    try:
        result = preprocess_ategen_stil(project, pattern, ignore_signals=set(ignore))
    except AssertionError as exc:  # the preprocessor asserts that a Pattern block exists
        raise RunnerError(f"preprocess rejected {pattern.name}: no Pattern block found") from exc
    declared = [signal.name for signal in result.signals]
    unknown = [name for name in ignore if name not in declared]
    if unknown:
        raise RunnerError(f"ignore signals not declared in {project.name}: {unknown} (declared: {declared})")
    out_dir.mkdir(parents=True, exist_ok=True)
    converted = out_dir / "pattern.pre.stil"
    converted.write_text(result.output_text, encoding="utf-8")
    generated = None
    if pin_map:
        generated = out_dir / "pins.json"
        generated.write_text(json.dumps(build_default_pin_map(result, ignored_signals=set(ignore)), indent=2) + "\n",
                             encoding="utf-8")
    return converted, generated
