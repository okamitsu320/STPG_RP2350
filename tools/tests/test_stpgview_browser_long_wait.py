"""Real-browser checks of long Rate Vector periods: a GUI Build of mixed narrow/wide segments, and a run refused before
RESET because the STPG lacks the timer-aware stream watchdog.

Public synthetic patterns and the simulated STPG only; no real port is opened and USB is not enumerated.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from stpg_runner import build_project, init_project
from stpgview.runner import service as service_module

from test_stpg_runner_long_wait import write_source
from test_stpgview_long_wait import DeviceTimeout
from test_stpgview_browser import (Page, By, _confirm_and_start, _wait_run, browser, page_factory)  # noqa: F401

pytestmark = pytest.mark.stpg_host_only


def test_a_long_period_builds_without_a_wait_plan_notice(page_factory, tmp_path):
    source = write_source(tmp_path / "synthetic.stil")  # one 10 s WFT period among 400 ns vectors
    project = init_project(source, tmp_path / "project", wait_preset="chunk-4ms", completion="hold", service_us=1,
                           service_source="synthetic budget", service_scope="non-loop", max_total_wait_us=10,
                           max_segment_vectors=6, compile_mode="rate-vector")
    page = page_factory()
    page.inject(f"window.__opened = false; openProject({json.dumps(str(project))}).then(() => {{ window.__opened = true; }});")
    page.until(lambda: page.js("return window.__opened === true"), what="project open")
    page.d.find_element(By.ID, "rpBuild").click()
    page.until(lambda: page.js("const e = window.__R.jobs.build; return e && e.job.status !== 'running'"), timeout=120,
               what="build")
    assert page.js("return window.__R.jobs.build.job.status") == "done"
    assert page.d.find_elements(By.ID, "rpWaitPlanRejected") == []
    page.until(lambda: page.js("return !!(window.__R.bundle && window.__R.bundle.verification)"), what="bundle")
    assert page.js("return window.__R.bundle.verification.status") == "PASS"
    assert page.js("return window.__R.bundle.patterns.length") == 2
    text = page.d.find_element(By.TAG_NAME, "body").text
    # max_total_wait_us 10 is listed among the settings; the 10 s period is never reported against it
    assert "Wait plan rejected" not in text and "exceeds max_total_wait" not in text and "wait plan rejected" not in text
    page.shot("long-period-built")
    assert page.errors() == []


def test_old_firmware_refusal_reads_as_a_check_before_reset(page_factory, tmp_path):
    source = write_source(tmp_path / "synthetic.stil", period="3s")
    built = build_project(init_project(source, tmp_path / "project", wait_preset="none", completion="hold",
                                       max_segment_vectors=6, compile_mode="rate-vector"))
    page = page_factory()
    page.open_bundle(SimpleNamespace(bundle_dir=built.bundle_dir, manifest_sha256=built.manifest_sha256))
    page.choose("#rpRunBody select[name=device]", "simulated")
    page.d.find_element(By.ID, "rpRun").click()
    _confirm_and_start(page, double=False)
    result = _wait_run(page)
    assert result["run"]["outcome"] == "REJECT"
    text = page.text("rpRunBody")
    assert "timer-aware stream watchdog" in text and "not a host response timeout" in text
    assert "0/2" in text
    rows = page.js("return [...document.querySelectorAll('#rpRunBody table')].map(t => t.innerText).join('\\n')")
    for request in ("INFO", "GET_CLOCK_INFO", "GET_STATUS"):
        assert request in rows
    for absent in ("UPLOAD_BEGIN", "UPLOAD_END", "RESET", "\tRUN"):
        assert absent not in rows, absent
    for other in ("Refused at the STPG start check", "Upload accepted", "USB link to the STPG", "Port open"):
        assert other not in text, other
    assert page.d.find_elements(By.ID, "rpDeviceError") == []  # nothing ran: no device error to explain
    page.js("document.getElementById('rpExchanges').scrollIntoView()")
    page.shot("long-period-refused-before-reset")
    assert page.errors() == []


def test_a_device_error_code_is_named_and_not_called_a_host_timeout(page_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "SimulatedStpg", DeviceTimeout)
    source = write_source(tmp_path / "synthetic.stil", period="400ns")
    built = build_project(init_project(source, tmp_path / "project", wait_preset="none", completion="hold",
                                       max_segment_vectors=6, compile_mode="rate-vector"))
    page = page_factory()
    page.open_bundle(SimpleNamespace(bundle_dir=built.bundle_dir, manifest_sha256=built.manifest_sha256))
    page.choose("#rpRunBody select[name=device]", "simulated")
    page.d.find_element(By.ID, "rpRun").click()
    _confirm_and_start(page, double=False)
    assert _wait_run(page)["run"]["outcome"] == "DEVICE_ERROR"
    tables = page.js("return [...document.querySelectorAll('#rpRunBody table')].map(t => t.innerText).join('\\n')")
    assert "12 (ERR_TIMEOUT)" in tables
    text = page.text("rpDeviceError")
    assert "error code 12 (ERR_TIMEOUT)" in text and "it is not this program's response deadline" in text
    assert "the code alone does not say where it was set" in text and "CAP_STREAM_WAIT_WATCHDOG" in text
    assert "does not reset the STPG or try again" in text
    page.js("document.getElementById('rpDeviceError').scrollIntoView()")
    page.shot("device-error-named")
    # another firmware code is named too, without the stream-watchdog sentence
    page.js("const v = deviceErrorNotice([{index: 1, outcome: 'DEVICE_ERROR', error_name: 'ERR_DMA_ERROR', terminal: {error_code: 9}}]);"
            "v.id = 'otherCode'; document.getElementById('rpRunBody').append(v);")
    other = page.text("otherCode")
    assert "error code 9 (ERR_DMA_ERROR)" in other and "CAP_STREAM_WAIT_WATCHDOG" not in other
    assert page.errors() == []
