"""Browser checks of the Viewer page (host only): activity, long tables, columns, colours, Build in Runner, edits.

Real HTTP server and headless Firefox, as in ``test_stpgview_browser.py`` (skipped without selenium, Firefox or
geckodriver). They check what the user reported: a click whose work takes long shows that it is running (also an
open started by a double-click, which no button marks), the vector table reaches every row of a long pattern, the
rate column fits its names and every column can be resized, rates and signals can be coloured, the open STIL goes
to the Runner's New project dialog instead of the old Convert dialog, and the new edit operations work from the page.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

import pytest

webdriver = pytest.importorskip("selenium.webdriver")
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException  # noqa: E402
from selenium.webdriver.common.action_chains import ActionChains  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.support.ui import Select  # noqa: E402

from stpgview.physical.patterns import counter, counter_pins  # noqa: E402
from stpgview.server import ViewerServer, make_runner  # noqa: E402
from stpgview.session import Session  # noqa: E402

from stpg_hardware_guard import forbid_hardware  # noqa: E402
from test_stpgview import ATE_PATTERN, ATE_PROJECT  # noqa: E402
from test_stpgview_browser import browser  # noqa: E402,F401 - the module's Firefox fixture

pytestmark = pytest.mark.stpg_host_only
F8 = Path(__file__).parent / "fixtures" / "stil" / "vf_c10lp_f8_bidir_turnaround_200ns.stil"


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


class ViewerPage:
    def __init__(self, driver, srv) -> None:
        self.d, self.srv, self.session = driver, srv, srv.session
        host, port = srv.server_address[:2]
        self.url = f"http://{host}:{port}/?token=tok"

    def open(self) -> "ViewerPage":
        self.d.get(self.url)
        self.until(lambda: self.d.execute_script("return document.readyState") == "complete")
        self.js("localStorage.clear()")
        self.d.get(self.url)  # again, without remembered columns, colours or mode
        self.until(lambda: self.d.execute_script("return document.readyState") == "complete")
        if not self.js("return document.querySelector('.workspace').hidden === false"):
            self.d.find_element(By.ID, "modeView").click()
        self.inject("window.__errors = []; window.addEventListener('error', (e) => window.__errors.push(String(e.message)));"
                    "window.addEventListener('unhandledrejection', (e) => window.__errors.push(String(e.reason)));"
                    "window.__S = S;")
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
            except (NoSuchElementException, StaleElementReferenceException):  # a panel re-rendered meanwhile
                value = None
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    def load(self, *paths: Path, pin_map: Path | None = None) -> None:
        """Open files through the Open dialog's own submit (the path a double-click takes too)."""

        self.d.find_element(By.ID, "openBtn").click()
        self.js("document.getElementById('openPaths').value = arguments[0];"
                "document.getElementById('openPinMap').value = arguments[1];",
                "\n".join(str(p) for p in paths), str(pin_map or ""))
        self.inject("window.__opened = false; submitOpen().then(() => { window.__opened = true; });")
        self.until(lambda: self.js("return window.__opened === true && !document.getElementById('openDialog').open"),
                   what="pattern open")
        self.until(lambda: self.js("return !!(window.__S.meta && window.__S.wave)"), what="waveform")

    def press(self, label: str) -> None:
        """Click an Edit panel button once the panel shows the edit state of the trace on screen (it re-renders
        when that state arrives, which would replace a button found before)."""

        self.until(lambda: self.js("return !!(window.__S.edit && window.__S.meta)"), what="edit state")

        def pressed():
            for button in self.d.find_elements(By.CSS_SELECTOR, "#editPanel button"):
                if button.text == label:
                    button.click()
                    return True
            return False

        self.until(pressed, what=f"press {label}")

    def errors(self) -> list[str]:
        return self.js("return window.__errors")


@pytest.fixture
def viewer(browser, tmp_path):  # noqa: F811 - the imported fixture
    servers = []

    def make() -> ViewerPage:
        session = Session(tmp_path / f"work{len(servers)}")
        runner = make_runner(session, host="127.0.0.1", demo=True, config_dir=tmp_path / "cfg",
                             results_dir=tmp_path / "runs")
        srv = ViewerServer(("127.0.0.1", 0), session, "tok", runner)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return ViewerPage(browser, srv).open()

    yield make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _counter(tmp_path: Path, n: int, rate: str = "NORMAL") -> tuple[Path, Path]:
    stil = tmp_path / f"counter{n}.stil"
    stil.write_text(counter(n).replace("NORMAL", rate))
    pins = tmp_path / "counter.pins.json"
    pins.write_text(counter_pins())
    return stil, pins


def test_an_open_that_takes_long_shows_the_activity_until_it_is_done(viewer, tmp_path, monkeypatch):
    page = viewer()
    stil, _pins = _counter(tmp_path, 400)
    gate, original = threading.Event(), page.session.load

    def held(*args, **kwargs):
        gate.wait(30)
        return original(*args, **kwargs)

    monkeypatch.setattr(page.session, "load", held)
    page.d.find_element(By.ID, "openBtn").click()
    page.js("document.getElementById('openPaths').value = arguments[0]", str(stil))
    page.inject("submitOpen();")  # what a double-click on a file does: no button was clicked
    activity = page.d.find_element(By.ID, "activity")
    page.until(lambda: activity.is_displayed() and "Opening the pattern" in activity.text, timeout=3, what="activity")
    page.until(lambda: activity.text.endswith(("1 s", "2 s", "3 s")), timeout=5, what="seconds")
    submit = page.d.find_element(By.ID, "openSubmit")
    assert "busy" in submit.get_attribute("class").split() and submit.get_attribute("disabled")
    assert page.js("return document.body.classList.contains('working')")
    gate.set()
    page.until(lambda: not activity.is_displayed(), what="activity gone")
    # The open's activity ends when its answer arrives; the page then asks for the state, so wait for the pattern.
    page.until(lambda: page.js("return window.__S.meta && window.__S.meta.vector_count") == 400, what="the pattern")
    assert page.errors() == []


def test_the_table_reaches_every_row_of_a_table_taller_than_the_browser_allows(viewer, tmp_path):
    page = viewer()
    stil, _pins = _counter(tmp_path, 5000)
    page.load(stil)
    page.inject("TABLE_MAX_PX = 20000; setupTable();")  # 5,000 rows x 20 px = 100,000 px > the cap
    assert page.js("return document.getElementById('tableSpacer').offsetHeight") == 20000

    def rows():
        return page.js("""const box = document.getElementById('tableScroll').getBoundingClientRect();
            return [...document.querySelectorAll('#tableRows .trow')].filter((r) => {
              const b = r.getBoundingClientRect(); return b.top >= box.top - 1 && b.bottom <= box.bottom + 1; })
              .map((r) => Number(r.dataset.i));""")

    page.js("const b = document.getElementById('tableScroll'); b.scrollTop = b.scrollHeight;")
    page.until(lambda: rows() and rows()[-1] == 4999, what="last row visible")
    page.js("scrollTableTo(2500); renderTable();")
    page.until(lambda: 2500 in rows(), what="row 2500 visible")
    page.js("const b = document.getElementById('tableScroll'); b.scrollTop = 0;")
    page.until(lambda: rows() and rows()[0] == 0, what="first row visible")
    page.inject("selectVector(3210, { center: true });")
    page.until(lambda: 3210 in rows(), what="selected row scrolled into view")
    assert page.errors() == []


def test_the_rate_column_fits_long_rate_names_and_columns_resize(viewer, tmp_path):
    page = viewer()
    stil, _pins = _counter(tmp_path, 300, rate="VERY_LONG_WAVEFORM_TABLE_NAME_FOR_THE_RATE_COLUMN")
    page.load(stil)
    page.until(lambda: page.js("return document.querySelectorAll('#tableRows .c-r').length > 0"), what="rows")
    assert page.js("const c = document.querySelector('#tableRows .c-r'); return c.scrollWidth <= c.clientWidth")
    before = page.js("return document.querySelector('#colMeta .c-t').getBoundingClientRect().width")
    grip = page.d.find_element(By.CSS_SELECTOR, "#colMeta .c-t .col-grip")
    ActionChains(page.d).click_and_hold(grip).move_by_offset(60, 0).release().perform()
    after = page.js("return document.querySelector('#colMeta .c-t').getBoundingClientRect().width")
    assert 50 <= after - before <= 70
    cell = page.js("return document.querySelector('#tableRows .c-t').getBoundingClientRect().width")
    assert abs(cell - after) < 1  # rows follow the header
    saved = json.loads(page.js("return localStorage.getItem('stpg-view-columns')"))
    assert abs(saved["t"] - after) < 1
    # double-click the edge: the column fits its contents again
    ActionChains(page.d).double_click(grip).perform()
    fitted = page.js("return document.querySelector('#colMeta .c-t').getBoundingClientRect().width")
    assert fitted < after and page.js("const c = document.querySelector('#tableRows .c-t'); return c.scrollWidth <= c.clientWidth")
    assert page.errors() == []


def test_rates_and_signals_can_be_coloured(viewer, tmp_path):
    page = viewer()
    stil, _pins = _counter(tmp_path, 300)
    page.load(stil)
    page.d.find_element(By.ID, "colorBtn").click()
    page.until(lambda: page.js("return document.getElementById('colorDialog').open"), what="colours dialog")
    page.d.find_element(By.ID, "colorAutoRates").click()
    page.d.find_element(By.ID, "colorAutoSignals").click()
    stored = json.loads(page.js("return localStorage.getItem('stpg-view-colors')"))
    assert stored["rates"]["NORMAL"] and set(stored["signals"]) == {"A0", "A1", "A2", "A3", "A4", "A5", "A6", "IOOE"}
    page.js("document.getElementById('colorDialog').close()")
    page.until(lambda: page.js("const c = document.querySelector('#tableRows .c-r'); return c && c.style.color !== ''"),
               what="coloured rate cell")
    assert page.js("return document.querySelectorAll('#charHead span.colored').length") == 8
    # right-click a signal name: the dialog opens on that signal
    canvas = page.d.find_element(By.ID, "wave")
    ActionChains(page.d).move_to_element_with_offset(canvas, -canvas.size["width"] // 2 + 60,
                                                     -canvas.size["height"] // 2 + 46 + 26 * 2 + 13).context_click().perform()
    page.until(lambda: page.js("return document.getElementById('colorDialog').open"), what="dialog from the name")
    assert page.js("return document.querySelector('#colorSignals li.focus').dataset.name") == "A2"
    page.js("const li = document.querySelector('#colorSignals li.focus'); li.querySelector('button').click();")
    assert "A2" not in json.loads(page.js("return localStorage.getItem('stpg-view-colors')"))["signals"]
    assert page.errors() == []


def _form_values():
    return """const out = {}; for (const e of document.querySelectorAll('#formDialogForm [name]')) out[e.name] = e.value;
              return out;"""


def test_build_in_runner_opens_a_new_project_for_the_open_stil(viewer, tmp_path):
    page = viewer()
    assert not page.d.find_elements(By.ID, "convertBtn") and not page.d.find_elements(By.ID, "convertDialog")
    stil, pins = _counter(tmp_path, 300)
    page.load(stil, pin_map=pins)
    page.d.find_element(By.ID, "runnerBtn").click()
    page.until(lambda: page.js("return document.getElementById('formDialog').open"), what="New project dialog")
    assert page.js("return document.getElementById('runnerPage').hidden") is False
    assert page.d.find_element(By.ID, "formDialogTitle").text == "New runner project"
    values = page.js(_form_values())
    assert values["input_form"] == "stil" and values["stil"] == str(stil) and values["pin_map"] == str(pins)
    assert values["directory"] == str(tmp_path / "counter300-project")
    page.js("document.getElementById('formDialog').close()")
    # an ATE-generator pair goes to the runner as the same pair (preprocessed at Create)
    (tmp_path / "proj.stil").write_text(ATE_PROJECT)
    (tmp_path / "pat.stil").write_text(ATE_PATTERN.replace("\\r2 1", "1H"))
    page.d.find_element(By.ID, "modeView").click()
    page.load(tmp_path / "pat.stil", tmp_path / "proj.stil")
    page.d.find_element(By.ID, "runnerBtn").click()
    page.until(lambda: page.js("return document.getElementById('formDialog').open"), what="New project dialog (ATE)")
    values = page.js(_form_values())
    assert values["input_form"] == "ate-pair" and values["stil"] == str(tmp_path / "pat.stil")
    assert values["ate_project"] == str(tmp_path / "proj.stil") and values["directory"] == str(tmp_path / "pat-project")
    assert page.errors() == []


def test_a_running_build_keeps_its_button_spinning_and_shows_in_the_activity(viewer):
    page = viewer()
    page.d.find_element(By.ID, "modeRun").click()
    page.inject("window.__R = R;")
    page.until(lambda: page.js("return !!window.__R.state"), what="runner state")
    page.inject("R.project = { project_path: '/p', inputs: {}, settings: {} };"
                "R.jobs.build = { job: { id: 'x', status: 'running', title: 'build p' }, events: [{ stage: 'segmented_compile', event: 'heartbeat', job_s: 1 }],"
                " next: 1, started: performance.now() - 4000 }; renderBuild(); renderActivity();")
    build = page.d.find_element(By.ID, "rpBuild")
    assert "running" in build.get_attribute("class").split() and build.get_attribute("disabled")
    activity = page.d.find_element(By.ID, "activity")
    page.until(lambda: "Build · segmented compile" in activity.text, what="build in the activity")
    page.inject("R.jobs.build.job.status = 'done'; renderBuild(); renderActivity();")
    page.until(lambda: not activity.is_displayed(), what="activity gone")
    assert "running" not in build.get_attribute("class").split()


def test_change_a_character_on_a_bidirectional_pin_and_export_the_stil(viewer, tmp_path):
    page = viewer()
    stil = tmp_path / "f8.stil"
    stil.write_text(F8.read_text())
    page.load(stil)
    io0 = page.js("return window.__S.meta.signals.findIndex((s) => s.name === 'io_0')")
    page.inject(f"S.selectedSignal = {io0}; selectVector(3);")
    page.until(lambda: page.js("return window.__S.selDetail && window.__S.selDetail.i === 3"), what="vector 3")
    page.until(lambda: page.js("return !!document.querySelector('#editPanel button')"), what="edit panel")
    page.press("Change character")
    page.until(lambda: page.js("return document.querySelectorAll('#editPanel select[name=char] option').length") == 6,
               what="character choices")
    Select(page.d.find_element(By.CSS_SELECTOR, "#editPanel select[name=char]")).select_by_value("H")
    note = page.d.find_element(By.CSS_SELECTOR, "#editPanel .edit-form .warn-text")
    assert "drive → compare" in note.text and "stops driving" in note.text
    page.d.find_element(By.CSS_SELECTOR, "#editPanel .edit-form button[type=submit]").click()
    page.until(lambda: page.js("return window.__S.meta && window.__S.meta.edit_of"), what="edited preview")
    page.until(lambda: page.js("return window.__S.selDetail && window.__S.selDetail.i === 3"), what="vector 3 again")
    assert page.js(f"return window.__S.selDetail.chars[{io0}]") == "H"
    # Export STIL…: the picker takes the file name
    page.press("Export STIL…")
    page.until(lambda: page.js("return document.getElementById('pickDialog').open"), what="path picker")
    target = tmp_path / "exported" / "mine.stil"
    page.js("document.getElementById('pickPath').value = arguments[0]", str(target))
    page.d.find_element(By.ID, "pickOk").click()
    page.until(lambda: target.is_file(), what="exported file")
    from stpgview.load_stil import load_stil

    assert load_stil(target).chars[3][io0] == "H" and stil.read_text() == F8.read_text()
    assert page.errors() == []


def test_add_and_rename_signals_from_the_page(viewer, tmp_path):
    page = viewer()
    stil, pins = _counter(tmp_path, 50)
    page.load(stil, pin_map=pins)
    page.press("Add signal")
    page.d.find_element(By.CSS_SELECTOR, "#editPanel input[name=signal]").send_keys("TP")
    Select(page.d.find_element(By.CSS_SELECTOR, "#editPanel select[name=direction]")).select_by_value("In")
    Select(page.d.find_element(By.CSS_SELECTOR, "#editPanel select[name=fill]")).select_by_value("1")
    page.d.find_element(By.CSS_SELECTOR, "#editPanel .edit-form button[type=submit]").click()
    page.until(lambda: page.js("return window.__S.meta && window.__S.meta.edit_of && window.__S.meta.signals.length") == 9,
               what="preview with the new signal")
    assert page.js("return window.__S.meta.signals[8]") == {"name": "TP", "bit": 7, "pin": "DUT7", "declared": "In"}  # the free bit
    page.until(lambda: page.js("return window.__S.edit && window.__S.edit.preview_id === window.__S.activeId"),
               what="edit state of the preview")
    page.press("Rename signal")
    Select(page.d.find_element(By.CSS_SELECTOR, "#editPanel select[name=signal]")).select_by_value("IOOE")
    page.d.find_element(By.CSS_SELECTOR, "#editPanel input[name=to]").send_keys("OE_N")
    page.d.find_element(By.CSS_SELECTOR, "#editPanel .edit-form button[type=submit]").click()
    page.until(lambda: "OE_N" in page.js("return window.__S.meta.signals.map((s) => s.name)"), what="renamed")
    names = page.js("return window.__S.meta.signals.map((s) => s.name)")
    assert names == ["A0", "A1", "A2", "A3", "A4", "A5", "A6", "OE_N", "TP"]
    assert page.errors() == []


def test_the_open_dialog_shows_the_roots_and_opens_one(viewer):
    page = viewer()
    page.d.find_element(By.ID, "openBtn").click()
    page.until(lambda: page.d.find_elements(By.CSS_SELECTOR, "#browseRoots button"), what="roots")
    roots = [b.text for b in page.d.find_elements(By.CSS_SELECTOR, "#browseRoots button")]
    assert roots and (os.name == "nt" or roots == ["/"])
    page.d.find_elements(By.CSS_SELECTOR, "#browseRoots button")[0].click()
    page.until(lambda: page.d.find_element(By.ID, "browsePath").get_attribute("value") == roots[0], what="root opened")
    assert page.errors() == []

