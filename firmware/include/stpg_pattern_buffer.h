#ifndef STPG_PATTERN_BUFFER_H
#define STPG_PATTERN_BUFFER_H

#include <stdbool.h>
#include <stdint.h>

#include "stpg_compare.h"
#include "stpg_error.h"
#include "stpg_pattern.h"

typedef struct {
    const uint32_t *words;
    uint32_t word_count;
    uint32_t event_count;
    uint32_t sample_event_count;
} stpg_pio_word_view_t;

typedef struct {
    uint32_t vector_count;
    uint32_t rate_count;
    uint32_t slot_count;
    uint32_t loop_block_count;
    uint32_t expanded_vector_count_estimate;
    uint32_t active_pins;
    bool loaded;
} stpg_rate_info_t;

typedef struct {
    uint32_t body_offset;
    uint32_t body_record_count;
    uint32_t loop_count;
} stpg_loop_desc_t;

typedef struct {
    uint32_t time_ticks;
    uint32_t gap_from_prev_ticks;
    uint32_t gap_from_cycle_start_ticks;
    uint8_t first_slot_index;
    uint8_t slot_count;
    uint32_t sample_mask_or;
} stpg_rate_slot_group_t;

typedef struct {
    uint32_t drive_mask;
    uint32_t packed_drive_mask;
    uint8_t drive_op;
    uint8_t reserved0;
    uint16_t reserved1;
} stpg_rate_group_op_t;

typedef struct {
    uint32_t time_ticks;
    uint32_t gap_from_prev_ticks;
    uint32_t gap_from_cycle_start_ticks;
    uint8_t op_count;
    uint8_t has_sample;
    uint8_t group_flags;
    uint8_t sample_word;
    uint32_t sample_mask_or;
    uint32_t dir_update_mask;
    uint32_t packed_dir_update_mask;
    stpg_rate_group_op_t ops[STPG_MAX_SLOTS_PER_RATE];
#if STPG_STREAM_RATE_TEMPLATE_FILL && !STPG_STREAM_HOT_TELEMETRY && !STPG_STREAM_FAIL_STOP
    /* PIO-window masks composed once; later writes to a bit win. Logical
     * masks remain in ops[] for comparison replay and legacy fallbacks. */
    uint32_t drive_keep_mask;
    uint32_t drive_vector_mask;
    uint32_t drive_one_mask;
    uint32_t drive_complement_mask;
    uint32_t stable_wait_word;
    bool stable_wait_valid;
#endif
} stpg_rate_group_uop_t;

/* Internal representation; v1 wire records are widened on read and v2 values
 * remain wide through the PSRAM ring-loader and PIO-word generation. */
typedef struct {
    uint32_t period_ticks;
    uint8_t slot_count;
    uint8_t flags;
    uint32_t slot_offset;
} stpg_rate_desc_runtime_t;

typedef struct {
    uint32_t time_ticks;
    uint16_t flags;
    uint32_t drive_mask;
    uint32_t dir_mask;
    uint32_t sample_mask;
} stpg_rate_slot_runtime_t;

typedef struct {
    stpg_rate_desc_runtime_t rate;
    uint8_t group_count;
    uint8_t reserved0;
    uint16_t reserved1;
    stpg_rate_slot_runtime_t slots[STPG_MAX_SLOTS_PER_RATE];
    stpg_rate_slot_group_t groups[STPG_MAX_SLOTS_PER_RATE];
} stpg_rate_runtime_t;

typedef enum {
    STPG_PATTERN_SOURCE_NONE = 0,
    STPG_PATTERN_SOURCE_SRAM = 1,
    STPG_PATTERN_SOURCE_PSRAM = 2,
    STPG_PATTERN_SOURCE_FLASH = 3,
} stpg_pattern_source_t;

typedef struct {
    bool valid;
    uint32_t pattern_size;
    uint32_t format;
    uint32_t active_pins;
    uint32_t tick_hz;
} stpg_flash_slot_view_t;

typedef enum {
    STPG_STREAM_KIND_RATE_VECTOR = 0,
    STPG_STREAM_KIND_MACRO_STREAM = 1,
} stpg_stream_kind_t;

typedef struct stpg_rate_stream_state {
    RateVectorHeader header;
    MacroVectorHeader macro_header;
    stpg_loop_desc_t loop_descs[STPG_MAX_LOOP_BLOCKS];
    uint32_t loop_block_count;
    uint32_t vector_index;
    uint32_t repeat_index;
    uint32_t slot_cursor;
    uint32_t current_value;
    uint32_t current_dir;
    uint32_t current_loop_id;
    uint32_t current_loop_cycle;
    uint32_t current_loop_body_index;
    uint32_t prev_time_ticks;
    uint32_t prev_period_ticks;
    uint16_t current_group_index;
    VectorRecord current_vector;
    MacroVectorRecord current_macro_record;
    stpg_rate_desc_runtime_t current_rate;
    stpg_rate_slot_runtime_t cached_slots[STPG_MAX_SLOTS_PER_RATE];
    stpg_rate_group_uop_t current_group_uop;
    stpg_rate_runtime_t rate_runtime[STPG_MAX_RATES];
    /* Share equal group UOPs through rate/group indices. A full cache keeps
     * its entries; only unmapped groups use the generic builder. */
    stpg_rate_group_uop_t precomputed_group_uops[STPG_STREAM_PRECOMPUTED_UOP_CAPACITY];
    uint8_t precomputed_uop_index[STPG_MAX_RATES][STPG_MAX_SLOTS_PER_RATE];
    uint8_t precomputed_uop_count;
    /* All groups are mapped; false does not disable a partial cache. */
    bool precomputed_uops_complete;
    uint32_t cached_rate_id;
    uint32_t macro_drive_value_cached;
    uint32_t macro_drive_enable_cached;
    uint32_t macro_sample_mask_cached;
    uint32_t macro_expect_value_cached;
    ExecEvent macro_compare_proto;
    uint32_t macro_packed_drive_value_cached;
    uint32_t macro_packed_drive_enable_cached;
    uint32_t macro_phase_packed_value[2];
    uint32_t macro_phase_packed_enable[2];
    uint16_t macro_phase_time_ticks[2];
    uint16_t macro_phase_initial_wait_word[2];
    uint16_t macro_phase_cycle_wait_word[2];
    uint16_t macro_phase_intra_wait_word[2];
    uint16_t macro_phase_initial_desired_delta[2];
    uint16_t macro_phase_cycle_desired_delta[2];
    uint16_t macro_phase_intra_desired_delta[2];
    uint16_t macro_phase_initial_required_ticks[2];
    uint16_t macro_phase_cycle_required_ticks[2];
    uint16_t macro_phase_intra_required_ticks[2];
    uint16_t macro_phase_flags[2];
    uint8_t macro_phase_sample_trigger[2];
    uint8_t macro_phase_logical_complete[2];
    uint8_t macro_phase_updates_output[2];
    uint8_t macro_phase_initial_wait_valid[2];
    uint8_t macro_phase_cycle_wait_valid[2];
    uint8_t macro_phase_intra_wait_valid[2];
    uint32_t macro_record_index;
    uint32_t macro_repeat_index;
    stpg_stream_kind_t stream_kind;
    uint8_t macro_phase_count;
    uint8_t macro_event_phase;
    uint16_t reserved2;
    bool vector_loaded;
    bool macro_record_loaded;
    bool have_prev_time;
    bool first_in_cycle;
    bool loop_active;
    bool eof;
    bool rate_cached;
    bool batch_path_enabled;
    bool reduced_density_compare_enabled;
    bool rate_runtime_prepared;
} stpg_rate_stream_state_t;

typedef struct {
    uint32_t event_count;
    uint32_t sample_event_count;
    uint32_t word_count;
    uint32_t p4_explicit_wait_end_vector_index;
    bool p4_explicit_wait_end_present;
    bool eof;
} stpg_stream_chunk_result_t;

typedef enum {
    STPG_STREAM_PREFETCH_DIAG_NONE = 0,
    STPG_STREAM_PREFETCH_DIAG_READ_ERROR = 1,
    STPG_STREAM_PREFETCH_DIAG_VALIDATION_ERROR = 2,
} stpg_stream_prefetch_diag_stage_t;

typedef struct {
    uint32_t rate_vector_read_call_count;
    uint32_t rate_vector_read_byte_total;
    uint32_t rate_vector_read_total_us;
    uint32_t rate_vector_read_max_us;
    uint32_t rate_vector_read_last_byte_count;
    uint32_t rate_vector_read_last_us;
    uint32_t emit_call_count;
    uint32_t sample_event_count;
    uint32_t emit_total_us;
    uint32_t vector_apply_total_us;
    uint32_t pack_total_us;
    uint32_t sample_wait_total_us;
    uint32_t load_vector_call_count;
    uint32_t load_vector_total_us;
    uint32_t fast_vector_count;
    uint32_t fallback_vector_count;
    uint32_t load_cache_lookup_total_us;
    uint32_t load_cache_miss_count;
    uint32_t load_prefetch_wait_total_us;
    uint32_t load_prefetch_wait_count;
    uint32_t load_vector_copy_total_us;
    uint32_t load_state_setup_total_us;
    uint32_t prefetch_call_count;
    uint32_t prefetch_total_us;
    uint32_t compare_bookkeeping_total_us;
    uint32_t fast_group_emit_count;
    uint32_t fast_chunk_cache_span_count;
    uint32_t fast_chunk_cache_span_vectors;
    uint32_t prefetch_record_count_total;
    uint32_t prefetch_byte_total;
    uint32_t prefetch_last_record_count;
    uint32_t prefetch_last_byte_count;
    /* Bounded refill diagnostics.  These fields describe the last failed
     * attempt independently of the published cache metadata. */
    uint32_t prefetch_diag_attempt_base_index;
    uint32_t prefetch_diag_attempt_record_count;
    uint32_t prefetch_diag_attempt_number;
    uint32_t prefetch_diag_last_success_base_index;
    uint32_t prefetch_diag_last_success_record_count;
    uint32_t prefetch_diag_failure_stage;
    uint32_t prefetch_diag_failure_record_index;
    uint32_t prefetch_diag_failure_record_offset;
    uint32_t prefetch_diag_failure_error;
    uint32_t prefetch_diag_failure_attempt_number;
    uint32_t prefetch_diag_failure_words[5];
    uint32_t prefetch_diag_destination_address;
    uint32_t prefetch_diag_destination_bytes;
    uint32_t prefetch_diag_post_read_error;
    uint32_t prefetch_diag_post_read_complete;
    uint32_t prefetch_diag_post_read_words[5];
    uint32_t prefetch_diag_post_validation_words[5];
    uint32_t prefetch_diag_mapped_source_address;
    uint32_t prefetch_diag_mapped_source_words[5];
    uint32_t prefetch_diag_qmi_before_direct_csr;
    uint32_t prefetch_diag_qmi_before_timing;
    uint32_t prefetch_diag_qmi_before_rfmt;
    uint32_t prefetch_diag_qmi_before_rcmd;
    uint32_t prefetch_diag_qmi_before_wfmt;
    uint32_t prefetch_diag_qmi_before_wcmd;
    uint32_t prefetch_diag_qmi_after_direct_csr;
    uint32_t prefetch_diag_qmi_after_timing;
    uint32_t prefetch_diag_qmi_after_rfmt;
    uint32_t prefetch_diag_qmi_after_rcmd;
    uint32_t prefetch_diag_qmi_after_wfmt;
    uint32_t prefetch_diag_qmi_after_wcmd;
    uint32_t prefetch_max_us;
    uint32_t load_vector_copy_byte_total;
    uint32_t fast_store_pack_total_us;
    uint32_t fast_store_total_us;
    uint32_t fast_pack_total_us;
    uint32_t fast_wait_compute_total_us;
    uint32_t fast_compare_sample_count;
    uint32_t fast_vector_read_total_us;
    uint32_t fast_cache_walk_total_us;
    uint32_t fast_repeat_dispatch_total_us;
    uint32_t fast_runtime_access_total_us;
    uint32_t fast_capacity_check_total_us;
    uint32_t fast_compare_write_total_us;
    uint32_t fast_state_commit_total_us;
    uint32_t fast_group_work_total_us;
    uint32_t fast_group_apply_total_us;
    uint32_t fast_group_bookkeeping_total_us;
    uint32_t fast_counter_update_total_us;
    uint32_t fast_loop_bookkeeping_total_us;
    uint32_t fast_store_select_total_us;
    uint32_t fast_branch_control_total_us;
    uint32_t precomputed_uop_cache_capacity;
    uint32_t precomputed_uop_cache_entry_count;
    uint32_t precomputed_uop_cache_hit_count;
    uint32_t precomputed_uop_cache_fallback_count;
    uint32_t precomputed_uop_cache_build_count;
    uint32_t sample_event_scan_count;
    uint32_t practical_group_apply_total_us;
    uint32_t practical_wait_store_total_us;
    uint32_t practical_compare_write_total_us;
    uint32_t practical_commit_control_total_us;
    uint32_t macro_record_read_call_count;
    uint32_t macro_record_read_total_us;
    uint32_t macro_record_read_max_us;
    uint32_t macro_record_read_last_us;
    uint32_t macro_build_event_total_us;
    uint32_t macro_wait_compute_total_us;
    uint32_t macro_compare_write_total_us;
    uint32_t macro_state_commit_total_us;
    uint32_t macro_repeat_advance_total_us;
    uint32_t mismatch_stage;
    uint32_t mismatch_rate_id;
    uint32_t mismatch_index;
    uint32_t mismatch_desired_delta_ticks;
    uint32_t mismatch_required_ticks;
    uint32_t mismatch_period_ticks;
    uint32_t mismatch_prev_time_ticks;
    uint32_t mismatch_time_ticks;
    uint32_t mismatch_detail_flags;
    uint32_t mismatch_group_count;
    uint32_t mismatch_gap_from_cycle_start_ticks;
    uint32_t mismatch_gap_from_prev_ticks;
    uint32_t last_chunk_vector_start;
    uint32_t last_chunk_vector_end;
    uint32_t last_chunk_emitted_event_start;
    uint32_t last_chunk_emitted_event_end;
    uint32_t last_chunk_processed_vectors;
    uint32_t last_chunk_group_emit_count;
    /* Boundary diagnostics: updated at vector/rate boundaries, not per PIO event. */
    uint32_t vector_observation_count;
    uint32_t rate_transition_count;
    uint32_t repeat_gt1_count;
    uint32_t last_vector_index;
    uint32_t last_vector_rate_id;
    uint32_t last_vector_repeat_index;
    uint32_t last_vector_repeat_count;
    uint32_t last_vector_period_ticks;
    uint32_t last_vector_prev_period_ticks;
    uint32_t last_vector_desired_delta_ticks;
    uint32_t last_vector_output_changed;
    uint32_t last_rate_transition_vector_index;
    uint32_t last_rate_transition_from_rate_id;
    uint32_t last_rate_transition_to_rate_id;
    uint32_t last_rate_transition_from_period_ticks;
    uint32_t last_rate_transition_to_period_ticks;
    uint32_t last_rate_transition_repeat_index;
    uint32_t last_rate_transition_prev_time_ticks;
    uint32_t last_rate_transition_desired_delta_ticks;
    uint32_t last_rate_transition_output_changed;
    /* Repeat-aware first-group boundary calculation diagnostics. */
    uint32_t boundary_observation_count;
    uint32_t last_boundary_kind;
    uint32_t last_boundary_vector_index;
    uint32_t last_boundary_rate_id;
    uint32_t last_boundary_repeat_index;
    uint32_t last_boundary_repeat_count;
    uint32_t last_boundary_period_ticks;
    uint32_t last_boundary_prev_period_ticks;
    uint32_t last_boundary_prev_time_ticks;
    uint32_t last_boundary_group_time_ticks;
    uint32_t last_boundary_desired_delta_ticks;
    uint32_t last_boundary_output_changed;
    /* Preserved-loop execution must not reread a short encoded body from
     * PSRAM for every expanded cycle.  These counters attest the bounded
     * active-loop SRAM cache independently of top-level vector prefetch. */
    uint32_t loop_body_cache_lookup_count;
    uint32_t loop_body_cache_hit_count;
    uint32_t loop_body_cache_miss_count;
    uint32_t loop_body_cache_load_count;
    uint32_t loop_body_cache_record_count;
    uint32_t loop_body_cache_byte_count;
    uint32_t loop_body_cache_validation_retry_count;
    uint32_t rate_runtime_cache_load_count;
    uint32_t rate_runtime_cache_slot_copy_count;
} stpg_stream_buffer_telemetry_t;

typedef struct {
    uint32_t global_vector_start;
    uint32_t global_vector_end;
    uint32_t emitted_event_start;
    uint32_t emitted_event_end;
    uint32_t load_vector_total_us;
    uint32_t processed_vectors;
    uint32_t vector_prep_total_us;
    uint32_t group_work_total_us;
    uint32_t compare_write_total_us;
    uint32_t store_select_total_us;
    uint32_t branch_control_total_us;
    uint32_t group_emit_count;
    uint32_t practical_group_apply_total_us;
    uint32_t practical_wait_store_total_us;
    uint32_t practical_compare_write_total_us;
    uint32_t practical_commit_control_total_us;
} stpg_stream_hotspot_entry_t;

typedef struct {
    uint32_t recorded_chunk_count;
    uint32_t recorded_subwindow_count;
    uint32_t subwindow_width_vectors;
    uint32_t current_run_chunk_count;
    uint32_t hottest_chunk_rank_count;
    uint32_t hottest_subwindow_rank_count;
} stpg_stream_hotspot_meta_t;

void stpg_buffer_reset(void);
stpg_err_t stpg_buffer_begin(uint32_t storage_target, uint32_t total_size,
                              uint32_t total_crc, uint32_t flags);
stpg_err_t stpg_buffer_precheck_chunk(uint32_t offset, uint32_t size);
stpg_err_t stpg_buffer_write_chunk(uint32_t offset, const void *data,
                                    uint32_t size, uint32_t chunk_crc);
stpg_err_t stpg_buffer_finalize(uint32_t total_size, uint32_t total_crc);
uint32_t stpg_buffer_received_size(void);
uint32_t stpg_buffer_expected_size(void);
uint32_t stpg_buffer_pattern_size(void);
uint32_t stpg_buffer_event_count(void);
uint32_t stpg_buffer_sample_event_count(void);
uint32_t stpg_buffer_active_pins(void);
bool stpg_buffer_has_pattern(void);
stpg_pattern_source_t stpg_buffer_active_source(void);
stpg_pattern_format_t stpg_buffer_active_format(void);
stpg_err_t stpg_buffer_prepare_for_run(void);
stpg_err_t stpg_buffer_prepare_measurement_family(uint32_t family_id,
                                                  uint32_t period_ticks,
                                                  uint32_t phase_ticks,
                                                  uint32_t cycle_count);
stpg_err_t stpg_buffer_prepare_measurement_sequence(uint32_t sequence_id,
                                                    uint32_t period_ticks,
                                                    uint32_t phase_ticks,
                                                    uint32_t cycle_count);
stpg_err_t stpg_buffer_prepare_measurement_burst(uint32_t burst_id,
                                                 uint32_t period_ticks,
                                                 uint32_t phase_ticks,
                                                 uint32_t cycle_count);
const ExecEvent *stpg_buffer_events(void);
const uint32_t *stpg_buffer_pio_words(void);
uint32_t stpg_buffer_pio_word_count(void);
stpg_pio_word_view_t stpg_buffer_pio_word_view(void);
stpg_err_t stpg_buffer_psram_checksum(uint32_t offset, uint32_t size, uint32_t *out_crc32);
stpg_err_t stpg_buffer_psram_read_prepare(uint32_t offset, uint32_t size, uint32_t *out_crc32);
stpg_err_t stpg_buffer_psram_read_bytes(uint32_t offset, void *dst, uint32_t size);
stpg_err_t stpg_buffer_psram_erase(uint32_t offset, uint32_t size);
stpg_err_t stpg_buffer_flash_list(uint32_t *out_valid_bitmap, uint32_t *out_slot_count, uint32_t *out_slot_size);
stpg_err_t stpg_buffer_flash_get_slot_info(uint32_t slot_id, stpg_flash_slot_view_t *out_view);
stpg_err_t stpg_buffer_flash_select_run(uint32_t slot_id);
stpg_err_t stpg_buffer_flash_select_write(uint32_t slot_id);
stpg_err_t stpg_buffer_flash_erase_slot(uint32_t slot_id);
stpg_err_t stpg_buffer_get_rate_info(stpg_rate_info_t *out_info);
/* Direct expansion and streaming are mutually exclusive run modes.  Keep the
 * large Rate Vector state in one statically allocated owner so neither path
 * can place it on the 2 KiB core-0 stack. */
stpg_rate_stream_state_t *stpg_buffer_stream_state_storage(void);
stpg_err_t stpg_buffer_prepare_for_streaming(stpg_rate_stream_state_t *out_state, uint32_t run_flags);
stpg_err_t stpg_buffer_stream_fill_next_chunk(stpg_rate_stream_state_t *state, uint32_t *dst_words,
                                              uint32_t max_events, stpg_stream_chunk_result_t *out_result);
stpg_err_t stpg_buffer_stream_fill_next_chunk_reduced_compare(stpg_rate_stream_state_t *state, uint32_t *dst_words,
                                                              uint32_t *dst_sample_waits,
                                                              uint32_t max_events,
                                                              stpg_stream_chunk_result_t *out_result);
#if defined(STPG_STREAM_PSRAM_COMPARE_SAMPLE) && STPG_STREAM_PSRAM_COMPARE_SAMPLE
stpg_err_t stpg_buffer_stream_compare_replay_init(stpg_rate_stream_state_t *out_state);
stpg_err_t stpg_buffer_stream_compare_replay_fill_next(stpg_rate_stream_state_t *state,
                                                       stpg_stream_chunk_result_t *out_result);
#endif
const ExecEvent *stpg_buffer_stream_compare_events(void);
const uint32_t *stpg_buffer_stream_compare_event_indices(void);
uint32_t stpg_buffer_stream_compare_event_count(void);
stpg_err_t stpg_buffer_stream_compare_begin_metadata_slot(uint32_t destination_offset);
const ExecEvent *stpg_buffer_stream_compare_events_at(uint32_t offset);
const uint32_t *stpg_buffer_stream_compare_event_indices_at(uint32_t offset);
bool stpg_buffer_stream_has_samples(void);
uint32_t stpg_buffer_stream_expected_sample_count(void);
void stpg_buffer_snapshot_stream_telemetry(void);
void stpg_buffer_clear_stream_telemetry_snapshot(void);
void stpg_buffer_get_stream_telemetry(stpg_stream_buffer_telemetry_t *out);
void stpg_buffer_get_stream_hotspot_meta(stpg_stream_hotspot_meta_t *out);
bool stpg_buffer_get_stream_hotspot_chunk(uint32_t rank, stpg_stream_hotspot_entry_t *out);
void stpg_buffer_get_measurement_debug(uint32_t *out_family_id,
                                       uint32_t *out_period_ticks,
                                       uint32_t *out_phase_ticks,
                                       uint32_t *out_cycle_count);
uint32_t *stpg_buffer_streaming_scratch_words(void);
uint32_t stpg_buffer_streaming_scratch_word_capacity(void);

#endif
