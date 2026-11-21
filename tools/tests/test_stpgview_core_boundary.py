"""The MIT core contains no logic-analyzer code and starts without it (static checks, one subprocess import).

The logic-analyzer tool is a separate GPL distribution and a separate process: the core never imports it, never
serves its pages or routes and reaches it only through the SDK's external accessory contract (a local config file
and a child process). These checks fail when an import, a route or a page reference creeps back in.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.stpg_host_only
TOOLS = Path(__file__).resolve().parents[1]
CORE_PACKAGES = ("stpg", "stil", "stpg_runner", "stpgview", "scripts")
ANALYZER_PACKAGES = {"stpg_capture", "stpg_la"}


def _package_dir(name: str) -> Path:
    """Where the package is imported from: the checkout in a source run, site-packages when testing an install."""

    spec = importlib.util.find_spec(name)
    return Path(spec.submodule_search_locations[0]) if spec and spec.submodule_search_locations else TOOLS / name


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.lineno, node.module
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
                "import_module", "__import__"):
            yield from ((node.lineno, a.value) for a in node.args if isinstance(a, ast.Constant)
                        and isinstance(a.value, str))


def test_no_core_module_imports_the_analyzer_package():
    found = [f"{path.relative_to(_package_dir(package).parent)}:{line}: {name}"
             for package in CORE_PACKAGES for path in sorted(_package_dir(package).rglob("*.py"))
             for line, name in _imports(path) if name.split(".")[0] in ANALYZER_PACKAGES]
    assert found == []
    assert not (TOOLS / "stpg_capture").exists()  # the package lives in the separate distribution


def test_the_core_page_has_no_analyzer_routes_or_scripts():
    static = _package_dir("stpgview") / "static"
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(static.iterdir()) if p.suffix in (".js", ".html"))
    for needle in ("/api/capture", "/api/overlay", "/api/runner/wiring", "requestOverlay", "drawOverlay",
                   "captureForm", "/plugin/"):
        assert needle not in text, needle
    server = (_package_dir("stpgview") / "server.py").read_text(encoding="utf-8")
    assert "_api_capture" not in server and "_api_overlay" not in server


def test_an_installed_analyzer_tool_is_reported_from_its_metadata_without_importing_or_starting_it(tmp_path,
                                                                                                   monkeypatch):
    from importlib import metadata

    from stpgview.runner import service as service_module

    real_version = metadata.version
    monkeypatch.setattr(metadata, "version", lambda name: "9.9.9" if name == service_module.LA_DISTRIBUTION
                        else real_version(name))  # "installed", by its metadata only
    started = []

    def no_process(*args, **kwargs):  # a fake process layer: nothing can be started from this test
        started.append(args)
        raise OSError("fake process layer: nothing was started")

    class Block:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in ANALYZER_PACKAGES:
                raise ImportError("analyzer package blocked: " + name)

    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(sys, "meta_path", [Block(), *sys.meta_path])
    loaded = {name for name in sys.modules if name.split(".")[0] in ANALYZER_PACKAGES}
    service = service_module.RunnerService(config_dir=tmp_path / "cfg", device_run=False, demo=True,
                                           results_dir=tmp_path / "runs")
    assert service.state()["accessory_tool"] == {"distribution": "stpg-rp2350-la", "command": "stpg-la",
                                                 "installed": True, "version": "9.9.9"}
    assert started == [] and {name for name in sys.modules if name.split(".")[0] in ANALYZER_PACKAGES} == loaded
    assert not (tmp_path / "cfg").exists()


def test_the_core_starts_without_the_analyzer_package(tmp_path):
    code = ("import sys\n"
            "class Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in ('stpg_capture', 'stpg_la'):\n"
            "            raise ImportError('analyzer package blocked: ' + name)\n"
            "sys.meta_path.insert(0, Block())\n"
            "import stpgview.server, stpgview.runner.service, stpg_runner.cli, stpg_runner.external_accessory\n"
            "from stpgview.runner.service import RunnerService\n"
            "RunnerService(config_dir=sys.argv[1], device_run=False, demo=True, results_dir=sys.argv[2])\n"
            "print('ok', sorted(m for m in sys.modules if m.split('.')[0] in ('stpg_capture', 'stpg_la')))\n")
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path / "cfg"), str(tmp_path / "runs")],
                         capture_output=True, text=True, timeout=120, cwd=TOOLS)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok []"
