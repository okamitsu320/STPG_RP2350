"""The settings folder rule of stpg-view: order, path forms, what the tool says, and that nothing is changed for you.

Host only. Every case uses temporary folders and an explicit environment; no real user settings are read. The
same rule in the separately installed logic-analyzer tool is tested in that tool's suite against this code.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from stpg_runner.artifacts import RunnerError
from stpgview.cli import main as view_main
from stpgview.config_location import (DEFAULT, ENV_NAME, OPTION, ConfigLocationError, default_location,
                                      describe_config_dir, resolve_config_dir, start_line, state_text)
from stpgview.runner.service import RunnerService
from stpgview.runner.simulated import SIMULATED_DEVICE
from stpgview.runner.store import BindingStore
from stpgview.server import make_runner
from stpgview.session import Session

from stpg_hardware_guard import forbid_hardware

pytestmark = pytest.mark.stpg_host_only
HOME_KEY = "USERPROFILE" if sys.platform == "win32" else "HOME"


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


def env(home: Path, **extra) -> dict:
    return {HOME_KEY: str(home), **extra}


def tree_digest(root: Path) -> dict:
    """Every file under root with its content hash and modification time."""

    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


# ---- order ------------------------------------------------------------------------------------------------------

def test_the_option_wins_over_the_variable_and_the_variable_over_the_default(tmp_path):
    variable = {ENV_NAME: str(tmp_path / "from-env")}
    chosen = resolve_config_dir(tmp_path / "from-option", env(tmp_path / "home", **variable))
    assert (chosen.path, chosen.source) == ((tmp_path / "from-option").resolve(), OPTION)
    chosen = resolve_config_dir(None, env(tmp_path / "home", **variable))
    assert (chosen.path, chosen.source) == ((tmp_path / "from-env").resolve(), ENV_NAME)
    chosen = resolve_config_dir(None, env(tmp_path / "home"))
    assert chosen.source == DEFAULT and chosen.path.name == "stpg"


@pytest.mark.parametrize("blank,noted", [("", False), ("   ", True), ("\t", True)])
def test_a_blank_variable_counts_as_not_set_and_a_non_empty_one_is_reported(tmp_path, blank, noted):
    chosen = resolve_config_dir(None, env(tmp_path, **{ENV_NAME: blank}))
    assert chosen.source == DEFAULT and bool(chosen.notes) is noted
    if noted:
        assert "blank and was ignored" in chosen.notes[0]


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_option_is_an_error_and_never_falls_back(tmp_path, blank):
    with pytest.raises(ConfigLocationError, match="--config-dir is blank"):
        resolve_config_dir(blank, env(tmp_path, **{ENV_NAME: str(tmp_path / "from-env")}))


# ---- path forms -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [("~", "."), ("~/cfg", "cfg"), ("~/my settings/stpg", "my settings/stpg")])
def test_the_home_folder_is_expanded_the_same_from_option_and_variable(tmp_path, value, expected):
    target = (tmp_path / expected).resolve()
    assert resolve_config_dir(value, env(tmp_path)).path == target
    assert resolve_config_dir(None, env(tmp_path, **{ENV_NAME: value})).path == target


def test_an_unknown_user_home_is_an_error_not_a_folder_named_tilde(tmp_path):
    for call in (lambda: resolve_config_dir("~no-such-user-for-this-test/x", env(tmp_path)),
                 lambda: resolve_config_dir(None, env(tmp_path, **{ENV_NAME: "~no-such-user-for-this-test"}))):
        with pytest.raises(ConfigLocationError, match="cannot expand"):
            call()


@pytest.mark.parametrize("name", ["with space", " leading", "trailing ", "日本語 フォルダ"])
def test_spaces_and_other_characters_are_kept_as_typed(tmp_path, name):
    assert resolve_config_dir(str(tmp_path / name), env(tmp_path)).path.name == name
    assert resolve_config_dir(None, env(tmp_path, **{ENV_NAME: str(tmp_path / name)})).path.name == name


def test_a_relative_value_is_taken_from_the_start_folder_once(tmp_path, monkeypatch):
    start = tmp_path / "started-here"
    start.mkdir()
    monkeypatch.chdir(start)
    from_option = resolve_config_dir("rel/cfg", env(tmp_path))
    from_env = resolve_config_dir(None, env(tmp_path, **{ENV_NAME: "rel/cfg"}))
    assert from_option.path == from_env.path == (start / "rel" / "cfg").resolve() and from_env.path.is_absolute()
    monkeypatch.chdir(tmp_path)  # a later change of folder changes nothing: the value was fixed at start
    assert from_env.path == (start / "rel" / "cfg").resolve()


@pytest.mark.parametrize("value", ['"C:\\my settings"', "'/tmp/x'", "bad\x01name"])
def test_quoted_and_control_character_values_are_refused(tmp_path, value):
    with pytest.raises(ConfigLocationError):
        resolve_config_dir(None, env(tmp_path, **{ENV_NAME: value}))
    with pytest.raises(ConfigLocationError):
        resolve_config_dir(value, env(tmp_path))


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need extra rights on Windows")
def test_a_symbolic_link_is_followed_at_start_so_a_later_change_cannot_move_the_settings(tmp_path):
    first, second, link = tmp_path / "first", tmp_path / "second", tmp_path / "link"
    first.mkdir()
    second.mkdir()
    link.symlink_to(first)
    chosen = resolve_config_dir(link, env(tmp_path))
    link.unlink()
    link.symlink_to(second)
    assert chosen.path == first.resolve()


@pytest.mark.parametrize("platform,variables,expected", [
    ("win32", {"APPDATA": "C:/Profiles/test/AppData/Roaming"}, "C:/Profiles/test/AppData/Roaming/stpg"),
    ("win32", {}, "HOME/AppData/Roaming/stpg"),
    ("darwin", {}, "HOME/Library/Application Support/stpg"),
    ("linux", {}, "HOME/.config/stpg"),
    ("linux", {"XDG_CONFIG_HOME": "/xdg"}, "/xdg/stpg"),
])
def test_the_default_folder_of_each_platform(tmp_path, platform, variables, expected):
    # A model of the rule on this host; a native run on each OS is a separate check.
    folder, notes = default_location({"HOME": "HOME", "USERPROFILE": "HOME", **variables}, platform)
    assert folder.as_posix() == expected and notes == ()


def test_a_relative_xdg_value_is_ignored_and_reported():
    folder, notes = default_location({"HOME": "/h", "XDG_CONFIG_HOME": "rel"}, "linux")
    assert folder.as_posix() == "/h/.config/stpg" and "XDG_CONFIG_HOME" in notes[0]


# ---- what is reported, and that reporting changes nothing --------------------------------------------------------

def test_the_folder_state_is_described_without_creating_it(tmp_path):
    missing = tmp_path / "missing"
    assert describe_config_dir(missing)["state"] == "new" and not missing.exists()
    assert "created when the first binding is saved" in state_text(describe_config_dir(missing))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert describe_config_dir(empty)["state"] == "empty" and state_text(describe_config_dir(empty)) == "no bindings yet"
    (empty / "devices").mkdir()
    (empty / "devices" / "a.json").write_text("{}")
    (empty / "analyzers").mkdir()
    (empty / "analyzers" / "b.json").write_text("{}")
    info = describe_config_dir(empty)
    assert info["state"] == "in use" and info["counts"] == {"devices": 1, "controllers": 0, "analyzers": 1}
    assert state_text(info) == "1 devices, 1 analyzers"


def test_a_file_in_place_of_the_folder_is_a_problem_and_no_other_folder_is_used(tmp_path):
    target = tmp_path / "not-a-folder"
    target.write_text("x")
    info = describe_config_dir(target)
    assert info["state"] == "problem" and "is a file" in info["problem"]
    service = RunnerService(config_dir=target, device_run=False, demo=False, results_dir=tmp_path / "r")
    assert service.state()["config"]["state"] == "problem" and service.store.root == target
    with pytest.raises(OSError):
        service.store.save("devices", "dev", SIMULATED_DEVICE.to_dict())
    assert target.read_text() == "x"


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions and a non-root user")
def test_an_unreadable_folder_is_reported_not_shown_as_empty(tmp_path):
    target = tmp_path / "locked"
    (target / "devices").mkdir(parents=True)
    (target / "devices" / "dev.json").write_text("{}")
    os.chmod(target / "devices", 0)
    try:
        info = describe_config_dir(target)
        assert info["state"] == "problem" and "cannot read" in info["problem"]
        with pytest.raises(RunnerError, match="cannot read the settings folder"):
            BindingStore(target).list("devices")
    finally:
        os.chmod(target / "devices", 0o755)


def test_start_line_names_folder_source_and_state(tmp_path):
    line = start_line("stpg-view", resolve_config_dir(None, env(tmp_path, **{ENV_NAME: str(tmp_path / "s")})))
    assert line == (f"stpg-view: settings folder: {(tmp_path / 's').resolve()} (from {ENV_NAME}; "
                    "new: created when the first binding is saved)")
    noted = start_line("stpg-view", resolve_config_dir(None, env(tmp_path, **{ENV_NAME: "  "})))
    assert noted.splitlines()[1] == f"stpg-view: note: {ENV_NAME} is blank and was ignored."


def test_the_runner_uses_and_reports_the_resolved_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_NAME, str(tmp_path / "from env"))
    runner = make_runner(Session(tmp_path / "work"), host="127.0.0.1", device_run=False, results_dir=tmp_path / "r")
    state = runner.state()
    assert Path(state["config_dir"]) == (tmp_path / "from env").resolve() == runner.store.root
    assert (state["config"]["source"], state["config"]["state"]) == (ENV_NAME, "new")
    assert state["config"]["source_text"] == f"from {ENV_NAME}"
    chosen = make_runner(Session(tmp_path / "work2"), host="127.0.0.1", device_run=False, results_dir=tmp_path / "r",
                         config_dir=tmp_path / "option")
    assert chosen.state()["config"]["source"] == OPTION
    assert not (tmp_path / "from env").exists() and not (tmp_path / "option").exists()  # starting creates nothing


def test_starting_and_listing_leave_existing_files_exactly_as_they_are(tmp_path):
    root = tmp_path / "settings"
    (root / "devices").mkdir(parents=True)
    (root / "analyzers").mkdir()
    (root / "other").mkdir()
    (root / "devices" / "old-format.json").write_text('{"kind": "an_old_schema", "port": "COM9"}')
    (root / "devices" / "ok.json").write_text(json.dumps(SIMULATED_DEVICE.to_dict()))
    (root / "analyzers" / "broken.json").write_text("{not json")
    (root / "other" / "notes.txt").write_text("kept")
    before = tree_digest(root)
    service = make_runner(Session(tmp_path / "work"), host="127.0.0.1", device_run=False, config_dir=root,
                          results_dir=tmp_path / "r")
    items = {i["name"]: i for i in service.bindings("devices")["items"]}
    assert "old-format" in items and items["old-format"].get("error") and "config" not in items["old-format"]
    assert items["ok"]["config"]["port"] == SIMULATED_DEVICE.port  # the good file still loads beside the bad one
    assert service.bindings("analyzers")["items"][0]["error"]  # a reason, not a repaired file
    assert service.state()["config"]["counts"] == {"devices": 2, "controllers": 0, "analyzers": 1}
    assert tree_digest(root) == before


def test_a_name_that_exists_is_not_overwritten_without_an_explicit_replace(tmp_path):
    store = BindingStore(tmp_path)
    store.save("devices", "dev", SIMULATED_DEVICE.to_dict())
    before = tree_digest(tmp_path)
    with pytest.raises(RunnerError, match="exists"):
        store.save("devices", "dev", SIMULATED_DEVICE.to_dict())
    assert tree_digest(tmp_path) == before


# ---- the command --------------------------------------------------------------------------------------------------

def test_the_command_stops_on_a_blank_option_before_anything_is_served(capsys):
    assert view_main(["--config-dir", "", "--no-browser", "--no-device-run"]) == 2
    captured = capsys.readouterr()
    assert "--config-dir is blank" in captured.err and "pattern viewer" not in captured.out


def start_tool(argv: list[str], environ: dict, cwd: Path) -> list[str]:
    """Start a tool, return its start-up lines and stop it. No device option is used."""

    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environ, cwd=cwd)
    lines, end = [], time.monotonic() + 30
    try:
        while time.monotonic() < end:
            line = process.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
            if "Press Ctrl+C" in line:
                break
    finally:
        process.terminate()
        process.wait(10)
    return lines


def test_the_real_command_prints_the_folder_and_creates_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    environ = {k: v for k, v in os.environ.items() if k not in (ENV_NAME, "XDG_CONFIG_HOME", "APPDATA")}
    environ.update({HOME_KEY: str(home), ENV_NAME: str(tmp_path / "my settings"),
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    lines = start_tool([sys.executable, "-m", "stpgview", "--no-browser", "--no-device-run", "--results-dir",
                        str(tmp_path / "r")], environ, tmp_path)
    expected = f"stpg-view: settings folder: {(tmp_path / 'my settings').resolve()} (from {ENV_NAME};"
    assert any(line.startswith(expected) for line in lines), lines
    assert not (tmp_path / "my settings").exists() and list(home.iterdir()) == []
