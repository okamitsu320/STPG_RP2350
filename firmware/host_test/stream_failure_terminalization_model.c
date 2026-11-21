/* Extracted real runner/status bodies, modeled IRQ/service interleavings.
 * Not a physical IRQ/DMA/USB schedule or a live-debug continuity test. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define STPG_SAMPLE_PSRAM_SRAM_STAGING 1
#define STPG_STREAM_PSRAM_COMPARE_SAMPLE 1
#define STPG_STREAM_FAIL_STOP 0
#include "stpg_error.h"
#include "stpg_protocol.h"
#include "stpg_state.h"
typedef unsigned int uint;
enum { DMA_IRQ_0 = 0 };

static struct {
    bool fail_stop_terminalized, active, draining, streaming;
    bool reduced_density_streaming, core1_started, pending_noready_prod_recheck;
    stpg_err_t pending_stream_fail_error;
    bool pending_stream_fail_from_irq;
    int output_dma_channel, sample_dma_channel, sample_sched_dma_channel;
    uint32_t drain_deadline_us, sample_event_count, stream_last_progress_us;
    uint32_t stream_last_dma_remaining, stream_last_slot_index, stream_last_queued_event_count;
    struct {
        bool active;
        struct { bool stop_requested; int fill_request_slot; stpg_err_t fill_error; } shared;
        struct {
            uint32_t recovery_fail_stage, recovery_fail_main_loop_count;
            uint32_t recovery_fail_usb_task_count, recovery_fail_tud_task_count;
            uint32_t recovery_fail_usb_available_poll_count, recovery_fail_usb_rx_read_call_count;
            uint32_t recovery_fail_usb_rx_byte_count, recovery_fail_usb_frame_complete_count;
            uint32_t recovery_fail_usb_dispatch_call_count, recovery_fail_usb_write_response_count;
            uint32_t recovery_fail_usb_write_spin_count, recovery_fail_usb_last_write_success_bytes;
            uint32_t recovery_fail_usb_last_payload_error_code;
            uint32_t fail_snapshot_status_state, fail_snapshot_runner_flags;
            uint32_t fail_snapshot_shared_fill_request_slot, fail_snapshot_shared_fill_error;
            uint32_t fail_snapshot_from_irq, recovery_fail_dma_irq0_pending, recovery_fail_dma_irq0_enabled;
        } telemetry;
    } stream;
} g_runner;
static struct { bool pending; } g_terminal_replay;
static struct { stpg_state_t state; stpg_err_t error_code; } g_status;
static struct { uint32_t ints0; } dma_registers, *dma_hw = &dma_registers;
volatile uint32_t g_main_loop_heartbeat_count_raw, g_main_loop_stage_raw;
volatile uint32_t g_usb_protocol_task_call_count_raw, g_usb_tud_task_call_count_raw;
volatile uint32_t g_usb_available_poll_count_raw, g_usb_rx_read_call_count_raw;
volatile uint32_t g_usb_rx_byte_count_raw, g_usb_frame_complete_count_raw;
volatile uint32_t g_usb_dispatch_call_count_raw, g_usb_write_response_count_raw;
volatile uint32_t g_usb_write_spin_count_raw, g_usb_write_last_success_bytes_raw;
volatile uint32_t g_usb_last_payload_error_code_raw;
static unsigned finishes, snapshots, service_calls, status_writes;
static bool inject_during_service, inject_during_quiescence, hardware_quiescent;
static stpg_err_t service_result;
static uint32_t result_flags, sample_count, fail_count;
static void stpg_runner_request_stream_fail(stpg_err_t err, bool from_irq);
static bool irq_is_enabled(uint irq) { assert(irq == DMA_IRQ_0); return true; }
static void stpg_runner_finish_hw_hold_outputs(void) {
    ++finishes;
    /* Model an IRQ latched before the existing abort/ack sequence completes. */
    if (inject_during_quiescence && !hardware_quiescent)
        stpg_runner_request_stream_fail(ERR_BAD_STATE, true);
    hardware_quiescent = true;
}
static void stpg_buffer_snapshot_stream_telemetry(void) { ++snapshots; }
static void stpg_runner_clear_hold_state(void) {}
static void __sev(void) {}
static uint32_t stpg_samples_result_flags(void) { return STPG_RESULT_FLAG_SAMPLE_OVERFLOW; }
static void stpg_status_set_result_flags(uint32_t flags) { result_flags = flags; }
static void stpg_status_set_sample_count(uint32_t count) { sample_count = count; }
static void stpg_status_set_fail_count(uint32_t count) { fail_count = count; }
static uint32_t stpg_runner_captured_sample_count(void) { return 266236u; }
static stpg_err_t stpg_samples_service(uint32_t (*frontier)(void)) {
    ++service_calls;
    assert(frontier() == 266236u);
    if (inject_during_service) stpg_runner_request_stream_fail(ERR_BAD_STATE, true);
    return service_result;
}
#include "stream-failure-subject.inc"

static void reset_model(void) {
    memset(&g_runner, 0, sizeof(g_runner));
    g_runner.active = g_runner.streaming = g_runner.stream.active = g_runner.core1_started = true;
    g_runner.stream.shared.fill_request_slot = -1;
    g_runner.output_dma_channel = 0;
    g_runner.sample_dma_channel = g_runner.sample_sched_dma_channel = -1;
    g_status.state = STATE_RUNNING;
    g_status.error_code = ERR_NONE;
    finishes = snapshots = service_calls = status_writes = 0;
    inject_during_service = inject_during_quiescence = hardware_quiescent = false;
    service_result = ERR_NONE;
    result_flags = sample_count = fail_count = 99u;
}

static void check_terminal(stpg_err_t err, unsigned count, stpg_state_t prior, bool from_irq) {
    assert(g_status.state == STATE_ERROR && g_status.error_code == err);
    assert(finishes == count && snapshots == count && status_writes == count);
    assert(!g_runner.active && !g_runner.streaming && !g_runner.stream.active);
    assert(hardware_quiescent && g_runner.stream.shared.stop_requested);
    assert(result_flags == STPG_RESULT_FLAG_SAMPLE_OVERFLOW && sample_count == 0 && fail_count == 0);
    assert(g_runner.stream.telemetry.fail_snapshot_status_state == (uint32_t)prior);
    assert(g_runner.stream.telemetry.fail_snapshot_from_irq == (from_irq ? 1u : 0u));
    assert(g_runner.pending_stream_fail_error == ERR_NONE);
    assert(!g_runner.pending_stream_fail_from_irq);
}

int main(void) {
    reset_model();
    stpg_runner_request_stream_fail(ERR_BAD_STATE, true);
    stpg_runner_request_stream_fail(ERR_DMA_ERROR, false);
    assert(g_runner.pending_stream_fail_error == ERR_BAD_STATE);
    assert(stpg_runner_service_samples() && service_calls == 0);
    consume_pending_error();
    check_terminal(ERR_BAD_STATE, 1, STATE_RUNNING, true);

    reset_model();
    service_result = ERR_DMA_ERROR;
    assert(!stpg_runner_service_samples());
    consume_pending_error();
    check_terminal(ERR_DMA_ERROR, 1, STATE_RUNNING, false);

    for (unsigned phase = 0; phase < 2; ++phase) {
        reset_model();
        inject_during_service = phase == 0;
        inject_during_quiescence = phase == 1;
        service_result = ERR_DMA_ERROR;
        assert(!stpg_runner_service_samples());
        assert(g_status.error_code == ERR_DMA_ERROR && finishes == 1);
#if !EXPECT_HISTORICAL
        assert(g_runner.pending_stream_fail_error == ERR_NONE && !g_runner.pending_stream_fail_from_irq);
#endif
        assert(stpg_runner_service_samples() && service_calls == 1);
        consume_pending_error();
#if EXPECT_HISTORICAL
        check_terminal(ERR_BAD_STATE, 2, STATE_ERROR, true);
        assert(g_runner.stream.telemetry.fail_snapshot_runner_flags == 96u);
#else
        check_terminal(ERR_DMA_ERROR, 1, STATE_RUNNING, false);
        assert(g_runner.stream.telemetry.fail_snapshot_runner_flags == 53u);
#endif
    }

    reset_model();
    inject_during_service = true;
    assert(stpg_runner_service_samples());
    consume_pending_error();
    check_terminal(ERR_BAD_STATE, 1, STATE_RUNNING, true);

#if !EXPECT_HISTORICAL
    /* A nested promotion helper and its caller may both report the error. */
    stpg_runner_fail_streaming(ERR_DMA_ERROR, false);
    check_terminal(ERR_BAD_STATE, 1, STATE_RUNNING, true);
    /* ERROR alone must not skip cleanup when any ownership is still live. */
    for (unsigned owner = 0; owner < 3; ++owner) {
        reset_model();
        g_status.state = STATE_ERROR;
        g_runner.active = g_runner.draining = g_runner.streaming = false;
        if (owner == 0) g_runner.active = true;
        if (owner == 1) g_runner.draining = true;
        if (owner == 2) g_runner.streaming = true;
        stpg_runner_fail_streaming(ERR_DMA_ERROR, false);
        check_terminal(ERR_DMA_ERROR, 1, STATE_ERROR, false);
    }
    /* A newly admitted RUNNING run is not a previous terminal failure. */
    reset_model();
    g_runner.active = g_runner.draining = g_runner.streaming = false;
    stpg_runner_fail_streaming(ERR_DMA_ERROR, false);
    check_terminal(ERR_DMA_ERROR, 1, STATE_RUNNING, false);
#endif

    reset_model();
    g_runner.fail_stop_terminalized = true;
    stpg_runner_fail_streaming(ERR_DMA_ERROR, false);
    assert(finishes == 0 && g_status.state == STATE_RUNNING);
    puts(EXPECT_HISTORICAL ? "OLD_DOUBLE_FINALIZATION_REPRODUCED" : "SINGLE_FAILURE_FINALIZATION_MODEL_PASS");
    return 0;
}
