"""Real-browser checks of R9: the Custom chunk-wait length in New / Edit / Copy, refused wait plans, the controller's
identification lines (shown as text) and the STPG that kept an upload without RUN.

Public synthetic patterns, the simulated STPG and the simulated GPIO controller only; no real port is opened and USB
is not enumerated.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from stpg import constants as c
from stpg_runner import build_project, init_project
from stpg_runner.artifacts import identity, read_json, write_json
from stpg_runner.build import BUNDLE_NAME
from stpgview.runner import service as service_module
from stpgview.runner.simulated import SimulatedController, SimulatedStpg

from test_stpg_runner_wait_capacity import source_text
from test_stpgview_browser import (RUNNER_STIL, Page, Select, By, _add_action, _chosen, _confirm_and_start,  # noqa: F401
                                   _create, _new_project_dialog, _set, _wait_run, browser, counter, page_factory)

pytestmark = pytest.mark.stpg_host_only
MARKUP = "<img src=x onerror=window.__pwned=1> other-sketch v0"


def _note(page: Page) -> str:
    return page.d.find_element(By.CSS_SELECTOR, "#rpChunkWait .hint").text


def _dialog_wait(body) -> tuple[str, str, bool]:
    box = body.find_element(By.ID, "rpChunkWait")
    return _chosen(body, "wait_preset"), body.find_element(By.NAME, "chunk_wait_ms").get_attribute("value"), box.is_displayed()


def _save(page: Page) -> dict:
    page.d.find_element(By.ID, "formDialogOk").click()
    page.until(lambda: not page.dialog_open("formDialog") or page.d.find_element(By.ID, "formDialogError").is_displayed(),
               what="save finished")
    assert not page.dialog_open("formDialog"), page.text("formDialogError")
    return page.js("return window.__R.project")


def test_custom_length_is_typed_exactly_kept_by_edit_and_copy_and_dropped_by_another_preset(page_factory, tmp_path):
    page = page_factory()
    body = _new_project_dialog(page)
    wait = Select(body.find_element(By.NAME, "wait_preset"))
    assert [o.get_attribute("value") for o in wait.options][1:] == ["none", "chunk-4ms", "chunk-12ms", "chunk-custom"]
    assert not body.find_element(By.ID, "rpChunkWait").is_displayed()
    assert "Edit → Insert wait" in page.text("rpWaitAtVector") and "×" in page.text("rpWaitAtVector")
    Select(body.find_element(By.NAME, "input_form")).select_by_value("stil")
    _set(body, "stil", str(RUNNER_STIL))
    _set(body, "directory", str(tmp_path / "custom"))
    Select(body.find_element(By.NAME, "completion")).select_by_value("hold")
    wait.select_by_value("chunk-custom")
    assert body.find_element(By.ID, "rpChunkWait").is_displayed()
    where = page.text("rpWaitWhere")
    assert "not at the first boundary of a segment" in where and "not at every vector or every chunk" in where
    assert "no zero-wait fallback" in where
    # the service suggestion is the CUI example, not the wait length
    assert body.find_element(By.NAME, "service_us").get_attribute("value") == "4000"
    for typed, expected in (("6.5001", "Not accepted: finer than 1 µs"), ("6,5", "Not accepted: type a positive"),
                            ("21474.837", "more than 21474.836 ms"), ("1.005", "= 1,005 µs."), ("6.5", "= 6,500 µs.")):
        _set(body, "chunk_wait_ms", typed)
        assert expected in _note(page), (typed, _note(page))
    assert "not the service budget" in _note(page)
    _set(body, "chunk_wait_ms", "6.5001")
    page.d.find_element(By.ID, "formDialogOk").click()
    page.until(lambda: page.d.find_element(By.ID, "formDialogError").is_displayed(), what="refused length")
    assert "finer than 1 µs" in page.text("formDialogError") and not (tmp_path / "custom").exists()
    _set(body, "chunk_wait_ms", "6.5")
    project = _create(page)
    assert project["settings"]["wait"]["preset"] == "chunk-custom" and project["settings"]["wait"]["chunk_wait_us"] == 6500
    assert "6500 (6.5 ms)" in page.text("rpProjectBody")
    page.shot("custom-wait-project")

    page.d.find_element(By.ID, "rpEditSettings").click()
    page.until(lambda: page.dialog_open("formDialog"), what="edit dialog")
    body = page.d.find_element(By.ID, "formDialogBody")
    assert _dialog_wait(body) == ("chunk-custom", "6.5", True)
    Select(body.find_element(By.NAME, "wait_preset")).select_by_value("chunk-4ms")
    assert not body.find_element(By.ID, "rpChunkWait").is_displayed()
    project = _save(page)
    assert project["settings"]["wait"]["preset"] == "chunk-4ms" and "chunk_wait_us" not in project["settings"]["wait"]
    assert "chunk_wait_us" not in read_json(Path(project["project_path"]))["wait"]

    page.d.find_element(By.ID, "rpEditSettings").click()
    page.until(lambda: page.dialog_open("formDialog"), what="edit dialog")
    body = page.d.find_element(By.ID, "formDialogBody")
    assert _dialog_wait(body) == ("chunk-4ms", "", False)
    Select(body.find_element(By.NAME, "wait_preset")).select_by_value("chunk-custom")
    page.d.find_element(By.ID, "formDialogOk").click()
    page.until(lambda: page.d.find_element(By.ID, "formDialogError").is_displayed(), what="missing length")
    assert "Type the Custom chunk wait length" in page.text("formDialogError")
    _set(body, "chunk_wait_ms", "0.001")
    project = _save(page)
    assert project["settings"]["wait"]["chunk_wait_us"] == 1

    page.button("Copy to a new project…", page.d.find_element(By.ID, "rpProjectBody")).click()
    page.until(lambda: page.dialog_open("formDialog"), what="copy dialog")
    assert _dialog_wait(page.d.find_element(By.ID, "formDialogBody")) == ("chunk-custom", "0.001", True)
    assert page.errors() == []


def test_a_refused_wait_plan_says_nothing_was_adjusted(page_factory, tmp_path):
    source = tmp_path / "public.stil"
    source.write_text(source_text(6000))
    project = init_project(source, tmp_path / "project", wait_preset="chunk-custom", chunk_wait_us=6500,
                           completion="hold", service_us=50, service_source="public declared budget, not measured",
                           service_scope="non-loop", max_total_wait_us=100)
    before = project.read_bytes()
    page = page_factory()
    page.inject(f"window.__opened = false; openProject({json.dumps(str(project))}).then(() => {{ window.__opened = true; }});")
    page.until(lambda: page.js("return window.__opened === true"), what="project open")
    page.d.find_element(By.ID, "rpBuild").click()
    page.until(lambda: page.js("const e = window.__R.jobs.build; return e && e.job.status !== 'running'"), timeout=120,
               what="build")
    page.until(lambda: page.d.find_elements(By.ID, "rpWaitPlanRejected"), what="wait plan notice")
    text = page.text("rpWaitPlanRejected")
    assert "never used: there is no zero-wait fallback" in text and "Nothing was changed for you" in text
    page.js("document.getElementById('rpWaitPlanRejected').scrollIntoView()")
    page.shot("wait-plan-rejected")
    assert project.read_bytes() == before
    assert page.errors() == []


def test_a_bundle_with_a_rejected_saved_plan_offers_no_update(page_factory, tmp_path):
    result = build_project(init_project(RUNNER_STIL, tmp_path / "project", wait_preset="chunk-4ms", completion="hold",
                                        service_us=50, service_source="declared", service_scope="non-loop",
                                        max_total_wait_us=1_000_000))
    proposal_path = result.bundle_dir / "wait.proposal.json"
    proposal = read_json(proposal_path)
    proposal["wait_plan"].update(status="rejected", reason="public synthetic rejected partial proposal")
    write_json(proposal_path, proposal)
    manifest_path = result.bundle_dir / BUNDLE_NAME
    manifest = read_json(manifest_path)
    manifest["files"][proposal_path.name] = identity(proposal_path)  # only to reach the plan check
    write_json(manifest_path, manifest)
    page = page_factory()
    page.open_bundle(SimpleNamespace(bundle_dir=result.bundle_dir, manifest_sha256=identity(manifest_path)["sha256"]))
    text = page.text("rpBundleWaitPlan")
    assert "Update for this SDK cannot repair it" in text and "public synthetic rejected partial proposal" in text
    assert page.d.find_elements(By.ID, "rpRebind") == []
    assert page.errors() == []


class _OtherSketch(SimulatedController):
    def _command(self, text):
        return MARKUP if text.startswith("i ") else super()._command(text)


def test_controller_lines_are_text_and_the_stpg_kept_the_upload(page_factory, counter, monkeypatch):
    monkeypatch.setattr(service_module, "SimulatedController", _OtherSketch)
    page = page_factory()
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    page.choose("#rpRunBody select[name=run_controller]", "simulated")
    _add_action(page, 2, "Hi-Z")
    page.d.find_element(By.ID, "rpRun").click()
    _confirm_and_start(page, double=False)
    result = _wait_run(page)
    assert result["control"]["identity_lines"][-1] == {"line": MARKUP, "disposition": "REJECT"}
    lines = page.text("rpControlLines")
    assert MARKUP in lines and "not an accepted identification" in lines and "not why" in lines
    assert "not added to STIL details" in lines
    assert page.js("return document.querySelectorAll('#rpRunBody img').length") == 0
    assert page.js("return window.__pwned === undefined")
    kept = page.text("rpLeftLoaded")
    assert "accepted the upload of segment 1 of 1 (UPLOAD_END → OK · READY)" in kept
    assert "the GPIO controller step before RUN did not complete" in kept
    assert "the next run is refused at its start check" in kept and "does not reset the STPG" in kept
    assert "not a live device check" in kept and "current state or pin levels" in kept
    assert "no subsequent STPG command is recorded" in kept and "no RUN was sent" not in kept
    page.js("document.getElementById('rpLeftLoaded').scrollIntoView()")
    page.shot("controller-lines-and-kept-upload")
    assert page.errors() == []


class _ReadyStpg(SimulatedStpg):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last = replace(self.last, state=c.STATE_READY)


def test_a_start_check_refusal_lists_no_controller_lines_and_names_the_earlier_stop(page_factory, counter, monkeypatch):
    monkeypatch.setattr(service_module, "SimulatedStpg", _ReadyStpg)
    page = page_factory()
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    page.choose("#rpRunBody select[name=run_controller]", "simulated")
    _add_action(page, 2, "Hi-Z")
    page.d.find_element(By.ID, "rpRun").click()
    _confirm_and_start(page, double=False)
    result = _wait_run(page)
    assert result["start_check"]["state"] == "READY" and result["left_loaded"] is None
    text = page.text("rpRunBody")
    assert "An earlier run that stopped after its upload and before RUN" in text
    assert "Not started: the STPG refused the run at its start check" in text
    assert page.d.find_elements(By.ID, "rpControlLines") == [] and page.d.find_elements(By.ID, "rpLeftLoaded") == []
    # a record of an earlier version (no identification lines) says so instead of showing an empty list
    page.js("const v = gpioResultView({outcome: 'REJECT', open_attempted: true, identity_lines: [], errors: []}, {runAttached: true});"
            "v.id = 'oldRecord'; document.getElementById('rpRunBody').append(v);")
    assert "No received-line diagnostics in this record" in page.text("oldRecord")
    assert page.errors() == []
