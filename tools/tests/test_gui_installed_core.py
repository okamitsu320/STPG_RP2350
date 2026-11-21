"""The core as a user installs it: the wheel in a clean environment, away from the source tree, no logic-analyzer tool.

Needs ``STPG_CORE_WHEEL`` (the built wheel) and, without network access, ``STPG_WHEELHOUSE`` (a folder holding the
dependencies). Without the wheel these tests are skipped, and a skip is **not** an installed-wheel pass. HOME and
every setting are temporary; no device option is used.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import zipfile

import pytest

_WHEEL = os.environ.get("STPG_CORE_WHEEL")
# Resolve in the caller's folder, before pip is launched in the fresh test root.
WHEEL = str(Path(_WHEEL).expanduser().resolve()) if _WHEEL else None
pytestmark = [pytest.mark.stpg_host_only,
              pytest.mark.skipif(not WHEEL, reason="set STPG_CORE_WHEEL to the built wheel; a skip is not a pass")]
BIN = "Scripts" if sys.platform == "win32" else "bin"
DRIVER = r"""
import json, sys, time
from pathlib import Path
from stpgview.physical import patterns
from stpgview.runner.service import ACKNOWLEDGEMENTS, RunnerService
from stpgview.session import Session

root = Path(sys.argv[1])
patterns.main([str(root / "samples")])
service = RunnerService(config_dir=root / "settings", device_run=False, demo=True, results_dir=root / "runs",
                        session=Session(root / "work"))
project = service.init_project({"stil": str(root / "samples" / "gui_tiny.stil"), "directory": str(root / "project"),
                                "wait_preset": "none", "completion": "hold", "storage": "psram",
                                "compile_mode": "auto", "loop_policy": "preserve", "segmentation": "single"})

def wait(job):
    while job.status == "running":
        time.sleep(0.02)
    return job.public(0)

built = wait(service.start_build(project["project_path"]))
assert built["status"] == "done", built["error"]
bundle = built["result"]
prep = service.prepare_run({"bundle": bundle["bundle_dir"], "manifest_sha256": bundle["manifest_sha256"],
                            "device": "simulated"})
done = wait(service.start_run({"nonce": prep["nonce"], "acknowledgements": {k: True for k in ACKNOWLEDGEMENTS}}))
print("RESULT " + json.dumps({"outcome": done["result"]["run"]["outcome"], "external": done["result"].get("external"),
                              "exit": done["result"]["overall"]["exit_code"], "accessory": prep["summary"]["accessories"]}))
"""


def environment(root: Path) -> dict:
    drop = ("PYTHONPATH", "STPG_CONFIG_DIR", "XDG_CONFIG_HOME", "APPDATA", "VIRTUAL_ENV")
    home = root / "home"
    home.mkdir(exist_ok=True)
    return {**{k: v for k, v in os.environ.items() if k not in drop}, "HOME": str(home), "USERPROFILE": str(home)}


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    root = tmp_path_factory.mktemp("core-only")
    subprocess.run([sys.executable, "-m", "venv", str(root / "env")], check=True, timeout=300)
    python = root / "env" / BIN / ("python.exe" if sys.platform == "win32" else "python")
    command = [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    if os.environ.get("STPG_WHEELHOUSE"):
        command += ["--no-index", "--find-links", str(Path(os.environ["STPG_WHEELHOUSE"]).expanduser().resolve())]
    subprocess.run(command + [WHEEL], check=True, timeout=600, env=environment(root), cwd=root)
    return root, python


def run(installed, *args):
    root, python = installed
    environ = environment(root)
    environ["PATH"] = ""  # no per-user or venv console-script lookup
    return subprocess.run([str(python), *args], capture_output=True, text=True, timeout=300, cwd=root,
                          env=environ)


def test_the_program_comes_from_the_installed_wheel(installed):
    root, _ = installed
    out = run(installed, "-c", "import stpgview, stpg_runner, stil; print(stpgview.__file__, stpg_runner.__file__)")
    assert out.returncode == 0, out.stderr
    for path in out.stdout.split():
        assert Path(path).resolve().is_relative_to((root / "env").resolve()), path


def test_the_example_command_writes_only_the_public_offline_inputs(installed, tmp_path):
    from stpgview.physical.patterns import PATTERNS
    root, _ = installed
    output = tmp_path / "samples"
    result = subprocess.run([str(root / "env" / BIN / "stpg-examples"), str(output)],
                            capture_output=True, text=True, timeout=30, cwd=root, env=environment(root))
    assert result.returncode == 0, result.stderr
    assert {p.name for p in output.iterdir()} == set(PATTERNS)
    assert all((output / name).read_bytes() == text.encode("utf-8") for name, text in PATTERNS.items())


def test_documented_module_commands_work_without_scripts_on_path(installed, tmp_path):
    from stpgview.physical.patterns import PATTERNS
    output = tmp_path / "new samples"
    result = run(installed, "-I", "-m", "stpgview.physical.patterns", str(output))
    assert result.returncode == 0, result.stderr
    expected = {name: text.encode("utf-8") for name, text in PATTERNS.items()}
    assert {p.name: p.read_bytes() for p in output.iterdir()} == expected
    # C1-01 also applies when using the module rather than its console wrapper.
    repeat = run(installed, "-I", "-m", "stpgview.physical.patterns", str(output))
    assert repeat.returncode != 0
    assert {p.name: p.read_bytes() for p in output.iterdir()} == expected
    help_result = run(installed, "-I", "-m", "stpgview", "--help")
    assert help_result.returncode == 0, help_result.stderr
    assert "--no-device-run" in help_result.stdout


def test_no_logic_analyzer_code_command_or_gpl_text_is_installed(installed):
    root, _ = installed
    assert run(installed, "-c", "import importlib.util as u; print(u.find_spec('stpg_capture'))").stdout.strip() == "None"
    scripts = {p.stem for p in (root / "env" / BIN).iterdir()}
    assert {"stpg-view", "stpg-runner"} <= scripts and not {"stpg-la", "stpg-capture"} & scripts
    with zipfile.ZipFile(WHEEL) as wheel:
        assert not [n for n in wheel.namelist() if n.startswith(("stpg_capture", "reviews/"))]
        gpl = b"GNU GENERAL " + b"PUBLIC LICENSE"  # split: this file can itself be in a wheel that ships tests
        assert not [n for n in wheel.namelist() if gpl in wheel.read(n)]


def test_the_installed_gui_serves_its_page_and_says_the_analyzer_tool_is_absent(installed, tmp_path):
    root, python = installed
    command = [str(python), "-I", "-m", "stpgview", "--no-browser", "--demo-device", "--no-device-run",
               "--config-dir", str(tmp_path / "my settings"), "--results-dir", str(tmp_path / "runs")]
    environ = environment(root)
    environ["PATH"] = ""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=root,
                               env=environ)
    lines, url, end = [], None, time.monotonic() + 60
    try:
        while time.monotonic() < end and url is None:
            line = process.stdout.readline()
            if not line:
                break
            lines.append(line)
            if "http://127.0.0.1" in line:
                url = line[line.index("http://"):].strip()
        assert url, lines
        base, token = url.split("/?token=")
        assert any("settings folder:" in line and "from --config-dir" in line for line in lines), lines
        html = urllib.request.urlopen(url, timeout=20).read().decode()
        assert "External accessory" in html
        assert b"renderExternal" in urllib.request.urlopen(f"{base}/static/runner.js", timeout=20).read()
        request = urllib.request.Request(f"{base}/api/runner/state", headers={"X-STPG-Token": token})
        state = json.loads(urllib.request.urlopen(request, timeout=20).read())
        assert state["accessory_tool"]["installed"] is False and state["config"]["state"] == "new"
        assert not (tmp_path / "my settings").exists()
    finally:
        process.terminate()
        process.wait(10)


def test_a_project_builds_and_runs_with_no_accessory(installed, tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    out = run(installed, str(driver), str(tmp_path))
    assert out.returncode == 0, out.stderr
    result = json.loads(next(line for line in out.stdout.splitlines() if line.startswith("RESULT "))[7:])
    assert result == {"outcome": "COMPLETED", "external": None, "exit": 0,
                      "accessory": {"controller": None, "external": None}}
