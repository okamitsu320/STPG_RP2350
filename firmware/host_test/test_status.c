#include "stpg_status.h"

#include <stdio.h>

#include "stpg_protocol.h"

static int expect_u32(uint32_t actual, uint32_t expected, const char *label) {
    if (actual != expected) {
        fprintf(stderr, "%s: expected %u got %u\n", label, expected, actual);
        return 1;
    }
    return 0;
}

int main(void) {
    StpgStatusResponse rsp;

    stpg_status_init();
    stpg_status_set_state(STATE_IDLE);
    stpg_status_set_pattern_info(123u, 7u);
    stpg_status_set_run_progress(5u);
    stpg_status_build(RSP_STATUS, 42u, &rsp);

    if (expect_u32(rsp.rsp, RSP_STATUS, "rsp")) return 1;
    if (expect_u32(rsp.seq, 42u, "seq")) return 1;
    if (expect_u32(rsp.state, STATE_IDLE, "state")) return 1;
    if (expect_u32(rsp.capability, STPG_CAPABILITY_CURRENT | CAP_FLASH_SLOTS | CAP_UPLOAD_FLASH | CAP_HEADLESS_BOOT, "capability")) return 1;
    if (expect_u32(rsp.active_pins, 0u, "idle_active_pins")) return 1;
    if (expect_u32(rsp.pattern_size, 123u, "pattern_size")) return 1;
    if (expect_u32(rsp.event_count, 7u, "event_count")) return 1;
    if (expect_u32(rsp.run_progress, 5u, "run_progress")) return 1;
    if (expect_u32(rsp.error_code, ERR_NONE, "error_code")) return 1;
    if (expect_u32(rsp.firmware_version, STPG_FW_VERSION, "firmware_version")) return 1;
    if (expect_u32(rsp.allowed_cmds, stpg_allowed_cmds_for_state(STATE_IDLE), "idle_allowed_cmds")) return 1;

    stpg_status_set_result_flags(7u);
    stpg_status_set_fail_count(8u);
    stpg_status_set_sample_count(9u);
    stpg_status_build(RSP_STATUS, 43u, &rsp);
    if (expect_u32(rsp.result_flags, 7u, "result_flags")) return 1;
    if (expect_u32(rsp.fail_count, 8u, "fail_count")) return 1;
    if (expect_u32(rsp.sample_count, 9u, "sample_count")) return 1;

    stpg_status_set_state(STATE_RUN_DRAINING);
    stpg_status_build(RSP_STATUS, 77u, &rsp);
    if (expect_u32(rsp.allowed_cmds, stpg_allowed_cmds_for_state(STATE_RUN_DRAINING), "draining_allowed_cmds")) return 1;

    if (expect_u32(stpg_allowed_cmds_for_state(STATE_HOLD_AWAITING_NEXT),
                   ALLOW_UPLOAD | ALLOW_RUN | ALLOW_STATUS |
                       ALLOW_RESET | ALLOW_INFO | ALLOW_RESULT_READ,
                   "hold_allowed_cmds")) return 1;

    stpg_status_set_boot_error(ERR_FLASH_BAD_META, 2u);
    stpg_status_set_state(STATE_IDLE);
    stpg_status_build(RSP_STATUS, 88u, &rsp);
    if (expect_u32(rsp.state, STATE_IDLE, "boot_idle_state")) return 1;
    if (expect_u32(rsp.error_code, ERR_FLASH_BAD_META, "boot_error_sticky")) return 1;
    stpg_status_clear_boot_error();
    stpg_status_build(RSP_STATUS, 89u, &rsp);
    if (expect_u32(rsp.error_code, ERR_NONE, "boot_error_cleared")) return 1;

    stpg_status_set_state(STATE_DONE);
    stpg_status_build_clock_info(99u, &rsp);
    if (expect_u32(rsp.rsp, RSP_CLOCK_INFO, "clock_rsp")) return 1;
    if (expect_u32(rsp.seq, 99u, "clock_seq")) return 1;
    if (expect_u32(rsp.state, STATE_DONE, "clock_state")) return 1;
    if (expect_u32(rsp.pattern_size, STPG_TICK_HZ, "tick_hz")) return 1;
    if (expect_u32(rsp.event_count, STPG_T_MIN_TICKS, "t_min_ticks")) return 1;
    if (expect_u32(rsp.run_progress, STPG_MAX_WAIT_TICKS, "max_wait_ticks")) return 1;
    if (expect_u32(rsp.result_flags, STPG_TICK_HZ, "target_tick_hz")) return 1;
    if (expect_u32(rsp.fail_count, STPG_TICK_HZ, "peri_hz")) return 1;
    if (expect_u32(rsp.error_code, ERR_NONE, "clock_error")) return 1;

    return 0;
}
