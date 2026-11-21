"""The GUI shows how the SDK checked a bundle (host only, no device contact).

Since SDK 8f698155 the SDK keeps a process receipt after one full verification of a bundle's bytes (a build, Open
or run) and later re-hashes every file against it. Open asks for that reuse (``verify_bundle(reuse=True)``), Run…
gets it from ``prepare_run``; the GUI reports the SDK's ``check_mode`` / ``RunPlan.verification_mode`` as given and
keeps no verdict of its own.
"""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

import stpg_runner.build as sdk_build
from stpg_runner.artifacts import RunnerError
from stpgview.runner import service as service_module

from stpg_hardware_guard import forbid_hardware
from test_stpgview_runner import ACKS, built, svc, wait  # noqa: F401  (svc: demo service fixture)
from test_stpgview_runner_acceptance import segmented  # noqa: F401  (module fixture: 3200 vectors, 2 segments)

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def _cold(monkeypatch):
    forbid_hardware(monkeypatch)
    sdk_build._clear_verification_receipts()  # receipts are per process: each test starts without one
    yield
    sdk_build._clear_verification_receipts()


def request(bundle, sha):
    return {"bundle": str(bundle), "manifest_sha256": sha, "device": "simulated"}


def test_open_after_a_gui_build_reuses_the_builds_verification(svc, tmp_path):  # noqa: F811
    b = built(svc, tmp_path)
    assert b["bundle"]["verification"]["status"] == "PASS"
    assert b["bundle"]["check"]["mode"] == "process_receipt"  # the build verified these bytes in this process


def test_open_verifies_once_then_rehashes(svc, segmented):  # noqa: F811
    first = svc.open_bundle(str(segmented.bundle_dir), segmented.manifest_sha256)
    second = svc.open_bundle(str(segmented.bundle_dir), segmented.manifest_sha256)
    assert first["check"]["mode"] == "semantic" and second["check"]["mode"] == "process_receipt"
    assert first["verification"]["status"] == second["verification"]["status"] == "PASS"
    assert first["verification"]["cache_key"] == second["verification"]["cache_key"]


def test_run_reports_the_sdks_check_and_a_reused_check_still_runs(svc, segmented):  # noqa: F811
    first = svc.prepare_run(request(segmented.bundle_dir, segmented.manifest_sha256))
    second = svc.prepare_run(request(segmented.bundle_dir, segmented.manifest_sha256))
    assert first["summary"]["bundle_check"]["mode"] == "semantic"
    assert second["summary"]["bundle_check"]["mode"] == "process_receipt"
    same = ("segments", "runtime_profile", "storage", "completion", "observation", "manifest_sha256")
    assert {k: second["summary"][k] for k in same} == {k: first["summary"][k] for k in same}
    result = wait(svc.start_run({"nonce": second["nonce"], "acknowledgements": ACKS}))["result"]
    assert result["run"]["outcome"] == "COMPLETED"


def _flip_byte(root: Path) -> None:
    target = next(root.rglob("pattern.bin"))
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))


def _remove_listed(root: Path) -> None:
    next(root.rglob("pattern.metadata.json")).unlink()


def _add_unlisted(root: Path) -> None:
    (root / "unlisted.txt").write_text("x", encoding="utf-8")


@pytest.mark.parametrize("change", [_flip_byte, _remove_listed, _add_unlisted])
def test_a_changed_bundle_is_not_accepted_on_the_receipt(svc, segmented, tmp_path, change):  # noqa: F811
    copy = tmp_path / "copy"
    shutil.copytree(segmented.bundle_dir, copy)
    sha = segmented.manifest_sha256
    assert svc.open_bundle(str(copy), sha)["check"]["mode"] == "semantic"
    change(copy)
    reopened = svc.open_bundle(str(copy), sha)
    assert reopened["verification"]["status"] == "REJECT" and reopened["check"]["mode"] != "process_receipt"
    with pytest.raises(RunnerError):
        svc.prepare_run(request(copy, sha))


def test_the_bundle_card_names_the_psram_pattern_region(svc, segmented):  # noqa: F811
    info = svc.open_bundle(str(segmented.bundle_dir), segmented.manifest_sha256)
    assert info["settings"]["execution"]["storage"] == "psram"
    assert info["psram_pattern_bytes"] == service_module.get_runtime_capacity_profile(
        info["settings"]["target"]["runtime_profile"]).psram_pattern_bytes


def test_a_capacity_first_rejection_is_not_answered_with_a_cap_retry(svc, tmp_path, monkeypatch):  # noqa: F811
    # The SDK recognizes the wrapped and the wide encoder capacity errors. A capacity-first
    # rejection that remains is another failure (parser, timing, I/O): the GUI keeps the SDK's text and does not
    # suggest an explicit Max vectors / segment as a retry.
    from stil.segmented_package import SegmentedPackageError
    from test_stpgview_runner import gui_project

    def rejected(*args, **kwargs):
        raise SegmentedPackageError("capacity-first probe rejected at 0..299999: preserved loop could not be "
                                    "compiled by the rate-vector backend; refusing raw-event fallback")

    monkeypatch.setattr(service_module, "build_project", rejected)
    job = wait(svc.start_build(gui_project(svc, tmp_path)["project_path"]))
    assert job["status"] == "failed" and "capacity-first probe rejected at 0..299999" in job["error"]
    assert "Max vectors" not in job["error"] and "known problem" not in job["error"]
    assert not hasattr(service_module, "capacity_first_hint")
