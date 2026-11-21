"""Machine-local bindings (STPG devices, GPIO controllers, analyzer identities) kept outside every portable project.

Location: see ``stpgview.config_location`` (``--config-dir``, ``STPG_CONFIG_DIR``, the per-user default), one rule
shared with separately installed tools. Files use the SDK schemas ``stpg_runner_device`` and
``stpg_gpio_controller``. Analyzer bindings (``analyzers/``) are written by the separate
logic-analyzer tool; this GUI reads only their USB identity (``ExternalDeviceBinding``), the RUN target of a manual
analyzer reset, and never writes them.
"""

from __future__ import annotations

from pathlib import Path
import re

from stpg_runner import ControllerConfig, DeviceConfig, ExternalDeviceBinding
from stpg_runner.artifacts import RunnerError, read_json, write_json

from ..config_location import resolve_config_dir

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def default_config_dir() -> Path:
    return resolve_config_dir().path


class BindingStore:
    KINDS = {"devices": DeviceConfig, "analyzers": ExternalDeviceBinding, "controllers": ControllerConfig}
    READ_ONLY = {"analyzers"}  # owned by the logic-analyzer tool: listed and leased here, never rewritten

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _folder(self, kind: str) -> Path:
        if kind not in self.KINDS:
            raise RunnerError(f"unknown binding kind {kind!r}")
        return self.root / kind

    def _path(self, kind: str, name: str) -> Path:
        if not isinstance(name, str) or not NAME.fullmatch(name) or name.endswith("."):
            raise RunnerError("binding names use letters, digits, '.', '_' and '-' (max 64)")
        return self._folder(kind) / f"{name}.json"

    def list(self, kind: str) -> list[dict]:
        folder = self._folder(kind)
        out = []
        try:  # an unreadable folder is reported, never shown as "no bindings"
            paths = sorted(p for p in folder.iterdir() if p.suffix == ".json") if folder.is_dir() else []
        except OSError as exc:
            raise RunnerError(f"cannot read the settings folder {folder}: {exc.strerror or exc}") from exc
        for path in paths:
            entry = {"name": path.stem, "path": str(path)}
            try:
                entry["config"] = self.KINDS[kind].from_dict(read_json(path)).to_dict()
            except (RunnerError, ValueError) as exc:
                entry["error"] = str(exc)
            out.append(entry)
        return out

    def load(self, kind: str, name: str):
        path = self._path(kind, name)
        if not path.is_file():
            raise RunnerError(f"no {kind[:-1]} binding named {name!r}")
        return self.KINDS[kind].from_dict(read_json(path))

    def save(self, kind: str, name: str, config: dict, *, replace: bool = False) -> dict:
        if kind in self.READ_ONLY:
            raise RunnerError("analyzer bindings are managed by the logic-analyzer tool, not by this GUI")
        value = self.KINDS[kind].from_dict(config)  # validates; unknown keys reject
        path = self._path(kind, name)
        if path.exists() and not replace:
            raise RunnerError(f"{kind[:-1]} binding {name!r} exists; confirm replacing it")
        write_json(path, value.to_dict())
        return {"name": name, "path": str(path), "config": value.to_dict()}

    def delete(self, kind: str, name: str) -> None:
        if kind in self.READ_ONLY:
            raise RunnerError("analyzer bindings are managed by the logic-analyzer tool, not by this GUI")
        path = self._path(kind, name)
        if not path.is_file():
            raise RunnerError(f"no {kind[:-1]} binding named {name!r}")
        path.unlink()
