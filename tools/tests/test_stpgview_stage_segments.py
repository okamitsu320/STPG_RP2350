"""Production jobView in Node: acceptance belongs to an upload, not its predecessor.

Public synthetic event/journal fixtures only. This renders the actual JavaScript
with a minimal DOM; it is not a browser or physical-device test.
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest
import stpgview

pytestmark = pytest.mark.stpg_host_only

STUB = """
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
class Dom {
  constructor(tag, attrs, ...children) { this.tag = tag; this.attrs = attrs || {}; this.style = {};
    this.children = children.filter(x => x !== null && x !== undefined); }
  append(...children) { this.children.push(...children.filter(x => x !== null && x !== undefined)); }
  get firstChild() { return this.children[0]; }
  toString() { return this.children.map(String).join(' '); }
}
const el = (tag, attrs, ...children) => new Dom(tag, attrs, ...children);
const badge = text => new Dom('span', {}, text);
const notice = (_kind, title, lines=[]) => new Dom('p', {}, title, ...lines);
const STAGE_LABELS = {upload: 'upload', run: 'RUN', observe: 'observe (STATUS)', segment: 'segment'};
const PORT_CHECK_PHASES = {};
"""


def rows(events, *, running=False, stop=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for production-JS rendering")
    source = (Path(stpgview.__file__).parent / "static/runner.js").read_text(encoding="utf-8")
    start = source.index("function stagesOf(")
    body = source[start:source.index("\n// ---------------------------------------------------------------------------", start)]
    script = STUB + body + """
const box = jobView(input), out = [];
function visit(x) { if (!(x instanceof Dom)) return;
  if (x.tag === 'li') out.push([x.attrs.class || '', String(x)]);
  x.children.forEach(visit); }
visit(box); console.log(JSON.stringify(out));
"""
    job = {"status": "running" if running else "done", "elapsed_s": 30, "title": "synthetic run",
           "result": None if running else {"run": {"outcome": "REJECT"}, "stopped_at": stop}}
    result = subprocess.run([node, "-e", script],
                            input=json.dumps({"job": job, "events": events, "started": 0}),
                            text=True, encoding="utf-8", capture_output=True, timeout=10, check=True)
    return json.loads(result.stdout)


def event(stage, kind, *, index=None, **fields):
    detail = fields.pop("detail", "")
    if index is not None:
        detail = f"segment={index}; {detail}"
    return {"stage": stage, "event": kind, "job_s": 1, "detail": detail, **fields}


def finished_first_segment():
    return [event("upload", "start", completed=0, total=100),
            event("upload", "done", index=0, completed=100, total=100, detail="UPLOAD_END accepted"),
            event("run", "accepted", index=0, detail="RUN accepted; waiting for STATUS"),
            event("observe", "progress", index=0, completed=6, total=10),
            event("segment", "done", completed=1, total=2)]


@pytest.mark.parametrize("running", [False, True])
def test_later_upload_never_inherits_previous_segment_acceptance(running):
    events = finished_first_segment() + [event("upload", "start", completed=0, total=200),
                                         event("upload", "progress", completed=50, total=200)]
    view = rows(events, running=running)
    current = [row for row in view if "upload 50 / 200" in row[1]]
    assert len(current) == 1 and current[0][0] == ("running" if running else "failed"), view
    assert "accepted" not in current[0][1] and "from the result file" not in current[0][1]
    assert any(cls == "done" and "UPLOAD_END accepted (segment 1)" in text for cls, text in view)


def test_later_accepted_run_remains_after_its_own_upload_in_the_stage_list():
    events = finished_first_segment() + [event("upload", "start", completed=0, total=200),
        event("upload", "done", index=1, completed=200, total=200, detail="UPLOAD_END accepted"),
        event("run", "accepted", index=1, detail="RUN accepted; waiting for STATUS")]
    view = rows(events)
    accepted_runs = [(i, text) for i, (cls, text) in enumerate(view) if cls == "done" and "RUN accepted" in text]
    assert len(accepted_runs) == 2, view
    assert "segment 1" in accepted_runs[0][1] and "segment 2" in accepted_runs[1][1]
    upload = next(i for i, (_, text) in enumerate(view) if "UPLOAD_END accepted (segment 2)" in text)
    assert upload < accepted_runs[1][0]
    assert view[-1][0] == "recorded" and "how the run ended" in view[-1][1]


@pytest.mark.parametrize("latest_events", [False, True])
def test_later_journal_fallback_is_not_suppressed_by_an_earlier_acceptance(latest_events):
    events = finished_first_segment()
    if latest_events:
        events += [event("upload", "start", completed=0, total=200),
                   event("upload", "progress", completed=200, total=200)]
    stop = {"segment": 1, "upload_end": "OK · HOLD_AWAITING_NEXT", "run": "OK · RUNNING", "run_took_s": .001,
            "failed_request": "GET_STATUS", "response_bytes": 0, "failure_stage": "deadline", "failed_took_s": 20}
    view = rows(events, stop=stop)
    assert any(cls == "done" and "UPLOAD_END answered OK · HOLD_AWAITING_NEXT" in text for cls, text in view), view
    assert any(cls == "done" and "RUN answered OK · RUNNING" in text for cls, text in view), view
    failed = [text for cls, text in view if cls == "failed"]
    assert len(failed) == 1 and "GET_STATUS after RUN" in failed[0]


def test_non_run_build_stages_keep_their_existing_grouping():
    view = rows([event("source_analysis", "start"), event("source_analysis", "done"),
                 event("compile_package", "start"), event("compile_package", "done")])
    assert [text.split()[1] for _, text in view] == ["source_analysis", "compile_package"]


def test_legacy_earlier_upload_is_not_annotated_as_the_later_segment():
    # An older SDK emitted no upload/done or run/accepted, but its STATUS event
    # still identifies this upload as segment 0. The journal concerns segment 1.
    events = [event("upload", "start", completed=0, total=100),
              event("upload", "progress", completed=100, total=100),
              event("observe", "progress", index=0, completed=6, total=10),
              event("segment", "done", completed=1, total=2)]
    stop = {"segment": 1, "upload_end": "OK · HOLD_AWAITING_NEXT", "run": "OK · RUNNING", "run_took_s": .001,
            "failed_request": "GET_STATUS", "response_bytes": 0, "failure_stage": "deadline", "failed_took_s": 20}
    view = rows(events, stop=stop)
    earlier = next(text for _, text in view if "upload 100 / 100" in text)
    assert "HOLD_AWAITING_NEXT" not in earlier and "from the result file" not in earlier, view
    assert any("UPLOAD_END answered OK · HOLD_AWAITING_NEXT" in text for _, text in view)
