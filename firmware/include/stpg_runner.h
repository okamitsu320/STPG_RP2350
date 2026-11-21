#ifndef STPG_RUNNER_H
#define STPG_RUNNER_H

#include <stdbool.h>
#include <stdint.h>

#include "stpg_fail_stop.h"
#include "stpg_error.h"

#define STPG_DMA_COMPLETION_TRACE_CAPACITY 8u
#define STPG_REFILL_TRACE_CAPACITY 64u

enum {
    STPG_DMA_TRACE_PATH_CHAIN_HANDOFF = 1u << 0,
    STPG_DMA_TRACE_PATH_NORMAL_RESTART = 1u << 1,
    STPG_DMA_TRACE_PATH_MAIN_LOOP_HANDOFF = 1u << 2,
    STPG_DMA_TRACE_PATH_TERMINAL = 1u << 3,
    STPG_DMA_TRACE_PATH_NO_READY = 1u << 4,
    STPG_DMA_TRACE_PATH_CHUNK_BOUNDARY = 1u << 5,
};

typedef struct {
    uint32_t completion_index;
    uint32_t chunk_index;
    uint32_t event_start;
    uint32_t event_end;
    uint32_t boundary_event;
    uint32_t path_flags;
    uint32_t completed_slot;
    uint32_t armed_slot;
    uint32_t next_ready_slot;
    uint32_t restart_us;
    uint32_t handler_us;
    uint32_t dma_remaining;
    uint32_t pio_flevel;
    uint32_t pio_fstat;
} stpg_dma_completion_trace_t;

#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
typedef struct {
    uint32_t sequence;
    uint32_t slot_index;
    uint32_t event_start;
    uint32_t event_end;
    uint32_t request_us;
    uint32_t claim_us;
    uint32_t body_start_us;
    uint32_t body_end_us;
    uint32_t ready_us;
    uint32_t promote_us;
    uint32_t arm_us;
    uint32_t fill_error;
} stpg_refill_trace_t;
#endif

typedef struct {
    uint32_t refill_count;
    uint32_t refill_event_count;
    uint32_t refill_total_us;
    uint32_t refill_max_us;
    uint32_t refill_last_us;
    uint32_t refill_last_event_count;
    uint32_t swap_count;
    uint32_t underflow_count;
    uint32_t irq_swap_count;
    uint32_t task_swap_count;
    uint32_t queue_slot_count;
    uint32_t slot_event_capacity;
    uint32_t startup_prefill_slots;
    uint32_t startup_prefill_total_us;
    uint32_t min_ready_slots_observed;
    uint32_t swap_restart_total_us;
    uint32_t swap_restart_max_us;
    uint32_t underflow_reason;
    uint32_t sample_schedule_word_count;
    uint32_t sample_request_count;
    uint32_t sample_capture_count;
    uint32_t compare_event_count;
    uint32_t core1_loop_count;
    uint32_t core1_request_count;
    uint32_t core1_service_count;
    uint32_t core1_last_slot_index;
    uint32_t core1_last_fill_error;
    uint32_t startup_prefill_attempted_slots;
    uint32_t startup_prefill_last_slot_index;
    uint32_t startup_prefill_break_reason;
    uint32_t startup_prefill_wait_error;
    uint32_t startup_prefill_last_event_count;
    uint32_t startup_prefill_last_word_count;
    uint32_t startup_timeout_core1_loop_count;
    uint32_t startup_timeout_core1_service_count;
    uint32_t startup_timeout_stop_requested;
    uint32_t startup_timeout_fill_request_slot;
    uint32_t startup_timeout_core1_boot_stage_raw;
    uint32_t startup_timeout_core1_request_raw;
    uint32_t startup_timeout_core1_service_raw;
    uint32_t startup_timeout_core1_fill_error_raw;
    uint32_t startup_timeout_core1_request_seen_raw;
    uint32_t startup_timeout_core1_claim_raw;
    uint32_t startup_timeout_core1_skip_stop_raw;
    uint32_t startup_timeout_core1_skip_no_request_raw;
    uint32_t startup_timeout_core1_last_seen_slot_raw;
    uint32_t startup_timeout_core1_last_polled_slot_raw;
    uint32_t startup_timeout_core1_negative_poll_count_raw;
    uint32_t startup_timeout_core1_valid_poll_count_raw;
    uint32_t startup_timeout_core1_invalid_poll_count_raw;
    uint32_t startup_timeout_core1_last_polled_fill_error_raw;
    uint32_t startup_timeout_core1_last_polled_slot0_ready_raw;
    uint32_t startup_timeout_request_set_us;
    uint32_t startup_timeout_request_clear_us;
    uint32_t startup_timeout_core1_first_poll_us;
    uint32_t startup_timeout_core1_first_negative_poll_us;
    uint32_t startup_timeout_core1_first_valid_poll_us;
    uint32_t startup_timeout_core1_last_stage_raw;
    uint32_t startup_timeout_core1_after_stop_gate_us;
    uint32_t startup_timeout_core1_after_dmb_us;
    uint32_t startup_timeout_core1_after_slot_read_us;
    uint32_t visibility_probe_request_set_us;
    uint32_t visibility_probe_core1_saw_request_us;
    uint32_t visibility_probe_core1_write1_us;
    uint32_t visibility_probe_core1_write2_us;
    uint32_t visibility_probe_core1_done_us;
    uint32_t visibility_probe_core0_first_stage_seen_us;
    uint32_t visibility_probe_core0_first_value1_seen_us;
    uint32_t visibility_probe_core0_first_value2_seen_us;
    uint32_t visibility_probe_core0_first_done_seen_us;
    uint32_t visibility_probe_final_stage;
    uint32_t visibility_probe_final_value1;
    uint32_t visibility_probe_final_value2;
    uint32_t visibility_probe_final_done;
    uint32_t visibility_probe_timeout_hit;
    uint32_t handshake_probe_request_set_us;
    uint32_t handshake_probe_core1_saw_request_us;
    uint32_t handshake_probe_core1_claim_us;
    uint32_t handshake_probe_core1_ready_us;
    uint32_t handshake_probe_core0_ready_seen_us;
    uint32_t handshake_probe_core0_error_seen_us;
    uint32_t handshake_probe_final_request_slot;
    uint32_t handshake_probe_final_ready;
    uint32_t handshake_probe_final_error;
    uint32_t handshake_probe_timeout_hit;
    uint32_t startup_timeout_shared_fill_request_slot;
    uint32_t startup_timeout_shared_slot0_ready;
    uint32_t startup_timeout_shared_fill_error;
    uint32_t startup_timeout_shared_slot0_event_count;
    uint32_t startup_timeout_shared_slot0_word_count;
    uint32_t fail_snapshot_status_state;
    uint32_t fail_snapshot_runner_flags;
    uint32_t fail_snapshot_shared_fill_request_slot;
    uint32_t fail_snapshot_shared_fill_error;
    uint32_t fail_snapshot_from_irq;
    uint32_t dma_abort_output_us;
    uint32_t dma_abort_sample_us;
    uint32_t dma_abort_sample_sched_us;
    uint32_t dma_abort_total_us;
    uint32_t startup_first_fill_request_us;
    uint32_t startup_first_service_complete_us;
    uint32_t startup_slot0_ready_us;
    uint32_t recovery_live_main_loop_count;
    uint32_t recovery_live_main_loop_stage;
    uint32_t recovery_live_usb_task_count;
    uint32_t recovery_live_tud_task_count;
    uint32_t recovery_fail_stage;
    uint32_t recovery_fail_main_loop_count;
    uint32_t recovery_fail_usb_task_count;
    uint32_t recovery_fail_tud_task_count;
    uint32_t recovery_fail_dma_irq0_pending;
    uint32_t recovery_fail_dma_irq0_enabled;
    uint32_t recovery_live_usb_available_poll_count;
    uint32_t recovery_live_usb_rx_read_call_count;
    uint32_t recovery_live_usb_rx_byte_count;
    uint32_t recovery_live_usb_frame_complete_count;
    uint32_t recovery_live_usb_dispatch_call_count;
    uint32_t recovery_live_usb_write_response_count;
    uint32_t recovery_fail_usb_available_poll_count;
    uint32_t recovery_fail_usb_rx_read_call_count;
    uint32_t recovery_fail_usb_rx_byte_count;
    uint32_t recovery_fail_usb_frame_complete_count;
    uint32_t recovery_fail_usb_dispatch_call_count;
    uint32_t recovery_fail_usb_write_response_count;
    uint32_t recovery_live_usb_write_spin_count;
    uint32_t recovery_live_usb_last_write_success_bytes;
    uint32_t recovery_live_usb_last_payload_error_code;
    uint32_t recovery_fail_usb_write_spin_count;
    uint32_t recovery_fail_usb_last_write_success_bytes;
    uint32_t recovery_fail_usb_last_payload_error_code;
    uint32_t underflow_snapshot_active_slot;
    uint32_t underflow_snapshot_next_requested_slot;
    uint32_t underflow_snapshot_next_ready_slot;
    uint32_t underflow_snapshot_ready_mask;
    uint32_t underflow_snapshot_request_seq;
    uint32_t underflow_snapshot_service_seq;
    uint32_t underflow_snapshot_last_service_slot;
    uint32_t underflow_snapshot_last_service_event_count;
    uint32_t underflow_snapshot_last_service_word_count;
    uint32_t underflow_snapshot_last_service_sample_count;
    uint32_t underflow_snapshot_queued_event_count;
    uint32_t underflow_snapshot_reason;
    uint32_t prod_noready_defer_count;
    uint32_t prod_noready_recheck_success_count;
    uint32_t prod_noready_recheck_fail_count;
    uint32_t prod_noready_safety_reject_count;
    uint32_t prod_noready_last_iterations;
    uint32_t prod_noready_last_wait_us;
    uint32_t prod_noready_wait_step_us;
    uint32_t prod_noready_max_iterations;
    uint32_t prod_noready_defer_dma_busy;
    uint32_t prod_noready_defer_dma_remaining;
    uint32_t prod_noready_defer_pio_txempty;
    uint32_t prod_noready_defer_completed_slot;
    uint32_t prod_noready_defer_queued_events;
    uint32_t prod_noready_defer_safety_flags;
    uint32_t underflow_snapshot_vector_index;
    uint32_t underflow_snapshot_repeat_index;
    uint32_t underflow_snapshot_rate_id;
    uint32_t underflow_snapshot_period_ticks;
    uint32_t underflow_snapshot_prev_period_ticks;
    uint32_t underflow_snapshot_desired_delta_ticks;
    uint32_t underflow_snapshot_vector_repeat;
    uint32_t underflow_snapshot_vector_flags;
    uint32_t underflow_snapshot_current_value;
    uint32_t underflow_snapshot_current_dir;
    uint32_t underflow_snapshot_state_flags;
    uint32_t p3_stream_run_sequence;
    uint32_t p3_telemetry_sequence;
    uint32_t p3_chain_arm_count;
    uint32_t p3_chain_completion_count;
    uint32_t p3_no_ready_fail_count;
    uint32_t p3_terminal_chain_neutralize_count;
    uint32_t p3_no_ready_chain_neutralize_count;
    uint32_t p3_max_irq_to_arm_us;
    uint32_t p3_handoff_owner;
    uint32_t p3_handoff_conflict_count;
    uint32_t p3_irq_handoff_count;
    uint32_t p3_main_loop_handoff_count;
    uint32_t p3_main_loop_handoff_last_us;
    uint32_t p3_main_loop_handoff_max_us;
    uint32_t p3_main_loop_rearm_count;
    uint32_t p3_main_loop_rearm_last_us;
    uint32_t p3_main_loop_idle_fallback_count;
    uint32_t p4_diag_marker_slot;
    uint32_t p4_diag_marker_vector_index;
    uint32_t p4_diag_observation_count;
    uint32_t p4_diag_active_dma_remaining;
    uint32_t p4_diag_active_dma_busy;
    uint32_t p4_diag_promotable_slot;
    uint32_t p4_diag_promotable_count;
    uint32_t p4_diag_first_promotable_us;
    uint32_t p4_diag_spare_state;
    uint32_t p4_diag_ready_before_end;
    uint32_t dma_pio_boundary_count;
    uint32_t dma_pio_last_entry_channel;
    uint32_t dma_pio_last_entry_irq_pending;
    uint32_t dma_pio_last_entry_busy;
    uint32_t dma_pio_last_entry_remaining;
    uint32_t dma_pio_last_entry_fstat;
    uint32_t dma_pio_last_entry_fdebug;
    uint32_t dma_pio_last_entry_flevel;
    uint32_t dma_pio_last_exit_channel;
    uint32_t dma_pio_last_exit_busy;
    uint32_t dma_pio_last_exit_remaining;
    uint32_t dma_pio_last_exit_fstat;
    uint32_t dma_pio_last_exit_fdebug;
    uint32_t dma_pio_last_exit_flevel;
    uint32_t dma_pio_last_handler_us;
    uint32_t dma_completion_trace_count;
    uint32_t dma_completion_trace_overflow;
    uint32_t dma_completion_trace_boundary_count;
    uint32_t dma_completion_trace_max_restart_us;
    uint32_t dma_completion_trace_last_path_flags;
    stpg_dma_completion_trace_t dma_completion_trace[STPG_DMA_COMPLETION_TRACE_CAPACITY];
#ifdef STPG_REFILL_TRACE_DIAGNOSTIC
    uint32_t refill_trace_count;
    uint32_t refill_trace_overflow;
    stpg_refill_trace_t refill_trace[STPG_REFILL_TRACE_CAPACITY];
#endif
} stpg_stream_telemetry_t;

void stpg_runner_init(void);
void stpg_runner_task(void);
/* Core0 cooperative USB waits only. Never starts a RUN or advances output
 * handoffs/replay; false means staging failed and the runner was stopped. */
bool stpg_runner_service_samples(void);
/* Read-only page75: steps, events, samples, compares, step total_us/max_us.
 * Counters exclude replay initialization and wrap modulo uint32. */
bool stpg_runner_terminal_replay_diagnostics(uint32_t words[6]);
enum {
    STPG_CHAIN_IRQ_REJECT_NONE = 0u,
    STPG_CHAIN_IRQ_REJECT_SLOT_INDEX = 1u,
    STPG_CHAIN_IRQ_REJECT_DMA_OBSERVATION = 2u,
    STPG_CHAIN_IRQ_REJECT_SLOT_OWNERSHIP = 3u,
    STPG_CHAIN_IRQ_REJECT_NONTERMINAL_COMPLETE = 4u,
    STPG_CHAIN_IRQ_REJECT_REDUCED_COMPLETE = 5u,
    STPG_CHAIN_IRQ_REJECT_SLOT_TRANSITION = 6u,
};
/* Read-only page80: first chained-IRQ reject site, error, completed index,
 * armed index (raw int32), observations, target remaining count.
 * Observations: bit0=count observed, bit1=BUSY, bit2=EOF, bit3=reduced,
 * bit4=chain enabled; completed/armed states at bits8..15/16..23 (255=absent).
 * An unobserved count is UINT32_MAX with bit0 clear; an observed raw all-ones
 * count retains bit0 set. Zero site means no covered rejection,
 * not proof that no other failure occurred. Reset at stream initialization. */
bool stpg_runner_chained_irq_diagnostics(uint32_t words[6]);
stpg_err_t stpg_runner_start(uint32_t run_flags,
                             uint32_t start_event_index,
                             uint32_t max_events,
                             uint32_t aux_arg);
/* Validate and queue a non-measurement protocol RUN so its USB response is
 * emitted before the potentially blocking PSRAM/PIO/DMA start path runs. */
stpg_err_t stpg_runner_request_start(uint32_t run_flags,
                                     uint32_t start_event_index,
                                     uint32_t max_events,
                                     uint32_t aux_arg);
bool stpg_runner_start_pending(void);
stpg_err_t stpg_runner_stop(void);
bool stpg_runner_is_active(void);
bool stpg_runner_is_draining(void);
bool stpg_runner_hold_awaiting_next(void);
bool stpg_runner_hold_state(uint32_t *out_drive_value, uint32_t *out_drive_enable);
void stpg_runner_get_stream_telemetry(stpg_stream_telemetry_t *out);
void stpg_runner_get_start_debug(uint32_t *out_stage,
                                 uint32_t *out_run_flags,
                                 uint32_t *out_arg1,
                                 uint32_t *out_arg2,
                                 uint32_t *out_arg3,
                                 uint32_t *out_active,
                                 uint32_t *out_draining);
void stpg_runner_get_direct_debug(uint32_t *out_output_dma_remaining,
                                  uint32_t *out_output_dma_busy,
                                  uint32_t *out_output_dma_irq_pending,
                                  uint32_t *out_sample_dma_remaining,
                                  uint32_t *out_sample_dma_busy,
                                  uint32_t *out_pio_fdebug,
                                  uint32_t *out_event_count,
                                  uint32_t *out_sample_event_count);
void stpg_runner_get_stream_core1_raw(uint32_t *loop_count,
                                      uint32_t *request_seq,
                                      uint32_t *service_seq,
                                      uint32_t *service_slot,
                                      uint32_t *fill_error);

#endif
