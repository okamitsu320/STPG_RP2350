"""Where the STPG tools keep local settings: one rule for ``stpg-view`` and for separately installed tools.

Order, decided once when a tool starts:

1. ``--config-dir FOLDER``;
2. the environment variable ``STPG_CONFIG_DIR``, when it is not blank;
3. the per-user default: ``%APPDATA%\\stpg`` on Windows, ``~/Library/Application Support/stpg`` on macOS,
   ``$XDG_CONFIG_HOME/stpg`` or ``~/.config/stpg`` elsewhere.

A value is used as typed: spaces inside or around it are kept. ``~`` and ``~/...`` mean the home folder. A relative
value is taken from the folder the tool was started in, and symbolic links are followed, so the folder shown at start
is the real one and does not change while the tool runs.

A blank ``--config-dir``, a ``~name`` that cannot be expanded and a value wrapped in quotes are errors: the tool stops
and does not fall back to another folder. A blank ``STPG_CONFIG_DIR`` counts as not set, and the start-up line says so.

Nothing here creates, changes, moves or copies a file. Standard library only, so that a separately installed tool
gets the same answer by calling the same code.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Mapping

ENV_NAME = "STPG_CONFIG_DIR"
OPTION = "--config-dir"
DEFAULT = "default"
BINDING_FOLDERS = ("devices", "controllers", "analyzers")
SOURCE_TEXT = {OPTION: "from --config-dir", ENV_NAME: f"from {ENV_NAME}", DEFAULT: "the per-user default"}


class ConfigLocationError(ValueError):
    """The requested settings folder cannot be used; nothing else is tried."""


@dataclass(frozen=True)
class ConfigLocation:
    path: Path
    source: str  # OPTION, ENV_NAME or DEFAULT
    notes: tuple[str, ...] = ()

    @property
    def source_text(self) -> str:
        return SOURCE_TEXT[self.source]


def _absolute(value: str, what: str, environ: Mapping[str, str]) -> Path:
    if any(ord(c) < 32 for c in value):
        raise ConfigLocationError(f"{what} contains a control character")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        raise ConfigLocationError(f"{what} is wrapped in quotes ({value}); give the folder without them")
    expanded = _expand_home(value, environ)
    if expanded.startswith("~"):
        raise ConfigLocationError(f"{what}: cannot expand {value.split('/')[0].split(os.sep)[0]}; give a full path")
    return _real(Path(expanded), what)


def _real(path: Path, what: str) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:  # e.g. a loop of symbolic links
        raise ConfigLocationError(f"{what}: cannot resolve {path}: {exc}") from exc


def _expand_home(value: str, environ: Mapping[str, str]) -> str:
    if value == "~" or value.startswith(("~/", "~" + os.sep)):
        return str(_home(environ)) + value[1:]
    return os.path.expanduser(value)


def _home(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("USERPROFILE" if sys.platform == "win32" else "HOME") or Path.home())


def default_location(environ: Mapping[str, str] | None = None, platform: str | None = None) -> tuple[Path, tuple[str, ...]]:
    """The per-user default folder and any note about ignored environment values."""

    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = _home(environ)
    if platform == "win32":
        return Path(environ.get("APPDATA") or home / "AppData" / "Roaming") / "stpg", ()
    if platform == "darwin":
        return home / "Library" / "Application Support" / "stpg", ()
    xdg = environ.get("XDG_CONFIG_HOME") or ""
    if xdg and not os.path.isabs(xdg):  # the XDG rule: a relative value is invalid and ignored
        return home / ".config" / "stpg", (f"XDG_CONFIG_HOME is not an absolute path and was ignored ({xdg}).",)
    return (Path(xdg) if xdg else home / ".config") / "stpg", ()


def resolve_config_dir(explicit: str | os.PathLike | None = None,
                       environ: Mapping[str, str] | None = None) -> ConfigLocation:
    """The settings folder of this start. Raises ConfigLocationError rather than falling back to another folder."""

    environ = os.environ if environ is None else environ
    if explicit is not None:
        value = os.fspath(explicit)
        if not value.strip():
            raise ConfigLocationError(f"{OPTION} is blank; give a folder or leave the option out")
        return ConfigLocation(_absolute(value, OPTION, environ), OPTION)
    value = environ.get(ENV_NAME)
    if value is not None and value.strip():
        return ConfigLocation(_absolute(value, ENV_NAME, environ), ENV_NAME)
    folder, notes = default_location(environ)
    if value is not None and value != "":
        notes = (f"{ENV_NAME} is blank and was ignored.", *notes)
    return ConfigLocation(_real(folder, "the default settings folder"), DEFAULT, notes)


def describe_config_dir(path: Path) -> dict:
    """What is in the folder now, for display. Reads names only and never creates anything.

    ``state`` is ``new`` (no folder yet), ``empty`` (no binding files), ``in use`` or ``problem`` (see ``problem``).
    """

    counts = dict.fromkeys(BINDING_FOLDERS, 0)
    try:
        if not path.exists():
            return {"state": "new", "problem": None, "counts": counts}
        if not path.is_dir():
            return {"state": "problem", "problem": f"{path} is a file, not a folder", "counts": counts}
        with os.scandir(path):
            pass
        for name in BINDING_FOLDERS:
            folder = path / name
            if folder.is_dir():
                with os.scandir(folder) as entries:
                    counts[name] = sum(1 for entry in entries if entry.name.endswith(".json"))
    except OSError as exc:
        return {"state": "problem", "problem": f"cannot read {path}: {exc.strerror or exc}", "counts": counts}
    return {"state": "in use" if any(counts.values()) else "empty", "problem": None, "counts": counts}


def state_text(info: dict) -> str:
    """One line for start-up output and pages."""

    if info["state"] == "new":
        return "new: created when the first binding is saved"
    if info["state"] == "problem":
        return info["problem"]
    if info["state"] == "empty":
        return "no bindings yet"
    counts = info["counts"]
    return ", ".join(f"{counts[name]} {name}" for name in BINDING_FOLDERS if counts[name])


def start_line(tool: str, location: ConfigLocation) -> str:
    """The start-up line of a tool: the folder, why it was chosen and what is in it."""

    text = f"{tool}: settings folder: {location.path} ({location.source_text}; {state_text(describe_config_dir(location.path))})"
    return "\n".join((text, *(f"{tool}: note: {note}" for note in location.notes)))
