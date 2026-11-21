/* Actual STOP/RESET/deferred-error bodies with explicit IRQ ordering stubs.
 * Hardware quiescence is a stub, NOT a SWD/DMA/USB recovery simulation. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "stpg_error.h"
#include "stpg_protocol.h"
#include "stpg_state.h"

static struct {
    bool pending_start, active, draining, streaming, reduced_density_streaming;
    bool pending_noready_prod_recheck, pending_stream_fail_from_irq;
    bool hold_awaiting_next, fail_stop_enabled;
    stpg_err_t pending_stream_fail_error;
    uint32_t drain_deadline_us, sample_event_count;
    uint32_t stream_last_progress_us, stream_last_dma_remaining;
    uint32_t stream_last_slot_index, stream_last_queued_event_count;
    uint32_t pending_noready_prod_completed_slot, fail_stop_next_event_index;
    struct {
        bool active;
        struct {
            bool stop_requested, fill_request_pending[STPG_STREAM_SLOT_COUNT];
            int fill_request_slot;
        } shared;
    } stream;
} g_runner;

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
static struct { bool pending; uint32_t calls; } g_terminal_replay;
#endif

static stpg_state_t state;
static stpg_err_t error, injected_error, failed_with;
static bool quiescent, failed_from_irq;
static uint32_t disable_calls, cancel_calls, failure_calls, finish_fail_stop_calls;
static uint32_t buffer_resets, sample_resets;

static void stpg_runner_request_stream_fail(stpg_err_t err, bool from_irq);

static void stpg_usb_run_response_cancel(void) { ++cancel_calls; }
static void __sev(void) {
    assert(g_runner.stream.shared.stop_requested);
    assert(g_runner.stream.shared.fill_request_slot == -1);
    for (uint32_t i = 0; i < STPG_STREAM_SLOT_COUNT; ++i)
        assert(!g_runner.stream.shared.fill_request_pending[i]);
}
static void stpg_runner_disable_hw_safe(void) {
    /* An already-pending IRQ may be handled during the stop path, before
     * owned DMA/PIO are quiescent. Clearing the latch before here is unsafe. */
    ++disable_calls;
    if (injected_error != ERR_NONE)
        stpg_runner_request_stream_fail(injected_error, true);
    quiescent = true;
    g_runner.hold_awaiting_next = false;
}
static void stpg_runner_reset_fail_stop_context(void) { g_runner.fail_stop_enabled = false; }
static void stpg_samples_reset(void) { ++sample_resets; }
static void stpg_status_set_run_progress(uint32_t count) { assert(count == 0); }
static void stpg_status_reset_result_info(void) {}
static void stpg_status_clear_error(void) { error = ERR_NONE; }
static void stpg_status_set_state(stpg_state_t next) { state = next; }
static void stpg_status_clear_boot_error(void) {}
static void stpg_status_reset_pattern_info(void) {}
static void stpg_buffer_reset(void) { ++buffer_resets; }
static void stpg_compare_reset(void) {}
static void stpg_status_build(uint16_t rsp, uint32_t seq, StpgStatusResponse *out) {
    memset(out, 0, sizeof(*out));
    out->rsp = rsp;
    out->seq = seq;
    out->state = state;
    out->error_code = error;
}
static void stpg_error_response(uint32_t seq, stpg_err_t err, StpgStatusResponse *out) {
    error = err;
    stpg_status_build(RSP_ERROR, seq, out);
}
static void stpg_runner_fail_streaming(stpg_err_t err, bool irq) {
    ++failure_calls;
    failed_with = err;
    failed_from_irq = irq;
    state = STATE_ERROR;
    error = err;
    g_runner.active = g_runner.streaming = false;
}
#if STPG_STREAM_FAIL_STOP
static void stpg_runner_finish_streaming_fail_stop(stpg_err_t err, uint32_t event) {
    assert(event == g_runner.fail_stop_next_event_index);
    ++finish_fail_stop_calls;
    stpg_runner_fail_streaming(err, true);
}
#endif

#include "runner-stop-subject.inc"

static void prepare(void) {
    memset(&g_runner, 0, sizeof(g_runner));
    g_runner.active = g_runner.streaming = g_runner.stream.active = true;
    g_runner.pending_start = g_runner.pending_noready_prod_recheck = true;
    g_runner.drain_deadline_us = g_runner.sample_event_count = 123u;
    g_runner.stream.shared.fill_request_slot = 1;
    for (uint32_t i = 0; i < STPG_STREAM_SLOT_COUNT; ++i)
        g_runner.stream.shared.fill_request_pending[i] = true;
    state = STATE_RUNNING;
    error = injected_error = failed_with = ERR_NONE;
    quiescent = failed_from_irq = false;
    disable_calls = cancel_calls = failure_calls = finish_fail_stop_calls = 0u;
    buffer_resets = sample_resets = 0u;
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
    g_terminal_replay.pending = true;
    g_terminal_replay.calls = 17u;
#endif
}

static void assert_stopped(void) {
    assert(quiescent && disable_calls == 1u && cancel_calls == 1u);
    assert(!g_runner.active && !g_runner.draining && !g_runner.streaming);
    assert(!g_runner.stream.active && !g_runner.pending_start);
    assert(!g_runner.pending_noready_prod_recheck && !g_runner.sample_event_count);
    assert(state == STATE_IDLE && error == ERR_NONE);
    consume_pending_error();
    assert(!failure_calls && state == STATE_IDLE && error == ERR_NONE);
    assert(g_runner.pending_stream_fail_error == ERR_NONE);
    assert(!g_runner.pending_stream_fail_from_irq);
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
    /* A later task turn cannot publish the canceled run's DONE/HOLD. */
    assert(!g_terminal_replay.pending && g_terminal_replay.calls == 0u);
#endif
}

int main(void) {
    const stpg_err_t old_errors[] = {ERR_BAD_STATE, ERR_RING_UNDERFLOW,
                                   ERR_FAIL_STOP_METADATA_OVERRUN};
    for (uint32_t i = 0; i < sizeof(old_errors) / sizeof(old_errors[0]); ++i) {
        for (uint32_t origin = 0; origin < 2; ++origin) {
            for (uint32_t late = 0; late < 2; ++late) {
                prepare();
                if (!late) stpg_runner_request_stream_fail(old_errors[i], origin != 0);
                else injected_error = old_errors[i];
                assert(stpg_runner_stop() == ERR_NONE);
                assert_stopped();
                assert(sample_resets == 1u);

                /* A new run's genuine error must still be delivered once. */
                g_runner.active = g_runner.streaming = true;
                const stpg_err_t new_error = origin ? ERR_RING_UNDERFLOW : ERR_DMA_ERROR;
                stpg_runner_request_stream_fail(new_error, origin != 0u);
                stpg_runner_request_stream_fail(ERR_BAD_STATE, origin == 0u);
                consume_pending_error();
                assert(failure_calls == 1u && failed_with == new_error);
                assert(failed_from_irq == (origin != 0u));
                assert(state == STATE_ERROR && error == new_error);
                assert(g_runner.pending_stream_fail_error == ERR_NONE);
                assert(!g_runner.pending_stream_fail_from_irq);
                consume_pending_error();
                assert(failure_calls == 1u);
            }
        }
    }
    for (uint32_t release = 0; release < 2; ++release) {
        prepare();
        stpg_runner_request_stream_fail(ERR_BAD_STATE, true);
        StpgCommand cmd = {.cmd = CMD_RESET, .seq = 7u,
                          .arg0 = release ? STPG_RESET_FLAG_RELEASE_OUTPUTS : 0u};
        StpgStatusResponse rsp;
        dispatch_reset(&cmd, &rsp);
        assert(rsp.rsp == RSP_OK && rsp.seq == 7u && rsp.state == STATE_IDLE);
        assert(buffer_resets == 1u && sample_resets == 2u);
        assert_stopped();
    }
    /* Release RESET must also clear a deferred error in inactive ERROR. */
    prepare();
    g_runner.active = g_runner.streaming = g_runner.pending_start = false;
    state = STATE_ERROR;
    stpg_runner_request_stream_fail(ERR_BAD_STATE, true);
    StpgCommand cmd = {.cmd = CMD_RESET, .seq = 8u,
                      .arg0 = STPG_RESET_FLAG_RELEASE_OUTPUTS};
    StpgStatusResponse rsp;
    dispatch_reset(&cmd, &rsp);
    assert_stopped();

    /* An invalid RESET must not cancel the active run's real pending error. */
    prepare();
    stpg_runner_request_stream_fail(ERR_BAD_STATE, true);
    cmd.arg1 = 1u;
    dispatch_reset(&cmd, &rsp);
    assert(rsp.rsp == RSP_ERROR && rsp.error_code == ERR_UNSUPPORTED);
    assert(!disable_calls && !sample_resets && !buffer_resets);
    assert(g_runner.pending_stream_fail_error == ERR_BAD_STATE);
    consume_pending_error();
    assert(failure_calls == 1u && failed_with == ERR_BAD_STATE && failed_from_irq);

    /* Inactive plain RESET retains its historical GPIO hold behavior. */
    prepare();
    g_runner.active = g_runner.streaming = g_runner.pending_start = false;
    state = STATE_DONE;
    cmd.arg0 = cmd.arg1 = 0u;
    dispatch_reset(&cmd, &rsp);
    assert(!disable_calls && !cancel_calls && buffer_resets == 1u);
    assert(rsp.rsp == RSP_OK && state == STATE_IDLE);

#if STPG_STREAM_FAIL_STOP
    prepare();
    g_runner.fail_stop_enabled = true;
    g_runner.fail_stop_next_event_index = 99u;
    stpg_runner_request_stream_fail(ERR_FAIL_STOP_METADATA_OVERRUN, true);
    consume_pending_error();
    assert(finish_fail_stop_calls == 1u && failure_calls == 1u);
    assert(failed_with == ERR_FAIL_STOP_METADATA_OVERRUN);
#endif
    puts("STOP/RESET deferred-error lifetime and new-error preservation PASS");
    return 0;
}
