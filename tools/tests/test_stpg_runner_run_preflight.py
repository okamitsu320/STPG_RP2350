"""Actual host SDK paths, public synthetic bundles and injected peers only."""

from dataclasses import replace
import json
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

import pytest

from stpg_runner import CancellationToken, build_project, init_project, prepare_run, run_bundle
from stpg_runner.accessories import run_with_accessories
from stpg_runner.artifacts import RunnerError, read_json, write_json
from test_stpg_runner_execution import Clock, DEVICE, FirmwarePeer, ports


pytestmark = pytest.mark.stpg_host_only
FIXTURE = Path(__file__).parent / "fixtures" / "runner" / "pattern.stil"


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    import stpg_runner.execution as execution

    def forbidden(*args, **kwargs):
        raise AssertionError("run preflight test attempted physical access")

    monkeypatch.setattr(execution, "open_serial", forbidden)
    monkeypatch.setattr(execution, "enumerate_ports", forbidden)


@pytest.fixture
def built(tmp_path):
    project = init_project(FIXTURE, tmp_path / "project", wait_preset="none", completion="release")
    return build_project(project)


def options(built, tmp_path, peer, *, events=None, provider=ports, **extra):
    clock = Clock()
    return dict(manifest_sha256=built.manifest_sha256, device=DEVICE, output_dir=tmp_path / "result",
                confirm_run=True, stream_factory=lambda _: peer, port_provider=provider,
                clock=clock, sleep=clock.sleep,
                on_progress=events.append if events is not None else None, **extra)


@pytest.mark.parametrize("accessory_wrapper", [False, True])
def test_ordinary_run_has_one_deep_verification(built, tmp_path, monkeypatch, accessory_wrapper):
    import stpg_runner.execution as execution
    original = execution.verify_bundle
    calls = []

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(execution, "verify_bundle", counted)
    peer = FirmwarePeer()
    entry = run_with_accessories if accessory_wrapper else run_bundle
    result = entry(built.bundle_dir, **options(built, tmp_path, peer))
    run = result["run"] if accessory_wrapper else result
    assert run["outcome"] == "COMPLETED"
    assert len(calls) == 1
    assert peer.close_count == 1 and peer.payloads


def test_missing_port_rejects_before_deep_verification_and_records_list(built, tmp_path, monkeypatch):
    import stpg_runner.execution as execution
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("missing port must reject before semantic verification")

    monkeypatch.setattr(execution, "verify_bundle", forbidden)
    peer = FirmwarePeer()
    report = run_bundle(built.bundle_dir, **options(built, tmp_path, peer, provider=lambda: []))
    assert report["outcome"] == "REJECT" and not calls
    assert not report["stream_open_attempted"] and not peer.commands and not peer.close_count
    check, = report["port_checks"]
    assert check["phase"] == "before_bundle_verify" and check["status"] == "REJECT"
    assert check["ports"] == [] and check["code"] == "PORT_NOT_LISTED"
    assert read_json(tmp_path / "result" / "run.json.gz") == report


def test_port_is_rechecked_after_verification_before_open(built, tmp_path):
    peer = FirmwarePeer()
    snapshots = iter([ports(), []])
    report = run_bundle(built.bundle_dir, **options(built, tmp_path, peer, provider=lambda: next(snapshots)))
    assert report["outcome"] == "REJECT" and not report["stream_open_attempted"]
    assert [check["status"] for check in report["port_checks"]] == ["PASS", "REJECT"]
    assert report["port_checks"][-1]["phase"] == "before_open"
    assert not peer.commands and peer.close_count == 0


def test_progress_starts_before_semantics_and_carries_real_file_counts(built, tmp_path, monkeypatch):
    import stpg_runner.execution as execution
    original = execution.verify_bundle
    events = []

    def counted(*args, **kwargs):
        assert any(e.stage == "bundle_verify" and e.event == "start" for e in events)
        return original(*args, **kwargs)

    monkeypatch.setattr(execution, "verify_bundle", counted)
    peer = FirmwarePeer()
    report = run_bundle(built.bundle_dir, **options(built, tmp_path, peer, events=events))
    assert report["outcome"] == "COMPLETED"
    assert any(e.stage == "bundle_files" and e.total and e.completed == e.total for e in events)
    assert any(e.stage == "snapshot_verify" and e.event == "done" for e in events)
    assert [e.elapsed_s for e in events] == sorted(e.elapsed_s for e in events)


@pytest.mark.parametrize("mutation", ["source", "metadata", "payload", "manifest", "unlisted", "missing"])
def test_post_semantic_edit_is_rejected_by_snapshot_guard(built, tmp_path, monkeypatch, mutation):
    import stpg_runner.execution as execution
    original = execution.verify_bundle

    def change_after_check(*args, **kwargs):
        result = original(*args, **kwargs)
        root = built.bundle_dir
        manifest = read_json(root / "runner.bundle.json")
        if mutation == "source":
            path = root / "sources" / "inputs" / "pattern.stil"
            path.write_bytes(path.read_bytes() + b"\n// changed\n")
        elif mutation == "metadata":
            name = next(name for name in manifest["files"] if name.endswith("pattern.metadata.json"))
            path = root / name
            path.write_bytes(path.read_bytes() + b" ")
        elif mutation == "payload":
            path = root / manifest["patterns"][0]["path"]
            path.write_bytes(path.read_bytes() + b"\x00")
        elif mutation == "manifest":
            path = root / "runner.bundle.json"
            path.write_bytes(path.read_bytes() + b" ")
        elif mutation == "unlisted":
            (root / "unlisted.txt").write_text("public test mutation", encoding="utf-8")
        else:
            (root / "verify.py").unlink()
        return result

    monkeypatch.setattr(execution, "verify_bundle", change_after_check)
    peer = FirmwarePeer()
    report = run_bundle(built.bundle_dir, **options(built, tmp_path, peer))
    assert report["outcome"] == "REJECT", report["errors"]
    assert not report["stream_open_attempted"] and not peer.commands and peer.close_count == 0


@pytest.mark.parametrize("rebind_outer_inventory", [False, True])
def test_hold_edit_after_semantics_rejects_before_open(tmp_path, monkeypatch, rebind_outer_inventory):
    import stpg_runner.execution as execution
    from stpg_runner.artifacts import identity

    project = init_project(FIXTURE, tmp_path / "project", wait_preset="none", completion="hold",
                           max_segment_vectors=2)
    built = build_project(project)
    original = execution.verify_bundle
    events = []

    def change_after_check(*args, **kwargs):
        result = original(*args, **kwargs)
        root = built.bundle_dir
        manifest_path = root / "runner.bundle.json"
        manifest = read_json(manifest_path)
        name = manifest["package_manifest"]
        path = root / name
        record = read_json(path)
        assert len(record["segments"]) == 2
        # Keep the two adjacent declared states consistent with each other,
        # but change them after semantic validation of the binary-derived hold.
        for segment, field in [(0, "final_hold_state"), (1, "entry_hold_state")]:
            state = record["segments"][segment][field]
            state["drive_value"] = f"0x{int(state['drive_value'], 0) ^ 1:08x}"
        write_json(path, record)
        if rebind_outer_inventory:
            manifest["files"][name] = identity(path)
            write_json(manifest_path, manifest)  # original user manifest pin must still win
        return result

    monkeypatch.setattr(execution, "verify_bundle", change_after_check)
    peer = FirmwarePeer()
    result = run_bundle(built.bundle_dir, **options(built, tmp_path, peer, events=events))
    assert any(e.stage == "snapshot" and e.event == "done" for e in events), result["errors"]
    assert result["outcome"] == "REJECT" and not result["stream_open_attempted"]
    assert any("bundle manifest SHA-256 mismatch" in error if rebind_outer_inventory
               else "bundle artifact mismatch" in error for error in result["errors"])
    assert not peer.commands and not peer.close_count


def test_shared_cancel_message_does_not_assert_zero_device_contact():
    from stpg_runner.progress import BuildCancelled

    token = CancellationToken()
    token.cancel()
    with pytest.raises(BuildCancelled) as caught:
        token.check()
    assert "lifecycle" in str(caught.value) and "no device I/O" not in str(caught.value)


@pytest.mark.parametrize("command", ["plan", "verify"])
def test_offline_cli_progress_keeps_json_stdout_and_never_enumerates(built, capsys, command):
    from stpg_runner.cli import main

    assert main([command, str(built.bundle_dir), "--manifest-sha256", built.manifest_sha256,
                 "--progress", "jsonl"]) == 0
    streams = capsys.readouterr()
    report = json.loads(streams.out)
    assert report["status"] == ("HOST_ONLY_PLANNED" if command == "plan" else "PASS")
    assert report["device_io_performed"] is False
    events = [json.loads(line) for line in streams.err.splitlines()]
    assert events and all(event["kind"] == "stpg_runner_progress" for event in events)
    assert any(event["stage"] == "bundle_files" and event["event"] == "done" for event in events)


def test_heartbeat_cancellation_during_host_verification_never_opens(built, tmp_path, monkeypatch):
    import stpg_runner.build as builder
    builder._clear_verification_receipts()  # specifically exercise a cold semantic check
    original = builder.verify_pattern_package
    token = CancellationToken()
    events = []

    def slow(*args, **kwargs):
        time.sleep(1.15)  # host test only, long enough for one real heartbeat
        return original(*args, **kwargs)

    def cancel_on_heartbeat(event):
        events.append(event)
        if event.stage == "bundle_verify" and event.event == "heartbeat":
            token.cancel()

    monkeypatch.setattr(builder, "verify_pattern_package", slow)
    peer = FirmwarePeer()
    opts = options(built, tmp_path, peer, cancel=token)
    opts["on_progress"] = cancel_on_heartbeat
    report = run_bundle(built.bundle_dir, **opts)
    assert report["outcome"] == "CANCELLED"
    assert any(e.event == "heartbeat" for e in events)
    assert not report["stream_open_attempted"] and not peer.commands


def test_port_diagnostics_distinguish_identity_mismatch_without_fallback():
    from stpg_runner.device import validate_port
    device = replace(DEVICE, port="COM18", serial_number="SYNTHETIC_A")
    metadata = [SimpleNamespace(device="COM19", vid=device.vid, pid=device.pid, serial_number=device.serial_number)]
    with pytest.raises(RunnerError) as caught:
        validate_port(device, metadata)
    assert "PORT_NOT_LISTED" in str(caught.value)
    assert "COM18" in str(caught.value) and "COM19" in str(caught.value)


def test_windows_com_spelling_does_not_relax_usb_serial_case(monkeypatch):
    import stpg_runner.device as module
    native = module._port_endpoint
    monkeypatch.setattr(module, "_port_endpoint", lambda name: native(name, windows=True))
    config = replace(DEVICE, port="COM18", serial_number="SYNTHETIC_A")
    metadata = [SimpleNamespace(device="com18", vid=config.vid, pid=config.pid,
                                serial_number=config.serial_number)]
    assert module.validate_port(config, metadata)["port"] == "COM18"
    metadata[0].serial_number = "synthetic_a"
    with pytest.raises(RunnerError, match="serial"):
        module.validate_port(config, metadata)


@pytest.mark.parametrize("case,code", [
    ("missing", "PORT_NOT_LISTED"), ("duplicate", "PORT_RECORD_DUPLICATED"),
    ("ambiguous", "USB_IDENTITY_AMBIGUOUS"), ("vid", "PORT_USB_MISMATCH"),
    ("pid", "PORT_USB_MISMATCH"), ("serial", "PORT_USB_MISMATCH"),
    ("serial_case", "USB_SERIAL_CASE_MISMATCH"), ("usb_missing", "PORT_USB_MISMATCH"),
])
def test_port_rejection_matrix_keeps_exact_metadata(case, code):
    from stpg_runner.device import PortBindingError, validate_port

    selected = SimpleNamespace(device=DEVICE.port, vid=DEVICE.vid, pid=DEVICE.pid,
                               serial_number=DEVICE.serial_number)
    metadata = [selected]
    if case == "missing":
        metadata = []
    elif case == "duplicate":
        metadata.append(selected)
    elif case == "ambiguous":
        metadata.append(SimpleNamespace(**{**vars(selected), "device": "DIFFERENT-ENDPOINT"}))
    elif case == "usb_missing":
        metadata = [SimpleNamespace(device=DEVICE.port)]
    elif case == "serial_case":
        selected.serial_number = DEVICE.serial_number.lower()
    else:
        setattr(selected, "serial_number" if case == "serial" else case,
                "OTHER-SERIAL" if case == "serial" else 1)
    with pytest.raises(PortBindingError) as caught:
        validate_port(DEVICE, metadata)
    assert caught.value.details["code"] == code
    assert caught.value.details["ports"] == [
        {field: getattr(item, field, None) for field in ("device", "vid", "pid", "serial_number")}
        for item in metadata]


@pytest.mark.parametrize("before,after", [
    ('check_port("before_open")', 'check_port("before_bundle_verify")'),
    ('check_port("before_open")', 'check_port("before_open"); check_port("before_open")'),
    ('lambda: _verify_inventory(', 'lambda: dict('),
    ('verified = verify_bundle(', 'verified = dict('),
    ('expected_sha256=manifest_sha256)', 'expected_sha256=None)'),
])
def test_admission_gate_rejects_omitted_or_duplicate_guards(before, after):
    import stpg_runner.execution as execution
    from stpg_runner.execution_gate import check_execution_contract

    source = Path(execution.__file__).read_text(encoding="utf-8")
    assert before in source
    assert check_execution_contract(execution_source=source.replace(before, after))["status"] == "REJECT"


def test_full_semantics_is_not_replaced_with_only_file_hashes(built, tmp_path):
    from stpg_runner.artifacts import identity

    root = built.bundle_dir
    policy = root / "compile.policy.json"
    record = read_json(policy)
    record["mode_reason"] = "synthetic contradiction"
    write_json(policy, record)
    manifest_path = root / "runner.bundle.json"
    manifest = read_json(manifest_path)
    manifest["files"][policy.name] = identity(policy)
    write_json(manifest_path, manifest)
    peer = FirmwarePeer()
    opts = options(built, tmp_path, peer)
    opts["manifest_sha256"] = identity(manifest_path)["sha256"]
    result = run_bundle(root, **opts)
    assert result["outcome"] == "REJECT" and not result["stream_open_attempted"]
    assert any("compile policy" in error for error in result["errors"])
    assert not peer.commands


def test_outdated_sdk_rejects_before_deep_verification(built, monkeypatch):
    import stpg_runner.execution as execution

    def forbidden(*args, **kwargs):
        pytest.fail("known old SDK identity reached expensive semantics")

    monkeypatch.setattr(execution, "tool_fingerprint", lambda: "0" * 64)
    monkeypatch.setattr(execution, "verify_bundle", forbidden)
    with pytest.raises(RunnerError, match="installed compiler/SDK differs"):
        prepare_run(built.bundle_dir, manifest_sha256=built.manifest_sha256)


def test_snapshot_rejection_assertion_detects_a_disabled_inventory_guard(built, tmp_path, monkeypatch):
    import stpg_runner.execution as execution

    monkeypatch.setattr(execution, "_verify_inventory", lambda *args, **kwargs: None)
    with pytest.raises(AssertionError):
        test_post_semantic_edit_is_rejected_by_snapshot_guard(built, tmp_path, monkeypatch, "metadata")


def test_offline_relocated_plan_remains_noncontact(built, tmp_path):
    target = tmp_path / "relocated public bundle"
    shutil.copytree(built.bundle_dir, target)
    plan = prepare_run(target, manifest_sha256=built.manifest_sha256)
    assert plan.bundle_dir == target and len(plan.segments) == 1
    assert plan.segments[0].expected["events"] == 6


def test_preopen_evidence_failure_prevents_open(built, tmp_path, monkeypatch):
    import stpg_runner.execution as execution
    original = execution.write_run_result

    def fail(path, report):
        if any(check["phase"] == "before_open" for check in report["port_checks"]):
            raise OSError("synthetic evidence storage failure")
        original(path, report)

    monkeypatch.setattr(execution, "write_run_result", fail)
    peer = FirmwarePeer()
    result = run_bundle(built.bundle_dir, **options(built, tmp_path, peer))
    assert result["outcome"] == "REJECT" and not result["result_persisted"]
    assert not result["stream_open_attempted"] and not peer.commands
