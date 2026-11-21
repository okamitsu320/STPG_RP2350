"""Host-only guard for the GUI runner tests: no serial open and no USB metadata enumeration.

A demo switch or an injected stream is not a non-contact guard by itself, so
every GUI test module installs this autouse: the real pyserial constructor,
``serial.tools.list_ports.comports`` and the imported SDK and GPIO controller
opener and enumeration aliases are replaced. Loopback HTTP sockets stay
available. The logic-analyzer tool is a separate distribution with its own guard;
nothing here imports it.
"""

from __future__ import annotations

import ntpath
import os
import shlex
import subprocess


DEBUG_EXECUTABLES = frozenset(("openocd", "gdb", "gdb-multiarch", "arm-none-eabi-gdb", "picotool", "pyocd"))


def debug_process_requested(command, extra=(), options=None) -> bool:
    """Conservative Popen policy, not an OS sandbox or a general shell parser.

    Inspect both the apparent program and an executable override. Command-line
    strings are tokenized even without shell=True; a harmless mention may also
    be rejected. Never execute a command to discover its type.
    """
    options = {} if options is None else options
    override = options.get("executable", extra[1] if len(extra) > 1 else None)
    shell = options.get("shell", extra[7] if len(extra) > 7 else False)
    programs = [override]
    if isinstance(command, (str, bytes)):
        line = os.fsdecode(command)
    elif isinstance(command, (list, tuple)) and command:
        programs.append(command[0])
        line = os.fsdecode(command[0]) if shell else None
    else:
        programs.append(command)
        line = None
    if line is not None:
        lexer = shlex.shlex(line, posix=False, punctuation_chars=";&|()<>`")
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            programs.extend(lexer)
        except ValueError as exc:
            raise AssertionError("host-only test has an unparseable process command") from exc
    for program in programs:
        if isinstance(program, (str, bytes, os.PathLike)):
            name = ntpath.basename(os.fsdecode(program).strip("\"'")).casefold().removesuffix(".exe")
            if name in DEBUG_EXECUTABLES:
                return True
    return False


def forbid_debugger_processes(monkeypatch) -> None:
    lower = subprocess.Popen

    def checked_process(command, *extra, **options):
        if debug_process_requested(command, extra, options):
            raise AssertionError("host-only tests cannot start a debugger or flasher")
        return lower(command, *extra, **options)

    monkeypatch.setattr(subprocess, "Popen", checked_process)


def forbid_hardware(monkeypatch) -> None:
    import serial
    from serial.tools import list_ports

    import stpg_runner.accessories as accessories
    import stpg_runner.external_accessory as external
    import stpg_runner.controller as controller
    import stpg_runner.device as device
    import stpg_runner.execution as execution

    def forbidden(*args, **kwargs):
        raise AssertionError("host-only test attempted physical contact")

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(list_ports, "comports", lambda *args, **kwargs: [])
    for module in (execution, device):
        monkeypatch.setattr(module, "open_serial", forbidden)
        monkeypatch.setattr(module, "enumerate_ports", forbidden)
    for module in (accessories, external):  # STPG port checks before the bundle check
        monkeypatch.setattr(module, "enumerate_ports", forbidden)
    monkeypatch.setattr(controller, "open_controller_serial", forbidden)
    monkeypatch.setattr(controller, "enumerate_ports", forbidden)
    forbid_debugger_processes(monkeypatch)


def plug_in_bound_stpgs(service) -> None:
    """Host-only port list for the GUI's early STPG port check: every stored STPG binding is listed as bound.

    Only this list is a stand-in; serial opens and the SDK's own enumeration stay forbidden.
    """

    from types import SimpleNamespace

    def ports():
        return [SimpleNamespace(device=item["config"]["port"], vid=item["config"]["vid"], pid=item["config"]["pid"],
                                serial_number=item["config"]["serial_number"])
                for item in service.store.list("devices") if "config" in item]

    service.port_provider = ports
