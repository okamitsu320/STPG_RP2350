#include "stpg_usb_protocol.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "bsp/board_api.h"
#include "pico/time.h"
#include "stpg_compare.h"
#include "stpg_command_dispatch.h"
#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_error.h"
#include "stpg_pattern_buffer.h"
#include "stpg_psram.h"
#include "stpg_protocol.h"
#include "stpg_runner.h"
#include "stpg_samples.h"
#include "stpg_status.h"
#include "tusb.h"

typedef enum {
    RX_WAIT_MAGIC = 0,
    RX_FILL_HEADER,
    RX_FILL_PAYLOAD,
} stpg_rx_state_t;

static stpg_rx_state_t g_rx_state;
static uint8_t g_cmd_buf[STPG_CMD_SIZE];
static uint8_t g_payload_buf[STPG_UPLOAD_CHUNK_SIZE];
static uint32_t g_magic_shift;
static uint32_t g_rx_pos;
static uint32_t g_pending_seq;
static uint32_t g_pending_offset;
static uint32_t g_pending_size;
static uint32_t g_pending_crc;
volatile uint32_t g_usb_protocol_task_call_count_raw;
volatile uint32_t g_usb_tud_task_call_count_raw;
volatile uint32_t g_usb_write_spin_count_raw;
volatile uint32_t g_usb_available_poll_count_raw;
volatile uint32_t g_usb_rx_read_call_count_raw;
volatile uint32_t g_usb_rx_byte_count_raw;
volatile uint32_t g_usb_frame_complete_count_raw;
volatile uint32_t g_usb_dispatch_call_count_raw;
volatile uint32_t g_usb_write_response_count_raw;
volatile uint32_t g_usb_write_last_success_bytes_raw;
volatile uint32_t g_usb_last_payload_error_code_raw;
static volatile bool g_run_response_pending;
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
static struct {
    uint32_t calls, total_us, max_us, tx_yields, ready_yields, rx_reads;
} g_usb_service_diag;
static struct {
    uint32_t calls, total_us, max_us, ready_at_entry, ready_at_exit, last_us;
} g_usb_quantum_diag;
#endif

/* Main-loop/USB waits only, not a TinyUSB callback or IRQ entry. */
static void stpg_usb_service_samples(void) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    (void)stpg_runner_service_samples();
#endif
}

/* Never invoke this wrapper from a TinyUSB callback or an IRQ. Sample service
 * happens after the single event has returned, without full runner reentry. */
static void stpg_usb_task_quantum(void) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    const bool track = stpg_runner_is_active();
    const uint32_t started_us = track ? time_us_32() : 0u;
    if (track && tud_task_event_ready()) ++g_usb_quantum_diag.ready_at_entry;
#endif
    tud_task_ext(0u, false);
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    if (track) {
        const uint32_t elapsed = time_us_32() - started_us;
        ++g_usb_quantum_diag.calls;
        g_usb_quantum_diag.total_us += elapsed;
        if (elapsed > g_usb_quantum_diag.max_us) g_usb_quantum_diag.max_us = elapsed;
        g_usb_quantum_diag.last_us = elapsed;
        if (tud_task_event_ready()) ++g_usb_quantum_diag.ready_at_exit;
    }
#endif
    stpg_usb_service_samples();
}

bool stpg_usb_quantum_diagnostics(uint32_t words[6]) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    if (words == 0) return false;
    words[0] = g_usb_quantum_diag.calls; words[1] = g_usb_quantum_diag.total_us;
    words[2] = g_usb_quantum_diag.max_us; words[3] = g_usb_quantum_diag.ready_at_entry;
    words[4] = g_usb_quantum_diag.ready_at_exit; words[5] = g_usb_quantum_diag.last_us;
    return true;
#else
    (void)words;
    return false;
#endif
}

bool stpg_usb_service_diagnostics(uint32_t words[6]) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    if (words == 0) return false;
    words[0] = g_usb_service_diag.calls; words[1] = g_usb_service_diag.total_us;
    words[2] = g_usb_service_diag.max_us; words[3] = g_usb_service_diag.tx_yields;
    words[4] = g_usb_service_diag.ready_yields; words[5] = g_usb_service_diag.rx_reads;
    return true;
#else
    (void)words;
    return false;
#endif
}

static void stpg_usb_send_error(uint32_t seq, stpg_err_t error);
static void stpg_usb_send_payload_error(uint32_t seq, stpg_err_t error);
static void stpg_usb_write_bytes(const uint8_t *buf, uint32_t len);

static void stpg_usb_write_response(const StpgStatusResponse *rsp) {
    g_usb_write_response_count_raw += 1u;
    stpg_usb_write_bytes((const uint8_t *)rsp, STPG_RSP_SIZE);
}

void tud_cdc_tx_complete_cb(uint8_t itf) {
    if (itf == 0u) {
        g_run_response_pending = false;
    }
}

void stpg_usb_run_response_begin(void) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    memset(&g_usb_service_diag, 0, sizeof(g_usb_service_diag));
    memset(&g_usb_quantum_diag, 0, sizeof(g_usb_quantum_diag));
#endif
    g_run_response_pending = true;
}

bool stpg_usb_run_response_pending(void) {
    return g_run_response_pending;
}

void stpg_usb_run_response_cancel(void) {
    g_run_response_pending = false;
}

// DTR is a host-side CDC line-state convention, not the readiness condition
// for bulk data endpoints.  Some Linux hosts can enumerate the interface and
// deliver a command while SET_CONTROL_LINE_STATE is delayed or rejected.
// Gating protocol responses on tud_cdc_connected() would then discard a valid
// command response even though the configured bulk endpoints are usable.
#define STPG_USB_CDC_READY_WAIT_US 100000u

static bool stpg_usb_wait_for_cdc_ready(void) {
    if (tud_cdc_n_ready(0)) {
        return true;
    }

    const uint32_t started_us = time_us_32();
    while ((uint32_t)(time_us_32() - started_us) < STPG_USB_CDC_READY_WAIT_US) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
        if (stpg_runner_is_active()) ++g_usb_service_diag.ready_yields;
#endif
        stpg_usb_service_samples();
        stpg_usb_task_quantum();
        if (tud_cdc_n_ready(0)) {
            return true;
        }
    }
    return false;
}

static inline void stpg_usb_note_tud_task_call(void) {
    g_usb_tud_task_call_count_raw += 1u;
}

static void stpg_usb_write_bytes(const uint8_t *buf, uint32_t len) {
    if (len == 0u || !stpg_usb_wait_for_cdc_ready()) {
        return;
    }
    uint32_t total_written = 0u;
    uint32_t offset = 0u;
    while (offset < len) {
        const uint32_t written = tud_cdc_write(buf + offset, len - offset);
        if (written == 0u) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
            if (stpg_runner_is_active()) ++g_usb_service_diag.tx_yields;
#endif
            stpg_usb_service_samples();
            g_usb_write_spin_count_raw += 1u;
            stpg_usb_note_tud_task_call();
            stpg_usb_task_quantum();
            tud_cdc_write_flush();
            continue;
        }
        offset += written;
        total_written += written;
        g_usb_write_last_success_bytes_raw = total_written;
        tud_cdc_write_flush();
    }
}

static void stpg_usb_send_read_payload(uint32_t seq, uint32_t offset, uint32_t length) {
    stpg_sample_read_view_t view;
    const stpg_err_t err = stpg_samples_prepare_read(offset, length, &view);
    if (err != ERR_NONE) {
        stpg_usb_send_error(seq, err);
        return;
    }

    uint8_t scratch[64];
    uint32_t cursor = 0u;
    while (cursor < view.payload_length) {
        const uint32_t chunk = (view.payload_length - cursor) > sizeof(scratch)
                                   ? (uint32_t)sizeof(scratch)
                                   : (view.payload_length - cursor);
        stpg_samples_read_bytes(view.payload_offset + cursor, scratch, chunk);
        stpg_usb_write_bytes(scratch, chunk);
        cursor += chunk;
    }

    StpgStatusResponse rsp;
    stpg_status_build(RSP_DATA_END, seq, &rsp);
    rsp.pattern_size = view.payload_length;
    rsp.event_count = view.payload_crc32;
    rsp.run_progress = view.payload_offset;
    rsp.result_flags = STPG_RESULT_TYPE_SAMPLE;
    rsp.error_code = ERR_NONE;
    stpg_status_refresh_crc(&rsp);
    stpg_usb_write_response(&rsp);
}

static void stpg_usb_send_fail_log_payload(uint32_t seq, uint32_t offset_entries, uint32_t count) {
    stpg_fail_log_read_view_t view;
    const stpg_err_t err = stpg_compare_prepare_read(offset_entries, count, &view);
    if (err != ERR_NONE) {
        stpg_usb_send_error(seq, err);
        return;
    }

    uint8_t scratch[64];
    uint32_t cursor = 0u;
    while (cursor < view.payload_length_bytes) {
        const uint32_t chunk = (view.payload_length_bytes - cursor) > sizeof(scratch)
                                   ? (uint32_t)sizeof(scratch)
                                   : (view.payload_length_bytes - cursor);
        stpg_compare_read_bytes(cursor + view.payload_offset_entries * (uint32_t)sizeof(FailLogEntry), scratch, chunk);
        stpg_usb_write_bytes(scratch, chunk);
        cursor += chunk;
    }

    StpgStatusResponse rsp;
    stpg_status_build(RSP_DATA_END, seq, &rsp);
    rsp.pattern_size = view.payload_length_bytes;
    rsp.event_count = view.payload_crc32;
    rsp.run_progress = view.payload_offset_entries;
    rsp.result_flags = STPG_RESULT_TYPE_FAIL_LOG;
    rsp.error_code = ERR_NONE;
    stpg_status_refresh_crc(&rsp);
    stpg_usb_write_response(&rsp);
}

static void stpg_usb_send_psram_payload(uint32_t seq, uint32_t offset, uint32_t length) {
    uint8_t scratch[64];
    uint32_t cursor = 0u;
    while (cursor < length) {
        const uint32_t chunk = (length - cursor) > sizeof(scratch)
                                   ? (uint32_t)sizeof(scratch)
                                   : (length - cursor);
        const stpg_err_t err = stpg_buffer_psram_read_bytes(offset + cursor, scratch, chunk);
        if (err != ERR_NONE) {
            stpg_usb_send_error(seq, err);
            return;
        }
        stpg_usb_write_bytes(scratch, chunk);
        cursor += chunk;
    }

    uint32_t payload_crc32 = 0u;
    const stpg_err_t crc_err = stpg_buffer_psram_read_prepare(offset, length, &payload_crc32);
    if (crc_err != ERR_NONE) {
        stpg_usb_send_error(seq, crc_err);
        return;
    }

    StpgStatusResponse rsp;
    stpg_status_build(RSP_DATA_END, seq, &rsp);
    rsp.pattern_size = length;
    rsp.event_count = payload_crc32;
    rsp.run_progress = offset;
    rsp.error_code = ERR_NONE;
    stpg_status_refresh_crc(&rsp);
    stpg_usb_write_response(&rsp);
}

static void stpg_usb_send_error(uint32_t seq, stpg_err_t error) {
    StpgStatusResponse rsp;
    stpg_status_set_error(error);
    stpg_status_build(RSP_ERROR, seq, &rsp);
    stpg_usb_write_response(&rsp);
}

static void stpg_usb_send_payload_error(uint32_t seq, stpg_err_t error) {
    StpgStatusResponse rsp;
    stpg_status_set_error(error);
    stpg_status_build(RSP_ERROR, seq, &rsp);
    if (error == ERR_BAD_CRC) {
        const stpg_psram_write_diag_t diag = stpg_psram_get_write_diag();
        if (diag.valid) {
            rsp.pattern_size = diag.offset;
            rsp.event_count = diag.size;
            rsp.run_progress = diag.mismatch_index;
            rsp.result_flags = diag.attempt;
            rsp.fail_count = diag.expected;
            rsp.sample_count = diag.actual;
        }
    }
    stpg_status_refresh_crc(&rsp);
    stpg_usb_write_response(&rsp);
}

void stpg_usb_protocol_init(void) {
    board_init();
    tusb_init();

    // A debugger-driven flash/reset can leave the host-side CDC node bound to
    // the pre-reset USB instance.  Force one bounded detach/attach transition
    // so the host observes a fresh device identity before sending commands.
    // Do not reinitialize the TinyUSB root port here; tusb_init() above owns
    // that initialization.
    tud_disconnect();
    sleep_ms(100);
    tud_connect();

    g_rx_state = RX_WAIT_MAGIC;
    g_magic_shift = 0;
    g_rx_pos = 0;
    g_pending_seq = 0;
    g_pending_offset = 0;
    g_pending_size = 0;
    g_pending_crc = 0;
}

static void stpg_usb_reset_rx(void) {
    g_rx_state = RX_WAIT_MAGIC;
    g_magic_shift = 0;
    g_rx_pos = 0;
    g_pending_seq = 0;
    g_pending_offset = 0;
    g_pending_size = 0;
    g_pending_crc = 0;
}

static void stpg_usb_enter_payload_state(const StpgCommand *cmd) {
    g_pending_seq = cmd->seq;
    g_pending_offset = cmd->arg0;
    g_pending_size = cmd->arg1;
    g_pending_crc = cmd->arg2;
    g_rx_pos = 0;
    g_rx_state = RX_FILL_PAYLOAD;
}

static void stpg_usb_handle_payload(void) {
    const stpg_err_t err = stpg_buffer_write_chunk(g_pending_offset, g_payload_buf,
                                                   g_pending_size, g_pending_crc);
    StpgStatusResponse rsp;
    if (err == ERR_NONE) {
        stpg_status_set_run_progress(stpg_buffer_received_size());
        stpg_status_build(RSP_DATA_ACK, g_pending_seq, &rsp);
        stpg_usb_write_response(&rsp);
    } else {
        g_usb_last_payload_error_code_raw = (uint32_t)err;
        stpg_usb_send_payload_error(g_pending_seq, err);
    }
    stpg_usb_reset_rx();
}

static void stpg_usb_handle_frame(void) {
    g_usb_frame_complete_count_raw += 1u;
    StpgCommand cmd;
    memcpy(&cmd, g_cmd_buf, sizeof(cmd));

    if (cmd.magic != STPG_CMD_MAGIC) {
        stpg_usb_reset_rx();
        return;
    }

    if (stpg_crc32(g_cmd_buf, STPG_CMD_SIZE - sizeof(cmd.crc32)) != cmd.crc32) {
        stpg_usb_send_error(cmd.seq, ERR_BAD_CRC);
        stpg_usb_reset_rx();
        return;
    }

    g_usb_dispatch_call_count_raw += 1u;
    StpgStatusResponse rsp;
    stpg_dispatch_command(&cmd, &rsp);
    const bool deferred_run_response =
        cmd.cmd == CMD_RUN
        && rsp.rsp == RSP_OK
        && stpg_runner_start_pending();
    if (deferred_run_response) {
        stpg_usb_run_response_begin();
    }
    stpg_usb_write_response(&rsp);

    if (cmd.cmd == CMD_UPLOAD_DATA && rsp.rsp == RSP_READY_FOR_PAYLOAD) {
        stpg_usb_enter_payload_state(&cmd);
    } else if (cmd.cmd == CMD_READ_SAMPLES && rsp.rsp == RSP_READY_FOR_READ) {
        stpg_usb_send_read_payload(cmd.seq, cmd.arg0, cmd.arg1);
        stpg_usb_reset_rx();
    } else if (cmd.cmd == CMD_READ_FAIL_LOG && rsp.rsp == RSP_READY_FOR_READ) {
        stpg_usb_send_fail_log_payload(cmd.seq, cmd.arg0, cmd.arg1);
        stpg_usb_reset_rx();
    } else if (cmd.cmd == CMD_PSRAM_READ && rsp.rsp == RSP_READY_FOR_READ) {
        stpg_usb_send_psram_payload(cmd.seq, cmd.arg0, cmd.arg1);
        stpg_usb_reset_rx();
    } else {
        stpg_usb_reset_rx();
    }
}

static void stpg_usb_accept_byte(uint8_t byte) {
    switch (g_rx_state) {
    case RX_WAIT_MAGIC:
        g_magic_shift = (g_magic_shift >> 8) | ((uint32_t)byte << 24);
        if (g_magic_shift == STPG_CMD_MAGIC) {
            memcpy(g_cmd_buf, &g_magic_shift, sizeof(g_magic_shift));
            g_rx_pos = sizeof(g_magic_shift);
            g_rx_state = RX_FILL_HEADER;
        }
        break;
    case RX_FILL_HEADER:
        g_cmd_buf[g_rx_pos++] = byte;
        if (g_rx_pos == STPG_CMD_SIZE) {
            stpg_usb_handle_frame();
        }
        break;
    case RX_FILL_PAYLOAD:
        g_payload_buf[g_rx_pos++] = byte;
        if (g_rx_pos == g_pending_size) {
            stpg_usb_handle_payload();
        }
        break;
    default:
        stpg_usb_reset_rx();
        break;
    }
}

void stpg_usb_protocol_task(void) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    const bool track = stpg_runner_is_active();
    const uint32_t started = track ? time_us_32() : 0u;
#endif
    stpg_usb_service_samples();
    g_usb_protocol_task_call_count_raw += 1u;
    stpg_usb_note_tud_task_call();
    stpg_usb_task_quantum();

    g_usb_available_poll_count_raw += 1u;
    /* Bound RX work per main-loop turn, retaining parser state across turns.
     * Continuous traffic must not starve runner/staging service indefinitely. */
    if (tud_cdc_available() > 0u) {
        uint8_t buf[64];
        const uint32_t count = tud_cdc_read(buf, sizeof(buf));
        g_usb_rx_read_call_count_raw += 1u;
        g_usb_rx_byte_count_raw += count;
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
        if (track) ++g_usb_service_diag.rx_reads;
#endif
        for (uint32_t i = 0; i < count; ++i) {
            stpg_usb_accept_byte(buf[i]);
        }
    }
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    if (track) {
        const uint32_t elapsed = time_us_32() - started;
        ++g_usb_service_diag.calls;
        g_usb_service_diag.total_us += elapsed;
        if (elapsed > g_usb_service_diag.max_us) g_usb_service_diag.max_us = elapsed;
    }
#endif
}
