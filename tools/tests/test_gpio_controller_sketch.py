"""Compile the actual sketch with native GPIO/Serial stubs, not Arduino/hardware."""
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.stpg_host_only
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "controller_native"
PINS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 14, 15, 26]


@pytest.fixture(scope="module")
def sketch(tmp_path_factory):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("native g++ is unavailable (not an Arduino toolchain result)")
    output = tmp_path_factory.mktemp("controller-native") / "controller"
    result = subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(FIXTURES),
                             '-DSTPG_CONTROLLER_SKETCH="' + str(ROOT / "controller/firmware/rp2040_zero_gpio_toggle.ino") + '"',
                             str(FIXTURES / "harness.cpp"), "-o", str(output)], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return output


def invoke(sketch, commands):
    result = subprocess.run([str(sketch)], input="\n".join(commands) + "\n", text=True,
                             capture_output=True, check=True, timeout=5)
    boot, *steps = result.stderr.split("step ")
    actions = [step.splitlines()[1:] for step in steps]
    return result.stdout, boot.splitlines(), actions


def test_boot_all_requested_pins_hiz_without_enabling_fpga(sketch):
    _, boot, _ = invoke(sketch, [])
    assert boot == [row for pin in PINS for row in (f"init {pin}", f"dir {pin} 0", f"pulls_off {pin}")]


@pytest.mark.parametrize("pin", PINS)
def test_every_pin_latch_order_hiz_and_legacy_toggle(sketch, pin):
    text, _, actions = invoke(sketch, [f"{pin}l", f"{pin}n", f"{pin}n", f"{pin}n", f"{pin}h", f"{pin}z"])
    assert actions == [[f"pulls_off {pin}", f"put {pin} 0", f"dir {pin} 1"],
                       [f"dir {pin} 0", f"pulls_off {pin}"],
                       [f"pulls_off {pin}", f"put {pin} 1", f"dir {pin} 1"],
                       [f"pulls_off {pin}", f"put {pin} 0", f"dir {pin} 1"],
                       [f"pulls_off {pin}", f"put {pin} 1", f"dir {pin} 1"],
                       [f"dir {pin} 0", f"pulls_off {pin}"]]
    assert f"CH{pin} = HIGH" in text and f"CH{pin} = Hi-Z" in text


def test_legacy_status_stays_three_lines_and_extended_status_is_explicit(sketch):
    text, _, actions = invoke(sketch, ["s", "a"])
    blocks = text.split("Status:\n")
    assert blocks[2].splitlines() == [f"CH{pin} GPIO{pin} state=Hi-Z" for pin in (0, 1, 2)]
    assert blocks[3].splitlines() == [f"CH{pin} GPIO{pin} state=Hi-Z" for pin in PINS] + ["END"]
    assert actions == [[], []]


@pytest.mark.parametrize("command", ["9h", "16h", "27h", "014h", "14hh", "0h extra", "-1h", "999999999999h",
                                     "0h\x00junk", "0h\x1bjunk", "0h\x7fjunk"])
def test_invalid_command_never_touches_gpio(sketch, command):
    _, _, actions = invoke(sketch, [command])
    assert actions == [[]]


def test_overflow_discards_entire_line_not_only_its_prefix(sketch):
    text, _, actions = invoke(sketch, ["x" * 70 + "0h", "14h"])
    assert "Input too long" in text
    assert actions[0] == [] and actions[1] == ["pulls_off 14", "put 14 1", "dir 14 1"]


def test_versioned_nonce_sequence_and_manual_interference(sketch):
    token = "0123456789abcdef"
    text, _, actions = invoke(sketch, [f"i {token}", f"@{token}:1 14h", f"@{token}:1 15h",
                                      "@ffffffffffffffff:2 26l", f"@{token}:2 26n", f"@{token}:2 26z",
                                      "s", f"@{token}:3 2h"])
    assert f"STPG_GPIO_V1 {token} GPIO=" + ",".join(map(str, PINS)) in text
    assert f"@{token}:1 CH14 = HIGH" in text and f"@{token}:2 CH26 = Hi-Z" in text
    assert [bool(a) for a in actions] == [False, True, False, False, False, True, False, False]


def test_python_client_and_actual_sketch_share_the_versioned_protocol(sketch, tmp_path):
    from stpg_runner import manual_control
    from test_stpg_runner_controller import CONFIG, controller_ports
    from test_stpg_runner_execution import DEVICE, Clock
    class NativeSketch:
        def __init__(self):
            self.process = subprocess.Popen([str(sketch)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, bufsize=0)
            self.closed = 0
        def write(self, data):
            return self.process.stdin.write(data)
        def read(self, size):
            return self.process.stdout.readline(size)
        def close(self):
            self.closed += 1
            self.process.stdin.close()
            try:
                assert self.process.wait(timeout=5) == 0
                self.gpio = self.process.stderr.read().decode("ascii")
            finally:
                if self.process.poll() is None:
                    self.process.kill(); self.process.wait(timeout=5)
                self.process.stdout.close(); self.process.stderr.close()
    clock, peers = Clock(), []
    def factory(config):
        peer = NativeSketch(); peers.append(peer); return peer
    result = manual_control(CONFIG, output_dir=tmp_path / "reset", pin=0, pulse_ms=10, target=DEVICE,
                            confirm_control=True, stream_factory=factory, port_provider=controller_ports,
                            clock=clock, sleep=clock.sleep)
    assert result["outcome"] == "COMPLETED" and peers[0].closed == 1
    assert result["transport"]["last_acknowledged_states"] == {"0": "Hi-Z"}
    assert "put 0 0\ndir 0 1" in peers[0].gpio
