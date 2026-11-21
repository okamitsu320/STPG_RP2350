"""Guard checks always install an independent fake before touching the guard.

Disabling the guard in these tests can only reach our fake constructors. No
debugger executable, real serial constructor or OS port enumeration is used.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys

import pytest
import serial
from serial.tools import list_ports

import stpg_hardware_guard as guard


@pytest.fixture
def lower_layer(monkeypatch):
    recorded = []

    def fake_process(*args, **kwargs):
        recorded.append(("process", args, kwargs))
        raise OSError("independent fake: no executable started")

    def fake_serial(*args, **kwargs):
        recorded.append(("serial", args, kwargs))

    def fake_ports(*args, **kwargs):
        recorded.append(("ports", args, kwargs))
        return []

    monkeypatch.setattr(subprocess, "Popen", fake_process)
    monkeypatch.setattr(serial, "Serial", fake_serial)
    monkeypatch.setattr(list_ports, "comports", fake_ports)
    return recorded


@pytest.fixture(params=["portable", "gui"])
def protected(request, lower_layer):
    # Fixture wrappers are unwrapped only AFTER the fake layer is installed.
    if request.param == "portable":
        from conftest import no_physical_contact
        install = no_physical_contact.__wrapped__
    else:
        install = guard.forbid_hardware
    with pytest.MonkeyPatch.context() as patch:
        install(patch)
        yield lower_layer


@pytest.mark.parametrize("program", sorted(guard.DEBUG_EXECUTABLES))
@pytest.mark.parametrize("form", ["argv", "windows", "bytes", "shell", "override"])
def test_debugger_forms_never_reach_the_lower_layer(protected, program, form):
    options = {}
    if form == "argv":
        args = [Path("/not-installed") / program, "--version"]
    elif form == "windows":
        args = ["C:\\Unavailable tools\\" + program.upper() + ".EXE"]
    elif form == "bytes":
        args = program.encode()
    elif form == "shell":
        args = "cd /nonexistent && '" + program + "' --version"
        options["shell"] = True
    else:
        args = [sys.executable, "-I", "-c", "pass"]
        options["executable"] = program
    with pytest.raises(AssertionError, match="debugger or flasher"):
        subprocess.Popen(args, **options)
    assert not protected


@pytest.mark.parametrize("invocation", [
    lambda: subprocess.Popen([sys.executable], -1, "gdb"),
    lambda: subprocess.Popen(["picotool info"], shell=True),
    lambda: subprocess.Popen(["openocd --version"], -1, None, None, None, None, None, True, True),
    lambda: subprocess.run(["openocd"], capture_output=True),
    lambda: subprocess.check_output(["gdb-multiarch"]),
    lambda: subprocess.check_call(["pyocd"]),
    lambda: asyncio.run(asyncio.create_subprocess_exec("picotool")),
])
def test_alternate_process_entry_forms_are_checked(protected, invocation):
    with pytest.raises(AssertionError, match="debugger or flasher"):
        invocation()
    assert not protected


def test_serial_and_enumerator_do_not_reach_the_os(protected):
    with pytest.raises(AssertionError):
        serial.Serial("FAKE-PORT-NEVER-OPEN")
    try:
        ports = list_ports.comports()  # GUI uses an empty-list stub.
    except AssertionError:
        ports = []  # The portable guard deliberately refuses enumeration instead.
    assert ports == [] and not protected


def test_disabling_the_guard_only_reaches_the_independent_fake(lower_layer):
    with pytest.raises(OSError, match="independent fake"):
        subprocess.Popen(["openocd"])
    serial.Serial("FAKE-PORT-NEVER-OPEN")
    assert list_ports.comports() == []
    assert [call[0] for call in lower_layer] == ["process", "serial", "ports"]


@pytest.mark.parametrize("command", [[sys.executable, "-I", "-c", "pass"], ["node", "--version"],
                                     ["geckodriver", "--version"], ["gdbus"], ["openocd-notes.txt"]])
def test_ordinary_programs_are_forwarded_only_to_the_fake(protected, command):
    with pytest.raises(OSError, match="independent fake"):
        subprocess.Popen(command)
    assert protected == [("process", (command,), {})]


def test_real_host_python_still_works_with_the_suite_guard():
    done = subprocess.run([sys.executable, "-I", "-c", "print('host-only control')"],
                          capture_output=True, text=True, timeout=10)
    assert done.returncode == 0 and done.stdout == "host-only control\n"


@pytest.mark.parametrize("command,options", [([], {}), ("", {}), (["python", "gdb"], {}),
                                            (["node"], {"executable": None})])
def test_non_executable_arguments_are_not_debuggers(command, options):
    assert not guard.debug_process_requested(command, options=options)
