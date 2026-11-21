#include "stpg_status.h"

#include <string.h>

#ifdef PICO_ON_DEVICE
#include "pico.h"
#include "hardware/clocks.h"
#else
#define __no_inline_not_in_flash_func(name) name
#endif

#include "stpg_board.h"
#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_flash.h"
#include "stpg_pattern_buffer.h"

static uint32_t stpg_status_runtime_tick_hz(void) {
#ifdef PICO_ON_DEVICE
    return clock_get_hz(clk_sys);
#else
    return STPG_TICK_HZ;
#endif
}

static uint32_t stpg_status_runtime_peri_hz(void) {
#ifdef PICO_ON_DEVICE
    return clock_get_hz(clk_peri);
#else
    return STPG_TICK_HZ;
#endif
}

static stpg_status_context_t g_status;

void stpg_status_init(void) {
    memset(&g_status, 0, sizeof(g_status));
    g_status.state = STATE_BOOT;
    g_status.error_code = ERR_NONE;
}

void stpg_status_set_state(stpg_state_t state) {
    g_status.state = state;
}

stpg_state_t stpg_status_get_state(void) {
    return g_status.state;
}

void stpg_status_set_error(stpg_err_t error_code) {
    g_status.error_code = error_code;
    if (error_code != ERR_NONE) {
        g_status.state = STATE_ERROR;
    }
}

void stpg_status_clear_error(void) {
    g_status.error_code = ERR_NONE;
}

void stpg_status_set_boot_error(stpg_err_t error_code, uint32_t slot_id) {
    g_status.last_boot_error = error_code;
    g_status.last_boot_slot = slot_id;
    g_status.error_code = error_code;
}

void stpg_status_clear_boot_error(void) {
    g_status.last_boot_error = ERR_NONE;
    g_status.last_boot_slot = 0u;
    if (g_status.state != STATE_ERROR) {
        g_status.error_code = ERR_NONE;
    }
}

void stpg_status_reset_pattern_info(void) {
    g_status.pattern_size = 0;
    g_status.event_count = 0;
    g_status.run_progress = 0;
}

void stpg_status_set_pattern_info(uint32_t pattern_size, uint32_t event_count) {
    g_status.pattern_size = pattern_size;
    g_status.event_count = event_count;
}

void __no_inline_not_in_flash_func(stpg_status_set_run_progress)(uint32_t run_progress) {
    g_status.run_progress = run_progress;
}

void stpg_status_reset_result_info(void) {
    g_status.result_flags = 0u;
    g_status.fail_count = 0u;
    g_status.sample_count = 0u;
}

void stpg_status_set_result_flags(uint32_t result_flags) {
    g_status.result_flags = result_flags;
}

void stpg_status_set_fail_count(uint32_t fail_count) {
    g_status.fail_count = fail_count;
}

void stpg_status_set_sample_count(uint32_t sample_count) {
    g_status.sample_count = sample_count;
}

void stpg_status_refresh_crc(StpgStatusResponse *out) {
    out->crc32 = stpg_crc32(out, STPG_RSP_SIZE - sizeof(out->crc32));
}

void stpg_status_build(uint16_t rsp, uint32_t seq, StpgStatusResponse *out) {
    memset(out, 0, sizeof(*out));
    out->magic = STPG_CMD_MAGIC;
    out->version = STPG_PROTO_VERSION;
    out->rsp = rsp;
    out->seq = seq;
    out->state = (uint32_t)g_status.state;
    out->capability = STPG_CAPABILITY_CURRENT | stpg_flash_capability_flags();
    out->allowed_cmds = stpg_allowed_cmds_for_state(g_status.state);
    out->active_pins = stpg_buffer_active_pins();
    out->pattern_size = g_status.pattern_size;
    out->event_count = g_status.event_count;
    out->run_progress = g_status.run_progress;
    out->result_flags = g_status.result_flags;
    out->fail_count = g_status.fail_count;
    out->sample_count = g_status.sample_count;
    out->error_code = (uint32_t)g_status.error_code;
    out->firmware_version = STPG_FW_VERSION;
    stpg_status_refresh_crc(out);
}

void stpg_status_build_clock_info(uint32_t seq, StpgStatusResponse *out) {
    stpg_status_build(RSP_CLOCK_INFO, seq, out);
    out->pattern_size = stpg_status_runtime_tick_hz();
    out->event_count = STPG_T_MIN_TICKS;
    out->run_progress = STPG_MAX_WAIT_TICKS;
    out->result_flags = STPG_TICK_HZ;
    out->fail_count = stpg_status_runtime_peri_hz();
    out->error_code = ERR_NONE;
    stpg_status_refresh_crc(out);
}
