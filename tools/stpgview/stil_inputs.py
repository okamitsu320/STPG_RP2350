"""Which STIL forms the viewer can open, and how.

The viewer reads exactly what the repository's STIL compiler front end reads
(``stil.parser``): the toolchain subset form, e.g. ``0: 0ns Drive(0);``.
ATE-generator output that uses tester event codes (``'0ns' D;``) is converted
first with the compiler's own preprocessor (``stil.preprocess``), either as a
project + pattern pair or as one file containing both. Anything else is
rejected with the compiler's error plus a hint; nothing is silently guessed.
"""

from __future__ import annotations

from decimal import InvalidOperation
from pathlib import Path
import re

from stil.errors import StilError

from .load_stil import load_stil
from .model import Trace

_ATE_EVENT = re.compile(r"'[^']*'\s*[UDZHLXT](?:/[UDZHLXT])*\s*;")
_PATTERN = re.compile(r"\bPattern\s+[A-Za-z_][A-Za-z0-9_]*\s*\{")
_SIGNALS = re.compile(r"(?m)^\s*Signals\s*\{")
_TIMING = re.compile(r"(?m)^\s*Timing\b[^{]*\{")


def _strip_comments(text: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)


def preprocessor_reads_signals(path: Path) -> bool:
    """Whether ``stil.preprocess`` reads Signals from this ATE project file.

    The preprocessor takes the first ``Signals`` block of the raw text, also one inside a comment:
    ATE-generator project files can carry the block commented out (``/* Signals { ... } */``), and it
    reads commented SignalGroups the same way. Ask only for a file without a Pattern block.
    """

    from stil.preprocess import read_ategen_signals

    try:
        return bool(read_ategen_signals(path))
    except (StilError, OSError, UnicodeDecodeError):
        return False


def stil_role(path: Path) -> str:
    """'project' (Signals/Timing, no Pattern), 'pattern' (Pattern, no Signals) or 'single'."""

    text = _strip_comments(path.read_text(errors="replace"))
    has_pattern = _PATTERN.search(text) is not None
    has_project = _TIMING.search(text) is not None and (
        _SIGNALS.search(text) is not None or (not has_pattern and preprocessor_reads_signals(path)))
    if has_project and not has_pattern:
        return "project"
    if has_pattern and _SIGNALS.search(text) is None:
        return "pattern"
    return "single"


def uses_ate_event_codes(path: Path) -> bool:
    return _ATE_EVENT.search(_strip_comments(path.read_text(errors="replace"))) is not None


def preprocess_to(project: Path, pattern: Path, work_dir: Path) -> Path:
    from stil.preprocess import preprocess_ategen_stil

    result = preprocess_ategen_stil(project, pattern)
    folder = work_dir / "preprocessed"
    folder.mkdir(parents=True, exist_ok=True)
    name = pattern.stem if project == pattern else f"{project.stem}__{pattern.stem}"
    out = folder / f"{name}.pre.stil"
    out.write_text(result.output_text, encoding="utf-8")
    return out


def hint_for(exc: Exception, path: Path) -> str:
    """A short explanation for the most common reasons a STIL is rejected."""

    message = str(exc)
    text = ""
    try:
        text = _strip_comments(path.read_text(errors="replace"))
    except OSError:
        pass
    if isinstance(exc, InvalidOperation) or "ConversionSyntax" in message:
        return ("a time value is not a plain literal such as '100ns' (Spec/Category variables and "
                "expressions are not supported)")
    if "active_pins out of supported range: 0" in message or re.search(r"Signals\s*\{[^\n}]*;[^\n}]*\}", text):
        return ("no signals were recognised: the subset parser reads one declaration per line, so blocks "
                "written on one line (e.g. 'Signals { A In; B Out; }') are not supported")
    if "missing waveform-char definitions" in message or _ATE_EVENT.search(text):
        return ("this WaveformTable uses tester event codes ('0ns' D/U). The toolchain form "
                "('0: 0ns Drive(0);') is read directly; ATE-generator files in one-character-per-line "
                "form are converted automatically, other event-code layouts are not supported")
    if "bad Signals declaration" in message:
        return "quoted signal names (e.g. \"DQ[0]\") are not part of the supported subset"
    if "unsupported Pattern statement" in message:
        return "see the STIL subset reference for supported Pattern statements (V, W, Loop, Macro, Call, Goto, Stop)"
    return ""


def load_stil_input(path: Path, *, pin_map: Path | None, work_dir: Path,
                    project: Path | None = None) -> Trace:
    """Load a STIL for viewing, preprocessing ATE-generator input when needed."""

    if project is not None:
        pre = preprocess_to(project, path, work_dir)
        trace = load_stil(pre, pin_map=pin_map)
        trace.origin_path = path  # type: ignore[attr-defined]
        trace.preprocessed_from = {"project": str(project), "pattern": str(path)}  # type: ignore[attr-defined]
        trace.title = f"{path.stem}.pre.stil"
        if project == path:
            trace.notes.insert(0, f"{path.name} uses tester event codes; converted with stil.preprocess for viewing")
            trace.sources.append({"kind": "ategen_stil", "path": str(path), "size": path.stat().st_size,
                                  "sha256": _sha(path)})
        else:
            trace.notes.insert(0, f"converted with stil.preprocess from project {project.name} + pattern {path.name}")
            trace.sources.extend([{"kind": "ategen_project", "path": str(project), "size": project.stat().st_size,
                                   "sha256": _sha(project)},
                                  {"kind": "ategen_pattern", "path": str(path), "size": path.stat().st_size,
                                   "sha256": _sha(path)}])
        return trace
    role = stil_role(path)
    if role == "project":
        raise StilError("ERR_VIEW_PROJECT_ONLY",
                        f"{path.name} looks like an ATE-generator project file (Signals/Timing without a "
                        "Pattern); open it together with its pattern file")
    if role == "pattern":
        raise StilError("ERR_VIEW_PATTERN_ONLY",
                        f"{path.name} looks like an ATE-generator pattern file (Pattern without Signals); "
                        "open it together with its project file")
    try:
        return load_stil(path, pin_map=pin_map)
    except (StilError, InvalidOperation, ValueError, KeyError) as exc:
        if uses_ate_event_codes(path):
            try:
                return load_stil_input(path, pin_map=pin_map, work_dir=work_dir, project=path)
            except Exception:  # noqa: BLE001 - report the original, more relevant error
                pass
        hint = hint_for(exc, path)
        detail = f"{type(exc).__name__}: {exc}" if not isinstance(exc, StilError) else str(exc)
        raise StilError(getattr(exc, "code", "ERR_VIEW_STIL"),
                        f"{path.name}: cannot be read by the STIL subset front end: {detail}"
                        + (f"\nhint: {hint}" if hint else "")) from exc


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
