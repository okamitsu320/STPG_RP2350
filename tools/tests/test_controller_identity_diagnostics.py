"""Existing identity reads become inspectable; no extra request or relaxed admission."""
from dataclasses import replace

import pytest

from stpg import constants as c
from stpg_runner.artifacts import read_json
from stpg_runner.controller_gate import check_controller_contract
from test_stpg_runner_controller import Peer, bundle, execute, manual
from test_stpg_runner_execution import FirmwarePeer

pytestmark = pytest.mark.stpg_host_only


@pytest.mark.parametrize("fragment", [1, 7, 256])
def test_known_boot_and_exact_identity_are_classified_without_extra_requests(tmp_path, fragment):
    class Fragmented(Peer):
        def read(self,size):
            before = self.clock.now
            data = super().read(size)
            self.clock.now = before + 0.001  # fragmentation within the unchanged 5s limit
            return data
    peer = Fragmented(startup=True, fragment=fragment)
    result,_ = manual(tmp_path, peer)
    assert result["outcome"] == "COMPLETED" and peer.closed == 1 and len(peer.writes) == 2
    rows = [row for row in result["transport"]["journal"] if row["event"] == "identity.line"]
    assert [row["disposition"] for row in rows] == ["STARTUP"]*3 + ["MATCH"]
    assert not any(row["disposition"].startswith("REJECT") for row in rows)


def test_bad_identity_is_preserved_and_stops_before_gpio_or_stpg_run(bundle, tmp_path):
    gpio = Peer(bad_at=1)
    result,stpg,_ = execute(bundle, tmp_path, gpio=gpio)
    assert result["run"]["outcome"] == result["control"]["outcome"] == "REJECT"
    assert len(gpio.writes) == 1 and gpio.writes[0].startswith(b"i ")
    assert gpio.applied == [] and gpio.closed == 1
    assert stpg.commands[-1].cmd == c.CMD_UPLOAD_END and not any(cmd.cmd == c.CMD_RUN for cmd in stpg.commands)
    saved = read_json(tmp_path / "run/control.json")
    row = next(row for row in saved["transport"]["journal"] if row["event"] == "identity.line")
    assert row["disposition"] == "REJECT" and row["line"] == "WRONG IDENTITY"
    assert check_controller_contract()["status"] == "PASS"


def test_trailing_bytes_are_not_mistaken_for_success(tmp_path):
    class Trailing(Peer):
        def write(self,raw):
            n = super().write(raw)
            if len(self.writes) == 1:
                self.rx.extend(b"UNEXPECTED\n")
            return n
    peer = Trailing()
    result,_ = manual(tmp_path, peer)
    assert result["outcome"] == "REJECT" and len(peer.writes) == 1 and peer.applied == [] and peer.closed == 1
    rows = [row for row in result["transport"]["journal"] if row["event"] == "identity.line"]
    assert rows[-1]["disposition"] == "REJECT_TRAILING_DATA"


def test_stpg_ready_start_rejection_never_contacts_or_diagnoses_the_controller(bundle, tmp_path):
    stpg = FirmwarePeer()
    stpg.last = replace(stpg.last, state=c.STATE_READY)
    gpio = Peer()
    result,_,_ = execute(bundle, tmp_path, peer=stpg, gpio=gpio)
    assert result["run"]["outcome"] == "REJECT"
    assert [cmd.cmd for cmd in stpg.commands] == [c.CMD_INFO] and stpg.close_count == 1
    assert result["control"]["outcome"] == "INCOMPLETE"
    assert gpio.writes == [] and gpio.closed == 0
    assert result["control"]["transport"]["journal"] == []
    assert result["control"]["transport"]["close_attempts"] == 0
