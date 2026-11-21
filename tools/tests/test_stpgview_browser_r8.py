"""Real-browser checks of the R8 GUI: the fail-log read (confirmation, live view, result), STIL details and text,
fail marks in the viewer, and the explicit update of a bundle bound to another SDK.

Public synthetic patterns and the simulated STPG only; no real port is opened and USB is not enumerated.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from stpg import constants as c
from stpg_runner import verify_bundle
from stpg_runner.artifacts import canonical_json, identity, read_json, write_json
from stpg_runner.build import BUNDLE_NAME
from stpgview.runner import service as service_module
from stpgview.runner.simulated import SimulatedStpg

from test_stpgview_browser import (Page, _confirm_and_start, _live, _wait_run, browser, counter,  # noqa: F401
                                   page_factory, By, Select)
from stpg_public_patterns import build_public as _build, counter_stil

pytestmark = pytest.mark.stpg_host_only


class _ThreeFails(SimulatedStpg):
    """The simulated STPG reporting three compare failures (and keeping their fail-log entries)."""

    def __init__(self, **options):
        super().__init__(fails=3, **options)


class _SilentAfterFirstPage(_ThreeFails):
    """Answers the first READ_FAIL_LOG page; the second gets no reply, so the SDK's 2 s receive window ends."""

    pages = 0

    def write(self, data):
        size = super().write(data)
        if self.commands and self.commands[-1] == c.CMD_READ_FAIL_LOG:
            self.pages += 1
            if self.pages == 2:
                self.rx.clear()
        return size


@pytest.fixture(scope="module")
def segmented(tmp_path_factory):
    return _build(tmp_path_factory, "browser-r8-segmented", counter_stil(1600), max_segment_vectors=800)


def _start(page: Page, built) -> None:
    page.open_bundle(built)
    page.choose("#rpRunBody select[name=device]", "simulated")
    page.d.find_element(By.ID, "rpRun").click()
    page.until(lambda: page.dialog_open("confirmRunDialog"), what="run confirmation")


def _expose_viewer(page: Page) -> None:
    page.inject("window.__S = S;")


# ---- confirmation ------------------------------------------------------------------------------------

def test_the_confirmation_names_the_fail_log_read_and_what_it_holds(page_factory, counter, segmented):
    page = page_factory()
    _start(page, counter)
    text = page.text("confirmFailLog")
    assert "up to 341 entries in up to 171 requests of 2 entries" in text
    assert "up to about 1026 s per segment" in text and "not a measured time" in text
    assert "No segment of this plan waits in HOLD" in text and "completion=hold" in text
    assert "no retry, no reset" in text and "The compare result stays" in text
    acks = page.text("confirmRunAcks")
    assert "reads that segment's retained fail log" in acks and "summary-only" not in acks
    assert "retained fail log (up to 341 entries)" in page.text("confirmRunBody")
    page.shot("r8-confirm-fail-log")
    page.d.find_element(By.CSS_SELECTOR, "#confirmRunForm button[value=cancel]").click()
    page.until(lambda: not page.dialog_open("confirmRunDialog"), what="confirmation closed")
    _start(page, segmented)
    text = page.text("confirmFailLog")
    assert "One segment waits in HOLD for the next one" in text and "stay driven that much longer" in text
    assert page.errors() == []


# ---- live view ----------------------------------------------------------------------------------------

def test_the_live_view_tells_the_fail_log_read_from_run_progress(page_factory):
    page = page_factory()
    base = [{"stage": "run_verify", "event": "start", "segments": 2}, {"stage": "run_verify", "event": "done"},
            {"stage": "upload", "event": "start", "completed": 0, "total": 4096},
            {"stage": "upload", "event": "done", "completed": 4096, "total": 4096, "detail": "segment=0; UPLOAD_END accepted"},
            {"stage": "run", "event": "accepted", "detail": "segment=0; RUN accepted"},
            {"stage": "observe", "event": "progress", "completed": 50, "total": 50, "detail": "segment=0; poll=2"}]
    texts = page.js("""
      const base = arguments[0];
      const view = (events, extra) => { const v = runProgressView(Object.assign({ events, started: performance.now(), failLog: { response_timeout_s: 2 } }, extra || {}));
        return [v.querySelector('#rpRunPhase').textContent, v.querySelector('#rpRunLive').textContent]; };
      const ev = (event, completed, extra) => Object.assign({ stage: "fail_log", event, completed, total: 3, detail: "segment=0; terminal mismatch" }, extra || {});
      return [view(base.concat([ev("start", 0)])), view(base.concat([ev("progress", 2)]), { holdSegments: [0] }),
              view(base.concat([ev("done", 3)])), view(base.concat([ev("progress", 2, { job_s: -60 })]))];""", base)
    start, held, done, quiet = texts
    assert "ended with compare failures" in start[0] and "Reading its retained fail log: 0 / 3 entries" in start[0]
    assert "not RUN or waveform progress" in start[0] and "up to 2 s; nothing is retried" in start[0]
    assert "2 / 3 entries received and checked" in held[0] and "ended in HOLD" in held[0]
    assert "ended in HOLD" not in start[0]
    assert "Its retained fail log was received: 3 / 3 entries." in done[0] and "retried" not in done[0]
    assert "no fail-log page for a while" in quiet[1] and "information, not a timeout" in quiet[1]
    assert page.errors() == []


# ---- a mismatching run: result, STIL details, text, viewer marks ---------------------------------------

def test_a_mismatch_shows_its_fail_log_stil_details_and_marks_in_the_viewer(page_factory, counter, monkeypatch, tmp_path):
    monkeypatch.setattr(service_module, "SimulatedStpg", _ThreeFails)
    page = page_factory()
    _start(page, counter)
    _confirm_and_start(page, double=False)
    result = _wait_run(page)
    assert result["run"]["comparison"] == "MISMATCH" and result["run"]["outcome"] == "COMPLETED"
    body = page.text("rpRunBody")
    assert "retained fail log (segment 1): 3 / 3 entries received" in body
    assert "3 / 3 read" in body  # the result table's fail-log column
    page.click_css("#rpRunBody button[data-describe]")
    page.until(lambda: "STIL details" in _live(page, "rpDescription") and page.d.find_elements(By.ID, "rpFailList"),
               what="STIL details", timeout=60)
    details = page.text("rpDescription")
    assert "confidential" in details and "no port, USB serial or folder" in details
    rows = page.d.find_elements(By.CSS_SELECTOR, "#rpFailList tbody tr")
    assert len(rows) == 3 and all("expected" in row.text and "got" in row.text for row in rows)
    assert str(tmp_path) not in details
    page.shot("r8-stil-details")
    # Save as text: a new .txt file, the server's own text
    page.click("rpSaveText")
    page.until(lambda: page.dialog_open("formDialog"), what="save dialog")
    target = tmp_path / "saved" / "details.txt"
    box = page.d.find_element(By.CSS_SELECTOR, "#formDialogBody input[name=path]")
    box.clear()
    box.send_keys(str(target))
    page.d.find_element(By.ID, "formDialogOk").click()
    page.until(lambda: target.is_file(), what="saved text")
    described = page.svc.describe_run({"result": result["result_dir"]})
    assert target.read_text() == described["text"]
    # the fails on the bundle's STIL copy in the viewer
    page.click("rpShowFails")
    _expose_viewer(page)
    page.until(lambda: page.js("return window.__S.marks && window.__S.marks.marks.length") == 3, what="fail marks")
    assert "3 of 3 marked" in page.text("failsSummary")
    page.click("failsNext")
    page.until(lambda: page.js("return window.__S.markAt") == 0, what="first fail")
    mark = page.js("return window.__S.marks.marks[0]")
    page.until(lambda: page.js("return window.__S.sel") == mark["vector"], what="vector selected")
    assert "1 / 3" in page.text("failsPos") and "segment 0 · fail" in page.text("failsCurrent")
    page.until(lambda: page.d.find_elements(By.CSS_SELECTOR, f"#tableRows .trow.fail[data-i='{mark['vector']}']"),
               what="table row marked")
    page.shot("r8-viewer-fails")
    # a mark whose line differs is listed, never moved
    trace = page.js("return window.__S.activeId")
    page.js("""return post('/api/marks', { trace: arguments[0], title: 't', marks: [Object.assign({}, arguments[1], { line: arguments[1].line + 1, text: 'shifted' })] })
               .then(() => refreshState(arguments[0]))""", trace, {"vector": mark["vector"], "line": mark["line"], "signals": []})
    page.until(lambda: "0 of 1 marked, 1 not placed" in page.text("failsSummary"), what="unplaced listed")
    assert "not the same STIL occurrence" in page.js("return document.getElementById('failsUnplaced').textContent")
    assert page.errors() == []


def test_a_stopped_fail_log_read_is_explained_and_only_that_row_fails(page_factory, counter, monkeypatch):
    monkeypatch.setattr(service_module, "SimulatedStpg", _SilentAfterFirstPage)
    page = page_factory()
    _start(page, counter)
    _confirm_and_start(page, double=False)
    result = _wait_run(page)
    assert result["run"]["outcome"] == "REJECT" and result["run"]["comparison"] == "MISMATCH"
    body = page.text("rpRunBody")
    assert "Reading the retained fail log stopped (from the result file)" in body
    assert "the comparison result MISMATCH stands" in body and "after 2 of 3 entries: READ_FAIL_LOG, no reply" in body
    assert "no further segment, no release, no retry, no reset" in body
    assert "GET_STATUS after RUN" not in body and "FAIL_LOG page data" in body
    failed = page.d.find_elements(By.CSS_SELECTOR, "#rpRunBody ul.stages li.failed")
    assert [li.text for li in failed] and all("retained fail log" in li.text for li in failed)
    assert "stopped at 2 / 3" in body
    page.shot("r8-fail-log-stopped")
    assert page.errors() == []


# ---- a bundle bound to another SDK ----------------------------------------------------------------------

def _older(bundle_dir: Path) -> tuple[Path, str]:
    root = bundle_dir.parent / "older-sdk-copy"
    shutil.copytree(bundle_dir, root)
    manifest = read_json(root / BUNDLE_NAME)
    manifest["binding"]["tool_sha256"] = "0" * 64
    manifest["binding"]["python"] = "3.10.0"
    manifest["cache_key"] = hashlib.sha256(canonical_json(manifest["binding"])).hexdigest()
    write_json(root / "build.state.json", {"status": "HOST_ONLY_VERIFIED", "cache_key": manifest["cache_key"]})
    manifest["files"]["build.state.json"] = identity(root / "build.state.json")
    write_json(root / BUNDLE_NAME, manifest)
    assert verify_bundle(root)["status"] == "PASS"
    return root, identity(root / BUNDLE_NAME)["sha256"]


def test_a_bundle_of_another_sdk_is_updated_on_request_and_says_so(page_factory, tmp_path_factory):
    built = _build(tmp_path_factory, "browser-r8-rebind", counter_stil(64))
    old, pin = _older(built.bundle_dir)
    before = sorted((p.relative_to(old).as_posix(), identity(p)["sha256"]) for p in old.rglob("*") if p.is_file())
    page = page_factory()
    page.inject(f"window.__open = false; openBundle({json.dumps(str(old))}, {json.dumps(pin)}).then(() => {{ window.__open = true; }});")
    page.until(lambda: page.js("return window.__open === true"), what="old bundle open")
    assert "Run refuses this bundle" in page.text("rpSdkDiffers")
    page.click("rpRebind")
    page.until(lambda: page.dialog_open("formDialog"), what="update dialog")
    dialog = page.text("formDialogBody")
    assert "Compiled with an older SDK; later compiler fixes are not included." in dialog
    assert "Nothing is compiled" in dialog and "This bundle stays as it is" in dialog
    page.shot("r8-rebind-dialog")
    page.d.find_element(By.ID, "formDialogOk").click()
    page.until(lambda: page.js("return window.__R.bundle && window.__R.bundle.rebound !== null && window.__R.bundle.bundle_dir !== arguments[0]", str(old)),
               what="updated bundle open", timeout=60)
    assert "later compiler fixes are not included" in page.text("rpRebound")
    assert not page.d.find_elements(By.ID, "rpSdkDiffers")
    assert "a new bundle beside the old one (unchanged)" in page.text("rpBuildBody")
    after = sorted((p.relative_to(old).as_posix(), identity(p)["sha256"]) for p in old.rglob("*") if p.is_file())
    assert after == before
    assert page.errors() == []
