"""Copyable source-first instructions, not assumed Releases or PATH entries.

These structure checks supplement the installed-wheel/real-browser execution.
They are not evidence of native Windows or PowerShell execution on Linux.
"""
from pathlib import Path
import re
import struct

import pytest
import stpgview

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("page", ["README.md", "manual/getting-started.md"])
def test_source_quick_start_is_explicit_and_device_free(page):
    text = (ROOT / page).read_text(encoding="utf-8")
    windows = "\n".join(re.findall(r"```powershell\n(.*?)```", text, re.S))
    posix = "\n".join(re.findall(r"```sh\n(.*?)```", text, re.S))
    assert "python -m venv stpg-env" in windows
    assert ".\\stpg-env\\Scripts\\python.exe -m pip install ." in windows
    assert "./stpg-env/bin/python -m pip install ." in posix
    for commands, python in [(windows, ".\\stpg-env\\Scripts\\python.exe"),
                             (posix, "./stpg-env/bin/python")]:
        assert f"{python} -m stpgview.physical.patterns samples" in commands
        assert f"{python} -m stpgview --demo-device --no-device-run" in commands
        assert "--config-dir ./settings-check --results-dir ./runs-check" in commands
        assert not re.search(r"<[^>]+>|Activate\.ps1|^stpg-", commands, re.M)


def test_optional_la_source_install_is_matched_and_module_based():
    text = (ROOT / "manual/logic-analyzer.md").read_text(encoding="utf-8")
    assert "-m pip install . ../STPG_RP2350_LA" in text
    assert "-m stpg_capture.ui.server --demo --no-device-io" in text
    source = (Path(stpgview.__file__).parent / "static/runner.js").read_text(encoding="utf-8")
    assert "rpLaInstallWindows" in source and "rpLaInstallPosix" in source
    assert "-m pip install . ../STPG_RP2350_LA" in source
    assert "-m stpg_capture.ui.server --demo --no-device-io" in source


def test_actual_demo_panels_are_linked_and_included_in_source_archives():
    text = (ROOT / "manual/getting-started.md").read_text(encoding="utf-8")
    assert "not a measured" in text and "Actual simulated-run result" in text
    for name in ("viewer-simulated.png", "run-simulated.png"):
        assert f"(images/{name})" in text
        raw = (ROOT / "manual/images" / name).read_bytes()
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", raw[16:24])
        assert width >= 600 and height >= 200
    assert "recursive-include manual *.md *.png" in (ROOT / "MANIFEST.in").read_text()
