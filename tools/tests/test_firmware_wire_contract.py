"""Compare actual C headers/packed frames with Python, without a device or SDK.

Names come from the C declarations, values from a compiled native C probe. The
Python constant sets are checked in both directions; adding a C command without
its Python counterpart cannot silently leave this test unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from stpg import constants as host
from stpg.protocol import COMMAND_STRUCT, RESPONSE_STRUCT, StpgCommand, StpgStatusResponse


ROOT = Path(__file__).resolve().parents[2]
ENUM_GROUPS = ("CMD_", "RSP_", "ERR_", "STATE_")
MACROS = ("STPG_CMD_MAGIC", "STPG_PROTO_VERSION", "STPG_CMD_SIZE", "STPG_RSP_SIZE",
          "CAP_STREAM_WAIT_WATCHDOG")
COMMAND_VALUES = dict(cmd=host.CMD_RUN, seq=0x10203040, arg0=0x12345678,
                      arg1=0x89ABCDEF, arg2=0x55667788, arg3=0xEEFF0011)
RESPONSE_VALUES = dict(rsp=host.RSP_STATUS, seq=0x10203040, state=host.STATE_ERROR,
                       capability=0x01020304, allowed_cmds=0x05060708, active_pins=24,
                       pattern_size=0x11223344, event_count=0x55667788, run_progress=0x1020304,
                       result_flags=host.STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW, fail_count=342,
                       sample_count=0x12345678, error_code=host.ERR_BAD_CRC,
                       firmware_version=0xAABBCCDD)


def assert_constants_agree(observed):
    for prefix in ENUM_GROUPS:
        expected = {name: value for name, value in vars(host).items() if name.startswith(prefix)}
        actual = {name: value for name, value in observed.items() if name.startswith(prefix)}
        assert actual == expected, prefix
    for name in MACROS:
        assert observed[name] == getattr(host, name), name


@dataclass
class Oracle:
    constants: dict[str, int]
    sizes: dict[str, int]
    frames: dict[str, bytes]


@pytest.fixture(scope="module")
def c_oracle(tmp_path_factory):
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if compiler is None:
        pytest.skip("native C compiler missing: C/Python wire cross-check NOT_RUN")
    if sys.byteorder != "little":
        pytest.skip("native packed-structure oracle requires a little-endian host")
    folder = tmp_path_factory.mktemp("wire-contract")
    include = ROOT / "firmware/include"
    declarations = "\n".join((include / name).read_text(encoding="utf-8") for name in
                             ("stpg_protocol.h", "stpg_error.h", "stpg_state.h"))
    names = re.findall(r"^\s*((?:CMD_|RSP_|ERR_|STATE_)[A-Z0-9_]+)\s*=", declarations, re.MULTILINE)
    assert names and len(names) == len(set(names))
    prints = "\n".join(f'    printf("V {name} %u\\n", (unsigned){name});' for name in sorted(names) + list(MACROS))
    initializers = []
    for variable, type_name, values in (("command", "StpgCommand", COMMAND_VALUES),
                                        ("response", "StpgStatusResponse", RESPONSE_VALUES)):
        fields = {"magic": "STPG_CMD_MAGIC", "version": "STPG_PROTO_VERSION", **values}
        initializer = ", ".join(f".{key} = {value}" for key, value in fields.items())
        initializers.append(f"    {type_name} {variable} = {{ {initializer} }};")
        initializers.append(f"    {variable}.crc32 = stpg_crc32(&{variable}, offsetof({type_name}, crc32));")
        initializers.append(f'    printf("S {variable} %u\\n", (unsigned)sizeof({variable}));')
        initializers.append(f'    dump("{variable}", &{variable}, sizeof({variable}));')
    source = folder / "wire.c"
    source.write_text('''#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include "stpg_protocol.h"
#include "stpg_error.h"
#include "stpg_state.h"
#include "stpg_crc32.h"
static void dump(const char *name, const void *ptr, size_t count) {
    const uint8_t *bytes = ptr;
    printf("B %s ", name);
    for (size_t i = 0; i < count; ++i) printf("%02x", bytes[i]);
    printf("\\n");
}
int main(void) {
''' + prints + "\n" + "\n".join(initializers) + "\n    return 0;\n}\n", encoding="utf-8")
    executable = folder / "wire-probe"
    built = subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(include),
                            str(source), str(ROOT / "firmware/src/crc32.c"), "-o", str(executable)],
                           capture_output=True, text=True, timeout=30)
    assert built.returncode == 0, built.stdout + built.stderr
    executed = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert executed.returncode == 0, executed.stderr
    values, sizes, frames = {}, {}, {}
    for line in executed.stdout.splitlines():
        kind, name, value = line.split()
        if kind == "V":
            assert name not in values
            values[name] = int(value)
        elif kind == "S":
            assert name not in sizes
            sizes[name] = int(value)
        else:
            assert kind == "B" and name not in frames
            frames[name] = bytes.fromhex(value)
    assert set(frames) == set(sizes) == {"command", "response"}
    return Oracle(values, sizes, frames)


def test_all_c_command_response_error_and_state_values_match_python(c_oracle):
    assert_constants_agree(c_oracle.constants)


def test_c_packed_frames_and_crc_match_python_serializers(c_oracle):
    assert c_oracle.sizes == {"command": COMMAND_STRUCT.size, "response": RESPONSE_STRUCT.size}
    assert c_oracle.frames["command"] == StpgCommand(**COMMAND_VALUES).pack()
    assert c_oracle.frames["response"] == StpgStatusResponse(**RESPONSE_VALUES).pack()
    decoded_command = StpgCommand.unpack(c_oracle.frames["command"])
    decoded_response = StpgStatusResponse.unpack(c_oracle.frames["response"])
    for key, value in COMMAND_VALUES.items():
        assert getattr(decoded_command, key) == value
    for key, value in RESPONSE_VALUES.items():
        assert getattr(decoded_response, key) == value


@pytest.mark.parametrize("name", ["CMD_RUN", "RSP_STATUS", "ERR_BAD_CRC", "STATE_ERROR", "STPG_CMD_MAGIC",
                                  "CAP_STREAM_WAIT_WATCHDOG"])
def test_a_one_sided_value_change_is_detected(c_oracle, monkeypatch, name):
    monkeypatch.setattr(host, name, getattr(host, name) ^ 1)
    with pytest.raises(AssertionError):
        assert_constants_agree(c_oracle.constants)


def test_a_new_c_command_without_a_python_declaration_is_detected(c_oracle):
    changed = {**c_oracle.constants, "CMD_PUBLIC_TEST_ONLY_NEW": 0x1234}
    with pytest.raises(AssertionError):
        assert_constants_agree(changed)
