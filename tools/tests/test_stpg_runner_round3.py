"""Additional actual-callsite admission tests; synthetic peers, never hardware."""
import pytest

from stpg_runner import manual_control
from stpg_runner.artifacts import RunnerError
from test_stpg_runner_controller import CONFIG, Peer, controller_ports

pytestmark = pytest.mark.stpg_host_only


@pytest.mark.parametrize("state", ["", False, 0, [], {}])
def test_falsey_manual_state_is_not_silently_changed_to_low(tmp_path, state):
    peer = Peer()
    with pytest.raises(RunnerError):
        manual_control(CONFIG, output_dir=tmp_path / "manual", pin=14, state=state, confirm_control=True,
                       stream_factory=lambda _: peer, port_provider=controller_ports,
                       clock=peer.clock, sleep=peer.clock.sleep)
    assert not peer.writes and not peer.applied and not peer.closed
    assert not (tmp_path / "manual").exists()
