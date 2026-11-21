"""Real SDK execution/preparation paths, independent counters and public inputs.

No report/constant-only oracle: injected protocol peers actually execute all
four compositions. Mutation cases must stop before serial/accessory contact.
"""
from concurrent.futures import ThreadPoolExecutor
import shutil

import pytest

from stpg_runner import CancellationToken, prepare_run, verify_bundle
from stpg_runner import build as builder, execution
from stpg_runner.accessories import run_with_accessories
from stpg_runner.external_accessory import run_with_external_accessory
from stpg_runner.artifacts import RunnerError, identity, read_json, write_json
from stpg_runner.preparation import _current_invocation, _invocation_scope
from stpg_runner.progress import Reporter
from test_external_accessory_process import bundle as process_bundle, config as external_config
from test_stpg_runner_controller import CONFIG, Peer as GpioPeer, controller_ports
from test_stpg_runner_execution import DEVICE, FirmwarePeer, Clock, ports

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(scope="module")
def built(process_bundle):
    # Keep the shared receipt fixtures independent of the separately licensed
    # analyzer. The child peer is a real process using only the Python stdlib.
    return None, process_bundle, None, None, None


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("verification reuse test attempted hardware contact")
    from stpg_runner import controller
    for module, name in ((execution, "open_serial"), (execution, "enumerate_ports"),
                         (controller, "open_controller_serial"), (controller, "enumerate_ports")):
        monkeypatch.setattr(module, name, forbidden)


@pytest.mark.parametrize("controlled,captured", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("warm", [False, True])
def test_one_deep_check_and_snapshot_for_every_composition(built, tmp_path, monkeypatch, controlled, captured, warm):
    _, bundle, *_ = built
    builder._clear_verification_receipts()
    if warm:
        assert verify_bundle(bundle.bundle_dir)["status"] == "PASS"
    original = builder._verify_bundle
    calls = []
    def count(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(builder, "_verify_bundle", count)
    events = []
    # A later invocation reuses only the semantic receipt, not the previous
    # snapshot/leases/identity/terminal response/output directory.
    for iteration in range(2):
        peer, clock = FirmwarePeer(), Clock()
        options = dict(stream_factory=lambda _: peer, port_provider=ports,
                       clock=clock, sleep=clock.sleep)
        gpio = GpioPeer()
        actions = {"kind": "stpg_gpio_action_plan", "schema_version": 1,
                   "manifest_sha256": bundle.manifest_sha256,
                   "actions": [{"segment": 0, "phase": "before_run", "pin": 14, "state": "Hi-Z"}]}
        execute = run_with_accessories
        if captured:
            spec, _ = external_config(tmp_path / f"config-{iteration}", bundle)
            execute = run_with_external_accessory
            options["spec"] = spec
        result = execute(
            bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE,
            output_dir=tmp_path / f"run-{iteration}", confirm_run=True, on_progress=events.append,
            controller=CONFIG if controlled else None, control_plan=actions if controlled else None,
            controller_stream_factory=lambda _: gpio, controller_port_provider=controller_ports, **options)
        assert result["run"]["outcome"] == "COMPLETED", result
        if captured:
            assert result["external"]["outcome"] == "COMPLETED"
            assert result["external"]["spawn_attempts"] == 1
            assert result["external"]["stdin_close_attempts"] == 1
            assert result["external"]["adopted"]
        assert peer.close_count == 1 and peer.payloads
        assert gpio.closed == int(controlled)
        assert _current_invocation() is None
    assert len(calls) == int(not warm)
    assert sum(event.stage == "snapshot" and event.event == "start" for event in events) == 2
    assert any(event.stage == "bundle_check" and "process_receipt" in event.detail for event in events)
    # The exact wire protocol/terminal judge still run twice, never cached.
    records = list(tmp_path.rglob("run.json.gz"))
    assert len(records) == 2
    assert all(read_json(path)["transactions"] for path in records)


def copied_bundle(built, tmp_path):
    _, bundle, *_ = built
    root = tmp_path / "bundle"
    shutil.copytree(bundle.bundle_dir, root)
    pin = bundle.manifest_sha256
    assert verify_bundle(root, manifest_sha256=pin)["status"] == "PASS"
    return root, pin


@pytest.mark.parametrize("mutation", ["changed", "missing", "extra", "symlink", "manifest"])
def test_receipt_rechecks_all_inventory_bytes_paths_and_file_set(built, tmp_path, mutation):
    root, pin = copied_bundle(built, tmp_path)
    path = root / "verify.py"
    if mutation == "changed":
        path.write_bytes(path.read_bytes() + b"\n# public mutation\n")
    elif mutation == "missing":
        path.unlink()
    elif mutation == "extra":
        (root / "unlisted.txt").write_text("public mutation")
    elif mutation == "symlink":
        external = tmp_path / "outside.py"
        path.rename(external)
        path.symlink_to(external)
    else:
        path = root / builder.BUNDLE_NAME
        path.write_bytes(path.read_bytes() + b" ")
    result = verify_bundle(root, manifest_sha256=pin, reuse=True)
    assert result["status"] == "REJECT", result
    assert prepare_rejects(root, pin)


def prepare_rejects(root, pin):
    with pytest.raises(RunnerError):
        prepare_run(root, manifest_sha256=pin)
    return True


def test_rehashed_metadata_cannot_reuse_old_receipt(built, tmp_path, monkeypatch):
    root, pin = copied_bundle(built, tmp_path)
    manifest = read_json(root / builder.BUNDLE_NAME)
    # Change a semantic contract and honestly update its outer file hash.
    manifest["patterns"][0]["expected"]["events"] += 1
    write_json(root / builder.BUNDLE_NAME, manifest)
    new_pin = identity(root / builder.BUNDLE_NAME)["sha256"]
    original, calls = builder._verify_bundle, []
    def count(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(builder, "_verify_bundle", count)
    result = verify_bundle(root, manifest_sha256=new_pin, reuse=True)
    assert result["status"] == "REJECT" and calls == [True]
    assert "expectations differ" in str(result["errors"])


def test_receipts_are_bounded_root_and_tool_bound_not_a_caller_pass(built, tmp_path, monkeypatch):
    root, pin = copied_bundle(built, tmp_path)
    builder._clear_verification_receipts()
    # Saving an alleged PASS inside the bundle is an unlisted-file failure,
    # never an admission token.
    write_json(root / "receipt.json", {"status": "PASS", "manifest_sha256": pin})
    assert verify_bundle(root, manifest_sha256=pin, reuse=True)["status"] == "REJECT"
    (root / "receipt.json").unlink()
    monkeypatch.setattr(builder, "_VERIFICATION_RECEIPT_LIMIT", 2)
    for i in range(3):
        moved = tmp_path / f"copy-{i}"
        shutil.copytree(root, moved)
        assert verify_bundle(moved, manifest_sha256=pin, reuse=True)["check_mode"] == "semantic"
    assert len(builder._verification_receipts) == 2
    original = builder._verify_bundle
    calls = []
    def count(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(builder, "_verify_bundle", count)
    assert verify_bundle(tmp_path / "copy-0", manifest_sha256=pin, reuse=True)["check_mode"] == "semantic"
    assert calls == [True]  # evicted; a copied/moved root cannot borrow a token
    monkeypatch.setattr(builder, "tool_fingerprint", lambda: "0" * 64)
    assert verify_bundle(tmp_path / "copy-0", manifest_sha256=pin, reuse=True)["check_mode"] == "semantic"
    assert len(calls) == 2


@pytest.mark.parametrize("mutation", ["expected", "flags", "payload", "manifest", "extra", "tool"])
def test_shared_snapshot_mutations_reject_before_core_open(built, tmp_path, monkeypatch, mutation):
    root, pin = copied_bundle(built, tmp_path)
    clock, peer = Clock(), FirmwarePeer()
    mutated = []
    def progress(event):
        if event.stage != "device_preflight" or mutated:
            return
        mutated.append(True)
        plan = _current_invocation().plans[(root, pin)][0]
        if mutation == "expected":
            plan.segments[0].expected["events"] = 0
        elif mutation == "flags":
            flags, a1, a2, a3 = plan.segments[0].run_args
            object.__setattr__(plan.segments[0], "run_args", (flags ^ 1, a1, a2, a3))
        elif mutation == "payload":
            object.__setattr__(plan.segments[0], "payload", b"public mutation")
        elif mutation == "manifest":
            path = root / builder.BUNDLE_NAME
            path.write_bytes(path.read_bytes() + b" ")
        elif mutation == "extra":
            (root / "unlisted.txt").write_text("public mutation")
        else:
            monkeypatch.setattr(execution, "tool_fingerprint", lambda: "0" * 64)
    result = execution.run_bundle(root, manifest_sha256=pin, device=DEVICE, confirm_run=True,
                                  output_dir=tmp_path / "run", stream_factory=lambda _: peer, port_provider=ports,
                                  clock=clock, sleep=clock.sleep, on_progress=progress)
    assert mutated and result["outcome"] == "REJECT", result["errors"]
    assert not result["stream_open_attempted"] and not peer.commands and peer.close_count == 0
    assert _current_invocation() is None


def test_cancelled_receipt_and_invocation_cleanup(built):
    _, bundle, *_ = built
    assert verify_bundle(bundle.bundle_dir)["status"] == "PASS"
    token = CancellationToken(); token.cancel()
    with pytest.raises(RunnerError, match="cancelled"):
        prepare_run(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, cancel=token)
    @_invocation_scope
    def broken():
        assert _current_invocation() is not None
        raise ValueError("public injected failure")
    with pytest.raises(ValueError):
        broken()
    assert _current_invocation() is None


def test_threaded_invocations_do_not_share_snapshot_state(built):
    _, bundle, *_ = built
    @_invocation_scope
    def prepare():
        state = _current_invocation()
        plan = prepare_run(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256)
        assert prepare_run(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256) is plan
        return state, plan
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: prepare(), range(2)))
    assert first[0] is not second[0] and first[1] is not second[1]
    assert _current_invocation() is None


@pytest.mark.parametrize("old,new", [
    ("@_invocation_scope\ndef run_bundle", "def run_bundle"),
    ("or _plan_seal(plan) != seal", "or False"),
    ("canonical_json(manifest) != manifest_bytes", "False"),
    ("tool_fingerprint() != fingerprint", "False"),
    ("_verify_inventory(root, manifest, manifest_sha256=manifest_sha256, progress=progress, stage=\"shared_files\")", "pass"),
    ("Reporter(None, token, clock=clock)) is not plan", "Reporter(None, token, clock=clock)) is None"),
    ("cancel=progress.cancel, reuse=True", "cancel=progress.cancel, reuse=False"),
])
def test_gate_rejects_removal_of_each_new_guard(old, new):
    from stpg_runner.execution_gate import check_execution_contract
    source = __import__("pathlib").Path(execution.__file__).read_text()
    assert old in source and check_execution_contract(execution_source=source)["status"] == "PASS"
    assert check_execution_contract(execution_source=source.replace(old, new))["status"] == "REJECT"


def test_receipt_inventory_mutation_is_detected_by_live_negative_test(built, tmp_path, monkeypatch):
    # A real weakened implementation, not a constant oracle or a test that
    # merely duplicates production judgements. The normal case passes above.
    original = builder._verify_inventory
    with monkeypatch.context() as mutation:
        mutation.setattr(builder, "_verify_inventory", lambda *args, **kw: None)
        with pytest.raises(AssertionError):
            test_receipt_rechecks_all_inventory_bytes_paths_and_file_set(built, tmp_path, "extra")
    assert builder._verify_inventory is original
