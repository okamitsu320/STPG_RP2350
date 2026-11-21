#include "pico/stdlib.h"
#include "hardware/clocks.h"
#ifdef PICO_ON_DEVICE
#include "pico/multicore.h"
#endif

#include "stpg_app.h"
#include "stpg_board.h"
#include "stpg_compare.h"
#include "stpg_flash.h"
#include "stpg_flash_xip.h"
#include "stpg_pattern_buffer.h"
#include "stpg_runner.h"
#include "stpg_stack_watermark.h"
#include "stpg_samples.h"
#include "stpg_status.h"
#include "stpg_usb_protocol.h"

volatile uint32_t g_main_init_stage_raw;
volatile uint32_t g_main_loop_heartbeat_count_raw;
volatile uint32_t g_main_loop_stage_raw;

static void stpg_clock_init(void) {
    set_sys_clock_khz(STPG_SYS_CLK_HZ / 1000u, true);
}

static void stpg_headless_boot_try_autorun(void) {
    if ((stpg_flash_capability_flags() & CAP_HEADLESS_BOOT) == 0u) {
        return;
    }

    stpg_flash_boot_config_info_t info;
    const stpg_err_t cfg_err = stpg_flash_get_boot_config(&info);
    if (cfg_err != ERR_NONE || !info.valid) {
        return;
    }
    if (info.boot_slot_id == STPG_BOOT_SLOT_DISABLED) {
        return;
    }
    if ((info.boot_flags & STPG_BOOT_FLAG_ENABLE_AUTO_RUN) == 0u) {
        return;
    }

    stpg_err_t err = stpg_buffer_flash_select_run(info.boot_slot_id);
    if (err != ERR_NONE) {
        stpg_buffer_reset();
        stpg_status_reset_pattern_info();
        stpg_status_reset_result_info();
        stpg_status_set_run_progress(0u);
        stpg_status_set_boot_error(err, info.boot_slot_id);
        stpg_status_set_state(STATE_IDLE);
        return;
    }

    stpg_status_set_state(STATE_READY);
    stpg_status_set_pattern_info(stpg_buffer_pattern_size(), stpg_buffer_event_count());
    stpg_status_set_run_progress(0u);
    err = stpg_runner_start(0u, 0u, 0u, 0u);
    if (err != ERR_NONE) {
        stpg_buffer_reset();
        stpg_status_reset_pattern_info();
        stpg_status_reset_result_info();
        stpg_status_set_run_progress(0u);
        stpg_status_set_boot_error(err, info.boot_slot_id);
        stpg_status_set_state(STATE_IDLE);
        return;
    }

    stpg_status_clear_error();
    stpg_status_clear_boot_error();
    stpg_status_set_state(STATE_RUNNING);
}

int main(void) {
#ifdef PICO_ON_DEVICE
    if (get_core_num() != 0u) {
        while (true) {
            tight_loop_contents();
        }
    }
#endif
    stpg_stack_watermark_core0_init_once();
    stpg_board_init_psram_cs1();
    g_main_init_stage_raw = 1u;
    stpg_status_init();
    g_main_init_stage_raw = 2u;
    stpg_clock_init();
    g_main_init_stage_raw = 3u;
    stdio_init_all();
    g_main_init_stage_raw = 4u;
    stpg_board_init_platform_gpio();
    stpg_board_init_safe_gpio();
    g_main_init_stage_raw = 5u;
    stpg_compare_reset();
    g_main_init_stage_raw = 6u;
    stpg_samples_reset();
    g_main_init_stage_raw = 7u;
    stpg_runner_init();
    stpg_flash_xip_init();
    g_main_init_stage_raw = 8u;
    stpg_usb_protocol_init();
    g_main_init_stage_raw = 9u;
    stpg_status_set_state(STATE_IDLE);
    g_main_init_stage_raw = 10u;
    stpg_headless_boot_try_autorun();
    g_main_init_stage_raw = 11u;

    while (true) {
        g_main_init_stage_raw = 12u;
        g_main_loop_heartbeat_count_raw += 1u;
        g_main_loop_stage_raw = 1u;
        stpg_usb_protocol_task();
        g_main_loop_stage_raw = 2u;
        stpg_runner_task();
        g_main_loop_stage_raw = 3u;
        tight_loop_contents();
    }
}
