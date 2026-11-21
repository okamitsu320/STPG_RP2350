"""Browser-level checks of the Runner page: real HTTP server, real page, clicks and DOM assertions (host only).

These drive headless Firefox through Selenium against an in-process stpg-view server in demo mode, so every
STPG / GPIO controller is an in-memory model, an external accessory is the SDK's public stdio peer (a separate
process that touches no device), and the hardware guard forbids real serial opens and USB enumeration. No
logic-analyzer code is imported: that tool is a separate program with its own page. They check what service tests cannot: that the confirmation dialogs gate their buttons, that
a double click starts one run, that a GPIO state is never preselected, that error, unknown and
close-failure states are visible on the page, that an Identify shows the port lifecycle it actually
recorded, that a run stays visible and cancellable from the page, also after a reload, and that the New
project dialog (folder picker, preselected CUI defaults, wait suggestions, ATE preprocessing, copies,
automatic segmentation with the segment count of the build, the hint of a one-package build that does not
fit and projects made before automatic segmentation) behaves as the manual describes.

Skipped (not passed) when selenium, Firefox or geckodriver is missing. ``STPG_BROWSER_FIREFOX`` and
``STPG_BROWSER_GECKODRIVER`` select the binaries; screenshots go to ``STPG_BROWSER_SHOTS`` when set.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time

import pytest

webdriver = pytest.importorskip("selenium.webdriver")
from selenium.common.exceptions import (NoAlertPresentException, NoSuchElementException,  # noqa: E402
                                        StaleElementReferenceException)
from selenium.webdriver.common.action_chains import ActionChains  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.firefox.options import Options  # noqa: E402
from selenium.webdriver.firefox.service import Service  # noqa: E402
from selenium.webdriver.support.ui import Select  # noqa: E402

from stpg import constants as c  # noqa: E402
from stpg_runner import external_accessory, identify_controller  # noqa: E402
import stpg_runner.build as sdk_build  # noqa: E402
from stpg_runner.artifacts import read_json as read_record, write_json as write_record  # noqa: E402
from stpg_runner.device import DeviceLease  # noqa: E402
from stpgview.runner import jobs as jobs_module  # noqa: E402
from stpgview.runner import service as service_module  # noqa: E402
from stpgview.runner import simulated as simulated_module  # noqa: E402
from stpgview.runner.simulated import SIMULATED_DEVICE, SimulatedController, SimulatedStpg, controller_ports  # noqa: E402
from stpgview.physical.patterns import PATTERNS  # noqa: E402
from stpgview.server import ViewerServer, make_runner  # noqa: E402
from stpgview.session import Session  # noqa: E402

from stpg_hardware_guard import forbid_hardware, plug_in_bound_stpgs  # noqa: E402
from stpg_public_patterns import build_public as _build, counter_stil  # noqa: E402
from test_stpgview_controller_identify import CONTROLLER  # noqa: E402
from test_stpgview_runner_accessories import ANALYZER, _post_run_status_record, external_config  # noqa: E402
from test_stpgview_run_progress import (_LinkStopsAfterFirstDataBlock, _LinkStopsAfterRun,  # noqa: E402
                                        _PartialAnswerAfterBlock, _ReleaseAnswerFails, _RunWriteFails,
                                        _RunWriteShort, WINDOWS_OPEN_DENIED, _open_fails, _ports_gone_at,
                                        released)  # noqa: F401

pytestmark = pytest.mark.stpg_host_only
SNAP_FIREFOX = "/snap/firefox/current/usr/lib/firefox/firefox"  # /snap/bin/firefox is a wrapper selenium rejects


def _binaries() -> tuple[str, str] | None:
    firefox = os.environ.get("STPG_BROWSER_FIREFOX") or (SNAP_FIREFOX if Path(SNAP_FIREFOX).is_file() else None) \
        or shutil.which("firefox")
    driver = os.environ.get("STPG_BROWSER_GECKODRIVER") or shutil.which("geckodriver")
    return (firefox, driver) if firefox and driver else None


@pytest.fixture(scope="module")
def browser():
    found = _binaries()
    if found is None:
        pytest.skip("Firefox and geckodriver are required for the browser checks")
    options = Options()
    options.add_argument("-headless")
    options.binary_location = found[0]
    driver = webdriver.Firefox(options=options, service=Service(found[1]))
    driver.set_window_size(1400, 1000)
    yield driver
    driver.quit()


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture(scope="module")
def counter(tmp_path_factory):
    return _build(tmp_path_factory, "browser-counter", counter_stil(1600))


class Page:
    """The live page plus the server-side service it talks to."""

    def __init__(self, driver, srv, shots: Path | None) -> None:
        self.d, self.srv, self.svc, self.shots = driver, srv, srv.runner, shots
        host, port = srv.server_address[:2]
        self.url = f"http://{host}:{port}/?token=tok"

    def open(self) -> "Page":
        self.d.get(self.url)
        self.until(lambda: self.d.execute_script("return document.readyState") == "complete")
        self.d.find_element(By.ID, "modeRun").click()
        # expose the page's lexical globals (let/const) to the test, and record script errors
        self.inject("window.__errors = []; window.addEventListener('error', (e) => window.__errors.push(String(e.message)));"
                    "window.addEventListener('unhandledrejection', (e) => window.__errors.push(String(e.reason)));"
                    "window.__R = R;")
        self.until(lambda: self.js("return !!(window.__R && window.__R.state)"))
        # the binding lists are loaded and the cards rendered (the control card has its selector)
        self.until(lambda: self.d.find_elements(By.CSS_SELECTOR, "#rpControlBody select[name=controller]"),
                   what="page rendered")
        return self

    def inject(self, code: str) -> None:
        self.d.execute_script("const s = document.createElement('script'); s.textContent = arguments[0];"
                              "document.head.append(s);", code)

    def js(self, code: str, *args):
        return self.d.execute_script(code, *args)

    def until(self, predicate, timeout: float = 30.0, what: str = "condition"):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                value = predicate()
            except NoSuchElementException:
                value = None
            if value:
                return value
            time.sleep(0.05)
        self.shot(f"timeout-{what}")
        raise AssertionError(f"timed out waiting for {what}")

    def choose(self, css: str, value: str) -> None:
        """Select an option in a card's select. Cards re-render after their data loads (and on every job
        tick), which can replace the element between finding and using it; retry until the choice holds."""

        def chosen():
            try:
                Select(self.d.find_element(By.CSS_SELECTOR, css)).select_by_value(value)
                return Select(self.d.find_element(By.CSS_SELECTOR, css)).first_selected_option.get_attribute("value") == value
            except StaleElementReferenceException:
                return False

        self.until(chosen, what=f"{css} = {value}")

    def click(self, element_id: str) -> None:
        """Click a card's button by id. The card can re-render between finding and clicking the button
        (job ticks, the results list); retry with the new element until the click lands."""

        self.click_css(f"#{element_id}")

    def click_css(self, css: str) -> None:
        def clicked():
            try:
                self.d.find_element(By.CSS_SELECTOR, css).click()
                return True
            except StaleElementReferenceException:
                return False

        self.until(clicked, what=f"click {css}")

    def text(self, element_id: str) -> str:
        """The element's text. Cards re-render after their data loads, which can replace it while it is read."""

        for _ in range(100):
            try:
                return self.d.find_element(By.ID, element_id).text
            except StaleElementReferenceException:
                time.sleep(0.02)
        raise AssertionError(f"#{element_id} kept being replaced")

    def button(self, label: str, within=None, row: str | None = None):
        scope = within or self.d
        literal = lambda text: json.dumps(text, ensure_ascii=False)  # XPath has no \\u escapes
        prefix = f".//li[.//strong[normalize-space()={literal(row)}]]" if row else "."
        return scope.find_element(By.XPATH, f"{prefix}//button[normalize-space()={literal(label)}]")

    def open_bundle(self, built) -> None:
        self.inject(f"window.__bundleOpen = false; openBundle({json.dumps(str(built.bundle_dir))}, {json.dumps(built.manifest_sha256)})"
                    ".then(() => { window.__bundleOpen = true; });")
        self.until(lambda: self.js("return window.__bundleOpen === true"), what="bundle open")

    def dialog_open(self, element_id: str) -> bool:
        return self.js("return document.getElementById(arguments[0]).open", element_id)

    def errors(self) -> list[str]:
        return self.js("return window.__errors")

    def shot(self, name: str) -> None:
        if self.shots is not None:
            self.shots.mkdir(parents=True, exist_ok=True)
            self.d.save_screenshot(str(self.shots / f"{name}.png"))


@pytest.fixture
def page_factory(browser, tmp_path):
    servers = []

    def make(device_run: bool = True, bindings: dict | None = None) -> Page:
        session = Session(tmp_path / f"work{len(servers)}")
        runner = make_runner(session, host="127.0.0.1", demo=True, device_run=device_run,
                             config_dir=tmp_path / "cfg", results_dir=tmp_path / "runs")
        for (kind, name), config in (bindings or {}).items():  # saved before the page loads its lists
            if kind == "analyzers":  # written by the logic-analyzer tool; read-only in this GUI
                write_record(runner.store.root / kind / f"{name}.json", config)
            else:
                runner.store.save(kind, name, config)
        plug_in_bound_stpgs(runner)
        srv = ViewerServer(("127.0.0.1", 0), session, "tok", runner)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        shots = os.environ.get("STPG_BROWSER_SHOTS")
        return Page(browser, srv, Path(shots) if shots else None).open()

    yield make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _run_dirs(built) -> list[Path]:
    results = Path(built.bundle_dir).parent.parent / "results"
    return sorted(p for p in results.iterdir() if p.is_dir()) if results.is_dir() else []


def _type_external(page: Page, config: Path) -> None:
    """Type an accessory config path; clearing the box re-renders the card, so a stale element is retried."""

    def typed():
        try:
            box = page.d.find_element(By.CSS_SELECTOR, "#rpExternalBody input[name=external_config]")
            box.clear()
            box.send_keys(str(config), Keys.TAB)  # the change event
            return page.js("return window.__R.external.config") == str(config)
        except StaleElementReferenceException:
            return False

    page.until(typed, what="accessory config")


def _tick_external(page: Page) -> None:
    def ticked():
        try:
            box = page.d.find_element(By.CSS_SELECTOR, "#rpExternalBody input[name=external_use]")
            if not box.is_selected():
                box.click()
            return page.js("return window.__R.external.use") is True
        except StaleElementReferenceException:
            return False

    page.until(ticked, what="use with the next run")


def _select_external(page: Page, config: Path) -> None:
    """Choose an accessory config in the External accessory card, check it and use it for the next run."""

    _type_external(page, config)
    page.click("rpExternalCheck")
    page.until(lambda: page.js("return !!(window.__R.external.summary || window.__R.external.error)"),
               what="accessory check")
    assert page.js("return window.__R.external.error") is None, page.js("return window.__R.external.error")
    _tick_external(page)


def _add_action(page: Page, pin: int, state: str, phase: str = "before_run") -> None:
    page.button("Add action…", page.d.find_element(By.ID, "rpRunBody")).click()
    page.until(lambda: page.dialog_open("formDialog"), what="action dialog")
    body = page.d.find_element(By.ID, "formDialogBody")
    Select(body.find_element(By.NAME, "pin")).select_by_value(str(pin))
    Select(body.find_element(By.NAME, "phase")).select_by_value(phase)
    Select(body.find_element(By.NAME, "state")).select_by_value(state)
    page.d.find_element(By.ID, "formDialogOk").click()
    page.until(lambda: not page.dialog_open("formDialog"), what="action added")


def _confirm_and_start(page: Page, *, double: bool) -> None:
    page.until(lambda: page.dialog_open("confirmRunDialog"), what="run confirmation")
    start = page.d.find_element(By.ID, "confirmRunStart")
    boxes = page.d.find_elements(By.CSS_SELECTOR, "#confirmRunAcks input[type=checkbox]")
    assert boxes and not start.is_enabled()  # nothing preselected: every statement is checked by hand
    for index, box in enumerate(boxes):
        box.click()
        assert start.is_enabled() == (index == len(boxes) - 1)
    page.shot("run-confirmation")
    if double:
        ActionChains(page.d).double_click(start).perform()
    else:
        start.click()


def _wait_run(page: Page) -> dict:
    page.until(lambda: page.js("const e = window.__R.jobs.run; return e && e.job.status !== 'running'"),
               timeout=90, what="run job")
    page.until(lambda: "Overall" in page.text("rpRunBody"), what="run result")
    return page.js("return window.__R.jobs.run.job.result")


# ---- run confirmation ------------------------------------------------------------------------------------

def test_three_device_demo_run_needs_every_acknowledgement_and_a_double_click_starts_one_run(page_factory, counter,
                                                                                           tmp_path):
    page = page_factory()
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    _select_external(page, external_config(tmp_path / "accessory", mode="simulated"))
    page.choose("#rpRunBody select[name=run_controller]", "simulated")
    _add_action(page, 14, "HIGH" if 14 in page.js("return window.__R.controllers.find((c) => c.name === 'simulated')"
                                                  ".config.allow_high") else "LOW")
    _add_action(page, 14, "Hi-Z", phase="after_terminal")
    before = _run_dirs(counter)
    page.d.find_element(By.ID, "rpRun").click()
    _confirm_and_start(page, double=True)
    result = _wait_run(page)
    page.shot("run-three-devices")
    started = [p for p in _run_dirs(counter) if p not in before]
    assert len(started) == 1  # one confirmation, one POST, one run folder
    assert result["overall"]["exit_code"] == 0 and result["run"]["outcome"] == "COMPLETED"
    assert result["control"]["outcome"] == "COMPLETED" and result["external"]["mode"] == "simulated"
    assert result["external"]["outcome"] == "COMPLETED" and result["external"]["adopted"] is True
    text = page.text("rpRunBody")
    assert "SIMULATED — no hardware" in text and "status 0" in text
    assert "gpio controller" in text.lower() and "external accessory" in text.lower()  # headings may be upper-cased
    assert "public-peer" in text and "data adopted (the accessory's own judgement)" in text
    assert page.errors() == []


def test_a_binding_replaced_after_confirmation_is_rejected_in_the_dialog(page_factory, counter):
    # real (non-demo) bindings: the start must reject before any contact, which the hardware guard enforces
    page = page_factory(bindings={("devices", "dev"): SIMULATED_DEVICE.to_dict(),
                                  ("controllers", "gpio"): CONTROLLER.to_dict()})
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "dev")
    page.choose("#rpRunBody select[name=run_controller]", "gpio")
    _add_action(page, 14, "LOW")
    before = _run_dirs(counter)
    page.d.find_element(By.ID, "rpRun").click()
    page.until(lambda: page.dialog_open("confirmRunDialog"), what="run confirmation")
    assert "PUBLIC-GPIO-0001" in page.text("confirmRunBody")
    changed = {**CONTROLLER.to_dict(), "serial_number": "PUBLIC-GPIO-0002"}  # e.g. another tab edited it
    page.svc.store.save("controllers", "gpio", changed, replace=True)
    _confirm_and_start(page, double=False)
    error = page.until(lambda: page.d.find_element(By.ID, "confirmRunError").is_displayed()
                       and page.text("confirmRunError"), what="rejection")
    page.shot("run-binding-replaced")
    assert "changed" in error and "prepare again" in error
    assert _run_dirs(counter) == before and page.dialog_open("confirmRunDialog")
    assert not page.d.find_element(By.ID, "confirmRunStart").is_enabled()  # the nonce is gone with the click



def test_an_accessory_config_edited_after_confirmation_is_rejected_in_the_dialog(page_factory, counter, tmp_path):
    page = page_factory()
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    wrong = external_config(tmp_path / "device-mode")  # mode "device" cannot run with the simulated STPG
    page.until(lambda: page.d.find_element(By.ID, "rpExternalCheck").get_attribute("disabled") is not None,
               what="Check disabled without a config")
    box = page.d.find_element(By.CSS_SELECTOR, "#rpExternalBody input[name=external_config]")
    box.send_keys(str(wrong), Keys.TAB)
    page.click("rpExternalCheck")
    error = page.until(lambda: page.js("return window.__R.external.error"), what="mode refusal")
    assert "mode 'device'" in error and "mode 'device'" in page.text("rpExternalBody")
    assert page.d.find_element(By.CSS_SELECTOR, "#rpExternalBody input[name=external_use]").get_attribute("disabled")
    config = external_config(tmp_path / "accessory", mode="simulated")
    _select_external(page, config)
    assert "public-peer" in page.text("rpExternalBody") and "This run also starts the external accessory" in page.text("rpRunBody")
    before = _run_dirs(counter)
    page.d.find_element(By.ID, "rpRun").click()
    page.until(lambda: page.dialog_open("confirmRunDialog"), what="run confirmation")
    confirm = page.text("confirmRunBody")
    assert "External accessory public-peer" in confirm and "What the core checks, and what it does not" in confirm
    assert "external accessory: public-peer" in confirm
    assert "open limit" in confirm and "arm/complete limit" in confirm and "stdin EOF grace" in confirm
    assert "stdin EOF grace" in page.text("rpExternalBody")  # the Check summary lists the three limits too
    write_record(config, {**read_record(config), "timeout_s": 3})  # e.g. the accessory tool rewrote its config
    _confirm_and_start(page, double=False)
    error = page.until(lambda: page.d.find_element(By.ID, "confirmRunError").is_displayed()
                       and page.text("confirmRunError"), what="rejection")
    page.shot("run-accessory-config-changed")
    assert "external accessory selection changed after this run was confirmed" in error
    assert _run_dirs(counter) == before and page.dialog_open("confirmRunDialog")
    assert page.errors() == []


def test_analyzer_bindings_are_listed_read_only_by_usb_identity(page_factory):
    page = page_factory(bindings={("analyzers", "la"): {**ANALYZER.to_dict(), "limits": {"id_line": "opaque"},
                                                         "wiring": "same-gpio"}})
    card = page.text("rpBindingsBody")
    assert "logic analyzers (usb identity only)" in card.lower()  # headings are upper-cased by the style sheet
    assert "PUBLIC-LA-0001" in card and "2e8a:000a" in card
    assert "opaque" not in card and "same-gpio" not in card  # what the LA tool keeps is not shown or interpreted
    page.d.find_element(By.ID, "rpManageAnalyzers").click()
    page.until(lambda: page.dialog_open("formDialog"), what="analyzer list")
    body = page.d.find_element(By.ID, "formDialogBody")
    page.shot("analyzer-bindings-read-only")
    labels = [b.text for b in body.find_elements(By.TAG_NAME, "button")]
    assert not {"Add…", "Edit", "Delete", "Identify (opens the analyzer port)"} & set(labels), labels
    assert "separately installed logic-analyzer tool" in page.text("rpAnalyzerReadOnly")
    assert page.errors() == []


# ---- first use: settings folder and logic-analyzer guidance ----------------------------------------------------

def test_a_new_user_sees_the_settings_folder_and_how_to_use_an_earlier_one(page_factory, tmp_path):
    page = page_factory()
    folder = page.until(lambda: "from --config-dir" in page.text("rpSettingsFolder") and page.text("rpSettingsFolder"),
                        what="settings folder")
    assert str((tmp_path / "cfg").resolve()) in folder
    assert "New: created when the first binding is saved." in folder
    assert "Have bindings from an earlier setup?" in folder and "Nothing is moved or copied for you." in folder
    assert not (tmp_path / "cfg").exists()  # looking at the page created nothing
    page.shot("settings-folder-new")
    assert page.errors() == []


def test_existing_settings_are_counted_and_the_earlier_setup_hint_goes_away(page_factory):
    page = page_factory(bindings={("devices", "dev"): SIMULATED_DEVICE.to_dict(),
                                  ("controllers", "gpio"): CONTROLLER.to_dict(),
                                  ("analyzers", "la"): ANALYZER.to_dict()})
    folder = page.until(lambda: "1 analyzers." in page.text("rpSettingsFolder") and page.text("rpSettingsFolder"),
                        what="settings folder counts")
    assert "1 devices, 1 controllers, 1 analyzers." in folder and "Have bindings from an earlier setup?" not in folder
    assert page.errors() == []


def test_a_settings_folder_problem_is_shown_and_no_other_folder_is_used(page_factory, tmp_path):
    (tmp_path / "cfg").write_text("a file where the folder should be")
    page = page_factory()
    folder = page.until(lambda: "Settings folder problem" in page.text("rpSettingsFolder")
                        and page.text("rpSettingsFolder"), what="settings folder problem")
    assert "is a file, not a folder" in folder
    assert "No other folder is used instead." in folder
    assert (tmp_path / "cfg").read_text() == "a file where the folder should be"
    assert page.errors() == []


@pytest.mark.parametrize("installed", [False, True])
def test_the_logic_analyzer_guidance_says_what_is_here_and_what_to_do(page_factory, monkeypatch, installed):
    tool = {"distribution": "stpg-rp2350-la", "command": "stpg-la", "installed": installed,
            "version": "0.1.0" if installed else None}
    monkeypatch.setattr(service_module, "accessory_tool", lambda: tool)
    page = page_factory()
    line = page.text("rpLaTool")
    if installed:
        assert "installed in this Python environment (stpg-rp2350-la 0.1.0)" in line and "stpg-la" in line
    else:
        assert "not found in this Python environment" in line and "STPG-only and GPIO runs work without it" in line
    assert not page.js("return document.getElementById('rpLaGuide').open")
    page.click_css("#rpLaGuide summary")
    read = "const g = document.getElementById('rpLaGuide'); return g.open ? g.innerText : ''"
    text = page.until(lambda: page.js(read), what="guide opened")
    page.inject("renderExternal();")  # the card re-renders during jobs: an open guide stays open for the reader
    assert page.until(lambda: page.js(read), what="guide still open")
    page.shot(f"la-guide-{'installed' if installed else 'absent'}")
    for needle in ("separate download", "Start the separate page", "same settings folder", "press Identify",
                   "writes external.json", "press Check", "Open the run's folder on its page"):
        assert needle in text, needle
    assert r".\stpg-env\Scripts\python.exe -m pip install . ../STPG_RP2350_LA" in text
    assert "./stpg-env/bin/python -m pip install . ../STPG_RP2350_LA" in text
    assert "-m stpg_capture.ui.server --demo --no-device-io" in text
    assert page.errors() == []


def test_the_analyzer_list_points_to_the_tool_that_manages_it(page_factory):
    page = page_factory()
    page.d.find_element(By.ID, "rpManageAnalyzers").click()
    page.until(lambda: page.dialog_open("formDialog"), what="analyzer list")
    note = page.text("rpAnalyzerReadOnly")
    assert note.startswith("Read only.") and "-m stpg_capture.ui.server" in note
    assert "stpg-la is an alias in an activated environment" in note
    assert page.errors() == []


def test_each_accessory_state_is_told_apart_and_nothing_starts(page_factory, counter, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Check or the card started a process")

    monkeypatch.setattr(external_accessory._ProcessSession, "start", forbidden)
    page = page_factory()
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    # one atomic read: the card re-renders while its check runs
    state = lambda: page.js("return document.getElementById('rpExternalState').innerText")  # noqa: E731
    assert state().startswith("not selected") and "uses the STPG" in state()
    _type_external(page, tmp_path / "missing.json")
    page.until(lambda: state().startswith("not checked"), what="not checked")
    page.click("rpExternalCheck")
    page.until(lambda: state().startswith("check failed"), what="check failed")
    assert page.js("return window.__R.external.error")
    config = external_config(tmp_path / "accessory", mode="simulated")
    _type_external(page, config)
    page.until(lambda: state().startswith("not checked"), what="not checked again")
    page.click("rpExternalCheck")
    page.until(lambda: state().startswith("checked, not in use"), what="checked")
    _tick_external(page)
    page.until(lambda: state().startswith("used with the next run"), what="used")
    assert "nothing falls back to an STPG-only run" in state()
    assert list(page.svc.jobs.jobs.values()) == []  # checking and ticking started no job or process
    assert page.errors() == []


def test_a_run_that_cannot_be_prepared_says_why_in_the_run_card(page_factory, counter, monkeypatch):
    # Preparing re-verifies the bundle on the server; a refusal used to reach only the status bar, so a click on
    # Run… looked like nothing happened (seen on the user's Windows PC).
    page = page_factory()
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    gate, refusing, original = threading.Event(), [True], page.svc.prepare_run

    def refuse(request):
        if not refusing[0]:
            return original(request)
        gate.wait(30)
        raise service_module.RunnerError("execution preview supports 1..256 segments")

    monkeypatch.setattr(page.svc, "prepare_run", refuse)
    page.d.find_element(By.ID, "rpRun").click()
    page.until(lambda: page.d.find_elements(By.ID, "rpRunPreparing"), what="preparing note")
    assert "no device contact" in page.text("rpRunPreparing")
    assert not page.d.find_element(By.ID, "rpRun").is_enabled()  # one preparation at a time
    gate.set()
    error = page.until(lambda: page.d.find_elements(By.ID, "rpRunPrepareError") and page.text("rpRunPrepareError"),
                       what="refusal in the run card")
    page.shot("run-prepare-refused")
    assert "could not be prepared" in error and "execution preview supports 1..256 segments" in error
    assert not page.d.find_elements(By.ID, "rpRunPreparing")
    assert not page.dialog_open("confirmRunDialog") and page.d.find_element(By.ID, "rpRun").is_enabled()
    assert not any(Path(page.svc.results_dir).glob("*"))  # nothing reserved, nothing sent
    refusing[0] = False  # the real preparation: the refusal goes away and the confirmation opens
    page.d.find_element(By.ID, "rpRun").click()
    page.until(lambda: page.dialog_open("confirmRunDialog"), what="run confirmation")
    assert not page.d.find_elements(By.ID, "rpRunPrepareError")
    assert page.errors() == []


def test_a_clicked_button_shows_it_is_working_and_a_second_run_reuses_the_bundle_check(page_factory, counter,
                                                                                     monkeypatch):
    # The first check of a bundle verifies it in full (minutes for a large one) and the click used to change little
    # more than a greyed button (user report). The button is busy from the click until the server answers, the card
    # counts the seconds, and the confirmation says how the SDK checked the bundle (its process receipt, 8f698155).
    sdk_build._clear_verification_receipts()  # per process: the module fixture's CUI build left one
    page = page_factory()
    page.open_bundle(counter)
    assert "verified in full now" in page.text("rpBundleCheck")
    page.open_bundle(counter)
    assert "re-hashed against the SDK's full verification" in page.text("rpBundleCheck")
    sdk_build._clear_verification_receipts()  # so that the first Run… verifies in full
    assert "pattern region of the runtime profile" in page.text("rpPsramShare")
    page.choose("#rpRunBody select[name=device]", "simulated")
    gate, original = threading.Event(), page.svc.prepare_run

    def held(request):
        gate.wait(30)
        return original(request)

    monkeypatch.setattr(page.svc, "prepare_run", held)
    run = page.d.find_element(By.ID, "rpRun")
    run.click()
    page.until(lambda: "busy" in run.get_attribute("class").split() and run.get_attribute("aria-busy") == "true",
               timeout=2, what="busy Run button")
    page.until(lambda: re.fullmatch(r"[1-9]\d* s", page.text("rpRunPreparingTime")), timeout=5, what="elapsed seconds")
    assert "Preparing the run" in page.text("rpRunPreparing") and "Preparing the run" in page.text("statusMsg")
    page.shot("run-preparing-busy")
    gate.set()
    page.until(lambda: page.dialog_open("confirmRunDialog"), what="run confirmation")
    assert "busy" not in run.get_attribute("class").split() and run.get_attribute("aria-busy") is None
    first = page.text("confirmRunBody")
    assert "verified in full now" in first and "checks the bundle again on its own" in first
    page.button("Cancel", page.d.find_element(By.ID, "confirmRunForm")).click()
    page.until(lambda: not page.dialog_open("confirmRunDialog"), what="confirmation closed")
    gate.clear()
    run.click()
    page.until(lambda: "busy" in run.get_attribute("class").split(), timeout=2, what="busy Run button again")
    gate.set()
    page.until(lambda: page.dialog_open("confirmRunDialog"), what="second confirmation")
    page.shot("run-confirmation-reused-check")
    assert "every file re-hashed against the SDK's full verification of these bytes" in page.text("confirmRunBody")
    assert "verified in full now" not in page.text("confirmRunBody")
    assert page.errors() == []

# ---- GPIO state is chosen, never defaulted ---------------------------------------------------------------

def test_gpio_dialogs_never_preselect_a_state(page_factory, counter):
    page = page_factory()
    page.choose("#rpControlBody select[name=controller]", "simulated")
    page.until(lambda: page.d.find_element(By.ID, "rpSetGpio").is_enabled(), what="Set GPIO enabled")
    page.d.find_element(By.ID, "rpSetGpio").click()
    page.until(lambda: page.dialog_open("formDialog"), what="set dialog")
    body = page.d.find_element(By.ID, "formDialogBody")
    Select(body.find_element(By.NAME, "pin")).select_by_value("14")
    assert Select(body.find_element(By.NAME, "state")).first_selected_option.get_attribute("value") == ""
    page.d.find_element(By.ID, "formDialogOk").click()
    error = page.until(lambda: page.d.find_element(By.ID, "formDialogError").is_displayed()
                       and page.text("formDialogError"), what="state error")
    assert "choose the GPIO state explicitly" in error
    assert not page.dialog_open("confirmControlDialog")
    assert not any(Path(page.svc.results_dir).glob("*"))  # nothing reserved, nothing sent
    Select(body.find_element(By.NAME, "state")).select_by_value("Hi-Z")
    page.d.find_element(By.ID, "formDialogOk").click()
    page.until(lambda: page.dialog_open("confirmControlDialog"), what="control confirmation")
    assert page.text("confirmControlTitle") == "Confirm GPIO14 → Hi-Z"
    assert not page.d.find_element(By.ID, "confirmControlStart").is_enabled()
    page.shot("set-confirmation")
    page.d.find_element(By.CSS_SELECTOR, "#confirmControlForm button[value=cancel]").click()

    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=run_controller]", "simulated")
    page.button("Add action…", page.d.find_element(By.ID, "rpRunBody")).click()
    page.until(lambda: page.dialog_open("formDialog"), what="action dialog")
    body = page.d.find_element(By.ID, "formDialogBody")
    assert Select(body.find_element(By.NAME, "state")).first_selected_option.get_attribute("value") == ""
    page.d.find_element(By.ID, "formDialogOk").click()
    assert "choose the GPIO state explicitly" in page.until(
        lambda: page.d.find_element(By.ID, "formDialogError").is_displayed() and page.text("formDialogError"))
    assert page.js("return window.__R.gpioPlan.actions.length") == 0
    assert page.errors() == []


# ---- error, unknown and close-failure states are visible -------------------------------------------------

class _LateSecondAck(SimulatedController):
    """Acknowledges the first tagged pin command only: the second write's level is unknown afterwards."""

    def _command(self, text):
        if text.startswith("@") and self.sequence >= 1:
            return None
        return super()._command(text)


def test_accessory_failures_render_as_separate_axes_without_relabelling_the_stpg_result(page_factory, counter,
                                                                                       monkeypatch, tmp_path):
    monkeypatch.setattr(service_module, "SimulatedController", _LateSecondAck)
    page = page_factory()
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    _select_external(page, external_config(tmp_path / "accessory", mode="simulated"))
    page.choose("#rpRunBody select[name=run_controller]", "simulated")
    _add_action(page, 14, "Hi-Z")
    _add_action(page, 14, "LOW", phase="after_terminal")
    page.d.find_element(By.ID, "rpRun").click()
    _confirm_and_start(page, double=False)
    result = _wait_run(page)
    page.shot("run-accessory-failures")
    # the after-DONE GPIO failure stops the invocation (REJECT), but the saved segment terminal keeps its own
    # COMPLETED outcome and full progress: nothing is relabelled as a device error or a pass
    assert result["run"]["outcome"] == "REJECT" and result["run"]["completed_segments"] == 1
    assert [(g["outcome"], g["terminal"]["run_progress"]) for g in result["segments"]] == [("COMPLETED", 6400)]
    # the GPIO failure after DONE ended the invocation before the accessory's COMPLETE request
    assert result["control"]["outcome"] == "REJECT" and result["external"]["outcome"] == "INCOMPLETE"
    assert result["external"]["adopted"] is False and result["overall"]["exit_code"] == 2
    text = page.text("rpRunBody")
    assert "status 2" in text and "DEVICE_ERROR" not in text and "6,400/6,400" in text
    assert "controller response timeout; prefix retained" in text
    assert "GPIO14 LOW was requested but not acknowledged" in text
    assert "INCOMPLETE" in text and "Accessory not asked to collect" in text
    assert page.errors() == []


class _ReadyStpg(SimulatedStpg):
    """A simulated STPG that reports READY (a pattern loaded and not yet run) when the run starts."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last = replace(self.last, state=c.STATE_READY)


def test_a_run_the_stpg_refuses_at_its_start_check_says_why_and_that_gpio_was_not_started(page_factory, counter,
                                                                                         monkeypatch):
    monkeypatch.setattr(service_module, "SimulatedStpg", _ReadyStpg)
    page = page_factory()
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    page.choose("#rpRunBody select[name=run_controller]", "simulated")
    _add_action(page, 2, "Hi-Z")
    page.d.find_element(By.ID, "rpRun").click()
    _confirm_and_start(page, double=False)
    result = _wait_run(page)
    assert result["run"]["outcome"] == "REJECT" and result["start_check"]["state"] == "READY"
    text = page.text("rpRunBody")
    page.js("[...document.querySelectorAll('#rpRunBody .notice.bad')][0].scrollIntoView()")
    page.shot("run-refused-at-start-check")
    assert "finished · REJECT" in text  # the job ended; the run did not succeed
    assert "Refused at the STPG start check" in text
    assert "A run starts only when the STPG reports IDLE or DONE; it reported READY." in text
    assert "Nothing was uploaded and no RUN was sent" in text and "READY means a pattern was loaded" in text
    assert "does not reset the STPG, run it anyway or try again" in text
    assert "Not started: the STPG refused the run at its start check" in text
    assert "the controller's port was not opened" in text and "GPIO2 Hi-Z" not in text  # no request row
    assert page.errors() == []


class _OtherFirmwareStpg(SimulatedStpg):
    """A simulated STPG that reports another firmware version than its binding expects."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last = replace(self.last, firmware_version=SIMULATED_DEVICE.firmware_version ^ 0x100)


def test_a_different_firmware_shows_both_identities_and_the_form_explains_the_values(page_factory, counter,
                                                                                    monkeypatch):
    monkeypatch.setattr(service_module, "SimulatedStpg", _OtherFirmwareStpg)
    page = page_factory()
    page.d.find_element(By.ID, "rpManageDevices").click()
    page.until(lambda: page.dialog_open("formDialog"), what="device list")
    page.button("Add…", page.d.find_element(By.ID, "formDialogBody")).click()
    help_box = page.until(lambda: page.d.find_element(By.ID, "rpDeviceHelp"), what="help")
    assert help_box.get_attribute("open") is None
    page.click_css("#rpDeviceHelp summary")
    text = page.text("rpDeviceHelp")
    assert "INFO reply" in text and "not the USB device version" in text and "No firmware release" in text
    page.d.find_element(By.XPATH, "//dialog[@id='formDialog']//button[@value='cancel']").click()
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    page.d.find_element(By.ID, "rpRun").click()
    _confirm_and_start(page, double=False)
    result = _wait_run(page)
    assert result["identity_mismatch"]["fields"][0]["reported"] == SIMULATED_DEVICE.firmware_version ^ 0x100
    text = page.text("rpRunBody")
    page.js("[...document.querySelectorAll('#rpRunBody .notice.bad')][0].scrollIntoView()")
    page.shot("run-identity-differs")
    assert "The STPG's identity differs from this binding" in text and "Nothing was uploaded" in text
    assert f"0x{SIMULATED_DEVICE.firmware_version:08x}" in text
    assert f"0x{SIMULATED_DEVICE.firmware_version ^ 0x100:08x}" in text
    assert "never copies reported values into a binding" in text and "Refused at the STPG start check" not in text
    assert page.errors() == []


class _SilentAfterRunStpg(SimulatedStpg):
    """Answers RUN, then no STATUS: the bounded host read ends without a byte."""

    def _handle(self, command):
        if command.cmd == c.CMD_GET_STATUS and self.run_args:
            return
        super()._handle(command)


def test_a_run_without_a_status_reply_shows_the_answered_steps_not_a_failed_upload(page_factory, counter, monkeypatch):
    monkeypatch.setattr(service_module, "SimulatedStpg", _SilentAfterRunStpg)
    page = page_factory()
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    page.d.find_element(By.ID, "rpRun").click()
    _confirm_and_start(page, double=False)
    result = _wait_run(page)
    assert result["run"]["outcome"] == "REJECT" and result["stopped_at"]["run"] == "OK · RUNNING"
    rows = page.d.find_elements(By.CSS_SELECTOR, "#rpRunBody ul.stages li")
    marks = [(row.text, row.get_attribute("class")) for row in rows]
    # The SDK reported the accepted UPLOAD_END and RUN itself; the result file adds only the failed STATUS.
    upload = [row for row in rows if "upload" in row.text and "UPLOAD_END accepted (segment 1)" in row.text]
    assert len(upload) == 1 and upload[0].get_attribute("class") == "done", marks
    run_row = [row for row in rows if "RUN accepted" in row.text or "RUN answered" in row.text]
    status_row = [row for row in rows if "GET_STATUS after RUN · no reply (" in row.text]  # the recorded stage
    assert len(run_row) == 1 and run_row[0].get_attribute("class") == "done", marks  # not shown twice
    assert status_row and status_row[0].get_attribute("class") == "failed"
    assert [row for row in rows if row.get_attribute("class") == "failed"] == status_row  # the only failed step
    page.js("[...document.querySelectorAll('#rpRunBody ul.stages li')].pop().scrollIntoView({block: 'center'})")
    page.shot("run-no-status-reply-stages")
    text = page.text("rpRunBody")
    page.js("[...document.querySelectorAll('#rpRunBody .notice.warn')][0].scrollIntoView()")
    page.shot("run-no-status-reply")
    assert "Where the run stopped (from the result file)" in text
    assert "UPLOAD_END was answered OK · READY: the upload was accepted." in text
    assert "Whether this segment ran to its end is not known: no terminal STATUS was observed for it." in text
    assert "After this failure, the runner did not retry or send a recovery reset." in text
    assert page.errors() == []


def test_first_status_failure_rows_do_not_depend_on_the_upload_being_the_last_progress_stage(page_factory):
    page = page_factory()
    stop = service_module.run_stop_point(_post_run_status_record(segment=1, response_bytes=12))
    assert stop is not None
    # Aggregated progress may end in a previous segment's completion, or be
    # absent. The recorded failure still belongs to the current segment.
    results = page.js("""
      const job = {status: "done", elapsed_s: 3, title: "synthetic", result: {run: {outcome: "REJECT"}, stopped_at: arguments[0]}};
      const events = [{stage: "upload", event: "start", job_s: 0},
                      {stage: "segment", event: "done", completed: 1, job_s: 1},
                      {stage: "upload", event: "progress", completed: 8, total: 8, job_s: 2}];
      return [events, []].map(ev => [...jobView({job, events: ev}).querySelectorAll("ul.stages li")]
        .map(li => [li.className, li.textContent]));""", stop)
    for rows in results:
        assert any(cls == "done" and "UPLOAD_END answered OK · HOLD_AWAITING_NEXT" in label for cls, label in rows)
        assert any(cls == "done" and "RUN answered OK · RUNNING" in label for cls, label in rows)
        failed = [label for cls, label in rows if cls == "failed"]
        assert len(failed) == 1 and "GET_STATUS after RUN · 12 response bytes, not a complete reply" in failed[0]
    assert page.errors() == []


def test_an_accessory_that_is_not_ready_says_the_stpg_was_not_started(page_factory, counter, tmp_path):
    # e.g. the accessory found that its own device differs from its binding: it never answers READY, so the SDK
    # never starts the STPG run (the first Windows LA run of the user, now in the accessory's own process).
    page = page_factory()
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    _select_external(page, external_config(tmp_path / "accessory", scenario="open-status", mode="simulated"))
    page.d.find_element(By.ID, "rpRun").click()
    _confirm_and_start(page, double=False)
    result = _wait_run(page)
    assert result["run"]["outcome"] == "NOT_STARTED" and result["external"]["outcome"] == "REJECT"
    text = page.text("rpRunBody")
    assert "STPG not started" in text and "opened no STPG port and sent no STPG command" in text
    assert "The final result was not persisted" not in text and "null" not in text
    assert "0/unknown" in text and "status rejected" in text and "No cleanup command was sent" in text
    assert text.count("external accessory response identity/sequence/status rejected") == 1  # shown once
    status = page.until(lambda: "NOT_STARTED" in page.text("statusMsg") and page.text("statusMsg"), what="status")
    assert "0/unknown segments" in status and "null" not in status
    page.js("[...document.querySelectorAll('#rpRunBody .notice.warn')].pop().scrollIntoView()")
    page.shot("run-accessory-not-ready")
    assert page.errors() == []


def test_controller_identify_close_failure_is_shown_with_its_saved_evidence(page_factory, monkeypatch):
    class CloseFails(SimulatedController):
        def close(self):
            super().close()
            raise OSError("injected controller close failure")

    def injected(config, **kwargs):
        peer = CloseFails()
        kwargs.update(stream_factory=lambda _config: peer, port_provider=lambda: controller_ports(config))
        return identify_controller(config, **kwargs)

    monkeypatch.setattr(service_module, "identify_controller", injected)
    page = page_factory(bindings={("controllers", "gpio"): CONTROLLER.to_dict()})
    page.d.find_element(By.ID, "rpManageControllers").click()
    page.until(lambda: page.dialog_open("formDialog"), what="controller list")
    page.button("Identify", page.d.find_element(By.ID, "formDialogBody"), row="gpio").click()
    text = page.until(lambda: "Identify rejected" in page.text("formDialogBody") and page.text("formDialogBody"),
                      what="identify result")
    page.shot("controller-identify-close-failure")
    assert "close: OSError: injected controller close failure" in text
    assert "evidence saved:" in text and "USB effects and the pin voltages are not measured" in text
    assert "The port was opened; successful close is unproved." in text and "close was recorded successful" not in text
    assert "pin commands written: 0" in text and "STPG_GPIO_V1" in text  # the consumed reply stays visible
    saved = json.loads(next(Path(page.svc.results_dir).glob("*-controller-identify-*/identify.json")).read_text())
    assert saved["status"] == "REJECT" and saved["result_persisted"] is True
    assert page.errors() == []


def test_no_device_run_scan_lists_no_real_ports_and_says_so(page_factory, monkeypatch):
    calls = []
    from serial.tools import list_ports

    monkeypatch.setattr(list_ports, "comports", lambda *a, **k: calls.append(1) or [])
    page = page_factory(device_run=False)
    assert "Device execution is disabled" in page.d.find_element(By.TAG_NAME, "body").text
    page.d.find_element(By.ID, "rpManageDevices").click()
    page.until(lambda: page.dialog_open("formDialog"), what="device list")
    page.button("Add…", page.d.find_element(By.ID, "formDialogBody")).click()
    page.button("Scan ports", page.d.find_element(By.ID, "formDialogBody")).click()
    text = page.until(lambda: "No real port scan" in page.text("formDialogBody") and page.text("formDialogBody"),
                      what="scan result")
    page.shot("no-device-run-scan")
    assert "--no-device-run" in text and calls == []
    assert page.errors() == []


def test_the_first_stpg_binding_is_saved_by_one_press_after_a_scan(page_factory, tmp_path):
    # Scan ports posts {} and the Save that follows may use the same kept-alive connection. The server once left
    # that {} unread, so the first Save failed with "Unsupported method ('{}POST')". One press must save.
    page = page_factory(device_run=False)
    page.d.find_element(By.ID, "rpManageDevices").click()
    page.until(lambda: page.dialog_open("formDialog"), what="device list")
    page.button("Add…", page.d.find_element(By.ID, "formDialogBody")).click()
    page.until(lambda: page.text("formDialogTitle") == "Add STPG device binding", what="add form")
    page.button("Scan ports", page.d.find_element(By.ID, "formDialogBody")).click()
    use = f"//div[@id='formDialogBody']//tr[td[normalize-space()={json.dumps(SIMULATED_DEVICE.port)}]]" \
          "//button[normalize-space()='Use']"
    page.until(lambda: page.d.find_element(By.XPATH, use), what="scanned port").click()
    form = page.d.find_element(By.ID, "formDialogForm")
    assert form.find_element(By.NAME, "port").get_attribute("value") == SIMULATED_DEVICE.port
    form.find_element(By.NAME, "name").send_keys("bench")
    # Save straight after Use, as a new user does: the binding is refused for its own reason, not the transport.
    page.d.find_element(By.ID, "formDialogOk").click()
    refused = page.until(lambda: page.d.find_element(By.ID, "formDialogError").is_displayed()
                         and page.text("formDialogError"), what="the binding's own refusal")
    assert "Unsupported method" not in refused and "firmware_version" in refused, refused
    assert not (tmp_path / "cfg" / "devices").exists()
    form.find_element(By.NAME, "firmware_version").send_keys(hex(SIMULATED_DEVICE.firmware_version))
    form.find_element(By.NAME, "capability").send_keys(hex(SIMULATED_DEVICE.capability))
    page.d.find_element(By.ID, "formDialogOk").click()
    page.until(lambda: not page.dialog_open("formDialog"), what="saved")
    saved = tmp_path / "cfg" / "devices" / "bench.json"
    assert read_record(saved)["port"] == SIMULATED_DEVICE.port
    assert page.errors() == []


def test_identify_rejected_before_open_never_says_a_port_was_opened(page_factory, monkeypatch):
    opened = []
    peer = SimulatedController()

    def injected(config, **kwargs):
        kwargs.update(stream_factory=lambda _config: opened.append(1) or peer,
                      port_provider=lambda: controller_ports(config))
        return identify_controller(config, **kwargs)

    monkeypatch.setattr(service_module, "identify_controller", injected)
    page = page_factory(bindings={("controllers", "gpio"): CONTROLLER.to_dict()})
    page.d.find_element(By.ID, "rpManageControllers").click()
    page.until(lambda: page.dialog_open("formDialog"), what="controller list")
    with DeviceLease(CONTROLLER):
        page.button("Identify", page.d.find_element(By.ID, "formDialogBody"), row="gpio").click()
        text = page.until(lambda: "Identify rejected" in page.text("formDialogBody") and page.text("formDialogBody"),
                          what="controller identify rejection")
    page.shot("controller-identify-lease")
    assert "No port-open attempt was recorded for this invocation." in text and "The port was opened" not in text
    assert opened == [] and "pin commands written: 0" in text
    page.button("Identify", page.d.find_element(By.ID, "formDialogBody"), row="gpio").click()
    text = page.until(lambda: "matches this binding" in page.text("formDialogBody") and page.text("formDialogBody"),
                      what="controller identify")
    assert "The port was opened; close was recorded successful." in text and opened == [1]
    assert len(list(Path(page.svc.results_dir).glob("*-identify-*/identify.json"))) == 2  # every attempt kept
    assert page.errors() == []


# ---- a running job stays visible and cancellable ---------------------------------------------------------

def _slow_simulated_stpg(monkeypatch) -> list:
    """The demo STPG reports RUNNING until the host gives up (the SDK's poll limit, about 15 s here)."""
    monkeypatch.setattr(simulated_module, "RUNNING_POLLS", 10**6)
    peers = []

    class Recorded(SimulatedStpg):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            peers.append(self)

    monkeypatch.setattr(service_module, "SimulatedStpg", Recorded)
    return peers


def _start_demo_run(page: Page, built) -> None:
    page.open_bundle(built)
    page.choose("#rpRunBody select[name=device]", "simulated")
    page.d.find_element(By.ID, "rpRun").click()
    _confirm_and_start(page, double=False)


def _cancel_from_page(page: Page) -> None:
    page.d.find_element(By.ID, "rpCancel").click()

    def alert():
        try:
            return page.d.switch_to.alert
        except NoAlertPresentException:
            return None

    prompt = page.until(alert, what="cancel prompt")
    assert "No STOP or release is sent" in prompt.text
    prompt.accept()


def _assert_cancelled_without_stop(page: Page, peers: list, built, before: list) -> None:
    result = _wait_run(page)
    assert result["run"]["outcome"] == "CANCELLED" and result["overall"]["exit_code"] == 130
    assert page.js("return window.__R.jobs.run.job.status") == "cancelled"
    assert len(peers) == 1 and c.CMD_STOP not in peers[0].commands and peers[0].runs == 1  # nothing sent after
    assert len([p for p in _run_dirs(built) if p not in before]) == 1
    text = page.text("rpRunBody")
    assert "CANCELLED" in text and "status 130" in text
    assert page.errors() == []


def test_a_run_is_cancelled_from_the_page_without_a_stop(page_factory, counter, monkeypatch):
    peers = _slow_simulated_stpg(monkeypatch)
    page = page_factory()
    before = _run_dirs(counter)
    _start_demo_run(page, counter)
    page.until(lambda: page.d.find_element(By.ID, "rpCancel").is_displayed(), what="Cancel shown")
    page.until(lambda: peers and peers[0].polls >= 2, what="the device polled while RUNNING")
    _cancel_from_page(page)
    _assert_cancelled_without_stop(page, peers, counter, before)
    page.shot("run-cancelled")


def test_a_reloaded_page_reattaches_to_the_running_job_and_can_cancel_it(page_factory, counter, monkeypatch):
    peers = _slow_simulated_stpg(monkeypatch)
    page = page_factory()
    before = _run_dirs(counter)
    _start_demo_run(page, counter)
    page.until(lambda: peers and peers[0].polls >= 2, what="the device polled while RUNNING")
    job_id = page.js("return window.__R.jobs.run.job.id")
    page.open()  # a reload: the page has no local job any more
    page.until(lambda: page.js("const e = window.__R.jobs.run; return e && e.job.id") == job_id, what="re-attached job")
    page.until(lambda: page.d.find_element(By.ID, "rpCancel").is_displayed(), what="Cancel after the reload")
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    assert not page.d.find_element(By.ID, "rpRun").is_enabled()  # the running job still locks Run
    assert "running" in page.text("rpRunBody")
    page.shot("run-reattached")
    _cancel_from_page(page)
    _assert_cancelled_without_stop(page, peers, counter, before)



def _live(page: Page, element_id: str) -> str:
    """Text of an element of the run card, read in one script call (the card re-renders every poll)."""
    return page.js("const e = document.getElementById(arguments[0]); return e ? e.textContent : ''", element_id)


def test_a_running_run_shows_the_check_then_its_live_progress(page_factory, counter, monkeypatch):
    # After Start run the SDK prepares on the host (port check, bundle verification, port check again), then
    # reports upload and STATUS polls. The page shows each phase and how long ago the run last reported, so a long
    # step is not a hang.
    peers = _slow_simulated_stpg(monkeypatch)
    gate, original = threading.Event(), service_module.run_with_accessories

    def held(*args, **kwargs):
        gate.wait(30)
        return original(*args, **kwargs)

    monkeypatch.setattr(service_module, "run_with_accessories", held)
    page = page_factory()
    _start_demo_run(page, counter)
    page.until(lambda: "Host preparation: enter SDK preparation" in _live(page, "rpRunPhase"), what="the hand-off to the SDK")
    assert "not pattern execution" in _live(page, "rpRunPhase") and "Host preparation for" in _live(page, "rpRunLive")
    assert not page.js("return document.querySelector('#rpRunBody ul.stages') !== null")  # merged stage list: at the end
    page.shot("run-progress-check")
    gate.set()
    page.until(lambda: peers and peers[0].polls >= 2, what="the device polled while RUNNING")
    page.until(lambda: "Segment 1 of 1: not finished yet (STATUS poll" in _live(page, "rpRunPhase"), what="live STATUS polls")
    # the demo device reports run_progress 0 while RUNNING: no percentage, polls and time instead
    assert "currently reports zero progress" in _live(page, "rpRunPhase") and "s in this segment" in _live(page, "rpRunPhase")
    assert not page.js("return document.querySelector('#rpRunProgress .bar') !== null")
    assert "Last report from the run" in _live(page, "rpRunLive")
    assert "Run: segment 1 of 1: not finished, STATUS poll" in _live(page, "statusMsg")  # the status bar is always in view
    assert page.js("const r = document.getElementById('rpRunProgress').getBoundingClientRect();"
                   " return r.top >= 0 && r.bottom <= window.innerHeight")  # the live view was scrolled into view
    page.shot("run-progress-running")
    _cancel_from_page(page)
    _wait_run(page)
    assert not page.d.find_elements(By.ID, "rpRunProgress")  # the live view ends with the run
    assert page.d.find_elements(By.CSS_SELECTOR, "#rpRunBody ul.stages")
    rows = page.text("rpRunBody")
    assert "enter SDK preparation" in rows and "verify bundle" in rows
    # the SDK checks the port twice; each check is its own row
    assert "check USB port metadata · before the bundle check" in rows
    assert "check USB port metadata · right before the port is opened" in rows
    assert "Run cancelled: CANCELLED" in _live(page, "statusMsg")
    assert page.errors() == []


def test_the_live_view_tells_bytes_sent_upload_accepted_run_accepted_and_status_apart(page_factory):
    # The page's reading of the SDK's acceptance notifications: bytes sent are not acceptance, an accepted RUN is not
    # progress, and a STATUS count does not name RUNNING or RUN_DRAINING.
    page = page_factory()
    phases = page.js("""
      const view = (ev, extra) => { const v = runProgressView(Object.assign({events: ev, started: performance.now() - 2000}, extra || {}));
                                    return [v.querySelector('#rpRunPhase').textContent, v.querySelector('#rpRunLive').textContent]; };
      const base = [{stage: "run_verify", event: "start", segments: 1}, {stage: "device_preflight", event: "start", job_s: 0.1},
                    {stage: "upload", event: "start", completed: 0, total: 4096, job_s: 0.2}];
      const sent = base.concat([{stage: "upload", event: "progress", completed: 2048, total: 4096, job_s: 0.3}]);
      const done = base.concat([{stage: "upload", event: "done", completed: 4096, total: 4096, detail: "segment=0; UPLOAD_END accepted", job_s: 0.4}]);
      const accepted = done.concat([{stage: "run", event: "accepted", detail: "segment=0; RUN accepted; waiting for STATUS; no waveform or terminal completion observed", job_s: 0.5}]);
      const quietAccepted = done.concat([{stage: "run", event: "accepted", detail: "segment=0; RUN accepted", job_s: -10}]);
      const zero = accepted.concat([{stage: "observe", event: "progress", completed: 0, total: 50, detail: "segment=0; poll=1", job_s: 1}]);
      return [view(sent), view(done), view(done, {accessories: {controller: "gpio", external: null}}),
              view(accepted, {statusBudget: 20}), view(quietAccepted, {statusBudget: 20}), view(zero)];""")
    sent, done, done_hook, accepted, quiet, zero = phases
    assert "sending the pattern, 2,048 / 4,096 bytes (not accepted until UPLOAD_END is answered)" in sent[0]
    assert "upload accepted (UPLOAD_END, 4,096 / 4,096 bytes). Next: RUN." in done[0]
    assert "Next: the before-RUN step of the selected GPIO controller or accessory, if this segment has one, then RUN." \
        in done_hook[0]
    assert "RUN accepted; waiting for the first STATUS reply (" in accepted[0]
    assert "each STATUS reply may take up to 20 s" in accepted[0]
    assert "No STATUS has arrived yet, so no progress has been reported either way" in accepted[0]
    assert "zero progress" not in accepted[0] and "running" not in accepted[0].lower().replace("run accepted", "")
    assert "no STATUS reply yet after RUN (the SDK waits up to 20 s for each reply)" in quiet[1]
    assert "information, not a timeout; nothing is retried" in quiet[1]
    assert "not finished yet (STATUS poll 1" in zero[0] and "running" not in zero[0].lower().replace("run accepted", "")
    assert "DRAINING" not in zero[0] and "RUNNING" not in zero[0]  # an observe event names no state
    assert page.errors() == []


def _stage_rows(page: Page, job: dict, events: list) -> list:
    return page.js("""
      const box = jobView({job: arguments[0], events: arguments[1], started: performance.now()});
      return [...box.querySelectorAll('ul.stages li')].map((li) => [li.className, li.textContent]);""", job, events)


def test_the_stage_list_uses_the_acceptance_events_and_keeps_the_result_file_rows(page_factory):
    page = page_factory()
    stop = {"segment": 0, "planned_segments": 1, "upload_end": "OK · READY", "run": "OK · RUNNING", "run_took_s": 0.001,
            "failed_request": "GET_STATUS", "response_bytes": 0, "failure_stage": "deadline", "failed_took_s": 20.0}
    job = {"status": "done", "elapsed_s": 25.0, "title": "run", "result": {"run": {"outcome": "REJECT"}, "stopped_at": stop}}
    upload = [{"stage": "upload", "event": "start", "completed": 0, "total": 4096, "job_s": 1},
              {"stage": "upload", "event": "progress", "completed": 4096, "total": 4096, "job_s": 2}]
    accepted = [{"stage": "upload", "event": "done", "completed": 4096, "total": 4096,
                 "detail": "segment=0; UPLOAD_END accepted", "job_s": 3},
                {"stage": "run", "event": "accepted", "detail": "segment=0; RUN accepted; waiting for STATUS", "job_s": 4}]
    # With the SDK's acceptance events: one RUN row, an upload accepted by its event, one failed STATUS row.
    rows = _stage_rows(page, job, upload + accepted)
    texts = [text for _, text in rows]
    assert sum("RUN" in text and "accepted" in text or "RUN answered" in text for text in texts) == 1, texts
    assert any(cls == "done" and "UPLOAD_END accepted (segment 1)" in text and "from the result file" not in text
               for cls, text in rows), rows
    assert any(cls == "done" and "RUN accepted (segment 1)" in text for cls, text in rows), rows
    failed = [text for cls, text in rows if cls == "failed"]
    assert len(failed) == 1 and "GET_STATUS after RUN · no reply (deadline)" in failed[0]
    # Without them (an older event stream or a reloaded run): the R6 reconstruction from the result file stays.
    rows = _stage_rows(page, job, upload)
    assert any(cls == "done" and "UPLOAD_END answered OK · READY (from the result file)" in text for cls, text in rows)
    assert any(cls == "done" and "RUN answered OK · RUNNING (from the result file)" in text for cls, text in rows)
    assert [text for cls, text in rows if cls == "failed"] == failed
    # A failure after the accepted RUN that the journal does not narrow down: no cross on the accepted steps.
    plain = {**job, "result": {"run": {"outcome": "REJECT"}, "stopped_at": None}}
    rows = _stage_rows(page, plain, upload + accepted)
    assert [cls for cls, _ in rows] == ["done", "done", "recorded"], rows
    assert "how the run ended is in the result below" in rows[-1][1]
    assert page.errors() == []


class _SlowFirstStatus(SimulatedStpg):
    """Answers the first STATUS after RUN about three seconds late (a firmware start-up wait), then normally."""

    delayed = False

    def _handle(self, command):
        if command.cmd == c.CMD_GET_STATUS and self.run_args and not self.delayed:
            self.delayed = True
            time.sleep(3)
        super()._handle(command)


def test_a_slow_first_status_is_shown_as_waiting_after_an_accepted_run(page_factory, counter, monkeypatch):
    monkeypatch.setattr(service_module, "SimulatedStpg", _SlowFirstStatus)
    page = page_factory()
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    page.d.find_element(By.ID, "rpRun").click()
    page.until(lambda: page.dialog_open("confirmRunDialog"), what="run confirmation")
    assert "each STATUS reply may take up to 20 s" in page.text("confirmStatusBudget")
    assert "nothing is retried or reset" in page.text("confirmStatusBudget")
    _confirm_and_start(page, double=False)
    phase = page.until(lambda: "RUN accepted; waiting for the first STATUS reply" in _live(page, "rpRunPhase")
                       and _live(page, "rpRunPhase"), what="the wait after RUN", timeout=20)
    assert "No STATUS has arrived yet" in phase and "zero progress" not in phase
    assert "each STATUS reply may take up to 20 s" in phase
    page.shot("run-waiting-for-first-status")
    result = _wait_run(page)
    assert result["run"]["outcome"] == "COMPLETED"
    rows = page.text("rpRunBody")
    assert "UPLOAD_END accepted (segment 1)" in rows and "RUN accepted (segment 1)" in rows
    assert page.errors() == []


def test_the_live_view_counts_the_segments_of_a_segmented_run(page_factory):
    # The page's own reading of the SDK's event sequence for a 3-segment run (upload, STATUS polls, done per segment).
    page = page_factory()
    events = [{"stage": "run_verify", "event": "start", "segments": 3}, {"stage": "run_verify", "event": "done"},
              {"stage": "device_preflight", "event": "start"}]
    for k in range(2):
        events += [{"stage": "upload", "event": "start", "completed": 0, "total": 4096},
                   {"stage": "upload", "event": "progress", "completed": 4096, "total": 4096},
                   {"stage": "observe", "event": "progress", "completed": 10, "total": 50, "detail": f"segment={k}; poll=1"},
                   {"stage": "segment", "event": "done", "completed": k + 1, "total": 3}]
    states = page.js("""
      const ev = arguments[0];
      const upTo = (n) => runProgressState(ev.slice(0, n));
      return [upTo(1), upTo(ev.length), upTo(ev.length - 1),
              runProgressState(ev.concat([{stage: "upload", event: "start", completed: 0, total: 4096}]))];""", events)
    assert states[0]["planned"] == 3 and states[0]["finished"] == 0
    assert (states[1]["current"], states[1]["finished"]) == (2, 2)
    assert (states[2]["current"], states[2]["finished"], states[2]["poll"]) == (2, 1, 1)
    assert (states[3]["current"], states[3]["finished"], states[3]["poll"]) == (3, 2, None)
    texts = page.js("""
      const view = (ev) => { const v = runProgressView({events: ev, started: performance.now()});
                             return [v.querySelector('#rpRunPhase').textContent, v.querySelectorAll('.bar').length]; };
      const base = arguments[0].slice(0, 3).concat([{stage: "upload", event: "start", completed: 0, total: 4096, job_s: 1}]);
      return [view(base.concat([{stage: "observe", event: "progress", completed: 0, total: 50, detail: "segment=0; poll=3", job_s: 2}])),
              view(base.concat([{stage: "observe", event: "progress", completed: 10, total: 50, detail: "segment=0; poll=4", job_s: 2}]))];""",
                    events)
    assert "not finished yet (STATUS poll 3" in texts[0][0] and "currently reports zero progress" in texts[0][0]
    assert texts[0][1] == 1  # the segments bar only: no bar for a count the device does not report
    assert "not finished yet — 10 / 50 reported events" in texts[1][0] and texts[1][1] == 2  # a reported count gets its bar
    host = page.js("""
      const v = runProgressView({events: [{stage: "bundle_files", event: "progress", completed: 2, total: 4, job_s: 1}],
                                 started: performance.now()});
      return [v.querySelector('#rpRunPhase').textContent, v.querySelectorAll('.bar').length];""")
    assert "Host preparation: hash bundle files" in host[0] and "This step: 2 / 4 files" in host[0] and host[1] == 1
    checks = page.js("""
      return ["before_bundle_verify", "before_open"].map((detail) => runProgressView(
        {events: [{stage: "port_check", event: "start", detail, job_s: 1}], started: performance.now()})
        .querySelector('#rpRunPhase').textContent);""")
    assert "check USB port metadata before the bundle check" in checks[0]
    assert "check USB port metadata right before the port is opened" in checks[1]
    assert page.errors() == []

def test_a_missing_stpg_port_is_refused_when_run_is_pressed(page_factory, counter):
    # On the user's Windows PC the SDK reported the port only after it had verified a large bundle for minutes.
    page = page_factory(bindings={("devices", "dev"): SIMULATED_DEVICE.to_dict()})
    page.svc.port_provider = lambda: []  # the STPG is not plugged in
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "dev")
    page.d.find_element(By.ID, "rpRun").click()
    error = page.until(lambda: page.d.find_elements(By.ID, "rpRunPrepareError") and page.text("rpRunPrepareError"),
                       what="port refusal in the run card")
    page.shot("run-port-missing")
    assert "STPG port check" in error and "is not in the USB port list" in error and "bundle was not checked" in error
    assert not page.dialog_open("confirmRunDialog") and not any(Path(page.svc.results_dir).glob("*"))
    assert page.errors() == []


def test_a_rejected_run_marks_the_stage_where_it_stopped_and_explains_the_port_check(page_factory):
    # A REJECT is a "done" job (the SDK returned a result); its last stage used to get a check mark.
    page = page_factory()
    stages = page.js("""
      const entry = {job: {status: "done", elapsed_s: 10, title: "run", result: {run: {outcome: arguments[0]}}},
                     events: [{stage: "run_verify", event: "start", job_s: 0}, {stage: "run_verify", event: "done", job_s: 9},
                              {stage: "device_preflight", event: "start", elapsed_s: 0, job_s: 9}]};
      return [...jobView(entry).querySelectorAll("ul.stages li")].map((li) => [li.className, li.textContent]);""", "REJECT")
    assert [cls for cls, _ in stages] == ["done", "failed"]
    assert stages[0][1].endswith("9.0 s") and stages[1][1].startswith("✗") and stages[1][1].endswith("1.0 s")
    completed = page.js("""
      const entry = {job: {status: "done", elapsed_s: 10, title: "run", result: {run: {outcome: "COMPLETED"}}},
                     events: [{stage: "segment", event: "start", job_s: 1}]};
      return [...jobView(entry).querySelectorAll("ul.stages li")].map((li) => li.className);""")
    assert completed == ["done"]
    texts = page.js("""
      return [false, true, undefined].map((attempted) => {
        const run = {outcome: "REJECT", comparison: "UNOBSERVED", completed_segments: 0, planned_segments: 9,
                     release_command_attempted: false, release_acknowledged: false, release_sequence_complete: false,
                     result_persisted: true, stream_open_attempted: attempted,
                     errors: ["RunnerError: selected port missing/duplicated or USB identity missing/ambiguous"]};
        return runResultView({run, completion: "release", segments: []}).textContent; });""")
    assert all("Port check" in text and "Scan ports" in text for text in texts)
    assert "no STPG serial open attempt" in texts[0]
    assert not any("wrote no STPG command" in text for text in texts)  # no failed open in these results
    # an accessory's port check can fail after the upload: without the SDK's fact, no "nothing was sent"
    assert all("does not prove no STPG contact" in text and "no STPG serial open attempt" not in text for text in texts[1:])
    rows = page.js("""
      const entry = {job: {status: "done", elapsed_s: 5, title: "run", result: {run: {outcome: "REJECT"}}},
                     events: [{stage: "port_check", event: "start", detail: "before_bundle_verify", job_s: 0},
                              {stage: "port_check", event: "done", detail: "before_bundle_verify", job_s: 1},
                              {stage: "bundle_verify", event: "start", job_s: 1}, {stage: "bundle_verify", event: "done", job_s: 4},
                              {stage: "port_check", event: "start", detail: "before_open", job_s: 4}]};
      return [...jobView(entry).querySelectorAll("ul.stages li")].map((li) => [li.className, li.textContent]);""")
    assert [cls for cls, _ in rows] == ["done", "done", "failed"]
    assert "before the bundle check" in rows[0][1] and rows[0][1].endswith("1.0 s")
    assert "right before the port is opened" in rows[2][1] and rows[2][1].endswith("1.0 s")
    assert page.errors() == []


def test_the_live_view_keeps_up_after_the_server_drops_old_events(page_factory, counter, monkeypatch):
    # A large run emits thousands of events (one per 4 KB uploaded, one per STATUS poll); the server keeps the last
    # MAX_EVENTS. The page used to receive nothing more once the first events were dropped.
    monkeypatch.setattr(jobs_module, "MAX_EVENTS", 30)
    peers = _slow_simulated_stpg(monkeypatch)
    gates = {20: threading.Event(), 40: threading.Event()}
    parent = service_module.SimulatedStpg

    class Batched(parent):
        def _handle(self, command):
            if command.cmd == c.CMD_GET_STATUS and self.run_args:
                gate = gates.get(self.polls)
                if gate is not None:
                    gate.wait(30)  # model only: each 20-reply batch fits in the 30-event history
            super()._handle(command)

    monkeypatch.setattr(service_module, "SimulatedStpg", Batched)
    page = page_factory()
    _start_demo_run(page, counter)

    def poll_shown() -> int:
        m = re.search(r"STATUS poll (\d+)", _live(page, "rpRunPhase"))
        return int(m.group(1)) if m else 0

    try:
        page.until(lambda: poll_shown() >= 20, what="the page receiving the first retained batch")
        gates[20].set()
        page.until(lambda: poll_shown() >= 40, what="the page following the polls past the dropped events")
        assert peers[0].polls >= 40 and len(page.svc.jobs.get(page.js("return window.__R.jobs.run.job.id")).events) <= 30
        # We acknowledge each batch before producing the next; this checks the
        # absolute cursor, not an unbounded delivery guarantee under browser lag.
        polls = page.js("""return window.__R.jobs.run.events.filter((e) => e.stage === "observe")
                             .map((e) => Number(/poll=(\\d+)/.exec(e.detail)[1]));""")
        assert len(polls) >= 40 and polls == list(range(polls[0], polls[0] + len(polls)))
        _cancel_from_page(page)
        gates[40].set()
        _wait_run(page)
        assert page.errors() == []
    finally:
        # A failed assertion must not leave a simulated device leased for the
        # next test. Cancellation sends no cleanup/STOP command.
        job = page.svc.jobs.active_device_job()
        if job is not None:
            job.token.cancel()
        for gate in gates.values():
            gate.set()
        end = time.monotonic() + 30
        while job is not None and job.status == "running" and time.monotonic() < end:
            time.sleep(.01)
        assert job is None or job.status != "running", "simulated job did not finish after cancellation"


@pytest.mark.parametrize("phase", ["before_bundle_verify", "before_open"])
def test_a_port_that_goes_away_inside_the_sdk_shows_where_the_run_stopped(page_factory, counter, monkeypatch, phase):
    # The GUI's own checks (Run…, Start run) pass; the port is gone at one of the SDK's two checks.
    peers = _slow_simulated_stpg(monkeypatch)
    monkeypatch.setattr(service_module, "simulated_ports", _ports_gone_at(phase))
    page = page_factory()
    _start_demo_run(page, counter)
    result = _wait_run(page)
    assert result["run"]["outcome"] == "REJECT" and result["run"]["stream_open_attempted"] is False and peers == []
    text = page.text("rpRunBody")
    assert "[PORT_NOT_LISTED]" in text and "no STPG serial open attempt" in text and "Scan ports" in text
    where = {"before_bundle_verify": "before the bundle check", "before_open": "right before the port is opened"}[phase]
    failed = page.js("return [...document.querySelectorAll('#rpRunBody ul.stages li.failed')].map((li) => li.textContent)")
    assert len(failed) == 1 and f"check USB port metadata · {where}" in failed[0]
    # without a plan the completion is unknown, not "no"
    assert ("unknown (preparation did not complete)" in text) == (phase == "before_bundle_verify")
    page.shot(f"run-port-gone-{phase}")
    assert page.errors() == []


def test_a_usb_link_that_stops_after_the_first_data_block_shows_the_last_exchanges(page_factory, counter, monkeypatch):
    # The user's Windows runs: the device answered up to READY_FOR_PAYLOAD, the first data block was written, and the
    # driver call before the answer failed. The page shows those exchanges and what to do next.
    monkeypatch.setattr(service_module, "SimulatedStpg", _LinkStopsAfterFirstDataBlock)
    page = page_factory()
    _start_demo_run(page, counter)
    result = _wait_run(page)
    assert result["run"]["outcome"] == "REJECT" and result["exchanges"]["link_lost"] is True
    text = page.text("rpRunBody")
    assert "Last STPG exchanges (6 of 8, from the result file)" in text
    exchanges = page.js("return [...document.getElementById('rpExchanges').nextElementSibling"
                        ".querySelectorAll('tbody tr')].map((tr) => [...tr.children].map((td) => td.textContent))")
    assert [row[1] for row in exchanges] == ["GET_STATUS", "RESET", "GET_STATUS", "UPLOAD_BEGIN", "UPLOAD_DATA",
                                             "payload"]
    assert exchanges[-2][3] == "READY_FOR_PAYLOAD · RECEIVING" and exchanges[-1][3] == "no complete response (see Errors)"
    assert exchanges[-1][2].endswith(" B") and "ReadFile failed" in text
    assert 'Windows error 121 ("semaphore timeout") reports a Windows API timeout' in text  # the error carries 121
    assert "USB link to the STPG" in text and "does not prove target consumption or execution" in text
    assert "No new RUN write was attempted" in text and "previously held outputs" in text
    assert "Unplug and replug" not in text
    assert "wrote no STPG command" not in text  # the port was open
    failed = page.js("return [...document.querySelectorAll('#rpRunBody ul.stages li.failed')].map((li) => li.textContent)")
    assert len(failed) == 1 and "upload" in failed[0]
    page.js("document.getElementById('rpExchanges').scrollIntoView()")
    page.shot("run-usb-link-lost")
    assert page.errors() == []


def test_a_usb_link_that_stops_after_run_says_the_outputs_may_still_be_driven(page_factory, counter, monkeypatch):
    monkeypatch.setattr(service_module, "SimulatedStpg", _LinkStopsAfterRun)
    page = page_factory()
    _start_demo_run(page, counter)
    result = _wait_run(page)
    assert result["exchanges"]["link_lost"] is True and result["exchanges"]["run_sent"] is True
    text = page.text("rpRunBody")
    assert "does not prove target consumption or execution" in text
    assert "A RUN write was attempted; outputs may still be driven" in text and "No new RUN write was attempted" not in text
    assert page.errors() == []


@pytest.mark.parametrize("peer, bundle, request_name, sent, answer, present, absent", [
    (_RunWriteFails, "counter", "RUN", "—", "no complete response (see Errors)",
     ["A write was attempted, but its sent-byte count is unknown or incomplete", "failing stage: write",
      "A RUN write was attempted; outputs may still be driven"], ["The host reported a full write"]),
    (_RunWriteShort, "counter", "RUN", "12 B", "no complete response (see Errors)",
     ["A write was attempted, but its sent-byte count is unknown or incomplete", "failing stage: write",
      "A RUN write was attempted; outputs may still be driven"], ["The host reported a full write"]),
    (_PartialAnswerAfterBlock, "counter", "payload", "4,096 B", "9 response bytes (see Errors)",
     ["9 response bytes recorded; failing stage: read", "The host reported a full write of the last request",
      "No new RUN write was attempted; previously held outputs may still be driven"], ["A RUN write was attempted"]),
    (_ReleaseAnswerFails, "released", "RESET", "32 B", "no complete response (see Errors)",
     ["This does not release previously held outputs", "A RUN write was attempted; outputs may still be driven"],
     ["sends no STOP, RESET or release"]),
])
def test_each_failure_shape_reads_as_what_the_journal_shows(page_factory, request, monkeypatch, peer, bundle,
                                                          request_name, sent, answer, present, absent):
    # Write attempted vs reported complete, partial answers, the failing stage, and a failed release answer.
    built = request.getfixturevalue(bundle)
    monkeypatch.setattr(service_module, "SimulatedStpg", peer)
    page = page_factory()
    _start_demo_run(page, built)
    result = _wait_run(page)
    assert result["run"]["outcome"] == "REJECT" and result["exchanges"]["link_lost"] is True
    rows = page.js("return [...document.getElementById('rpExchanges').nextElementSibling"
                   ".querySelectorAll('tbody tr')].map((tr) => [...tr.children].map((td) => td.textContent))")
    assert rows[-1][1:4] == [request_name, sent, answer]
    text = page.text("rpRunBody")
    assert all(phrase in text for phrase in present), [p for p in present if p not in text]
    assert not any(phrase in text for phrase in absent)
    carries_121 = any("121" in error for error in result["run"]["errors"])  # the read peers say "error 121"
    assert ('Windows error 121 ("semaphore timeout") reports a Windows API timeout' in text) is carries_121
    assert ("This host I/O error does not identify a USB, driver or target root cause" in text) is not carries_121
    assert page.errors() == []


@pytest.mark.parametrize("message, windows_denied", [
    (WINDOWS_OPEN_DENIED, True),
    ("[Errno 13] could not open port /dev/ttyACM9: [Errno 13] Permission denied: '/dev/ttyACM9'", False),  # POSIX
    # Python also maps other Windows errors to PermissionError(13, ...); only error 5 gets the access-denied line
    ("could not open port 'COM3': PermissionError(13, 'The process cannot access the file because it is being used "
     "by another process.', None, 32)", False),
])
def test_a_port_open_that_fails_says_no_command_was_written(page_factory, counter, monkeypatch, message,
                                                            windows_denied):
    monkeypatch.setattr(service_module, "SimulatedStpg", _open_fails(message))
    page = page_factory()
    _start_demo_run(page, counter)
    result = _wait_run(page)
    assert result["run"]["stream_open_attempted"] is True and result["run"]["stream_opened"] is False
    text = page.text("rpRunBody")
    assert "Port open" in text and "so this run wrote no STPG command" in text and message in text
    assert "This does not release outputs held from an earlier run" in text and "does not retry the open" in text
    assert ('Windows error 5 ("access denied") is what Windows returns while another handle' in text) is windows_denied
    assert "Last STPG exchanges" not in text and "USB link to the STPG" not in text and "Port check" not in text
    failed = page.js("return [...document.querySelectorAll('#rpRunBody ul.stages li.failed')].map((li) => li.textContent)")
    assert len(failed) == 1 and "device preflight" in failed[0]
    if windows_denied:
        page.js("[...document.querySelectorAll('#rpRunBody .notice.warn')].pop().scrollIntoView()")
        page.shot("run-port-open-denied")
    assert page.errors() == []


@pytest.mark.parametrize("check,held_names", [
    ("semantic", ("verify_pattern_package", "verify_segmented_manifest")),  # no receipt: the full check
    ("receipt", ("_verify_inventory",)),  # the SDK's receipt from the confirmation: every file re-hashed
])
def test_a_long_bundle_check_keeps_reporting_and_can_be_cancelled(page_factory, counter, monkeypatch, check,
                                                                  held_names):
    # The SDK's verification of a large bundle takes minutes; it reports heartbeats, so the elapsed time goes on
    # and the last host update stays recent. Cancel ends the run at the SDK's next boundary, before any open.
    # Since SDK 8f698155 the check after Start re-hashes against the receipt of the confirmation's check; without
    # a receipt (restart, eviction) it is the full check. Both are held here, and both end before any open.
    peers = _slow_simulated_stpg(monkeypatch)
    gate, entered = threading.Event(), threading.Event()
    page = page_factory()
    before = _run_dirs(counter)
    page.open_bundle(counter)
    page.choose("#rpRunBody select[name=device]", "simulated")
    page.d.find_element(By.ID, "rpRun").click()
    page.until(lambda: page.dialog_open("confirmRunDialog"), what="run confirmation")
    if check == "semantic":
        sdk_build._clear_verification_receipts()
    for name in held_names:  # only the check after Start run is held
        original = getattr(sdk_build, name)

        def held(*args, _original=original, **kwargs):
            entered.set()
            gate.wait(60)
            return _original(*args, **kwargs)

        monkeypatch.setattr(sdk_build, name, held)
    try:
        _confirm_and_start(page, double=False)
        assert entered.wait(30)
        page.until(lambda: "Host preparation: verify bundle" in _live(page, "rpRunPhase"), what="the SDK's bundle check")

        def times():
            m = re.search(r"Host preparation for ([\d.]+) s; last host update ([\d.]+) s ago", _live(page, "rpRunLive"))
            return (float(m.group(1)), float(m.group(2))) if m else None

        first = page.until(times, what="elapsed time")
        time.sleep(3)
        # heartbeats: the elapsed time goes on and the last update stays recent (it used to grow with the elapsed
        # time, because the server replaces a heartbeat in place and the page fetched only new positions)
        later = page.until(lambda: (t := times()) and t[0] >= first[0] + 2.5 and t[1] < 2.0 and t, timeout=10,
                           what="a recent heartbeat")
        assert later[1] < later[0] - first[0]
        status = re.search(r"Run: host preparation: verify bundle · ([\d.]+) s", _live(page, "statusMsg"))
        assert status and float(status.group(1)) >= first[0] + 2.0  # the status bar shows the elapsed time too
        assert "waiting for the device" not in _live(page, "rpRunLive")
        page.shot("run-host-preparation")
        _cancel_from_page(page)
    finally:
        gate.set()
    result = _wait_run(page)
    assert result["run"]["outcome"] == "CANCELLED" and result["run"]["stream_open_attempted"] is False
    assert peers == [] and page.js("return window.__R.jobs.run.job.status") == "cancelled"
    assert len([p for p in _run_dirs(counter) if p not in before]) == 1
    assert "CANCELLED" in page.text("rpRunBody") and "Port check" not in page.text("rpRunBody")
    assert page.errors() == []


# ---- new project dialog ---------------------------------------------------------------------------------

RUNNER_STIL = Path(__file__).parent / "fixtures" / "runner" / "pattern.stil"


def _new_project_dialog(page: Page, button: str = "rpNewProject"):
    page.d.find_element(By.ID, button).click()
    page.until(lambda: page.dialog_open("formDialog"), what="new project dialog")
    return page.d.find_element(By.ID, "formDialogBody")


def _field(body, label: str):
    """The labelled field box whose caption reads ``label``."""

    literal = json.dumps(label, ensure_ascii=False)
    return body.find_element(By.XPATH, f".//label[contains(@class,'field')][span[1][normalize-space()={literal}]]")


def _set(body, name: str, value: str) -> None:
    box = body.find_element(By.NAME, name)
    box.clear()
    box.send_keys(value)


def _chosen(body, name: str) -> str:
    return Select(body.find_element(By.NAME, name)).first_selected_option.get_attribute("value")


def _create(page: Page) -> dict:
    page.d.find_element(By.ID, "formDialogOk").click()
    page.until(lambda: not page.dialog_open("formDialog") or page.d.find_element(By.ID, "formDialogError").is_displayed(),
               what="create finished")
    assert not page.dialog_open("formDialog"), page.text("formDialogError")
    return page.js("return window.__R.project")


def test_new_project_defaults_new_folder_picker_and_wait_suggestions(page_factory, tmp_path):
    page = page_factory()
    parent = tmp_path / "parent"
    (parent / "taken").mkdir(parents=True)
    body = _new_project_dialog(page)
    # CUI defaults are preselected and visible; the choices the CUI also requires are left open.
    assert [_chosen(body, n) for n in ("storage", "compile_mode", "loop_policy", "segmentation")] == \
        ["psram", "auto", "preserve", "auto"]
    assert [_chosen(body, n) for n in ("input_form", "wait_preset", "completion")] == ["", "", ""]
    # The SDK's two modes; automatic takes an optional vector upper bound, left blank (no segment count).
    seg = Select(body.find_element(By.NAME, "segmentation"))
    assert [o.get_attribute("value") for o in seg.options] == ["auto", "single"]
    vectors = _field(body, "Max vectors / segment (optional)")
    assert vectors.is_displayed() and body.find_element(By.NAME, "max_segment_vectors").get_attribute("value") == ""
    seg.select_by_value("single")
    assert not vectors.is_displayed()
    seg.select_by_value("auto")
    assert vectors.is_displayed() and body.find_element(By.NAME, "max_segment_vectors").get_attribute("value") == ""
    # A chunk preset fills visible suggestions (the CUI example values); an edited value is kept.
    Select(body.find_element(By.NAME, "wait_preset")).select_by_value("chunk-4ms")
    value = lambda name: body.find_element(By.NAME, name).get_attribute("value")  # noqa: E731
    assert [value("service_us"), value("max_total_wait_us"), value("service_source")] == \
        ["4000", "1000000", "declared, not measured"]
    Select(body.find_element(By.NAME, "wait_preset")).select_by_value("chunk-12ms")
    assert value("service_us") == "12000"
    _set(body, "service_us", "5000")
    Select(body.find_element(By.NAME, "wait_preset")).select_by_value("chunk-4ms")
    assert value("service_us") == "5000"
    # Enter in a text field does not press the form's first button (Close).
    body.find_element(By.NAME, "service_us").send_keys(Keys.ENTER)
    assert page.dialog_open("formDialog")
    # The folder picker takes a parent plus a new name and creates nothing.
    page.button("Browse…", _field(body, "Project folder")).click()
    page.until(lambda: page.dialog_open("pickDialog"), what="folder picker")
    assert page.d.find_element(By.ID, "pickNewName").is_displayed()
    page.d.find_element(By.ID, "pickDir").clear()
    page.d.find_element(By.ID, "pickDir").send_keys(str(parent), Keys.ENTER)
    page.until(lambda: "taken" in page.text("pickList"), what="parent listed")
    page.d.find_element(By.ID, "pickNewName").send_keys("taken")
    page.d.find_element(By.ID, "pickOk").click()
    assert page.dialog_open("pickDialog") and "already exists here" in page.text("pickError")
    page.d.find_element(By.ID, "pickNewName").clear()
    page.d.find_element(By.ID, "pickNewName").send_keys("TAKEN")  # the same folder on Windows and macOS
    page.d.find_element(By.ID, "pickOk").click()
    assert page.dialog_open("pickDialog") and "upper and lower case" in page.text("pickError")
    roots = [b.text for b in page.d.find_elements(By.CSS_SELECTOR, "#pickRoots button")]
    assert roots and (os.name == "nt" or roots == ["/"])  # every drive on Windows: a model test covers that
    page.d.find_element(By.ID, "pickNewName").clear()
    page.d.find_element(By.ID, "pickNewName").send_keys("fresh")
    assert page.d.find_element(By.ID, "pickPath").get_attribute("value") == str(parent / "fresh")
    page.shot("new-folder-picker")
    page.d.find_element(By.ID, "pickNewName").send_keys(Keys.ENTER)  # Enter = Choose
    page.until(lambda: not page.dialog_open("pickDialog"), what="picker closed")
    assert page.dialog_open("formDialog")
    assert body.find_element(By.NAME, "directory").get_attribute("value") == str(parent / "fresh")
    assert not (parent / "fresh").exists()
    # Back to wait=none: the suggestions are not sent (the SDK rejects ignored service settings).
    Select(body.find_element(By.NAME, "input_form")).select_by_value("stil")
    _set(body, "stil", str(RUNNER_STIL))
    Select(body.find_element(By.NAME, "completion")).select_by_value("hold")
    Select(body.find_element(By.NAME, "wait_preset")).select_by_value("none")
    project = _create(page)
    assert project["settings"]["wait"]["preset"] == "none" and project["settings"]["wait"]["service_us"] is None
    assert project["settings"]["execution"]["storage"] == "psram"
    assert project["settings"]["segmentation"] == {"mode": "auto", "max_vectors": None}
    assert Path(project["project_dir"]) == parent / "fresh"
    assert page.errors() == []


def test_a_chunk_preset_without_a_budget_source_is_explained_in_the_dialog(page_factory, tmp_path):
    page = page_factory()
    body = _new_project_dialog(page)
    Select(body.find_element(By.NAME, "input_form")).select_by_value("stil")
    _set(body, "stil", str(RUNNER_STIL))
    _set(body, "directory", str(tmp_path / "budget"))
    Select(body.find_element(By.NAME, "completion")).select_by_value("hold")
    Select(body.find_element(By.NAME, "wait_preset")).select_by_value("chunk-4ms")
    Select(body.find_element(By.NAME, "service_scope")).select_by_value("non-loop")
    body.find_element(By.NAME, "service_source").clear()
    page.d.find_element(By.ID, "formDialogOk").click()
    page.until(lambda: page.d.find_element(By.ID, "formDialogError").is_displayed(), what="error shown")
    assert "Budget source is required" in page.text("formDialogError")
    assert page.dialog_open("formDialog") and not (tmp_path / "budget").exists()
    _set(body, "service_source", "declared, not measured")
    project = _create(page)
    assert project["settings"]["wait"]["service_source"] == "declared, not measured"


def test_ate_input_is_preprocessed_from_the_dialog_and_a_copy_keeps_its_record(page_factory, tmp_path):
    from test_stpgview_runner_inputs import ATE_PATTERN, ATE_PROJECT

    (tmp_path / "proj_project.stil").write_text(ATE_PROJECT)
    (tmp_path / "pat.stil").write_text(ATE_PATTERN)
    page = page_factory()
    body = _new_project_dialog(page)
    ate_field = _field(body, "ATE project file")
    assert not ate_field.is_displayed()
    Select(body.find_element(By.NAME, "input_form")).select_by_value("ate-pair")
    assert ate_field.is_displayed() and _field(body, "ATE pattern file").is_displayed()
    assert "default pin map" in _field(body, "Pin map (optional)").text
    _set(body, "stil", str(tmp_path / "pat.stil"))
    _set(body, "ate_project", str(tmp_path / "proj_project.stil"))
    _set(body, "directory", str(tmp_path / "ate-project"))
    Select(body.find_element(By.NAME, "completion")).select_by_value("hold")
    Select(body.find_element(By.NAME, "wait_preset")).select_by_value("none")
    project = _create(page)
    assert project["preprocess"]["form"] == "ate-pair"
    assert "preprocessed from\nproj_project.stil + pat.stil" in page.text("rpProjectBody")  # a dt/dd pair
    page.shot("ate-project")
    page.button("Copy to a new project…", page.d.find_element(By.ID, "rpProjectBody")).click()
    page.until(lambda: page.dialog_open("formDialog"), what="copy dialog")
    assert page.text("formDialogTitle") == "Copy to a new runner project"
    assert _chosen(body, "input_form") == "stil"
    assert body.find_element(By.NAME, "stil").get_attribute("value") == str(tmp_path / "ate-project" / "inputs" / "pattern.stil")
    assert body.find_element(By.NAME, "directory").get_attribute("value") == str(tmp_path / "ate-project") + "-copy"
    assert _chosen(body, "segmentation") == "auto"
    _set(body, "max_segment_vectors", "2")
    copy = _create(page)
    assert copy["settings"]["segmentation"] == {"mode": "auto", "max_vectors": 2}
    assert copy["preprocess"]["copied_from"] == str(tmp_path / "ate-project")
    assert "record copied from the original project" in page.text("rpProjectBody")
    assert page.errors() == []


def _open_project(page: Page, path: str) -> None:
    page.inject(f"window.__opened = false; openProject({json.dumps(path)}).then(() => {{ window.__opened = true; }});")
    page.until(lambda: page.js("return window.__opened === true"), what="project open")


def _build_on_page(page: Page, timeout: float = 120) -> str:
    page.d.find_element(By.ID, "rpBuild").click()
    page.until(lambda: page.js("const e = window.__R.jobs.build; return e && e.job.status !== 'running'"),
               timeout=timeout, what="build finished")
    return page.js("return window.__R.jobs.build.job.status")


def test_a_default_automatic_project_builds_a_pattern_too_long_for_one_package_and_shows_the_segment_count(
        page_factory, tmp_path):
    from test_stpgview_runner_inputs import _long_raw

    page = page_factory()
    body = _new_project_dialog(page)
    Select(body.find_element(By.NAME, "input_form")).select_by_value("stil")
    _set(body, "stil", str(_long_raw(tmp_path, 8000)))  # 12 000 raw events: more than one direct package holds
    _set(body, "directory", str(tmp_path / "auto"))
    Select(body.find_element(By.NAME, "completion")).select_by_value("hold")
    Select(body.find_element(By.NAME, "wait_preset")).select_by_value("none")
    page.choose("#formDialogBody select[name=compile_mode]", "raw-event")
    page.choose("#formDialogBody select[name=loop_policy]", "expand")
    # Segmentation is left as preselected: automatic, no vector count.
    project = _create(page)
    assert project["settings"]["segmentation"] == {"mode": "auto", "max_vectors": None}
    assert _build_on_page(page) == "done", page.text("rpBuildBody")
    result = page.js("return window.__R.jobs.build.job.result")
    assert (result["package_kind"], result["segment_count"]) == ("segmented", 2)
    text = page.text("rpBuildBody")
    assert "segmentation decision · segmented; reason=capacity; vectors=8000" in text
    assert "\npackage\nsegmented: 2 segments (one upload/RUN each; compare results per segment)\n" in text  # a dt/dd pair
    assert page.js("return window.__R.bundle.package_kind") == "segmented"
    page.until(lambda: page.js("return (window.__R.bundle.patterns || []).length") == 2
               and "package\nsegmented" in page.text("rpProjectBody"), what="the bundle card lists two segments")
    page.js("document.getElementById('rpBuildBody').scrollIntoView()")
    page.shot("auto-segmented-build")
    assert page.errors() == []


def test_a_project_without_a_segmentation_mode_is_labelled_and_its_copy_chooses_one(page_factory, tmp_path):
    page = page_factory()
    legacy = page.svc.init_project({"input_form": "stil", "stil": str(RUNNER_STIL), "directory": str(tmp_path / "old"),
                                    "wait_preset": "none", "completion": "hold", "storage": "psram",
                                    "compile_mode": "auto", "loop_policy": "preserve"})  # an API caller without a mode
    assert legacy["settings"]["segmentation"] == {"max_vectors": None}
    _open_project(page, legacy["project_path"])
    assert "Segmentation has no mode" in page.text("rpProjectBody") and "always one package" in page.text("rpProjectBody")
    page.button("Copy to a new project…", page.d.find_element(By.ID, "rpProjectBody")).click()
    page.until(lambda: page.dialog_open("formDialog"), what="copy dialog")
    body = page.d.find_element(By.ID, "formDialogBody")
    assert _chosen(body, "segmentation") == "single"  # the old meaning, now stated explicitly
    assert "made before automatic segmentation" in body.text
    page.choose("#formDialogBody select[name=segmentation]", "auto")
    copy = _create(page)
    assert copy["settings"]["segmentation"] == {"mode": "auto", "max_vectors": None}
    assert "Segmentation has no mode" not in page.text("rpProjectBody")
    assert page.svc.open_project(legacy["project_path"])["settings"]["segmentation"] == {"max_vectors": None}
    assert page.errors() == []


def test_a_build_that_does_not_fit_one_package_offers_a_segmented_copy(page_factory, tmp_path):
    from test_stpgview_runner_inputs import _long_raw

    page = page_factory()
    info = page.svc.init_project({"input_form": "stil", "stil": str(_long_raw(tmp_path, 8000)),
                                  "directory": str(tmp_path / "long"), "wait_preset": "none", "completion": "hold",
                                  "storage": "psram", "compile_mode": "raw-event", "loop_policy": "expand",
                                  "segmentation": "single"})
    _open_project(page, info["project_path"])
    assert _build_on_page(page, timeout=90) == "failed"
    page.until(lambda: "does not fit in one package" in page.text("rpBuildBody"), what="hint shown")
    text = page.text("rpBuildBody")
    assert "This project is set to one package (Segmentation = single)." in text
    assert text.count("raw-event: event_count out of range") == 1  # the reasons once, not repeated by the GUI
    page.js("document.getElementById('rpBuildBody').scrollIntoView()")
    page.shot("build-needs-segmentation")
    page.button("Copy to a new project with automatic segmentation…", page.d.find_element(By.ID, "rpBuildBody")).click()
    page.until(lambda: page.dialog_open("formDialog"), what="copy dialog")
    body = page.d.find_element(By.ID, "formDialogBody")
    assert _chosen(body, "segmentation") == "auto"
    assert body.find_element(By.NAME, "max_segment_vectors").get_attribute("value") == ""
    assert [_chosen(body, n) for n in ("compile_mode", "loop_policy", "completion", "wait_preset")] == \
        ["raw-event", "expand", "hold", "none"]
    copy = _create(page)
    assert copy["settings"]["segmentation"] == {"mode": "auto", "max_vectors": None}
    assert page.js("return !window.__R.jobs.build")  # the old project's failed build is not shown with the copy
    assert "does not fit" not in page.text("rpBuildBody")
    assert _build_on_page(page, timeout=90) == "done", page.text("rpBuildBody")
    assert page.js("return window.__R.bundle.package_kind") == "segmented"
    assert "\npackage\nsegmented: 2 segments (" in page.text("rpBuildBody")
    assert page.errors() == []


def test_a_failed_build_is_fixed_with_edit_settings_in_the_same_project(page_factory, tmp_path):
    from test_stpgview_runner_inputs import _long_raw

    page = page_factory()
    info = page.svc.init_project({"input_form": "stil", "stil": str(_long_raw(tmp_path, 8000)),
                                  "directory": str(tmp_path / "long"), "wait_preset": "none", "completion": "hold",
                                  "storage": "psram", "compile_mode": "raw-event", "loop_policy": "expand",
                                  "segmentation": "single"})
    _open_project(page, info["project_path"])
    assert _build_on_page(page, timeout=90) == "failed"
    page.until(lambda: page.d.find_elements(By.ID, "rpBuildEdit"), what="Edit settings offered after the failure")
    page.click("rpBuildEdit")
    page.until(lambda: page.dialog_open("formDialog"), what="edit dialog")
    assert page.text("formDialogTitle") == "Edit project settings"
    body = page.d.find_element(By.ID, "formDialogBody")
    assert not body.find_elements(By.NAME, "stil") and not body.find_elements(By.NAME, "directory")
    assert info["project_path"] in body.text and page.text("formDialogOk") == "Save"
    assert [_chosen(body, n) for n in ("compile_mode", "loop_policy", "segmentation")] == ["raw-event", "expand", "single"]
    page.choose("#formDialogBody select[name=segmentation]", "auto")
    saved = _create(page)
    assert saved["project_path"] == info["project_path"]  # the same project, not a copy
    assert saved["settings"]["segmentation"] == {"mode": "auto", "max_vectors": None}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["long", "long8000.stil"]  # no new folder
    text = page.text("rpProjectBody")
    assert "settings used" in text and "raw-event · expand · exact timing · no wait · seg single · psram · hold" in text
    assert "(= current settings)" not in text  # the failed attempt used the old settings
    assert page.js("return !window.__R.jobs.build")  # the old failure is no longer shown as current
    assert _build_on_page(page, timeout=90) == "done", page.text("rpBuildBody")
    assert page.js("return window.__R.bundle.package_kind") == "segmented"
    page.until(lambda: "(= current settings)" in page.text("rpProjectBody"), what="the successful attempt is current")
    button = page.d.find_element(By.ID, "rpEditSettings")
    assert not button.is_enabled() and "successful build" in button.get_attribute("title")
    assert "Edit settings is not available: it has a successful build" in page.text("rpProjectBody")
    page.js("document.getElementById('rpProjectBody').scrollIntoView()")
    page.shot("edit-settings-after-failure")
    assert page.errors() == []


def test_the_wait_scope_is_chosen_for_preserve_and_follows_expand(page_factory, tmp_path):
    """Preserve: the compiled pattern decides (the build names a wrong scope), so the dialog picks none.
    Expand never keeps a Loop block: non-loop is suggested and another choice is flagged."""

    page = page_factory()
    prefix = RUNNER_STIL.read_text().split("Pattern MAIN")[0]
    source = tmp_path / "loops.stil"
    source.write_text(prefix + "Pattern MAIN {\n  W NORMAL;\n  Loop 3 {\n    V { ALL = 01; }\n    V { ALL = 10; }\n  }\n"
                      "  V { ALL = 1X; }\n}\n")
    info = page.svc.init_project({"input_form": "stil", "stil": str(source), "directory": str(tmp_path / "p"),
                                  "wait_preset": "chunk-12ms", "service_us": "8000", "service_scope": "non-loop",
                                  "service_source": "declared, not measured", "max_total_wait_us": "10000000",
                                  "completion": "hold", "storage": "psram", "compile_mode": "rate-vector",
                                  "loop_policy": "preserve", "segmentation": "auto"})
    assert info["stil_loops"] == 1
    _open_project(page, info["project_path"])
    page.click("rpEditSettings")
    page.until(lambda: page.dialog_open("formDialog"), what="edit dialog")
    body = page.d.find_element(By.ID, "formDialogBody")
    # The saved scope is kept; with preserve the dialog does not claim to know the answer.
    assert _chosen(body, "service_scope") == "non-loop"
    assert "the build expects scope" not in body.text
    assert "With preserve it depends on the selected pattern, so you choose it." in body.text
    assert "The STIL text has 1 line starting with Loop (a text count" in body.text
    assert "A wrong scope is rejected by Build, which names the scope it found." in body.text
    page.choose("#formDialogBody select[name=loop_policy]", "expand")  # non-loop is right for expand
    assert "the build expects scope" not in page.d.find_element(By.ID, "formDialogBody").text
    page.choose("#formDialogBody select[name=service_scope]", "loop-generic")  # chosen: kept, and flagged
    page.until(lambda: "With loop policy expand the build expects scope non-loop; loop-generic is rejected."
               in page.d.find_element(By.ID, "formDialogBody").text, what="expand mismatch named")
    page.d.find_element(By.CSS_SELECTOR, "#formDialog button[value=cancel]").click()
    page.until(lambda: not page.dialog_open("formDialog"), what="dialog closed")
    # New project: no scope for preserve; expand suggests non-loop; back to preserve withdraws the suggestion.
    body = _new_project_dialog(page)
    Select(body.find_element(By.NAME, "wait_preset")).select_by_value("chunk-4ms")
    page.d.execute_script("arguments[0].dispatchEvent(new Event('change'))", body.find_element(By.NAME, "wait_preset"))
    assert _chosen(body, "loop_policy") == "preserve" and _chosen(body, "service_scope") == ""
    page.choose("#formDialogBody select[name=loop_policy]", "expand")
    page.until(lambda: _chosen(page.d.find_element(By.ID, "formDialogBody"), "service_scope") == "non-loop",
               what="suggested scope follows expand")
    page.choose("#formDialogBody select[name=loop_policy]", "preserve")
    page.until(lambda: _chosen(page.d.find_element(By.ID, "formDialogBody"), "service_scope") == "",
               what="the suggestion is withdrawn for preserve")
    assert page.errors() == []


def test_a_stil_without_loop_takes_non_loop_and_a_scope_failure_offers_the_fix(page_factory, tmp_path):
    """User report (2026-11-03): gui_counter_long.stil with chunk-4ms and Scope = loop-generic failed at the wait
    plan ("service profile scope 'loop-generic' does not match artifact scope 'non-loop'"), twice. That is the SDK's
    rule; the dialog let the choice through. A STIL without any Loop statement can only be non-loop: the dialog now
    suggests it, refuses loop-generic at Create/Save, and a failed build offers the scope it found."""

    page = page_factory()
    stil = tmp_path / "gui_counter_long.stil"
    stil.write_text(PATTERNS["gui_counter_long.stil"])
    pins = tmp_path / "pins.json"
    pins.write_text(PATTERNS["gui_counter.pins.json"])
    body = _new_project_dialog(page)
    page.choose("#formDialogBody select[name=input_form]", "stil")
    _set(body, "stil", str(stil))
    page.d.execute_script("arguments[0].dispatchEvent(new Event('change'))", body.find_element(By.NAME, "stil"))
    _set(body, "directory", str(tmp_path / "new"))
    page.choose("#formDialogBody select[name=completion]", "release")
    page.choose("#formDialogBody select[name=wait_preset]", "chunk-4ms")
    page.until(lambda: _chosen(page.d.find_element(By.ID, "formDialogBody"), "service_scope") == "non-loop",
               what="non-loop suggested for a STIL without Loop")
    assert "This STIL has no Loop statement" in page.d.find_element(By.ID, "formDialogBody").text
    page.choose("#formDialogBody select[name=service_scope]", "loop-generic")
    page.until(lambda: "the build expects scope non-loop; loop-generic is rejected"
               in page.d.find_element(By.ID, "formDialogBody").text, what="mismatch named")
    page.d.find_element(By.ID, "formDialogOk").click()
    page.until(lambda: page.d.find_element(By.ID, "formDialogError").is_displayed(), what="Create refused")
    assert "choose Scope = non-loop" in page.text("formDialogError") and not (tmp_path / "new").exists()
    page.d.find_element(By.CSS_SELECTOR, "#formDialog button[value=cancel]").click()
    page.until(lambda: not page.dialog_open("formDialog"), what="dialog closed")
    # A project made with loop-generic (as before this version): the failed build offers the scope it found.
    info = page.svc.init_project({"input_form": "stil", "stil": str(stil), "pin_map": str(pins),
                                  "directory": str(tmp_path / "old"), "wait_preset": "chunk-4ms", "service_us": "4000",
                                  "service_scope": "loop-generic", "service_source": "declared, not measured",
                                  "max_total_wait_us": "100000000000", "completion": "release", "storage": "psram",
                                  "compile_mode": "auto", "loop_policy": "preserve", "segmentation": "auto"})
    _open_project(page, info["project_path"])
    assert _build_on_page(page) == "failed"
    assert "does not match artifact scope 'non-loop'" in page.text("rpBuildBody")
    page.click("rpBuildScope")
    page.until(lambda: page.dialog_open("formDialog"), what="edit dialog with the found scope")
    assert _chosen(page.d.find_element(By.ID, "formDialogBody"), "service_scope") == "non-loop"
    page.d.find_element(By.ID, "formDialogOk").click()
    page.until(lambda: not page.dialog_open("formDialog"), what="settings saved")
    assert _build_on_page(page) == "done"
    assert page.errors() == []


# ---- timing choice (SDK 6a329215) ----------------------------------------------------------------

def _off_grid(tmp_path: Path) -> Path:
    """The runner fixture with its compare strobe at 103 ns (not on the 5 ns tick grid)."""

    prefix = RUNNER_STIL.read_text().split("Pattern MAIN")[0].replace("100ns", "103ns")
    body = "".join(f"  V {{ ALL = {'1X' if i % 2 else '01'}; }}\n" for i in range(40))
    source = tmp_path / "off.stil"
    source.write_text(prefix + "Pattern MAIN {\n  W NORMAL;\n" + body + "  Loop 3 {\n    V { ALL = 01; }\n"
                      "    V { ALL = 1X; }\n  }\n}\n")
    return source


def test_timing_is_exact_by_default_and_approx_rounds_in_the_same_project(page_factory, tmp_path):
    page = page_factory()
    body = _new_project_dialog(page)
    timing = _field(body, "Timing")
    assert _chosen(body, "allow_approx") == "false"
    assert [o.text for o in Select(body.find_element(By.NAME, "allow_approx")).options] == \
        ["Exact ticks (default)", "Approx — allow nearest-tick rounding"]
    assert "Build rejects a STIL off that grid and lists the times" in timing.text
    Select(body.find_element(By.NAME, "input_form")).select_by_value("stil")
    _set(body, "stil", str(_off_grid(tmp_path)))
    _set(body, "directory", str(tmp_path / "p"))
    Select(body.find_element(By.NAME, "completion")).select_by_value("hold")
    Select(body.find_element(By.NAME, "wait_preset")).select_by_value("none")
    created = _create(page)
    assert created["settings"]["compile"]["allow_approx"] is False
    # Exact: the build stops before any package and lists the off-grid times with what they would become.
    assert _build_on_page(page, timeout=90) == "failed"
    text = page.text("rpBuildBody")
    assert "ERR_APPROXIMATION_REQUIRED: source timing is off the target tick grid" in text
    assert "Off the target tick grid (5 ns per tick)" in text
    assert "time 103ns quantized to 21 ticks (= 105 ns)" in text
    assert "choose Timing = Approx with Edit settings…" in text
    page.until(lambda: page.d.find_elements(By.ID, "rpBuildEdit"), what="Edit settings after the failure")
    page.click("rpBuildEdit")
    page.until(lambda: page.dialog_open("formDialog"), what="edit dialog")
    assert _chosen(page.d.find_element(By.ID, "formDialogBody"), "allow_approx") == "false"
    page.choose("#formDialogBody select[name=allow_approx]", "true")
    saved = _create(page)
    assert saved["settings"]["compile"]["allow_approx"] is True
    assert "settings used" in page.text("rpProjectBody") and "exact timing" in page.text("rpProjectBody")
    assert _build_on_page(page, timeout=90) == "done", page.text("rpBuildBody")
    assert "\ntiming\napprox: " in page.text("rpBuildBody")
    view = page.until(lambda: page.d.find_elements(By.ID, "rpTiming"), what="timing view")[0]
    rows = [[c.text for c in r.find_elements(By.TAG_NAME, "td")] for r in view.find_elements(By.CSS_SELECTOR, "tbody tr")]
    assert [r[:6] for r in rows] == [["edge", "NORMAL", "D1", "103ns", "105 ns", "2.0"]]
    records = page.js("return window.__R.bundle.timing.records")
    assert rows[0][6] == f"{records:,}" and records > 0
    assert "The run and the bundle in the viewer use the emitted times" in view.text
    # Two attempts with two cache keys, each with the timing it used.
    project = page.text("rpProjectBody")
    assert "quantized timing" in project and "exact timing" in project
    keys = page.js("return window.__R.project.reservations.map((r) => [r.status, r.key, r.settings.allow_approx])")
    assert sorted(k[0] for k in keys) == ["FAILED", "HOST_ONLY_VERIFIED"] and len({k[1] for k in keys}) == 2
    assert {k[0]: k[2] for k in keys} == {"FAILED": False, "HOST_ONLY_VERIFIED": True}
    page.js("document.getElementById('rpTiming').scrollIntoView()")
    page.shot("timing-approx-rounded")
    # A copy keeps the choice.
    page.button("Copy to a new project…", page.d.find_element(By.ID, "rpProjectBody")).click()
    page.until(lambda: page.dialog_open("formDialog"), what="copy dialog")
    assert _chosen(page.d.find_element(By.ID, "formDialogBody"), "allow_approx") == "true"
    _set(page.d.find_element(By.ID, "formDialogBody"), "directory", str(tmp_path / "copy"))
    copy = _create(page)
    assert copy["settings"]["compile"]["allow_approx"] is True
    assert page.errors() == []


def test_an_older_project_without_the_timing_field_is_exact_and_stays_so(page_factory, tmp_path):
    from stpg_runner.artifacts import read_json, write_json

    page = page_factory()
    info = page.svc.init_project({"input_form": "stil", "stil": str(RUNNER_STIL), "directory": str(tmp_path / "p"),
                                  "wait_preset": "none", "completion": "hold", "storage": "psram",
                                  "compile_mode": "auto", "loop_policy": "preserve", "segmentation": "auto"})
    path = Path(info["project_path"])
    old = read_json(path)
    del old["compile"]["allow_approx"]
    write_json(path, old)
    _open_project(page, str(path))
    assert "Timing: exact. The project file has no compile.allow_approx" in page.text("rpProjectBody")
    page.click("rpEditSettings")
    page.until(lambda: page.dialog_open("formDialog"), what="edit dialog")
    assert _chosen(page.d.find_element(By.ID, "formDialogBody"), "allow_approx") == "false"
    Select(page.d.find_element(By.CSS_SELECTOR, "#formDialogBody select[name=completion]")).select_by_value("release")
    _create(page)
    stored = read_json(path)
    assert "allow_approx" not in stored["compile"] and stored["execution"]["completion"] == "release"
    assert _build_on_page(page, timeout=90) == "done", page.text("rpBuildBody")
    assert "\ntiming\nexact ticks" in page.text("rpBuildBody")
    assert page.errors() == []


def test_a_loop_pattern_in_segments_names_the_scope_and_builds_with_it(page_factory, tmp_path):
    """The user's case: preserve + a chunk wait + automatic segments, where some segments keep the Loop."""

    page = page_factory()
    prefix = RUNNER_STIL.read_text().split("Pattern MAIN")[0].replace("Period '400ns'", "Period '20000ns'")
    plain = "\n".join(f"  V {{ ALL = {'01' if i % 2 else '10'}; }}" for i in range(9000))
    source = tmp_path / "plain_then_loop.stil"
    source.write_text(prefix + "Pattern MAIN {\n  W NORMAL;\n" + plain + "\n  Loop 10 {\n    V { ALL = 01; }\n"
                      "    V { ALL = 10; }\n  }\n}\n")
    info = page.svc.init_project({"input_form": "stil", "stil": str(source), "directory": str(tmp_path / "p"),
                                  "wait_preset": "chunk-12ms", "service_us": "8000", "service_scope": "non-loop",
                                  "service_source": "declared, not measured", "max_total_wait_us": "10000000",
                                  "completion": "hold", "storage": "psram", "compile_mode": "rate-vector",
                                  "loop_policy": "preserve", "segmentation": "auto", "max_segment_vectors": "3000"})
    _open_project(page, info["project_path"])
    assert _build_on_page(page, timeout=300) == "failed"
    text = page.text("rpBuildBody")
    assert "service profile scope 'non-loop' does not match whole segmented sequence scope 'loop-generic'" in text
    assert "At least one segment keeps a Loop block, so this build needs Scope = loop-generic" in text
    page.click("rpBuildEdit")
    page.until(lambda: page.dialog_open("formDialog"), what="edit dialog")
    page.choose("#formDialogBody select[name=service_scope]", "loop-generic")
    _create(page)
    assert _build_on_page(page, timeout=300) == "done", page.text("rpBuildBody")
    count = page.js("return window.__R.jobs.build.job.result.segment_count")
    assert count >= 3 and f"segmented: {count} segments (" in page.text("rpBuildBody")
    assert page.errors() == []
