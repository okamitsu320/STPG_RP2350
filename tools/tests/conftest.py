"""Portable product test isolation: no real serial, USB enumeration or debugger.

Historical environment-dependent skip conversion is deliberately not included.
An unexpected failure in this public-fixture suite is a failure, not a skip.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from stpg_hardware_guard import forbid_debugger_processes


@pytest.fixture(autouse=True)
def no_physical_contact(monkeypatch):
    import serial
    from serial.tools import list_ports
    import stpg_runner.device as device
    import stpg_runner.execution as execution
    import stpg_runner.accessories as accessories
    import stpg_runner.external_accessory as external
    import stpg_runner.controller as controller

    def forbidden(*args, **kwargs):
        raise AssertionError("portable tests attempted physical device I/O")

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(list_ports, "comports", forbidden)
    for module in (device, execution, accessories, external, controller):
        if hasattr(module, "enumerate_ports"):
            monkeypatch.setattr(module, "enumerate_ports", forbidden)
    # Leave the STPG opener callable so its settings can be tested with a fake
    # serial constructor. The actual constructor remains forbidden above.
    for module, name in ((controller, "open_controller_serial"),):
        monkeypatch.setattr(module, name, forbidden)

    forbid_debugger_processes(monkeypatch)


@pytest.fixture
def xiao_test_source():
    import hashlib
    path = Path(__file__).parent / "fixtures" / "xiao_test_source.ino"
    return path, hashlib.sha256(path.read_bytes()).hexdigest()
