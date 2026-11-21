"""GUI adapter / SDK parity with public synthetic off-grid STIL; no hardware."""

from pathlib import Path

import pytest

from stpg_runner.artifacts import RunnerError, read_json
from stpgview.runner.service import RunnerService
from stpgview.session import Session
from stpg_hardware_guard import forbid_hardware
from test_stpgview_runner_inputs import RUNNER_STIL, request, _wait

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def no_contact(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture
def svc(tmp_path):
    return RunnerService(config_dir=tmp_path / "cfg", device_run=False, demo=False,
                         results_dir=tmp_path / "runs", session=Session(tmp_path / "work"))


@pytest.mark.parametrize("approx", [True, "true"])
def test_failed_exact_project_can_explicitly_opt_in_and_retains_original_attempt(svc, tmp_path, approx):
    prefix = RUNNER_STIL.read_text().split("Pattern MAIN")[0].replace("100ns", "103ns")
    source = tmp_path / "public.stil"
    source.write_text(prefix + "Pattern MAIN {\nW NORMAL;\nLoop 3 {\nV { ALL = 01; }\nV { ALL = 1X; }\n}\n}\n")
    info = svc.init_project(request(tmp_path / "p", stil=str(source), segmentation="auto"))
    path = Path(info["project_path"])
    assert read_json(path)["compile"]["allow_approx"] is False
    failed = _wait(svc.start_build(str(path)))
    assert failed["status"] == "failed" and "ERR_APPROXIMATION_REQUIRED" in failed["error"]
    opened = svc.open_project(str(path))
    options = request(path.parent, allow_approx=approx, segmentation="auto")
    options.update(project=str(path), project_sha256=opened["project_sha256"])
    edited = svc.edit_project(options)
    assert edited["settings"]["compile"]["allow_approx"] is True
    assert edited["reservations"][0]["settings"]["allow_approx"] is False
    done = _wait(svc.start_build(str(path)))
    assert done["status"] == "done", done["error"]
    assert (path.parent / "inputs/pattern.stil").read_bytes() == source.read_bytes()
    copied = svc.init_project(request(tmp_path / "copy", stil=str(source), copy_of=str(path),
                                     segmentation="auto", allow_approx=True))
    assert copied["settings"]["compile"]["allow_approx"] is True


@pytest.mark.parametrize("value", [None, 0, 1, "yes", "False", "", [], {}])
def test_invalid_timing_choice_rejects_before_project_reservation(svc, tmp_path, value):
    with pytest.raises(RunnerError, match="allow_approx"):
        svc.init_project(request(tmp_path / "p", allow_approx=value))
    assert not (tmp_path / "p").exists()
