from stpg.constants import (
    CAPABILITY_V01,
    CMD_GET_STATUS,
    RSP_STATUS,
    STATE_IDLE,
    STPG_ACTIVE_PINS_V01,
    STPG_CMD_SIZE,
    STPG_FW_VERSION,
    STPG_RSP_SIZE,
)
from stpg.protocol import StpgCommand, StpgStatusResponse


def test_command_roundtrip() -> None:
    command = StpgCommand(CMD_GET_STATUS, seq=7, arg0=1, arg1=2, arg2=3, arg3=4)
    data = command.pack()
    assert len(data) == STPG_CMD_SIZE
    assert StpgCommand.unpack(data) == command.__class__(
        CMD_GET_STATUS,
        seq=7,
        arg0=1,
        arg1=2,
        arg2=3,
        arg3=4,
        packet_crc32=StpgCommand.unpack(data).packet_crc32,
    )


def test_response_roundtrip() -> None:
    response = StpgStatusResponse(
        RSP_STATUS,
        seq=7,
        state=STATE_IDLE,
        capability=CAPABILITY_V01,
        allowed_cmds=0x39,
        active_pins=STPG_ACTIVE_PINS_V01,
        firmware_version=STPG_FW_VERSION,
    )
    data = response.pack()
    assert len(data) == STPG_RSP_SIZE
    unpacked = StpgStatusResponse.unpack(data)
    assert unpacked.rsp == response.rsp
    assert unpacked.seq == response.seq
    assert unpacked.state == response.state
    assert unpacked.capability == response.capability
    assert unpacked.firmware_version == response.firmware_version
