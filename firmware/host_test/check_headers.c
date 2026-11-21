#include "stpg_board.h"
#include "stpg_common.h"
#include "stpg_error.h"
#include "stpg_pattern.h"
#include "stpg_protocol.h"
#include "stpg_state.h"

int main(void) {
    if (STPG_CLOCK_MODE != STPG_CLOCK_MODE_200MHZ ||
        STPG_TICK_HZ != 200000000u ||
        STPG_SYS_CLK_HZ != 200000000u) {
        return 1;
    }
    if (STPG_STREAM_SLOT_COUNT != 3u || STPG_STREAM_BUFFER_EVENTS != 2048u) {
        return 1;
    }
    return stpg_allowed_cmds_for_state(STATE_IDLE) == 0x7Bu ? 0 : 1;
}
