#include "stpg_command_dispatch.h"

#include <stdbool.h>

#include "stpg_compare.h"
#include "stpg_common.h"
#include "stpg_error.h"
#include "stpg_flash.h"
#include "stpg_fpu_access.h"
#include "stpg_flash_xip.h"
#include "stpg_pattern_buffer.h"
#include "stpg_runner.h"
#include "stpg_samples.h"
#include "stpg_status.h"
#include "stpg_usb_protocol.h"

/* Command dispatch runs on Core 0. A telemetry response uses one shared
 * snapshot per producer, never a multi-kilobyte temporary on its 2 KiB
 * stack. The two owners remain separate because one response reads both. */
static stpg_stream_telemetry_t g_dispatch_runner_telemetry;
static stpg_stream_buffer_telemetry_t g_dispatch_buffer_telemetry;

static bool stpg_is_info_command(uint16_t cmd) {
    return cmd == CMD_HELLO || cmd == CMD_INFO || cmd == CMD_GET_STATUS || cmd == CMD_GET_CLOCK_INFO
        || cmd == CMD_GET_RATE_INFO || cmd == CMD_GET_STREAM_TELEMETRY;
}

static bool stpg_is_upload_command(uint16_t cmd) {
    return cmd == CMD_UPLOAD_BEGIN || cmd == CMD_UPLOAD_DATA || cmd == CMD_UPLOAD_END;
}

static bool stpg_is_reset_command(uint16_t cmd) {
    return cmd == CMD_RESET;
}

static bool stpg_is_run_command(uint16_t cmd) {
    return cmd == CMD_RUN || cmd == CMD_STOP;
}

static bool stpg_is_result_command(uint16_t cmd) {
    return cmd == CMD_GET_SAMPLE_INFO || cmd == CMD_READ_SAMPLES
        || cmd == CMD_GET_FAIL_SUMMARY || cmd == CMD_READ_FAIL_LOG;
}

static bool stpg_is_psram_command(uint16_t cmd) {
    return cmd == CMD_PSRAM_CHECKSUM || cmd == CMD_PSRAM_READ || cmd == CMD_PSRAM_ERASE;
}

static bool stpg_is_flash_command(uint16_t cmd) {
    return cmd == CMD_LIST_FLASH_SLOTS || cmd == CMD_SELECT_FLASH_SLOT
        || cmd == CMD_ERASE_FLASH_SLOT || cmd == CMD_GET_FLASH_SLOT_INFO
        || cmd == CMD_SET_BOOT_SLOT;
}

static bool stpg_is_future_reserved_command(uint16_t cmd) {
    if (cmd >= 0x0034u && cmd <= 0x003fu) {
        return true;
    }
    if (cmd >= 0x0045u && cmd <= 0x004fu) {
        return true;
    }
    return cmd >= 0x0053u;
}

static void stpg_error_response(uint32_t seq, stpg_err_t error, StpgStatusResponse *rsp) {
    stpg_status_set_error(error);
    stpg_status_build(RSP_ERROR, seq, rsp);
}

static bool state_allows_upload_command(stpg_state_t state, uint16_t cmd) {
    if (cmd == CMD_UPLOAD_BEGIN) {
        return state == STATE_IDLE || state == STATE_READY || state == STATE_DONE || state == STATE_HOLD_AWAITING_NEXT;
    }
    if (cmd == CMD_UPLOAD_DATA || cmd == CMD_UPLOAD_END) {
        return state == STATE_RECEIVING;
    }
    return false;
}

/* The post-debug F-3 fault recorded NOCP at a finalizer VFP prologue.  Keep
 * this entry integer-only so it can restore CPACR access before the finalizer's
 * compiler-generated VFP prologue (for example, a vpush in the loop parser). */
#if defined(PICO_ON_DEVICE) && defined(__GNUC__)
__attribute__((noinline, target("general-regs-only")))
#endif
static void dispatch_upload(const StpgCommand *cmd, StpgStatusResponse *rsp) {
    const stpg_state_t state = stpg_status_get_state();
    if (!state_allows_upload_command(state, cmd->cmd)) {
        stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
        return;
    }

    stpg_err_t err = ERR_NONE;
    switch (cmd->cmd) {
    case CMD_UPLOAD_BEGIN:
        err = stpg_buffer_begin(cmd->arg0, cmd->arg1, cmd->arg2, cmd->arg3);
        if (err == ERR_NONE) {
            stpg_status_set_state(STATE_RECEIVING);
            stpg_status_clear_error();
            stpg_status_reset_pattern_info();
            stpg_status_reset_result_info();
            stpg_samples_reset();
            stpg_compare_reset();
            stpg_status_build(RSP_OK, cmd->seq, rsp);
        }
        break;
    case CMD_UPLOAD_DATA:
        if (cmd->arg3 != 0u) {
            err = ERR_UNSUPPORTED;
        } else {
            err = stpg_buffer_precheck_chunk(cmd->arg0, cmd->arg1);
        }
        if (err == ERR_NONE) {
            stpg_status_set_run_progress(stpg_buffer_received_size());
            stpg_status_build(RSP_READY_FOR_PAYLOAD, cmd->seq, rsp);
        }
        break;
    case CMD_UPLOAD_END:
        if (cmd->arg2 != 0u || cmd->arg3 != 0u) {
            err = ERR_UNSUPPORTED;
        } else if (!stpg_fpu_access_ensure_for_run()) {
            err = ERR_BAD_STATE;
        } else {
            err = stpg_buffer_finalize(cmd->arg0, cmd->arg1);
        }
        if (err == ERR_NONE) {
            stpg_status_set_state(stpg_runner_hold_awaiting_next() ? STATE_HOLD_AWAITING_NEXT : STATE_READY);
            stpg_status_clear_error();
            stpg_status_set_pattern_info(stpg_buffer_pattern_size(), stpg_buffer_event_count());
            stpg_status_set_run_progress(0);
            stpg_status_build(RSP_OK, cmd->seq, rsp);
        }
        break;
    default:
        err = ERR_BAD_STATE;
        break;
    }

    if (err != ERR_NONE) {
        stpg_error_response(cmd->seq, err, rsp);
    }
}

static void dispatch_reset(const StpgCommand *cmd, StpgStatusResponse *rsp) {
    if ((cmd->arg0 & ~STPG_RESET_FLAG_RELEASE_OUTPUTS) != 0u
        || cmd->arg1 != 0u || cmd->arg2 != 0u || cmd->arg3 != 0u) {
        stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
        return;
    }
    /* stop also disables PIO/DMA and releases GPIO in inactive DONE/ERROR.
     * Plain RESET intentionally keeps its existing inactive output hold. */
    if ((cmd->arg0 & STPG_RESET_FLAG_RELEASE_OUTPUTS) != 0u
        || stpg_runner_is_active() || stpg_runner_is_draining()
        || stpg_runner_hold_awaiting_next() || stpg_runner_start_pending()) {
        const stpg_err_t stop_err = stpg_runner_stop();
        if (stop_err != ERR_NONE) {
            stpg_error_response(cmd->seq, stop_err, rsp);
            return;
        }
    }
    stpg_buffer_reset();
    stpg_samples_reset();
    stpg_compare_reset();
    stpg_status_clear_error();
    stpg_status_clear_boot_error();
    stpg_status_reset_pattern_info();
    stpg_status_reset_result_info();
    stpg_status_set_state(STATE_IDLE);
    stpg_status_build(RSP_OK, cmd->seq, rsp);
}

static void dispatch_run_control(const StpgCommand *cmd, StpgStatusResponse *rsp) {
    stpg_err_t err = ERR_NONE;
    if (cmd->cmd == CMD_RUN) {
        const uint32_t measurement_flags = STPG_RUN_FLAG_MEASURE_FAMILY
            | STPG_RUN_FLAG_MEASURE_SEQUENCE
            | STPG_RUN_FLAG_MEASURE_BURST
            | STPG_RUN_FLAG_MEASURE_VISIBILITY
            | STPG_RUN_FLAG_MEASURE_HANDSHAKE;
        /* Normal, streaming, segmented, and fail-stop RUNs are acknowledged
         * after complete validation but before PSRAM reload/PIO/DMA setup.
         * Measurement commands remain synchronous because their response is
         * the completion boundary for the requested measurement. */
        const bool deferred_run = (cmd->arg0 & measurement_flags) == 0u;
        if (deferred_run) {
            err = stpg_runner_request_start(
                cmd->arg0, cmd->arg1, cmd->arg2, cmd->arg3);
        } else {
            err = stpg_runner_start(cmd->arg0, cmd->arg1, cmd->arg2, cmd->arg3);
        }
        if (err == ERR_NONE) {
            stpg_status_clear_boot_error();
            if (deferred_run || stpg_runner_is_active()
                || stpg_runner_is_draining() || stpg_runner_hold_awaiting_next()) {
                stpg_status_clear_error();
                stpg_status_set_state(STATE_RUNNING);
            }
            stpg_status_build(RSP_OK, cmd->seq, rsp);
            return;
        }
    } else if (cmd->cmd == CMD_STOP) {
        if (cmd->arg0 != 0u || cmd->arg1 != 0u || cmd->arg2 != 0u || cmd->arg3 != 0u) {
            err = ERR_UNSUPPORTED;
        } else {
            err = stpg_runner_stop();
        }
        if (err == ERR_NONE) {
            stpg_status_build(RSP_OK, cmd->seq, rsp);
            return;
        }
    } else {
        err = ERR_BAD_STATE;
    }

    stpg_error_response(cmd->seq, err, rsp);
}

static void dispatch_result_read(const StpgCommand *cmd, StpgStatusResponse *rsp) {
    if (cmd->cmd == CMD_GET_SAMPLE_INFO) {
        if (cmd->arg0 != 0u || cmd->arg1 != 0u || cmd->arg2 != 0u || cmd->arg3 != 0u) {
            stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
            return;
        }
        stpg_status_build(RSP_STATUS, cmd->seq, rsp);
        return;
    }
    if (cmd->cmd == CMD_GET_FAIL_SUMMARY) {
        if (cmd->arg0 != 0u || cmd->arg1 != 0u || cmd->arg2 != 0u || cmd->arg3 != 0u) {
            stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
            return;
        }
        stpg_status_build(RSP_STATUS, cmd->seq, rsp);
        return;
    }

    if (cmd->cmd == CMD_READ_SAMPLES) {
        if (cmd->arg2 != 0u || cmd->arg3 != 0u) {
            stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
            return;
        }
        stpg_sample_read_view_t view;
        const stpg_err_t err = stpg_samples_prepare_read(cmd->arg0, cmd->arg1, &view);
        if (err != ERR_NONE) {
            stpg_error_response(cmd->seq, err, rsp);
            return;
        }
        stpg_status_build(RSP_READY_FOR_READ, cmd->seq, rsp);
        rsp->pattern_size = view.payload_length;
        rsp->event_count = view.element_count;
        rsp->run_progress = view.payload_offset;
        rsp->result_flags = STPG_RESULT_TYPE_SAMPLE;
        rsp->error_code = ERR_NONE;
        stpg_status_refresh_crc(rsp);
        return;
    }
    if (cmd->cmd == CMD_READ_FAIL_LOG) {
        if (cmd->arg2 != 0u || cmd->arg3 != 0u) {
            stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
            return;
        }
        stpg_fail_log_read_view_t view;
        const stpg_err_t err = stpg_compare_prepare_read(cmd->arg0, cmd->arg1, &view);
        if (err != ERR_NONE) {
            stpg_error_response(cmd->seq, err, rsp);
            return;
        }
        stpg_status_build(RSP_READY_FOR_READ, cmd->seq, rsp);
        rsp->pattern_size = view.payload_length_bytes;
        rsp->event_count = view.element_count;
        rsp->run_progress = view.payload_offset_entries;
        rsp->result_flags = STPG_RESULT_TYPE_FAIL_LOG;
        rsp->error_code = ERR_NONE;
        stpg_status_refresh_crc(rsp);
        return;
    }

    stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
}

static void dispatch_psram_command(const StpgCommand *cmd, StpgStatusResponse *rsp) {
    stpg_err_t err = ERR_NONE;

    if (cmd->cmd == CMD_PSRAM_CHECKSUM) {
        if (cmd->arg2 != 0u || cmd->arg3 != 0u) {
            stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
            return;
        }
        uint32_t crc32 = 0u;
        err = stpg_buffer_psram_checksum(cmd->arg0, cmd->arg1, &crc32);
        if (err != ERR_NONE) {
            stpg_error_response(cmd->seq, err, rsp);
            return;
        }
        stpg_status_build(RSP_OK, cmd->seq, rsp);
        rsp->pattern_size = cmd->arg1;
        rsp->run_progress = cmd->arg0;
        rsp->result_flags = crc32;
        rsp->error_code = ERR_NONE;
        stpg_status_refresh_crc(rsp);
        return;
    }

    if (cmd->cmd == CMD_PSRAM_READ) {
        if (cmd->arg2 != 0u || cmd->arg3 != 0u) {
            stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
            return;
        }
        uint32_t crc32 = 0u;
        err = stpg_buffer_psram_read_prepare(cmd->arg0, cmd->arg1, &crc32);
        if (err != ERR_NONE) {
            stpg_error_response(cmd->seq, err, rsp);
            return;
        }
        stpg_status_build(RSP_READY_FOR_READ, cmd->seq, rsp);
        rsp->pattern_size = cmd->arg1;
        rsp->run_progress = cmd->arg0;
        rsp->event_count = crc32;
        rsp->result_flags = STPG_RESULT_TYPE_SAMPLE;
        rsp->error_code = ERR_NONE;
        stpg_status_refresh_crc(rsp);
        return;
    }

    if (cmd->cmd == CMD_PSRAM_ERASE) {
        err = stpg_buffer_psram_erase(cmd->arg0, cmd->arg1);
        if (err != ERR_NONE) {
            stpg_error_response(cmd->seq, err, rsp);
            return;
        }
        stpg_status_build(RSP_OK, cmd->seq, rsp);
        return;
    }

    stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
}

static void dispatch_flash_command(const StpgCommand *cmd, StpgStatusResponse *rsp) {
    stpg_err_t err = ERR_NONE;

    if (cmd->cmd == CMD_LIST_FLASH_SLOTS) {
        if (cmd->arg0 != 0u || cmd->arg1 != 0u || cmd->arg2 != 0u || cmd->arg3 != 0u) {
            stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
            return;
        }
        uint32_t valid_bitmap = 0u;
        uint32_t slot_count = 0u;
        uint32_t slot_size = 0u;
        err = stpg_buffer_flash_list(&valid_bitmap, &slot_count, &slot_size);
        if (err != ERR_NONE) {
            stpg_error_response(cmd->seq, err, rsp);
            return;
        }
        stpg_status_build(RSP_OK, cmd->seq, rsp);
        rsp->pattern_size = slot_count;
        rsp->event_count = slot_size;
        rsp->result_flags = valid_bitmap;
        rsp->error_code = ERR_NONE;
        stpg_status_refresh_crc(rsp);
        return;
    }

    if (cmd->cmd == CMD_GET_FLASH_SLOT_INFO) {
        if (cmd->arg1 != 0u || cmd->arg2 != 0u || cmd->arg3 != 0u) {
            stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
            return;
        }
        stpg_flash_slot_view_t view;
        err = stpg_buffer_flash_get_slot_info(cmd->arg0, &view);
        if (err != ERR_NONE) {
            stpg_error_response(cmd->seq, err, rsp);
            return;
        }
        stpg_status_build(RSP_FLASH_SLOT_INFO, cmd->seq, rsp);
        if (view.valid) {
            rsp->pattern_size = view.pattern_size;
            rsp->event_count = view.format;
            rsp->run_progress = view.active_pins;
            rsp->sample_count = view.tick_hz;
            rsp->result_flags = 1u;
        }
        rsp->error_code = ERR_NONE;
        stpg_status_refresh_crc(rsp);
        return;
    }

    if (cmd->cmd == CMD_SELECT_FLASH_SLOT) {
        if (cmd->arg2 != 0u || cmd->arg3 != 0u) {
            stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
            return;
        }
        if (stpg_runner_is_active()) {
            stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
            return;
        }
        if (cmd->arg1 == 0u) {
            err = stpg_buffer_flash_select_run(cmd->arg0);
        } else if (cmd->arg1 == 1u) {
            err = stpg_buffer_flash_select_write(cmd->arg0);
        } else {
            err = ERR_UNSUPPORTED;
        }
        if (err != ERR_NONE) {
            stpg_error_response(cmd->seq, err, rsp);
            return;
        }
        if (cmd->arg1 == 0u) {
            stpg_status_set_state(STATE_READY);
            stpg_status_clear_error();
            stpg_status_reset_result_info();
            stpg_status_set_pattern_info(stpg_buffer_pattern_size(), stpg_buffer_event_count());
            stpg_status_set_run_progress(0u);
        }
        stpg_status_build(RSP_OK, cmd->seq, rsp);
        return;
    }

    if (cmd->cmd == CMD_ERASE_FLASH_SLOT) {
        if (cmd->arg1 != 0u || cmd->arg2 != 0u || cmd->arg3 != 0u) {
            stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
            return;
        }
        if (stpg_runner_is_active()) {
            stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
            return;
        }
        err = stpg_buffer_flash_erase_slot(cmd->arg0);
        if (err != ERR_NONE) {
            stpg_error_response(cmd->seq, err, rsp);
            return;
        }
        stpg_status_build(RSP_OK, cmd->seq, rsp);
        return;
    }

    if (cmd->cmd == CMD_SET_BOOT_SLOT) {
        if (cmd->arg3 != 0u) {
            stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
            return;
        }
        if (cmd->arg0 == STPG_BOOTCFG_READ_CURRENT) {
            if (cmd->arg1 != 0u || cmd->arg2 != 0u) {
                stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
                return;
            }
            stpg_flash_boot_config_info_t info;
            err = stpg_flash_get_boot_config(&info);
            if (err != ERR_NONE) {
                stpg_error_response(cmd->seq, err, rsp);
                return;
            }
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            if (info.valid) {
                rsp->pattern_size = info.boot_slot_id;
                rsp->event_count = info.boot_flags;
                rsp->sample_count = info.version;
                rsp->result_flags = 1u;
            }
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            return;
        }

        if (stpg_runner_is_active()) {
            stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
            return;
        }
        if (cmd->arg0 == STPG_BOOTCFG_SET_SLOT) {
            err = stpg_flash_set_boot_slot(cmd->arg1, cmd->arg2);
        } else if (cmd->arg0 == STPG_BOOTCFG_DISABLE) {
            if (cmd->arg1 != 0u || cmd->arg2 != 0u) {
                stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
                return;
            }
            err = stpg_flash_disable_boot_slot();
        } else {
            err = ERR_UNSUPPORTED;
        }
        if (err != ERR_NONE) {
            stpg_error_response(cmd->seq, err, rsp);
            return;
        }
        stpg_status_build(RSP_OK, cmd->seq, rsp);
        return;
    }

    stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
}

void stpg_dispatch_command(const StpgCommand *cmd, StpgStatusResponse *rsp) {
    const stpg_state_t state = stpg_status_get_state();

    if (cmd->version != STPG_PROTO_VERSION) {
        stpg_error_response(cmd->seq, ERR_BAD_VERSION, rsp);
        return;
    }

    if (stpg_is_future_reserved_command(cmd->cmd)) {
        stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
        return;
    }

    if (stpg_is_upload_command(cmd->cmd)) {
        dispatch_upload(cmd, rsp);
        return;
    }

    if (stpg_is_reset_command(cmd->cmd)) {
        if ((stpg_allowed_cmds_for_state(state) & ALLOW_RESET) == 0u) {
            stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
            return;
        }
        dispatch_reset(cmd, rsp);
        return;
    }

    if (stpg_is_run_command(cmd->cmd)) {
        const uint32_t allow_mask = (cmd->cmd == CMD_RUN) ? ALLOW_RUN : ALLOW_STOP;
        if ((stpg_allowed_cmds_for_state(state) & allow_mask) == 0u) {
            stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
            return;
        }
        dispatch_run_control(cmd, rsp);
        return;
    }

    if (stpg_is_result_command(cmd->cmd)) {
        if ((stpg_allowed_cmds_for_state(state) & ALLOW_RESULT_READ) == 0u) {
            stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
            return;
        }
        dispatch_result_read(cmd, rsp);
        return;
    }

    if (stpg_is_psram_command(cmd->cmd)) {
        const uint32_t allowed = stpg_allowed_cmds_for_state(state);
        if ((allowed & ALLOW_INFO) == 0u && (allowed & ALLOW_STATUS) == 0u) {
            stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
            return;
        }
        dispatch_psram_command(cmd, rsp);
        return;
    }

    if (stpg_is_flash_command(cmd->cmd)) {
        const uint32_t allowed = stpg_allowed_cmds_for_state(state);
        if ((allowed & ALLOW_INFO) == 0u && (allowed & ALLOW_STATUS) == 0u) {
            stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
            return;
        }
        dispatch_flash_command(cmd, rsp);
        return;
    }

    if (!stpg_is_info_command(cmd->cmd)) {
        stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
        return;
    }

    const uint32_t allowed = stpg_allowed_cmds_for_state(state);
    if ((allowed & ALLOW_INFO) == 0u && cmd->cmd != CMD_GET_STATUS) {
        stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
        return;
    }
    if (cmd->cmd == CMD_GET_STATUS && (allowed & ALLOW_STATUS) == 0u) {
        stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
        return;
    }

    switch (cmd->cmd) {
    case CMD_HELLO:
        stpg_status_build(RSP_OK, cmd->seq, rsp);
        break;
    case CMD_INFO:
        stpg_status_build(RSP_INFO, cmd->seq, rsp);
        break;
    case CMD_GET_STATUS:
        stpg_status_build(RSP_STATUS, cmd->seq, rsp);
        break;
    case CMD_GET_CLOCK_INFO:
        stpg_status_build_clock_info(cmd->seq, rsp);
        break;
    case CMD_GET_RATE_INFO: {
        stpg_rate_info_t rate_info;
        stpg_err_t err = stpg_buffer_get_rate_info(&rate_info);
        if (err != ERR_NONE) {
            stpg_error_response(cmd->seq, err, rsp);
            break;
        }
        stpg_status_build(RSP_INFO, cmd->seq, rsp);
        if (rate_info.loaded) {
            rsp->pattern_size = rate_info.vector_count;
            rsp->event_count = rate_info.rate_count;
            rsp->run_progress = rate_info.slot_count;
            rsp->result_flags = rate_info.active_pins;
            rsp->fail_count = rate_info.loop_block_count;
            rsp->sample_count = rate_info.expanded_vector_count_estimate;
        } else {
            rsp->pattern_size = 0u;
            rsp->event_count = 0u;
            rsp->run_progress = 0u;
            rsp->result_flags = 0u;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
        }
        rsp->error_code = ERR_NONE;
        stpg_status_refresh_crc(rsp);
        break;
    }
    case CMD_GET_STREAM_TELEMETRY: {
        if (cmd->arg1 != 0u || cmd->arg2 != 0u || cmd->arg3 != 0u) {
            stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
            break;
        }
        if (cmd->arg0 == 80u) {
            uint32_t words[6];
            stpg_runner_chained_irq_diagnostics(words);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = words[0]; rsp->event_count = words[1];
            rsp->run_progress = words[2]; rsp->result_flags = words[3];
            rsp->fail_count = words[4]; rsp->sample_count = words[5];
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
        if (cmd->arg0 == 75u) {
            uint32_t words[6];
            if (!stpg_runner_terminal_replay_diagnostics(words)) {
                stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
                break;
            }
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = words[0]; rsp->event_count = words[1];
            rsp->run_progress = words[2]; rsp->result_flags = words[3];
            rsp->fail_count = words[4]; rsp->sample_count = words[5];
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
#endif
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
        if ((cmd->arg0 >= 70u && cmd->arg0 <= 74u) || (cmd->arg0 >= 76u && cmd->arg0 <= 79u)) {
            uint32_t words[6];
            if (!(cmd->arg0 >= 77u ? stpg_flash_xip_diagnostics(cmd->arg0, words)
                  : cmd->arg0 == 76u ? stpg_usb_quantum_diagnostics(words)
                  : cmd->arg0 == 74u ? stpg_usb_service_diagnostics(words)
                                  : stpg_samples_staging_diagnostics(cmd->arg0, words))) {
                stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
                break;
            }
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = words[0]; rsp->event_count = words[1];
            rsp->run_progress = words[2]; rsp->result_flags = words[3];
            rsp->fail_count = words[4]; rsp->sample_count = words[5];
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
#endif
        if (cmd->arg0 == 0u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->refill_count;
            rsp->event_count = telemetry->refill_event_count;
            rsp->run_progress = telemetry->refill_total_us;
            rsp->result_flags = telemetry->refill_max_us;
            rsp->fail_count = telemetry->refill_last_us;
            rsp->sample_count = telemetry->refill_last_event_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 1u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->emit_call_count;
            rsp->event_count = telemetry->sample_event_count;
            rsp->run_progress = telemetry->emit_total_us;
            rsp->result_flags = telemetry->vector_apply_total_us;
            rsp->fail_count = telemetry->prefetch_total_us;
            rsp->sample_count = telemetry->compare_bookkeeping_total_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 2u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->load_vector_call_count;
            rsp->event_count = telemetry->prefetch_call_count;
            rsp->run_progress = 0u;
            rsp->result_flags = 0u;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 3u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->swap_count;
            rsp->event_count = telemetry->underflow_count;
            rsp->run_progress = telemetry->irq_swap_count;
            rsp->result_flags = telemetry->task_swap_count;
            rsp->fail_count = telemetry->slot_event_capacity;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 4u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            const uint32_t vector_prep_total_us =
                telemetry->fast_vector_read_total_us
                + telemetry->fast_cache_walk_total_us
                + telemetry->fast_runtime_access_total_us
                + telemetry->fast_capacity_check_total_us;
            const uint32_t coarse_accounted_total_us =
                vector_prep_total_us + telemetry->fast_group_work_total_us;
            const uint32_t vector_apply_total_us =
                telemetry->vector_apply_total_us <= coarse_accounted_total_us
                ? telemetry->vector_apply_total_us
                : 0u;
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->emit_total_us;
            rsp->event_count = coarse_accounted_total_us;
            rsp->run_progress = vector_apply_total_us;
            rsp->result_flags = telemetry->pack_total_us;
            rsp->fail_count = telemetry->sample_wait_total_us;
            rsp->sample_count = telemetry->compare_bookkeeping_total_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 5u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            const uint32_t fallback_vector_count =
                telemetry->load_vector_call_count > telemetry->fast_vector_count
                ? (telemetry->load_vector_call_count - telemetry->fast_vector_count)
                : telemetry->fallback_vector_count;
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->fast_vector_count;
            rsp->event_count = fallback_vector_count;
            rsp->run_progress = telemetry->load_cache_lookup_total_us;
            rsp->result_flags = telemetry->load_prefetch_wait_total_us;
            rsp->fail_count = telemetry->load_vector_copy_total_us;
            rsp->sample_count = telemetry->load_state_setup_total_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 6u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->queue_slot_count;
            rsp->event_count = telemetry->slot_event_capacity;
            rsp->run_progress = telemetry->startup_prefill_slots;
            rsp->result_flags = telemetry->startup_prefill_total_us;
            rsp->fail_count = telemetry->swap_restart_max_us;
            rsp->sample_count = telemetry->min_ready_slots_observed;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 7u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->swap_restart_total_us;
            rsp->event_count = telemetry->underflow_reason;
            rsp->run_progress = 0u;
            rsp->result_flags = 0u;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 8u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->sample_schedule_word_count;
            rsp->event_count = telemetry->sample_request_count;
            rsp->run_progress = telemetry->sample_capture_count;
            rsp->result_flags = telemetry->compare_event_count;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 9u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->core1_loop_count;
            rsp->event_count = telemetry->core1_request_count;
            rsp->run_progress = telemetry->core1_service_count;
            rsp->result_flags = telemetry->core1_last_slot_index;
            rsp->fail_count = telemetry->core1_last_fill_error;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 10u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->fast_vector_count;
            rsp->event_count = telemetry->fast_group_emit_count;
            rsp->run_progress = telemetry->fast_chunk_cache_span_count;
            rsp->result_flags = telemetry->fast_chunk_cache_span_vectors;
            rsp->fail_count = telemetry->fallback_vector_count;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 11u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->prefetch_call_count;
            rsp->event_count = telemetry->prefetch_total_us;
            rsp->run_progress = telemetry->prefetch_record_count_total;
            rsp->result_flags = telemetry->prefetch_last_record_count;
            rsp->fail_count = telemetry->prefetch_max_us;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 12u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->fast_wait_compute_total_us;
            rsp->event_count = telemetry->fast_store_pack_total_us;
            rsp->run_progress = telemetry->compare_bookkeeping_total_us;
            rsp->result_flags = telemetry->fast_compare_sample_count;
            rsp->fail_count = telemetry->vector_apply_total_us;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 13u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->fast_store_total_us;
            rsp->event_count = telemetry->fast_pack_total_us;
            rsp->run_progress = telemetry->fast_store_pack_total_us;
            rsp->result_flags = telemetry->fast_group_emit_count;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 14u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->fast_vector_read_total_us;
            rsp->event_count = telemetry->fast_cache_walk_total_us;
            rsp->run_progress = telemetry->fast_repeat_dispatch_total_us;
            rsp->result_flags = telemetry->load_vector_total_us;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 15u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->fast_runtime_access_total_us;
            rsp->event_count = telemetry->fast_capacity_check_total_us;
            rsp->run_progress = telemetry->fast_compare_write_total_us;
            rsp->result_flags = telemetry->fast_state_commit_total_us;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 16u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            const uint32_t vector_prep_total_us =
                telemetry->fast_vector_read_total_us
                + telemetry->fast_cache_walk_total_us
                + telemetry->fast_runtime_access_total_us
                + telemetry->fast_capacity_check_total_us;
            const uint32_t coarse_accounted_total_us =
                vector_prep_total_us + telemetry->fast_group_work_total_us;
            const uint32_t control_residual_total_us =
                telemetry->load_vector_total_us > coarse_accounted_total_us
                ? (telemetry->load_vector_total_us - coarse_accounted_total_us)
                : 0u;
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->load_vector_total_us;
            rsp->event_count = vector_prep_total_us;
            rsp->run_progress = telemetry->fast_group_work_total_us;
            rsp->result_flags = control_residual_total_us;
            rsp->fail_count = telemetry->fast_vector_count;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 17u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->fast_group_apply_total_us;
            rsp->event_count = telemetry->fast_group_bookkeeping_total_us;
            rsp->run_progress = telemetry->fast_group_work_total_us;
            rsp->result_flags = telemetry->fast_group_emit_count;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 18u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->fast_compare_write_total_us;
            rsp->event_count = telemetry->fast_counter_update_total_us;
            rsp->run_progress = telemetry->fast_state_commit_total_us;
            rsp->result_flags = telemetry->fast_loop_bookkeeping_total_us;
            rsp->fail_count = telemetry->fast_group_bookkeeping_total_us;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 19u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            const uint32_t branch_control_residual =
                telemetry->fast_group_bookkeeping_total_us >
                    (telemetry->fast_compare_write_total_us
                    + telemetry->fast_counter_update_total_us
                    + telemetry->fast_state_commit_total_us
                    + telemetry->fast_loop_bookkeeping_total_us
                    + telemetry->fast_store_select_total_us)
                ? (telemetry->fast_group_bookkeeping_total_us
                    - (telemetry->fast_compare_write_total_us
                       + telemetry->fast_counter_update_total_us
                       + telemetry->fast_state_commit_total_us
                       + telemetry->fast_loop_bookkeeping_total_us
                       + telemetry->fast_store_select_total_us))
                : 0u;
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->fast_store_select_total_us;
            rsp->event_count = branch_control_residual;
            rsp->run_progress = telemetry->fast_group_bookkeeping_total_us;
            rsp->result_flags = telemetry->fast_group_emit_count;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 20u) {
            if (cmd->arg1 >= 4u) {
                stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
                break;
            }
            stpg_stream_hotspot_entry_t entry;
            if (!stpg_buffer_get_stream_hotspot_chunk(cmd->arg1, &entry)) {
                stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
                break;
            }
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = entry.global_vector_start;
            rsp->event_count = entry.global_vector_end;
            rsp->run_progress = entry.emitted_event_start;
            rsp->result_flags = entry.emitted_event_end;
            rsp->fail_count = entry.load_vector_total_us;
            rsp->sample_count = entry.processed_vectors;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 21u) {
            if (cmd->arg1 >= 4u) {
                stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
                break;
            }
            stpg_stream_hotspot_entry_t entry;
            if (!stpg_buffer_get_stream_hotspot_chunk(cmd->arg1, &entry)) {
                stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
                break;
            }
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = entry.vector_prep_total_us;
            rsp->event_count = entry.group_work_total_us;
            rsp->run_progress = entry.compare_write_total_us;
            rsp->result_flags = entry.store_select_total_us;
            rsp->fail_count = entry.branch_control_total_us;
            rsp->sample_count = entry.group_emit_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 22u) {
            if (cmd->arg1 >= 4u) {
                stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
                break;
            }
            stpg_stream_hotspot_entry_t entry;
            if (!stpg_buffer_get_stream_hotspot_chunk(cmd->arg1, &entry)) {
                stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
                break;
            }
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = entry.practical_group_apply_total_us;
            rsp->event_count = entry.practical_wait_store_total_us;
            rsp->run_progress = entry.practical_compare_write_total_us;
            rsp->result_flags = entry.practical_commit_control_total_us;
            rsp->fail_count = entry.group_work_total_us;
            rsp->sample_count = entry.group_emit_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 23u) {
            stpg_stream_hotspot_meta_t meta;
            stpg_buffer_get_stream_hotspot_meta(&meta);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = meta.recorded_chunk_count;
            rsp->event_count = meta.recorded_subwindow_count;
            rsp->run_progress = meta.subwindow_width_vectors;
            rsp->result_flags = meta.current_run_chunk_count;
            rsp->fail_count = meta.hottest_chunk_rank_count;
            rsp->sample_count = meta.hottest_subwindow_rank_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 24u) {
            uint32_t loop_count = 0u;
            uint32_t request_seq = 0u;
            uint32_t service_seq = 0u;
            uint32_t service_slot = 0u;
            uint32_t fill_error = 0u;
            stpg_runner_get_stream_core1_raw(&loop_count, &request_seq, &service_seq, &service_slot, &fill_error);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = loop_count;
            rsp->event_count = request_seq;
            rsp->run_progress = service_seq;
            rsp->result_flags = service_slot;
            rsp->fail_count = fill_error;
            extern volatile uint32_t g_stream_core1_boot_stage_raw;
            rsp->sample_count = g_stream_core1_boot_stage_raw;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 25u) {
            extern volatile uint32_t g_main_init_stage_raw;
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = g_main_init_stage_raw;
            rsp->event_count = 0u;
            rsp->run_progress = 0u;
            rsp->result_flags = 0u;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 26u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->rate_vector_read_call_count;
            rsp->event_count = telemetry->rate_vector_read_byte_total;
            rsp->run_progress = telemetry->rate_vector_read_total_us;
            rsp->result_flags = telemetry->rate_vector_read_max_us;
            rsp->fail_count = telemetry->rate_vector_read_last_byte_count;
            rsp->sample_count = telemetry->rate_vector_read_last_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 27u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->prefetch_call_count;
            rsp->event_count = telemetry->prefetch_record_count_total;
            rsp->run_progress = telemetry->prefetch_byte_total;
            rsp->result_flags = telemetry->prefetch_total_us;
            rsp->fail_count = telemetry->prefetch_last_byte_count;
            rsp->sample_count = telemetry->prefetch_max_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 28u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->load_vector_call_count;
            rsp->event_count = telemetry->load_vector_copy_byte_total;
            rsp->run_progress = telemetry->load_vector_copy_total_us;
            rsp->result_flags = telemetry->load_state_setup_total_us;
            rsp->fail_count = telemetry->load_vector_total_us;
            rsp->sample_count = telemetry->fallback_vector_count + telemetry->fast_vector_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 29u) {
            stpg_stream_buffer_telemetry_t *const buffer_telemetry = &g_dispatch_buffer_telemetry;
            stpg_stream_telemetry_t *const runner_telemetry = &g_dispatch_runner_telemetry;
            stpg_buffer_get_stream_telemetry(buffer_telemetry);
            stpg_runner_get_stream_telemetry(runner_telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = runner_telemetry->sample_schedule_word_count;
            rsp->event_count = buffer_telemetry->sample_event_count;
            rsp->run_progress = runner_telemetry->sample_capture_count;
            rsp->result_flags = runner_telemetry->compare_event_count;
            rsp->fail_count = buffer_telemetry->compare_bookkeeping_total_us;
            rsp->sample_count = buffer_telemetry->emit_total_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 30u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->mismatch_stage;
            rsp->event_count = telemetry->mismatch_rate_id;
            rsp->run_progress = (telemetry->mismatch_index & 0xFFFFu)
                | ((telemetry->mismatch_period_ticks & 0xFFFFu) << 16);
            rsp->result_flags = telemetry->mismatch_desired_delta_ticks;
            rsp->fail_count = telemetry->mismatch_required_ticks;
            rsp->sample_count = (telemetry->mismatch_prev_time_ticks & 0xFFFFu)
                | ((telemetry->mismatch_time_ticks & 0xFFFFu) << 16);
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 31u) {
            uint32_t family_id = 0u;
            uint32_t period_ticks = 0u;
            uint32_t phase_ticks = 0u;
            uint32_t cycle_count = 0u;
            stpg_buffer_get_measurement_debug(&family_id, &period_ticks, &phase_ticks, &cycle_count);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = family_id;
            rsp->event_count = period_ticks;
            rsp->run_progress = phase_ticks;
            rsp->result_flags = cycle_count;
            rsp->fail_count = stpg_runner_is_active() ? 1u : 0u;
            rsp->sample_count = stpg_runner_is_draining() ? 1u : 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 32u) {
            uint32_t stage = 0u;
            uint32_t run_flags = 0u;
            uint32_t arg1 = 0u;
            uint32_t arg2 = 0u;
            uint32_t arg3 = 0u;
            uint32_t active = 0u;
            uint32_t draining = 0u;
            stpg_runner_get_start_debug(&stage, &run_flags, &arg1, &arg2, &arg3, &active, &draining);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = stage;
            rsp->event_count = run_flags;
            rsp->run_progress = arg1;
            rsp->result_flags = arg2;
            rsp->fail_count = arg3;
            rsp->sample_count = active | (draining << 1u);
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 33u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->practical_group_apply_total_us;
            rsp->event_count = telemetry->practical_wait_store_total_us;
            rsp->run_progress = telemetry->practical_compare_write_total_us;
            rsp->result_flags = telemetry->practical_commit_control_total_us;
            rsp->fail_count = telemetry->fast_group_work_total_us;
            rsp->sample_count = telemetry->fast_group_emit_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 34u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->practical_group_apply_total_us
                + telemetry->practical_wait_store_total_us
                + telemetry->practical_compare_write_total_us
                + telemetry->practical_commit_control_total_us;
            rsp->event_count = telemetry->fast_group_work_total_us;
            rsp->run_progress = telemetry->fast_group_apply_total_us;
            rsp->result_flags = telemetry->fast_group_bookkeeping_total_us;
            rsp->fail_count = telemetry->fast_compare_write_total_us;
            rsp->sample_count = telemetry->fast_store_select_total_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 35u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->last_chunk_vector_start;
            rsp->event_count = telemetry->last_chunk_vector_end;
            rsp->run_progress = telemetry->last_chunk_emitted_event_start;
            rsp->result_flags = telemetry->last_chunk_emitted_event_end;
            rsp->fail_count = telemetry->last_chunk_processed_vectors;
            rsp->sample_count = telemetry->last_chunk_group_emit_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 36u) {
            uint32_t output_dma_remaining = 0u;
            uint32_t output_dma_busy = 0u;
            uint32_t output_dma_irq_pending = 0u;
            uint32_t sample_dma_remaining = 0u;
            uint32_t sample_dma_busy = 0u;
            uint32_t pio_fdebug = 0u;
            uint32_t event_count = 0u;
            uint32_t sample_event_count = 0u;
            stpg_runner_get_direct_debug(&output_dma_remaining,
                                         &output_dma_busy,
                                         &output_dma_irq_pending,
                                         &sample_dma_remaining,
                                         &sample_dma_busy,
                                         &pio_fdebug,
                                         &event_count,
                                         &sample_event_count);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = output_dma_remaining;
            rsp->event_count = output_dma_busy;
            rsp->run_progress = output_dma_irq_pending;
            rsp->result_flags = sample_dma_remaining;
            rsp->fail_count = sample_dma_busy;
            rsp->sample_count = pio_fdebug;
            rsp->error_code = (event_count & 0xFFFFu) | ((sample_event_count & 0xFFFFu) << 16);
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 37u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->macro_record_read_call_count;
            rsp->event_count = telemetry->macro_record_read_total_us;
            rsp->run_progress = telemetry->macro_record_read_max_us;
            rsp->result_flags = telemetry->macro_record_read_last_us;
            rsp->fail_count = telemetry->macro_wait_compute_total_us;
            rsp->sample_count = telemetry->macro_build_event_total_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 38u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->macro_compare_write_total_us;
            rsp->event_count = telemetry->macro_state_commit_total_us;
            rsp->run_progress = telemetry->macro_repeat_advance_total_us;
            rsp->result_flags = telemetry->emit_total_us;
            rsp->fail_count = telemetry->compare_bookkeeping_total_us;
            rsp->sample_count = telemetry->emit_call_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 39u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->startup_prefill_attempted_slots;
            rsp->event_count = telemetry->startup_prefill_last_slot_index;
            rsp->run_progress = telemetry->startup_prefill_break_reason;
            rsp->result_flags = telemetry->startup_prefill_wait_error;
            rsp->fail_count = telemetry->startup_prefill_last_event_count;
            rsp->sample_count = telemetry->startup_prefill_last_word_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 40u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->startup_timeout_core1_loop_count;
            rsp->event_count = telemetry->startup_timeout_core1_service_count;
            rsp->run_progress = telemetry->startup_timeout_stop_requested;
            rsp->result_flags = telemetry->startup_timeout_fill_request_slot;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 41u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->startup_timeout_core1_boot_stage_raw;
            rsp->event_count = telemetry->startup_timeout_core1_request_raw;
            rsp->run_progress = telemetry->startup_timeout_core1_service_raw;
            rsp->result_flags = telemetry->startup_timeout_core1_fill_error_raw;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 42u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->startup_timeout_shared_fill_request_slot;
            rsp->event_count = telemetry->startup_timeout_shared_slot0_ready;
            rsp->run_progress = telemetry->startup_timeout_shared_fill_error;
            rsp->result_flags = telemetry->startup_timeout_shared_slot0_event_count;
            rsp->fail_count = telemetry->startup_timeout_shared_slot0_word_count;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 43u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->fail_snapshot_status_state;
            rsp->event_count = telemetry->fail_snapshot_runner_flags;
            rsp->run_progress = telemetry->fail_snapshot_shared_fill_request_slot;
            rsp->result_flags = telemetry->fail_snapshot_shared_fill_error;
            rsp->fail_count = telemetry->fail_snapshot_from_irq;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 44u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->dma_abort_output_us;
            rsp->event_count = telemetry->dma_abort_sample_us;
            rsp->run_progress = telemetry->dma_abort_sample_sched_us;
            rsp->result_flags = telemetry->dma_abort_total_us;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 45u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->startup_first_fill_request_us;
            rsp->event_count = telemetry->startup_first_service_complete_us;
            rsp->run_progress = telemetry->startup_slot0_ready_us;
            rsp->result_flags = 0u;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 52u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->startup_timeout_core1_last_polled_slot_raw;
            rsp->event_count = telemetry->startup_timeout_core1_negative_poll_count_raw;
            rsp->run_progress = telemetry->startup_timeout_core1_valid_poll_count_raw;
            rsp->result_flags = telemetry->startup_timeout_core1_invalid_poll_count_raw;
            rsp->fail_count = telemetry->startup_timeout_core1_request_seen_raw;
            rsp->sample_count = telemetry->startup_timeout_core1_claim_raw;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 53u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->startup_timeout_core1_last_polled_fill_error_raw;
            rsp->event_count = telemetry->startup_timeout_core1_last_polled_slot0_ready_raw;
            rsp->run_progress = telemetry->startup_timeout_shared_fill_error;
            rsp->result_flags = telemetry->startup_timeout_shared_slot0_ready;
            rsp->fail_count = telemetry->startup_timeout_fill_request_slot;
            rsp->sample_count = telemetry->startup_timeout_core1_last_polled_slot_raw;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 54u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->startup_timeout_request_set_us;
            rsp->event_count = telemetry->startup_timeout_request_clear_us;
            rsp->run_progress = telemetry->startup_timeout_core1_first_poll_us;
            rsp->result_flags = telemetry->startup_timeout_core1_first_negative_poll_us;
            rsp->fail_count = telemetry->startup_timeout_core1_first_valid_poll_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 55u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->startup_timeout_core1_last_stage_raw;
            rsp->event_count = telemetry->startup_timeout_core1_after_stop_gate_us;
            rsp->run_progress = telemetry->startup_timeout_core1_after_dmb_us;
            rsp->result_flags = telemetry->startup_timeout_core1_after_slot_read_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 56u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->visibility_probe_request_set_us;
            rsp->event_count = telemetry->visibility_probe_core1_saw_request_us;
            rsp->run_progress = telemetry->visibility_probe_core1_write1_us;
            rsp->result_flags = telemetry->visibility_probe_core1_write2_us;
            rsp->fail_count = telemetry->visibility_probe_core1_done_us;
            rsp->sample_count = telemetry->visibility_probe_timeout_hit;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 57u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->visibility_probe_core0_first_stage_seen_us;
            rsp->event_count = telemetry->visibility_probe_core0_first_value1_seen_us;
            rsp->run_progress = telemetry->visibility_probe_core0_first_value2_seen_us;
            rsp->result_flags = telemetry->visibility_probe_core0_first_done_seen_us;
            rsp->fail_count = telemetry->visibility_probe_final_stage;
            rsp->sample_count = telemetry->visibility_probe_final_done;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 58u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->visibility_probe_final_value1;
            rsp->event_count = telemetry->visibility_probe_final_value2;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 59u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->handshake_probe_request_set_us;
            rsp->event_count = telemetry->handshake_probe_core1_saw_request_us;
            rsp->run_progress = telemetry->handshake_probe_core1_claim_us;
            rsp->result_flags = telemetry->handshake_probe_core1_ready_us;
            rsp->fail_count = telemetry->handshake_probe_timeout_hit;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 60u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->handshake_probe_core0_ready_seen_us;
            rsp->event_count = telemetry->handshake_probe_core0_error_seen_us;
            rsp->run_progress = telemetry->handshake_probe_final_request_slot;
            rsp->result_flags = telemetry->handshake_probe_final_ready;
            rsp->fail_count = telemetry->handshake_probe_final_error;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 61u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            const uint32_t fallback_prep_total_us =
                telemetry->load_cache_lookup_total_us
                + telemetry->load_prefetch_wait_total_us
                + telemetry->load_vector_copy_total_us
                + telemetry->load_state_setup_total_us;
            const uint32_t fast_prep_total_us =
                telemetry->fast_vector_read_total_us
                + telemetry->fast_cache_walk_total_us
                + telemetry->fast_runtime_access_total_us
                + telemetry->fast_capacity_check_total_us;
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->load_cache_miss_count;
            rsp->event_count = telemetry->load_prefetch_wait_count;
            rsp->run_progress = fallback_prep_total_us;
            rsp->result_flags = fast_prep_total_us;
            rsp->fail_count = telemetry->load_vector_call_count;
            rsp->sample_count = telemetry->fast_vector_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 62u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->underflow_snapshot_active_slot;
            rsp->event_count = telemetry->underflow_snapshot_next_requested_slot;
            rsp->run_progress = telemetry->underflow_snapshot_next_ready_slot;
            rsp->result_flags = telemetry->underflow_snapshot_ready_mask;
            rsp->fail_count = telemetry->underflow_snapshot_request_seq;
            rsp->sample_count = telemetry->underflow_snapshot_service_seq;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 63u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->underflow_snapshot_last_service_slot;
            rsp->event_count = telemetry->underflow_snapshot_last_service_event_count;
            rsp->run_progress = telemetry->underflow_snapshot_last_service_word_count;
            rsp->result_flags = telemetry->underflow_snapshot_last_service_sample_count;
            rsp->fail_count = telemetry->underflow_snapshot_queued_event_count;
            rsp->sample_count = telemetry->underflow_snapshot_reason;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 123u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->prod_noready_defer_count;
            rsp->event_count = telemetry->prod_noready_recheck_success_count;
            rsp->run_progress = telemetry->prod_noready_recheck_fail_count;
            rsp->result_flags = telemetry->prod_noready_safety_reject_count;
            rsp->fail_count = telemetry->prod_noready_last_iterations;
            rsp->sample_count = telemetry->prod_noready_last_wait_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 124u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->prod_noready_defer_dma_busy;
            rsp->event_count = telemetry->prod_noready_defer_dma_remaining;
            rsp->run_progress = telemetry->prod_noready_defer_pio_txempty;
            rsp->result_flags = telemetry->prod_noready_defer_completed_slot;
            rsp->fail_count = telemetry->prod_noready_defer_queued_events;
            rsp->sample_count = telemetry->prod_noready_defer_safety_flags;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 125u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->prod_noready_wait_step_us;
            rsp->event_count = telemetry->prod_noready_max_iterations;
            rsp->run_progress = telemetry->prod_noready_last_iterations;
            rsp->result_flags = telemetry->prod_noready_last_wait_us;
            rsp->fail_count = telemetry->prod_noready_recheck_success_count;
            rsp->sample_count = telemetry->prod_noready_recheck_fail_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 64u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->mismatch_detail_flags;
            rsp->event_count = telemetry->mismatch_group_count;
            rsp->run_progress = telemetry->mismatch_gap_from_cycle_start_ticks;
            rsp->result_flags = telemetry->mismatch_gap_from_prev_ticks;
            rsp->fail_count = 0u;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 191u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->p4_diag_marker_slot;
            rsp->event_count = telemetry->p4_diag_marker_vector_index;
            rsp->run_progress = telemetry->p4_diag_observation_count;
            rsp->result_flags = telemetry->p4_diag_active_dma_remaining;
            rsp->fail_count = telemetry->p4_diag_active_dma_busy;
            rsp->sample_count = telemetry->p4_diag_promotable_slot;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 192u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->p4_diag_promotable_count;
            rsp->event_count = telemetry->p4_diag_spare_state;
            rsp->run_progress = telemetry->p4_diag_ready_before_end;
            rsp->sample_count = telemetry->p4_diag_first_promotable_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 189u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->p3_stream_run_sequence;
            rsp->event_count = telemetry->p3_telemetry_sequence;
            rsp->run_progress = telemetry->p3_chain_arm_count;
            rsp->result_flags = telemetry->p3_chain_completion_count;
            rsp->fail_count = telemetry->p3_no_ready_fail_count;
            rsp->sample_count = telemetry->p3_max_irq_to_arm_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 190u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->p3_terminal_chain_neutralize_count;
            rsp->event_count = telemetry->p3_no_ready_chain_neutralize_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 193u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->p3_handoff_owner;
            rsp->event_count = telemetry->p3_handoff_conflict_count;
            rsp->run_progress = telemetry->p3_irq_handoff_count;
            rsp->result_flags = telemetry->p3_main_loop_handoff_count;
            rsp->fail_count = telemetry->p3_main_loop_handoff_last_us;
            rsp->sample_count = telemetry->p3_main_loop_handoff_max_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 194u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->task_swap_count;
            rsp->event_count = telemetry->irq_swap_count;
            rsp->run_progress = telemetry->prod_noready_defer_safety_flags;
            rsp->result_flags = telemetry->prod_noready_defer_pio_txempty;
            rsp->fail_count = telemetry->prod_noready_defer_dma_busy;
            rsp->sample_count = telemetry->prod_noready_defer_dma_remaining;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 195u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->dma_pio_boundary_count;
            rsp->event_count = telemetry->dma_pio_last_entry_channel;
            rsp->run_progress = telemetry->dma_pio_last_entry_irq_pending;
            rsp->result_flags = telemetry->dma_pio_last_entry_busy;
            rsp->fail_count = telemetry->dma_pio_last_entry_remaining;
            rsp->sample_count = telemetry->dma_pio_last_entry_fstat;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 196u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->dma_pio_last_entry_fdebug;
            rsp->event_count = telemetry->dma_pio_last_entry_flevel;
            rsp->run_progress = telemetry->dma_pio_last_exit_channel;
            rsp->result_flags = telemetry->dma_pio_last_exit_busy;
            rsp->fail_count = telemetry->dma_pio_last_exit_remaining;
            rsp->sample_count = telemetry->dma_pio_last_handler_us;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 197u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->boundary_observation_count;
            rsp->event_count = telemetry->last_boundary_vector_index;
            rsp->run_progress = telemetry->last_boundary_rate_id;
            rsp->result_flags = (telemetry->last_boundary_repeat_index & 0xFFFFu)
                | ((telemetry->last_boundary_repeat_count & 0xFFFFu) << 16u);
            rsp->fail_count = telemetry->last_boundary_kind;
            rsp->sample_count = telemetry->last_boundary_output_changed;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 198u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->last_boundary_period_ticks;
            rsp->event_count = telemetry->last_boundary_prev_period_ticks;
            rsp->run_progress = telemetry->last_boundary_prev_time_ticks;
            rsp->result_flags = telemetry->last_boundary_group_time_ticks;
            rsp->fail_count = telemetry->last_boundary_desired_delta_ticks;
            rsp->sample_count = telemetry->last_boundary_rate_id;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 199u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->dma_pio_last_exit_fstat;
            rsp->event_count = telemetry->dma_pio_last_exit_fdebug;
            rsp->run_progress = telemetry->dma_pio_last_exit_flevel;
            rsp->result_flags = telemetry->dma_pio_boundary_count;
            rsp->fail_count = telemetry->dma_pio_last_entry_fstat;
            rsp->sample_count = telemetry->dma_pio_last_entry_fdebug;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 200u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->dma_completion_trace_count;
            rsp->event_count = telemetry->dma_completion_trace_overflow;
            rsp->run_progress = telemetry->dma_completion_trace_boundary_count;
            rsp->result_flags = telemetry->dma_completion_trace_last_path_flags;
            rsp->fail_count = telemetry->dma_completion_trace_max_restart_us;
            rsp->sample_count = telemetry->dma_pio_boundary_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 >= 201u && cmd->arg0 <= 208u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            const uint32_t index = cmd->arg0 - 201u;
            const stpg_dma_completion_trace_t *entry =
                &telemetry->dma_completion_trace[index];
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = entry->completion_index;
            rsp->event_count = entry->chunk_index;
            rsp->run_progress = entry->event_start;
            rsp->result_flags = entry->event_end;
            rsp->fail_count = entry->boundary_event;
            rsp->sample_count = entry->path_flags;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 >= 209u && cmd->arg0 <= 216u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            const uint32_t index = cmd->arg0 - 209u;
            const stpg_dma_completion_trace_t *entry =
                &telemetry->dma_completion_trace[index];
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = (entry->completed_slot & 0xFFu)
                | ((entry->armed_slot & 0xFFu) << 8u)
                | ((entry->next_ready_slot & 0xFFu) << 16u);
            rsp->event_count = entry->restart_us;
            rsp->run_progress = entry->handler_us;
            rsp->result_flags = entry->dma_remaining;
            rsp->fail_count = entry->pio_flevel;
            rsp->sample_count = entry->pio_fstat;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 217u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->p3_main_loop_rearm_count;
            rsp->event_count = telemetry->p3_main_loop_rearm_last_us;
            rsp->run_progress = telemetry->p3_main_loop_idle_fallback_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
        if (cmd->arg0 == 218u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->refill_trace_count;
            rsp->event_count = telemetry->refill_trace_overflow;
            rsp->run_progress = STPG_REFILL_TRACE_CAPACITY;
            rsp->result_flags = telemetry->core1_request_count;
            rsp->fail_count = telemetry->core1_service_count;
            rsp->sample_count = telemetry->refill_count;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 >= 219u
            && cmd->arg0 < (219u + (2u * STPG_REFILL_TRACE_CAPACITY))) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            const uint32_t page_offset = cmd->arg0 - 219u;
            const uint32_t index = page_offset / 2u;
            const stpg_refill_trace_t *entry = &telemetry->refill_trace[index];
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            if ((page_offset & 1u) == 0u) {
                rsp->pattern_size = entry->sequence;
                rsp->event_count = entry->slot_index;
                rsp->run_progress = entry->event_start;
                rsp->result_flags = entry->event_end;
                rsp->fail_count = entry->request_us;
                rsp->sample_count = entry->claim_us;
            } else {
                rsp->pattern_size = entry->body_start_us;
                rsp->event_count = entry->body_end_us;
                rsp->run_progress = entry->ready_us;
                rsp->result_flags = entry->promote_us;
                rsp->fail_count = entry->arm_us;
                rsp->sample_count = entry->fill_error;
            }
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
#endif
        if (cmd->arg0 == 46u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->recovery_live_main_loop_count;
            rsp->event_count = telemetry->recovery_live_main_loop_stage;
            rsp->run_progress = telemetry->recovery_live_usb_task_count;
            rsp->result_flags = telemetry->recovery_live_tud_task_count;
            rsp->fail_count = telemetry->recovery_fail_stage;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 47u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->recovery_fail_main_loop_count;
            rsp->event_count = telemetry->recovery_fail_usb_task_count;
            rsp->run_progress = telemetry->recovery_fail_tud_task_count;
            rsp->result_flags = telemetry->recovery_fail_dma_irq0_pending;
            rsp->fail_count = telemetry->recovery_fail_dma_irq0_enabled;
            rsp->sample_count = 0u;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 65u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->rate_transition_count;
            rsp->event_count = telemetry->last_rate_transition_vector_index;
            rsp->run_progress = (telemetry->last_rate_transition_from_rate_id & 0xFFFFu)
                | ((telemetry->last_rate_transition_to_rate_id & 0xFFFFu) << 16u);
            rsp->result_flags = (telemetry->last_rate_transition_from_period_ticks & 0xFFFFu)
                | ((telemetry->last_rate_transition_to_period_ticks & 0xFFFFu) << 16u);
            rsp->fail_count = telemetry->last_rate_transition_repeat_index;
            rsp->sample_count = telemetry->last_rate_transition_desired_delta_ticks;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 66u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->vector_observation_count;
            rsp->event_count = telemetry->last_vector_index;
            rsp->run_progress = telemetry->last_vector_rate_id;
            rsp->result_flags = (telemetry->last_vector_repeat_index & 0xFFFFu)
                | ((telemetry->last_vector_repeat_count & 0xFFFFu) << 16u);
            rsp->fail_count = telemetry->last_vector_period_ticks;
            rsp->sample_count = telemetry->last_vector_desired_delta_ticks;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 67u) {
            stpg_stream_buffer_telemetry_t *const telemetry = &g_dispatch_buffer_telemetry;
            stpg_buffer_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->repeat_gt1_count;
            rsp->event_count = telemetry->last_vector_prev_period_ticks;
            rsp->run_progress = telemetry->last_vector_output_changed;
            rsp->result_flags = telemetry->last_rate_transition_prev_time_ticks;
            rsp->fail_count = telemetry->last_rate_transition_output_changed;
            rsp->sample_count = telemetry->last_vector_rate_id;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 68u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->underflow_snapshot_vector_index;
            rsp->event_count = telemetry->underflow_snapshot_repeat_index;
            rsp->run_progress = telemetry->underflow_snapshot_rate_id;
            rsp->result_flags = telemetry->underflow_snapshot_period_ticks;
            rsp->fail_count = telemetry->underflow_snapshot_prev_period_ticks;
            rsp->sample_count = telemetry->underflow_snapshot_desired_delta_ticks;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        if (cmd->arg0 == 69u) {
            stpg_stream_telemetry_t *const telemetry = &g_dispatch_runner_telemetry;
            stpg_runner_get_stream_telemetry(telemetry);
            stpg_status_build(RSP_INFO, cmd->seq, rsp);
            rsp->pattern_size = telemetry->underflow_snapshot_vector_repeat;
            rsp->event_count = telemetry->underflow_snapshot_vector_flags;
            rsp->run_progress = telemetry->underflow_snapshot_current_value;
            rsp->result_flags = telemetry->underflow_snapshot_current_dir;
            rsp->fail_count = telemetry->underflow_snapshot_state_flags;
            rsp->sample_count = telemetry->underflow_snapshot_reason;
            rsp->error_code = ERR_NONE;
            stpg_status_refresh_crc(rsp);
            break;
        }
        stpg_error_response(cmd->seq, ERR_UNSUPPORTED, rsp);
        break;
    }
    default:
        stpg_error_response(cmd->seq, ERR_BAD_STATE, rsp);
        break;
    }
}
