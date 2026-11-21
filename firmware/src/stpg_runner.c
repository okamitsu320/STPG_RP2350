#include "stpg_runner.h"

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "hardware/dma.h"
#include "hardware/irq.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#ifdef PICO_ON_DEVICE
#include "hardware/clocks.h"
#include "pico/flash.h"
#include "pico/multicore.h"
#endif
#include "pico.h"
#include "pico/time.h"

#include "stpg_app.h"
#include "stpg_board.h"
#include "stpg_compare.h"
#include "stpg_common.h"
#include "stpg_fail_stop.h"
#include "stpg_fpu_access.h"
#include "stpg_flash_xip.h"
#include "stpg_pattern_buffer.h"
#include "stpg_psram.h"
#include "stpg_runner.pio.h"
#include "stpg_runner27.pio.h"
#include "stpg_runner_reduced.pio.h"
#include "stpg_runner27_reduced.pio.h"
#include "stpg_sample_in.pio.h"
#include "stpg_sample_in27.pio.h"
#include "stpg_sample_delayed_in.pio.h"
#include "stpg_sample_delayed_in27.pio.h"
#include "stpg_sample_delayed_schedule_in.pio.h"
#include "stpg_sample_delayed_schedule_in27.pio.h"
#include "stpg_samples.h"
#include "stpg_stack_watermark.h"
#include "stpg_state.h"
#include "stpg_status.h"
#include "stpg_stream_handoff.h"
#include "stpg_stream_watchdog.h"
#include "stpg_usb_protocol.h"

#ifndef STPG_STREAM_PRODUCER_SRAM
#define STPG_STREAM_PRODUCER_SRAM 0
#endif

#ifndef STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF
#define STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF 0
#endif

#ifndef STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF
#define STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF 0
#endif

#ifndef STPG_STREAM_FIFO_FILL_REQUESTS
#define STPG_STREAM_FIFO_FILL_REQUESTS 0
#endif

#ifndef STPG_STREAM_FAIL_STOP
#define STPG_STREAM_FAIL_STOP 0
#endif

#if STPG_STREAM_PRODUCER_SRAM && defined(PICO_ON_DEVICE)
#define STPG_STREAM_PRODUCER_FUNC(name) __no_inline_not_in_flash_func(name)
#else
#define STPG_STREAM_PRODUCER_FUNC(name) name
#endif

_Static_assert(STPG_STREAM_SLOT_COUNT >= 2u, "streaming ring requires at least two slots");

#if STPG_STREAM_COMPARE_EVENT_SCHEDULE
#define STPG_SAMPLE_DELAYED_IN_PROGRAM stpg_sample_delayed_schedule_in_program
#define STPG_SAMPLE_DELAYED_IN27_PROGRAM stpg_sample_delayed_schedule_in27_program
#define STPG_SAMPLE_DELAYED_IN_PROGRAM_GET_DEFAULT_CONFIG stpg_sample_delayed_schedule_in_program_get_default_config
#define STPG_SAMPLE_DELAYED_IN27_PROGRAM_GET_DEFAULT_CONFIG stpg_sample_delayed_schedule_in27_program_get_default_config
#else
#define STPG_SAMPLE_DELAYED_IN_PROGRAM stpg_sample_delayed_in_program
#define STPG_SAMPLE_DELAYED_IN27_PROGRAM stpg_sample_delayed_in27_program
#define STPG_SAMPLE_DELAYED_IN_PROGRAM_GET_DEFAULT_CONFIG stpg_sample_delayed_in_program_get_default_config
#define STPG_SAMPLE_DELAYED_IN27_PROGRAM_GET_DEFAULT_CONFIG stpg_sample_delayed_in27_program_get_default_config
#endif
#define STPG_STREAM_SLOT_WORD_CAPACITY (STPG_STREAM_BUFFER_EVENTS * 4u)

_Static_assert(
    (STPG_STREAM_SLOT_COUNT * STPG_STREAM_SLOT_WORD_CAPACITY) <= (STPG_MAX_PATTERN_BYTES / sizeof(uint32_t)),
    "streaming slots exceed pattern scratch capacity"
);

_Static_assert(
    !STPG_STREAM_FAIL_STOP
        || (STPG_STREAM_SLOT_COUNT * STPG_STREAM_BUFFER_EVENTS) <= STPG_MAX_EVENT_COUNT,
    "streaming fail-stop metadata exceeds SRAM capacity"
);

typedef enum {
    STPG_UNDERFLOW_REASON_NONE = 0u,
    STPG_UNDERFLOW_REASON_NO_READY_SLOT = 1u,
    STPG_UNDERFLOW_REASON_DMA_RESTART_GAP = 2u,
    STPG_UNDERFLOW_REASON_FILL_ERROR = 3u,
    STPG_UNDERFLOW_REASON_STREAM_STALL = 4u,
} stpg_stream_underflow_reason_t;

typedef enum {
    STPG_STARTUP_PREFILL_BREAK_NONE = 0u,
    STPG_STARTUP_PREFILL_BREAK_EOF = 1u,
    STPG_STARTUP_PREFILL_BREAK_ZERO_EVENT = 2u,
    STPG_STARTUP_PREFILL_BREAK_WAIT_ERROR = 3u,
} stpg_startup_prefill_break_reason_t;

#define STPG_STREAM_PREFILL_TIMEOUT_US 250000u
#define STPG_SAMPLE_SCHEDULE_HANDOFF_TIMEOUT_US 10000u
#define STPG_VISIBILITY_PROBE_TIMEOUT_US 250000u
#define STPG_VISIBILITY_VALUE1 0x13579BDFu
#define STPG_VISIBILITY_VALUE2 0x2468ACE0u

typedef enum {
    STPG_STREAM_SLOT_EMPTY = 0u,
    STPG_STREAM_SLOT_FILLING = 1u,
    STPG_STREAM_SLOT_READY = 2u,
    STPG_STREAM_SLOT_ARMED = 3u,
    STPG_STREAM_SLOT_ACTIVE = 4u,
} stpg_stream_slot_state_t;

typedef struct {
    uint32_t *words;
    uint32_t event_count;
    uint32_t word_count;
    uint32_t sample_count;
    uint32_t stream_event_start;
    uint32_t stream_event_end;
    uint32_t p4_explicit_wait_end_vector_index;
    bool p4_explicit_wait_end_present;
    bool eof;
    stpg_stream_slot_state_t state;
    stpg_fail_stop_metadata_t fail_stop_metadata;
#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
    uint32_t refill_trace_index;
#endif
} stpg_stream_slot_t;

typedef struct {
    volatile bool stop_requested;
    volatile int32_t fill_request_slot;
    /*
     * A single fill_request_slot is only a level-triggered mailbox: a second
     * producer request can overwrite the first one before core 1 observes it.
     * Keep one pending bit per slot so requests for distinct slots cannot be
     * lost.  A slot remains FILLING until core 1 claims its bit, so the same
     * slot cannot be queued twice.
     */
    volatile bool fill_request_pending[STPG_STREAM_SLOT_COUNT];
    /* The optional FIFO is an SPSC mailbox: Core0 enqueues refill slots and
     * Core1 claims them in the same order.  The legacy pending-bit scan is
     * retained for the default build; it is not order preserving. */
    volatile uint32_t fill_request_queue_head;
    volatile uint32_t fill_request_queue_tail;
    volatile uint32_t fill_request_queue[STPG_STREAM_SLOT_COUNT];
    volatile bool slot_ready[STPG_STREAM_SLOT_COUNT];
    volatile uint32_t slot_event_count[STPG_STREAM_SLOT_COUNT];
    volatile uint32_t slot_word_count[STPG_STREAM_SLOT_COUNT];
    volatile uint32_t slot_sample_count[STPG_STREAM_SLOT_COUNT];
    volatile uint32_t slot_metadata_sample_start[STPG_STREAM_SLOT_COUNT];
    volatile uint32_t slot_metadata_count[STPG_STREAM_SLOT_COUNT];
    volatile uint32_t producer_sample_count;
    volatile uint32_t slot_p4_explicit_wait_end_vector_index[STPG_STREAM_SLOT_COUNT];
    volatile bool slot_p4_explicit_wait_end_present[STPG_STREAM_SLOT_COUNT];
    volatile bool slot_eof[STPG_STREAM_SLOT_COUNT];
    volatile stpg_err_t slot_fill_error[STPG_STREAM_SLOT_COUNT];
    volatile stpg_err_t fill_error;
} stpg_stream_shared_t;

typedef struct {
    volatile uint32_t refill_count;
    volatile uint32_t refill_event_count;
    volatile uint32_t refill_total_us;
    volatile uint32_t refill_max_us;
    volatile uint32_t refill_last_us;
    volatile uint32_t refill_last_event_count;
    volatile uint32_t swap_count;
    volatile uint32_t underflow_count;
    volatile uint32_t irq_swap_count;
    volatile uint32_t task_swap_count;
    volatile uint32_t queue_slot_count;
    volatile uint32_t slot_event_capacity;
    volatile uint32_t startup_prefill_slots;
    volatile uint32_t startup_prefill_total_us;
    volatile uint32_t min_ready_slots_observed;
    volatile uint32_t swap_restart_total_us;
    volatile uint32_t swap_restart_max_us;
    volatile uint32_t underflow_reason;
    volatile uint32_t sample_schedule_word_count;
    volatile uint32_t sample_request_count;
    volatile uint32_t sample_capture_count;
    volatile uint32_t compare_event_count;
    volatile uint32_t core1_loop_count;
    volatile uint32_t core1_request_count;
    volatile uint32_t core1_service_count;
    volatile uint32_t core1_last_slot_index;
    volatile uint32_t core1_last_fill_error;
    volatile uint32_t startup_prefill_attempted_slots;
    volatile uint32_t startup_prefill_last_slot_index;
    volatile uint32_t startup_prefill_break_reason;
    volatile uint32_t startup_prefill_wait_error;
    volatile uint32_t startup_prefill_last_event_count;
    volatile uint32_t startup_prefill_last_word_count;
    volatile uint32_t startup_timeout_core1_loop_count;
    volatile uint32_t startup_timeout_core1_service_count;
    volatile uint32_t startup_timeout_stop_requested;
    volatile uint32_t startup_timeout_fill_request_slot;
    volatile uint32_t startup_timeout_core1_boot_stage_raw;
    volatile uint32_t startup_timeout_core1_request_raw;
    volatile uint32_t startup_timeout_core1_service_raw;
    volatile uint32_t startup_timeout_core1_fill_error_raw;
    volatile uint32_t startup_timeout_core1_request_seen_raw;
    volatile uint32_t startup_timeout_core1_claim_raw;
    volatile uint32_t startup_timeout_core1_skip_stop_raw;
    volatile uint32_t startup_timeout_core1_skip_no_request_raw;
    volatile uint32_t startup_timeout_core1_last_seen_slot_raw;
    volatile uint32_t startup_timeout_core1_last_polled_slot_raw;
    volatile uint32_t startup_timeout_core1_negative_poll_count_raw;
    volatile uint32_t startup_timeout_core1_valid_poll_count_raw;
    volatile uint32_t startup_timeout_core1_invalid_poll_count_raw;
    volatile uint32_t startup_timeout_core1_last_polled_fill_error_raw;
    volatile uint32_t startup_timeout_core1_last_polled_slot0_ready_raw;
    volatile uint32_t startup_timeout_request_set_us;
    volatile uint32_t startup_timeout_request_clear_us;
    volatile uint32_t startup_timeout_core1_first_poll_us;
    volatile uint32_t startup_timeout_core1_first_negative_poll_us;
    volatile uint32_t startup_timeout_core1_first_valid_poll_us;
    volatile uint32_t startup_timeout_core1_last_stage_raw;
    volatile uint32_t startup_timeout_core1_after_stop_gate_us;
    volatile uint32_t startup_timeout_core1_after_dmb_us;
    volatile uint32_t startup_timeout_core1_after_slot_read_us;
    volatile uint32_t visibility_probe_request_set_us;
    volatile uint32_t visibility_probe_core1_saw_request_us;
    volatile uint32_t visibility_probe_core1_write1_us;
    volatile uint32_t visibility_probe_core1_write2_us;
    volatile uint32_t visibility_probe_core1_done_us;
    volatile uint32_t visibility_probe_core0_first_stage_seen_us;
    volatile uint32_t visibility_probe_core0_first_value1_seen_us;
    volatile uint32_t visibility_probe_core0_first_value2_seen_us;
    volatile uint32_t visibility_probe_core0_first_done_seen_us;
    volatile uint32_t visibility_probe_final_stage;
    volatile uint32_t visibility_probe_final_value1;
    volatile uint32_t visibility_probe_final_value2;
    volatile uint32_t visibility_probe_final_done;
    volatile uint32_t visibility_probe_timeout_hit;
    volatile uint32_t handshake_probe_request_set_us;
    volatile uint32_t handshake_probe_core1_saw_request_us;
    volatile uint32_t handshake_probe_core1_claim_us;
    volatile uint32_t handshake_probe_core1_ready_us;
    volatile uint32_t handshake_probe_core0_ready_seen_us;
    volatile uint32_t handshake_probe_core0_error_seen_us;
    volatile uint32_t handshake_probe_final_request_slot;
    volatile uint32_t handshake_probe_final_ready;
    volatile uint32_t handshake_probe_final_error;
    volatile uint32_t handshake_probe_timeout_hit;
    volatile uint32_t startup_timeout_shared_fill_request_slot;
    volatile uint32_t startup_timeout_shared_slot0_ready;
    volatile uint32_t startup_timeout_shared_fill_error;
    volatile uint32_t startup_timeout_shared_slot0_event_count;
    volatile uint32_t startup_timeout_shared_slot0_word_count;
    volatile uint32_t fail_snapshot_status_state;
    volatile uint32_t fail_snapshot_runner_flags;
    volatile uint32_t fail_snapshot_shared_fill_request_slot;
    volatile uint32_t fail_snapshot_shared_fill_error;
    volatile uint32_t fail_snapshot_from_irq;
    volatile uint32_t dma_abort_output_us;
    volatile uint32_t dma_abort_sample_us;
    volatile uint32_t dma_abort_sample_sched_us;
    volatile uint32_t dma_abort_total_us;
    volatile uint32_t startup_first_fill_request_us;
    volatile uint32_t startup_first_service_complete_us;
    volatile uint32_t startup_slot0_ready_us;
    volatile uint32_t recovery_live_main_loop_count;
    volatile uint32_t recovery_live_main_loop_stage;
    volatile uint32_t recovery_live_usb_task_count;
    volatile uint32_t recovery_live_tud_task_count;
    volatile uint32_t recovery_fail_stage;
    volatile uint32_t recovery_fail_main_loop_count;
    volatile uint32_t recovery_fail_usb_task_count;
    volatile uint32_t recovery_fail_tud_task_count;
    volatile uint32_t recovery_fail_dma_irq0_pending;
    volatile uint32_t recovery_fail_dma_irq0_enabled;
    volatile uint32_t recovery_live_usb_available_poll_count;
    volatile uint32_t recovery_live_usb_rx_read_call_count;
    volatile uint32_t recovery_live_usb_rx_byte_count;
    volatile uint32_t recovery_live_usb_frame_complete_count;
    volatile uint32_t recovery_live_usb_dispatch_call_count;
    volatile uint32_t recovery_live_usb_write_response_count;
    volatile uint32_t recovery_fail_usb_available_poll_count;
    volatile uint32_t recovery_fail_usb_rx_read_call_count;
    volatile uint32_t recovery_fail_usb_rx_byte_count;
    volatile uint32_t recovery_fail_usb_frame_complete_count;
    volatile uint32_t recovery_fail_usb_dispatch_call_count;
    volatile uint32_t recovery_fail_usb_write_response_count;
    volatile uint32_t recovery_live_usb_write_spin_count;
    volatile uint32_t recovery_live_usb_last_write_success_bytes;
    volatile uint32_t recovery_live_usb_last_payload_error_code;
    volatile uint32_t recovery_fail_usb_write_spin_count;
    volatile uint32_t recovery_fail_usb_last_write_success_bytes;
    volatile uint32_t recovery_fail_usb_last_payload_error_code;
    volatile uint32_t underflow_snapshot_active_slot;
    volatile uint32_t underflow_snapshot_next_requested_slot;
    volatile uint32_t underflow_snapshot_next_ready_slot;
    volatile uint32_t underflow_snapshot_ready_mask;
    volatile uint32_t underflow_snapshot_request_seq;
    volatile uint32_t underflow_snapshot_service_seq;
    volatile uint32_t underflow_snapshot_last_service_slot;
    volatile uint32_t underflow_snapshot_last_service_event_count;
    volatile uint32_t underflow_snapshot_last_service_word_count;
    volatile uint32_t underflow_snapshot_last_service_sample_count;
    volatile uint32_t underflow_snapshot_queued_event_count;
    volatile uint32_t underflow_snapshot_reason;
    volatile uint32_t prod_noready_defer_count;
    volatile uint32_t prod_noready_recheck_success_count;
    volatile uint32_t prod_noready_recheck_fail_count;
    volatile uint32_t prod_noready_safety_reject_count;
    volatile uint32_t prod_noready_last_iterations;
    volatile uint32_t prod_noready_last_wait_us;
    volatile uint32_t prod_noready_wait_step_us;
    volatile uint32_t prod_noready_max_iterations;
    volatile uint32_t prod_noready_defer_dma_busy;
    volatile uint32_t prod_noready_defer_dma_remaining;
    volatile uint32_t prod_noready_defer_pio_txempty;
    volatile uint32_t prod_noready_defer_completed_slot;
    volatile uint32_t prod_noready_defer_queued_events;
    volatile uint32_t prod_noready_defer_safety_flags;
    volatile uint32_t underflow_snapshot_vector_index;
    volatile uint32_t underflow_snapshot_repeat_index;
    volatile uint32_t underflow_snapshot_rate_id;
    volatile uint32_t underflow_snapshot_period_ticks;
    volatile uint32_t underflow_snapshot_prev_period_ticks;
    volatile uint32_t underflow_snapshot_desired_delta_ticks;
    volatile uint32_t underflow_snapshot_vector_repeat;
    volatile uint32_t underflow_snapshot_vector_flags;
    volatile uint32_t underflow_snapshot_current_value;
    volatile uint32_t underflow_snapshot_current_dir;
    volatile uint32_t underflow_snapshot_state_flags;
    volatile uint32_t p3_stream_run_sequence;
    volatile uint32_t p3_telemetry_sequence;
    volatile uint32_t p3_chain_arm_count;
    volatile uint32_t p3_chain_completion_count;
    volatile uint32_t p3_no_ready_fail_count;
    volatile uint32_t p3_terminal_chain_neutralize_count;
    volatile uint32_t p3_no_ready_chain_neutralize_count;
    volatile uint32_t p3_max_irq_to_arm_us;
    volatile uint32_t p3_handoff_owner;
    volatile uint32_t p3_handoff_conflict_count;
    volatile uint32_t p3_irq_handoff_count;
    volatile uint32_t p3_main_loop_handoff_count;
    volatile uint32_t p3_main_loop_handoff_last_us;
    volatile uint32_t p3_main_loop_handoff_max_us;
    volatile uint32_t p3_main_loop_rearm_count;
    volatile uint32_t p3_main_loop_rearm_last_us;
    volatile uint32_t p3_main_loop_idle_fallback_count;
    volatile uint32_t p4_diag_marker_slot;
    volatile uint32_t p4_diag_marker_vector_index;
    volatile uint32_t p4_diag_observation_count;
    volatile uint32_t p4_diag_active_dma_remaining;
    volatile uint32_t p4_diag_active_dma_busy;
    volatile uint32_t p4_diag_promotable_slot;
    volatile uint32_t p4_diag_promotable_count;
    volatile uint32_t p4_diag_first_promotable_us;
    volatile uint32_t p4_diag_spare_state;
    volatile uint32_t p4_diag_ready_before_end;
    volatile uint32_t dma_pio_boundary_count;
    volatile uint32_t dma_pio_last_entry_channel;
    volatile uint32_t dma_pio_last_entry_irq_pending;
    volatile uint32_t dma_pio_last_entry_busy;
    volatile uint32_t dma_pio_last_entry_remaining;
    volatile uint32_t dma_pio_last_entry_fstat;
    volatile uint32_t dma_pio_last_entry_fdebug;
    volatile uint32_t dma_pio_last_entry_flevel;
    volatile uint32_t dma_pio_last_exit_channel;
    volatile uint32_t dma_pio_last_exit_busy;
    volatile uint32_t dma_pio_last_exit_remaining;
    volatile uint32_t dma_pio_last_exit_fstat;
    volatile uint32_t dma_pio_last_exit_fdebug;
    volatile uint32_t dma_pio_last_exit_flevel;
    volatile uint32_t dma_pio_last_handler_us;
    volatile uint32_t dma_completion_trace_count;
    volatile uint32_t dma_completion_trace_overflow;
    volatile uint32_t dma_completion_trace_boundary_count;
    volatile uint32_t dma_completion_trace_max_restart_us;
    volatile uint32_t dma_completion_trace_last_path_flags;
    volatile stpg_dma_completion_trace_t dma_completion_trace[STPG_DMA_COMPLETION_TRACE_CAPACITY];
#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
    volatile uint32_t refill_trace_count;
    volatile uint32_t refill_trace_overflow;
    volatile stpg_refill_trace_t refill_trace[STPG_REFILL_TRACE_CAPACITY];
#endif
} stpg_stream_telemetry_shared_t;

typedef struct {
    bool active;
    uint32_t queued_event_count;
    uint32_t active_slot_index;
    stpg_stream_slot_t slots[STPG_STREAM_SLOT_COUNT];
    stpg_stream_shared_t shared;
    stpg_stream_telemetry_shared_t telemetry;
} stpg_stream_context_t;

typedef struct {
    bool initialized;
    bool output_program_loaded;
    bool output_program27_loaded;
    bool sample_program_loaded;
    bool sample_program27_loaded;
    bool output_reduced_program_loaded;
    bool output27_reduced_program_loaded;
    bool sample_delayed_program_loaded;
    bool sample_delayed27_program_loaded;
    bool active;
    bool draining;
    bool streaming;
    bool reduced_density_streaming;
    bool segment_end_hold;
    bool hold_awaiting_next;
    bool hold_valid;
    /* Entry state for the current streaming segment.  Rate Vector compare
     * replay must start from the held state, not from logical zero. */
    uint32_t stream_entry_drive_value;
    uint32_t stream_entry_drive_enable;
    bool core1_started;
    bool output_dma_chain_enabled;
    bool p4_explicit_wait_enabled;
    bool fail_stop_enabled;
    uint32_t fail_stop_limit;
    bool fail_stop_terminalized;
    int output_dma_channel;
    int output_dma_spare_channel;
    int32_t output_dma_armed_slot_index;
    int sample_dma_channel;
    int sample_sched_dma_channel;
    uint output_pio_offset;
    uint output_pio27_offset;
    uint output_reduced_pio_offset;
    uint output27_reduced_pio_offset;
    uint sample_pio_offset;
    uint sample_pio27_offset;
    uint sample_delayed_pio_offset;
    uint sample_delayed27_pio_offset;
    uint32_t event_count;
    uint32_t sample_event_count;
    uint64_t drain_deadline_us;
    uint64_t tail_drain_ticks;
    uint32_t stream_last_progress_us;
    uint32_t stream_last_dma_remaining;
    uint32_t stream_last_slot_index;
    uint32_t stream_last_queued_event_count;
    uint32_t fail_stop_next_event_index;
    uint32_t fail_stop_next_sample_index;
    uint32_t hold_drive_value;
    uint32_t hold_drive_enable;
    bool pending_noready_prod_recheck;
    uint32_t pending_noready_prod_completed_slot;
    uint32_t pending_noready_prod_recheck_start_us;
    uint32_t pending_noready_prod_recheck_iterations;
    bool pending_start;
    bool stream_timed_wait_seen;
    uint32_t stream_wait_budget_us;
    uint32_t pending_start_run_flags;
    uint32_t pending_start_event_index;
    uint32_t pending_start_max_events;
    uint32_t pending_start_aux_arg;
    stpg_err_t pending_stream_fail_error;
    bool pending_stream_fail_from_irq;
    stpg_stream_context_t stream;
} stpg_runner_context_t;

static stpg_runner_context_t g_runner;
static volatile uint32_t g_chained_irq_reject[6];
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
static struct {
    stpg_compare_replay_t compare;
    bool pending;
    uint32_t calls, total_us, max_us;
} g_terminal_replay;
#endif
static volatile uint32_t g_stream_core1_loop_raw;
static volatile uint32_t g_stream_core1_request_raw;
static volatile uint32_t g_stream_core1_service_raw;
static volatile uint32_t g_stream_core1_service_slot_raw;
static volatile uint32_t g_stream_core1_fill_error_raw;
static uint32_t g_stpg_runner_stream_run_sequence;
static uint32_t g_dma_irq_entry_us_raw;

enum {
    STPG_OUTPUT_HANDOFF_OWNER_NONE = 0u,
    STPG_OUTPUT_HANDOFF_OWNER_IRQ = 1u,
    STPG_OUTPUT_HANDOFF_OWNER_MAIN_LOOP = 2u,
};

static uint32_t stpg_runner_stream_ready_mask(void) {
    uint32_t ready_mask = 0u;
    for (uint32_t index = 0u; index < STPG_STREAM_SLOT_COUNT; ++index) {
        if (g_runner.stream.shared.slot_ready[index]) {
            ready_mask |= (1u << index);
        }
    }
    return ready_mask;
}

static inline stpg_rate_stream_state_t *stpg_runner_stream_state(void) {
    return stpg_buffer_stream_state_storage();
}

static void stpg_runner_capture_underflow_snapshot(uint32_t active_slot,
                                                   int32_t next_ready_slot,
                                                   uint32_t underflow_reason) {
    stpg_stream_slot_t *active_slot_ptr = &g_runner.stream.slots[active_slot];
    g_runner.stream.telemetry.underflow_snapshot_active_slot = active_slot + 1u;
    g_runner.stream.telemetry.underflow_snapshot_next_requested_slot =
        (uint32_t)(g_runner.stream.shared.fill_request_slot + 1);
    g_runner.stream.telemetry.underflow_snapshot_next_ready_slot =
        (uint32_t)(next_ready_slot + 1);
    g_runner.stream.telemetry.underflow_snapshot_ready_mask = stpg_runner_stream_ready_mask();
    g_runner.stream.telemetry.underflow_snapshot_request_seq = g_stream_core1_request_raw;
    g_runner.stream.telemetry.underflow_snapshot_service_seq = g_stream_core1_service_raw;
    g_runner.stream.telemetry.underflow_snapshot_last_service_slot =
        active_slot + 1u;
    g_runner.stream.telemetry.underflow_snapshot_last_service_event_count =
        active_slot_ptr->event_count;
    g_runner.stream.telemetry.underflow_snapshot_last_service_word_count =
        active_slot_ptr->word_count;
    g_runner.stream.telemetry.underflow_snapshot_last_service_sample_count =
        active_slot_ptr->sample_count;
    g_runner.stream.telemetry.underflow_snapshot_queued_event_count = g_runner.stream.queued_event_count;
    g_runner.stream.telemetry.underflow_snapshot_reason = underflow_reason;
    const stpg_rate_stream_state_t *state = stpg_runner_stream_state();
    g_runner.stream.telemetry.underflow_snapshot_vector_index = state->vector_index;
    g_runner.stream.telemetry.underflow_snapshot_repeat_index = state->repeat_index;
    g_runner.stream.telemetry.underflow_snapshot_rate_id = state->current_vector.rate_id;
    g_runner.stream.telemetry.underflow_snapshot_period_ticks =
        state->rate_cached ? state->current_rate.period_ticks : 0u;
    g_runner.stream.telemetry.underflow_snapshot_prev_period_ticks = state->prev_period_ticks;
    g_runner.stream.telemetry.underflow_snapshot_desired_delta_ticks =
        state->have_prev_time ? state->prev_period_ticks : 0u;
    g_runner.stream.telemetry.underflow_snapshot_vector_repeat = state->current_vector.repeat;
    g_runner.stream.telemetry.underflow_snapshot_vector_flags = state->current_vector.flags;
    g_runner.stream.telemetry.underflow_snapshot_current_value = state->current_value;
    g_runner.stream.telemetry.underflow_snapshot_current_dir = state->current_dir;
    g_runner.stream.telemetry.underflow_snapshot_state_flags =
        (state->vector_loaded ? 1u : 0u)
        | (state->have_prev_time ? 2u : 0u)
        | (state->eof ? 4u : 0u)
        | (state->loop_active ? 8u : 0u);
}
static volatile uint32_t g_stream_core1_request_seen_raw;
static volatile uint32_t g_stream_core1_claim_raw;
static volatile uint32_t g_stream_core1_skip_stop_raw;
static volatile uint32_t g_stream_core1_skip_no_request_raw;
static volatile uint32_t g_stream_core1_last_seen_slot_raw;
static volatile uint32_t g_stream_core1_last_polled_slot_raw;
static volatile uint32_t g_stream_core1_negative_poll_count_raw;
static volatile uint32_t g_stream_core1_valid_poll_count_raw;
static volatile uint32_t g_stream_core1_invalid_poll_count_raw;
static volatile uint32_t g_stream_core1_last_polled_fill_error_raw;
static volatile uint32_t g_stream_core1_last_polled_slot0_ready_raw;
static volatile uint32_t g_stream_request_set_us_raw;
static volatile uint32_t g_stream_request_clear_us_raw;
static volatile uint32_t g_stream_core1_first_poll_us_raw;
static volatile uint32_t g_stream_core1_first_negative_poll_us_raw;
static volatile uint32_t g_stream_core1_first_valid_poll_us_raw;
static volatile uint32_t g_stream_core1_last_stage_raw;
static volatile uint32_t g_stream_core1_after_stop_gate_us_raw;
static volatile uint32_t g_stream_core1_after_dmb_us_raw;
static volatile uint32_t g_stream_core1_after_slot_read_us_raw;
static volatile uint32_t g_stream_startup_t0_us;
volatile uint32_t g_stream_core1_boot_stage_raw;
typedef struct {
    volatile uint32_t request;
    volatile uint32_t stage;
    volatile uint32_t value1;
    volatile uint32_t value2;
    volatile uint32_t done;
} stpg_visibility_probe_shared_t;

static volatile stpg_visibility_probe_shared_t g_visibility_probe_shared;
static volatile uint32_t g_visibility_probe_t0_us;
static volatile uint32_t g_visibility_probe_core1_boot_stage_raw;
typedef struct {
    volatile bool stop_requested;
    volatile int32_t fill_request_slot;
    volatile bool slot_ready;
    volatile stpg_err_t fill_error;
} stpg_handshake_probe_shared_t;

static volatile stpg_handshake_probe_shared_t g_handshake_probe_shared;
static volatile uint32_t g_handshake_probe_t0_us;
static volatile uint32_t g_handshake_probe_core1_boot_stage_raw;
typedef enum {
    STPG_HANDSHAKE_PROBE_MODE_SYNTHETIC = 0u,
    STPG_HANDSHAKE_PROBE_MODE_REAL_SHARED = 1u,
    STPG_HANDSHAKE_PROBE_MODE_REAL_SHARED_VOLATILE = 2u,
    STPG_HANDSHAKE_PROBE_MODE_REAL_CORE1_POLL_ONLY = 3u,
} stpg_handshake_probe_mode_t;

static volatile uint32_t g_handshake_probe_mode_raw;
static volatile uint32_t g_stream_core1_probe_mode_raw;
static volatile uint32_t g_runner_start_debug_stage_raw;
static volatile uint32_t g_runner_start_debug_run_flags_raw;
static volatile uint32_t g_runner_start_debug_arg1_raw;
static volatile uint32_t g_runner_start_debug_arg2_raw;
static volatile uint32_t g_runner_start_debug_arg3_raw;

/* Retain the one-time streaming CS1 admission and post-PIO mux evidence for
 * read-only SWD inspection.  UINT32_MAX means that the stage was not reached. */
volatile uint32_t g_stream_psram_cs1_gpio_raw = UINT32_MAX;
volatile uint32_t g_stream_psram_cs1_function_before_pio_raw = UINT32_MAX;
volatile uint32_t g_stream_psram_cs1_function_after_pio_raw = UINT32_MAX;
volatile uint32_t g_stream_psram_cs1_conflict_raw = 0u;
volatile uint32_t g_stream_psram_access_recheck_error_raw = UINT32_MAX;

static void __no_inline_not_in_flash_func(stpg_runner_dma_irq0)(void);
static void stpg_runner_p4_prearm_diagnostic_task(void);
static void stpg_runner_prepare_output_sm(bool preserve_gpio_state);
static void __no_inline_not_in_flash_func(stpg_runner_stream_swap_or_finish)(bool from_irq);
static void __no_inline_not_in_flash_func(stpg_runner_start_output_dma)(const uint32_t *words, uint32_t word_count);
static stpg_err_t __no_inline_not_in_flash_func(stpg_runner_start_sample_sched_dma)(const uint32_t *wait_words, uint32_t sample_count);
static uint64_t stpg_runner_compute_tail_ticks(stpg_pio_word_view_t view);
static void stpg_runner_clear_hold_state(void);
static void stpg_runner_capture_terminal_hold_state(void);
static void __no_inline_not_in_flash_func(stpg_runner_note_stream_progress)(void);
static void stpg_runner_request_stream_fail(stpg_err_t err, bool from_irq);
static void stpg_runner_fail_streaming(stpg_err_t err, bool from_irq) __attribute__((noinline));
static void __no_inline_not_in_flash_func(stpg_runner_record_dma_completion)(uint32_t path_flags,
                                                                               uint32_t completed_index,
                                                                               int32_t armed_index,
                                                                               int32_t next_ready,
                                                                               uint32_t restart_us);
static void __no_inline_not_in_flash_func(stpg_runner_try_main_loop_rearm_ready_slot)(void);
static bool __no_inline_not_in_flash_func(stpg_runner_try_main_loop_dma_fallback)(void);
static bool __no_inline_not_in_flash_func(stpg_runner_try_main_loop_finite_completion)(void);
static stpg_err_t stpg_runner_check_fail_stop_samples(uint32_t *out_fail_event_index);
static void stpg_runner_finish_direct_error(stpg_err_t err, uint32_t progress);
static stpg_err_t __no_inline_not_in_flash_func(stpg_runner_request_stream_fill_safe)(uint32_t slot_index);
#if STPG_STREAM_FAIL_STOP
static stpg_err_t stpg_runner_check_stream_fail_stop_samples(uint32_t *out_fail_event_index);
static void stpg_runner_finish_streaming_fail_stop(stpg_err_t err, uint32_t progress);
#endif

static PIO stpg_runner_pio(void) {
    return pio0;
}

static uint stpg_runner_sm(void) {
    return 0u;
}

static uint stpg_sample_sm(void) {
    return 1u;
}

static uint32_t __no_inline_not_in_flash_func(stpg_runner_output_dma_busy_snapshot)(void) {
    if (g_runner.output_dma_channel < 0) {
        /* An invalid channel is unsafe for a deferred handoff. */
        return 1u;
    }
    return dma_channel_is_busy((uint)g_runner.output_dma_channel) ? 1u : 0u;
}

static uint32_t __no_inline_not_in_flash_func(stpg_runner_output_dma_remaining_snapshot)(void) {
    if (g_runner.output_dma_channel < 0) {
        /* Treat an invalid channel as non-quiescent and fail closed. */
        return 1u;
    }
    return dma_hw->ch[(uint)g_runner.output_dma_channel].transfer_count;
}

static uint32_t __no_inline_not_in_flash_func(stpg_runner_pio_output_txempty_snapshot)(void) {
    return (stpg_runner_pio()->fstat >> (PIO_FSTAT_TXEMPTY_LSB + stpg_runner_sm())) & 0x1u;
}

static void stpg_runner_neutralize_active_output_dma_chain(void);

static bool __no_inline_not_in_flash_func(stpg_runner_try_defer_production_noready)(uint32_t completed_index, bool from_irq) {
    /*
     * The output-DMA IRQ is the completion owner.  Its acknowledged IRQ is
     * the completion token, so the transient BUSY/remaining values and the
     * PIO TXEMPTY state are recorded for diagnosis but do not veto the IRQ
     * handoff.  The main-loop fallback has no completion token and therefore
     * may defer only after it observes an idle, fully-counted-down DMA while
     * chaining is already disabled.
     */
    const uint32_t prod_dma_busy = stpg_runner_output_dma_busy_snapshot();
    const uint32_t prod_dma_remaining = stpg_runner_output_dma_remaining_snapshot();
    const uint32_t prod_pio_txempty = stpg_runner_pio_output_txempty_snapshot();
    const uint32_t safety_flags =
        (prod_dma_busy ? 0x1u : 0u) |
        (prod_dma_remaining ? 0x2u : 0u) |
        (prod_pio_txempty ? 0u : 0x4u);
    g_runner.stream.telemetry.prod_noready_defer_dma_busy = prod_dma_busy;
    g_runner.stream.telemetry.prod_noready_defer_dma_remaining = prod_dma_remaining;
    g_runner.stream.telemetry.prod_noready_defer_pio_txempty = prod_pio_txempty;
    g_runner.stream.telemetry.prod_noready_defer_completed_slot = completed_index + 1u;
    g_runner.stream.telemetry.prod_noready_defer_queued_events = g_runner.stream.queued_event_count;
    g_runner.stream.telemetry.prod_noready_defer_safety_flags = safety_flags;
    if (g_runner.pending_noready_prod_recheck) {
        if (from_irq) {
            g_runner.stream.telemetry.p3_handoff_conflict_count += 1u;
            g_runner.stream.telemetry.p3_telemetry_sequence =
                g_runner.stream.telemetry.p3_stream_run_sequence;
            stpg_runner_request_stream_fail(ERR_BAD_STATE, true);
        }
        return true;
    }
    if (!from_irq && (g_runner.output_dma_chain_enabled
                      || prod_dma_busy != 0u
                      || prod_dma_remaining != 0u)) {
        g_runner.stream.telemetry.prod_noready_safety_reject_count += 1u;
        return false;
    }

    if (from_irq && g_runner.output_dma_chain_enabled) {
        stpg_runner_neutralize_active_output_dma_chain();
    }
    g_runner.stream.telemetry.prod_noready_defer_count += 1u;
    g_runner.pending_noready_prod_recheck = true;
    g_runner.pending_noready_prod_completed_slot = completed_index;
    g_runner.pending_noready_prod_recheck_start_us = time_us_32();
    g_runner.pending_noready_prod_recheck_iterations = 0u;
    g_runner.stream.telemetry.p3_handoff_owner =
        STPG_OUTPUT_HANDOFF_OWNER_MAIN_LOOP;
    return true;
}

static uint32_t *stpg_runner_stream_slot_words(uint32_t slot_index) {
    uint32_t *scratch = stpg_buffer_streaming_scratch_words();
    return &scratch[slot_index * STPG_STREAM_SLOT_WORD_CAPACITY];
}

static uint32_t *stpg_runner_stream_sample_wait_buffer(uint32_t slot_index) {
    return &stpg_runner_stream_slot_words(slot_index)[STPG_STREAM_BUFFER_EVENTS * 3u];
}

static bool __no_inline_not_in_flash_func(stpg_runner_stream_slot_transition_allowed)(
    stpg_stream_slot_state_t current,
    stpg_stream_slot_state_t next
) {
    switch (current) {
        case STPG_STREAM_SLOT_EMPTY:
            return next == STPG_STREAM_SLOT_FILLING;
        case STPG_STREAM_SLOT_FILLING:
            return next == STPG_STREAM_SLOT_READY;
        case STPG_STREAM_SLOT_READY:
            return next == STPG_STREAM_SLOT_ARMED || next == STPG_STREAM_SLOT_ACTIVE;
        case STPG_STREAM_SLOT_ARMED:
            return next == STPG_STREAM_SLOT_ACTIVE;
        case STPG_STREAM_SLOT_ACTIVE:
            return next == STPG_STREAM_SLOT_EMPTY;
        default:
            return false;
    }
}

static bool __no_inline_not_in_flash_func(stpg_runner_stream_slot_transition)(
    stpg_stream_slot_t *slot,
    stpg_stream_slot_state_t next
) {
    if (!stpg_runner_stream_slot_transition_allowed(slot->state, next)) {
        return false;
    }
    slot->state = next;
    return true;
}

static uint32_t __no_inline_not_in_flash_func(stpg_runner_stream_ready_slot_count)(void) {
    uint32_t count = 0u;
    for (uint32_t index = 0u; index < STPG_STREAM_SLOT_COUNT; ++index) {
        if (g_runner.stream.slots[index].state == STPG_STREAM_SLOT_READY) {
            count += 1u;
        }
    }
    return count;
}

static void __no_inline_not_in_flash_func(stpg_runner_stream_update_min_ready_slots)(void) {
    const uint32_t ready = stpg_runner_stream_ready_slot_count();
    if (ready < g_runner.stream.telemetry.min_ready_slots_observed) {
        g_runner.stream.telemetry.min_ready_slots_observed = ready;
    }
}

static int32_t __no_inline_not_in_flash_func(stpg_runner_stream_find_next_ready_slot)(uint32_t after_index) {
    for (uint32_t step = 1u; step <= STPG_STREAM_SLOT_COUNT; ++step) {
        const uint32_t candidate = (after_index + step) % STPG_STREAM_SLOT_COUNT;
        if (g_runner.stream.slots[candidate].state == STPG_STREAM_SLOT_READY) {
            return (int32_t)candidate;
        }
    }
    return -1;
}

static void stpg_runner_stream_init_slots(void) {
    for (uint32_t index = 0u; index < STPG_STREAM_SLOT_COUNT; ++index) {
        g_runner.stream.slots[index].words = stpg_runner_stream_slot_words(index);
        g_runner.stream.slots[index].event_count = 0u;
        g_runner.stream.slots[index].word_count = 0u;
        g_runner.stream.slots[index].sample_count = 0u;
        g_runner.stream.slots[index].stream_event_start = 0u;
        g_runner.stream.slots[index].stream_event_end = 0u;
        g_runner.stream.slots[index].p4_explicit_wait_end_vector_index = 0u;
        g_runner.stream.slots[index].p4_explicit_wait_end_present = false;
        g_runner.stream.slots[index].eof = false;
        g_runner.stream.slots[index].state = STPG_STREAM_SLOT_EMPTY;
        stpg_fail_stop_metadata_init(
            &g_runner.stream.slots[index].fail_stop_metadata,
            g_stpg_runner_stream_run_sequence);
    }
}

static void stpg_runner_clear_hold_state(void) {
    g_runner.segment_end_hold = false;
    g_runner.hold_awaiting_next = false;
    g_runner.hold_valid = false;
    g_runner.hold_drive_value = 0u;
    g_runner.hold_drive_enable = 0u;
}

static void stpg_runner_reset_fail_stop_context(void) {
    g_runner.fail_stop_enabled = false;
    g_runner.fail_stop_limit = 0u;
    g_runner.fail_stop_terminalized = true;
    g_runner.fail_stop_next_event_index = 0u;
    g_runner.fail_stop_next_sample_index = 0u;
}

static void stpg_runner_capture_terminal_hold_state(void) {
    if (g_runner.streaming) {
        const stpg_rate_stream_state_t *state = stpg_runner_stream_state();
        const uint32_t active_pins = stpg_buffer_active_pins();
        const uint32_t mask = active_pins >= 32u ? 0xFFFFFFFFu : ((1u << active_pins) - 1u);
        g_runner.hold_drive_value = state->current_value & mask;
        g_runner.hold_drive_enable = state->current_dir & mask;
        g_runner.hold_valid = true;
        g_runner.hold_awaiting_next = true;
        return;
    }
    const ExecEvent *events = stpg_buffer_events();
    const uint32_t active_pins = stpg_buffer_active_pins();
    const uint32_t mask = active_pins >= 32u ? 0xFFFFFFFFu : ((1u << active_pins) - 1u);
    if (events == 0 || g_runner.event_count == 0u) {
        g_runner.hold_drive_value = 0u;
        g_runner.hold_drive_enable = 0u;
    } else {
        const ExecEvent *last = &events[g_runner.event_count - 1u];
        g_runner.hold_drive_value = last->out_value & mask;
        g_runner.hold_drive_enable = last->out_enable & mask;
    }
    g_runner.hold_valid = true;
    g_runner.hold_awaiting_next = true;
}

static void stpg_runner_set_pio_pins(void) {
    const uint32_t active_pins = stpg_buffer_active_pins();
    /* The 24-pin PIO word spans GPIO2..28, but GPIO19/24/25 are packing gaps.
     * Initializing the entire consecutive span steals GPIO19 from XIP_CS1
     * after the ring startup prefill. Configure only logical DUT pins;
     * PIO out/pindirs writes to gap positions remain disconnected by mux. */
    for (uint32_t logical_bit = 0u; logical_bit < active_pins; ++logical_bit) {
        const uint32_t gpio = stpg_board_gpio_for_dut_pin(logical_bit);
        gpio_disable_pulls(gpio);
        pio_gpio_init(stpg_runner_pio(), gpio);
        gpio_set_input_enabled(gpio, true);
    }
}

static void stpg_runner_reset_psram_mux_evidence(void) {
    g_stream_psram_cs1_gpio_raw = UINT32_MAX;
    g_stream_psram_cs1_function_before_pio_raw = UINT32_MAX;
    g_stream_psram_cs1_function_after_pio_raw = UINT32_MAX;
    g_stream_psram_cs1_conflict_raw = 0u;
    g_stream_psram_access_recheck_error_raw = UINT32_MAX;
}

static stpg_err_t stpg_runner_admit_streaming_psram_mux(void) {
    if (stpg_buffer_active_source() != STPG_PATTERN_SOURCE_PSRAM) {
        return ERR_NONE;
    }
    const stpg_err_t err = stpg_psram_prepare_access();
    if (err != ERR_NONE) {
        g_stream_psram_access_recheck_error_raw = (uint32_t)err;
        return err;
    }
    uint32_t cs1_gpio = UINT32_MAX;
    if (!stpg_psram_get_selected_cs1_gpio(&cs1_gpio)) {
        g_stream_psram_access_recheck_error_raw = (uint32_t)ERR_PSRAM_UNAVAILABLE;
        return ERR_PSRAM_UNAVAILABLE;
    }
    g_stream_psram_cs1_gpio_raw = cs1_gpio;
    g_stream_psram_cs1_function_before_pio_raw =
        stpg_psram_get_selected_cs1_function();
    if (stpg_board_gpio_is_dut_pin(cs1_gpio, stpg_buffer_active_pins())) {
        g_stream_psram_cs1_conflict_raw = 1u;
        g_stream_psram_access_recheck_error_raw = (uint32_t)ERR_UNSUPPORTED;
        return ERR_UNSUPPORTED;
    }
    return ERR_NONE;
}

static stpg_err_t stpg_runner_recheck_streaming_psram_mux(void) {
    if (stpg_buffer_active_source() != STPG_PATTERN_SOURCE_PSRAM) {
        return ERR_NONE;
    }
    g_stream_psram_cs1_function_after_pio_raw =
        stpg_psram_get_selected_cs1_function();
    if (!stpg_psram_selected_cs1_mux_matches()) {
        g_stream_psram_access_recheck_error_raw = (uint32_t)ERR_PSRAM_UNAVAILABLE;
        return ERR_PSRAM_UNAVAILABLE;
    }
    const stpg_err_t err = stpg_psram_prepare_access();
    g_stream_psram_access_recheck_error_raw = (uint32_t)err;
    return err;
}

static uint32_t stpg_runner_claimed_dma_mask(void) {
    uint32_t mask = 0u;
    if (g_runner.output_dma_channel >= 0) {
        mask |= 1u << (uint)g_runner.output_dma_channel;
    }
    if (g_runner.output_dma_spare_channel >= 0) {
        mask |= 1u << (uint)g_runner.output_dma_spare_channel;
    }
    if (g_runner.sample_dma_channel >= 0) {
        mask |= 1u << (uint)g_runner.sample_dma_channel;
    }
    if (g_runner.sample_sched_dma_channel >= 0) {
        mask |= 1u << (uint)g_runner.sample_sched_dma_channel;
    }
    return mask;
}

static void stpg_runner_disable_dma_channel_before_abort(int channel) {
    if (channel < 0) {
        return;
    }
    hw_clear_bits(
        &dma_hw->ch[(uint)channel].ctrl_trig,
        DMA_CH0_CTRL_TRIG_EN_BITS
    );
}

static void stpg_runner_abort_dma(void) {
    const uint32_t total_start_us = time_us_32();
    const uint32_t claimed_mask = stpg_runner_claimed_dma_mask();
    const uint32_t irq0_enabled_mask = dma_hw->inte0 & claimed_mask;

    /* RP2350-E5: an aborted channel can be retriggered while its EN bit, or
     * the EN bit of a channel chained to it, remains set.  Disable every
     * runner-owned IRQ source and channel before issuing the first abort.
     * This is especially important for direct fail-stop, which intentionally
     * terminates while the output DMA still has events queued. */
    dma_set_irq0_channel_mask_enabled(claimed_mask, false);
    stpg_runner_disable_dma_channel_before_abort(g_runner.output_dma_channel);
    stpg_runner_disable_dma_channel_before_abort(g_runner.output_dma_spare_channel);
    stpg_runner_disable_dma_channel_before_abort(g_runner.sample_dma_channel);
    stpg_runner_disable_dma_channel_before_abort(g_runner.sample_sched_dma_channel);
    __dmb();

    if (g_runner.output_dma_channel >= 0) {
        const uint32_t start_us = time_us_32();
        dma_channel_abort((uint)g_runner.output_dma_channel);
        g_runner.stream.telemetry.dma_abort_output_us = time_us_32() - start_us;
        dma_channel_acknowledge_irq0((uint)g_runner.output_dma_channel);
    } else {
        g_runner.stream.telemetry.dma_abort_output_us = 0u;
    }
    if (g_runner.output_dma_spare_channel >= 0) {
        dma_channel_abort((uint)g_runner.output_dma_spare_channel);
        dma_channel_acknowledge_irq0((uint)g_runner.output_dma_spare_channel);
    }
    if (g_runner.sample_dma_channel >= 0) {
        const uint32_t start_us = time_us_32();
        dma_channel_abort((uint)g_runner.sample_dma_channel);
        g_runner.stream.telemetry.dma_abort_sample_us = time_us_32() - start_us;
    } else {
        g_runner.stream.telemetry.dma_abort_sample_us = 0u;
    }
    if (g_runner.sample_sched_dma_channel >= 0) {
        const uint32_t start_us = time_us_32();
        dma_channel_abort((uint)g_runner.sample_sched_dma_channel);
        g_runner.stream.telemetry.dma_abort_sample_sched_us = time_us_32() - start_us;
    } else {
        g_runner.stream.telemetry.dma_abort_sample_sched_us = 0u;
    }
    dma_set_irq0_channel_mask_enabled(irq0_enabled_mask, true);
    g_runner.stream.telemetry.dma_abort_total_us = time_us_32() - total_start_us;
}

static void stpg_runner_finish_hw_hold_outputs(void) {
    const PIO pio = stpg_runner_pio();
    const uint out_sm = stpg_runner_sm();
    const uint sample_sm = stpg_sample_sm();

    pio_sm_set_enabled(pio, out_sm, false);
    pio_sm_set_enabled(pio, sample_sm, false);
    stpg_runner_abort_dma();
    pio_sm_clear_fifos(pio, out_sm);
    pio_sm_clear_fifos(pio, sample_sm);
    pio_interrupt_clear(pio, 4);
}

static stpg_err_t stpg_runner_start_prepared(bool preserve_gpio_state,
                                             bool fail_stop,
                                             uint32_t fail_stop_limit) {
    stpg_runner_reset_fail_stop_context();
    const stpg_pio_word_view_t source = stpg_buffer_pio_word_view();
    if (source.word_count == 0u || source.words == 0) {
        return ERR_PATTERN_INVALID;
    }
    if (preserve_gpio_state && source.sample_event_count != 0u) {
        return ERR_UNSUPPORTED;
    }

    const PIO pio = stpg_runner_pio();
    const uint sample_sm = stpg_sample_sm();
    g_runner.reduced_density_streaming = false;
    stpg_runner_prepare_output_sm(false);

    dma_channel_config sample_dma_cfg = dma_channel_get_default_config((uint)g_runner.sample_dma_channel);
    channel_config_set_transfer_data_size(&sample_dma_cfg, DMA_SIZE_32);
    channel_config_set_read_increment(&sample_dma_cfg, false);
    channel_config_set_write_increment(&sample_dma_cfg, true);
    channel_config_set_dreq(&sample_dma_cfg, pio_get_dreq(pio, sample_sm, false));

    stpg_err_t sample_err = stpg_samples_prepare_for_run(source.sample_event_count, stpg_buffer_active_pins());
    if (sample_err != ERR_NONE) {
        return sample_err;
    }
    dma_channel_acknowledge_irq0((uint)g_runner.output_dma_channel);
    pio_interrupt_clear(pio, 4);

    g_runner.active = true;
    g_runner.draining = false;
    g_runner.streaming = false;
    g_runner.fail_stop_enabled = fail_stop;
    g_runner.fail_stop_limit = fail_stop_limit;
    g_runner.fail_stop_terminalized = false;
    g_runner.fail_stop_next_event_index = 0u;
    g_runner.fail_stop_next_sample_index = 0u;
    g_runner.hold_awaiting_next = false;
    if (preserve_gpio_state) {
        g_runner.hold_valid = false;
    }
    g_runner.event_count = source.event_count;
    g_runner.sample_event_count = source.sample_event_count;
    g_runner.tail_drain_ticks = stpg_runner_compute_tail_ticks(source);
    g_runner.drain_deadline_us = 0u;
    stpg_status_set_run_progress(0u);
    stpg_status_set_result_flags(0u);
    stpg_status_set_fail_count(0u);
    stpg_status_set_sample_count(0u);
    stpg_compare_reset();
    stpg_status_clear_error();

    if (source.sample_event_count > 0u) {
        channel_config_set_ring(&sample_dma_cfg, true, stpg_samples_dma_ring_bits());
        dma_channel_configure((uint)g_runner.sample_dma_channel, &sample_dma_cfg,
                              stpg_samples_dma_buffer(),
                              &pio->rxf[sample_sm],
                              source.sample_event_count,
                              false);
        pio_sm_set_enabled(pio, sample_sm, true);
        dma_start_channel_mask(1u << (uint)g_runner.sample_dma_channel);
    }

    stpg_runner_start_output_dma(source.words, source.word_count);
    return ERR_NONE;
}

static void stpg_runner_disable_hw_safe(void) {
    const PIO pio = stpg_runner_pio();
    const uint out_sm = stpg_runner_sm();
    const uint sample_sm = stpg_sample_sm();

    pio_sm_set_enabled(pio, out_sm, false);
    pio_sm_set_enabled(pio, sample_sm, false);
    stpg_runner_abort_dma();
    pio_sm_clear_fifos(pio, out_sm);
    pio_sm_clear_fifos(pio, sample_sm);
    pio_sm_restart(pio, out_sm);
    pio_sm_restart(pio, sample_sm);
    pio_interrupt_clear(pio, 4);
    stpg_board_init_safe_gpio();
    stpg_runner_clear_hold_state();
}

static stpg_err_t stpg_runner_abort_stream_startup(stpg_err_t err) {
    g_runner.stream.shared.stop_requested = true;
    g_runner.stream.shared.fill_request_slot = -1;
    for (uint32_t slot_index = 0u; slot_index < STPG_STREAM_SLOT_COUNT; ++slot_index) {
        g_runner.stream.shared.fill_request_pending[slot_index] = false;
    }
    __sev();
    stpg_runner_disable_hw_safe();
    g_runner.active = false;
    g_runner.draining = false;
    g_runner.streaming = false;
    g_runner.reduced_density_streaming = false;
    g_runner.stream.active = false;
    stpg_runner_reset_fail_stop_context();
    g_runner.drain_deadline_us = 0u;
    g_runner.sample_event_count = 0u;
    g_runner.pending_noready_prod_recheck = false;
    stpg_status_set_error(err);
    return err;
}

static uint32_t stpg_runner_captured_sample_count(void) {
    if (g_runner.sample_event_count == 0u || g_runner.sample_dma_channel < 0) {
        return 0u;
    }
    uint32_t remaining;
    do {
        remaining = dma_hw->ch[g_runner.sample_dma_channel].transfer_count;
        __dmb();
    } while (remaining != dma_hw->ch[g_runner.sample_dma_channel].transfer_count);
    __dmb();
    return remaining > g_runner.sample_event_count
        ? 0u : g_runner.sample_event_count - remaining;
}

/*
 * The sample DMA is the first point at which a sampled DUT value is stable
 * for firmware inspection.  This is deliberately a best-effort observation,
 * not an edge-synchronous compare path: the main loop can observe one or more
 * already-emitted samples after the physical mismatch.  Streaming runs use a
 * separate slot-owned metadata path below; this routine
 * remains the materialized-run implementation.
 */
static stpg_err_t stpg_runner_check_fail_stop_samples(uint32_t *out_fail_event_index) {
    if (out_fail_event_index != 0) {
        *out_fail_event_index = 0u;
    }
    if (!g_runner.fail_stop_enabled) {
        return ERR_NONE;
    }

    const ExecEvent *events = stpg_buffer_events();
    if (g_runner.event_count != 0u && events == 0) {
        return ERR_PATTERN_INVALID;
    }

    const uint32_t captured_sample_count = stpg_runner_captured_sample_count();
    uint32_t event_index = g_runner.fail_stop_next_event_index;
    uint32_t sample_index = g_runner.fail_stop_next_sample_index;
    const uint32_t logical_mask = stpg_samples_active_pins() == STPG_ACTIVE_PINS_V11
        ? 0x00FFFFFFu : 0x0000FFFFu;

    while (event_index < g_runner.event_count && sample_index < captured_sample_count) {
        const ExecEvent *event = &events[event_index];
        if ((event->flags & STPG_EVF_SAMPLE_POINT) != 0u) {
            const uint32_t actual = stpg_samples_value_at(sample_index);
            if ((event->flags & STPG_EVF_COMPARE_POINT) != 0u
                && stpg_compare_check_value(
                    event_index,
                    actual,
                    event->expect_value & logical_mask,
                    event->sample_mask & logical_mask
                ) && g_runner.fail_stop_limit != 0u
                    && stpg_compare_fail_count() >= g_runner.fail_stop_limit) {
                g_runner.fail_stop_next_event_index = event_index + 1u;
                g_runner.fail_stop_next_sample_index = sample_index + 1u;
                if (out_fail_event_index != 0) {
                    *out_fail_event_index = event_index;
                }
                return ERR_COMPARE_FAIL_STOP;
            }
            sample_index += 1u;
        }
        event_index += 1u;
    }

    g_runner.fail_stop_next_event_index = event_index;
    g_runner.fail_stop_next_sample_index = sample_index;
    if (sample_index < captured_sample_count) {
        return ERR_BAD_SIZE;
    }
    return ERR_NONE;
}

static void stpg_runner_finish_direct_error(stpg_err_t err, uint32_t progress) {
    const uint32_t captured_sample_count = stpg_runner_captured_sample_count();
    stpg_runner_disable_hw_safe();
    stpg_samples_finish_run(captured_sample_count, false);
    g_runner.active = false;
    g_runner.draining = false;
    g_runner.streaming = false;
    g_runner.reduced_density_streaming = false;
    stpg_runner_reset_fail_stop_context();
    g_runner.drain_deadline_us = 0u;
    g_runner.sample_event_count = 0u;
    stpg_status_set_run_progress(progress);
    stpg_status_set_result_flags(stpg_samples_result_flags() | stpg_compare_result_flags());
    stpg_status_set_fail_count(stpg_compare_fail_count());
    stpg_status_set_sample_count(stpg_samples_sample_count());
    stpg_status_set_error(err);
}

static void __no_inline_not_in_flash_func(stpg_runner_note_stream_progress)(void) {
    /* The stall window is far below the 32-bit microsecond wrap interval. */
    g_runner.stream_last_progress_us = time_us_32();
    g_runner.stream_timed_wait_seen = false;
    g_runner.stream_last_slot_index = g_runner.stream.active_slot_index;
    g_runner.stream_last_queued_event_count = g_runner.stream.queued_event_count;
    if (g_runner.output_dma_channel >= 0) {
        g_runner.stream_last_dma_remaining = dma_hw->ch[g_runner.output_dma_channel].transfer_count;
    } else {
        g_runner.stream_last_dma_remaining = 0u;
    }
}

/* Cold path only: check the active transfer and SM together. The IRQ may
 * have handed off the slot after the caller's first progress observation. */
static bool stpg_runner_stream_stall_expired(void) {
    const uint32_t irq_state = save_and_disable_interrupts();
    bool expired = false;
    if (g_runner.active && g_runner.streaming && !g_runner.draining
        && g_runner.output_dma_channel >= 0 && g_runner.pending_stream_fail_error == ERR_NONE
        && !g_runner.pending_noready_prod_recheck) {
        const uint ch = (uint)g_runner.output_dma_channel;
        const uint32_t remaining = dma_hw->ch[ch].transfer_count;
        const uint32_t now_us = time_us_32();
        if (remaining == g_runner.stream_last_dma_remaining
            && g_runner.stream.active_slot_index == g_runner.stream_last_slot_index
            && g_runner.stream.queued_event_count == g_runner.stream_last_queued_event_count
            && (uint32_t)(now_us - g_runner.stream_last_progress_us) > STPG_STREAM_STALL_TIMEOUT_US) {
            const bool in_flight = remaining != 0u && dma_channel_is_busy(ch);
            bool in_timer_loop = false;
            if (in_flight && !g_runner.stream_timed_wait_seen
                && g_runner.stream_wait_budget_us > STPG_STREAM_STALL_TIMEOUT_US) {
                const bool pins24 = stpg_buffer_active_pins() == STPG_ACTIVE_PINS_V11;
                const bool reduced = g_runner.reduced_density_streaming;
                const uint wait_pc = pins24
                    ? (reduced ? g_runner.output27_reduced_pio_offset + stpg_runner27_reduced_offset_wait_loop
                               : g_runner.output_pio27_offset + stpg_runner27_offset_wait_loop)
                    : (reduced ? g_runner.output_reduced_pio_offset + stpg_runner_reduced_offset_wait_loop
                               : g_runner.output_pio_offset + stpg_runner_offset_wait_loop);
                const PIO pio = stpg_runner_pio();
                const uint sm = stpg_runner_sm();
                in_timer_loop = (pio->ctrl & (1u << sm)) != 0u && pio_sm_get_pc(pio, sm) == wait_pc;
            }
            /* DMA itself can progress with IRQs masked. Do not latch a
             * timer allowance or fault for an observation that moved. */
            if (dma_hw->ch[ch].transfer_count == remaining) {
                expired = stpg_stream_watchdog_expired(now_us, g_runner.stream_last_progress_us,
                    g_runner.stream_wait_budget_us, in_flight, in_timer_loop, &g_runner.stream_timed_wait_seen);
            }
        }
    }
    restore_interrupts(irq_state);
    return expired;
}

static uint64_t stpg_runner_compute_tail_ticks_from_words(const uint32_t *words, uint32_t event_count) {
    if (words == 0 || event_count == 0u) {
        return 0u;
    }
    const uint32_t stride = g_runner.reduced_density_streaming ? 3u : 4u;
    const uint32_t last_wait = words[(event_count - 1u) * stride];
    return (uint64_t)last_wait + 32u;
}

static uint64_t stpg_runner_compute_tail_ticks(stpg_pio_word_view_t view) {
    return stpg_runner_compute_tail_ticks_from_words(view.words, view.event_count);
}

static void stpg_runner_begin_draining(uint64_t tail_drain_ticks) {
    const uint64_t delay_us =
        (tail_drain_ticks * 1000000ull + (uint64_t)STPG_TICK_HZ - 1ull) / (uint64_t)STPG_TICK_HZ;
    g_runner.tail_drain_ticks = tail_drain_ticks;
    g_runner.draining = true;
    g_runner.drain_deadline_us = time_us_64() + delay_us + 1u;
    stpg_status_set_run_progress(g_runner.event_count);
    stpg_status_set_state(STATE_RUN_DRAINING);
}

static void stpg_stream_shared_reset(void) {
    memset(&g_runner.stream.shared, 0, sizeof(g_runner.stream.shared));
    g_runner.stream.shared.fill_request_slot = -1;
}

static int32_t STPG_STREAM_PRODUCER_FUNC(stpg_stream_find_pending_fill_request)(void) {
#if STPG_STREAM_FIFO_FILL_REQUESTS
    const uint32_t head = g_runner.stream.shared.fill_request_queue_head;
    const uint32_t tail = g_runner.stream.shared.fill_request_queue_tail;
    if (head == tail) {
        return -1;
    }
    const uint32_t slot_index =
        g_runner.stream.shared.fill_request_queue[tail % STPG_STREAM_SLOT_COUNT];
    return slot_index < STPG_STREAM_SLOT_COUNT ? (int32_t)slot_index : -1;
#else
    for (uint32_t slot_index = 0u; slot_index < STPG_STREAM_SLOT_COUNT; ++slot_index) {
        if (g_runner.stream.shared.fill_request_pending[slot_index]) {
            return (int32_t)slot_index;
        }
    }
    return -1;
#endif
}

static stpg_err_t __no_inline_not_in_flash_func(stpg_stream_find_fill_error)(void) {
    for (uint32_t slot_index = 0u; slot_index < STPG_STREAM_SLOT_COUNT; ++slot_index) {
        const stpg_err_t err = g_runner.stream.shared.slot_fill_error[slot_index];
        if (err != ERR_NONE) {
            return err;
        }
    }
    return ERR_NONE;
}

static void stpg_stream_telemetry_reset(void) {
    memset(&g_runner.stream.telemetry, 0, sizeof(g_runner.stream.telemetry));
    g_runner.stream.telemetry.queue_slot_count = STPG_STREAM_SLOT_COUNT;
    g_runner.stream.telemetry.slot_event_capacity = STPG_STREAM_BUFFER_EVENTS;
    g_runner.stream.telemetry.min_ready_slots_observed = STPG_STREAM_SLOT_COUNT;
}

static inline uint32_t stpg_stream_elapsed_since_startup_us(void) {
    const uint32_t start_us = g_stream_startup_t0_us;
    if (start_us == 0u) {
        return 0u;
    }
    return time_us_32() - start_us;
}

#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
static volatile stpg_refill_trace_t *stpg_refill_trace_for_slot(
    const stpg_stream_slot_t *slot
) {
    const uint32_t index = slot->refill_trace_index;
    if (index >= STPG_REFILL_TRACE_CAPACITY
        || index >= g_runner.stream.telemetry.refill_trace_count) {
        return 0;
    }
    return &g_runner.stream.telemetry.refill_trace[index];
}

static void stpg_refill_trace_begin(uint32_t slot_index) {
    stpg_stream_slot_t *slot = &g_runner.stream.slots[slot_index];
    slot->refill_trace_index = UINT32_MAX;
    const uint32_t count = g_runner.stream.telemetry.refill_trace_count;
    if (count >= STPG_REFILL_TRACE_CAPACITY) {
        g_runner.stream.telemetry.refill_trace_overflow += 1u;
        return;
    }
    volatile stpg_refill_trace_t *entry =
        &g_runner.stream.telemetry.refill_trace[count];
    memset((void *)entry, 0, sizeof(*entry));
    entry->sequence = count + 1u;
    entry->slot_index = slot_index;
    entry->request_us = stpg_stream_elapsed_since_startup_us() + 1u;
    slot->refill_trace_index = count;
    __dmb();
    g_runner.stream.telemetry.refill_trace_count = count + 1u;
}

static void stpg_refill_trace_mark_claim(uint32_t slot_index) {
    volatile stpg_refill_trace_t *entry =
        stpg_refill_trace_for_slot(&g_runner.stream.slots[slot_index]);
    if (entry != 0) {
        entry->claim_us = stpg_stream_elapsed_since_startup_us() + 1u;
    }
}

static void stpg_refill_trace_mark_body_start(uint32_t slot_index) {
    volatile stpg_refill_trace_t *entry =
        stpg_refill_trace_for_slot(&g_runner.stream.slots[slot_index]);
    if (entry != 0) {
        entry->body_start_us = stpg_stream_elapsed_since_startup_us() + 1u;
    }
}

static void stpg_refill_trace_mark_body_end(uint32_t slot_index, stpg_err_t error) {
    volatile stpg_refill_trace_t *entry =
        stpg_refill_trace_for_slot(&g_runner.stream.slots[slot_index]);
    if (entry != 0) {
        entry->body_end_us = stpg_stream_elapsed_since_startup_us() + 1u;
        entry->fill_error = (uint32_t)error;
    }
}

static void stpg_refill_trace_mark_ready(uint32_t slot_index) {
    volatile stpg_refill_trace_t *entry =
        stpg_refill_trace_for_slot(&g_runner.stream.slots[slot_index]);
    if (entry != 0) {
        entry->ready_us = stpg_stream_elapsed_since_startup_us() + 1u;
    }
}

static void stpg_refill_trace_mark_promoted(stpg_stream_slot_t *slot) {
    volatile stpg_refill_trace_t *entry = stpg_refill_trace_for_slot(slot);
    if (entry != 0) {
        entry->event_start = slot->stream_event_start;
        entry->event_end = slot->stream_event_end;
        entry->promote_us = stpg_stream_elapsed_since_startup_us() + 1u;
    }
}

static void stpg_refill_trace_mark_armed(stpg_stream_slot_t *slot) {
    volatile stpg_refill_trace_t *entry = stpg_refill_trace_for_slot(slot);
    if (entry != 0) {
        entry->event_start = slot->stream_event_start;
        entry->event_end = slot->stream_event_end;
        entry->arm_us = stpg_stream_elapsed_since_startup_us() + 1u;
    }
}
#endif

static inline uint32_t stpg_visibility_elapsed_us(void) {
    const uint32_t start_us = g_visibility_probe_t0_us;
    if (start_us == 0u) {
        return 0u;
    }
    return time_us_32() - start_us;
}

static inline uint32_t stpg_handshake_elapsed_us(void) {
    const uint32_t start_us = g_handshake_probe_t0_us;
    if (start_us == 0u) {
        return 0u;
    }
    return time_us_32() - start_us;
}


static void stpg_stream_reset(void) {
    memset((void *)g_chained_irq_reject, 0, sizeof(g_chained_irq_reject));
    memset(&g_runner.stream, 0, sizeof(g_runner.stream));
    stpg_runner_stream_init_slots();
    stpg_stream_shared_reset();
    stpg_stream_telemetry_reset();
}

#ifdef PICO_ON_DEVICE
static void stpg_visibility_probe_core1_entry(void) {
    g_visibility_probe_core1_boot_stage_raw = 1u;
    flash_safe_execute_core_init();
    g_visibility_probe_core1_boot_stage_raw = 2u;
    while (g_visibility_probe_shared.request == 0u) {
        tight_loop_contents();
    }
    g_runner.stream.telemetry.visibility_probe_core1_saw_request_us = stpg_visibility_elapsed_us() + 1u;
    g_visibility_probe_core1_boot_stage_raw = 3u;
    g_visibility_probe_shared.stage = 1u;
    g_visibility_probe_shared.value1 = STPG_VISIBILITY_VALUE1;
    g_runner.stream.telemetry.visibility_probe_core1_write1_us = stpg_visibility_elapsed_us() + 1u;
    __dmb();
    g_visibility_probe_shared.stage = 2u;
    g_visibility_probe_shared.value2 = STPG_VISIBILITY_VALUE2;
    g_runner.stream.telemetry.visibility_probe_core1_write2_us = stpg_visibility_elapsed_us() + 1u;
    __sev();
    g_visibility_probe_shared.stage = 3u;
    g_visibility_probe_shared.done = 1u;
    g_runner.stream.telemetry.visibility_probe_core1_done_us = stpg_visibility_elapsed_us() + 1u;
    while (true) {
        tight_loop_contents();
    }
}

static void stpg_handshake_probe_core1_entry(void) {
    g_handshake_probe_core1_boot_stage_raw = 1u;
    flash_safe_execute_core_init();
    g_handshake_probe_core1_boot_stage_raw = 2u;
    const uint32_t mode = g_handshake_probe_mode_raw;
    const bool use_real_shared = mode != STPG_HANDSHAKE_PROBE_MODE_SYNTHETIC;
    const bool explicit_volatile = mode == STPG_HANDSHAKE_PROBE_MODE_REAL_SHARED_VOLATILE;
    volatile stpg_stream_shared_t *real_shared = explicit_volatile
        ? (volatile stpg_stream_shared_t *)&g_runner.stream.shared
        : NULL;
    while (true) {
        const bool stop_requested = use_real_shared
            ? (explicit_volatile ? real_shared->stop_requested : g_runner.stream.shared.stop_requested)
            : g_handshake_probe_shared.stop_requested;
        if (stop_requested) {
            tight_loop_contents();
            continue;
        }
        __dmb();
        const int32_t slot_index = use_real_shared
            ? (explicit_volatile ? real_shared->fill_request_slot : g_runner.stream.shared.fill_request_slot)
            : g_handshake_probe_shared.fill_request_slot;
        if (slot_index < 0) {
            tight_loop_contents();
            continue;
        }
        g_runner.stream.telemetry.handshake_probe_core1_saw_request_us = stpg_handshake_elapsed_us() + 1u;
        g_handshake_probe_core1_boot_stage_raw = 3u;
        if (use_real_shared) {
            if (explicit_volatile) {
                real_shared->fill_request_slot = -1;
            } else {
                g_runner.stream.shared.fill_request_slot = -1;
            }
        } else {
            g_handshake_probe_shared.fill_request_slot = -1;
        }
        g_runner.stream.telemetry.handshake_probe_core1_claim_us = stpg_handshake_elapsed_us() + 1u;
        __dmb();
        if (use_real_shared) {
            if (explicit_volatile) {
                real_shared->fill_error = ERR_NONE;
                real_shared->slot_ready[0] = true;
            } else {
                g_runner.stream.shared.fill_error = ERR_NONE;
                g_runner.stream.shared.slot_ready[0] = true;
            }
        } else {
            g_handshake_probe_shared.fill_error = ERR_NONE;
            g_handshake_probe_shared.slot_ready = true;
        }
        g_runner.stream.telemetry.handshake_probe_core1_ready_us = stpg_handshake_elapsed_us() + 1u;
        while (true) {
            tight_loop_contents();
        }
    }
}

static void STPG_STREAM_PRODUCER_FUNC(stpg_stream_core1_entry)(void) {
    g_stream_core1_boot_stage_raw = 1u;
    stpg_stack_watermark_core1_init_once();
#ifdef PICO_ON_DEVICE
    flash_safe_execute_core_init();
#endif
    g_stream_core1_boot_stage_raw = 2u;
    while (true) {
        g_stream_core1_boot_stage_raw = 3u;
        g_stream_core1_loop_raw += 1u;
        g_runner.stream.telemetry.core1_loop_count += 1u;
        g_stream_core1_last_stage_raw = 10u;
        if (g_runner.stream.shared.stop_requested) {
            g_stream_core1_last_stage_raw = 11u;
            g_stream_core1_skip_stop_raw += 1u;
            tight_loop_contents();
            continue;
        }
        g_stream_core1_last_stage_raw = 12u;
        if (g_stream_core1_after_stop_gate_us_raw == 0u) {
            g_stream_core1_after_stop_gate_us_raw = stpg_stream_elapsed_since_startup_us() + 1u;
        }

        __dmb();
        g_stream_core1_last_stage_raw = 13u;
        if (g_stream_core1_after_dmb_us_raw == 0u) {
            g_stream_core1_after_dmb_us_raw = stpg_stream_elapsed_since_startup_us() + 1u;
        }
        const int32_t slot_index = stpg_stream_find_pending_fill_request();
        g_stream_core1_last_stage_raw = 14u;
        if (g_stream_core1_after_slot_read_us_raw == 0u) {
            g_stream_core1_after_slot_read_us_raw = stpg_stream_elapsed_since_startup_us() + 1u;
        }
        if (g_stream_core1_first_poll_us_raw == 0u) {
            g_stream_core1_first_poll_us_raw = stpg_stream_elapsed_since_startup_us() + 1u;
        }
        g_stream_core1_last_polled_slot_raw = (uint32_t)(slot_index + 1);
        g_stream_core1_last_polled_fill_error_raw = (uint32_t)g_runner.stream.shared.fill_error;
        g_stream_core1_last_polled_slot0_ready_raw = g_runner.stream.shared.slot_ready[0] ? 1u : 0u;
        if (slot_index < 0) {
            if (g_stream_core1_first_negative_poll_us_raw == 0u) {
                g_stream_core1_first_negative_poll_us_raw = stpg_stream_elapsed_since_startup_us() + 1u;
            }
            g_stream_core1_negative_poll_count_raw += 1u;
            g_stream_core1_skip_no_request_raw += 1u;
            tight_loop_contents();
            continue;
        }
        if ((uint32_t)slot_index >= STPG_STREAM_SLOT_COUNT) {
            g_stream_core1_invalid_poll_count_raw += 1u;
            g_stream_core1_skip_no_request_raw += 1u;
            tight_loop_contents();
            continue;
        }

        g_stream_core1_valid_poll_count_raw += 1u;
        if (g_stream_core1_first_valid_poll_us_raw == 0u) {
            g_stream_core1_first_valid_poll_us_raw = stpg_stream_elapsed_since_startup_us() + 1u;
        }
        g_stream_core1_request_seen_raw += 1u;
        g_stream_core1_last_seen_slot_raw = (uint32_t)slot_index;
        g_stream_core1_claim_raw += 1u;
        g_stream_core1_service_raw += 1u;
        g_stream_core1_service_slot_raw = (uint32_t)slot_index;
        g_runner.stream.telemetry.core1_service_count += 1u;
        g_runner.stream.telemetry.core1_last_slot_index = (uint32_t)slot_index;
#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
        stpg_refill_trace_mark_claim((uint32_t)slot_index);
#endif
        if (g_stream_core1_probe_mode_raw == STPG_HANDSHAKE_PROBE_MODE_REAL_CORE1_POLL_ONLY) {
            if (g_runner.stream.telemetry.handshake_probe_core1_saw_request_us == 0u) {
                g_runner.stream.telemetry.handshake_probe_core1_saw_request_us =
                    stpg_handshake_elapsed_us() + 1u;
            }
            g_handshake_probe_core1_boot_stage_raw = 3u;
        }
        g_runner.stream.shared.fill_request_pending[slot_index] = false;
        __dmb();
#if STPG_STREAM_FIFO_FILL_REQUESTS
        g_runner.stream.shared.fill_request_queue_tail += 1u;
        __dmb();
#endif
        g_runner.stream.shared.fill_request_slot = stpg_stream_find_pending_fill_request();
        if (g_stream_request_clear_us_raw == 0u) {
            g_stream_request_clear_us_raw = stpg_stream_elapsed_since_startup_us() + 1u;
        }
        if (g_stream_core1_probe_mode_raw == STPG_HANDSHAKE_PROBE_MODE_REAL_CORE1_POLL_ONLY) {
            g_runner.stream.telemetry.handshake_probe_core1_claim_us = stpg_handshake_elapsed_us() + 1u;
            g_runner.stream.shared.slot_event_count[slot_index] = 1u;
            g_runner.stream.shared.slot_word_count[slot_index] = 1u;
            g_runner.stream.shared.slot_sample_count[slot_index] = 0u;
            g_runner.stream.shared.slot_eof[slot_index] = false;
            g_stream_core1_fill_error_raw = (uint32_t)ERR_NONE;
            g_runner.stream.telemetry.core1_last_fill_error = (uint32_t)ERR_NONE;
            __dmb();
            g_runner.stream.shared.slot_fill_error[slot_index] = ERR_NONE;
            g_runner.stream.shared.fill_error = ERR_NONE;
            g_runner.stream.shared.slot_ready[slot_index] = true;
            g_runner.stream.telemetry.handshake_probe_core1_ready_us = stpg_handshake_elapsed_us() + 1u;
            if (slot_index == 0 && g_runner.stream.telemetry.startup_slot0_ready_us == 0u) {
                g_runner.stream.telemetry.startup_slot0_ready_us =
                    stpg_stream_elapsed_since_startup_us() + 1u;
            }
            continue;
        }
#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
        stpg_refill_trace_mark_body_start((uint32_t)slot_index);
#endif
        const uint32_t start_us = time_us_32();
        stpg_stream_chunk_result_t result = {0};
        stpg_err_t err = ERR_NONE;
#if STPG_STREAM_FAIL_STOP
        if (g_runner.fail_stop_enabled) {
            err = stpg_buffer_stream_compare_begin_metadata_slot(
                (uint32_t)slot_index * STPG_STREAM_BUFFER_EVENTS);
        }
#endif
        if (err == ERR_NONE) {
            stpg_rate_stream_state_t *const state = stpg_runner_stream_state();
            if (g_runner.reduced_density_streaming) {
                err = stpg_buffer_stream_fill_next_chunk_reduced_compare(
                    state,
                    g_runner.stream.slots[slot_index].words,
                    stpg_runner_stream_sample_wait_buffer((uint32_t)slot_index),
                    STPG_STREAM_BUFFER_EVENTS,
                    &result
                );
            } else {
                err = stpg_buffer_stream_fill_next_chunk(
                    state,
                    g_runner.stream.slots[slot_index].words,
                    STPG_STREAM_BUFFER_EVENTS,
                    &result
                );
            }
        }
        const uint32_t elapsed_us = time_us_32() - start_us;
#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
        stpg_refill_trace_mark_body_end((uint32_t)slot_index, err);
#endif

        g_runner.stream.shared.slot_event_count[slot_index] = result.event_count;
        g_runner.stream.shared.slot_word_count[slot_index] = result.word_count;
        uint32_t slot_sample_count = result.sample_event_count;
#if STPG_STREAM_FAIL_STOP
        if (err == ERR_NONE && g_runner.fail_stop_enabled) {
            const uint32_t metadata_count = stpg_buffer_stream_compare_event_count();
            if (metadata_count > STPG_STREAM_BUFFER_EVENTS
                || metadata_count != slot_sample_count) {
                err = ERR_FAIL_STOP_METADATA_OVERRUN;
            } else {
                g_runner.stream.shared.slot_metadata_sample_start[slot_index] =
                    g_runner.stream.shared.producer_sample_count;
                g_runner.stream.shared.slot_metadata_count[slot_index] = metadata_count;
                g_runner.stream.shared.producer_sample_count += metadata_count;
            }
        }
#endif
        g_runner.stream.shared.slot_sample_count[slot_index] = slot_sample_count;
        g_runner.stream.shared.slot_p4_explicit_wait_end_vector_index[slot_index] =
            result.p4_explicit_wait_end_vector_index;
        g_runner.stream.shared.slot_p4_explicit_wait_end_present[slot_index] =
            result.p4_explicit_wait_end_present;
        g_runner.stream.shared.slot_eof[slot_index] = result.eof;
        g_runner.stream.telemetry.refill_count += 1u;
        g_runner.stream.telemetry.refill_event_count += result.event_count;
        g_runner.stream.telemetry.refill_total_us += elapsed_us;
        if (elapsed_us > g_runner.stream.telemetry.refill_max_us) {
            g_runner.stream.telemetry.refill_max_us = elapsed_us;
        }
        g_runner.stream.telemetry.refill_last_us = elapsed_us;
        g_runner.stream.telemetry.refill_last_event_count = result.event_count;
        g_stream_core1_fill_error_raw = (uint32_t)err;
        g_runner.stream.telemetry.core1_last_fill_error = (uint32_t)err;
        if (err == ERR_NONE && g_runner.stream.telemetry.startup_first_service_complete_us == 0u) {
            g_runner.stream.telemetry.startup_first_service_complete_us =
                stpg_stream_elapsed_since_startup_us() + 1u;
        }
        __dmb();
        g_runner.stream.shared.slot_fill_error[slot_index] = err;
        g_runner.stream.shared.fill_error = err;
        g_runner.stream.shared.slot_ready[slot_index] = (err == ERR_NONE);
#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
        if (err == ERR_NONE) {
            stpg_refill_trace_mark_ready((uint32_t)slot_index);
        }
#endif
        if (err == ERR_NONE && slot_index == 0 && g_runner.stream.telemetry.startup_slot0_ready_us == 0u) {
            g_runner.stream.telemetry.startup_slot0_ready_us =
                stpg_stream_elapsed_since_startup_us() + 1u;
        }
    }
}
#endif

static stpg_err_t __no_inline_not_in_flash_func(stpg_stream_request_fill)(uint32_t slot_index) {
#if STPG_STREAM_FIFO_FILL_REQUESTS
    const uint32_t queue_head = g_runner.stream.shared.fill_request_queue_head;
    const uint32_t queue_tail = g_runner.stream.shared.fill_request_queue_tail;
    if ((uint32_t)(queue_head - queue_tail) >= STPG_STREAM_SLOT_COUNT) {
        return ERR_BAD_STATE;
    }
#endif
    stpg_stream_slot_t *slot = &g_runner.stream.slots[slot_index];
#if STPG_STREAM_FAIL_STOP
    if (g_runner.fail_stop_enabled
        && !stpg_fail_stop_metadata_reusable(&slot->fail_stop_metadata)) {
        return ERR_FAIL_STOP_METADATA_OVERRUN;
    }
#endif
    if (!stpg_runner_stream_slot_transition(slot, STPG_STREAM_SLOT_FILLING)) {
        return ERR_BAD_STATE;
    }
#if STPG_STREAM_FAIL_STOP
    if (g_runner.fail_stop_enabled) {
        stpg_fail_stop_metadata_init(
            &slot->fail_stop_metadata,
            g_stpg_runner_stream_run_sequence);
    }
#endif
    slot->event_count = 0u;
    slot->word_count = 0u;
    slot->sample_count = 0u;
    slot->p4_explicit_wait_end_vector_index = 0u;
    slot->p4_explicit_wait_end_present = false;
    slot->eof = false;
    g_runner.stream.shared.slot_ready[slot_index] = false;
    g_runner.stream.shared.slot_event_count[slot_index] = 0u;
    g_runner.stream.shared.slot_word_count[slot_index] = 0u;
    g_runner.stream.shared.slot_sample_count[slot_index] = 0u;
    g_runner.stream.shared.slot_metadata_sample_start[slot_index] = 0u;
    g_runner.stream.shared.slot_metadata_count[slot_index] = 0u;
    g_runner.stream.shared.slot_p4_explicit_wait_end_vector_index[slot_index] = 0u;
    g_runner.stream.shared.slot_p4_explicit_wait_end_present[slot_index] = false;
    g_runner.stream.shared.slot_eof[slot_index] = false;
    g_runner.stream.shared.slot_fill_error[slot_index] = ERR_NONE;
    g_runner.stream.shared.fill_error = ERR_NONE;
    g_stream_core1_request_raw += 1u;
    g_runner.stream.telemetry.core1_request_count += 1u;
    if (g_runner.stream.telemetry.startup_first_fill_request_us == 0u) {
        g_runner.stream.telemetry.startup_first_fill_request_us =
            stpg_stream_elapsed_since_startup_us() + 1u;
    }
#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
    stpg_refill_trace_begin(slot_index);
#endif
    __dmb();
    g_runner.stream.shared.fill_request_pending[slot_index] = true;
#if STPG_STREAM_FIFO_FILL_REQUESTS
    g_runner.stream.shared.fill_request_queue[queue_head % STPG_STREAM_SLOT_COUNT] = slot_index;
    __dmb();
    g_runner.stream.shared.fill_request_queue_head = queue_head + 1u;
#endif
    __dmb();
    g_runner.stream.shared.fill_request_slot = (int32_t)slot_index;
    if (g_stream_request_set_us_raw == 0u) {
        g_stream_request_set_us_raw = stpg_stream_elapsed_since_startup_us() + 1u;
    }
    return ERR_NONE;
}

static stpg_err_t __no_inline_not_in_flash_func(stpg_runner_request_stream_fill_safe)(uint32_t slot_index) {
#if STPG_STREAM_FAIL_STOP
    if (g_runner.fail_stop_enabled) {
        stpg_fail_stop_metadata_t *metadata =
            &g_runner.stream.slots[slot_index].fail_stop_metadata;
        if (metadata->state == STPG_FAIL_STOP_METADATA_PUBLISHED) {
            return stpg_fail_stop_metadata_defer_refill(metadata);
        }
        if (!stpg_fail_stop_metadata_reusable(metadata)) {
            return ERR_FAIL_STOP_METADATA_OVERRUN;
        }
    }
#endif
    const stpg_err_t err = stpg_stream_request_fill(slot_index);
#if STPG_STREAM_FAIL_STOP
    if (g_runner.fail_stop_enabled && err == ERR_BAD_STATE) {
        return ERR_FAIL_STOP_METADATA_OVERRUN;
    }
#endif
    return err;
}

static stpg_err_t stpg_runner_run_visibility_probe(void) {
#ifndef PICO_ON_DEVICE
    return ERR_UNSUPPORTED;
#else
    memset((void *)&g_visibility_probe_shared, 0, sizeof(g_visibility_probe_shared));
    g_visibility_probe_t0_us = time_us_32();
    g_visibility_probe_core1_boot_stage_raw = 0u;
    g_runner.stream.telemetry.visibility_probe_request_set_us = 0u;
    g_runner.stream.telemetry.visibility_probe_core1_saw_request_us = 0u;
    g_runner.stream.telemetry.visibility_probe_core1_write1_us = 0u;
    g_runner.stream.telemetry.visibility_probe_core1_write2_us = 0u;
    g_runner.stream.telemetry.visibility_probe_core1_done_us = 0u;
    g_runner.stream.telemetry.visibility_probe_core0_first_stage_seen_us = 0u;
    g_runner.stream.telemetry.visibility_probe_core0_first_value1_seen_us = 0u;
    g_runner.stream.telemetry.visibility_probe_core0_first_value2_seen_us = 0u;
    g_runner.stream.telemetry.visibility_probe_core0_first_done_seen_us = 0u;
    g_runner.stream.telemetry.visibility_probe_final_stage = 0u;
    g_runner.stream.telemetry.visibility_probe_final_value1 = 0u;
    g_runner.stream.telemetry.visibility_probe_final_value2 = 0u;
    g_runner.stream.telemetry.visibility_probe_final_done = 0u;
    g_runner.stream.telemetry.visibility_probe_timeout_hit = 0u;

    multicore_reset_core1();
    multicore_launch_core1(stpg_visibility_probe_core1_entry);
    __dmb();
    g_visibility_probe_shared.request = 1u;
    g_runner.stream.telemetry.visibility_probe_request_set_us = stpg_visibility_elapsed_us() + 1u;

    const uint64_t deadline_us = time_us_64() + STPG_VISIBILITY_PROBE_TIMEOUT_US;
    while (true) {
        __dmb();
        const uint32_t stage = g_visibility_probe_shared.stage;
        const uint32_t value1 = g_visibility_probe_shared.value1;
        const uint32_t value2 = g_visibility_probe_shared.value2;
        const uint32_t done = g_visibility_probe_shared.done;
        if (stage != 0u && g_runner.stream.telemetry.visibility_probe_core0_first_stage_seen_us == 0u) {
            g_runner.stream.telemetry.visibility_probe_core0_first_stage_seen_us = stpg_visibility_elapsed_us() + 1u;
        }
        if (value1 == STPG_VISIBILITY_VALUE1 && g_runner.stream.telemetry.visibility_probe_core0_first_value1_seen_us == 0u) {
            g_runner.stream.telemetry.visibility_probe_core0_first_value1_seen_us = stpg_visibility_elapsed_us() + 1u;
        }
        if (value2 == STPG_VISIBILITY_VALUE2 && g_runner.stream.telemetry.visibility_probe_core0_first_value2_seen_us == 0u) {
            g_runner.stream.telemetry.visibility_probe_core0_first_value2_seen_us = stpg_visibility_elapsed_us() + 1u;
        }
        if (done != 0u && g_runner.stream.telemetry.visibility_probe_core0_first_done_seen_us == 0u) {
            g_runner.stream.telemetry.visibility_probe_core0_first_done_seen_us = stpg_visibility_elapsed_us() + 1u;
        }
        g_runner.stream.telemetry.visibility_probe_final_stage = stage;
        g_runner.stream.telemetry.visibility_probe_final_value1 = value1;
        g_runner.stream.telemetry.visibility_probe_final_value2 = value2;
        g_runner.stream.telemetry.visibility_probe_final_done = done;
        if (done != 0u) {
            break;
        }
        if (time_us_64() >= deadline_us) {
            g_runner.stream.telemetry.visibility_probe_timeout_hit = 1u;
            break;
        }
        tight_loop_contents();
    }

    stpg_status_set_run_progress(g_runner.stream.telemetry.visibility_probe_final_done != 0u ? 1u : 0u);
    stpg_status_set_result_flags(0u);
    stpg_status_set_fail_count(0u);
    stpg_status_set_sample_count(0u);
    if (g_runner.stream.telemetry.visibility_probe_timeout_hit != 0u) {
        stpg_status_set_error(ERR_TIMEOUT);
    } else {
        stpg_status_clear_error();
    }
    stpg_status_set_state(STATE_DONE);
    return ERR_NONE;
#endif
}

static stpg_err_t stpg_runner_run_handshake_probe(uint32_t mode) {
#ifndef PICO_ON_DEVICE
    return ERR_UNSUPPORTED;
#else
    stpg_stream_reset();
    memset((void *)&g_handshake_probe_shared, 0, sizeof(g_handshake_probe_shared));
    g_handshake_probe_shared.fill_request_slot = -1;
    g_handshake_probe_t0_us = time_us_32();
    g_handshake_probe_core1_boot_stage_raw = 0u;
    g_handshake_probe_mode_raw = mode;
    g_stream_core1_probe_mode_raw = 0u;
    g_runner.stream.telemetry.handshake_probe_request_set_us = 0u;
    g_runner.stream.telemetry.handshake_probe_core1_saw_request_us = 0u;
    g_runner.stream.telemetry.handshake_probe_core1_claim_us = 0u;
    g_runner.stream.telemetry.handshake_probe_core1_ready_us = 0u;
    g_runner.stream.telemetry.handshake_probe_core0_ready_seen_us = 0u;
    g_runner.stream.telemetry.handshake_probe_core0_error_seen_us = 0u;
    g_runner.stream.telemetry.handshake_probe_final_request_slot = 0u;
    g_runner.stream.telemetry.handshake_probe_final_ready = 0u;
    g_runner.stream.telemetry.handshake_probe_final_error = 0u;
    g_runner.stream.telemetry.handshake_probe_timeout_hit = 0u;
    const bool use_real_shared = mode != STPG_HANDSHAKE_PROBE_MODE_SYNTHETIC;
    const bool explicit_volatile = mode == STPG_HANDSHAKE_PROBE_MODE_REAL_SHARED_VOLATILE;
    const bool use_real_core1_poll_only = mode == STPG_HANDSHAKE_PROBE_MODE_REAL_CORE1_POLL_ONLY;

    multicore_reset_core1();
    if (use_real_core1_poll_only) {
        g_stream_startup_t0_us = time_us_32();
        g_stream_request_set_us_raw = 0u;
        g_stream_request_clear_us_raw = 0u;
        g_stream_core1_first_poll_us_raw = 0u;
        g_stream_core1_first_negative_poll_us_raw = 0u;
        g_stream_core1_first_valid_poll_us_raw = 0u;
        g_stream_core1_last_stage_raw = 0u;
        g_stream_core1_after_stop_gate_us_raw = 0u;
        g_stream_core1_after_dmb_us_raw = 0u;
        g_stream_core1_after_slot_read_us_raw = 0u;
        g_stream_core1_last_polled_slot_raw = 0u;
        g_stream_core1_last_polled_fill_error_raw = 0u;
        g_stream_core1_last_polled_slot0_ready_raw = 0u;
        g_stream_core1_negative_poll_count_raw = 0u;
        g_stream_core1_valid_poll_count_raw = 0u;
        g_stream_core1_invalid_poll_count_raw = 0u;
        g_stream_core1_request_seen_raw = 0u;
        g_stream_core1_claim_raw = 0u;
        g_stream_core1_skip_stop_raw = 0u;
        g_stream_core1_skip_no_request_raw = 0u;
        g_stream_core1_last_seen_slot_raw = 0u;
        g_stream_core1_service_raw = 0u;
        g_stream_core1_service_slot_raw = 0u;
        g_stream_core1_fill_error_raw = 0u;
        g_stream_core1_loop_raw = 0u;
        g_runner.stream.shared.stop_requested = false;
        g_runner.stream.shared.fill_request_slot = -1;
        g_runner.stream.shared.fill_error = ERR_NONE;
#if STPG_STREAM_FIFO_FILL_REQUESTS
        g_runner.stream.shared.fill_request_queue_head = 0u;
        g_runner.stream.shared.fill_request_queue_tail = 0u;
#endif
        for (uint32_t slot_index = 0u; slot_index < STPG_STREAM_SLOT_COUNT; ++slot_index) {
            g_runner.stream.shared.fill_request_pending[slot_index] = false;
            g_runner.stream.shared.slot_ready[slot_index] = false;
            g_runner.stream.shared.slot_event_count[slot_index] = 0u;
            g_runner.stream.shared.slot_word_count[slot_index] = 0u;
            g_runner.stream.shared.slot_sample_count[slot_index] = 0u;
            g_runner.stream.shared.slot_fill_error[slot_index] = ERR_NONE;
            g_runner.stream.shared.slot_eof[slot_index] = false;
        }
        g_stream_core1_probe_mode_raw = mode;
        multicore_launch_core1(stpg_stream_core1_entry);
    } else {
        multicore_launch_core1(stpg_handshake_probe_core1_entry);
    }
    if (use_real_shared) {
        stpg_stream_shared_reset();
        if (explicit_volatile) {
            volatile stpg_stream_shared_t *shared = (volatile stpg_stream_shared_t *)&g_runner.stream.shared;
            shared->stop_requested = false;
            shared->slot_ready[0] = false;
            shared->fill_error = ERR_NONE;
            __dmb();
            shared->fill_request_slot = 0;
        } else {
            g_runner.stream.shared.stop_requested = false;
            g_runner.stream.shared.slot_ready[0] = false;
            g_runner.stream.shared.fill_error = ERR_NONE;
            __dmb();
            g_runner.stream.shared.fill_request_slot = 0;
        }
    } else {
        g_handshake_probe_shared.stop_requested = false;
        g_handshake_probe_shared.slot_ready = false;
        g_handshake_probe_shared.fill_error = ERR_NONE;
        __dmb();
        g_handshake_probe_shared.fill_request_slot = 0;
    }
    g_runner.stream.telemetry.handshake_probe_request_set_us = stpg_handshake_elapsed_us() + 1u;

    const uint64_t deadline_us = time_us_64() + STPG_VISIBILITY_PROBE_TIMEOUT_US;
    while (true) {
        __dmb();
        const bool ready = use_real_shared
            ? (explicit_volatile ? ((volatile stpg_stream_shared_t *)&g_runner.stream.shared)->slot_ready[0]
                                 : g_runner.stream.shared.slot_ready[0])
            : g_handshake_probe_shared.slot_ready;
        const stpg_err_t err = use_real_shared
            ? (explicit_volatile ? ((volatile stpg_stream_shared_t *)&g_runner.stream.shared)->fill_error
                                 : g_runner.stream.shared.fill_error)
            : g_handshake_probe_shared.fill_error;
        const int32_t slot = use_real_shared
            ? (explicit_volatile ? ((volatile stpg_stream_shared_t *)&g_runner.stream.shared)->fill_request_slot
                                 : g_runner.stream.shared.fill_request_slot)
            : g_handshake_probe_shared.fill_request_slot;
        if (ready && g_runner.stream.telemetry.handshake_probe_core0_ready_seen_us == 0u) {
            g_runner.stream.telemetry.handshake_probe_core0_ready_seen_us = stpg_handshake_elapsed_us() + 1u;
        }
        if (err != ERR_NONE && g_runner.stream.telemetry.handshake_probe_core0_error_seen_us == 0u) {
            g_runner.stream.telemetry.handshake_probe_core0_error_seen_us = stpg_handshake_elapsed_us() + 1u;
        }
        g_runner.stream.telemetry.handshake_probe_final_request_slot = (uint32_t)(slot + 1);
        g_runner.stream.telemetry.handshake_probe_final_ready = ready ? 1u : 0u;
        g_runner.stream.telemetry.handshake_probe_final_error = (uint32_t)err;
        if (ready || err != ERR_NONE) {
            break;
        }
        if (time_us_64() >= deadline_us) {
            g_runner.stream.telemetry.handshake_probe_timeout_hit = 1u;
            if (use_real_shared) {
                if (explicit_volatile) {
                    ((volatile stpg_stream_shared_t *)&g_runner.stream.shared)->fill_error = ERR_TIMEOUT;
                } else {
                    g_runner.stream.shared.fill_error = ERR_TIMEOUT;
                }
            } else {
                g_handshake_probe_shared.fill_error = ERR_TIMEOUT;
            }
            g_runner.stream.telemetry.handshake_probe_final_error = ERR_TIMEOUT;
            break;
        }
        tight_loop_contents();
    }

    stpg_status_set_run_progress(g_runner.stream.telemetry.handshake_probe_final_ready != 0u ? 1u : 0u);
    stpg_status_set_result_flags(0u);
    stpg_status_set_fail_count(0u);
    stpg_status_set_sample_count(0u);
    g_stream_core1_probe_mode_raw = 0u;
    if (g_runner.stream.telemetry.handshake_probe_timeout_hit != 0u) {
        stpg_status_set_error(ERR_TIMEOUT);
    } else {
        stpg_status_clear_error();
    }
    stpg_status_set_state(STATE_DONE);
    return ERR_NONE;
#endif
}

static stpg_err_t stpg_stream_wait_for_ready(uint32_t slot_index) {
    const uint64_t deadline_us = time_us_64() + STPG_STREAM_PREFILL_TIMEOUT_US;
    while (!g_runner.stream.shared.slot_ready[slot_index]) {
        __dmb();
        if (g_runner.stream.shared.slot_fill_error[slot_index] != ERR_NONE) {
            return g_runner.stream.shared.slot_fill_error[slot_index];
        }
        if (time_us_64() >= deadline_us) {
            g_runner.stream.telemetry.startup_timeout_core1_loop_count = g_runner.stream.telemetry.core1_loop_count;
            g_runner.stream.telemetry.startup_timeout_core1_service_count = g_runner.stream.telemetry.core1_service_count;
            g_runner.stream.telemetry.startup_timeout_stop_requested = g_runner.stream.shared.stop_requested ? 1u : 0u;
            g_runner.stream.telemetry.startup_timeout_fill_request_slot = (uint32_t)(g_runner.stream.shared.fill_request_slot + 1);
            g_runner.stream.telemetry.startup_timeout_core1_boot_stage_raw = g_stream_core1_boot_stage_raw;
            g_runner.stream.telemetry.startup_timeout_core1_request_raw = g_stream_core1_request_raw;
            g_runner.stream.telemetry.startup_timeout_core1_service_raw = g_stream_core1_service_raw;
            g_runner.stream.telemetry.startup_timeout_core1_fill_error_raw = g_stream_core1_fill_error_raw;
            g_runner.stream.telemetry.startup_timeout_core1_request_seen_raw = g_stream_core1_request_seen_raw;
            g_runner.stream.telemetry.startup_timeout_core1_claim_raw = g_stream_core1_claim_raw;
            g_runner.stream.telemetry.startup_timeout_core1_skip_stop_raw = g_stream_core1_skip_stop_raw;
            g_runner.stream.telemetry.startup_timeout_core1_skip_no_request_raw = g_stream_core1_skip_no_request_raw;
            g_runner.stream.telemetry.startup_timeout_core1_last_seen_slot_raw = g_stream_core1_last_seen_slot_raw;
            g_runner.stream.telemetry.startup_timeout_core1_last_polled_slot_raw = g_stream_core1_last_polled_slot_raw;
            g_runner.stream.telemetry.startup_timeout_core1_negative_poll_count_raw = g_stream_core1_negative_poll_count_raw;
            g_runner.stream.telemetry.startup_timeout_core1_valid_poll_count_raw = g_stream_core1_valid_poll_count_raw;
            g_runner.stream.telemetry.startup_timeout_core1_invalid_poll_count_raw = g_stream_core1_invalid_poll_count_raw;
            g_runner.stream.telemetry.startup_timeout_core1_last_polled_fill_error_raw = g_stream_core1_last_polled_fill_error_raw;
            g_runner.stream.telemetry.startup_timeout_core1_last_polled_slot0_ready_raw = g_stream_core1_last_polled_slot0_ready_raw;
            g_runner.stream.telemetry.startup_timeout_request_set_us = g_stream_request_set_us_raw;
            g_runner.stream.telemetry.startup_timeout_request_clear_us = g_stream_request_clear_us_raw;
            g_runner.stream.telemetry.startup_timeout_core1_first_poll_us = g_stream_core1_first_poll_us_raw;
            g_runner.stream.telemetry.startup_timeout_core1_first_negative_poll_us = g_stream_core1_first_negative_poll_us_raw;
            g_runner.stream.telemetry.startup_timeout_core1_first_valid_poll_us = g_stream_core1_first_valid_poll_us_raw;
            g_runner.stream.telemetry.startup_timeout_core1_last_stage_raw = g_stream_core1_last_stage_raw;
            g_runner.stream.telemetry.startup_timeout_core1_after_stop_gate_us = g_stream_core1_after_stop_gate_us_raw;
            g_runner.stream.telemetry.startup_timeout_core1_after_dmb_us = g_stream_core1_after_dmb_us_raw;
            g_runner.stream.telemetry.startup_timeout_core1_after_slot_read_us = g_stream_core1_after_slot_read_us_raw;
            g_runner.stream.telemetry.startup_timeout_shared_fill_request_slot =
                (uint32_t)(g_runner.stream.shared.fill_request_slot + 1);
            g_runner.stream.telemetry.startup_timeout_shared_slot0_ready =
                g_runner.stream.shared.slot_ready[0] ? 1u : 0u;
            g_runner.stream.telemetry.startup_timeout_shared_fill_error =
                (uint32_t)g_runner.stream.shared.fill_error;
            g_runner.stream.telemetry.startup_timeout_shared_slot0_event_count =
                g_runner.stream.shared.slot_event_count[0];
            g_runner.stream.telemetry.startup_timeout_shared_slot0_word_count =
                g_runner.stream.shared.slot_word_count[0];
            g_runner.stream.shared.fill_error = ERR_TIMEOUT;
            g_runner.stream.shared.fill_request_slot = -1;
            if (g_stream_request_clear_us_raw == 0u) {
                g_stream_request_clear_us_raw = stpg_stream_elapsed_since_startup_us() + 1u;
                g_runner.stream.telemetry.startup_timeout_request_clear_us = g_stream_request_clear_us_raw;
            }
            return ERR_TIMEOUT;
        }
        tight_loop_contents();
    }
    __dmb();
    g_runner.stream.slots[slot_index].event_count = g_runner.stream.shared.slot_event_count[slot_index];
    g_runner.stream.slots[slot_index].word_count = g_runner.stream.shared.slot_word_count[slot_index];
    g_runner.stream.slots[slot_index].sample_count = g_runner.stream.shared.slot_sample_count[slot_index];
    g_runner.stream.slots[slot_index].eof = g_runner.stream.shared.slot_eof[slot_index];
#if STPG_STREAM_FAIL_STOP
    if (g_runner.fail_stop_enabled) {
        const stpg_err_t metadata_err = stpg_fail_stop_metadata_publish(
            &g_runner.stream.slots[slot_index].fail_stop_metadata,
            g_stpg_runner_stream_run_sequence,
            g_runner.stream.shared.slot_metadata_sample_start[slot_index],
            g_runner.stream.shared.slot_metadata_count[slot_index]);
        if (metadata_err != ERR_NONE) {
            return metadata_err;
        }
    }
#endif
    if (!stpg_runner_stream_slot_transition(
            &g_runner.stream.slots[slot_index],
            STPG_STREAM_SLOT_READY
        )) {
        return ERR_BAD_STATE;
    }
    return ERR_NONE;
}

static stpg_err_t stpg_runner_ensure_initialized(void) {
    if (g_runner.initialized) {
        return ERR_NONE;
    }

    g_runner.output_dma_channel = dma_claim_unused_channel(false);
    if (g_runner.output_dma_channel < 0) {
        return ERR_DMA_ERROR;
    }
    g_runner.output_dma_spare_channel = dma_claim_unused_channel(false);
    if (g_runner.output_dma_spare_channel < 0) {
        return ERR_DMA_ERROR;
    }
    g_runner.sample_dma_channel = dma_claim_unused_channel(false);
    if (g_runner.sample_dma_channel < 0) {
        return ERR_DMA_ERROR;
    }
    g_runner.sample_sched_dma_channel = dma_claim_unused_channel(false);
    if (g_runner.sample_sched_dma_channel < 0) {
        return ERR_DMA_ERROR;
    }

    irq_set_exclusive_handler(DMA_IRQ_0, stpg_runner_dma_irq0);
    irq_set_enabled(DMA_IRQ_0, true);
    dma_channel_set_irq0_enabled((uint)g_runner.output_dma_channel, true);
    dma_channel_set_irq0_enabled((uint)g_runner.output_dma_spare_channel, false);

#ifdef PICO_ON_DEVICE
#endif
    g_runner.initialized = true;
    return ERR_NONE;
}

static void stpg_runner_prepare_output_sm(bool preserve_gpio_state) {
    const PIO pio = stpg_runner_pio();
    const uint out_sm = stpg_runner_sm();
    const uint sample_sm = stpg_sample_sm();
    const uint32_t active_pins = stpg_buffer_active_pins();
    const uint32_t out_base = stpg_board_out_pin_base(active_pins);
    const uint32_t out_span = stpg_board_out_pin_span(active_pins);
    const bool reduced = g_runner.reduced_density_streaming;

    pio_sm_set_enabled(pio, out_sm, false);
    pio_sm_set_enabled(pio, sample_sm, false);
    pio_sm_clear_fifos(pio, out_sm);
    pio_sm_clear_fifos(pio, sample_sm);
    pio_sm_restart(pio, out_sm);
    pio_sm_restart(pio, sample_sm);
    pio_clear_instruction_memory(pio);

    if (active_pins == STPG_ACTIVE_PINS_V11) {
        if (reduced) {
            if (!pio_can_add_program(pio, &stpg_runner27_reduced_program)
                || !pio_can_add_program(pio, &STPG_SAMPLE_DELAYED_IN27_PROGRAM)) {
                stpg_status_set_error(ERR_PIO_ERROR);
                return;
            }
            g_runner.output27_reduced_pio_offset = pio_add_program(pio, &stpg_runner27_reduced_program);
            g_runner.sample_delayed27_pio_offset = pio_add_program(pio, &STPG_SAMPLE_DELAYED_IN27_PROGRAM);
        } else {
            if (!pio_can_add_program(pio, &stpg_runner27_program)
                || !pio_can_add_program(pio, &stpg_sample_in27_program)) {
                stpg_status_set_error(ERR_PIO_ERROR);
                return;
            }
            g_runner.output_pio27_offset = pio_add_program(pio, &stpg_runner27_program);
            g_runner.sample_pio27_offset = pio_add_program(pio, &stpg_sample_in27_program);
        }
    } else {
        if (reduced) {
            if (!pio_can_add_program(pio, &stpg_runner_reduced_program)
                || !pio_can_add_program(pio, &STPG_SAMPLE_DELAYED_IN_PROGRAM)) {
                stpg_status_set_error(ERR_PIO_ERROR);
                return;
            }
            g_runner.output_reduced_pio_offset = pio_add_program(pio, &stpg_runner_reduced_program);
            g_runner.sample_delayed_pio_offset = pio_add_program(pio, &STPG_SAMPLE_DELAYED_IN_PROGRAM);
        } else {
            if (!pio_can_add_program(pio, &stpg_runner_program)
                || !pio_can_add_program(pio, &stpg_sample_in_program)) {
                stpg_status_set_error(ERR_PIO_ERROR);
                return;
            }
            g_runner.output_pio_offset = pio_add_program(pio, &stpg_runner_program);
            g_runner.sample_pio_offset = pio_add_program(pio, &stpg_sample_in_program);
        }
    }

    pio_sm_config out_cfg = active_pins == STPG_ACTIVE_PINS_V11
        ? (reduced
            ? stpg_runner27_reduced_program_get_default_config(g_runner.output27_reduced_pio_offset)
            : stpg_runner27_program_get_default_config(g_runner.output_pio27_offset))
        : (reduced
            ? stpg_runner_reduced_program_get_default_config(g_runner.output_reduced_pio_offset)
            : stpg_runner_program_get_default_config(g_runner.output_pio_offset));
    sm_config_set_clkdiv_int_frac(&out_cfg, 1u, 0u);
    sm_config_set_out_pins(&out_cfg, out_base, out_span);
    sm_config_set_out_shift(&out_cfg, true, false, 32u);
    pio_sm_init(
        pio,
        out_sm,
        active_pins == STPG_ACTIVE_PINS_V11
            ? (reduced ? g_runner.output27_reduced_pio_offset : g_runner.output_pio27_offset)
            : (reduced ? g_runner.output_reduced_pio_offset : g_runner.output_pio_offset),
        &out_cfg);
    /*
     * A segment-end hold leaves the PIO state machine disabled while the
     * host uploads the next segment.  Releasing the pins here would create
     * a Hi-Z interval at the segment boundary and allow the DUT to observe
     * an unintended transition.  The hold finalizer deliberately leaves
     * the PIO pin directions and output values intact, so a preserved
     * continuation must keep them intact through PIO reinitialization.
     *
     * Normal/direct starts must clear the PIO output enables BEFORE muxing
     * the pads back from SIO. RESET/STOP release the pads through SIO but do
     * not clear PIO's latched values/directions; SM init/restart does not
     * clear those latches either. An earlier mux would expose the previous
     * run's driven level before the first event of the new run.
     */
    if (!preserve_gpio_state) {
        pio_sm_set_consecutive_pindirs(pio, out_sm, out_base, out_span, false);
    }

    if (active_pins == STPG_ACTIVE_PINS_V01) {
        pio_sm_config sample_cfg = reduced
            ? STPG_SAMPLE_DELAYED_IN_PROGRAM_GET_DEFAULT_CONFIG(g_runner.sample_delayed_pio_offset)
            : stpg_sample_in_program_get_default_config(g_runner.sample_pio_offset);
        sm_config_set_clkdiv_int_frac(&sample_cfg, 1u, 0u);
        sm_config_set_in_pins(&sample_cfg, STPG_DUT_PIN_BASE);
        sm_config_set_in_shift(&sample_cfg, false, true, 16u);
        pio_sm_init(pio, sample_sm, reduced ? g_runner.sample_delayed_pio_offset : g_runner.sample_pio_offset, &sample_cfg);
    } else {
        pio_sm_config sample_cfg = reduced
            ? STPG_SAMPLE_DELAYED_IN27_PROGRAM_GET_DEFAULT_CONFIG(g_runner.sample_delayed27_pio_offset)
            : stpg_sample_in27_program_get_default_config(g_runner.sample_pio27_offset);
        sm_config_set_clkdiv_int_frac(&sample_cfg, 1u, 0u);
        sm_config_set_in_pins(&sample_cfg, stpg_board_out_pin_base(active_pins));
        sm_config_set_in_shift(&sample_cfg, false, true, 27u);
        pio_sm_init(pio, sample_sm, reduced ? g_runner.sample_delayed27_pio_offset : g_runner.sample_pio27_offset, &sample_cfg);
    }

    /* Commit the pad handoff only after both SMs are configured and normal
     * starts have released PIO output enables. HOLD keeps its existing PIO
     * latch/direction throughout. Only mapped DUT pads are muxed here. */
    stpg_runner_set_pio_pins();
}

static dma_channel_config __no_inline_not_in_flash_func(stpg_runner_make_output_dma_config)(uint dma_channel, uint chain_to) {
    dma_channel_config out_dma_cfg = dma_channel_get_default_config(dma_channel);
    channel_config_set_transfer_data_size(&out_dma_cfg, DMA_SIZE_32);
    channel_config_set_read_increment(&out_dma_cfg, true);
    channel_config_set_write_increment(&out_dma_cfg, false);
    channel_config_set_dreq(&out_dma_cfg, pio_get_dreq(stpg_runner_pio(), stpg_runner_sm(), true));
    channel_config_set_chain_to(&out_dma_cfg, chain_to);
    return out_dma_cfg;
}

static void __no_inline_not_in_flash_func(stpg_runner_configure_chained_output_dma)(
    uint dma_channel,
    uint chain_to_channel,
    const uint32_t *words,
    uint32_t word_count,
    bool trigger
) {
    const PIO pio = stpg_runner_pio();
    const uint out_sm = stpg_runner_sm();
    dma_channel_config out_dma_cfg = stpg_runner_make_output_dma_config(dma_channel, chain_to_channel);
    dma_channel_acknowledge_irq0(dma_channel);
    dma_channel_configure(dma_channel, &out_dma_cfg,
                          &pio->txf[out_sm],
                          words,
                          word_count,
                          trigger);
}

static void __no_inline_not_in_flash_func(stpg_runner_start_output_dma)(const uint32_t *words, uint32_t word_count) {
    const PIO pio = stpg_runner_pio();
    const uint out_sm = stpg_runner_sm();
    pio_interrupt_clear(pio, 4);
    pio_sm_set_enabled(pio, out_sm, true);
    stpg_runner_configure_chained_output_dma(
        (uint)g_runner.output_dma_channel,
        (uint)g_runner.output_dma_channel,
        words,
        word_count,
        true
    );
}

static stpg_err_t __no_inline_not_in_flash_func(stpg_runner_start_sample_sched_dma)(const uint32_t *wait_words, uint32_t sample_count) {
    if (sample_count == 0u) {
        return ERR_NONE;
    }
    if (g_runner.sample_sched_dma_channel < 0 || wait_words == 0) {
        return ERR_BAD_STATE;
    }

#if STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF
    /*
     * The schedule DMA has one transfer per output event and is driven by
     * the sample PIO TX DREQ.  At an output-DMA chunk boundary the previous
     * schedule transfer may still own this channel even though the output
     * completion IRQ has fired.  Reconfiguring a busy channel can discard
     * the old schedule tail and leave the sample PIO waiting for IRQ4.
     *
     * Wait only for the channel's current transfer to finish.  The PIO TX
     * FIFO preserves ordering, so a completed DMA transfer is a safe point
     * for appending the next slot's schedule words.  The timeout is bounded
     * because this function can run from the output-DMA IRQ path.
     */
    const uint32_t handoff_start_us = time_us_32();
    while (dma_channel_is_busy((uint)g_runner.sample_sched_dma_channel)) {
        if ((uint32_t)(time_us_32() - handoff_start_us) > STPG_SAMPLE_SCHEDULE_HANDOFF_TIMEOUT_US) {
            return ERR_TIMEOUT;
        }
        tight_loop_contents();
    }
#endif

    const PIO pio = stpg_runner_pio();
    const uint sample_sm = stpg_sample_sm();
    dma_channel_config cfg = dma_channel_get_default_config((uint)g_runner.sample_sched_dma_channel);
    channel_config_set_transfer_data_size(&cfg, DMA_SIZE_32);
    channel_config_set_read_increment(&cfg, true);
    channel_config_set_write_increment(&cfg, false);
    channel_config_set_dreq(&cfg, pio_get_dreq(pio, sample_sm, true));
    dma_channel_configure((uint)g_runner.sample_sched_dma_channel, &cfg,
                          &pio->txf[sample_sm],
                          wait_words,
                          sample_count,
                          false);
    g_runner.stream.telemetry.sample_schedule_word_count += sample_count;
    g_runner.stream.telemetry.sample_request_count += sample_count;
    dma_start_channel_mask(1u << (uint)g_runner.sample_sched_dma_channel);
    return ERR_NONE;
}

static void stpg_runner_publish_streaming_result(stpg_err_t compare_err);

#if defined(PICO_ON_DEVICE) && defined(__GNUC__)
__attribute__((noinline, target("general-regs-only")))
#endif
static void stpg_runner_finish_streaming_success(void) {
    if (g_runner.fail_stop_terminalized) {
        return;
    }
    /* No producer work may begin while the final sample frontier and
     * metadata are being checked.  This is also the first half of the
     * terminalization barrier used by the fail-stop path. */
    g_runner.stream.shared.stop_requested = true;
    g_runner.stream.shared.fill_request_slot = -1;
    for (uint32_t slot_index = 0u; slot_index < STPG_STREAM_SLOT_COUNT; ++slot_index) {
        g_runner.stream.shared.fill_request_pending[slot_index] = false;
    }
    __sev();
    const bool segment_end_hold = g_runner.segment_end_hold;
    if (segment_end_hold) {
        /* The stream state is the authoritative logical terminal state for a
         * Rate Vector ring.  Capture it before the stream context is reset by
         * any later segment and leave the board outputs parked in place. */
        stpg_runner_capture_terminal_hold_state();
    } else {
        stpg_runner_clear_hold_state();
    }
    stpg_runner_finish_hw_hold_outputs();
    const uint32_t captured_sample_count = stpg_runner_captured_sample_count();
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    /* No RX producer remains: flush the bounded valid SRAM suffix before
     * compare replay can read any sample from its PSRAM result backing. */
    const stpg_err_t staging_err =
        stpg_samples_finish_staging(stpg_runner_captured_sample_count);
    if (staging_err != ERR_NONE) {
        stpg_runner_fail_streaming(staging_err, false);
        return;
    }
#endif
#if STPG_STREAM_FAIL_STOP
    if (g_runner.fail_stop_enabled) {
        /* A short stream can reach its EOF drain deadline before the main
         * loop has consumed the final published fail-stop metadata slice.
         * Hardware is quiescent now, so drain that stable sample frontier
         * before treating an unconsumed frontier as metadata corruption. */
        uint32_t fail_event_index = 0u;
        const stpg_err_t fail_stop_err =
            stpg_runner_check_stream_fail_stop_samples(&fail_event_index);
        if (fail_stop_err == ERR_COMPARE_FAIL_STOP) {
            stpg_runner_finish_streaming_fail_stop(
                ERR_COMPARE_FAIL_STOP,
                fail_event_index + 1u);
            return;
        }
        if (fail_stop_err != ERR_NONE) {
            stpg_runner_finish_streaming_fail_stop(
                fail_stop_err,
                g_runner.fail_stop_next_event_index);
            return;
        }

        const uint32_t expected_sample_count = stpg_buffer_stream_expected_sample_count();
        const uint32_t metadata_sample_count = g_runner.stream.shared.producer_sample_count;
        if (metadata_sample_count != expected_sample_count
            || g_runner.fail_stop_next_sample_index != captured_sample_count) {
            stpg_runner_finish_streaming_fail_stop(
                ERR_FAIL_STOP_METADATA_OVERRUN,
                g_runner.fail_stop_next_event_index);
            return;
        }
    }
#endif
    g_runner.stream.telemetry.sample_capture_count = captured_sample_count;
    g_runner.stream.telemetry.compare_event_count = stpg_buffer_stream_compare_event_count();
    stpg_samples_finish_run(captured_sample_count, false);
    stpg_err_t compare_err = ERR_NONE;
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
    stpg_buffer_snapshot_stream_telemetry();
    /* Replay reconstructs compare metadata from PSRAM.  It must start with
     * the same logical value/direction that drove this segment; this matters
     * when a preserved GPIO state crosses a segment boundary. */
    /* The replay initializer reaches floating-point code in
     * stpg_buffer_prepare_for_streaming(). A debug halt/resume can leave
     * CPACR access unavailable, so repair it from this integer-only terminal
     * path before entering the replay call graph. */
    if (!stpg_fpu_access_ensure_for_run()) {
        compare_err = ERR_BAD_STATE;
    } else {
        compare_err = stpg_compare_replay_begin(
            &g_terminal_replay.compare,
            stpg_runner_stream_state(),
            g_runner.stream_entry_drive_value,
            g_runner.stream_entry_drive_enable
        );
    }
    if (compare_err == ERR_NONE) {
        /* Keep command ownership while the final comparisons are pending.
         * Return to main.c so USB can service STATUS/STOP/RESET between fills;
         * never recursively dispatch USB from the comparison loop. */
        g_terminal_replay.pending = true;
        stpg_status_set_state(STATE_RUN_DRAINING);
        return;
    }
#elif !STPG_STREAM_FAIL_STOP
    stpg_compare_run_streamed(
        stpg_buffer_stream_compare_events(),
        stpg_buffer_stream_compare_event_indices(),
        stpg_buffer_stream_compare_event_count()
    );
    stpg_buffer_snapshot_stream_telemetry();
#endif
    stpg_runner_publish_streaming_result(compare_err);
}

static void stpg_runner_publish_streaming_result(stpg_err_t compare_err) {
    const bool segment_end_hold = g_runner.segment_end_hold;
    g_runner.active = false;
    g_runner.draining = false;
    g_runner.streaming = false;
    g_runner.reduced_density_streaming = false;
    g_runner.stream.active = false;
    g_runner.drain_deadline_us = 0u;
    g_runner.sample_event_count = 0u;
    g_runner.stream.shared.stop_requested = true;
    stpg_runner_reset_fail_stop_context();
    stpg_status_set_result_flags(stpg_samples_result_flags() | stpg_compare_result_flags());
    stpg_status_set_fail_count(stpg_compare_fail_count());
    stpg_status_set_sample_count(stpg_samples_sample_count());
    if (compare_err != ERR_NONE) {
        stpg_status_set_error(compare_err);
        stpg_status_set_state(STATE_ERROR);
        return;
    }
#if STPG_STREAM_STATUS_LOGICAL_EVENTS
    /* The ring carries compressed output chunks, but a successful terminal
     * status must expose the logical expanded stream accepted at upload. */
    stpg_status_set_pattern_info(stpg_buffer_pattern_size(), stpg_buffer_event_count());
    stpg_status_set_run_progress(stpg_buffer_event_count());
#endif
    stpg_status_clear_error();
    if (segment_end_hold) {
        g_runner.segment_end_hold = false;
        stpg_status_set_state(STATE_HOLD_AWAITING_NEXT);
    } else {
        stpg_status_set_state(STATE_DONE);
    }
}

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
static void stpg_runner_terminal_replay_task(void) {
    if (!g_terminal_replay.pending) return;
    bool done = false;
    const uint32_t started_us = time_us_32();
    const stpg_err_t err = stpg_compare_replay_step(&g_terminal_replay.compare, &done);
    const uint32_t elapsed_us = time_us_32() - started_us;
    ++g_terminal_replay.calls;
    g_terminal_replay.total_us += elapsed_us;
    if (elapsed_us > g_terminal_replay.max_us) g_terminal_replay.max_us = elapsed_us;
    if (err == ERR_NONE && !done) return;
    g_terminal_replay.pending = false;
    if (err == ERR_NONE) {
        g_runner.stream.telemetry.compare_event_count = g_terminal_replay.compare.compare_count;
    }
    stpg_runner_publish_streaming_result(err);
}
#endif

bool stpg_runner_terminal_replay_diagnostics(uint32_t words[6]) {
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
    if (words == NULL) return false;
    words[0] = g_terminal_replay.calls;
    words[1] = g_terminal_replay.compare.event_count;
    words[2] = g_terminal_replay.compare.sample_index;
    words[3] = g_terminal_replay.compare.compare_count;
    words[4] = g_terminal_replay.total_us;
    words[5] = g_terminal_replay.max_us;
    return true;
#else
    (void)words;
    return false;
#endif
}

#if STPG_STREAM_FAIL_STOP
static void stpg_runner_finish_streaming_fail_stop(stpg_err_t err, uint32_t progress) {
    if (g_runner.fail_stop_terminalized) {
        return;
    }
    g_runner.fail_stop_terminalized = true;

    /* Publish the Core 1 stop gate before touching DMA/PIO.  Core 1 may
     * finish an already-running fill, but it must not claim another one
     * after this point. */
    g_runner.stream.shared.stop_requested = true;
    g_runner.stream.shared.fill_request_slot = -1;
    for (uint32_t slot_index = 0u; slot_index < STPG_STREAM_SLOT_COUNT; ++slot_index) {
        g_runner.stream.shared.fill_request_pending[slot_index] = false;
    }
    __sev();
    const uint32_t captured_sample_count = stpg_runner_captured_sample_count();
    stpg_runner_disable_hw_safe();
    stpg_samples_finish_run(captured_sample_count, false);

    g_runner.pending_noready_prod_recheck = false;
    g_runner.active = false;
    g_runner.draining = false;
    g_runner.streaming = false;
    g_runner.reduced_density_streaming = false;
    stpg_runner_reset_fail_stop_context();
    g_runner.drain_deadline_us = 0u;
    g_runner.sample_event_count = 0u;
    g_runner.stream.active = false;
    stpg_status_set_run_progress(progress);
    stpg_status_set_result_flags(stpg_samples_result_flags() | stpg_compare_result_flags());
    stpg_status_set_fail_count(stpg_compare_fail_count());
    stpg_status_set_sample_count(stpg_samples_sample_count());
    stpg_status_set_error(err);
}
#endif

static void stpg_runner_fail_streaming(stpg_err_t err, bool from_irq) {
    if (g_runner.fail_stop_terminalized
        || (!g_runner.active && !g_runner.draining && !g_runner.streaming
            && stpg_status_get_state() == STATE_ERROR)) {
        return;
    }
    extern volatile uint32_t g_main_loop_heartbeat_count_raw;
    extern volatile uint32_t g_main_loop_stage_raw;
    extern volatile uint32_t g_usb_protocol_task_call_count_raw;
    extern volatile uint32_t g_usb_tud_task_call_count_raw;
    extern volatile uint32_t g_usb_available_poll_count_raw;
    extern volatile uint32_t g_usb_rx_read_call_count_raw;
    extern volatile uint32_t g_usb_rx_byte_count_raw;
    extern volatile uint32_t g_usb_frame_complete_count_raw;
    extern volatile uint32_t g_usb_dispatch_call_count_raw;
    extern volatile uint32_t g_usb_write_response_count_raw;
    extern volatile uint32_t g_usb_write_spin_count_raw;
    extern volatile uint32_t g_usb_write_last_success_bytes_raw;
    extern volatile uint32_t g_usb_last_payload_error_code_raw;
    g_runner.stream.telemetry.recovery_fail_stage = 1u;
    g_runner.stream.telemetry.recovery_fail_main_loop_count = g_main_loop_heartbeat_count_raw;
    g_runner.stream.telemetry.recovery_fail_usb_task_count = g_usb_protocol_task_call_count_raw;
    g_runner.stream.telemetry.recovery_fail_tud_task_count = g_usb_tud_task_call_count_raw;
    g_runner.stream.telemetry.recovery_fail_usb_available_poll_count = g_usb_available_poll_count_raw;
    g_runner.stream.telemetry.recovery_fail_usb_rx_read_call_count = g_usb_rx_read_call_count_raw;
    g_runner.stream.telemetry.recovery_fail_usb_rx_byte_count = g_usb_rx_byte_count_raw;
    g_runner.stream.telemetry.recovery_fail_usb_frame_complete_count = g_usb_frame_complete_count_raw;
    g_runner.stream.telemetry.recovery_fail_usb_dispatch_call_count = g_usb_dispatch_call_count_raw;
    g_runner.stream.telemetry.recovery_fail_usb_write_response_count = g_usb_write_response_count_raw;
    g_runner.stream.telemetry.recovery_fail_usb_write_spin_count = g_usb_write_spin_count_raw;
    g_runner.stream.telemetry.recovery_fail_usb_last_write_success_bytes = g_usb_write_last_success_bytes_raw;
    g_runner.stream.telemetry.recovery_fail_usb_last_payload_error_code = g_usb_last_payload_error_code_raw;
    uint32_t runner_flags = 0u;
    if (g_runner.active) runner_flags |= 1u << 0;
    if (g_runner.draining) runner_flags |= 1u << 1;
    if (g_runner.streaming) runner_flags |= 1u << 2;
    if (g_runner.reduced_density_streaming) runner_flags |= 1u << 3;
    if (g_runner.stream.active) runner_flags |= 1u << 4;
    if (g_runner.core1_started) runner_flags |= 1u << 5;
    if (g_runner.stream.shared.stop_requested) runner_flags |= 1u << 6;
    g_runner.stream.telemetry.fail_snapshot_status_state = (uint32_t)stpg_status_get_state();
    g_runner.stream.telemetry.fail_snapshot_runner_flags = runner_flags;
    g_runner.stream.telemetry.fail_snapshot_shared_fill_request_slot =
        (uint32_t)(g_runner.stream.shared.fill_request_slot + 1);
    g_runner.stream.telemetry.fail_snapshot_shared_fill_error =
        (uint32_t)g_runner.stream.shared.fill_error;
    g_runner.stream.telemetry.fail_snapshot_from_irq = from_irq ? 1u : 0u;
    g_runner.stream.telemetry.recovery_fail_stage = 2u;
    g_runner.stream.telemetry.recovery_fail_dma_irq0_pending =
        (dma_hw->ints0 >> (uint)g_runner.output_dma_channel) & 0x1u;
    if (g_runner.sample_dma_channel >= 0) {
        g_runner.stream.telemetry.recovery_fail_dma_irq0_pending |=
            (((dma_hw->ints0 >> (uint)g_runner.sample_dma_channel) & 0x1u) << 1u);
    }
    if (g_runner.sample_sched_dma_channel >= 0) {
        g_runner.stream.telemetry.recovery_fail_dma_irq0_pending |=
            (((dma_hw->ints0 >> (uint)g_runner.sample_sched_dma_channel) & 0x1u) << 2u);
    }
    g_runner.stream.telemetry.recovery_fail_dma_irq0_enabled = irq_is_enabled(DMA_IRQ_0) ? 1u : 0u;
    stpg_runner_finish_hw_hold_outputs();
    stpg_buffer_snapshot_stream_telemetry();
    g_runner.stream.telemetry.recovery_fail_stage = 3u;
    g_runner.pending_noready_prod_recheck = false;
    stpg_runner_clear_hold_state();
    g_runner.active = false;
    g_runner.draining = false;
    g_runner.streaming = false;
    g_runner.reduced_density_streaming = false;
    g_runner.stream.active = false;
    g_runner.drain_deadline_us = 0u;
    g_runner.sample_event_count = 0u;
    g_runner.stream.shared.stop_requested = true;
    __sev();
    g_runner.stream_last_progress_us = 0u;
    g_runner.stream_last_dma_remaining = 0u;
    g_runner.stream_last_slot_index = 0u;
    g_runner.stream_last_queued_event_count = 0u;
    /* Hardware is quiescent and this run is inactive. Consume any IRQ failure
     * latched while a direct failure was in progress; it must not finalize
     * this run again and overwrite its terminal result/snapshot next turn. */
    g_runner.pending_stream_fail_error = ERR_NONE;
    g_runner.pending_stream_fail_from_irq = false;
    /* Preserve a staging overrun instead of clearing its rejection evidence. */
    stpg_status_set_result_flags(stpg_samples_result_flags() & STPG_RESULT_FLAG_SAMPLE_OVERFLOW);
    stpg_status_set_fail_count(0u);
    stpg_status_set_sample_count(0u);
    g_runner.stream.telemetry.recovery_fail_stage = 4u;
    stpg_status_set_error(err);
    g_runner.stream.telemetry.recovery_fail_stage = 5u;
}

static void stpg_runner_request_stream_fail(stpg_err_t err, bool from_irq) {
    if (g_runner.pending_stream_fail_error != ERR_NONE) {
        return;
    }
    g_runner.pending_stream_fail_error = err;
    g_runner.pending_stream_fail_from_irq = from_irq;
}

static stpg_err_t __no_inline_not_in_flash_func(stpg_runner_stream_promote_ready_slots)(bool from_irq) {
    for (uint32_t index = 0u; index < STPG_STREAM_SLOT_COUNT; ++index) {
        if (g_runner.stream.slots[index].state != STPG_STREAM_SLOT_FILLING
            || !g_runner.stream.shared.slot_ready[index]) {
            continue;
        }
        /* The producer publishes slot payload and metadata before READY. */
        __dmb();
        g_runner.stream.slots[index].event_count = g_runner.stream.shared.slot_event_count[index];
        g_runner.stream.slots[index].word_count = g_runner.stream.shared.slot_word_count[index];
        g_runner.stream.slots[index].sample_count = g_runner.stream.shared.slot_sample_count[index];
        g_runner.stream.slots[index].p4_explicit_wait_end_vector_index =
            g_runner.stream.shared.slot_p4_explicit_wait_end_vector_index[index];
        g_runner.stream.slots[index].p4_explicit_wait_end_present =
            g_runner.stream.shared.slot_p4_explicit_wait_end_present[index];
        g_runner.stream.slots[index].eof = g_runner.stream.shared.slot_eof[index];
#if STPG_STREAM_FAIL_STOP
        if (g_runner.fail_stop_enabled) {
            const stpg_err_t metadata_err = stpg_fail_stop_metadata_publish(
                &g_runner.stream.slots[index].fail_stop_metadata,
                g_stpg_runner_stream_run_sequence,
                g_runner.stream.shared.slot_metadata_sample_start[index],
                g_runner.stream.shared.slot_metadata_count[index]);
            if (metadata_err != ERR_NONE) {
                if (from_irq || g_runner.fail_stop_enabled) {
                    stpg_runner_request_stream_fail(metadata_err, true);
                } else {
                    stpg_runner_fail_streaming(metadata_err, false);
                }
                return metadata_err;
            }
        }
#endif
        if (!stpg_runner_stream_slot_transition(
                &g_runner.stream.slots[index],
                STPG_STREAM_SLOT_READY
            )) {
#if STPG_STREAM_FAIL_STOP
            const stpg_err_t state_err = g_runner.fail_stop_enabled
                ? ERR_FAIL_STOP_METADATA_OVERRUN : ERR_BAD_STATE;
#else
            const stpg_err_t state_err = ERR_BAD_STATE;
#endif
            if (from_irq) {
                stpg_runner_request_stream_fail(state_err, true);
            } else {
                stpg_runner_fail_streaming(state_err, false);
            }
            return state_err;
        }
#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
        stpg_refill_trace_mark_promoted(&g_runner.stream.slots[index]);
#endif
    }
    return ERR_NONE;
}

#if STPG_STREAM_FAIL_STOP
static stpg_err_t stpg_runner_check_stream_fail_stop_samples(uint32_t *out_fail_event_index) {
    if (out_fail_event_index != 0) {
        *out_fail_event_index = 0u;
    }
    if (!g_runner.fail_stop_enabled) {
        return ERR_NONE;
    }

    const uint32_t captured_sample_count = stpg_runner_captured_sample_count();
    uint32_t next_sample_index = g_runner.fail_stop_next_sample_index;
    uint32_t next_event_index = g_runner.fail_stop_next_event_index;
    const uint32_t logical_mask = stpg_samples_active_pins() == STPG_ACTIVE_PINS_V11
        ? 0x00FFFFFFu : 0x0000FFFFu;

    for (;;) {
        bool made_progress = false;
        for (uint32_t slot_index = 0u; slot_index < STPG_STREAM_SLOT_COUNT; ++slot_index) {
            stpg_fail_stop_metadata_t *metadata =
                &g_runner.stream.slots[slot_index].fail_stop_metadata;
            if (metadata->state != STPG_FAIL_STOP_METADATA_PUBLISHED) {
                continue;
            }
            if (metadata->generation != g_stpg_runner_stream_run_sequence) {
                return ERR_FAIL_STOP_METADATA_OVERRUN;
            }
            uint32_t metadata_sample_cursor = 0u;
            const stpg_err_t cursor_err = stpg_fail_stop_metadata_sample_cursor(
                metadata,
                &metadata_sample_cursor);
            if (cursor_err != ERR_NONE) {
                return cursor_err;
            }
            /* A published slice is consumable only at the current global
             * sample frontier.  A partially consumed slice resumes at
             * sample_start + consumed_count; comparing the immutable slice
             * start here would misclassify every partial consume as metadata
             * corruption on the next scan.  This also keeps zero-sample
             * slices from being released ahead of an unobserved gap. */
            if (metadata_sample_cursor != next_sample_index) {
                if (metadata_sample_cursor < next_sample_index) {
                    return ERR_FAIL_STOP_METADATA_OVERRUN;
                }
                continue;
            }

            const uint32_t remaining = metadata->sample_count - metadata->consumed_count;
            if (remaining == 0u) {
                const stpg_err_t consume_err = stpg_fail_stop_metadata_consume(metadata, 0u);
                if (consume_err != ERR_NONE) {
                    return consume_err;
                }
                if (stpg_fail_stop_metadata_take_deferred_refill(metadata)) {
                    const stpg_err_t refill_err = stpg_runner_request_stream_fill_safe(slot_index);
                    if (refill_err != ERR_NONE) {
                        return refill_err;
                    }
                }
                made_progress = true;
                continue;
            }
            if (metadata_sample_cursor >= captured_sample_count) {
                continue;
            }

            uint32_t available = captured_sample_count - metadata_sample_cursor;
            if (available > remaining) {
                available = remaining;
            }
            const uint32_t metadata_offset = slot_index * STPG_STREAM_BUFFER_EVENTS
                + metadata->consumed_count;
            const ExecEvent *events = stpg_buffer_stream_compare_events_at(metadata_offset);
            const uint32_t *event_indices =
                stpg_buffer_stream_compare_event_indices_at(metadata_offset);
            if (events == 0 || event_indices == 0) {
                return ERR_FAIL_STOP_METADATA_OVERRUN;
            }

            uint32_t processed = 0u;
            for (; processed < available; ++processed) {
                const ExecEvent *event = &events[processed];
                if ((event->flags & STPG_EVF_SAMPLE_POINT) == 0u) {
                    return ERR_FAIL_STOP_METADATA_OVERRUN;
                }
                const uint32_t event_index = event_indices[processed];
                if (event_index < next_event_index) {
                    return ERR_FAIL_STOP_METADATA_OVERRUN;
                }
                const uint32_t actual = stpg_samples_value_at(
                    metadata_sample_cursor + processed);
                const bool mismatch = (event->flags & STPG_EVF_COMPARE_POINT) != 0u
                    && stpg_compare_check_value(
                        event_index,
                        actual,
                        event->expect_value & logical_mask,
                        event->sample_mask & logical_mask);
                next_sample_index += 1u;
                next_event_index = event_index + 1u;
                if (mismatch && stpg_compare_fail_count() >= g_runner.fail_stop_limit) {
                    const stpg_err_t consume_err =
                        stpg_fail_stop_metadata_consume(metadata, processed + 1u);
                    if (consume_err != ERR_NONE) {
                        return consume_err;
                    }
                    g_runner.fail_stop_next_sample_index = next_sample_index;
                    g_runner.fail_stop_next_event_index = next_event_index;
                    if (out_fail_event_index != 0) {
                        *out_fail_event_index = event_index;
                    }
                    return ERR_COMPARE_FAIL_STOP;
                }
            }

            const stpg_err_t consume_err = stpg_fail_stop_metadata_consume(metadata, processed);
            if (consume_err != ERR_NONE) {
                return consume_err;
            }
            if (stpg_fail_stop_metadata_take_deferred_refill(metadata)) {
                const stpg_err_t refill_err = stpg_runner_request_stream_fill_safe(slot_index);
                if (refill_err != ERR_NONE) {
                    return refill_err;
                }
            }
            made_progress = processed != 0u;
            if (made_progress) {
                break;
            }
        }
        if (!made_progress) {
            break;
        }
    }

    g_runner.fail_stop_next_sample_index = next_sample_index;
    g_runner.fail_stop_next_event_index = next_event_index;
    if (next_sample_index < captured_sample_count) {
        return ERR_FAIL_STOP_METADATA_OVERRUN;
    }
    return ERR_NONE;
}
#endif

static bool __no_inline_not_in_flash_func(stpg_runner_stream_slot_is_empty_eof)(const stpg_stream_slot_t *slot) {
    return slot->eof && slot->event_count == 0u
        && slot->word_count == 0u && slot->sample_count == 0u;
}

static stpg_err_t __no_inline_not_in_flash_func(stpg_runner_arm_ready_output_slot)(uint32_t slot_index) {
    stpg_stream_slot_t *slot = &g_runner.stream.slots[slot_index];
    if (slot->state != STPG_STREAM_SLOT_READY || slot->event_count == 0u || slot->word_count == 0u
        || g_runner.output_dma_channel < 0 || g_runner.output_dma_spare_channel < 0) {
        return ERR_BAD_STATE;
    }
    const uint active_channel = (uint)g_runner.output_dma_channel;
    const uint idle_channel = (uint)g_runner.output_dma_spare_channel;
    slot->stream_event_start = g_runner.stream.queued_event_count;
    slot->stream_event_end = slot->stream_event_start + slot->event_count;
    /* The successor must not return to a predecessor whose payload has not
     * been reprogrammed yet. If its predecessor IRQ is delayed, both prepared
     * transfers may finish: a reverse chain would reload the old predecessor
     * count while retaining its advanced read address. Each rearm programs
     * its new successor first, then enables only the active -> successor link
     * below. CHAIN_TO=self disables chaining; TRANS_COUNT mode is unchanged. */
    const uint chain_to_channel = idle_channel;
    stpg_runner_configure_chained_output_dma(
        idle_channel,
        chain_to_channel,
        slot->words,
        slot->word_count,
        false
    );
    /* Neutralization changes the active channel's CHAIN_TO to self.
     * Reconfigure the reverse link too; programming only the spare channel
     * would leave the next hardware handoff disabled. */
    hw_write_masked(
        &dma_hw->ch[active_channel].al1_ctrl,
        idle_channel << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB,
        DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS
    );
    __dmb();
    if (!stpg_runner_stream_slot_transition(slot, STPG_STREAM_SLOT_ARMED)) {
        return ERR_BAD_STATE;
    }
#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
    stpg_refill_trace_mark_armed(slot);
#endif
    g_runner.output_dma_armed_slot_index = (int32_t)slot_index;
    g_runner.stream.telemetry.p3_chain_arm_count += 1u;
    if (slot->eof) {
        g_runner.stream.telemetry.p3_terminal_chain_neutralize_count += 1u;
    }
    g_runner.stream.telemetry.p3_telemetry_sequence = g_runner.stream.telemetry.p3_stream_run_sequence;
    return ERR_NONE;
}

static void __no_inline_not_in_flash_func(stpg_runner_neutralize_active_output_dma_chain)(void) {
    if (!g_runner.output_dma_chain_enabled || g_runner.output_dma_channel < 0) {
        return;
    }
    const uint active_channel = (uint)g_runner.output_dma_channel;
    hw_write_masked(
        &dma_hw->ch[active_channel].al1_ctrl,
        active_channel << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB,
        DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS
    );
    /* The IRQ owns completion until this point.  After neutralization, the
     * main loop may only start a replacement after observing DMA quiescence. */
    g_runner.output_dma_chain_enabled = false;
    g_runner.stream.telemetry.p3_no_ready_chain_neutralize_count += 1u;
    g_runner.stream.telemetry.p3_telemetry_sequence = g_runner.stream.telemetry.p3_stream_run_sequence;
}

static void stpg_runner_reject_chained_output(uint32_t site, bool target_busy,
                                             uint32_t target_remaining) {
    /* Failure-only Flash code: retain decision-time observations before the
     * existing cleanup. Do not allocate, retry, service USB or restart DMA. */
    if (g_chained_irq_reject[0] == STPG_CHAIN_IRQ_REJECT_NONE) {
        const uint32_t completed = g_runner.stream.active_slot_index;
        const int32_t armed = g_runner.output_dma_armed_slot_index;
        const bool completed_valid = completed < STPG_STREAM_SLOT_COUNT;
        const bool armed_valid = armed >= 0 && (uint32_t)armed < STPG_STREAM_SLOT_COUNT;
        const uint32_t completed_state = completed_valid
            ? (uint32_t)g_runner.stream.slots[completed].state : 255u;
        const uint32_t armed_state = armed_valid
            ? (uint32_t)g_runner.stream.slots[(uint32_t)armed].state : 255u;
        g_chained_irq_reject[1] = ERR_BAD_STATE;
        g_chained_irq_reject[2] = completed;
        g_chained_irq_reject[3] = (uint32_t)armed;
        g_chained_irq_reject[4] =
            (site != STPG_CHAIN_IRQ_REJECT_SLOT_INDEX ? 1u : 0u)
            | (target_busy ? 2u : 0u)
            | (armed_valid && g_runner.stream.slots[(uint32_t)armed].eof ? 4u : 0u)
            | (g_runner.reduced_density_streaming ? 8u : 0u)
            | (g_runner.output_dma_chain_enabled ? 16u : 0u)
            | ((completed_state & 255u) << 8u) | ((armed_state & 255u) << 16u);
        g_chained_irq_reject[5] = target_remaining;
        __dmb();
        g_chained_irq_reject[0] = site; /* Publish only the complete first record. */
    }
    const uint target_channel = (uint)g_runner.output_dma_spare_channel;
    if (g_runner.output_dma_spare_channel >= 0 && target_channel < NUM_DMA_CHANNELS) {
        dma_channel_set_irq0_enabled(target_channel, false);
        dma_channel_acknowledge_irq0(target_channel);
    }
    stpg_runner_neutralize_active_output_dma_chain();
    stpg_runner_request_stream_fail(ERR_BAD_STATE, true);
}

bool stpg_runner_chained_irq_diagnostics(uint32_t words[6]) {
    if (words == NULL) return false;
    const uint32_t site = g_chained_irq_reject[0];
    __dmb();
    words[0] = site;
    for (uint32_t i = 1u; i < 6u; ++i) {
        words[i] = site != STPG_CHAIN_IRQ_REJECT_NONE ? g_chained_irq_reject[i] : 0u;
    }
    return true;
}

static bool __no_inline_not_in_flash_func(stpg_runner_handle_chained_output_completion)(void) {
    if (!g_runner.output_dma_chain_enabled || g_runner.output_dma_channel < 0) {
        return false;
    }
    const uint completed_channel = (uint)g_runner.output_dma_channel;
    if (!dma_channel_get_irq0_status(completed_channel)) {
        return false;
    }
    dma_channel_acknowledge_irq0(completed_channel);
    g_runner.stream.telemetry.p3_handoff_owner = STPG_OUTPUT_HANDOFF_OWNER_IRQ;
    if (!g_runner.active || g_runner.draining || !g_runner.streaming) {
        return true;
    }

    const uint32_t completed_index = g_runner.stream.active_slot_index;
    if (completed_index >= STPG_STREAM_SLOT_COUNT
        || g_runner.output_dma_armed_slot_index < -1
        || g_runner.output_dma_armed_slot_index >= (int32_t)STPG_STREAM_SLOT_COUNT) {
        stpg_runner_reject_chained_output(STPG_CHAIN_IRQ_REJECT_SLOT_INDEX, false, UINT32_MAX);
        return true;
    }
    stpg_stream_slot_t *completed_slot = &g_runner.stream.slots[completed_index];
    const uint32_t handler_start_us = time_us_32();
    const int32_t armed_index = g_runner.output_dma_armed_slot_index;
    if (armed_index < 0) {
        __dmb();
        const stpg_err_t fill_err = stpg_stream_find_fill_error();
        if (fill_err != ERR_NONE) {
            stpg_runner_record_dma_completion(
                STPG_DMA_TRACE_PATH_CHAIN_HANDOFF | STPG_DMA_TRACE_PATH_NO_READY,
                completed_index,
                -1,
                -1,
                0u
            );
            g_runner.stream.telemetry.underflow_count += 1u;
            g_runner.stream.telemetry.underflow_reason = STPG_UNDERFLOW_REASON_FILL_ERROR;
            stpg_runner_capture_underflow_snapshot(
                completed_index,
                -1,
                STPG_UNDERFLOW_REASON_FILL_ERROR
            );
            stpg_runner_request_stream_fail(fill_err, true);
            return true;
        }
        if (completed_slot->eof) {
            stpg_runner_record_dma_completion(
                STPG_DMA_TRACE_PATH_CHAIN_HANDOFF | STPG_DMA_TRACE_PATH_TERMINAL,
                completed_index,
                -1,
                -1,
                0u
            );
            stpg_runner_begin_draining(stpg_runner_compute_tail_ticks_from_words(
                completed_slot->words,
                completed_slot->event_count
            ));
            return true;
        }
        if (g_runner.p4_explicit_wait_enabled) {
            __dmb();
            if (stpg_runner_stream_promote_ready_slots(true) != ERR_NONE) {
                return true;
            }
            if (stpg_runner_stream_find_next_ready_slot(completed_index) >= 0) {
                if (dma_channel_is_busy(completed_channel)
                    || dma_hw->ch[completed_channel].transfer_count != 0u) {
                    stpg_runner_request_stream_fail(ERR_BAD_STATE, true);
                    return true;
                }
                stpg_runner_neutralize_active_output_dma_chain();
                stpg_runner_stream_swap_or_finish(true);
                return true;
            }
            if (stpg_runner_try_defer_production_noready(completed_index, true)) {
                return true;
            }
        }
        g_runner.stream.telemetry.p3_no_ready_fail_count += 1u;
        g_runner.stream.telemetry.p3_telemetry_sequence = g_runner.stream.telemetry.p3_stream_run_sequence;
        g_runner.stream.telemetry.underflow_count += 1u;
        g_runner.stream.telemetry.underflow_reason = STPG_UNDERFLOW_REASON_NO_READY_SLOT;
        stpg_runner_capture_underflow_snapshot(completed_index, -1, STPG_UNDERFLOW_REASON_NO_READY_SLOT);
        stpg_runner_request_stream_fail(ERR_RING_UNDERFLOW, true);
        return true;
    }

    const uint target_channel = (uint)g_runner.output_dma_spare_channel;
    stpg_stream_slot_t *armed_slot = &g_runner.stream.slots[(uint32_t)armed_index];
    bool target_busy = dma_channel_is_busy(target_channel);
    uint32_t target_remaining = dma_hw->ch[target_channel].transfer_count;
    stpg_stream_chained_dma_state_t target_dma_state =
        stpg_stream_classify_chained_dma(target_busy, target_remaining);
    if (target_dma_state == STPG_STREAM_CHAINED_DMA_INVALID) {
        /* BUSY and TRANS_COUNT are separate reads.  A short successor can
         * finish between them; observe once more before rejecting it. */
        __dmb();
        target_busy = dma_channel_is_busy(target_channel);
        target_remaining = dma_hw->ch[target_channel].transfer_count;
        target_dma_state = stpg_stream_classify_chained_dma(target_busy, target_remaining);
    }
    if (target_dma_state == STPG_STREAM_CHAINED_DMA_INVALID
        || completed_slot->state != STPG_STREAM_SLOT_ACTIVE
        || armed_slot->state != STPG_STREAM_SLOT_ARMED) {
        /* The successor IRQ is not owned until the channel/slot observation
         * is accepted.  Keep a rejected successor from becoming an unowned
         * IRQ source while the deferred failure reaches the main loop. */
        stpg_runner_reject_chained_output(
            target_dma_state == STPG_STREAM_CHAINED_DMA_INVALID
                ? STPG_CHAIN_IRQ_REJECT_DMA_OBSERVATION : STPG_CHAIN_IRQ_REJECT_SLOT_OWNERSHIP,
            target_busy, target_remaining
        );
        return true;
    }
    const bool target_already_complete =
        target_dma_state == STPG_STREAM_CHAINED_DMA_COMPLETE;
    if (target_already_complete
        && (!armed_slot->eof || g_runner.reduced_density_streaming)) {
        /*
         * A nonterminal early completion cannot be adopted safely here: it
         * may already have fired a further chain, and reduced-density output
         * also requires a schedule-DMA handoff before the slot runs.  Ack the
         * observed successor so an unowned IRQ cannot starve the main loop,
         * then fail closed through the existing deferred error path.
         */
        stpg_runner_reject_chained_output(
            !armed_slot->eof ? STPG_CHAIN_IRQ_REJECT_NONTERMINAL_COMPLETE
                            : STPG_CHAIN_IRQ_REJECT_REDUCED_COMPLETE,
            target_busy, target_remaining
        );
        return true;
    }
    if (!stpg_runner_stream_slot_transition(completed_slot, STPG_STREAM_SLOT_EMPTY)
        || !stpg_runner_stream_slot_transition(armed_slot, STPG_STREAM_SLOT_ACTIVE)) {
        /* A completed successor can have its own pending IRQ already.  Do
         * not leave that IRQ unowned when a slot transition fails. */
        stpg_runner_reject_chained_output(
            STPG_CHAIN_IRQ_REJECT_SLOT_TRANSITION,
            target_busy, target_remaining
        );
        return true;
    }

    g_runner.output_dma_channel = (int)target_channel;
    g_runner.output_dma_spare_channel = (int)completed_channel;
    g_runner.output_dma_armed_slot_index = -1;
    g_runner.stream.active_slot_index = (uint32_t)armed_index;
    const uint32_t path_flags = STPG_DMA_TRACE_PATH_CHAIN_HANDOFF
        | (armed_slot->eof ? STPG_DMA_TRACE_PATH_TERMINAL : 0u);
    stpg_runner_record_dma_completion(
        path_flags,
        completed_index,
        armed_index,
        armed_index,
        0u
    );
    g_runner.stream.queued_event_count += armed_slot->event_count;
    g_runner.event_count = g_runner.stream.queued_event_count;
    g_runner.tail_drain_ticks = stpg_runner_compute_tail_ticks_from_words(
        armed_slot->words,
        armed_slot->event_count
    );
    stpg_status_set_run_progress(g_runner.stream.queued_event_count);
    g_runner.stream.telemetry.p3_chain_completion_count += 1u;
    const uint32_t elapsed_us = time_us_32() - handler_start_us;
    if (elapsed_us > g_runner.stream.telemetry.p3_max_irq_to_arm_us) {
        g_runner.stream.telemetry.p3_max_irq_to_arm_us = elapsed_us;
    }
    g_runner.stream.telemetry.p3_telemetry_sequence = g_runner.stream.telemetry.p3_stream_run_sequence;
    stpg_runner_note_stream_progress();

#if STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF
    /* The chained output path is the normal steady-state path when the
     * startup prefill exposes a second ready slot.  It changes the active
     * output slot without entering stream_swap_or_finish(), so the F44
     * schedule-DMA handoff there was never reached.  Arm the schedule words
     * for the newly active slot at the same ownership boundary. */
    if (g_runner.reduced_density_streaming && armed_slot->sample_count > 0u) {
        const stpg_err_t schedule_err = stpg_runner_start_sample_sched_dma(
            stpg_runner_stream_sample_wait_buffer((uint32_t)armed_index),
            armed_slot->sample_count);
        if (schedule_err != ERR_NONE) {
            g_runner.stream.telemetry.underflow_reason = STPG_UNDERFLOW_REASON_STREAM_STALL;
            stpg_runner_request_stream_fail(schedule_err, true);
            return true;
        }
    }

#endif

    if (armed_slot->eof) {
        if (target_already_complete) {
            /*
             * A short terminal slot can finish before this predecessor IRQ
             * is serviced.  Its zero remaining count is a valid completion,
             * not a bad-state condition.  Consume the successor IRQ in this
             * ownership turn and enter the same bounded tail drain that its
             * later standalone IRQ would have started.
             */
            dma_channel_acknowledge_irq0(target_channel);
            g_runner.output_dma_chain_enabled = false;
            stpg_runner_record_dma_completion(
                STPG_DMA_TRACE_PATH_CHAIN_HANDOFF | STPG_DMA_TRACE_PATH_TERMINAL,
                (uint32_t)armed_index,
                -1,
                -1,
                0u
            );
            stpg_runner_begin_draining(stpg_runner_compute_tail_ticks_from_words(
                armed_slot->words,
                armed_slot->event_count
            ));
        }
        return true;
    }
    const stpg_err_t fill_err = stpg_runner_request_stream_fill_safe(completed_index);
    if (fill_err != ERR_NONE) {
        stpg_runner_request_stream_fail(fill_err, true);
        return true;
    }
    if (stpg_runner_stream_promote_ready_slots(true) != ERR_NONE) {
        return true;
    }
    const int32_t next_ready = stpg_runner_stream_find_next_ready_slot((uint32_t)armed_index);
    if (next_ready >= 0) {
        /* A full final data chunk can be followed by a metadata-only EOF.
         * It is not a DMA transfer. Keep it READY, disable the reverse chain,
         * and let the active data channel finish before the normal handoff
         * starts its existing tail drain. Never drain at this predecessor IRQ. */
        if (stpg_runner_stream_slot_is_empty_eof(&g_runner.stream.slots[(uint32_t)next_ready])) {
            stpg_runner_neutralize_active_output_dma_chain();
            return true;
        }
        const stpg_err_t arm_err = stpg_runner_arm_ready_output_slot((uint32_t)next_ready);
        if (arm_err != ERR_NONE) {
            stpg_runner_neutralize_active_output_dma_chain();
            stpg_runner_request_stream_fail(arm_err, true);
        }
    } else {
        stpg_runner_neutralize_active_output_dma_chain();
    }
    return true;
}

static void __no_inline_not_in_flash_func(stpg_runner_stream_swap_or_finish)(bool from_irq) {
    if (from_irq && g_runner.pending_noready_prod_recheck) {
        g_runner.stream.telemetry.p3_handoff_conflict_count += 1u;
        g_runner.stream.telemetry.p3_telemetry_sequence =
            g_runner.stream.telemetry.p3_stream_run_sequence;
        stpg_runner_request_stream_fail(ERR_BAD_STATE, true);
        return;
    }
    if (from_irq) {
        g_runner.stream.telemetry.p3_handoff_owner = STPG_OUTPUT_HANDOFF_OWNER_IRQ;
    }
    const uint32_t completed_index = g_runner.stream.active_slot_index;
    stpg_stream_slot_t *completed_slot = &g_runner.stream.slots[completed_index];
    if (completed_slot->eof) {
        stpg_runner_record_dma_completion(
            STPG_DMA_TRACE_PATH_TERMINAL
                | (from_irq ? 0u : STPG_DMA_TRACE_PATH_MAIN_LOOP_HANDOFF),
            completed_index,
            g_runner.output_dma_armed_slot_index,
            -1,
            0u
        );
        stpg_runner_begin_draining(stpg_runner_compute_tail_ticks_from_words(
            completed_slot->words,
            completed_slot->event_count
        ));
        return;
    }

    __dmb();
    const stpg_err_t fill_err = stpg_stream_find_fill_error();
    if (fill_err != ERR_NONE) {
        g_runner.stream.telemetry.underflow_reason = STPG_UNDERFLOW_REASON_FILL_ERROR;
        if (from_irq) {
            stpg_runner_request_stream_fail(fill_err, true);
        } else {
            stpg_runner_fail_streaming(fill_err, false);
        }
        return;
    }

    if (stpg_runner_stream_promote_ready_slots(from_irq) != ERR_NONE) {
        return;
    }

    const int32_t next_ready = stpg_runner_stream_find_next_ready_slot(completed_index);
    if (next_ready < 0) {
        if (stpg_runner_try_defer_production_noready(completed_index, from_irq)) {
            return;
        }
        stpg_runner_record_dma_completion(
            STPG_DMA_TRACE_PATH_NORMAL_RESTART | STPG_DMA_TRACE_PATH_NO_READY
                | (from_irq ? 0u : STPG_DMA_TRACE_PATH_MAIN_LOOP_HANDOFF),
            completed_index,
            g_runner.output_dma_armed_slot_index,
            -1,
            0u
        );
        g_runner.stream.telemetry.underflow_count += 1u;
        g_runner.stream.telemetry.underflow_reason = STPG_UNDERFLOW_REASON_NO_READY_SLOT;
        stpg_runner_capture_underflow_snapshot(
            completed_index,
            next_ready,
            STPG_UNDERFLOW_REASON_NO_READY_SLOT
        );
        if (from_irq) {
            stpg_runner_request_stream_fail(ERR_RING_UNDERFLOW, true);
        } else {
            stpg_runner_fail_streaming(ERR_RING_UNDERFLOW, false);
        }
        return;
    }
    const uint32_t next_index = (uint32_t)next_ready;
    stpg_stream_slot_t *next_slot = &g_runner.stream.slots[next_index];

    const uint32_t next_event_count = next_slot->event_count;
    const bool next_eof = next_slot->eof;
    if (stpg_runner_stream_slot_is_empty_eof(next_slot)) {
        stpg_runner_begin_draining(stpg_runner_compute_tail_ticks_from_words(
            completed_slot->words,
            completed_slot->event_count
        ));
        return;
    }
    if (next_event_count == 0u) {
        stpg_runner_record_dma_completion(
            STPG_DMA_TRACE_PATH_NORMAL_RESTART | STPG_DMA_TRACE_PATH_NO_READY
                | (from_irq ? 0u : STPG_DMA_TRACE_PATH_MAIN_LOOP_HANDOFF),
            completed_index,
            g_runner.output_dma_armed_slot_index,
            next_ready,
            0u
        );
        g_runner.stream.telemetry.underflow_count += 1u;
        g_runner.stream.telemetry.underflow_reason = STPG_UNDERFLOW_REASON_NO_READY_SLOT;
        stpg_runner_capture_underflow_snapshot(
            completed_index,
            next_ready,
            STPG_UNDERFLOW_REASON_NO_READY_SLOT
        );
        if (from_irq) {
            stpg_runner_request_stream_fail(ERR_RING_UNDERFLOW, true);
        } else {
            stpg_runner_fail_streaming(ERR_RING_UNDERFLOW, false);
        }
        return;
    }

    if (!stpg_runner_stream_slot_transition(completed_slot, STPG_STREAM_SLOT_EMPTY)
        || !stpg_runner_stream_slot_transition(next_slot, STPG_STREAM_SLOT_ACTIVE)) {
        if (from_irq) {
            stpg_runner_request_stream_fail(ERR_BAD_STATE, true);
        } else {
            stpg_runner_fail_streaming(ERR_BAD_STATE, false);
        }
        return;
    }

    const uint32_t restart_start_us = time_us_32();
    const uint32_t next_word_count = next_slot->word_count;
    const uint32_t next_sample_count = next_slot->sample_count;
    if (g_runner.reduced_density_streaming && next_sample_count > 0u) {
        const stpg_err_t schedule_err = stpg_runner_start_sample_sched_dma(
            stpg_runner_stream_sample_wait_buffer(next_index), next_sample_count);
        if (schedule_err != ERR_NONE) {
            g_runner.stream.telemetry.underflow_reason = STPG_UNDERFLOW_REASON_STREAM_STALL;
            if (from_irq) {
                stpg_runner_request_stream_fail(schedule_err, true);
            } else {
                stpg_runner_fail_streaming(schedule_err, false);
            }
            return;
        }
    }
    stpg_runner_start_output_dma(next_slot->words, next_word_count);
    const uint32_t restart_elapsed_us = time_us_32() - restart_start_us;
    next_slot->stream_event_start = g_runner.stream.queued_event_count;
    next_slot->stream_event_end = next_slot->stream_event_start + next_event_count;
    stpg_runner_record_dma_completion(
        STPG_DMA_TRACE_PATH_NORMAL_RESTART
            | (from_irq ? 0u : STPG_DMA_TRACE_PATH_MAIN_LOOP_HANDOFF),
        completed_index,
        g_runner.output_dma_armed_slot_index,
        next_ready,
        restart_elapsed_us
    );
    g_runner.stream.telemetry.swap_restart_total_us += restart_elapsed_us;
    if (restart_elapsed_us > g_runner.stream.telemetry.swap_restart_max_us) {
        g_runner.stream.telemetry.swap_restart_max_us = restart_elapsed_us;
    }

    g_runner.stream.active_slot_index = next_index;
    g_runner.stream.queued_event_count += next_event_count;
    g_runner.stream.telemetry.swap_count += 1u;
    if (from_irq) {
        g_runner.stream.telemetry.irq_swap_count += 1u;
    } else {
        g_runner.stream.telemetry.task_swap_count += 1u;
    }
    g_runner.event_count = g_runner.stream.queued_event_count;
    g_runner.tail_drain_ticks = stpg_runner_compute_tail_ticks_from_words(
        next_slot->words, next_event_count);
    stpg_status_set_run_progress(g_runner.stream.queued_event_count);
    stpg_runner_stream_update_min_ready_slots();
    stpg_runner_note_stream_progress();

    if (!next_eof) {
        const stpg_err_t request_err = stpg_runner_request_stream_fill_safe(completed_index);
        if (request_err != ERR_NONE) {
            if (from_irq) {
                stpg_runner_request_stream_fail(request_err, true);
            } else {
                stpg_runner_fail_streaming(request_err, false);
            }
        }
    }
}

static void __no_inline_not_in_flash_func(stpg_runner_dma_irq0_impl)(void) {
    if (g_runner.output_dma_channel >= 0
        && dma_channel_get_irq0_status((uint)g_runner.output_dma_channel)) {
        g_runner.stream.telemetry.p3_handoff_owner = STPG_OUTPUT_HANDOFF_OWNER_IRQ;
        g_runner.stream.telemetry.p3_irq_handoff_count += 1u;
    }
    if (stpg_runner_handle_chained_output_completion()) {
        return;
    }
    const uint dma_channel = (uint)g_runner.output_dma_channel;
    if (g_runner.output_dma_channel < 0 || !dma_channel_get_irq0_status(dma_channel)) {
        return;
    }

    dma_channel_acknowledge_irq0(dma_channel);
    if (g_runner.pending_noready_prod_recheck) {
        g_runner.stream.telemetry.p3_handoff_conflict_count += 1u;
        g_runner.stream.telemetry.p3_telemetry_sequence =
            g_runner.stream.telemetry.p3_stream_run_sequence;
        stpg_runner_request_stream_fail(ERR_BAD_STATE, true);
        return;
    }
    if (!g_runner.active || g_runner.draining) {
        return;
    }
    if (g_runner.streaming) {
        stpg_runner_stream_swap_or_finish(true);
        return;
    }

    stpg_runner_begin_draining(g_runner.tail_drain_ticks);
}

static void __no_inline_not_in_flash_func(stpg_runner_capture_dma_pio_irq_entry)(uint32_t *out_entry_us) {
    const PIO pio = stpg_runner_pio();
    const int channel = g_runner.output_dma_channel;
    const bool valid_channel = channel >= 0;
    const uint32_t channel_u = valid_channel ? (uint32_t)channel : 0u;
    const uint32_t pending = valid_channel
        && dma_channel_get_irq0_status(channel_u) ? 1u : 0u;
    g_runner.stream.telemetry.dma_pio_boundary_count += 1u;
    g_runner.stream.telemetry.dma_pio_last_entry_channel = valid_channel ? channel_u : UINT32_MAX;
    g_runner.stream.telemetry.dma_pio_last_entry_irq_pending = pending;
    g_runner.stream.telemetry.dma_pio_last_entry_busy = valid_channel
        && dma_channel_is_busy(channel_u) ? 1u : 0u;
    g_runner.stream.telemetry.dma_pio_last_entry_remaining = valid_channel
        ? dma_hw->ch[channel_u].transfer_count : 0u;
    g_runner.stream.telemetry.dma_pio_last_entry_fstat = pio->fstat;
    g_runner.stream.telemetry.dma_pio_last_entry_fdebug = pio->fdebug;
    g_runner.stream.telemetry.dma_pio_last_entry_flevel = pio->flevel;
    if (out_entry_us != 0) {
        *out_entry_us = time_us_32();
    }
}

static void __no_inline_not_in_flash_func(stpg_runner_capture_dma_pio_irq_exit)(uint32_t entry_us) {
    const PIO pio = stpg_runner_pio();
    const int channel = g_runner.output_dma_channel;
    const bool valid_channel = channel >= 0;
    const uint32_t channel_u = valid_channel ? (uint32_t)channel : 0u;
    g_runner.stream.telemetry.dma_pio_last_exit_channel = valid_channel ? channel_u : UINT32_MAX;
    g_runner.stream.telemetry.dma_pio_last_exit_busy = valid_channel
        && dma_channel_is_busy(channel_u) ? 1u : 0u;
    g_runner.stream.telemetry.dma_pio_last_exit_remaining = valid_channel
        ? dma_hw->ch[channel_u].transfer_count : 0u;
    g_runner.stream.telemetry.dma_pio_last_exit_fstat = pio->fstat;
    g_runner.stream.telemetry.dma_pio_last_exit_fdebug = pio->fdebug;
    g_runner.stream.telemetry.dma_pio_last_exit_flevel = pio->flevel;
    g_runner.stream.telemetry.dma_pio_last_handler_us = time_us_32() - entry_us;
}

static void __no_inline_not_in_flash_func(stpg_runner_record_dma_completion)(uint32_t path_flags,
                                                                                uint32_t completed_index,
                                                                                int32_t armed_index,
                                                                                int32_t next_ready,
                                                                                uint32_t restart_us) {
    if (completed_index >= STPG_STREAM_SLOT_COUNT) {
        return;
    }

    const stpg_stream_slot_t *completed_slot = &g_runner.stream.slots[completed_index];
    const uint32_t event_start = completed_slot->stream_event_start;
    const uint32_t event_end = completed_slot->stream_event_end;
    const bool chunk_boundary = event_end != 0u
        && (event_end % STPG_STREAM_BUFFER_EVENTS) == 0u;
    if (chunk_boundary) {
        path_flags |= STPG_DMA_TRACE_PATH_CHUNK_BOUNDARY;
    }

    const uint32_t completion_index =
        g_runner.stream.telemetry.dma_completion_trace_count + 1u;
    const uint32_t trace_index = g_runner.stream.telemetry.dma_completion_trace_count
        < STPG_DMA_COMPLETION_TRACE_CAPACITY
        ? g_runner.stream.telemetry.dma_completion_trace_count
        : (STPG_DMA_COMPLETION_TRACE_CAPACITY - 1u);
    if (g_runner.stream.telemetry.dma_completion_trace_count
        >= STPG_DMA_COMPLETION_TRACE_CAPACITY) {
        g_runner.stream.telemetry.dma_completion_trace_overflow += 1u;
    } else {
        g_runner.stream.telemetry.dma_completion_trace_count += 1u;
    }

    volatile stpg_dma_completion_trace_t *entry =
        &g_runner.stream.telemetry.dma_completion_trace[trace_index];
    entry->completion_index = completion_index;
    entry->chunk_index = event_end / STPG_STREAM_BUFFER_EVENTS;
    entry->event_start = event_start;
    entry->event_end = event_end;
    entry->boundary_event = chunk_boundary ? event_end : 0u;
    entry->path_flags = path_flags;
    entry->completed_slot = completed_index + 1u;
    entry->armed_slot = armed_index >= 0 ? (uint32_t)armed_index + 1u : 0u;
    entry->next_ready_slot = next_ready >= 0 ? (uint32_t)next_ready + 1u : 0u;
    entry->restart_us = restart_us;
    entry->handler_us = (path_flags & STPG_DMA_TRACE_PATH_MAIN_LOOP_HANDOFF) != 0u
        ? g_runner.stream.telemetry.p3_main_loop_handoff_last_us
        : (g_dma_irq_entry_us_raw != 0u ? time_us_32() - g_dma_irq_entry_us_raw : 0u);
    entry->dma_remaining = g_runner.stream.telemetry.dma_pio_last_entry_remaining;
    entry->pio_flevel = g_runner.stream.telemetry.dma_pio_last_entry_flevel;
    entry->pio_fstat = g_runner.stream.telemetry.dma_pio_last_entry_fstat;
    g_runner.stream.telemetry.dma_completion_trace_last_path_flags = path_flags;
    if (chunk_boundary) {
        g_runner.stream.telemetry.dma_completion_trace_boundary_count += 1u;
    }
    if (restart_us > g_runner.stream.telemetry.dma_completion_trace_max_restart_us) {
        g_runner.stream.telemetry.dma_completion_trace_max_restart_us = restart_us;
    }
}

static void __no_inline_not_in_flash_func(stpg_runner_dma_irq0)(void) {
    uint32_t entry_us = 0u;
    stpg_runner_capture_dma_pio_irq_entry(&entry_us);
    g_dma_irq_entry_us_raw = entry_us;
    stpg_runner_dma_irq0_impl();
    stpg_runner_capture_dma_pio_irq_exit(entry_us);
    g_dma_irq_entry_us_raw = 0u;
}

static void stpg_runner_p4_prearm_diagnostic_task(void) {
    if (!g_runner.p4_explicit_wait_enabled || !g_runner.active || g_runner.draining
        || !g_runner.streaming || g_runner.output_dma_channel < 0) {
        return;
    }
    const uint32_t active_slot_index = g_runner.stream.active_slot_index;
    if (active_slot_index >= STPG_STREAM_SLOT_COUNT) {
        return;
    }
    const stpg_stream_slot_t *active_slot = &g_runner.stream.slots[active_slot_index];
    if (!active_slot->p4_explicit_wait_end_present) {
        return;
    }

    const uint active_channel = (uint)g_runner.output_dma_channel;
    const bool active_busy = dma_channel_is_busy(active_channel);
    const uint32_t active_remaining = dma_hw->ch[active_channel].transfer_count;
    g_runner.stream.telemetry.p4_diag_marker_slot = active_slot_index + 1u;
    g_runner.stream.telemetry.p4_diag_marker_vector_index =
        active_slot->p4_explicit_wait_end_vector_index;
    g_runner.stream.telemetry.p4_diag_observation_count += 1u;
    g_runner.stream.telemetry.p4_diag_active_dma_remaining = active_remaining;
    g_runner.stream.telemetry.p4_diag_active_dma_busy = active_busy ? 1u : 0u;

    __dmb();
    uint32_t promotable_count = 0u;
    int32_t promotable_slot = -1;
    for (uint32_t index = 0u; index < STPG_STREAM_SLOT_COUNT; ++index) {
        if (index == active_slot_index || g_runner.stream.slots[index].state != STPG_STREAM_SLOT_FILLING
            || !g_runner.stream.shared.slot_ready[index]) {
            continue;
        }
        if (g_runner.stream.shared.slot_word_count[index] == 0u && !g_runner.stream.shared.slot_eof[index]) {
            continue;
        }
        promotable_count += 1u;
        if (promotable_slot < 0) {
            promotable_slot = (int32_t)index;
        }
    }
    if (promotable_slot >= 0 && g_runner.stream.telemetry.p4_diag_promotable_slot == 0u) {
        g_runner.stream.telemetry.p4_diag_promotable_count = promotable_count;
        g_runner.stream.telemetry.p4_diag_promotable_slot = (uint32_t)(promotable_slot + 1);
        g_runner.stream.telemetry.p4_diag_active_dma_remaining = active_remaining;
        g_runner.stream.telemetry.p4_diag_active_dma_busy = active_busy ? 1u : 0u;
        g_runner.stream.telemetry.p4_diag_first_promotable_us = stpg_stream_elapsed_since_startup_us() + 1u;
        g_runner.stream.telemetry.p4_diag_ready_before_end =
            (active_busy && active_remaining != 0u) ? 1u : 0u;
    }
    if (g_runner.output_dma_armed_slot_index >= 0) {
        g_runner.stream.telemetry.p4_diag_spare_state = 2u;
    } else if (g_runner.output_dma_spare_channel >= 0
               && !dma_channel_is_busy((uint)g_runner.output_dma_spare_channel)) {
        g_runner.stream.telemetry.p4_diag_spare_state = 1u;
    } else {
        g_runner.stream.telemetry.p4_diag_spare_state = 0u;
    }
}

/*
 * Once the active channel has been neutralized, the producer may become
 * READY while the current DMA transfer is still draining.  Re-arm the spare
 * channel during that window so the next completion can use the hardware
 * chain again.  Interrupts are masked only for the ownership decision and
 * the bounded spare-channel programming.  The active channel is touched
 * only to restore its CHAIN_TO link; its transfer and the PIO are not
 * stopped or rewritten here.
 */
static void __no_inline_not_in_flash_func(stpg_runner_try_main_loop_rearm_ready_slot)(void) {
    if (!g_runner.active || g_runner.draining || !g_runner.streaming
        || g_runner.output_dma_chain_enabled
        || g_runner.output_dma_armed_slot_index >= 0
        || g_runner.output_dma_channel < 0
        || g_runner.output_dma_spare_channel < 0) {
        return;
    }

    const uint32_t irq_state = save_and_disable_interrupts();
    const uint active_channel = (uint)g_runner.output_dma_channel;
    const bool completion_pending = dma_channel_get_irq0_status(active_channel);
    const bool active_busy = dma_channel_is_busy(active_channel);
    const uint32_t active_remaining = dma_hw->ch[active_channel].transfer_count;
    if (!completion_pending && active_busy && active_remaining != 0u
        && !g_runner.output_dma_chain_enabled
        && g_runner.output_dma_armed_slot_index < 0) {
        const uint32_t rearm_start_us = time_us_32();
        __dmb();
        if (stpg_runner_stream_promote_ready_slots(false) == ERR_NONE) {
            const int32_t next_ready = stpg_runner_stream_find_next_ready_slot(
                g_runner.stream.active_slot_index);
            if (next_ready >= 0
                && !stpg_runner_stream_slot_is_empty_eof(&g_runner.stream.slots[(uint32_t)next_ready])
                && stpg_runner_arm_ready_output_slot((uint32_t)next_ready) == ERR_NONE) {
                __dmb();
                g_runner.output_dma_chain_enabled = true;
                g_runner.stream.telemetry.p3_main_loop_rearm_count += 1u;
                g_runner.stream.telemetry.p3_main_loop_rearm_last_us =
                    time_us_32() - rearm_start_us;
                g_runner.stream.telemetry.p3_handoff_owner =
                    STPG_OUTPUT_HANDOFF_OWNER_MAIN_LOOP;
                g_runner.stream.telemetry.p3_telemetry_sequence =
                    g_runner.stream.telemetry.p3_stream_run_sequence;
            }
        }
    }
    restore_interrupts(irq_state);
}

/*
 * A completion IRQ which was not observed can leave the output channel
 * idle with no pending IRQ bit.  This is the only main-loop path allowed to
 * perform the normal restart fallback.  The complete observation and the
 * handoff run with IRQs masked so a delayed completion cannot race the owner
 * decision and cause a second swap.
 */
static bool __no_inline_not_in_flash_func(stpg_runner_try_main_loop_dma_fallback)(void) {
    if (!g_runner.active || g_runner.draining || !g_runner.streaming
        || g_runner.output_dma_chain_enabled
        || g_runner.output_dma_armed_slot_index >= 0
        || g_runner.output_dma_channel < 0) {
        return false;
    }

    const uint32_t irq_state = save_and_disable_interrupts();
    const uint output_channel = (uint)g_runner.output_dma_channel;
    const bool eligible = !g_runner.output_dma_chain_enabled
        && g_runner.output_dma_armed_slot_index < 0
        && !dma_channel_get_irq0_status(output_channel)
        && !dma_channel_is_busy(output_channel)
        && dma_hw->ch[output_channel].transfer_count == 0u;
    if (eligible) {
        g_runner.stream.telemetry.p3_main_loop_idle_fallback_count += 1u;
        g_runner.stream.telemetry.p3_handoff_owner =
            STPG_OUTPUT_HANDOFF_OWNER_MAIN_LOOP;
        g_runner.stream.telemetry.p3_telemetry_sequence =
            g_runner.stream.telemetry.p3_stream_run_sequence;
        stpg_runner_stream_swap_or_finish(false);
    }
    restore_interrupts(irq_state);
    return eligible;
}

/*
 * Finite output runs do not use the streaming handoff path.  Their normal
 * completion owner is DMA_IRQ_0, but a completed channel can be observed by
 * the main loop after the IRQ bit has already been cleared or without the IRQ
 * handler having run.  Before this fallback existed, that left a finite run
 * permanently in STATE_RUNNING and prevented result finalization.
 *
 * The fallback is deliberately narrow: it requires an active non-streaming
 * run, a quiescent output DMA channel, and zero remaining transfers.  A
 * pending completion bit is routed through the same IRQ completion routine;
 * otherwise the already-drained channel enters the existing bounded tail
 * drain.  No output data is generated and no result state is asserted until
 * the normal drain deadline is reached by the main runner task.
 */
static bool __no_inline_not_in_flash_func(stpg_runner_try_main_loop_finite_completion)(void) {
    if (!g_runner.active || g_runner.draining || g_runner.streaming
        || g_runner.output_dma_channel < 0) {
        return false;
    }

    const uint32_t irq_state = save_and_disable_interrupts();
    const uint output_channel = (uint)g_runner.output_dma_channel;
    const bool completion_pending = dma_channel_get_irq0_status(output_channel);
    const bool output_busy = dma_channel_is_busy(output_channel);
    const uint32_t output_remaining = dma_hw->ch[output_channel].transfer_count;
    const bool eligible = !output_busy && output_remaining == 0u;
    if (eligible) {
        if (completion_pending) {
            stpg_runner_dma_irq0_impl();
        } else {
            stpg_runner_begin_draining(g_runner.tail_drain_ticks);
        }
    }
    restore_interrupts(irq_state);
    return eligible;
}

void stpg_runner_init(void) {
    memset(&g_runner, 0, sizeof(g_runner));
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
    memset(&g_terminal_replay, 0, sizeof(g_terminal_replay));
#endif
    g_runner.output_dma_channel = -1;
    g_runner.output_dma_spare_channel = -1;
    g_runner.output_dma_armed_slot_index = -1;
    g_runner.sample_dma_channel = -1;
    g_runner.sample_sched_dma_channel = -1;
    stpg_runner_clear_hold_state();
    stpg_stream_reset();
}

bool stpg_runner_service_samples(void) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    /* Core0 only. USB waits may call this narrow entry, never the full task.
     * No start, output handoff, replay or terminal flush can happen here. */
    if (g_runner.active && g_runner.streaming
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
        && !g_terminal_replay.pending
#endif
        && g_runner.pending_stream_fail_error == ERR_NONE) {
        const stpg_err_t staging_err =
            stpg_samples_service(stpg_runner_captured_sample_count);
        if (staging_err != ERR_NONE) {
            stpg_runner_fail_streaming(staging_err, false);
            return false;
        }
    }
#endif
    return true;
}

void stpg_runner_task(void) {
    if (g_runner.pending_start) {
        /* The USB receive handler has already queued RSP_OK for this RUN.
         * Do not enter PSRAM/PIO/DMA preparation until TinyUSB reports that
         * the response transfer completed.  This prevents the expensive
         * start path from starving the response on the same main-loop turn. */
        if (stpg_usb_run_response_pending()) {
            return;
        }
        const uint32_t run_flags = g_runner.pending_start_run_flags;
        const uint32_t start_event_index = g_runner.pending_start_event_index;
        const uint32_t max_events = g_runner.pending_start_max_events;
        const uint32_t aux_arg = g_runner.pending_start_aux_arg;
        g_runner.pending_start = false;
        const stpg_err_t start_err = stpg_runner_start(
            run_flags, start_event_index, max_events, aux_arg);
        if (start_err != ERR_NONE) {
            stpg_status_set_error(start_err);
            stpg_status_set_state(STATE_ERROR);
        } else if (stpg_runner_is_active()
                   || stpg_runner_is_draining()
                   || stpg_runner_hold_awaiting_next()) {
            stpg_status_clear_error();
            stpg_status_set_state(STATE_RUNNING);
        }
    }

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
    if (g_terminal_replay.pending) {
        stpg_runner_terminal_replay_task();
        return;
    }
#endif
    if (!stpg_runner_service_samples()) return;

    if (g_runner.pending_noready_prod_recheck) {
        const uint32_t completed_index = g_runner.pending_noready_prod_completed_slot;
        if (completed_index >= STPG_STREAM_SLOT_COUNT) {
            g_runner.pending_noready_prod_recheck = false;
            g_runner.stream.telemetry.p3_handoff_owner = STPG_OUTPUT_HANDOFF_OWNER_NONE;
            g_runner.stream.telemetry.prod_noready_recheck_fail_count += 1u;
            stpg_runner_fail_streaming(ERR_BAD_STATE, false);
            return;
        }

        const uint32_t now_us = time_us_32();
        if (g_runner.pending_noready_prod_recheck_start_us == 0u) {
            g_runner.pending_noready_prod_recheck_start_us = now_us;
        }
        g_runner.pending_noready_prod_recheck_iterations += 1u;
        const uint32_t recheck_iteration = g_runner.pending_noready_prod_recheck_iterations;
        const uint32_t elapsed_us = now_us - g_runner.pending_noready_prod_recheck_start_us;

        if (g_runner.output_dma_channel >= 0
            && dma_channel_get_irq0_status((uint)g_runner.output_dma_channel)) {
            g_runner.pending_noready_prod_recheck = false;
            g_runner.stream.telemetry.p3_handoff_owner = STPG_OUTPUT_HANDOFF_OWNER_NONE;
            g_runner.stream.telemetry.p3_handoff_conflict_count += 1u;
            g_runner.stream.telemetry.p3_telemetry_sequence =
                g_runner.stream.telemetry.p3_stream_run_sequence;
            stpg_runner_fail_streaming(ERR_BAD_STATE, false);
            return;
        }

        /* One observation per main-loop turn: no fixed delay and no busy wait
         * on the waveform-critical path. */
        g_runner.stream.telemetry.prod_noready_wait_step_us = 0u;
        g_runner.stream.telemetry.prod_noready_max_iterations = 8u;
        __dmb();
        const stpg_err_t fill_err = stpg_stream_find_fill_error();
        if (fill_err != ERR_NONE) {
            g_runner.pending_noready_prod_recheck = false;
            g_runner.stream.telemetry.prod_noready_last_iterations = recheck_iteration;
            g_runner.stream.telemetry.prod_noready_last_wait_us = elapsed_us;
            g_runner.stream.telemetry.prod_noready_recheck_fail_count += 1u;
            g_runner.stream.telemetry.underflow_reason = STPG_UNDERFLOW_REASON_FILL_ERROR;
            stpg_runner_capture_underflow_snapshot(completed_index, -1, STPG_UNDERFLOW_REASON_FILL_ERROR);
            stpg_runner_fail_streaming(fill_err, false);
            return;
        }

        if (stpg_runner_stream_promote_ready_slots(false) != ERR_NONE) {
            g_runner.pending_noready_prod_recheck = false;
            g_runner.stream.telemetry.prod_noready_last_iterations = recheck_iteration;
            g_runner.stream.telemetry.prod_noready_last_wait_us = elapsed_us;
            g_runner.stream.telemetry.prod_noready_recheck_fail_count += 1u;
            stpg_runner_fail_streaming(ERR_BAD_STATE, false);
            return;
        }

        const int32_t next_ready = stpg_runner_stream_find_next_ready_slot(completed_index);
        const bool dma_quiescent = g_runner.output_dma_channel >= 0
            && !dma_channel_is_busy((uint)g_runner.output_dma_channel)
            && dma_hw->ch[(uint)g_runner.output_dma_channel].transfer_count == 0u;
        g_runner.stream.telemetry.prod_noready_last_iterations = recheck_iteration;
        g_runner.stream.telemetry.prod_noready_last_wait_us = elapsed_us;
        if (next_ready >= 0 && dma_quiescent) {
            g_runner.pending_noready_prod_recheck = false;
            g_runner.pending_noready_prod_recheck_start_us = 0u;
            g_runner.pending_noready_prod_recheck_iterations = 0u;
            g_runner.stream.telemetry.p3_handoff_owner = STPG_OUTPUT_HANDOFF_OWNER_IRQ;
            g_runner.stream.telemetry.p3_main_loop_handoff_count += 1u;
            g_runner.stream.telemetry.p3_main_loop_handoff_last_us = elapsed_us;
            if (elapsed_us > g_runner.stream.telemetry.p3_main_loop_handoff_max_us) {
                g_runner.stream.telemetry.p3_main_loop_handoff_max_us = elapsed_us;
            }
            g_runner.stream.telemetry.prod_noready_recheck_success_count += 1u;
            stpg_runner_stream_swap_or_finish(false);
            return;
        }

        if (recheck_iteration < 8u && elapsed_us < 3200u) {
            return;
        }

        g_runner.pending_noready_prod_recheck = false;
        g_runner.stream.telemetry.p3_handoff_owner = STPG_OUTPUT_HANDOFF_OWNER_NONE;
        g_runner.stream.telemetry.prod_noready_recheck_fail_count += 1u;
        g_runner.stream.telemetry.underflow_count += 1u;
        g_runner.stream.telemetry.underflow_reason = STPG_UNDERFLOW_REASON_NO_READY_SLOT;
        stpg_runner_capture_underflow_snapshot(
            completed_index,
            -1,
            STPG_UNDERFLOW_REASON_NO_READY_SLOT
        );
        stpg_runner_fail_streaming(ERR_RING_UNDERFLOW, false);
        return;
    }

    if (g_runner.pending_stream_fail_error != ERR_NONE) {
        const stpg_err_t pending_err = g_runner.pending_stream_fail_error;
        const bool pending_from_irq = g_runner.pending_stream_fail_from_irq;
        g_runner.pending_stream_fail_error = ERR_NONE;
        g_runner.pending_stream_fail_from_irq = false;
#if STPG_STREAM_FAIL_STOP
        if (g_runner.streaming && g_runner.fail_stop_enabled
            && pending_err == ERR_FAIL_STOP_METADATA_OVERRUN) {
            stpg_runner_finish_streaming_fail_stop(
                pending_err,
                g_runner.fail_stop_next_event_index);
        } else
#endif
        stpg_runner_fail_streaming(pending_err, pending_from_irq);
        return;
    }

    if (g_runner.active && !g_runner.draining && g_runner.streaming && g_runner.output_dma_channel >= 0) {
#if STPG_STREAM_FAIL_STOP
        if (g_runner.fail_stop_enabled) {
            if (stpg_runner_stream_promote_ready_slots(false) != ERR_NONE) {
                return;
            }
            uint32_t fail_event_index = 0u;
            const stpg_err_t fail_stop_err =
                stpg_runner_check_stream_fail_stop_samples(&fail_event_index);
            if (fail_stop_err == ERR_COMPARE_FAIL_STOP) {
                stpg_runner_finish_streaming_fail_stop(
                    ERR_COMPARE_FAIL_STOP,
                    fail_event_index + 1u);
                return;
            }
            if (fail_stop_err != ERR_NONE) {
                stpg_runner_finish_streaming_fail_stop(
                    fail_stop_err,
                    g_runner.fail_stop_next_event_index);
                return;
            }
        }
#endif
        const uint dma_channel = (uint)g_runner.output_dma_channel;
        const uint32_t remaining = dma_hw->ch[dma_channel].transfer_count;
        if (remaining != g_runner.stream_last_dma_remaining
            || g_runner.stream.active_slot_index != g_runner.stream_last_slot_index
            || g_runner.stream.queued_event_count != g_runner.stream_last_queued_event_count) {
            stpg_runner_note_stream_progress();
        } else if ((uint32_t)(time_us_32() - g_runner.stream_last_progress_us)
                       > (g_runner.stream_timed_wait_seen
                           ? g_runner.stream_wait_budget_us : STPG_STREAM_STALL_TIMEOUT_US)
                   && stpg_runner_stream_stall_expired()) {
            g_runner.stream.telemetry.underflow_reason = STPG_UNDERFLOW_REASON_STREAM_STALL;
            stpg_runner_fail_streaming(ERR_TIMEOUT, false);
            return;
        }
        /* DMA completion belongs to IRQ0; main-loop recovery is limited to
         * the explicit rearm and idle-fallback ownership paths below. */
        stpg_runner_p4_prearm_diagnostic_task();
        stpg_runner_try_main_loop_rearm_ready_slot();
        (void)stpg_runner_try_main_loop_dma_fallback();
    }

    if (g_runner.active && !g_runner.draining && !g_runner.streaming) {
        if (g_runner.fail_stop_enabled) {
            uint32_t fail_event_index = 0u;
            const stpg_err_t fail_stop_err = stpg_runner_check_fail_stop_samples(&fail_event_index);
            if (fail_stop_err == ERR_COMPARE_FAIL_STOP) {
                stpg_runner_finish_direct_error(
                    ERR_COMPARE_FAIL_STOP,
                    fail_event_index + 1u
                );
                return;
            }
            if (fail_stop_err != ERR_NONE) {
                stpg_runner_finish_direct_error(fail_stop_err, g_runner.fail_stop_next_event_index);
                return;
            }
        }
        (void)stpg_runner_try_main_loop_finite_completion();
    }

    if (!g_runner.active || !g_runner.draining) {
        return;
    }

#if STPG_STREAM_FAIL_STOP
    if (g_runner.streaming && g_runner.fail_stop_enabled) {
        if (stpg_runner_stream_promote_ready_slots(false) != ERR_NONE) {
            return;
        }
        uint32_t fail_event_index = 0u;
        const stpg_err_t fail_stop_err =
            stpg_runner_check_stream_fail_stop_samples(&fail_event_index);
        if (fail_stop_err == ERR_COMPARE_FAIL_STOP) {
            stpg_runner_finish_streaming_fail_stop(
                ERR_COMPARE_FAIL_STOP,
                fail_event_index + 1u);
            return;
        }
        if (fail_stop_err != ERR_NONE) {
            stpg_runner_finish_streaming_fail_stop(
                fail_stop_err,
                g_runner.fail_stop_next_event_index);
            return;
        }
    }
#endif

    if (!g_runner.streaming && g_runner.fail_stop_enabled) {
        uint32_t fail_event_index = 0u;
        const stpg_err_t fail_stop_err = stpg_runner_check_fail_stop_samples(&fail_event_index);
        if (fail_stop_err == ERR_COMPARE_FAIL_STOP) {
            stpg_runner_finish_direct_error(
                ERR_COMPARE_FAIL_STOP,
                fail_event_index + 1u
            );
            return;
        }
        if (fail_stop_err != ERR_NONE) {
            stpg_runner_finish_direct_error(fail_stop_err, g_runner.fail_stop_next_event_index);
            return;
        }
    }

    if (time_us_64() < g_runner.drain_deadline_us) {
        return;
    }

    if (g_runner.streaming) {
        stpg_runner_finish_streaming_success();
        return;
    }

    stpg_runner_finish_hw_hold_outputs();
    uint32_t captured_sample_count = 0u;
    if (g_runner.sample_event_count > 0u && g_runner.sample_dma_channel >= 0) {
        const uint32_t remaining = dma_hw->ch[g_runner.sample_dma_channel].transfer_count;
        captured_sample_count = g_runner.sample_event_count - remaining;
    }
    stpg_samples_finish_run(captured_sample_count, false);
    if (!g_runner.fail_stop_enabled) {
        stpg_compare_run(stpg_buffer_events(), g_runner.event_count);
    }
    g_runner.active = false;
    g_runner.draining = false;
    stpg_runner_reset_fail_stop_context();
    g_runner.reduced_density_streaming = false;
    g_runner.drain_deadline_us = 0u;
    stpg_status_set_result_flags(stpg_samples_result_flags() | stpg_compare_result_flags());
    stpg_status_set_fail_count(stpg_compare_fail_count());
    stpg_status_set_sample_count(stpg_samples_sample_count());
    stpg_status_clear_error();
    if (g_runner.segment_end_hold) {
        stpg_runner_capture_terminal_hold_state();
        stpg_status_set_state(STATE_HOLD_AWAITING_NEXT);
        return;
    }
    stpg_status_set_state(STATE_DONE);
}

static stpg_err_t stpg_runner_start_streaming(bool p4_explicit_wait_enabled,
                                              bool preserve_gpio_state,
                                              bool segment_end_hold,
                                              bool fail_stop,
                                              uint32_t fail_stop_limit) {
#if !STPG_STREAM_FAIL_STOP
    if (fail_stop) {
        return ERR_UNSUPPORTED;
    }
#endif
    uint32_t entry_drive_value = 0u;
    uint32_t entry_drive_enable = 0u;
    if (preserve_gpio_state) {
        if (!g_runner.hold_awaiting_next || !g_runner.hold_valid) {
            return ERR_BAD_STATE;
        }
        entry_drive_value = g_runner.hold_drive_value;
        entry_drive_enable = g_runner.hold_drive_enable;
    } else {
        if (g_runner.hold_awaiting_next) {
            return ERR_BAD_STATE;
        }
        stpg_runner_clear_hold_state();
    }
    stpg_runner_reset_psram_mux_evidence();
    stpg_err_t err = stpg_runner_admit_streaming_psram_mux();
    if (err != ERR_NONE) {
        return err;
    }
    stpg_stream_reset();
    stpg_buffer_clear_stream_telemetry_snapshot();
    stpg_compare_reset();
    stpg_samples_reset();
    stpg_runner_reset_fail_stop_context();
    g_runner.segment_end_hold = segment_end_hold;
    g_runner.stream_entry_drive_value = entry_drive_value;
    g_runner.stream_entry_drive_enable = entry_drive_enable;
    stpg_rate_stream_state_t *const state = stpg_runner_stream_state();
    err = stpg_buffer_prepare_for_streaming(state, STPG_RUN_FLAG_STREAMING_POC);
    if (err != ERR_NONE) {
        return stpg_runner_abort_stream_startup(err);
    }
    uint32_t max_period = 0u;
    uint32_t max_first_time = 0u;
    if (state->rate_runtime_prepared) {
        for (uint32_t i = 0u; i < state->header.rate_count; ++i) {
            const stpg_rate_runtime_t *rate = &state->rate_runtime[i];
            if (rate->rate.period_ticks > max_period) max_period = rate->rate.period_ticks;
            if (rate->groups[0].time_ticks > max_first_time) max_first_time = rate->groups[0].time_ticks;
        }
    }
    g_runner.stream_wait_budget_us = stpg_stream_watchdog_budget_us(max_period, max_first_time);
    state->current_value = entry_drive_value;
    state->current_dir = entry_drive_enable;
    /* The previous hold has been consumed by this continuation.  Keep
     * hold_valid until successful terminal capture so an error path can still
     * report the last known state, but do not accept another continuation
     * while this run is active. */
    g_runner.hold_awaiting_next = false;
    /* A build without compare/sample streaming support remains fail-closed
     * for sampled runs.  The normal CMake build enables the bounded
     * Rate-Vector/PSRAM implementation; explicit legacy builds may still
     * disable it. */
#if !STPG_STREAM_COMPARE_STREAMING
    if (stpg_buffer_stream_has_samples()) {
        return stpg_runner_abort_stream_startup(ERR_UNSUPPORTED);
    }
#endif
    g_runner.reduced_density_streaming = state->reduced_density_compare_enabled;
#if STPG_STREAM_FAIL_STOP
    if (fail_stop) {
        if (g_runner.reduced_density_streaming
            || stpg_buffer_stream_expected_sample_count() > STPG_MAX_EVENT_COUNT) {
            return stpg_runner_abort_stream_startup(ERR_UNSUPPORTED);
        }
    }
#endif
    g_runner.fail_stop_enabled = fail_stop;
    g_runner.fail_stop_limit = fail_stop_limit;
    g_runner.fail_stop_terminalized = false;
    g_runner.fail_stop_next_event_index = 0u;
    g_runner.fail_stop_next_sample_index = 0u;
    g_runner.stream.active = true;
    g_runner.stream.queued_event_count = 0u;
    g_runner.stream.shared.stop_requested = false;
    g_runner.stream.shared.fill_request_slot = -1;
    g_runner.output_dma_chain_enabled = false;
    g_runner.pending_noready_prod_recheck = false;
    g_runner.pending_noready_prod_completed_slot = 0u;
    g_runner.pending_noready_prod_recheck_start_us = 0u;
    g_runner.pending_noready_prod_recheck_iterations = 0u;
    g_runner.stream.telemetry.p3_handoff_owner = STPG_OUTPUT_HANDOFF_OWNER_IRQ;
    g_runner.stream.telemetry.p3_handoff_conflict_count = 0u;
    g_runner.stream.telemetry.p3_irq_handoff_count = 0u;
    g_runner.stream.telemetry.p3_main_loop_handoff_count = 0u;
    g_runner.stream.telemetry.p3_main_loop_handoff_last_us = 0u;
    g_runner.stream.telemetry.p3_main_loop_handoff_max_us = 0u;
    g_runner.stream.telemetry.p3_main_loop_rearm_count = 0u;
    g_runner.stream.telemetry.p3_main_loop_rearm_last_us = 0u;
    g_runner.stream.telemetry.p3_main_loop_idle_fallback_count = 0u;
    g_runner.p4_explicit_wait_enabled = p4_explicit_wait_enabled;
    g_runner.output_dma_armed_slot_index = -1;
    g_stpg_runner_stream_run_sequence += 1u;
    if (g_stpg_runner_stream_run_sequence == 0u) {
        g_stpg_runner_stream_run_sequence = 1u;
    }
    g_runner.stream.telemetry.p3_stream_run_sequence = g_stpg_runner_stream_run_sequence;
    g_runner.stream.telemetry.p3_telemetry_sequence = 0u;
    g_stream_startup_t0_us = time_us_32();
    g_stream_request_set_us_raw = 0u;
    g_stream_request_clear_us_raw = 0u;
    g_stream_core1_first_poll_us_raw = 0u;
    g_stream_core1_first_negative_poll_us_raw = 0u;
    g_stream_core1_first_valid_poll_us_raw = 0u;
    g_stream_core1_last_stage_raw = 0u;
    g_stream_core1_after_stop_gate_us_raw = 0u;
    g_stream_core1_after_dmb_us_raw = 0u;
    g_stream_core1_after_slot_read_us_raw = 0u;
    g_stream_core1_last_polled_slot_raw = 0u;
    g_stream_core1_last_polled_fill_error_raw = 0u;
    g_stream_core1_last_polled_slot0_ready_raw = 0u;
    g_stream_core1_negative_poll_count_raw = 0u;
    g_stream_core1_valid_poll_count_raw = 0u;
    g_stream_core1_invalid_poll_count_raw = 0u;
    g_stream_core1_request_seen_raw = 0u;
    g_stream_core1_claim_raw = 0u;
    g_stream_core1_last_seen_slot_raw = 0u;

#ifdef PICO_ON_DEVICE
    multicore_reset_core1();
    multicore_launch_core1(stpg_stream_core1_entry);
    g_runner.core1_started = true;
#endif

    uint32_t startup_prefill_slots = 0u;
    uint32_t startup_prefill_total_us = 0u;
    uint32_t startup_prefill_attempted_slots = 0u;
    uint32_t startup_prefill_last_slot_index = 0u;
    uint32_t startup_prefill_break_reason = STPG_STARTUP_PREFILL_BREAK_NONE;
    uint32_t startup_prefill_wait_error = ERR_NONE;
    uint32_t startup_prefill_last_event_count = 0u;
    uint32_t startup_prefill_last_word_count = 0u;
    for (uint32_t slot_index = 0u; slot_index < STPG_STREAM_SLOT_COUNT; ++slot_index) {
        startup_prefill_attempted_slots = slot_index + 1u;
        startup_prefill_last_slot_index = slot_index;
        const uint32_t fill_start_us = time_us_32();
        err = stpg_runner_request_stream_fill_safe(slot_index);
        if (err != ERR_NONE) {
            return stpg_runner_abort_stream_startup(err);
        }
        err = stpg_stream_wait_for_ready(slot_index);
        startup_prefill_total_us += time_us_32() - fill_start_us;
        if (err != ERR_NONE) {
            startup_prefill_break_reason = STPG_STARTUP_PREFILL_BREAK_WAIT_ERROR;
            startup_prefill_wait_error = (uint32_t)err;
            g_runner.stream.telemetry.startup_prefill_attempted_slots = startup_prefill_attempted_slots;
            g_runner.stream.telemetry.startup_prefill_last_slot_index = startup_prefill_last_slot_index;
            g_runner.stream.telemetry.startup_prefill_break_reason = startup_prefill_break_reason;
            g_runner.stream.telemetry.startup_prefill_wait_error = startup_prefill_wait_error;
            g_runner.stream.telemetry.startup_prefill_last_event_count = startup_prefill_last_event_count;
            g_runner.stream.telemetry.startup_prefill_last_word_count = startup_prefill_last_word_count;
            g_runner.stream.telemetry.startup_prefill_total_us = startup_prefill_total_us;
            return stpg_runner_abort_stream_startup(err);
        }
        startup_prefill_last_event_count = g_runner.stream.slots[slot_index].event_count;
        startup_prefill_last_word_count = g_runner.stream.slots[slot_index].word_count;
        if (g_runner.stream.slots[slot_index].event_count == 0u) {
            startup_prefill_break_reason = STPG_STARTUP_PREFILL_BREAK_ZERO_EVENT;
            g_runner.stream.telemetry.startup_prefill_attempted_slots = startup_prefill_attempted_slots;
            g_runner.stream.telemetry.startup_prefill_last_slot_index = startup_prefill_last_slot_index;
            g_runner.stream.telemetry.startup_prefill_break_reason = startup_prefill_break_reason;
            g_runner.stream.telemetry.startup_prefill_wait_error = startup_prefill_wait_error;
            g_runner.stream.telemetry.startup_prefill_last_event_count = startup_prefill_last_event_count;
            g_runner.stream.telemetry.startup_prefill_last_word_count = startup_prefill_last_word_count;
            g_runner.stream.telemetry.startup_prefill_total_us = startup_prefill_total_us;
            if (startup_prefill_slots == 0u) {
                return stpg_runner_abort_stream_startup(ERR_PATTERN_INVALID);
            }
            break;
        }
        startup_prefill_slots += 1u;
        if (g_runner.stream.slots[slot_index].eof) {
            startup_prefill_break_reason = STPG_STARTUP_PREFILL_BREAK_EOF;
            break;
        }
    }
    if (startup_prefill_slots == 0u) {
        return stpg_runner_abort_stream_startup(ERR_PATTERN_INVALID);
    }

    g_runner.stream.telemetry.startup_prefill_slots = startup_prefill_slots;
    g_runner.stream.telemetry.startup_prefill_total_us = startup_prefill_total_us;
    g_runner.stream.telemetry.min_ready_slots_observed = startup_prefill_slots > 0u ? (startup_prefill_slots - 1u) : 0u;
    g_runner.stream.telemetry.startup_prefill_attempted_slots = startup_prefill_attempted_slots;
    g_runner.stream.telemetry.startup_prefill_last_slot_index = startup_prefill_last_slot_index;
    g_runner.stream.telemetry.startup_prefill_break_reason = startup_prefill_break_reason;
    g_runner.stream.telemetry.startup_prefill_wait_error = startup_prefill_wait_error;
    g_runner.stream.telemetry.startup_prefill_last_event_count = startup_prefill_last_event_count;
    g_runner.stream.telemetry.startup_prefill_last_word_count = startup_prefill_last_word_count;

    g_runner.stream.active_slot_index = 0u;
    if (!stpg_runner_stream_slot_transition(
            &g_runner.stream.slots[0],
            STPG_STREAM_SLOT_ACTIVE
        )) {
        return stpg_runner_abort_stream_startup(ERR_BAD_STATE);
    }
    g_runner.stream.slots[0].stream_event_start = 0u;
    g_runner.stream.slots[0].stream_event_end = g_runner.stream.slots[0].event_count;
    g_runner.stream.queued_event_count = g_runner.stream.slots[0].event_count;

    stpg_runner_prepare_output_sm(preserve_gpio_state);
    err = stpg_runner_recheck_streaming_psram_mux();
    if (err != ERR_NONE) {
        return stpg_runner_abort_stream_startup(err);
    }
    const bool stream_has_samples = stpg_buffer_stream_has_samples();
    uint32_t configured_sample_dma_words = 0u;
    if (stream_has_samples) {
        uint32_t sample_dma_words = stpg_samples_dma_capacity_words();
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP
        const uint32_t psram_expected_samples = stpg_buffer_stream_expected_sample_count();
        if (psram_expected_samples > 0u && psram_expected_samples < sample_dma_words) {
            sample_dma_words = psram_expected_samples;
        }
#endif
        if (g_runner.reduced_density_streaming) {
            const uint32_t expected_samples = stpg_buffer_stream_expected_sample_count();
            if (expected_samples > 0u && expected_samples < sample_dma_words) {
                sample_dma_words = expected_samples;
            }
        }
        configured_sample_dma_words = sample_dma_words;
        dma_channel_config sample_dma_cfg = dma_channel_get_default_config((uint)g_runner.sample_dma_channel);
        channel_config_set_transfer_data_size(&sample_dma_cfg, DMA_SIZE_32);
        channel_config_set_read_increment(&sample_dma_cfg, false);
        channel_config_set_write_increment(&sample_dma_cfg, true);
        channel_config_set_dreq(&sample_dma_cfg, pio_get_dreq(stpg_runner_pio(), stpg_sample_sm(), false));
        const stpg_err_t sample_err = stpg_samples_prepare_for_run(sample_dma_words, stpg_buffer_active_pins());
        if (sample_err != ERR_NONE) {
            return stpg_runner_abort_stream_startup(sample_err);
        }
        channel_config_set_ring(&sample_dma_cfg, true, stpg_samples_dma_ring_bits());
        dma_channel_configure((uint)g_runner.sample_dma_channel, &sample_dma_cfg,
                              stpg_samples_dma_buffer(),
                              &stpg_runner_pio()->rxf[stpg_sample_sm()],
                              sample_dma_words,
                              false);
        pio_sm_set_enabled(stpg_runner_pio(), stpg_sample_sm(), true);
        if (g_runner.reduced_density_streaming && g_runner.stream.slots[0].sample_count > 0u) {
            const stpg_err_t schedule_err = stpg_runner_start_sample_sched_dma(
                stpg_runner_stream_sample_wait_buffer(0u),
                g_runner.stream.slots[0].sample_count);
            if (schedule_err != ERR_NONE) {
                return stpg_runner_abort_stream_startup(schedule_err);
            }
        }
        dma_start_channel_mask(1u << (uint)g_runner.sample_dma_channel);
    }

    g_runner.active = true;
    g_runner.draining = false;
    g_runner.streaming = true;
    g_runner.event_count = g_runner.stream.slots[0].event_count;
    g_runner.sample_event_count = configured_sample_dma_words;
    g_runner.tail_drain_ticks = stpg_runner_compute_tail_ticks_from_words(
        g_runner.stream.slots[0].words, g_runner.stream.slots[0].event_count);
    g_runner.drain_deadline_us = 0u;
    g_runner.stream_last_progress_us = 0u;
    g_runner.stream_last_dma_remaining = 0u;
    g_runner.stream_last_slot_index = 0u;
    g_runner.stream_last_queued_event_count = 0u;
    stpg_status_set_run_progress(g_runner.stream.slots[0].event_count);
    stpg_status_set_result_flags(0u);
    stpg_status_set_fail_count(0u);
    stpg_status_set_sample_count(0u);
    stpg_status_clear_error();
    stpg_runner_note_stream_progress();

    if (startup_prefill_slots > 1u
        && stpg_runner_arm_ready_output_slot(1u) == ERR_NONE) {
        dma_channel_set_irq0_enabled((uint)g_runner.output_dma_spare_channel, true);
        g_runner.output_dma_chain_enabled = true;
        const PIO pio = stpg_runner_pio();
        const uint out_sm = stpg_runner_sm();
        pio_interrupt_clear(pio, 4);
        pio_sm_set_enabled(pio, out_sm, true);
        stpg_runner_configure_chained_output_dma(
            (uint)g_runner.output_dma_channel,
            (uint)g_runner.output_dma_spare_channel,
            g_runner.stream.slots[0].words,
            g_runner.stream.slots[0].word_count,
            true
        );
    } else {
        stpg_runner_start_output_dma(g_runner.stream.slots[0].words, g_runner.stream.slots[0].word_count);
    }
    return ERR_NONE;
}

static const uint32_t STPG_RUN_MEASUREMENT_FLAGS =
    STPG_RUN_FLAG_MEASURE_FAMILY
    | STPG_RUN_FLAG_MEASURE_SEQUENCE
    | STPG_RUN_FLAG_MEASURE_BURST
    | STPG_RUN_FLAG_MEASURE_VISIBILITY
    | STPG_RUN_FLAG_MEASURE_HANDSHAKE;

typedef struct {
    bool streaming;
    bool preserve_gpio_state;
    bool segment_end_hold;
    bool fail_stop;
    uint32_t fail_stop_limit;
} stpg_pattern_run_plan_t;

static void stpg_runner_note_start_request(uint32_t run_flags,
                                           uint32_t start_event_index,
                                           uint32_t max_events,
                                           uint32_t aux_arg) {
    g_runner_start_debug_stage_raw = 1u;
    g_runner_start_debug_run_flags_raw = run_flags;
    g_runner_start_debug_arg1_raw = start_event_index;
    g_runner_start_debug_arg2_raw = max_events;
    g_runner_start_debug_arg3_raw = aux_arg;
}

/* Validate every protocol-visible non-measurement RUN condition before its
 * RSP_OK is queued.  The deferred task may repeat this validation, but it is
 * not allowed to discover an argument/state error that should have been an
 * immediate RSP_ERROR. */
static stpg_err_t stpg_runner_validate_pattern_start(
    uint32_t run_flags,
    uint32_t start_event_index,
    uint32_t max_events,
    uint32_t aux_arg,
    stpg_pattern_run_plan_t *out_plan) {
    if (out_plan == 0) {
        return ERR_BAD_SIZE;
    }
#ifdef PICO_ON_DEVICE
    /* A matching header/capability alone is not proof of the active clock. */
    if (clock_get_hz(clk_sys) != STPG_TICK_HZ) {
        return ERR_UNSUPPORTED;
    }
#endif
    if ((run_flags & STPG_RUN_MEASUREMENT_FLAGS) != 0u) {
        g_runner_start_debug_stage_raw = 11u;
        return ERR_UNSUPPORTED;
    }
    if (!stpg_buffer_has_pattern()) {
        g_runner_start_debug_stage_raw = 20u;
        return ERR_NO_PATTERN;
    }

    const bool fail_stop = (run_flags & STPG_RUN_FLAG_FAIL_STOP) != 0u;
    uint32_t fail_stop_limit = 0u;
#if !STPG_FAIL_STOP
    if (fail_stop) {
        g_runner_start_debug_stage_raw = 21u;
        return ERR_UNSUPPORTED;
    }
#endif
    stpg_err_t err = stpg_fail_stop_resolve_limit(
        fail_stop, aux_arg, &fail_stop_limit);
    if (err != ERR_NONE) {
        g_runner_start_debug_stage_raw = 21u;
        return err;
    }

    const bool streaming = (run_flags & STPG_RUN_FLAG_STREAMING_POC) != 0u;
    const bool preserve_gpio_state =
        (run_flags & STPG_RUN_FLAG_PRESERVE_GPIO_STATE) != 0u;
    const bool segment_end_hold =
        (run_flags & STPG_RUN_FLAG_SEGMENT_END_HOLD) != 0u;
    const uint32_t streaming_flags = STPG_RUN_FLAG_STREAMING_POC
        | STPG_RUN_FLAG_EXPLICIT_WAIT_P4
        | STPG_RUN_FLAG_PRESERVE_GPIO_STATE
        | STPG_RUN_FLAG_SEGMENT_END_HOLD
        | STPG_RUN_FLAG_FAIL_STOP;
#if !STPG_STREAM_FAIL_STOP
    if (fail_stop && streaming) {
        g_runner_start_debug_stage_raw = 22u;
        return ERR_UNSUPPORTED;
    }
#endif
    if (streaming) {
        if ((run_flags & ~streaming_flags) != 0u
            || stpg_buffer_active_source() == STPG_PATTERN_SOURCE_FLASH) {
            g_runner_start_debug_stage_raw = 23u;
            return ERR_UNSUPPORTED;
        }
#if STPG_STREAM_FAIL_STOP
        if (fail_stop
            && (stpg_buffer_active_source() != STPG_PATTERN_SOURCE_PSRAM
                || stpg_buffer_active_format() != STPG_PATTERN_FORMAT_RATE_VECTOR)) {
            g_runner_start_debug_stage_raw = 25u;
            return ERR_UNSUPPORTED;
        }
#endif
        if ((preserve_gpio_state || segment_end_hold)
            && (stpg_buffer_active_source() != STPG_PATTERN_SOURCE_PSRAM
                || stpg_buffer_active_format() != STPG_PATTERN_FORMAT_RATE_VECTOR)) {
            g_runner_start_debug_stage_raw = 25u;
            return ERR_UNSUPPORTED;
        }
        if (preserve_gpio_state) {
            if (start_event_index != g_runner.hold_drive_value
                || max_events != g_runner.hold_drive_enable) {
                g_runner_start_debug_stage_raw = 27u;
                return ERR_HOLD_STATE_MISMATCH;
            }
        } else if (start_event_index != 0u || max_events != 0u) {
            g_runner_start_debug_stage_raw = 28u;
            return ERR_UNSUPPORTED;
        }
    } else {
        const uint32_t allowed_flags = STPG_RUN_FLAG_PRESERVE_GPIO_STATE
            | STPG_RUN_FLAG_SEGMENT_END_HOLD
            | STPG_RUN_FLAG_FAIL_STOP;
        if ((run_flags & ~allowed_flags) != 0u) {
            g_runner_start_debug_stage_raw = 23u;
            return ERR_UNSUPPORTED;
        }
        if ((preserve_gpio_state || segment_end_hold)
            && (stpg_buffer_active_source() != STPG_PATTERN_SOURCE_PSRAM
                || stpg_buffer_active_format() != STPG_PATTERN_FORMAT_RAW_EVENT)) {
            g_runner_start_debug_stage_raw = 25u;
            return ERR_UNSUPPORTED;
        }
        if (preserve_gpio_state) {
            if (!g_runner.hold_awaiting_next || !g_runner.hold_valid) {
                g_runner_start_debug_stage_raw = 26u;
                return ERR_BAD_STATE;
            }
            if (start_event_index != g_runner.hold_drive_value
                || max_events != g_runner.hold_drive_enable) {
                g_runner_start_debug_stage_raw = 27u;
                return ERR_HOLD_STATE_MISMATCH;
            }
        } else {
            if (start_event_index != 0u || max_events != 0u) {
                g_runner_start_debug_stage_raw = 28u;
                return ERR_UNSUPPORTED;
            }
            if (g_runner.hold_awaiting_next) {
                g_runner_start_debug_stage_raw = 29u;
                return ERR_BAD_STATE;
            }
        }
    }

    out_plan->streaming = streaming;
    out_plan->preserve_gpio_state = preserve_gpio_state;
    out_plan->segment_end_hold = segment_end_hold;
    out_plan->fail_stop = fail_stop;
    out_plan->fail_stop_limit = fail_stop_limit;
    return ERR_NONE;
}

#if defined(PICO_ON_DEVICE) && defined(__GNUC__)
__attribute__((noinline))
#endif
static stpg_err_t stpg_runner_start_after_fpu_enabled(
    uint32_t run_flags,
    uint32_t start_event_index,
    uint32_t max_events,
    uint32_t aux_arg) {
    stpg_runner_note_start_request(
        run_flags, start_event_index, max_events, aux_arg);
    if (g_runner.active || g_runner.draining) {
        g_runner_start_debug_stage_raw = 2u;
        return ERR_BAD_STATE;
    }

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
    memset(&g_terminal_replay, 0, sizeof(g_terminal_replay));
#endif

    stpg_err_t err = stpg_runner_ensure_initialized();
    if (err != ERR_NONE) {
        g_runner_start_debug_stage_raw = 3u;
        return err;
    }

    if ((run_flags & STPG_RUN_MEASUREMENT_FLAGS) != 0u) {
        g_runner_start_debug_stage_raw = 10u;
        if ((run_flags & ~STPG_RUN_MEASUREMENT_FLAGS) != 0u) {
            g_runner_start_debug_stage_raw = 11u;
            return ERR_UNSUPPORTED;
        }
        if (__builtin_popcount(run_flags & STPG_RUN_MEASUREMENT_FLAGS) != 1) {
            g_runner_start_debug_stage_raw = 11u;
            return ERR_UNSUPPORTED;
        }
        if ((run_flags & STPG_RUN_FLAG_MEASURE_VISIBILITY) != 0u) {
            g_runner_start_debug_stage_raw = 14u;
            return stpg_runner_run_visibility_probe();
        }
        if ((run_flags & STPG_RUN_FLAG_MEASURE_HANDSHAKE) != 0u) {
            g_runner_start_debug_stage_raw = 15u;
            return stpg_runner_run_handshake_probe(start_event_index);
        }
        const uint32_t family_id = start_event_index & 0xFFu;
        const uint32_t packed_timing = max_events;
        const uint32_t phase_ticks = packed_timing & 0xFFFFu;
        const uint32_t period_ticks = (packed_timing >> 16) & 0xFFFFu;
        const uint32_t cycle_count = aux_arg == 0u ? 256u : aux_arg;
        if ((run_flags & STPG_RUN_FLAG_MEASURE_BURST) != 0u) {
            err = stpg_buffer_prepare_measurement_burst(family_id, period_ticks, phase_ticks, cycle_count);
        } else if ((run_flags & STPG_RUN_FLAG_MEASURE_SEQUENCE) != 0u) {
            err = stpg_buffer_prepare_measurement_sequence(family_id, period_ticks, phase_ticks, cycle_count);
        } else {
            err = stpg_buffer_prepare_measurement_family(family_id, period_ticks, phase_ticks, cycle_count);
        }
        if (err != ERR_NONE) {
            g_runner_start_debug_stage_raw = 12u;
            return err;
        }
        g_runner.segment_end_hold = false;
        g_runner_start_debug_stage_raw = 13u;
        return stpg_runner_start_prepared(false, false, 0u);
    }

    stpg_pattern_run_plan_t plan;
    err = stpg_runner_validate_pattern_start(
        run_flags, start_event_index, max_events, aux_arg, &plan);
    if (err != ERR_NONE) {
        return err;
    }
    /* Normal debugger flash probing can leave M0 in slow serial-read mode.
     * Restore only after normal-pattern validation at quiescent RUN admission,
     * never from USB/IRQ/sample service or while output is live. Core1 lockout
     * and DMA-idle validation are additionally required by the helper. */
    const PIO xip_pio = stpg_runner_pio();
    const uint32_t xip_owned_sms = (1u << stpg_runner_sm()) | (1u << stpg_sample_sm());
    const stpg_err_t xip_err = stpg_flash_xip_ensure_for_run(
        g_runner.core1_started && (xip_pio->ctrl & xip_owned_sms) == 0u);
    if (xip_err != ERR_NONE) {
        g_runner_start_debug_stage_raw = 34u;
        return xip_err;
    }
    if (plan.streaming) {
        g_runner_start_debug_stage_raw = 24u;
        return stpg_runner_start_streaming(
            (run_flags & STPG_RUN_FLAG_EXPLICIT_WAIT_P4) != 0u,
            plan.preserve_gpio_state,
            plan.segment_end_hold,
            plan.fail_stop,
            plan.fail_stop_limit
        );
    }

    err = stpg_buffer_prepare_for_run();
    if (err != ERR_NONE) {
        g_runner_start_debug_stage_raw = 30u;
        return err;
    }
    if ((plan.preserve_gpio_state || plan.segment_end_hold)
        && stpg_buffer_sample_event_count() != 0u) {
        g_runner_start_debug_stage_raw = 31u;
        return ERR_UNSUPPORTED;
    }
    g_runner.segment_end_hold = plan.segment_end_hold;
    g_runner_start_debug_stage_raw = 32u;
    return stpg_runner_start_prepared(
        plan.preserve_gpio_state, plan.fail_stop, plan.fail_stop_limit);
}

/* Keep this entry wrapper integer-only.  The RP2350 RUN path uses the GPIO,
 * double, and FP coprocessors; no coprocessor instruction may execute before
 * software has repaired and verified their CPACR access. */
#if defined(PICO_ON_DEVICE) && defined(__GNUC__)
__attribute__((noinline, target("general-regs-only")))
#endif
stpg_err_t stpg_runner_start(uint32_t run_flags,
                             uint32_t start_event_index,
                             uint32_t max_events,
                             uint32_t aux_arg) {
    if (!stpg_fpu_access_ensure_for_run()) {
        g_runner_start_debug_stage_raw = 33u;
        return ERR_BAD_STATE;
    }
    return stpg_runner_start_after_fpu_enabled(
        run_flags, start_event_index, max_events, aux_arg);
}

stpg_err_t stpg_runner_request_start(uint32_t run_flags,
                                     uint32_t start_event_index,
                                     uint32_t max_events,
                                     uint32_t aux_arg) {
    stpg_runner_note_start_request(
        run_flags, start_event_index, max_events, aux_arg);
    if (g_runner.pending_start || g_runner.active || g_runner.draining) {
        g_runner_start_debug_stage_raw = 2u;
        return ERR_BAD_STATE;
    }
    stpg_err_t err = stpg_runner_ensure_initialized();
    if (err != ERR_NONE) {
        g_runner_start_debug_stage_raw = 3u;
        return err;
    }
    stpg_pattern_run_plan_t plan;
    err = stpg_runner_validate_pattern_start(
        run_flags, start_event_index, max_events, aux_arg, &plan);
    if (err != ERR_NONE) {
        return err;
    }
    g_runner.pending_start = true;
    g_runner.pending_start_run_flags = run_flags;
    g_runner.pending_start_event_index = start_event_index;
    g_runner.pending_start_max_events = max_events;
    g_runner.pending_start_aux_arg = aux_arg;
    return ERR_NONE;
}

bool stpg_runner_start_pending(void) {
    return g_runner.pending_start;
}

stpg_err_t stpg_runner_stop(void) {
    g_runner.pending_start = false;
    stpg_usb_run_response_cancel();
    if (g_runner.streaming) {
        g_runner.stream.shared.stop_requested = true;
        g_runner.stream.shared.fill_request_slot = -1;
        for (uint32_t slot_index = 0u; slot_index < STPG_STREAM_SLOT_COUNT; ++slot_index) {
            g_runner.stream.shared.fill_request_pending[slot_index] = false;
        }
        __sev();
    }
    stpg_runner_disable_hw_safe();
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE && !STPG_STREAM_FAIL_STOP
    /* Cancel after quiescence, before accepting any next RUN or buffer reset. */
    memset(&g_terminal_replay, 0, sizeof(g_terminal_replay));
#endif
    g_runner.active = false;
    g_runner.draining = false;
    g_runner.streaming = false;
    g_runner.reduced_density_streaming = false;
    stpg_runner_reset_fail_stop_context();
    g_runner.stream.active = false;
    g_runner.drain_deadline_us = 0u;
    g_runner.sample_event_count = 0u;
    g_runner.stream_last_progress_us = 0u;
    g_runner.stream_last_dma_remaining = 0u;
    g_runner.stream_last_slot_index = 0u;
    g_runner.stream_last_queued_event_count = 0u;
    g_runner.pending_noready_prod_recheck = false;
    g_runner.pending_noready_prod_completed_slot = 0u;
    /* Cancel the old run's deferred IRQ failure only after owned PIO/DMA
     * are quiescent and the runner is inactive. Otherwise a STOP/RESET that
     * precedes task-side error delivery can report IDLE, then replay that
     * stale failure on the next main-loop turn or into the next RUN. */
    g_runner.pending_stream_fail_error = ERR_NONE;
    g_runner.pending_stream_fail_from_irq = false;
    stpg_samples_reset();
    stpg_status_set_run_progress(0u);
    stpg_status_reset_result_info();
    stpg_status_clear_error();
    stpg_status_set_state(STATE_IDLE);
    return ERR_NONE;
}

bool stpg_runner_is_active(void) {
    return g_runner.active;
}

bool stpg_runner_is_draining(void) {
    return g_runner.draining;
}

bool stpg_runner_hold_awaiting_next(void) {
    return g_runner.hold_awaiting_next;
}

bool stpg_runner_hold_state(uint32_t *out_drive_value, uint32_t *out_drive_enable) {
    if (!g_runner.hold_valid) {
        return false;
    }
    if (out_drive_value != 0) {
        *out_drive_value = g_runner.hold_drive_value;
    }
    if (out_drive_enable != 0) {
        *out_drive_enable = g_runner.hold_drive_enable;
    }
    return true;
}

void stpg_runner_get_stream_telemetry(stpg_stream_telemetry_t *out) {
    if (out == 0) {
        return;
    }
    __dmb();
    out->refill_count = g_runner.stream.telemetry.refill_count;
    out->refill_event_count = g_runner.stream.telemetry.refill_event_count;
    out->refill_total_us = g_runner.stream.telemetry.refill_total_us;
    out->refill_max_us = g_runner.stream.telemetry.refill_max_us;
    out->refill_last_us = g_runner.stream.telemetry.refill_last_us;
    out->refill_last_event_count = g_runner.stream.telemetry.refill_last_event_count;
    out->swap_count = g_runner.stream.telemetry.swap_count;
    out->underflow_count = g_runner.stream.telemetry.underflow_count;
    out->irq_swap_count = g_runner.stream.telemetry.irq_swap_count;
    out->task_swap_count = g_runner.stream.telemetry.task_swap_count;
    out->queue_slot_count = g_runner.stream.telemetry.queue_slot_count;
    out->slot_event_capacity = g_runner.stream.telemetry.slot_event_capacity;
    out->startup_prefill_slots = g_runner.stream.telemetry.startup_prefill_slots;
    out->startup_prefill_total_us = g_runner.stream.telemetry.startup_prefill_total_us;
    out->min_ready_slots_observed = g_runner.stream.telemetry.min_ready_slots_observed;
    out->swap_restart_total_us = g_runner.stream.telemetry.swap_restart_total_us;
    out->swap_restart_max_us = g_runner.stream.telemetry.swap_restart_max_us;
    out->underflow_reason = g_runner.stream.telemetry.underflow_reason;
    out->sample_schedule_word_count = g_runner.stream.telemetry.sample_schedule_word_count;
    out->sample_request_count = g_runner.stream.telemetry.sample_request_count;
    out->sample_capture_count = g_runner.stream.telemetry.sample_capture_count;
    out->compare_event_count = g_runner.stream.telemetry.compare_event_count;
    out->core1_loop_count = g_runner.stream.telemetry.core1_loop_count;
    out->core1_request_count = g_runner.stream.telemetry.core1_request_count;
    out->core1_service_count = g_runner.stream.telemetry.core1_service_count;
    out->core1_last_slot_index = g_runner.stream.telemetry.core1_last_slot_index;
    out->core1_last_fill_error = g_runner.stream.telemetry.core1_last_fill_error;
    out->startup_prefill_attempted_slots = g_runner.stream.telemetry.startup_prefill_attempted_slots;
    out->startup_prefill_last_slot_index = g_runner.stream.telemetry.startup_prefill_last_slot_index;
    out->startup_prefill_break_reason = g_runner.stream.telemetry.startup_prefill_break_reason;
    out->startup_prefill_wait_error = g_runner.stream.telemetry.startup_prefill_wait_error;
    out->startup_prefill_last_event_count = g_runner.stream.telemetry.startup_prefill_last_event_count;
    out->startup_prefill_last_word_count = g_runner.stream.telemetry.startup_prefill_last_word_count;
    out->startup_timeout_core1_loop_count = g_runner.stream.telemetry.startup_timeout_core1_loop_count;
    out->startup_timeout_core1_service_count = g_runner.stream.telemetry.startup_timeout_core1_service_count;
    out->startup_timeout_stop_requested = g_runner.stream.telemetry.startup_timeout_stop_requested;
    out->startup_timeout_fill_request_slot = g_runner.stream.telemetry.startup_timeout_fill_request_slot;
    out->startup_timeout_core1_boot_stage_raw = g_runner.stream.telemetry.startup_timeout_core1_boot_stage_raw;
    out->startup_timeout_core1_request_raw = g_runner.stream.telemetry.startup_timeout_core1_request_raw;
    out->startup_timeout_core1_service_raw = g_runner.stream.telemetry.startup_timeout_core1_service_raw;
    out->startup_timeout_core1_fill_error_raw = g_runner.stream.telemetry.startup_timeout_core1_fill_error_raw;
    out->startup_timeout_core1_request_seen_raw = g_runner.stream.telemetry.startup_timeout_core1_request_seen_raw;
    out->startup_timeout_core1_claim_raw = g_runner.stream.telemetry.startup_timeout_core1_claim_raw;
    out->startup_timeout_core1_skip_stop_raw = g_runner.stream.telemetry.startup_timeout_core1_skip_stop_raw;
    out->startup_timeout_core1_skip_no_request_raw = g_runner.stream.telemetry.startup_timeout_core1_skip_no_request_raw;
    out->startup_timeout_core1_last_seen_slot_raw = g_runner.stream.telemetry.startup_timeout_core1_last_seen_slot_raw;
    out->startup_timeout_core1_last_polled_slot_raw = g_runner.stream.telemetry.startup_timeout_core1_last_polled_slot_raw;
    out->startup_timeout_core1_negative_poll_count_raw = g_runner.stream.telemetry.startup_timeout_core1_negative_poll_count_raw;
    out->startup_timeout_core1_valid_poll_count_raw = g_runner.stream.telemetry.startup_timeout_core1_valid_poll_count_raw;
    out->startup_timeout_core1_invalid_poll_count_raw = g_runner.stream.telemetry.startup_timeout_core1_invalid_poll_count_raw;
    out->startup_timeout_core1_last_polled_fill_error_raw = g_runner.stream.telemetry.startup_timeout_core1_last_polled_fill_error_raw;
    out->startup_timeout_core1_last_polled_slot0_ready_raw = g_runner.stream.telemetry.startup_timeout_core1_last_polled_slot0_ready_raw;
    out->startup_timeout_request_set_us = g_runner.stream.telemetry.startup_timeout_request_set_us;
    out->startup_timeout_request_clear_us = g_runner.stream.telemetry.startup_timeout_request_clear_us;
    out->startup_timeout_core1_first_poll_us = g_runner.stream.telemetry.startup_timeout_core1_first_poll_us;
    out->startup_timeout_core1_first_negative_poll_us = g_runner.stream.telemetry.startup_timeout_core1_first_negative_poll_us;
    out->startup_timeout_core1_first_valid_poll_us = g_runner.stream.telemetry.startup_timeout_core1_first_valid_poll_us;
    out->startup_timeout_core1_last_stage_raw = g_runner.stream.telemetry.startup_timeout_core1_last_stage_raw;
    out->startup_timeout_core1_after_stop_gate_us = g_runner.stream.telemetry.startup_timeout_core1_after_stop_gate_us;
    out->startup_timeout_core1_after_dmb_us = g_runner.stream.telemetry.startup_timeout_core1_after_dmb_us;
    out->startup_timeout_core1_after_slot_read_us = g_runner.stream.telemetry.startup_timeout_core1_after_slot_read_us;
    out->visibility_probe_request_set_us = g_runner.stream.telemetry.visibility_probe_request_set_us;
    out->visibility_probe_core1_saw_request_us = g_runner.stream.telemetry.visibility_probe_core1_saw_request_us;
    out->visibility_probe_core1_write1_us = g_runner.stream.telemetry.visibility_probe_core1_write1_us;
    out->visibility_probe_core1_write2_us = g_runner.stream.telemetry.visibility_probe_core1_write2_us;
    out->visibility_probe_core1_done_us = g_runner.stream.telemetry.visibility_probe_core1_done_us;
    out->visibility_probe_core0_first_stage_seen_us = g_runner.stream.telemetry.visibility_probe_core0_first_stage_seen_us;
    out->visibility_probe_core0_first_value1_seen_us = g_runner.stream.telemetry.visibility_probe_core0_first_value1_seen_us;
    out->visibility_probe_core0_first_value2_seen_us = g_runner.stream.telemetry.visibility_probe_core0_first_value2_seen_us;
    out->visibility_probe_core0_first_done_seen_us = g_runner.stream.telemetry.visibility_probe_core0_first_done_seen_us;
    out->visibility_probe_final_stage = g_runner.stream.telemetry.visibility_probe_final_stage;
    out->visibility_probe_final_value1 = g_runner.stream.telemetry.visibility_probe_final_value1;
    out->visibility_probe_final_value2 = g_runner.stream.telemetry.visibility_probe_final_value2;
    out->visibility_probe_final_done = g_runner.stream.telemetry.visibility_probe_final_done;
    out->visibility_probe_timeout_hit = g_runner.stream.telemetry.visibility_probe_timeout_hit;
    out->handshake_probe_request_set_us = g_runner.stream.telemetry.handshake_probe_request_set_us;
    out->handshake_probe_core1_saw_request_us = g_runner.stream.telemetry.handshake_probe_core1_saw_request_us;
    out->handshake_probe_core1_claim_us = g_runner.stream.telemetry.handshake_probe_core1_claim_us;
    out->handshake_probe_core1_ready_us = g_runner.stream.telemetry.handshake_probe_core1_ready_us;
    out->handshake_probe_core0_ready_seen_us = g_runner.stream.telemetry.handshake_probe_core0_ready_seen_us;
    out->handshake_probe_core0_error_seen_us = g_runner.stream.telemetry.handshake_probe_core0_error_seen_us;
    out->handshake_probe_final_request_slot = g_runner.stream.telemetry.handshake_probe_final_request_slot;
    out->handshake_probe_final_ready = g_runner.stream.telemetry.handshake_probe_final_ready;
    out->handshake_probe_final_error = g_runner.stream.telemetry.handshake_probe_final_error;
    out->handshake_probe_timeout_hit = g_runner.stream.telemetry.handshake_probe_timeout_hit;
    out->startup_timeout_shared_fill_request_slot = g_runner.stream.telemetry.startup_timeout_shared_fill_request_slot;
    out->startup_timeout_shared_slot0_ready = g_runner.stream.telemetry.startup_timeout_shared_slot0_ready;
    out->startup_timeout_shared_fill_error = g_runner.stream.telemetry.startup_timeout_shared_fill_error;
    out->startup_timeout_shared_slot0_event_count = g_runner.stream.telemetry.startup_timeout_shared_slot0_event_count;
    out->startup_timeout_shared_slot0_word_count = g_runner.stream.telemetry.startup_timeout_shared_slot0_word_count;
    out->fail_snapshot_status_state = g_runner.stream.telemetry.fail_snapshot_status_state;
    out->fail_snapshot_runner_flags = g_runner.stream.telemetry.fail_snapshot_runner_flags;
    out->fail_snapshot_shared_fill_request_slot = g_runner.stream.telemetry.fail_snapshot_shared_fill_request_slot;
    out->fail_snapshot_shared_fill_error = g_runner.stream.telemetry.fail_snapshot_shared_fill_error;
    out->fail_snapshot_from_irq = g_runner.stream.telemetry.fail_snapshot_from_irq;
    out->dma_abort_output_us = g_runner.stream.telemetry.dma_abort_output_us;
    out->dma_abort_sample_us = g_runner.stream.telemetry.dma_abort_sample_us;
    out->dma_abort_sample_sched_us = g_runner.stream.telemetry.dma_abort_sample_sched_us;
    out->dma_abort_total_us = g_runner.stream.telemetry.dma_abort_total_us;
    out->startup_first_fill_request_us = g_runner.stream.telemetry.startup_first_fill_request_us;
    out->startup_first_service_complete_us = g_runner.stream.telemetry.startup_first_service_complete_us;
    out->startup_slot0_ready_us = g_runner.stream.telemetry.startup_slot0_ready_us;
    extern volatile uint32_t g_main_loop_heartbeat_count_raw;
    extern volatile uint32_t g_main_loop_stage_raw;
    extern volatile uint32_t g_usb_protocol_task_call_count_raw;
    extern volatile uint32_t g_usb_tud_task_call_count_raw;
    extern volatile uint32_t g_usb_available_poll_count_raw;
    extern volatile uint32_t g_usb_rx_read_call_count_raw;
    extern volatile uint32_t g_usb_rx_byte_count_raw;
    extern volatile uint32_t g_usb_frame_complete_count_raw;
    extern volatile uint32_t g_usb_dispatch_call_count_raw;
    extern volatile uint32_t g_usb_write_response_count_raw;
    extern volatile uint32_t g_usb_write_spin_count_raw;
    extern volatile uint32_t g_usb_write_last_success_bytes_raw;
    extern volatile uint32_t g_usb_last_payload_error_code_raw;
    out->recovery_live_main_loop_count = g_main_loop_heartbeat_count_raw;
    out->recovery_live_main_loop_stage = g_main_loop_stage_raw;
    out->recovery_live_usb_task_count = g_usb_protocol_task_call_count_raw;
    out->recovery_live_tud_task_count = g_usb_tud_task_call_count_raw;
    out->recovery_fail_stage = g_runner.stream.telemetry.recovery_fail_stage;
    out->recovery_fail_main_loop_count = g_runner.stream.telemetry.recovery_fail_main_loop_count;
    out->recovery_fail_usb_task_count = g_runner.stream.telemetry.recovery_fail_usb_task_count;
    out->recovery_fail_tud_task_count = g_runner.stream.telemetry.recovery_fail_tud_task_count;
    out->recovery_fail_dma_irq0_pending = g_runner.stream.telemetry.recovery_fail_dma_irq0_pending;
    out->recovery_fail_dma_irq0_enabled = g_runner.stream.telemetry.recovery_fail_dma_irq0_enabled;
    out->recovery_live_usb_available_poll_count = g_usb_available_poll_count_raw;
    out->recovery_live_usb_rx_read_call_count = g_usb_rx_read_call_count_raw;
    out->recovery_live_usb_rx_byte_count = g_usb_rx_byte_count_raw;
    out->recovery_live_usb_frame_complete_count = g_usb_frame_complete_count_raw;
    out->recovery_live_usb_dispatch_call_count = g_usb_dispatch_call_count_raw;
    out->recovery_live_usb_write_response_count = g_usb_write_response_count_raw;
    out->recovery_fail_usb_available_poll_count = g_runner.stream.telemetry.recovery_fail_usb_available_poll_count;
    out->recovery_fail_usb_rx_read_call_count = g_runner.stream.telemetry.recovery_fail_usb_rx_read_call_count;
    out->recovery_fail_usb_rx_byte_count = g_runner.stream.telemetry.recovery_fail_usb_rx_byte_count;
    out->recovery_fail_usb_frame_complete_count = g_runner.stream.telemetry.recovery_fail_usb_frame_complete_count;
    out->recovery_fail_usb_dispatch_call_count = g_runner.stream.telemetry.recovery_fail_usb_dispatch_call_count;
    out->recovery_fail_usb_write_response_count = g_runner.stream.telemetry.recovery_fail_usb_write_response_count;
    out->recovery_live_usb_write_spin_count = g_usb_write_spin_count_raw;
    out->recovery_live_usb_last_write_success_bytes = g_usb_write_last_success_bytes_raw;
    out->recovery_live_usb_last_payload_error_code = g_usb_last_payload_error_code_raw;
    out->recovery_fail_usb_write_spin_count = g_runner.stream.telemetry.recovery_fail_usb_write_spin_count;
    out->recovery_fail_usb_last_write_success_bytes = g_runner.stream.telemetry.recovery_fail_usb_last_write_success_bytes;
    out->recovery_fail_usb_last_payload_error_code = g_runner.stream.telemetry.recovery_fail_usb_last_payload_error_code;
    out->underflow_snapshot_active_slot = g_runner.stream.telemetry.underflow_snapshot_active_slot;
    out->underflow_snapshot_next_requested_slot = g_runner.stream.telemetry.underflow_snapshot_next_requested_slot;
    out->underflow_snapshot_next_ready_slot = g_runner.stream.telemetry.underflow_snapshot_next_ready_slot;
    out->underflow_snapshot_ready_mask = g_runner.stream.telemetry.underflow_snapshot_ready_mask;
    out->underflow_snapshot_request_seq = g_runner.stream.telemetry.underflow_snapshot_request_seq;
    out->underflow_snapshot_service_seq = g_runner.stream.telemetry.underflow_snapshot_service_seq;
    out->underflow_snapshot_last_service_slot = g_runner.stream.telemetry.underflow_snapshot_last_service_slot;
    out->underflow_snapshot_last_service_event_count = g_runner.stream.telemetry.underflow_snapshot_last_service_event_count;
    out->underflow_snapshot_last_service_word_count = g_runner.stream.telemetry.underflow_snapshot_last_service_word_count;
    out->underflow_snapshot_last_service_sample_count = g_runner.stream.telemetry.underflow_snapshot_last_service_sample_count;
    out->underflow_snapshot_queued_event_count = g_runner.stream.telemetry.underflow_snapshot_queued_event_count;
    out->underflow_snapshot_reason = g_runner.stream.telemetry.underflow_snapshot_reason;
    out->prod_noready_defer_count = g_runner.stream.telemetry.prod_noready_defer_count;
    out->prod_noready_recheck_success_count = g_runner.stream.telemetry.prod_noready_recheck_success_count;
    out->prod_noready_recheck_fail_count = g_runner.stream.telemetry.prod_noready_recheck_fail_count;
    out->prod_noready_safety_reject_count = g_runner.stream.telemetry.prod_noready_safety_reject_count;
    out->prod_noready_last_iterations = g_runner.stream.telemetry.prod_noready_last_iterations;
    out->prod_noready_last_wait_us = g_runner.stream.telemetry.prod_noready_last_wait_us;
    out->prod_noready_wait_step_us = g_runner.stream.telemetry.prod_noready_wait_step_us;
    out->prod_noready_max_iterations = g_runner.stream.telemetry.prod_noready_max_iterations;
    out->prod_noready_defer_dma_busy = g_runner.stream.telemetry.prod_noready_defer_dma_busy;
    out->prod_noready_defer_dma_remaining = g_runner.stream.telemetry.prod_noready_defer_dma_remaining;
    out->prod_noready_defer_pio_txempty = g_runner.stream.telemetry.prod_noready_defer_pio_txempty;
    out->prod_noready_defer_completed_slot = g_runner.stream.telemetry.prod_noready_defer_completed_slot;
    out->prod_noready_defer_queued_events = g_runner.stream.telemetry.prod_noready_defer_queued_events;
    out->prod_noready_defer_safety_flags = g_runner.stream.telemetry.prod_noready_defer_safety_flags;
    out->underflow_snapshot_vector_index = g_runner.stream.telemetry.underflow_snapshot_vector_index;
    out->underflow_snapshot_repeat_index = g_runner.stream.telemetry.underflow_snapshot_repeat_index;
    out->underflow_snapshot_rate_id = g_runner.stream.telemetry.underflow_snapshot_rate_id;
    out->underflow_snapshot_period_ticks = g_runner.stream.telemetry.underflow_snapshot_period_ticks;
    out->underflow_snapshot_prev_period_ticks = g_runner.stream.telemetry.underflow_snapshot_prev_period_ticks;
    out->underflow_snapshot_desired_delta_ticks = g_runner.stream.telemetry.underflow_snapshot_desired_delta_ticks;
    out->underflow_snapshot_vector_repeat = g_runner.stream.telemetry.underflow_snapshot_vector_repeat;
    out->underflow_snapshot_vector_flags = g_runner.stream.telemetry.underflow_snapshot_vector_flags;
    out->underflow_snapshot_current_value = g_runner.stream.telemetry.underflow_snapshot_current_value;
    out->underflow_snapshot_current_dir = g_runner.stream.telemetry.underflow_snapshot_current_dir;
    out->underflow_snapshot_state_flags = g_runner.stream.telemetry.underflow_snapshot_state_flags;
    out->p3_stream_run_sequence = g_runner.stream.telemetry.p3_stream_run_sequence;
    out->p3_telemetry_sequence = g_runner.stream.telemetry.p3_telemetry_sequence;
    out->p3_chain_arm_count = g_runner.stream.telemetry.p3_chain_arm_count;
    out->p3_chain_completion_count = g_runner.stream.telemetry.p3_chain_completion_count;
    out->p3_no_ready_fail_count = g_runner.stream.telemetry.p3_no_ready_fail_count;
    out->p3_terminal_chain_neutralize_count = g_runner.stream.telemetry.p3_terminal_chain_neutralize_count;
    out->p3_no_ready_chain_neutralize_count = g_runner.stream.telemetry.p3_no_ready_chain_neutralize_count;
    out->p3_max_irq_to_arm_us = g_runner.stream.telemetry.p3_max_irq_to_arm_us;
    out->p3_handoff_owner = g_runner.stream.telemetry.p3_handoff_owner;
    out->p3_handoff_conflict_count = g_runner.stream.telemetry.p3_handoff_conflict_count;
    out->p3_irq_handoff_count = g_runner.stream.telemetry.p3_irq_handoff_count;
    out->p3_main_loop_handoff_count = g_runner.stream.telemetry.p3_main_loop_handoff_count;
    out->p3_main_loop_handoff_last_us = g_runner.stream.telemetry.p3_main_loop_handoff_last_us;
    out->p3_main_loop_handoff_max_us = g_runner.stream.telemetry.p3_main_loop_handoff_max_us;
    out->p3_main_loop_rearm_count = g_runner.stream.telemetry.p3_main_loop_rearm_count;
    out->p3_main_loop_rearm_last_us = g_runner.stream.telemetry.p3_main_loop_rearm_last_us;
    out->p3_main_loop_idle_fallback_count = g_runner.stream.telemetry.p3_main_loop_idle_fallback_count;
    out->p4_diag_marker_slot = g_runner.stream.telemetry.p4_diag_marker_slot;
    out->p4_diag_marker_vector_index = g_runner.stream.telemetry.p4_diag_marker_vector_index;
    out->p4_diag_observation_count = g_runner.stream.telemetry.p4_diag_observation_count;
    out->p4_diag_active_dma_remaining = g_runner.stream.telemetry.p4_diag_active_dma_remaining;
    out->p4_diag_active_dma_busy = g_runner.stream.telemetry.p4_diag_active_dma_busy;
    out->p4_diag_promotable_slot = g_runner.stream.telemetry.p4_diag_promotable_slot;
    out->p4_diag_promotable_count = g_runner.stream.telemetry.p4_diag_promotable_count;
    out->p4_diag_first_promotable_us = g_runner.stream.telemetry.p4_diag_first_promotable_us;
    out->p4_diag_spare_state = g_runner.stream.telemetry.p4_diag_spare_state;
    out->p4_diag_ready_before_end = g_runner.stream.telemetry.p4_diag_ready_before_end;
    out->dma_pio_boundary_count = g_runner.stream.telemetry.dma_pio_boundary_count;
    out->dma_pio_last_entry_channel = g_runner.stream.telemetry.dma_pio_last_entry_channel;
    out->dma_pio_last_entry_irq_pending = g_runner.stream.telemetry.dma_pio_last_entry_irq_pending;
    out->dma_pio_last_entry_busy = g_runner.stream.telemetry.dma_pio_last_entry_busy;
    out->dma_pio_last_entry_remaining = g_runner.stream.telemetry.dma_pio_last_entry_remaining;
    out->dma_pio_last_entry_fstat = g_runner.stream.telemetry.dma_pio_last_entry_fstat;
    out->dma_pio_last_entry_fdebug = g_runner.stream.telemetry.dma_pio_last_entry_fdebug;
    out->dma_pio_last_entry_flevel = g_runner.stream.telemetry.dma_pio_last_entry_flevel;
    out->dma_pio_last_exit_channel = g_runner.stream.telemetry.dma_pio_last_exit_channel;
    out->dma_pio_last_exit_busy = g_runner.stream.telemetry.dma_pio_last_exit_busy;
    out->dma_pio_last_exit_remaining = g_runner.stream.telemetry.dma_pio_last_exit_remaining;
    out->dma_pio_last_exit_fstat = g_runner.stream.telemetry.dma_pio_last_exit_fstat;
    out->dma_pio_last_exit_fdebug = g_runner.stream.telemetry.dma_pio_last_exit_fdebug;
    out->dma_pio_last_exit_flevel = g_runner.stream.telemetry.dma_pio_last_exit_flevel;
    out->dma_pio_last_handler_us = g_runner.stream.telemetry.dma_pio_last_handler_us;
    out->dma_completion_trace_count = g_runner.stream.telemetry.dma_completion_trace_count;
    out->dma_completion_trace_overflow = g_runner.stream.telemetry.dma_completion_trace_overflow;
    out->dma_completion_trace_boundary_count = g_runner.stream.telemetry.dma_completion_trace_boundary_count;
    out->dma_completion_trace_max_restart_us = g_runner.stream.telemetry.dma_completion_trace_max_restart_us;
    out->dma_completion_trace_last_path_flags = g_runner.stream.telemetry.dma_completion_trace_last_path_flags;
    for (uint32_t index = 0u; index < STPG_DMA_COMPLETION_TRACE_CAPACITY; ++index) {
        out->dma_completion_trace[index].completion_index =
            g_runner.stream.telemetry.dma_completion_trace[index].completion_index;
        out->dma_completion_trace[index].chunk_index =
            g_runner.stream.telemetry.dma_completion_trace[index].chunk_index;
        out->dma_completion_trace[index].event_start =
            g_runner.stream.telemetry.dma_completion_trace[index].event_start;
        out->dma_completion_trace[index].event_end =
            g_runner.stream.telemetry.dma_completion_trace[index].event_end;
        out->dma_completion_trace[index].boundary_event =
            g_runner.stream.telemetry.dma_completion_trace[index].boundary_event;
        out->dma_completion_trace[index].path_flags =
            g_runner.stream.telemetry.dma_completion_trace[index].path_flags;
        out->dma_completion_trace[index].completed_slot =
            g_runner.stream.telemetry.dma_completion_trace[index].completed_slot;
        out->dma_completion_trace[index].armed_slot =
            g_runner.stream.telemetry.dma_completion_trace[index].armed_slot;
        out->dma_completion_trace[index].next_ready_slot =
            g_runner.stream.telemetry.dma_completion_trace[index].next_ready_slot;
        out->dma_completion_trace[index].restart_us =
            g_runner.stream.telemetry.dma_completion_trace[index].restart_us;
        out->dma_completion_trace[index].handler_us =
            g_runner.stream.telemetry.dma_completion_trace[index].handler_us;
        out->dma_completion_trace[index].dma_remaining =
            g_runner.stream.telemetry.dma_completion_trace[index].dma_remaining;
        out->dma_completion_trace[index].pio_flevel =
            g_runner.stream.telemetry.dma_completion_trace[index].pio_flevel;
        out->dma_completion_trace[index].pio_fstat =
            g_runner.stream.telemetry.dma_completion_trace[index].pio_fstat;
    }
#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
    out->refill_trace_count = g_runner.stream.telemetry.refill_trace_count;
    out->refill_trace_overflow = g_runner.stream.telemetry.refill_trace_overflow;
    for (uint32_t index = 0u; index < STPG_REFILL_TRACE_CAPACITY; ++index) {
        out->refill_trace[index].sequence =
            g_runner.stream.telemetry.refill_trace[index].sequence;
        out->refill_trace[index].slot_index =
            g_runner.stream.telemetry.refill_trace[index].slot_index;
        out->refill_trace[index].event_start =
            g_runner.stream.telemetry.refill_trace[index].event_start;
        out->refill_trace[index].event_end =
            g_runner.stream.telemetry.refill_trace[index].event_end;
        out->refill_trace[index].request_us =
            g_runner.stream.telemetry.refill_trace[index].request_us;
        out->refill_trace[index].claim_us =
            g_runner.stream.telemetry.refill_trace[index].claim_us;
        out->refill_trace[index].body_start_us =
            g_runner.stream.telemetry.refill_trace[index].body_start_us;
        out->refill_trace[index].body_end_us =
            g_runner.stream.telemetry.refill_trace[index].body_end_us;
        out->refill_trace[index].ready_us =
            g_runner.stream.telemetry.refill_trace[index].ready_us;
        out->refill_trace[index].promote_us =
            g_runner.stream.telemetry.refill_trace[index].promote_us;
        out->refill_trace[index].arm_us =
            g_runner.stream.telemetry.refill_trace[index].arm_us;
        out->refill_trace[index].fill_error =
            g_runner.stream.telemetry.refill_trace[index].fill_error;
    }
#endif
}

void stpg_runner_get_start_debug(uint32_t *out_stage,
                                 uint32_t *out_run_flags,
                                 uint32_t *out_arg1,
                                 uint32_t *out_arg2,
                                 uint32_t *out_arg3,
                                 uint32_t *out_active,
                                 uint32_t *out_draining) {
    if (out_stage != 0) {
        *out_stage = g_runner_start_debug_stage_raw;
    }
    if (out_run_flags != 0) {
        *out_run_flags = g_runner_start_debug_run_flags_raw;
    }
    if (out_arg1 != 0) {
        *out_arg1 = g_runner_start_debug_arg1_raw;
    }
    if (out_arg2 != 0) {
        *out_arg2 = g_runner_start_debug_arg2_raw;
    }
    if (out_arg3 != 0) {
        *out_arg3 = g_runner_start_debug_arg3_raw;
    }
    if (out_active != 0) {
        *out_active = g_runner.active ? 1u : 0u;
    }
    if (out_draining != 0) {
        *out_draining = g_runner.draining ? 1u : 0u;
    }
}

void stpg_runner_get_direct_debug(uint32_t *out_output_dma_remaining,
                                  uint32_t *out_output_dma_busy,
                                  uint32_t *out_output_dma_irq_pending,
                                  uint32_t *out_sample_dma_remaining,
                                  uint32_t *out_sample_dma_busy,
                                  uint32_t *out_pio_fdebug,
                                  uint32_t *out_event_count,
                                  uint32_t *out_sample_event_count) {
    const PIO pio = stpg_runner_pio();
    const uint32_t output_dma_remaining =
        g_runner.output_dma_channel >= 0 ? dma_hw->ch[g_runner.output_dma_channel].transfer_count : 0u;
    const uint32_t output_dma_busy =
        g_runner.output_dma_channel >= 0 ? (dma_channel_is_busy((uint)g_runner.output_dma_channel) ? 1u : 0u) : 0u;
    const uint32_t output_dma_irq_pending =
        g_runner.output_dma_channel >= 0 ? (dma_channel_get_irq0_status((uint)g_runner.output_dma_channel) ? 1u : 0u) : 0u;
    const uint32_t sample_dma_remaining =
        g_runner.sample_dma_channel >= 0 ? dma_hw->ch[g_runner.sample_dma_channel].transfer_count : 0u;
    const uint32_t sample_dma_busy =
        g_runner.sample_dma_channel >= 0 ? (dma_channel_is_busy((uint)g_runner.sample_dma_channel) ? 1u : 0u) : 0u;

    if (out_output_dma_remaining != 0) {
        *out_output_dma_remaining = output_dma_remaining;
    }
    if (out_output_dma_busy != 0) {
        *out_output_dma_busy = output_dma_busy;
    }
    if (out_output_dma_irq_pending != 0) {
        *out_output_dma_irq_pending = output_dma_irq_pending;
    }
    if (out_sample_dma_remaining != 0) {
        *out_sample_dma_remaining = sample_dma_remaining;
    }
    if (out_sample_dma_busy != 0) {
        *out_sample_dma_busy = sample_dma_busy;
    }
    if (out_pio_fdebug != 0) {
        *out_pio_fdebug = pio->fdebug;
    }
    if (out_event_count != 0) {
        *out_event_count = g_runner.event_count;
    }
    if (out_sample_event_count != 0) {
        *out_sample_event_count = g_runner.sample_event_count;
    }
}

void stpg_runner_get_stream_core1_raw(uint32_t *loop_count,
                                      uint32_t *request_seq,
                                      uint32_t *service_seq,
                                      uint32_t *service_slot,
                                      uint32_t *fill_error) {
    __dmb();
    if (loop_count != 0) *loop_count = g_stream_core1_loop_raw;
    if (request_seq != 0) *request_seq = g_stream_core1_request_raw;
    if (service_seq != 0) *service_seq = g_stream_core1_service_raw;
    if (service_slot != 0) *service_slot = g_stream_core1_service_slot_raw;
    if (fill_error != 0) *fill_error = g_stream_core1_fill_error_raw;
}
