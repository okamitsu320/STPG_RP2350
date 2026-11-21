"""externalResultView, rendered from the production runner.js in Node (no browser): what the accessory result says.

The unadmitted-bytes notice must not claim when the bytes arrived (the SDK also counts bytes buffered before the
cancel), INCOMPLETE is explained, and a started process is never said to have released its device.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import stpgview

pytestmark = pytest.mark.stpg_host_only

STUBS = """
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
class Dom {
  constructor(...children) { this.children = children.filter(x => x !== null && x !== undefined); }
  append(...children) { this.children.push(...children.filter(x => x !== null && x !== undefined)); }
  toString() { return this.children.map(x => String(x)).join(' '); }
}
const el = (_tag, _attrs, ...children) => new Dom(...children);
const badge = (text) => new Dom(text);
const notice = (_type, title, lines=[]) => new Dom(title, ...lines);
const kvList = (rows) => new Dom(...rows.flat());
const copyable = (text) => text;
"""


def render(view: dict) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for production-JS rendering")
    source = (Path(stpgview.__file__).parent / "static" / "runner.js").read_text(encoding="utf-8")
    start = source.index("function externalResultView(")
    body = source[start:source.index("\nfunction renderRun(", start)]
    script = STUBS + body + "\nconsole.log(JSON.stringify(String(externalResultView(input))));"
    result = subprocess.run([node, "-e", script], input=json.dumps(view), text=True, capture_output=True,
                            check=True, timeout=10)
    return json.loads(result.stdout).lower()


def _view(**change):
    view = {"identifier": "public-peer", "mode": "device", "outcome": "CANCELLED", "adopted": False,
            "result_persisted": True, "cancel_requested": True, "errors": [], "returncode": 0, "result_file": None,
            "session_file": "accessory/session.json", "output_dir": "accessory", "hardware_effects_possible": True,
            "stderr_text": "", "unadmitted_stdout_bytes": 0}
    view.update(change)
    return view


def test_unadmitted_bytes_are_counted_without_an_arrival_claim():
    text = render(_view(unadmitted_stdout_bytes=57))
    assert "unadmitted bytes on cancellation" in text and "57 byte(s)" in text
    assert "can include bytes buffered before the cancel" in text and "does not establish their arrival time" in text
    assert "arrived after" not in text and "bounded received prefix" in text


def test_no_notice_without_unadmitted_bytes_and_no_release_claim():
    text = render(_view())
    assert "unadmitted" not in text and "cancel requested" in text
    assert "does not prove that its device or pins were released" in text


def test_incomplete_and_adopted_results_are_worded_apart():
    assert "accessory not asked to collect" in render(_view(outcome="INCOMPLETE", cancel_requested=False))
    adopted = render(_view(outcome="COMPLETED", adopted=True, cancel_requested=False, hardware_effects_possible=True))
    assert "data adopted (the accessory's own judgement)" in adopted and "not a dut pass" in adopted
    assert "does not prove that its device" not in adopted  # that notice is for results that did not complete
