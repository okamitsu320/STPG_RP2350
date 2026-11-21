"""GUI adapter acceptance of the SDK automatic-segmentation contract (host only)."""
from pathlib import Path

import pytest

from stpg_runner.artifacts import RunnerError
from stpgview.runner import service
from test_stpgview_runner_inputs import (ate, svc, request, _long_raw, _wait)  # fixtures
from stpg_hardware_guard import forbid_hardware


@pytest.fixture(autouse=True)
def no_contact(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.mark.parametrize("cap", [None, "100000"])
def test_auto_mode_builds_segments_without_segment_count(svc, tmp_path, cap):
    source = _long_raw(tmp_path, 8000)
    info = svc.init_project(request(tmp_path / "auto", stil=str(source), compile_mode="raw-event",
                                    loop_policy="expand", segmentation="auto", max_segment_vectors=cap))
    assert info["settings"]["segmentation"] == {"mode": "auto", "max_vectors": int(cap) if cap else None}
    result = _wait(svc.start_build(info["project_path"]))
    assert result["status"] == "done", result["error"]
    bundle = svc.open_bundle(result["result"]["bundle_dir"])
    assert bundle["package_kind"] == "segmented"
    assert len(bundle["patterns"]) == result["result"]["segment_count"] == 2
    assert sum(p["expected"]["events"] for p in bundle["patterns"]) == 12000
    assert sum(p["expected"]["samples"] for p in bundle["patterns"]) == 4000


@pytest.mark.parametrize("role", ["project", "pattern"])
def test_ate_source_change_rejects_before_project_reservation(svc, tmp_path, ate, monkeypatch, role):
    project, pattern = ate
    original = service.preprocess_ate_input
    def changes_source(*args, **kwargs):
        result = original(*args, **kwargs)
        changed = project if role == "project" else pattern
        changed.write_text(changed.read_text() + "\n// concurrent source edit\n", encoding="utf-8")
        return result
    monkeypatch.setattr(service, "preprocess_ate_input", changes_source)
    target = tmp_path / "never"
    with pytest.raises(RunnerError, match="source changed during preprocessing"):
        svc.init_project(request(target, input_form="ate-pair", stil=str(pattern), ate_project=str(project)))
    assert not target.exists()


def test_invalid_timing_is_not_a_segmentation_hint(svc, tmp_path):
    source = _long_raw(tmp_path, 10)
    source.write_text(source.read_text().replace("100ns Compare", "500ns Compare"), encoding="utf-8")
    info = svc.init_project(request(tmp_path / "invalid", stil=str(source), segmentation="auto"))
    failed = _wait(svc.start_build(info["project_path"]))
    assert failed["status"] == "failed"
    assert "source analysis is REJECT" in failed["error"]
    assert "does not fit in one package" not in failed["error"]
    assert "Segmentation = auto" not in failed["error"]
