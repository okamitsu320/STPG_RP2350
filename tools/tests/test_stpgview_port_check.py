"""The GUI's STPG port check before the bundle verification (host only, synthetic USB metadata).

Before SDK e5e97ca0 the SDK checked the port only after it had verified the whole bundle; on a large bundle that
was minutes after Start run. The GUI applies the SDK's own rule (``validate_port``) to the USB port list when Run…
is pressed (before its own bundle check) and again at Start run, and names which part differs. The oracle for
accept/refuse is ``validate_port``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from stpg_runner.artifacts import RunnerError
from stpg_runner.device import validate_port
from stpgview.runner import service as service_module
from stpgview.runner.service import ACKNOWLEDGEMENTS, RunnerService, port_mismatch
from stpgview.runner.simulated import SIMULATED_DEVICE
from stpgview.session import Session

from stpg_hardware_guard import forbid_hardware
from test_stpgview_runner_acceptance import segmented  # noqa: F401  (module fixture: 3200 vectors, 2 segments)

pytestmark = pytest.mark.stpg_host_only
ACKS = {key: True for key in ACKNOWLEDGEMENTS}
BOUND = replace(SIMULATED_DEVICE, port="COM7", vid=0x2E8A, pid=0x000A, serial_number="0123456789ABCDEF")


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


def port(device, vid=0x2E8A, pid=0x000A, serial="0123456789ABCDEF"):
    return SimpleNamespace(device=device, vid=vid, pid=pid, serial_number=serial)


def sdk_accepts(device, ports) -> bool:
    try:
        validate_port(device, ports)
        return True
    except RunnerError:
        return False


CASES = {
    "as bound": ([port("COM7"), port("COM3", 0x2886, 0x0045, "XIAO")], None),
    "port gone, identity gone": ([port("COM3", 0x2886, 0x0045, "XIAO")],
                                 "COM7 is not in the USB port list and no port has the binding's USB identity"),
    "port renamed": ([port("COM12")], "the binding's USB identity (2e8a:000a serial 0123456789ABCDEF) is on COM12 now"),
    "other board on the port": ([port("COM7", serial="FEDCBA9876543210")],
                                "COM7 is listed as COM7 (2e8a:000a serial FEDCBA9876543210), but the binding expects"),
    "serial case": ([port("COM7", serial="0123456789abcdef")], "only the letter case of the serial differs"),
    "identity twice": ([port("COM7"), port("COM8")], "is on COM7 and COM8; the SDK refuses an ambiguous identity"),
    "no USB metadata": ([port("COM7", None, None, None)], "COM7 is listed as COM7 (no USB metadata)"),
    "nothing listed": ([], "Ports listed now: none"),
    "an entry without a name": ([port("COM7"), port(None, None, None, None)], "has an entry without a port name"),
}


@pytest.mark.parametrize("case", CASES)
def test_the_check_refuses_exactly_what_the_sdk_refuses_and_names_the_part(case):
    ports, expected = CASES[case]
    text = port_mismatch(BOUND, ports)
    assert (text is None) == sdk_accepts(BOUND, ports)
    if expected is None:
        assert text is None
    else:
        assert expected in text and "Ports listed now:" in text


@pytest.fixture
def svc(tmp_path):
    service = RunnerService(config_dir=tmp_path / "cfg", device_run=True, demo=True, results_dir=tmp_path / "runs",
                            session=Session(tmp_path / "work"))
    service.store.save("devices", "bench", BOUND.to_dict())
    return service


def test_run_click_refuses_a_missing_port_before_the_bundle_verification(svc, segmented, monkeypatch):  # noqa: F811
    verified = []
    original = service_module.prepare_run
    monkeypatch.setattr(service_module, "prepare_run", lambda *a, **k: verified.append(1) or original(*a, **k))
    svc.port_provider = lambda: [port("COM3", 0x2886, 0x0045, "XIAO")]
    request = {"bundle": str(segmented.bundle_dir), "manifest_sha256": segmented.manifest_sha256, "device": "bench"}
    with pytest.raises(RunnerError, match="STPG port check .*COM7 is not in the USB port list.*bundle was not checked"):
        svc.prepare_run(request)
    assert verified == [] and not Path(svc.results_dir).exists()
    svc.port_provider = lambda: [port("COM7")]
    assert svc.prepare_run(request)["summary"]["device"]["port"] == "COM7" and verified == [1]


def test_start_refuses_a_port_that_went_away_after_the_confirmation(svc, segmented, monkeypatch):  # noqa: F811
    called = []
    monkeypatch.setattr(service_module, "run_with_accessories", lambda *a, **k: called.append(k))
    svc.port_provider = lambda: [port("COM7")]
    prep = svc.prepare_run({"bundle": str(segmented.bundle_dir), "manifest_sha256": segmented.manifest_sha256,
                            "device": "bench"})
    svc.port_provider = lambda: [port("COM12")]  # unplugged and back under another name
    with pytest.raises(RunnerError, match="is on COM12 now.*Nothing was started"):
        svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS})
    assert called == [] and not svc.jobs.jobs and not Path(prep["summary"]["output_dir"]).exists()


def test_disabled_device_runs_refuse_before_any_port_list(tmp_path):
    off = RunnerService(config_dir=tmp_path / "cfg", device_run=False, demo=False, results_dir=tmp_path / "r")
    off.store.save("devices", "bench", BOUND.to_dict())
    off.port_provider = lambda: pytest.fail("the port list was read although device runs are disabled")
    with pytest.raises(RunnerError, match="--no-device-run"):
        off.prepare_run({"bundle": str(tmp_path), "manifest_sha256": "0" * 64, "device": "bench"})
