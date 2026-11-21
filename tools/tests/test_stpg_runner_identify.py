"""One-shot no-GPIO identity/status exchanges, all streams and metadata injected."""
from dataclasses import replace

import pytest

from stpg_runner import CancellationToken, identify_controller
from stpg_runner import controller as module
from stpg_runner.artifacts import RunnerError, read_json, write_json
from stpg_runner.device import DeviceLease
from test_stpg_runner_controller import CONFIG, Peer, controller_ports

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("identity test attempted real hardware contact")
    monkeypatch.setattr(module, "open_controller_serial", forbidden)
    monkeypatch.setattr(module, "enumerate_ports", forbidden)


def identify(tmp_path, peer=None, config=CONFIG, **kwargs):
    peer = peer or Peer()
    options = dict(output_dir=tmp_path / "identity", confirm_identify=True,
                   stream_factory=lambda _: peer, port_provider=lambda: controller_ports(config), clock=peer.clock)
    options.update(kwargs)
    return identify_controller(config, **options), peer


@pytest.mark.parametrize("legacy", [False, True])
def test_one_request_no_gpio_and_exact_persisted_prefix(tmp_path, legacy):
    config = replace(CONFIG, protocol="legacy-3", allow_high=(2,), legacy_pins=(0, 1, 2)) if legacy else CONFIG
    result, peer = identify(tmp_path, Peer(legacy=legacy), config=config)
    assert result["status"] == "IDENTIFIED" and result["result_persisted"] is True
    assert len(peer.writes) == peer.closed == 1 and not peer.applied
    assert peer.writes[0] == b"s\n" if legacy else peer.writes[0].startswith(b"i ")
    assert result["pin_commands_written"] == 0 and result["physical_readback"] is False
    assert result["legacy_states"] == {"0": "Hi-Z", "1": "Hi-Z", "2": "Hi-Z"} if legacy else not result["legacy_states"]
    assert read_json(tmp_path / "identity" / "identify.json") == result


@pytest.mark.parametrize("prefix", [0, 1, 10, 40])
def test_partial_identity_never_falls_back_or_sends_pin_commands(tmp_path, prefix):
    result, peer = identify(tmp_path, Peer(partial_at=1, prefix=prefix))
    assert result["status"] == "REJECT" and result["result_persisted"] and len(peer.writes) == peer.closed == 1
    assert not peer.applied and len(bytes.fromhex(result["transport"]["pending_hex"])) == prefix


@pytest.mark.parametrize("fault", ["wrong", "write", "short", "close"])
def test_identity_failures_preserve_evidence_and_close_once(tmp_path, fault):
    peer = Peer(bad_at=1 if fault == "wrong" else 0, fail_at=1 if fault == "write" else 0,
                short_at=1 if fault == "short" else 0, close_error=fault == "close")
    result, peer = identify(tmp_path, peer)
    assert result["status"] == "REJECT" and result["result_persisted"] and peer.closed == len(peer.writes) == 1
    assert not peer.applied and result["errors"]
    if fault == "write":
        assert next(r for r in result["transport"]["journal"] if r["event"] == "write.begin")["written_bytes"] is None
    if fault == "close":
        assert result["transport"]["close_attempts"] == 1 and result["transport"]["close_successes"] == 0
        assert any(r.get("raw_hex") for r in result["transport"]["journal"])


def test_active_controller_lease_and_precancel_do_not_open(tmp_path):
    with DeviceLease(CONFIG):
        result, peer = identify(tmp_path / "lease")
    assert result["status"] == "REJECT" and not peer.writes and not peer.closed
    token = CancellationToken(); token.cancel()
    result, peer = identify(tmp_path / "cancel", cancel=token)
    assert result["status"] == "CANCELLED" and not peer.writes and not peer.closed


def test_no_default_endpoint_and_no_overwrite(tmp_path):
    result, peer = identify(tmp_path, port_provider=lambda: [])
    assert result["status"] == "REJECT" and not peer.writes and not peer.closed
    before = (tmp_path / "identity" / "identify.json").read_bytes()
    with pytest.raises(FileExistsError):
        identify(tmp_path)
    assert (tmp_path / "identity" / "identify.json").read_bytes() == before


def test_identify_requires_explicit_intent(tmp_path):
    with pytest.raises(RunnerError):
        identify(tmp_path, confirm_identify=False)
    assert not (tmp_path / "identity").exists()


def test_cancellation_during_metadata_stops_before_open(tmp_path):
    token = CancellationToken()
    def ports():
        token.cancel()
        return controller_ports()
    result, peer = identify(tmp_path, cancel=token, port_provider=ports)
    assert result["status"] == "CANCELLED" and not peer.writes and not peer.closed
    assert not any(r["event"] == "open.begin" for r in result["transport"]["journal"])


@pytest.mark.parametrize("initial", [False, True])
def test_checkpoint_failure_never_loses_in_memory_prefix_or_claims_success(tmp_path, monkeypatch, initial):
    original, calls = module.write_json, []
    def failing(path, value):
        calls.append(path)
        if initial or len(calls) == 2:
            raise OSError("synthetic identify evidence failure")
        return original(path, value)
    monkeypatch.setattr(module, "write_json", failing)
    peer = Peer()
    if initial:
        with pytest.raises(OSError):
            identify(tmp_path, peer)
        assert not peer.writes and not peer.closed
    else:
        result, peer = identify(tmp_path, peer)
        assert result["status"] == "REJECT" and not result["result_persisted"] and peer.closed == 1
        assert any(r.get("raw_hex") for r in result["transport"]["journal"])
        assert read_json(tmp_path / "identity" / "identify.json")["status"] == "PENDING"


def test_cli_uses_the_same_no_gpio_wrapper_and_codes(tmp_path, monkeypatch, capsys):
    import json
    from stpg_runner import cli
    path = tmp_path / "binding.json"; write_json(path, CONFIG.to_dict())
    original, peer = module.identify_controller, Peer()
    def injected(*args, **kwargs):
        return original(*args, **kwargs, stream_factory=lambda _: peer, port_provider=controller_ports, clock=peer.clock)
    monkeypatch.setattr(module, "identify_controller", injected)
    assert cli.main(["control", "identify", "--controller-config", str(path), "--output-dir", str(tmp_path / "cli"),
                     "--confirm-identify"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "IDENTIFIED" and result["pin_commands_written"] == 0 and not peer.applied
