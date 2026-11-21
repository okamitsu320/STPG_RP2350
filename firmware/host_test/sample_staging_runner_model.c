/* Actual runner service block / terminal function, with ordering stubs.
 * This is not a DMA, QMI, electrical or cycle-accurate simulation. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

typedef uint32_t stpg_err_t;
typedef uint32_t (*stpg_sample_frontier_fn)(void);
enum { ERR_NONE = 0, ERR_BAD_STATE = 1, ERR_DMA_ERROR = 14,
       STATE_RUN_DRAINING = 5, STATE_DONE = 6, STATE_ERROR = 7, STATE_HOLD_AWAITING_NEXT = 8 };
#define STPG_SAMPLE_PSRAM_SRAM_STAGING 1
#define STPG_STREAM_PSRAM_COMPARE_SAMPLE 1
#define STPG_STREAM_FAIL_STOP 0
#define STPG_STREAM_STATUS_LOGICAL_EVENTS 1
#define STPG_STREAM_SLOT_COUNT 3
static struct {
    bool active, streaming, draining, reduced_density_streaming;
    bool fail_stop_terminalized, segment_end_hold;
    uint32_t pending_stream_fail_error, drain_deadline_us, sample_event_count;
    uint32_t stream_entry_drive_value, stream_entry_drive_enable;
    struct {
        bool active;
        struct { bool stop_requested, fill_request_pending[3]; int fill_request_slot; } shared;
        struct { uint32_t sample_capture_count, compare_event_count; } telemetry;
    } stream;
} g_runner;
static struct {
    struct { uint32_t compare_count, event_count, sample_index; } compare;
    bool pending;
    uint32_t calls, total_us, max_us;
} g_terminal_replay;
static uint32_t time_us_32(void) { static uint32_t now; return ++now; }
static uint32_t service_calls, finish_calls, finish_run_calls, compare_calls, fail_calls, task_tail;
static uint32_t status_state, status_error, status_samples;
static bool rx_stopped, terminal_held;
static stpg_err_t service_result, finish_result, compare_result;
static uint32_t stpg_runner_captured_sample_count(void) { return 8192; }
static void stpg_runner_fail_streaming(stpg_err_t err, bool irq) {
    assert(err == ERR_DMA_ERROR && !irq);
    ++fail_calls;
    g_runner.active = false;
    status_error = err;
    status_state = STATE_ERROR;
}
static stpg_err_t stpg_samples_service(stpg_sample_frontier_fn frontier) {
    assert(g_runner.active && g_runner.streaming && !g_runner.pending_stream_fail_error);
    assert(frontier() == 8192);
    ++service_calls;
    return service_result;
}
static void __sev(void) { assert(g_runner.stream.shared.stop_requested); }
static void stpg_runner_capture_terminal_hold_state(void) { terminal_held = true; }
static void stpg_runner_clear_hold_state(void) { terminal_held = false; }
static void stpg_runner_finish_hw_hold_outputs(void) {
    assert(g_runner.stream.shared.stop_requested && g_runner.stream.shared.fill_request_slot == -1);
    for (uint32_t i = 0; i < 3; ++i) assert(!g_runner.stream.shared.fill_request_pending[i]);
    rx_stopped = true;
}
static stpg_err_t stpg_samples_finish_staging(stpg_sample_frontier_fn frontier) {
    assert(rx_stopped && frontier() == 8192);
    assert(!finish_run_calls && !compare_calls);
    ++finish_calls;
    return finish_result;
}
static uint32_t stpg_buffer_stream_compare_event_count(void) { return 8192; }
static void stpg_samples_finish_run(uint32_t count, bool overflow) {
    assert(rx_stopped && finish_calls == 1 && finish_result == ERR_NONE);
    assert(count == 8192 && !overflow && !compare_calls);
    ++finish_run_calls;
}
static void stpg_buffer_snapshot_stream_telemetry(void) { assert(finish_run_calls == 1); }
static bool stpg_fpu_access_ensure_for_run(void) { return true; }
static void *stpg_runner_stream_state(void) { return &g_runner.stream; }
static stpg_err_t stpg_compare_replay_begin(void *replay, void *state, uint32_t value, uint32_t enable) {
    assert(rx_stopped && finish_calls == 1 && finish_run_calls == 1 && finish_result == ERR_NONE);
    assert(state == &g_runner.stream && value == 0x1234 && enable == 0x5678);
    assert(replay == &g_terminal_replay.compare);
    return ERR_NONE;
}
static stpg_err_t stpg_compare_replay_step(void *replay, bool *done) {
    assert(replay == &g_terminal_replay.compare && g_terminal_replay.pending);
    ++compare_calls;
    *done = compare_calls == 8;
    g_terminal_replay.compare.compare_count = compare_calls * 1024;
    return compare_result;
}
static void stpg_runner_reset_fail_stop_context(void) {}
static uint32_t stpg_samples_result_flags(void) { return 0; }
static uint32_t stpg_compare_result_flags(void) { return 0; }
static uint32_t stpg_compare_fail_count(void) { return 0; }
static uint32_t stpg_samples_sample_count(void) { assert(finish_run_calls == 1); return 8192; }
static void stpg_status_set_result_flags(uint32_t value) { assert(!value); }
static void stpg_status_set_fail_count(uint32_t value) { assert(!value); }
static void stpg_status_set_sample_count(uint32_t value) { status_samples = value; }
static void stpg_status_set_error(uint32_t value) { status_error = value; }
static void stpg_status_clear_error(void) { status_error = 0; }
static void stpg_status_set_state(uint32_t value) { status_state = value; }
static uint32_t stpg_buffer_pattern_size(void) { return 164008; }
static uint32_t stpg_buffer_event_count(void) { return 16384; }
static void stpg_status_set_pattern_info(uint32_t size, uint32_t events) { assert(size == 164008 && events == 16384); }
static void stpg_status_set_run_progress(uint32_t value) { assert(value == 16384); }

#include "sample-staging-subject.inc"

static void prepare(void) {
    memset(&g_runner, 0, sizeof(g_runner));
    memset(&g_terminal_replay, 0, sizeof(g_terminal_replay));
    g_runner.active = g_runner.streaming = g_runner.draining = g_runner.stream.active = true;
    g_runner.stream_entry_drive_value = 0x1234;
    g_runner.stream_entry_drive_enable = 0x5678;
    service_calls = finish_calls = finish_run_calls = compare_calls = fail_calls = task_tail = 0;
    status_state = status_error = status_samples = 0;
    rx_stopped = terminal_held = false;
    service_result = finish_result = compare_result = ERR_NONE;
}

int main(void) {
    for (uint32_t held = 0; held < 2; ++held) {
        prepare();
        g_runner.segment_end_hold = held;
        extracted_task_prefix();
        assert(service_calls == 1 && task_tail == 1 && !rx_stopped);
        stpg_runner_finish_streaming_success();
        assert(finish_calls == 1 && finish_run_calls == 1 && !compare_calls && !fail_calls);
        assert(g_terminal_replay.pending && g_runner.active && status_state == STATE_RUN_DRAINING);
        for (uint32_t i = 0; i < 8; ++i) {
            assert(stpg_runner_service_samples() && service_calls == 1); /* RX already frozen */
            stpg_runner_terminal_replay_task();
            assert(compare_calls == i + 1);
            if (i < 7) assert(g_runner.active && status_state == STATE_RUN_DRAINING);
        }
        assert(!g_terminal_replay.pending && g_terminal_replay.calls == 8);
        assert(!g_runner.active && !g_runner.streaming && status_samples == 8192 && !status_error);
        assert(terminal_held == (bool)held);
        assert(status_state == (held ? STATE_HOLD_AWAITING_NEXT : STATE_DONE));
    }
    for (uint32_t unavailable = 0; unavailable < 3; ++unavailable) {
        prepare();
        if (unavailable == 0) g_runner.active = false;
        if (unavailable == 1) g_runner.streaming = false;
        if (unavailable == 2) g_runner.pending_stream_fail_error = ERR_BAD_STATE;
        extracted_task_prefix();
        assert(!service_calls && !fail_calls && task_tail == 1);
    }
    prepare();
    service_result = ERR_DMA_ERROR;
    extracted_task_prefix();
    assert(service_calls == 1 && fail_calls == 1 && !task_tail);
    assert(status_error == ERR_DMA_ERROR && status_state == STATE_ERROR);
    prepare();
    finish_result = ERR_DMA_ERROR;
    stpg_runner_finish_streaming_success();
    assert(rx_stopped && finish_calls == 1 && fail_calls == 1 && !finish_run_calls && !compare_calls);
    assert(status_state == STATE_ERROR && !status_samples);
    prepare();
    compare_result = ERR_BAD_STATE;
    stpg_runner_finish_streaming_success();
    stpg_runner_terminal_replay_task();
    assert(finish_calls == 1 && finish_run_calls == 1 && compare_calls == 1);
    assert(status_error == ERR_BAD_STATE && status_state == STATE_ERROR);
    return 0;
}
