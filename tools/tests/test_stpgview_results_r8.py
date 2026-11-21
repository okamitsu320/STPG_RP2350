"""GUI side of R8: readable results (STIL details, text), viewer fail marks, the fail-log read, bundle update.

Only public synthetic patterns and the simulated STPG are used. Nothing here opens a real port or enumerates USB.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path
import shutil
import time

import pytest

from stpg import constants as c
from stpg_runner import build_project, init_project, verify_bundle
from stpg_runner.artifacts import RunnerError, canonical_json, identity, read_json, write_json
from stpg_runner.build import BUNDLE_NAME
from stpgview.runner import service as service_module
from stpgview.runner.service import ACKNOWLEDGEMENTS, RunnerService, device_exchanges, run_stop_point
from stpgview.runner.simulated import SimulatedStpg
from stpgview.session import Session

from stpg_hardware_guard import forbid_hardware

pytestmark = pytest.mark.stpg_host_only
FIXTURE = Path(__file__).parent / "fixtures" / "runner" / "pattern.stil"
ACKS = {key: True for key in ACKNOWLEDGEMENTS}
FAIL_LOG_ACK = "fail_log_hold"  # the confirmation box of the fail-log read (named here: older GUIs lack it)


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


def compare_source(path: Path, vectors: int = 12) -> Path:
    """The public runner fixture with L/H compares on D1 (D0 drives)."""
    text = FIXTURE.read_text().replace("X: 100ns Compare(X);", "X: 100ns Compare(X); L: 100ns Compare(0); H: 100ns Compare(1);")
    body = "\n".join(f"  V {{ ALL = {i % 2}{'LH'[(i // 3) % 2]}; }}" for i in range(vectors))
    path.write_text(text[:text.index("Pattern MAIN")] + "Pattern MAIN {\n  W NORMAL;\n" + body + "\n}\n")
    return path


def wait(job, timeout=60.0):
    end = time.monotonic() + timeout
    while job.status == "running":
        if time.monotonic() > end:
            raise AssertionError("job did not finish")
        time.sleep(0.02)
    return job.public(0)


@pytest.fixture
def svc(tmp_path):
    return RunnerService(config_dir=tmp_path / "cfg", device_run=True, demo=True, results_dir=tmp_path / "runs",
                         session=Session(tmp_path / "work"))


def project(svc, tmp_path, *, completion="hold", source=None):
    stil = source or compare_source(tmp_path / "public.stil")
    info = svc.init_project({"stil": str(stil), "directory": str(tmp_path / "project"), "wait_preset": "none",
                             "completion": completion, "storage": "psram", "compile_mode": "rate-vector",
                             "loop_policy": "preserve"})
    done = wait(svc.start_build(info["project_path"]))
    assert done["status"] == "done", done["error"]
    return done["result"]["bundle"]


def run(svc, bundle, monkeypatch, peer_class=SimulatedStpg, **peer):
    monkeypatch.setattr(service_module, "SimulatedStpg", lambda: peer_class(**peer))
    prep = svc.prepare_run({"bundle": bundle["bundle_dir"], "manifest_sha256": bundle["manifest_sha256"],
                            "device": "simulated"})
    done = wait(svc.start_run({"nonce": prep["nonce"], "acknowledgements": {**ACKS, FAIL_LOG_ACK: True}}))
    assert done["status"] == "done", done["error"]
    return prep, done["result"]


# ---- confirmation -------------------------------------------------------------------------------

def test_the_fail_log_box_is_asked_for_a_plan_with_compares_and_required(svc, tmp_path, monkeypatch):
    bundle = project(svc, tmp_path)
    prep = svc.prepare_run({"bundle": bundle["bundle_dir"], "manifest_sha256": bundle["manifest_sha256"],
                            "device": "simulated"})
    assert FAIL_LOG_ACK in prep["acknowledgements"]
    assert "retained fail log" in prep["summary"]["collection"]
    assert "not collected" not in prep["summary"]["collection"].split(";")[1]  # only samples are not collected
    assert prep["summary"]["fail_log_collection"]["max_pages_per_segment"] == 171
    assert prep["summary"]["hold_segments"] == []  # one segment: it ends in DONE (completion=hold keeps its levels)
    without = {key: True for key in ACKNOWLEDGEMENTS if key != FAIL_LOG_ACK}
    with pytest.raises(RunnerError, match="every confirmation checkbox"):
        svc.start_run({"nonce": prep["nonce"], "acknowledgements": without})


def test_a_plan_without_compares_does_not_ask_the_fail_log_box(svc, tmp_path, monkeypatch):
    source = tmp_path / "drive-only.stil"
    text = FIXTURE.read_text()
    source.write_text(text[:text.index("Pattern MAIN")] + "Pattern MAIN { W NORMAL; V { ALL = 01; } V { ALL = 10; } }\n")
    bundle = project(svc, tmp_path, source=source)
    prep = svc.prepare_run({"bundle": bundle["bundle_dir"], "manifest_sha256": bundle["manifest_sha256"],
                            "device": "simulated"})
    assert all(segment["compares"] == 0 for segment in prep["summary"]["segments"])
    assert FAIL_LOG_ACK not in prep["acknowledgements"]
    without = {key: True for key in ACKNOWLEDGEMENTS if key != FAIL_LOG_ACK}
    assert wait(svc.start_run({"nonce": prep["nonce"], "acknowledgements": without}))["status"] == "done"


# ---- result view, STIL details and text ---------------------------------------------------------

def test_a_mismatch_is_described_against_the_stil_and_saved_as_text(svc, tmp_path, monkeypatch):
    bundle = project(svc, tmp_path)
    _, result = run(svc, bundle, monkeypatch, fails=3)
    stpg = result
    assert stpg["run"]["comparison"] == "MISMATCH"
    segment = stpg["segments"][0]
    assert segment["comparison"] == "MISMATCH"
    assert segment["fail_log"] == {"status": "READ", "entries_read": 3, "to_read": 3, "reported": 3, "overflow": False}
    assert stpg["fail_log_stop"] is None
    entry = Path(stpg["result_dir"])
    assert (entry / "run.json.gz").is_file()
    other, _ = _older(Path(bundle["bundle_dir"]))  # open, but another manifest: never used for this result
    described = svc.describe_run({"result": str(entry), "bundle": str(other)})
    assert described["available"], described.get("reason")
    assert Path(described["bundle_dir"]) == Path(bundle["bundle_dir"])  # found in the project's build folder
    shutil.rmtree(other)
    fails = described["description"]["segments"][0]["fail_log"]["entries"]
    assert len(fails) == 3 and all(item["source"]["stil_line"] >= 1 for item in fails)
    text = described["text"]
    assert "fail 1: line" in text and "confidential" in text
    for private in (str(tmp_path), "SIMULATED-STPG-0001", "SIMULATED-STPG"):
        assert private not in text  # no folder of this computer, no USB identity or port
    saved = tmp_path / "out" / "details.txt"
    assert svc.save_description({"result": str(entry), "path": str(saved)})["path"] == str(saved)
    assert saved.read_text() == text
    with pytest.raises(RunnerError, match="exists"):
        svc.save_description({"result": str(entry), "path": str(saved)})
    with pytest.raises(RunnerError, match=r"\.txt"):
        svc.save_description({"result": str(entry), "path": str(tmp_path / "out" / "details.json")})
    with pytest.raises(RunnerError, match="immutable bundle"):
        svc.save_description({"result": str(entry), "path": str(Path(bundle["bundle_dir"]) / "details.txt")})
    assert svc.results(str(entry.parent))["items"][0]["run"]["comparison"] == "MISMATCH"


def test_details_and_text_reuse_matching_read_result_once_and_omit_result_folder_name(svc, tmp_path, monkeypatch):
    from stpg_runner import results
    bundle = project(svc, tmp_path)
    _, result = run(svc, bundle, monkeypatch, fails=1)
    entry = Path(result["result_dir"])
    private_label = "PUBLIC-SYNTHETIC-serial-COM99-private-folder"
    renamed = entry.with_name(private_label)
    entry.rename(renamed)
    match_calls, result_reads = [], []
    original_match, original_read = results._describe_result, service_module.read_run_result
    def match(*args, **kwargs):
        match_calls.append(1)
        return original_match(*args, **kwargs)
    def read(*args, **kwargs):
        result_reads.append(1)
        return original_read(*args, **kwargs)
    def not_again(*args, **kwargs):
        raise AssertionError("SDK reread the GUI's owned result snapshot")
    monkeypatch.setattr(results, "_describe_result", match)
    monkeypatch.setattr(service_module, "read_run_result", read)
    monkeypatch.setattr(results, "read_run_result", not_again)
    described = svc.describe_run({"result": str(renamed)})
    assert described["available"], described.get("reason")
    saved = tmp_path / "safe-details.txt"
    svc.save_description({"result": str(renamed), "path": str(saved)})
    assert saved.read_text() == described["text"] and private_label not in saved.read_text()
    assert len(match_calls) == 1 and len(result_reads) == 2  # one fresh snapshot per operation


def test_the_viewer_marks_only_the_vector_on_the_named_line(svc, tmp_path, monkeypatch):
    bundle = project(svc, tmp_path)
    _, result = run(svc, bundle, monkeypatch, fails=3)
    described = svc.describe_run({"result": result["result_dir"]})
    session = Session(tmp_path / "view")
    trace_id = session.load([described["stil"]], pin_map=described["pin_map"], replace=False)[0]
    trace = session.get(trace_id)
    fails = described["description"]["segments"][0]["fail_log"]["entries"]
    marks = [{"vector": f["source"]["global_vector_index"], "line": f["source"]["stil_line"],
              "signals": [s["name"] for s in f["signals"]], "text": f"fail {i}"} for i, f in enumerate(fails)]
    first = marks[0]
    marks += [{**first, "line": first["line"] + 1, "text": "wrong line"},
              {**first, "vector": trace.vector_count, "text": "outside"},
              {"text": "no source"}]
    placed = session.set_marks(trace_id, marks, "fails of a run")
    assert placed["placed"] == 3 and placed["unplaced"] == 3 and placed["total"] == 6
    stored = session.marks(trace)
    names = [signal.name for signal in trace.signals]
    by_text = {mark["text"]: mark for mark in marks}
    for mark in stored["marks"]:
        assert trace.lines[mark["vector"]] == mark["line"]
        assert mark["start_ps"] == trace.starts[mark["vector"]]
        assert mark["lanes"] == [names.index(n) for n in by_text[mark["text"]]["signals"]] and mark["lanes"]
    reasons = {item["text"]: item["reason"] for item in stored["unplaced"]}
    assert "not the same STIL occurrence" in reasons["wrong line"]
    assert "outside this view" in reasons["outside"]
    assert "no STIL vector" in reasons["no source"]
    assert trace.meta()["fail_marks"] == 6


def test_a_gzip_stil_opens_in_the_viewer_with_the_same_lines(tmp_path):
    plain = compare_source(tmp_path / "public.stil")
    packed = tmp_path / "packed.stil.gz"
    packed.write_bytes(gzip.compress(plain.read_bytes(), mtime=0))
    session = Session(tmp_path / "view")
    a = session.get(session.load([plain], replace=False)[0])
    b = session.get(session.load([packed], replace=False)[0])
    assert list(a.lines) == list(b.lines) and a.vector_count == b.vector_count
    assert {"kind": "stil_gzip", "path": str(packed)} in b.sources
    assert b.detail(0)["source_text"] == a.detail(0)["source_text"]


# ---- where a fail-log read stopped ------------------------------------------------------------

class _SilentAfterFirstPage(SimulatedStpg):
    """Answers the first READ_FAIL_LOG page; the second request gets no reply (the SDK's 2 s window ends)."""

    def write(self, data):
        size = super().write(data)
        if self.commands and self.commands[-1] == c.CMD_READ_FAIL_LOG:
            self.pages = getattr(self, "pages", 0) + 1
            if self.pages == 2:
                self.rx.clear()
        return size


def test_a_stopped_fail_log_read_is_named_and_the_mismatch_stands(svc, tmp_path, monkeypatch):
    bundle = project(svc, tmp_path)
    _, result = run(svc, bundle, monkeypatch, peer_class=_SilentAfterFirstPage, fails=3)
    assert result["run"]["outcome"] == "REJECT" and result["run"]["comparison"] == "MISMATCH"
    assert result["segments"][0]["fail_log"]["status"] == "READ_ERROR"
    stop = result["fail_log_stop"]
    assert stop["segment"] == 0 and stop["terminal"] == "DONE" and stop["fail_count"] == 3
    assert (stop["entries_read"], stop["to_read"]) == (2, 3)
    assert stop["request"] == "READ_FAIL_LOG" and stop["failure_stage"] in ("deadline", "read_limit")
    assert stop["response_bytes"] == 0 and stop["answer"] is None
    assert result["stopped_at"] is None  # not the "first STATUS after RUN" case
    rows = result["exchanges"]["rows"]
    requests = [row["request"] for row in rows]
    assert requests[-1] == "READ_FAIL_LOG" and "FAIL_LOG page data" in requests and "FAIL_LOG page end" in requests
    data = next(row for row in rows if row["request"] == "FAIL_LOG page data")
    assert data["sent_bytes"] is None and data["answer"] == "32 bytes of retained fail-log entries"
    described = svc.describe_run({"result": result["result_dir"]})
    assert described["available"]
    log = described["description"]["segments"][0]["fail_log"]
    assert log["status"] == "READ_ERROR" and len(log["entries"]) == 2  # the pages read before are kept


def test_fail_log_stop_only_for_a_read_error_and_names_a_rejected_answer():
    from stpgview.runner.description import fail_log_stop
    record = {"index": 1, "terminal": {"state": c.STATE_DONE, "fail_count": 9},
              "fail_log": {"status": "READ_ERROR", "reported_fail_count": 9, "entries_read": 4, "entries": []}}
    report = {"planned_segments": 2, "segments": [{"index": 0, "fail_log": {"status": "NOT_NEEDED"}}, record],
              "transactions": [{"kind": "command", "command": c.CMD_READ_FAIL_LOG, "seq": 7, "written_bytes": 32,
                                "decoded": {"rsp": c.RSP_ERROR, "state": c.STATE_DONE}, "raw_response_hex": "00" * 64}]}
    stop = fail_log_stop(report)
    assert stop["answer"] == "ERROR · DONE" and stop["failure_stage"] is None and stop["to_read"] == 9
    for status in ("READ", "PENDING", "NOT_COLLECTED_ERROR"):
        record["fail_log"]["status"] = status
        assert fail_log_stop(report) is None
    assert run_stop_point({**report, "outcome": "REJECT", "completed_segments": 1}) is None


# ---- bundle bound to another SDK: explicit update without recompiling -----------------------------

def _older(bundle_dir: Path) -> tuple[Path, str]:
    """A copy bound to another SDK fingerprint (a unit fixture, not an old compiler run), in the same build folder."""
    root = bundle_dir.parent / "older-copy"
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


def test_a_bundle_of_another_sdk_is_updated_only_on_request_and_logged(svc, tmp_path, monkeypatch):
    bundle = project(svc, tmp_path)
    old, pin = _older(Path(bundle["bundle_dir"]))
    before = {p.relative_to(old).as_posix(): identity(p) for p in old.rglob("*") if p.is_file()}
    opened = svc.open_bundle(str(old), pin)
    assert opened["verification"]["status"] == "PASS" and opened["sdk_matches"] is False and opened["rebound"] is None
    with pytest.raises(RunnerError, match="differs from the bundle"):
        svc.prepare_run({"bundle": str(old), "manifest_sha256": pin, "device": "simulated"})
    done = wait(svc.start_rebind(str(old), pin))
    assert done["status"] == "done", done["error"]
    new = done["result"]["bundle"]
    assert Path(new["bundle_dir"]).parent == old.parent and new["sdk_matches"] is True
    assert new["rebound"] == {"warning": "Compiled with an older SDK; later compiler fixes are not included."}
    assert {p.relative_to(old).as_posix(): identity(p) for p in old.rglob("*") if p.is_file()} == before
    log = [json.loads(line) for line in (tmp_path / "project" / "stpg-view.log.jsonl").read_text().splitlines()]
    end = next(item for item in log if item.get("event") == "rebind.end")
    assert end["rebind"]["compile_tool_sha256"] == "0" * 64 and end["no_op"] is False
    again = wait(svc.start_rebind(new["bundle_dir"], new["manifest_sha256"]))
    assert again["result"]["no_op"] is True and again["result"]["bundle_dir"] == new["bundle_dir"]
    with pytest.raises(RunnerError, match="exact manifest"):
        svc.start_rebind(str(old), "")


def test_describe_text_lists_the_rebind_and_limits_the_entries():
    from stpgview.runner.description import describe_text
    description = {"manifest_sha256": "a" * 64, "outcome": "COMPLETED", "comparison": "MISMATCH", "stop": None,
                   "rebind": {"warning": "Compiled with an older SDK; later compiler fixes are not included.",
                              "compile_tool_sha256": "0" * 64, "compile_python": "3.10.0", "source_tool_sha256": "1" * 64},
                   "segments": [{"index": 0, "outcome": "COMPLETED", "comparison": "MISMATCH", "terminal_state": c.STATE_DONE,
                                 "fail_count": 2500, "source_range": {"first": None, "last": None},
                                 "fail_log": {"status": "READ", "entries_read": 2500, "overflow": True,
                                              "entries": [{"source": {"stil_line": 5, "global_vector_index": i},
                                                           "signals": [{"name": "D1", "expected": 0, "actual": 1}],
                                                           "unmapped_fail_bits": 0} for i in range(2500)]}}]}
    text = describe_text(description, result_name="r1")
    assert "later compiler fixes are not included" in text and "compiled by SDK " + "0" * 64 in text
    assert len(re.findall(r"(?m)^  fail \d+: ", text)) == 2000 and "first 2000 fail entries" in text
    assert "D1 expected L got H" in text and "keeps only the first 341" in text


def test_exchanges_name_fail_log_records_that_write_nothing():
    report = {"transactions": [
        {"kind": "command", "command": c.CMD_READ_FAIL_LOG, "seq": 5, "written_bytes": c.STPG_CMD_SIZE,
         "write_begin_s": 1.0, "read_end_s": 1.01, "decoded": {"rsp": c.RSP_READY_FOR_READ, "state": c.STATE_DONE}},
        {"kind": "fail_log_payload", "seq": 5, "size": 32, "read_begin_s": 1.01, "read_end_s": 1.02,
         "raw_response_hex": "00" * 32},
        {"kind": "fail_log_trailer", "seq": 5, "read_begin_s": 1.02, "read_end_s": 1.03, "read_error": "x",
         "read_failure_stage": "deadline", "raw_response_hex": ""}]}
    view = device_exchanges(report)
    assert [row["request"] for row in view["rows"]] == ["READ_FAIL_LOG", "FAIL_LOG page data", "FAIL_LOG page end"]
    assert view["rows"][1]["sent_bytes"] is None and view["rows"][1]["answer"] == "32 bytes of retained fail-log entries"
    assert view["last_write_completed"] is True  # what was written for that page is its READ_FAIL_LOG request
    assert view["rows"][2]["took_s"] == pytest.approx(0.01)
