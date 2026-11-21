"""Host-only tests of the GUI runner (stpgview.runner + HTTP routes).

The STPG side is the simulated firmware peer of ``--demo-device``; real device
paths are only checked for rejection. No port is opened by these tests.
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import threading
import time

import pytest

from stpg import constants as c
from stpg_runner import build_project, public_summary, run_bundle
from stpg_runner.artifacts import RunnerError, read_json
from stpg_runner.cli import main as runner_main
from stpgview.runner import service as service_module
from stpgview.runner.jobs import Busy, RunnerJobs
from stpgview.runner.service import ACKNOWLEDGEMENTS, RunnerService
from stpgview.runner.simulated import SIMULATED_DEVICE, SimulatedStpg, simulated_ports
from stpgview.server import ViewerServer, make_runner
from stpgview.session import Session

from stpg_hardware_guard import forbid_hardware

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)  # real serial opens and USB metadata enumeration fail the test
FIXTURES = Path(__file__).parent / "fixtures"
RUNNER_STIL = FIXTURES / "runner" / "pattern.stil"
ACKS = {key: True for key in ACKNOWLEDGEMENTS}


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


@pytest.fixture(scope="module")
def cui_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("gui-runner-cui")
    assert runner_main(["init", str(RUNNER_STIL), "--directory", str(root / "project"), "--wait-preset", "none",
                        "--completion", "release"]) == 0
    return build_project(root / "project" / "project.stpg-project.json")


def gui_project(svc, tmp_path, **override):
    request = {"stil": str(RUNNER_STIL), "directory": str(tmp_path / "gui-project"), "wait_preset": "none",
               "completion": "hold", "storage": "psram", "compile_mode": "rate-vector", "loop_policy": "preserve"}
    request.update(override)
    return svc.init_project(request)


# ---- projects / builds / bundles ---------------------------------------------------------

def test_new_project_requires_every_choice_explicitly(svc, tmp_path):
    with pytest.raises(RunnerError, match="choose explicitly"):
        svc.init_project({"stil": str(RUNNER_STIL), "directory": str(tmp_path / "p"), "wait_preset": "none"})
    info = gui_project(svc, tmp_path)
    assert info["settings"]["execution"] == {"storage": "psram", "completion": "hold"}
    assert info["settings"]["capture"] == {"enabled": False}
    with pytest.raises(RunnerError, match="already exists"):
        gui_project(svc, tmp_path)  # never reuses an existing folder


def test_open_project_marks_defaults_without_converting(svc):
    info = svc.open_project(str(FIXTURES / "runner" / "project.stpg-project.json"))
    assert "segmentation.max_vectors" in info["defaults"] and "capture.enabled" in info["defaults"]
    assert "wait.preset" not in info["defaults"]
    assert info["settings"]["compile"]["mode"] == "rate-vector"
    # opening does not create a build workspace
    assert not (FIXTURES / "runner" / "build").exists()


def test_gui_build_verifies_through_the_cui(svc, tmp_path, capsys):
    project = gui_project(svc, tmp_path)
    result = wait(svc.start_build(project["project_path"]))
    assert result["status"] == "done", result["error"]
    stages = [e["stage"] for e in result["events"]]
    assert stages[0] == "preflight" and "bundle_verify" in stages
    built = result["result"]
    assert built["physical_execution_authorized"] is False and built["bundle"]["verification"]["status"] == "PASS"
    capsys.readouterr()
    assert runner_main(["verify", built["bundle_dir"], "--manifest-sha256", built["manifest_sha256"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"
    again = wait(svc.start_build(project["project_path"]))
    assert again["result"]["cache_hit"] is True and again["result"]["manifest_sha256"] == built["manifest_sha256"]


def test_cui_bundle_opens_unchanged(svc, cui_bundle):
    before = {p: p.read_bytes() for p in Path(cui_bundle.bundle_dir).rglob("*") if p.is_file()}
    info = svc.open_bundle(str(cui_bundle.bundle_dir), cui_bundle.manifest_sha256)
    assert info["verification"]["status"] == "PASS" and info["manifest_sha256"] == cui_bundle.manifest_sha256
    expected = info["patterns"][0]["expected"]
    assert (expected["events"], expected["samples"], expected["compares"]) == (6, 2, 0)
    assert {p: p.read_bytes() for p in Path(cui_bundle.bundle_dir).rglob("*") if p.is_file()} == before
    wrong = svc.open_bundle(str(cui_bundle.bundle_dir), "0" * 64)
    assert wrong["verification"]["status"] == "REJECT"


def test_failed_build_reservation_is_listed_and_never_overwritten(svc, tmp_path):
    bad = tmp_path / "bad.stil"
    bad.write_text(RUNNER_STIL.read_text().replace("Pattern MAIN {", "Pattern MAIN {\n  V { ALL = 0; }"), encoding="utf-8")
    project = gui_project(svc, tmp_path, stil=str(bad))
    first = wait(svc.start_build(project["project_path"]))
    assert first["status"] == "failed"
    listed = svc.open_project(project["project_path"])["reservations"]
    assert listed and listed[0]["status"] == "FAILED"
    second = wait(svc.start_build(project["project_path"]))
    assert second["status"] == "failed" and "not overwritten" in second["error"]


# ---- run confirmation / ownership ------------------------------------------------------------

def built(svc, tmp_path):
    project = gui_project(svc, tmp_path)
    return wait(svc.start_build(project["project_path"]))["result"]


def test_run_needs_ticked_confirmations_and_a_fresh_nonce(svc, tmp_path):
    b = built(svc, tmp_path)
    prep = svc.prepare_run({"bundle": b["bundle_dir"], "manifest_sha256": b["manifest_sha256"], "device": "simulated"})
    summary = prep["summary"]
    assert summary["transport"].startswith("simulated") and summary["segments"][0]["events"] == 6
    assert "not collected" in summary["collection"] and not Path(summary["output_dir"]).exists()
    with pytest.raises(RunnerError, match="checkbox"):
        svc.start_run({"nonce": prep["nonce"], "acknowledgements": {"compatible_hardware": True}})
    job = svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS})
    result = wait(job)
    with pytest.raises(RunnerError, match="already used"):
        svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS})
    run = result["result"]["run"]
    assert result["result"]["simulated"] and result["result"]["transport_kind"] == "injected"
    assert run["outcome"] == "COMPLETED" and run["comparison"] == "NOT_APPLICABLE"  # fixture has no compares
    assert run["firmware_validation_pass"] is False and run["electrical_release_verified"] is False
    assert result["result"]["completion"] == "hold" and run["release_command_attempted"] is False
    assert "transactions" not in result["result"]["run"]  # raw journal stays in run.json
    assert "transactions" in read_json(Path(result["result"]["result_file"]))


def test_gui_and_sdk_results_agree(svc, tmp_path):
    b = built(svc, tmp_path)
    prep = svc.prepare_run({"bundle": b["bundle_dir"], "manifest_sha256": b["manifest_sha256"], "device": "simulated"})
    gui = wait(svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS}))["result"]["run"]
    sdk = run_bundle(Path(b["bundle_dir"]), manifest_sha256=b["manifest_sha256"], device=SIMULATED_DEVICE,
                     output_dir=tmp_path / "sdk-run", confirm_run=True, stream_factory=lambda _: SimulatedStpg(),
                     port_provider=simulated_ports, sleep=lambda s: None)
    assert gui == public_summary(sdk)


def test_device_paths_are_gated(tmp_path):
    off = RunnerService(config_dir=tmp_path / "cfg", device_run=False, demo=False, results_dir=tmp_path / "r")
    project = off.init_project({"stil": str(RUNNER_STIL), "directory": str(tmp_path / "p"), "wait_preset": "none",
                                "completion": "hold", "storage": "psram", "compile_mode": "rate-vector",
                                "loop_policy": "preserve"})
    b = wait(off.start_build(project["project_path"]))["result"]
    off.store.save("devices", "bench", SIMULATED_DEVICE.to_dict())
    with pytest.raises(RunnerError, match="--no-device-run"):
        off.prepare_run({"bundle": b["bundle_dir"], "manifest_sha256": b["manifest_sha256"], "device": "bench"})
    with pytest.raises(RunnerError, match="--demo-device"):
        off.prepare_run({"bundle": b["bundle_dir"], "manifest_sha256": b["manifest_sha256"], "device": "simulated"})
    with pytest.raises(RunnerError, match="--no-device-run"):
        off.start_controller_identify("any")


def test_one_device_job_at_a_time():
    jobs = RunnerJobs()
    gate = threading.Event()
    first = jobs.start("run", "first", lambda job: gate.wait(5), device=True)
    with pytest.raises(Busy):
        jobs.start("run", "second", lambda job: None, device=True)
    jobs.start("build", "offline is fine", lambda job: None)
    gate.set()
    wait(first)
    jobs.start("run", "after", lambda job: None, device=True)


def test_cancel_closes_without_stop_or_release(svc, tmp_path, monkeypatch):
    peers: list[SimulatedStpg] = []

    class Slow(SimulatedStpg):
        def __init__(self, **kw):
            super().__init__(**kw)
            peers.append(self)

    monkeypatch.setattr(service_module, "SimulatedStpg", Slow)
    monkeypatch.setattr("stpgview.runner.simulated.RUNNING_POLLS", 10_000)
    b = built(svc, tmp_path)
    prep = svc.prepare_run({"bundle": b["bundle_dir"], "manifest_sha256": b["manifest_sha256"], "device": "simulated"})
    job = svc.start_run({"nonce": prep["nonce"], "acknowledgements": ACKS})
    end = time.monotonic() + 20
    while not (peers and c.CMD_RUN in peers[0].commands) and time.monotonic() < end:
        time.sleep(0.02)
    svc.cancel(job.id)
    result = wait(job)
    assert result["status"] == "cancelled" and result["result"]["run"]["outcome"] == "CANCELLED"
    commands = peers[0].commands
    assert c.CMD_STOP not in commands and commands.count(c.CMD_RESET) == 1  # only the planned pre-upload RESET
    assert peers[0].closed == 1


def test_bindings_are_validated_and_stored_outside_projects(svc):
    saved = svc.store.save("devices", "bench-1", SIMULATED_DEVICE.to_dict())
    assert Path(saved["path"]).parent == svc.store.root / "devices"
    with pytest.raises(RunnerError, match="exists"):
        svc.store.save("devices", "bench-1", SIMULATED_DEVICE.to_dict())
    with pytest.raises(RunnerError):
        svc.store.save("devices", "../escape", SIMULATED_DEVICE.to_dict())
    bad = dict(SIMULATED_DEVICE.to_dict(), extra=1)
    with pytest.raises(RunnerError):
        svc.store.save("devices", "bench-2", bad)
    names = [item["name"] for item in svc.bindings("devices")["items"]]
    assert names == ["simulated", "bench-1"]


# ---- HTTP boundary ---------------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("device access during an HTTP test")

    monkeypatch.setattr("stpg_runner.device.open_serial", forbidden)
    monkeypatch.setattr("stpg_runner.device.enumerate_ports", forbidden)
    session = Session(tmp_path / "work")
    runner = make_runner(session, host="127.0.0.1", demo=True, config_dir=tmp_path / "cfg",
                         results_dir=tmp_path / "runs")
    srv = ViewerServer(("127.0.0.1", 0), session, "tok", runner)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, headers=None):
    host, port = srv.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=30)
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Host": f"{host}:{port}", **(headers or {})}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=hdrs)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    try:
        return response.status, json.loads(payload)
    except json.JSONDecodeError:
        return response.status, payload


def test_http_requires_token_header_and_same_origin_for_changes(server):
    assert call(server, "GET", "/api/runner/state")[0] == 401
    status, state = call(server, "GET", "/api/runner/state", headers={"X-STPG-Token": "tok"})
    assert status == 200 and state["demo"] is True
    # token in the query string is accepted for reads only
    assert call(server, "GET", "/api/runner/state?token=tok")[0] == 200
    assert call(server, "POST", "/api/runner/ports?token=tok", {})[0] == 403
    host, port = server.server_address[:2]
    evil = {"X-STPG-Token": "tok", "Origin": "http://evil.example"}
    assert call(server, "POST", "/api/runner/ports", {}, evil)[0] == 403
    cross = {"X-STPG-Token": "tok", "Sec-Fetch-Site": "cross-site"}
    assert call(server, "POST", "/api/runner/ports", {}, cross)[0] == 403
    same = {"X-STPG-Token": "tok", "Origin": f"http://{host}:{port}", "Sec-Fetch-Site": "same-origin"}
    status, ports = call(server, "POST", "/api/runner/ports", {}, same)
    assert status == 200 and any(p["device"] == SIMULATED_DEVICE.port for p in ports["ports"])
    # the guard replaced list_ports.comports: only the simulated entries, no real metadata enumeration
    assert {p["device"] for p in ports["ports"]} == {SIMULATED_DEVICE.port, "SIMULATED-LA"}
    assert call(server, "GET", "/api/runner/state", headers={"X-STPG-Token": "tok", "Host": "evil:80"})[0] == 403


def test_http_rejects_oversized_json_and_get_mutations(server):
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=30)
    try:
        # The server rejects the declared size without reading the body. Do not
        # race its close by sending a megabyte into that correctly closed socket.
        connection.request("POST", "/api/runner/project/open", headers={
            "X-STPG-Token": "tok", "Content-Type": "application/json",
            "Content-Length": str(1024 * 1024 + 10)})
        response = connection.getresponse()
        assert response.status == 400 and response.getheader("Connection") == "close"
        assert json.loads(response.read())["error"] == "request too large"
    finally:
        connection.close()
    assert call(server, "GET", "/api/runner/run/start", headers={"X-STPG-Token": "tok"})[0] == 405
    assert call(server, "GET", "/api/runner/build", headers={"X-STPG-Token": "tok"})[0] == 405
    assert call(server, "GET", "/api/runner/ports", headers={"X-STPG-Token": "tok"})[0] == 405


def test_http_run_flow_and_double_submit(server, tmp_path):
    t = {"X-STPG-Token": "tok"}
    status, project = call(server, "POST", "/api/runner/project/init", {
        "stil": str(RUNNER_STIL), "directory": str(tmp_path / "hp"), "wait_preset": "none", "completion": "hold",
        "storage": "psram", "compile_mode": "rate-vector", "loop_policy": "preserve"}, t)
    assert status == 200, project
    status, job = call(server, "POST", "/api/runner/build", {"project": project["project_path"]}, t)
    for _ in range(600):
        status, job = call(server, "GET", f"/api/runner/job?id={job['id']}", headers=t)
        if job["status"] != "running":
            break
        time.sleep(0.05)
    bundle = job["result"]
    status, prep = call(server, "POST", "/api/runner/run/prepare", {"bundle": bundle["bundle_dir"],
                                                                   "manifest_sha256": bundle["manifest_sha256"],
                                                                   "device": "simulated"}, t)
    assert status == 200
    body = {"nonce": prep["nonce"], "acknowledgements": ACKS}
    first = call(server, "POST", "/api/runner/run/start", body, t)
    second = call(server, "POST", "/api/runner/run/start", body, t)
    assert first[0] == 200 and second[0] == 400 and "already used" in second[1]["error"]


def test_runner_is_not_served_on_other_interfaces(tmp_path):
    assert make_runner(Session(tmp_path / "w"), host="0.0.0.0") is None
    session = Session(tmp_path / "w2")
    srv = ViewerServer(("127.0.0.1", 0), session, "tok", None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        assert call(srv, "GET", "/api/runner/state", headers={"X-STPG-Token": "tok"})[0] == 503
        assert call(srv, "GET", "/api/state", headers={"X-STPG-Token": "tok"})[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_role_pins_of_a_bundle_are_named_in_the_confirmation(svc, tmp_path):
    # A marker pattern made by another tool (for example the logic-analyzer tool) drives an extra pin; the core does
    # not judge its derivation, it names the pin so that the operator checks that it is spare and safe to drive.
    source = tmp_path / "marked.stil"
    text = RUNNER_STIL.read_text().replace("  D1 InOut;\n", "  D1 InOut;\n  TRIG In;\n")
    text = text.replace("ALL = 'D0 + D1';", "ALL = 'D0 + D1';\n  WITH = 'D0 + D1 + TRIG';")
    text = text.replace("      ALL { 0: 0ns Drive(0); 1: 0ns Drive(1); X: 100ns Compare(X); }",
                        "      ALL { 0: 0ns Drive(0); 1: 0ns Drive(1); X: 100ns Compare(X); }\n"
                        "      TRIG { 0: 0ns Drive(0); 1: 0ns Drive(1); }")
    text = text.replace("  V { ALL = 10; }", "  V { WITH = 101; }")
    source.write_text(text)
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"active_pins": 16, "signals": {
        "D0": {"bit": 0, "direction": "inout"}, "D1": {"bit": 1, "direction": "inout"},
        "TRIG": {"bit": 3, "logical_pin": "DUT3", "direction": "input", "role": "marker"}}}))
    project = gui_project(svc, tmp_path, stil=str(source), pin_map=str(pins))
    b = wait(svc.start_build(project["project_path"]))
    assert b["status"] == "done", b["error"]
    b = b["result"]
    assert svc.open_bundle(b["bundle_dir"], b["manifest_sha256"])["role_pins"] == [
        {"signal": "TRIG", "role": "marker", "bit": 3, "logical_pin": "DUT3"}]
    prep = svc.prepare_run({"bundle": b["bundle_dir"], "manifest_sha256": b["manifest_sha256"], "device": "simulated"})
    assert [r["signal"] for r in prep["summary"]["role_pins"]] == ["TRIG"]
    plain = built(svc, tmp_path / "plain")
    assert svc.open_bundle(plain["bundle_dir"], plain["manifest_sha256"])["role_pins"] == []


# ---- wait scope of a STIL without Loop (user report 2026-11-03) ----------------------------------------------

@pytest.mark.parametrize("pattern, has_loop", [
    ("  V { ALL = 01; }\n  V { ALL = 10; }\n", False),
    ("  // Loop 3 { only in a comment }\n  /* Loop 2 {\n */\n  V { ALL = 01; }\n", False),
    ("  Loop 3 {\n    V { ALL = 01; }\n  }\n", True),
    ("  V { ALL = 01; } Loop 2 { V { ALL = 10; } }\n", True),  # not at the start of a line
])
def test_a_stil_without_any_loop_statement_is_recognised(svc, tmp_path, pattern, has_loop):
    # gui_counter_long.stil has no Loop: its compiled pattern keeps no Loop block, so a wait preset needs Scope =
    # non-loop whatever the loop policy, and the build rejects loop-generic. The dialog now knows this beforehand.
    source = tmp_path / "p.stil"
    source.write_text(RUNNER_STIL.read_text().split("Pattern MAIN")[0] + "Pattern MAIN {\n  W NORMAL;\n" + pattern + "}\n")
    assert service_module.stil_has_loop(source) is has_loop
    assert svc.stil_info(str(source))["has_loop"] is has_loop
    info = svc.init_project({"input_form": "stil", "stil": str(source), "directory": str(tmp_path / "proj"),
                             "wait_preset": "none", "completion": "hold", "storage": "psram",
                             "compile_mode": "auto", "loop_policy": "preserve", "segmentation": "auto"})
    assert info["stil_has_loop"] is has_loop  # also for the snapshot the project keeps (Edit settings, Copy)
    with pytest.raises(ValueError, match="not a file"):
        svc.stil_info(str(tmp_path / "missing.stil"))
