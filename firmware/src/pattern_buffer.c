#include "stpg_pattern_buffer.h"

#include <string.h>

#ifndef STPG_STREAM_HOT_TELEMETRY
#define STPG_STREAM_HOT_TELEMETRY 1
#endif

#ifndef STPG_STREAM_COMPARE_FREE_BLOCK_FILL
#define STPG_STREAM_COMPARE_FREE_BLOCK_FILL 0
#endif

#ifndef STPG_STREAM_PRODUCER_SRAM
#define STPG_STREAM_PRODUCER_SRAM 0
#endif

#ifndef STPG_STREAM_PARTIAL_REPEAT_FAST_PATH
#define STPG_STREAM_PARTIAL_REPEAT_FAST_PATH 0
#endif

#ifndef STPG_STREAM_REPEAT_BLOCK_FILL
#define STPG_STREAM_REPEAT_BLOCK_FILL 0
#endif

#ifndef STPG_STREAM_REPEAT_BOUNDARY_FILL
#define STPG_STREAM_REPEAT_BOUNDARY_FILL 0
#endif

#ifndef STPG_STREAM_REPEAT_BOUNDARY_CONTINUE
#define STPG_STREAM_REPEAT_BOUNDARY_CONTINUE 0
#endif

#ifndef STPG_STREAM_REPEAT_BOUNDARY_REARM
#define STPG_STREAM_REPEAT_BOUNDARY_REARM 0
#endif

#ifndef STPG_STREAM_COMPARE_SAMPLE_REPAIR
#define STPG_STREAM_COMPARE_SAMPLE_REPAIR 0
#endif

#ifndef STPG_STREAM_COMPARE_MIXED_RATE_REPAIR
#define STPG_STREAM_COMPARE_MIXED_RATE_REPAIR 0
#endif

#ifndef STPG_STREAM_COMPARE_EVENT_SCHEDULE
#define STPG_STREAM_COMPARE_EVENT_SCHEDULE 0
#endif

#ifndef STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF
#define STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF 0
#endif

#ifndef STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF
#define STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF 0
#endif

#ifndef STPG_STREAM_LARGE_RATE_VECTOR
#define STPG_STREAM_LARGE_RATE_VECTOR 0
#endif

#ifndef STPG_STREAM_PSRAM_CAPACITY_RATE_VECTOR
#define STPG_STREAM_PSRAM_CAPACITY_RATE_VECTOR 0
#endif

#ifndef STPG_STREAM_PSRAM_COMPARE_SAMPLE
#define STPG_STREAM_PSRAM_COMPARE_SAMPLE 0
#endif

#ifndef STPG_STREAM_FAIL_STOP
#define STPG_STREAM_FAIL_STOP 0
#endif

#ifndef STPG_STREAM_LOOP_BATCH
#define STPG_STREAM_LOOP_BATCH 0
#endif

#ifndef STPG_STREAM_DENSE_VECTOR_FILL
#define STPG_STREAM_DENSE_VECTOR_FILL 0
#endif

#ifndef STPG_STREAM_RATE_VECTOR_EVENT_LIMIT
#if STPG_STREAM_PSRAM_CAPACITY_RATE_VECTOR
#define STPG_STREAM_RATE_VECTOR_EVENT_LIMIT 0xFFFFFFFFu
#elif STPG_STREAM_LARGE_RATE_VECTOR
#define STPG_STREAM_RATE_VECTOR_EVENT_LIMIT 40960u
#else
#define STPG_STREAM_RATE_VECTOR_EVENT_LIMIT STPG_MAX_EVENT_COUNT
#endif
#endif

/* The ordinary expanded-event buffers remain bounded by
 * STPG_MAX_EVENT_COUNT.  The configurable limit is only for compressed
 * Rate Vector streaming; no full expansion is permitted above the SRAM
 * capacity.  The PSRAM compare/sample build keeps only per-chunk metadata in
 * SRAM and replays it after output capture; other oversized formats remain
 * fail-closed. */

#if STPG_STREAM_PRODUCER_SRAM && defined(PICO_ON_DEVICE)
#define STPG_STREAM_PRODUCER_FUNC(name) __no_inline_not_in_flash_func(name)
#else
#define STPG_STREAM_PRODUCER_FUNC(name) name
#endif

#define STPG_GROUP_FLAG_HAS_SAMPLE        0x01u
#define STPG_GROUP_FLAG_HAS_DIR_UPDATE    0x02u
#define STPG_GROUP_FLAG_COMPARE_CANDIDATE 0x04u

#ifdef PICO_ON_DEVICE
#define STPG_QMI_DIAGNOSTIC_CAPTURE 1
#include "pico/time.h"
#include "hardware/sync.h"
#include "hardware/structs/qmi.h"
#if !STPG_STREAM_HOT_TELEMETRY
/* Every PICO_ON_DEVICE conditional in this translation unit is timing or
 * hot-loop telemetry. Keep the SDK include above, then remove those blocks
 * from the instrumentation-off diagnostic build. */
#undef PICO_ON_DEVICE
#endif
#endif

#ifndef STPG_QMI_DIAGNOSTIC_CAPTURE
#define STPG_QMI_DIAGNOSTIC_CAPTURE 0
#endif

#include "stpg_board.h"
#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_flash.h"
#include "stpg_pattern_validate.h"
#include "stpg_psram.h"
#include "stpg_protocol.h"

static uint8_t g_pattern_buffer[STPG_MAX_PATTERN_BYTES];
/* Small PSRAM uploads are already read-back verified chunk by chunk.  Keep a
 * bounded SRAM shadow in the existing pattern buffer so upload finalization
 * can validate the complete image without synchronously rereading PSRAM. */
static uint32_t g_psram_upload_shadow_size;
static const uint8_t *g_rate_vector_finalize_shadow;
static uint32_t g_rate_vector_finalize_shadow_size;
static uint32_t g_pio_words[STPG_MAX_EVENT_COUNT * 4u];
static ExecEvent g_expanded_events[STPG_MAX_EVENT_COUNT];

/* Wide raw events use PioWord4's 32-bit wait, but comparison metadata keeps
 * the stable ExecEvent ABI. The wait field is not consumed by compare/hold;
 * only the pin and compare fields are copied here. */
static stpg_err_t stpg_buffer_materialize_wide_compare_events(const uint8_t *buf,
                                                               uint32_t size,
                                                               uint32_t event_count) {
    if (buf == 0 || size < STPG_HEADER_SIZE || event_count > STPG_MAX_EVENT_COUNT) {
        return ERR_PATTERN_INVALID;
    }
    const PatternHeader *header = (const PatternHeader *)(const void *)buf;
    const uint8_t *event_data = buf + STPG_HEADER_SIZE;
    if (header->event_size != STPG_EXEC_EVENT32_SIZE
        || (size_t)header->data_size + STPG_HEADER_SIZE != size) {
        return ERR_PATTERN_INVALID;
    }
    for (uint32_t index = 0u; index < event_count; ++index) {
        const ExecEvent32 *wide = (const ExecEvent32 *)(const void *)
            (event_data + (index * STPG_EXEC_EVENT32_SIZE));
        ExecEvent *stable = &g_expanded_events[index];
        memset(stable, 0, sizeof(*stable));
        stable->wait_ticks = (uint16_t)(wide->wait_ticks > STPG_MAX_WAIT_TICKS
            ? STPG_MAX_WAIT_TICKS : wide->wait_ticks);
        stable->flags = wide->flags;
        stable->out_value = wide->out_value;
        stable->out_enable = wide->out_enable;
        stable->sample_mask = wide->sample_mask;
        stable->expect_value = wide->expect_value;
    }
    return ERR_NONE;
}
static VectorRecord g_stream_vector_cache[STPG_STREAM_VECTOR_CACHE_RECORDS];
static VectorRecord g_stream_vector_cache_next[STPG_STREAM_VECTOR_CACHE_NEXT_RECORDS];
static uint32_t g_expected_total_size;
static uint32_t g_expected_total_crc;
static uint32_t g_received_size;
static uint32_t g_pattern_size;
static uint32_t g_event_count;
static uint32_t g_sample_event_count;
static uint32_t g_stream_compare_event_count;
static uint32_t g_stream_compare_event_base;
static uint32_t g_stream_emitted_event_count;
static uint32_t g_stream_expected_sample_count;
static uint32_t g_pio_word_count;
static uint32_t g_active_pins;
static uint32_t g_stream_vector_cache_base_index;
static uint32_t g_stream_vector_cache_count;
static uint32_t g_stream_vector_cache_next_base_index;
static uint32_t g_stream_vector_cache_next_count;
/* The active top-level cache is reused while a preserved loop is executing.
 * Only this metadata gives the storage loop-body meaning, and the two
 * interpretations are never published at the same time. */
static uint32_t g_stream_loop_body_cache_id = UINT32_MAX;
static uint32_t g_stream_loop_body_cache_base_index;
static uint32_t g_stream_loop_body_cache_count;
static uint32_t g_measurement_family_id;
static uint32_t g_measurement_period_ticks;
static uint32_t g_measurement_phase_ticks;
static uint32_t g_measurement_cycle_count;
static bool g_has_pattern;
static bool g_use_expanded_event_view;
static bool g_stream_compare_collecting;
static bool g_stream_has_samples;
static stpg_stream_buffer_telemetry_t g_stream_telemetry;
static stpg_stream_buffer_telemetry_t g_stream_telemetry_snapshot;
static bool g_stream_telemetry_snapshot_valid;
/*
 * This state is intentionally the sole production owner for both direct
 * Rate Vector expansion and streaming.  The two run modes are mutually
 * exclusive, and the object is far larger than the core-0 stack reservation.
 */
static stpg_rate_stream_state_t g_rate_stream_state_storage;

/*
 * Rate Vector admission, upload finalization, direct expansion, and streaming
 * startup are mutually exclusive.  Reuse the sole production stream-state
 * owner for their bounded loop-descriptor scratch instead of placing the
 * 128-entry table on core 0's 2 KiB stack.  The table is always governed by
 * the separately returned descriptor count, so stale entries are never read.
 */
static stpg_loop_desc_t *stpg_buffer_loop_desc_scratch(void) {
    return g_rate_stream_state_storage.loop_descs;
}

static inline uint32_t stpg_stream_compare_storage_index(uint32_t index) {
    return g_stream_compare_event_base + index;
}

static inline bool stpg_stream_compare_capacity_exhausted(uint32_t count) {
    return g_stream_compare_event_base > STPG_MAX_EVENT_COUNT
        || count >= STPG_MAX_EVENT_COUNT - g_stream_compare_event_base;
}

enum {
    STPG_MISMATCH_STAGE_RATE_PERIOD_ZERO = 1u,
    STPG_MISMATCH_STAGE_RATE_SLOT_EXCEEDS_PERIOD = 2u,
    STPG_MISMATCH_STAGE_RATE_SLOT_NON_MONOTONIC = 3u,
    STPG_MISMATCH_STAGE_BATCH_WAIT_GENERIC = 10u,
    STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN = 11u,
    STPG_MISMATCH_STAGE_BATCH_WAIT_COMPARE24_FIRST = 12u,
    STPG_MISMATCH_STAGE_BATCH_WAIT_COMPARE24_GROUP = 13u,
    STPG_MISMATCH_STAGE_REDUCED_PREV_PERIOD = 14u,
    STPG_MISMATCH_STAGE_LEGACY_SLOT_MERGE = 15u,
};

enum {
    STPG_MISMATCH_DETAIL_COMPARE24_PRACTICAL_TWO_GROUP = 1u << 0,
    STPG_MISMATCH_DETAIL_COMPARE24_EFFECTIVE_SAMPLE_TRIGGER = 1u << 1,
    STPG_MISMATCH_DETAIL_COMPARE24_HAVE_PREV_TIME = 1u << 2,
};

static void stpg_stream_record_rate_mismatch(uint32_t stage,
                                             uint32_t rate_id,
                                             uint32_t index,
                                             uint32_t desired_delta_ticks,
                                             uint32_t required_ticks,
                                             uint32_t period_ticks,
                                             uint32_t prev_time_ticks,
                                             uint32_t time_ticks) {
    g_stream_telemetry.mismatch_stage = stage;
    g_stream_telemetry.mismatch_rate_id = rate_id;
    g_stream_telemetry.mismatch_index = index;
    g_stream_telemetry.mismatch_desired_delta_ticks = desired_delta_ticks;
    g_stream_telemetry.mismatch_required_ticks = required_ticks;
    g_stream_telemetry.mismatch_period_ticks = period_ticks;
    g_stream_telemetry.mismatch_prev_time_ticks = prev_time_ticks;
    g_stream_telemetry.mismatch_time_ticks = time_ticks;
    g_stream_telemetry.mismatch_detail_flags = 0u;
    g_stream_telemetry.mismatch_group_count = 0u;
    g_stream_telemetry.mismatch_gap_from_cycle_start_ticks = 0u;
    g_stream_telemetry.mismatch_gap_from_prev_ticks = 0u;
}

static void stpg_stream_record_compare24_mismatch_detail(uint32_t flags,
                                                         uint32_t group_count,
                                                         uint32_t gap_from_cycle_start_ticks,
                                                         uint32_t gap_from_prev_ticks) {
    g_stream_telemetry.mismatch_detail_flags = flags;
    g_stream_telemetry.mismatch_group_count = group_count;
    g_stream_telemetry.mismatch_gap_from_cycle_start_ticks = gap_from_cycle_start_ticks;
    g_stream_telemetry.mismatch_gap_from_prev_ticks = gap_from_prev_ticks;
}

static void STPG_STREAM_PRODUCER_FUNC(stpg_stream_record_vector_observation)(
    const stpg_rate_stream_state_t *state,
    const VectorRecord *vector,
    uint32_t vector_index,
    uint32_t repeat_index,
    const stpg_rate_runtime_t *runtime) {
    if (state == 0 || vector == 0 || runtime == 0) {
        return;
    }

    const bool have_previous = g_stream_telemetry.vector_observation_count != 0u;
    const bool rate_transition = have_previous
        && g_stream_telemetry.last_vector_rate_id != vector->rate_id;
    const bool repeated_vector = vector->repeat > 1u;
    /* Do not write telemetry for ordinary vectors: this candidate is intended
     * to preserve the refill timing as much as possible. */
    if (have_previous && !rate_transition && !repeated_vector) {
        return;
    }
    if (rate_transition) {
        g_stream_telemetry.rate_transition_count += 1u;
        g_stream_telemetry.last_rate_transition_vector_index = vector_index;
        g_stream_telemetry.last_rate_transition_from_rate_id = g_stream_telemetry.last_vector_rate_id;
        g_stream_telemetry.last_rate_transition_to_rate_id = vector->rate_id;
        g_stream_telemetry.last_rate_transition_from_period_ticks =
            g_stream_telemetry.last_vector_period_ticks;
        g_stream_telemetry.last_rate_transition_to_period_ticks = runtime->rate.period_ticks;
        g_stream_telemetry.last_rate_transition_repeat_index = repeat_index;
        g_stream_telemetry.last_rate_transition_prev_time_ticks = state->prev_time_ticks;
        g_stream_telemetry.last_rate_transition_desired_delta_ticks =
            state->have_prev_time ? state->prev_period_ticks : 0u;
        g_stream_telemetry.last_rate_transition_output_changed = 0u;
    }
    if (repeated_vector) {
        g_stream_telemetry.repeat_gt1_count += 1u;
    }
    g_stream_telemetry.vector_observation_count += 1u;
    g_stream_telemetry.last_vector_index = vector_index;
    g_stream_telemetry.last_vector_rate_id = vector->rate_id;
    g_stream_telemetry.last_vector_repeat_index = repeat_index;
    g_stream_telemetry.last_vector_repeat_count = vector->repeat;
    g_stream_telemetry.last_vector_period_ticks = runtime->rate.period_ticks;
    g_stream_telemetry.last_vector_prev_period_ticks = state->prev_period_ticks;
    g_stream_telemetry.last_vector_desired_delta_ticks =
        state->have_prev_time ? state->prev_period_ticks : 0u;
}

#if STPG_STREAM_HOT_TELEMETRY
static void stpg_stream_record_vector_emission(uint32_t vector_index,
                                               uint32_t repeat_index,
                                               uint32_t desired_delta_ticks,
                                               bool output_changed) {
    const bool same_observed_vector = g_stream_telemetry.last_vector_index == vector_index
        && g_stream_telemetry.last_vector_repeat_index == repeat_index;
    const bool same_transition = g_stream_telemetry.rate_transition_count != 0u
        && g_stream_telemetry.last_rate_transition_vector_index == vector_index
        && g_stream_telemetry.last_rate_transition_repeat_index == repeat_index;
    if (same_observed_vector) {
        g_stream_telemetry.last_vector_desired_delta_ticks = desired_delta_ticks;
        g_stream_telemetry.last_vector_output_changed = output_changed ? 1u : 0u;
    }
    if (same_transition) {
        g_stream_telemetry.last_rate_transition_output_changed = output_changed ? 1u : 0u;
    }
}
#else
#define stpg_stream_record_vector_emission(...) ((void)0)
#endif

static inline void stpg_stream_telemetry_reset(void) {
    memset(&g_stream_telemetry, 0, sizeof(g_stream_telemetry));
    g_stream_telemetry.precomputed_uop_cache_capacity =
        STPG_STREAM_PRECOMPUTED_UOP_CAPACITY;
    memset(&g_stream_telemetry_snapshot, 0, sizeof(g_stream_telemetry_snapshot));
    g_stream_telemetry_snapshot.precomputed_uop_cache_capacity =
        STPG_STREAM_PRECOMPUTED_UOP_CAPACITY;
    g_stream_telemetry_snapshot_valid = false;
}

static inline void stpg_stream_vector_cache_reset(void) {
    g_stream_vector_cache_base_index = 0u;
    g_stream_vector_cache_count = 0u;
    g_stream_vector_cache_next_base_index = 0u;
    g_stream_vector_cache_next_count = 0u;
    g_stream_loop_body_cache_id = UINT32_MAX;
    g_stream_loop_body_cache_base_index = 0u;
    g_stream_loop_body_cache_count = 0u;
}

static inline void stpg_stream_loop_body_cache_reset(void) {
    g_stream_loop_body_cache_id = UINT32_MAX;
    g_stream_loop_body_cache_base_index = 0u;
    g_stream_loop_body_cache_count = 0u;
}

static inline bool stpg_stream_vector_cache_current_contains(uint32_t vector_index) {
    return vector_index >= g_stream_vector_cache_base_index
        && vector_index < (g_stream_vector_cache_base_index + g_stream_vector_cache_count);
}

static inline bool stpg_stream_vector_cache_next_contains(uint32_t vector_index) {
    return vector_index >= g_stream_vector_cache_next_base_index
        && vector_index < (g_stream_vector_cache_next_base_index + g_stream_vector_cache_next_count);
}

static inline void stpg_stream_vector_cache_promote_next(void) {
    memcpy(g_stream_vector_cache, g_stream_vector_cache_next, sizeof(g_stream_vector_cache));
    g_stream_vector_cache_base_index = g_stream_vector_cache_next_base_index;
    g_stream_vector_cache_count = g_stream_vector_cache_next_count;
    g_stream_vector_cache_next_base_index = 0u;
    g_stream_vector_cache_next_count = 0u;
}

static void stpg_stream_record_prefetch_failure(
    stpg_stream_prefetch_diag_stage_t stage,
    uint32_t base_index,
    uint32_t record_count,
    uint32_t attempt_number,
    uint32_t record_index,
    uint32_t record_offset,
    stpg_err_t err,
    const VectorRecord *record) {
    g_stream_telemetry.prefetch_diag_attempt_base_index = base_index;
    g_stream_telemetry.prefetch_diag_attempt_record_count = record_count;
    g_stream_telemetry.prefetch_diag_attempt_number = attempt_number;
    g_stream_telemetry.prefetch_diag_failure_stage = (uint32_t)stage;
    g_stream_telemetry.prefetch_diag_failure_record_index = record_index;
    g_stream_telemetry.prefetch_diag_failure_record_offset = record_offset;
    g_stream_telemetry.prefetch_diag_failure_error = (uint32_t)err;
    g_stream_telemetry.prefetch_diag_failure_attempt_number = attempt_number;
    if (record != 0) {
        memcpy(g_stream_telemetry.prefetch_diag_failure_words,
               record,
               sizeof(g_stream_telemetry.prefetch_diag_failure_words));
    } else {
        memset(g_stream_telemetry.prefetch_diag_failure_words,
               0,
               sizeof(g_stream_telemetry.prefetch_diag_failure_words));
    }
}

#if STPG_QMI_DIAGNOSTIC_CAPTURE
#define STPG_QMI_SNAPSHOT_FUNC __no_inline_not_in_flash_func
#else
#define STPG_QMI_SNAPSHOT_FUNC
#endif
static void STPG_QMI_SNAPSHOT_FUNC(stpg_stream_capture_qmi_snapshot)(bool before) {
#if STPG_QMI_DIAGNOSTIC_CAPTURE
    /* Keep this diagnostic read sequence independent of the hot-telemetry
     * switch.  The practical profile deliberately undefines PICO_ON_DEVICE
     * below to remove timing-only blocks; using that macro here silently
     * removed the QMI MMIO reads from F276 and left all fields at zero. */
    const uint32_t direct_csr = qmi_hw->direct_csr;
    const uint32_t timing = qmi_hw->m[1].timing;
    const uint32_t rfmt = qmi_hw->m[1].rfmt;
    const uint32_t rcmd = qmi_hw->m[1].rcmd;
    const uint32_t wfmt = qmi_hw->m[1].wfmt;
    const uint32_t wcmd = qmi_hw->m[1].wcmd;
    __dsb();
    __compiler_memory_barrier();
    if (before) {
        g_stream_telemetry.prefetch_diag_qmi_before_direct_csr = direct_csr;
        g_stream_telemetry.prefetch_diag_qmi_before_timing = timing;
        g_stream_telemetry.prefetch_diag_qmi_before_rfmt = rfmt;
        g_stream_telemetry.prefetch_diag_qmi_before_rcmd = rcmd;
        g_stream_telemetry.prefetch_diag_qmi_before_wfmt = wfmt;
        g_stream_telemetry.prefetch_diag_qmi_before_wcmd = wcmd;
    } else {
        g_stream_telemetry.prefetch_diag_qmi_after_direct_csr = direct_csr;
        g_stream_telemetry.prefetch_diag_qmi_after_timing = timing;
        g_stream_telemetry.prefetch_diag_qmi_after_rfmt = rfmt;
        g_stream_telemetry.prefetch_diag_qmi_after_rcmd = rcmd;
        g_stream_telemetry.prefetch_diag_qmi_after_wfmt = wfmt;
        g_stream_telemetry.prefetch_diag_qmi_after_wcmd = wcmd;
    }
#else
    (void)before;
#endif
}
#undef STPG_QMI_SNAPSHOT_FUNC

static void stpg_stream_capture_words(uint32_t destination[5], const void *source) {
    if (source != 0) {
        memcpy(destination, source, sizeof(uint32_t) * 5u);
    } else {
        memset(destination, 0, sizeof(uint32_t) * 5u);
    }
}

static void stpg_stream_capture_mapped_source(const RateVectorHeader *header,
                                              uint32_t vector_index) {
    if (header == 0) {
        return;
    }
    const uint32_t offset = header->vector_offset +
        (vector_index * STPG_VECTOR_RECORD_SIZE);
    const void *source = stpg_psram_mapped_ptr(
        STPG_PSRAM_PATTERN_BASE + offset,
        STPG_VECTOR_RECORD_SIZE);
    g_stream_telemetry.prefetch_diag_mapped_source_address =
        source == 0 ? 0xffffffffu : (uint32_t)(uintptr_t)source;
    stpg_stream_capture_words(g_stream_telemetry.prefetch_diag_mapped_source_words, source);
}

#define STPG_STREAM_VECTOR_CACHE_PREFETCH_WATERMARK_RECORDS 512u

static inline void stpg_macro_prepare_wait_cache_entry(uint16_t desired_delta_ticks,
                                                       bool sample_trigger,
                                                       uint16_t *out_wait_word,
                                                       uint16_t *out_required_ticks,
                                                       uint8_t *out_valid) {
    const uint16_t overhead_ticks = (uint16_t)(sample_trigger ? 11u : 10u);
    if (desired_delta_ticks == 0u) {
        *out_wait_word = 0u;
        *out_required_ticks = 0u;
        *out_valid = 1u;
        return;
    }
    *out_required_ticks = overhead_ticks;
    if (desired_delta_ticks < overhead_ticks) {
        *out_wait_word = 0u;
        *out_valid = 0u;
        return;
    }
    *out_wait_word = (uint16_t)(desired_delta_ticks - overhead_ticks);
    *out_valid = 1u;
}

static inline void stpg_macro_fill_compare_proto(const stpg_rate_stream_state_t *state,
                                                 ExecEvent *out_compare_proto) {
    memset(out_compare_proto, 0, sizeof(*out_compare_proto));
    out_compare_proto->flags = state->macro_sample_mask_cached != 0u ? STPG_EVF_COMPARE_POINT : 0u;
    out_compare_proto->sample_mask = state->macro_sample_mask_cached;
    out_compare_proto->expect_value = state->macro_expect_value_cached;
}

static stpg_err_t stpg_buffer_stream_emit_next_macro_event_family12(stpg_rate_stream_state_t *state,
                                                                    uint32_t *dst_words,
                                                                    bool *out_sample_trigger,
                                                                    bool *out_emitted) {
#ifdef PICO_ON_DEVICE
    const uint32_t start_us = time_us_32();
#endif
    const MacroVectorRecord *record = &state->current_macro_record;
    const uint32_t phase_index = state->macro_event_phase;
    if (phase_index > 1u) {
        return ERR_BAD_STATE;
    }

    uint32_t wait_word;
    uint32_t required_ticks;
    uint32_t desired_delta_ticks;
    bool wait_valid;
    if (!state->have_prev_time) {
        desired_delta_ticks = state->macro_phase_initial_desired_delta[phase_index];
        required_ticks = state->macro_phase_initial_required_ticks[phase_index];
        wait_word = state->macro_phase_initial_wait_word[phase_index];
        wait_valid = state->macro_phase_initial_wait_valid[phase_index] != 0u;
    } else if (state->first_in_cycle) {
        desired_delta_ticks = state->macro_phase_cycle_desired_delta[phase_index];
        required_ticks = state->macro_phase_cycle_required_ticks[phase_index];
        wait_word = state->macro_phase_cycle_wait_word[phase_index];
        wait_valid = state->macro_phase_cycle_wait_valid[phase_index] != 0u;
    } else {
        desired_delta_ticks = state->macro_phase_intra_desired_delta[phase_index];
        required_ticks = state->macro_phase_intra_required_ticks[phase_index];
        wait_word = state->macro_phase_intra_wait_word[phase_index];
        wait_valid = state->macro_phase_intra_wait_valid[phase_index] != 0u;
    }
    if (!wait_valid) {
        stpg_stream_record_rate_mismatch(
            STPG_MISMATCH_STAGE_LEGACY_SLOT_MERGE,
            0u,
            state->macro_record_index,
            desired_delta_ticks,
            required_ticks,
            record->period_ticks,
            state->prev_time_ticks,
            state->macro_phase_time_ticks[phase_index]);
        return ERR_RATE_PERIOD_MISMATCH;
    }

    const bool sample_trigger = state->macro_phase_sample_trigger[phase_index] != 0u;
    dst_words[0] = wait_word;
    dst_words[1] = sample_trigger ? 1u : 0u;
    dst_words[2] = state->macro_phase_packed_value[phase_index];
    dst_words[3] = state->macro_phase_packed_enable[phase_index];

    const uint32_t event_index = g_stream_emitted_event_count;
    if (sample_trigger) {
        if (g_stream_compare_event_count >= STPG_MAX_EVENT_COUNT) {
            return ERR_BAD_SIZE;
        }
        const uint32_t compare_index = g_stream_compare_event_count;
        g_pio_words[compare_index] = event_index;
        g_expanded_events[compare_index] = state->macro_compare_proto;
        g_stream_compare_event_count = compare_index + 1u;
    }
    g_stream_emitted_event_count = event_index + 1u;

    state->have_prev_time = true;
    state->prev_time_ticks = state->macro_phase_time_ticks[phase_index];
    state->prev_period_ticks = record->period_ticks;
    state->first_in_cycle = false;

    if (phase_index == 0u) {
        state->macro_event_phase = 1u;
    } else {
        state->macro_event_phase = 0u;
        state->current_value = state->macro_drive_value_cached;
        state->current_dir = state->macro_drive_enable_cached;
        state->vector_index += 1u;
        state->macro_repeat_index += 1u;
        state->first_in_cycle = true;
        if (state->macro_repeat_index >= record->repeat) {
            state->macro_record_index += 1u;
            state->macro_repeat_index = 0u;
            state->macro_record_loaded = false;
            if (state->macro_record_index >= state->macro_header.record_count) {
                state->eof = true;
            }
        }
    }

#if STPG_STREAM_HOT_TELEMETRY
    g_stream_telemetry.emit_call_count += 1u;
    if (sample_trigger) {
        g_stream_telemetry.sample_event_count += 1u;
    }
#endif
#ifdef PICO_ON_DEVICE
    g_stream_telemetry.emit_total_us += time_us_32() - start_us;
#endif
    if (out_sample_trigger != 0) *out_sample_trigger = sample_trigger;
    if (out_emitted != 0) *out_emitted = true;
    return ERR_NONE;
}

static void stpg_stream_record_chunk_identity(uint32_t global_vector_start,
                                              uint32_t global_vector_end,
                                              uint32_t emitted_event_start,
                                              uint32_t emitted_event_end,
                                              uint32_t group_emit_count) {
    g_stream_telemetry.last_chunk_vector_start = global_vector_start;
    g_stream_telemetry.last_chunk_vector_end = global_vector_end;
    g_stream_telemetry.last_chunk_emitted_event_start = emitted_event_start;
    g_stream_telemetry.last_chunk_emitted_event_end = emitted_event_end;
    g_stream_telemetry.last_chunk_processed_vectors =
        global_vector_end >= global_vector_start ? (global_vector_end - global_vector_start) : 0u;
    g_stream_telemetry.last_chunk_group_emit_count = group_emit_count;
}

static stpg_stream_hotspot_meta_t g_stream_hotspot_meta;
static stpg_storage_t g_upload_target;
static stpg_pattern_source_t g_active_source;
static PsramPatternSlot g_psram_slot;
static uint32_t g_flash_selected_run_slot;
static bool g_flash_selected_run_slot_valid;
static FlashSlotMeta g_flash_selected_run_meta;
/* Only FLASH finalization/selection on Core 0 owns this pending record. */
static FlashSlotMeta g_flash_pending_meta;
static uint32_t g_flash_selected_write_slot;
static bool g_flash_selected_write_slot_valid;
static bool g_flash_selected_write_slot_erased;
static stpg_rate_info_t g_rate_info;

#define STPG_STREAM_HOTSPOT_MAX_RANKS 4u

typedef struct {
    uint32_t load_vector_total_us;
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
} stpg_stream_hotspot_snapshot_t;

static stpg_stream_hotspot_entry_t g_stream_hotspot_chunks[STPG_STREAM_HOTSPOT_MAX_RANKS];

static bool stpg_stream_hotspot_entry_valid(const stpg_stream_hotspot_entry_t *entry) {
    if (entry == 0) {
        return false;
    }
    return entry->processed_vectors != 0u
        || entry->load_vector_total_us != 0u
        || entry->group_work_total_us != 0u
        || entry->group_emit_count != 0u;
}

static uint32_t stpg_stream_hotspot_valid_rank_count(void) {
    uint32_t count = 0u;
    for (uint32_t rank = 0u; rank < STPG_STREAM_HOTSPOT_MAX_RANKS; ++rank) {
        if (!stpg_stream_hotspot_entry_valid(&g_stream_hotspot_chunks[rank])) {
            break;
        }
        count += 1u;
    }
    return count;
}

static void stpg_buffer_stream_state_init(stpg_rate_stream_state_t *state, const RateVectorHeader *header);
static stpg_err_t stpg_buffer_prepare_rate_runtime_cache(const RateVectorHeader *header,
                                                         stpg_rate_runtime_t *runtime,
                                                         uint32_t runtime_count);
static bool stpg_buffer_batch_path_eligible(const RateVectorHeader *header,
                                            uint32_t loop_block_count);
static bool stpg_buffer_reduced_density_compare_eligible(const RateVectorHeader *header,
                                                         const stpg_rate_runtime_t *runtime,
                                                         uint32_t loop_block_count);
#if STPG_STREAM_COMPARE_SAMPLE_REPAIR
static stpg_err_t stpg_buffer_count_reduced_compare_samples(const RateVectorHeader *header,
                                                            const stpg_rate_runtime_t *runtime,
                                                            uint32_t *out_count);
#endif
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP
static stpg_err_t stpg_buffer_count_rate_vector_samples_psram(
    const RateVectorHeader *header,
    const stpg_rate_runtime_t *runtime,
    const stpg_loop_desc_t *loop_descs,
    uint32_t loop_block_count,
    uint32_t *out_count);
#endif
static stpg_err_t stpg_buffer_parse_loop_descriptors_psram(const RateVectorHeader *header,
                                                           stpg_loop_desc_t *out_descs,
                                                           uint32_t *out_count);
static stpg_err_t stpg_buffer_validate_rate_vector_psram(uint32_t total_size, uint32_t total_crc,
                                                         RateVectorHeader *out_header);
static stpg_err_t stpg_buffer_expand_rate_vector_psram(const RateVectorHeader *header);
static stpg_err_t stpg_buffer_estimate_expanded_vectors(const RateVectorHeader *header,
                                                        const stpg_loop_desc_t *loop_descs,
                                                        uint32_t loop_block_count,
                                                        uint32_t *out_estimate);
static stpg_err_t stpg_buffer_estimate_emitted_events(const RateVectorHeader *header,
                                                      const stpg_loop_desc_t *loop_descs,
                                                      uint32_t loop_block_count,
                                                      uint32_t *out_estimate);
static stpg_err_t stpg_buffer_check_full_expansion_capacity(const RateVectorHeader *header,
                                                            const stpg_loop_desc_t *loop_descs,
                                                            uint32_t loop_block_count);
static stpg_err_t stpg_buffer_validate_loop_compare_compatibility(const RateVectorHeader *header,
                                                                  const stpg_loop_desc_t *loop_descs,
                                                                  uint32_t loop_block_count);
static stpg_err_t stpg_buffer_rate_vector_has_samples_psram(const RateVectorHeader *header, bool *out_has_samples);
static stpg_err_t stpg_buffer_stream_prefetch_vectors(const RateVectorHeader *header, uint32_t vector_index);
static stpg_err_t stpg_buffer_stream_prefetch_vectors_limited(const RateVectorHeader *header,
                                                              uint32_t vector_index,
                                                              uint32_t max_records);
static uint32_t stpg_buffer_stream_estimate_batch_prefetch_records(const stpg_rate_stream_state_t *state,
                                                                   uint32_t remaining_events);
static stpg_err_t stpg_batch_compute_wait_word(uint32_t desired_delta_ticks,
                                               bool sample_trigger,
                                               uint32_t *out_wait_word,
                                               uint32_t *out_required_ticks);
static uint32_t stpg_apply_group_uop_drive(uint32_t current_value, const stpg_rate_group_op_t *op,
                                           const VectorRecord *vector);
static inline uint32_t stpg_apply_group_uop_drive_packed(uint32_t current_value,
                                                         const stpg_rate_group_op_t *op,
                                                         uint32_t packed_vector_value);
static stpg_err_t stpg_buffer_stream_fill_next_chunk_batch(stpg_rate_stream_state_t *state, uint32_t *dst_words,
                                                           uint32_t max_events, stpg_stream_chunk_result_t *out_result);
static uint32_t stpg_stream_compute_first_group_delta_ticks(
    const stpg_rate_stream_state_t *state,
    const stpg_rate_group_uop_t *group_uop,
    uint32_t repeat_index,
    uint32_t repeat_count);
static inline uint32_t stpg_stream_pack_logical_to_window_fast(uint32_t logical_bits,
                                                                uint32_t active_pins);
static stpg_err_t stpg_buffer_build_rate_group_uop(
    const stpg_rate_runtime_t *runtime,
    uint32_t group_index,
    uint32_t active_pins,
    stpg_rate_group_uop_t *out_uop);

/* Group metadata is retained for every admitted rate.  Common group UOPs are
 * materialized in the bounded streaming cache during prepare; callers that
 * cannot use that cache retain a local fallback UOP. */
static stpg_err_t stpg_buffer_build_rate_group_uop(
    const stpg_rate_runtime_t *runtime,
    uint32_t group_index,
    uint32_t active_pins,
    stpg_rate_group_uop_t *out_uop) {
    if (runtime == 0 || out_uop == 0 || group_index >= runtime->group_count
        || active_pins == 0u) {
        return ERR_BAD_SIZE;
    }

    const stpg_rate_slot_group_t *group = &runtime->groups[group_index];
    if (group->slot_count == 0u
        || (uint32_t)group->first_slot_index + group->slot_count > runtime->rate.slot_count) {
        return ERR_RATEVEC_BAD_HEADER;
    }

    const uint32_t pin_mask = active_pins == STPG_ACTIVE_PINS_V11
        ? 0x00FFFFFFu : 0x0000FFFFu;
    memset(out_uop, 0, sizeof(*out_uop));
    out_uop->time_ticks = group->time_ticks;
    out_uop->gap_from_prev_ticks = group->gap_from_prev_ticks;
    out_uop->gap_from_cycle_start_ticks = group->gap_from_cycle_start_ticks;
    out_uop->sample_mask_or = group->sample_mask_or & pin_mask;

    for (uint32_t offset = 0u; offset < group->slot_count; ++offset) {
        const stpg_rate_slot_runtime_t *slot =
            &runtime->slots[group->first_slot_index + offset];
        if ((slot->flags & STPG_RATE_SLOT_SAMPLE) != 0u) {
            out_uop->has_sample = 1u;
            out_uop->sample_word = 1u;
            out_uop->group_flags |= STPG_GROUP_FLAG_HAS_SAMPLE;
        }
        if ((slot->flags & STPG_RATE_SLOT_DIR_UPDATE) != 0u) {
            out_uop->dir_update_mask |= slot->dir_mask & pin_mask;
            out_uop->group_flags |= STPG_GROUP_FLAG_HAS_DIR_UPDATE;
        }
        const uint16_t drive_op = slot->flags & STPG_RATE_SLOT_DRIVE_OP_MASK;
        if (drive_op != STPG_DRIVE_NONE) {
            if (out_uop->op_count >= STPG_MAX_SLOTS_PER_RATE) {
                return ERR_RATE_TOO_COMPLEX;
            }
            stpg_rate_group_op_t *op = &out_uop->ops[out_uop->op_count++];
            op->drive_mask = slot->drive_mask & pin_mask;
            op->packed_drive_mask = stpg_stream_pack_logical_to_window_fast(
                op->drive_mask, active_pins);
            op->drive_op = (uint8_t)drive_op;
        }
    }

    if (out_uop->sample_mask_or != 0u) {
        out_uop->group_flags |= STPG_GROUP_FLAG_COMPARE_CANDIDATE;
    }
    out_uop->packed_dir_update_mask = stpg_stream_pack_logical_to_window_fast(
        out_uop->dir_update_mask, active_pins);
#if STPG_STREAM_RATE_TEMPLATE_FILL && !STPG_STREAM_HOT_TELEMETRY && !STPG_STREAM_FAIL_STOP
    out_uop->drive_keep_mask = UINT32_MAX;
    for (uint32_t i = 0u; i < out_uop->op_count; ++i) {
        const stpg_rate_group_op_t *op = &out_uop->ops[i];
        const uint32_t mask = op->packed_drive_mask;
        out_uop->drive_keep_mask &= ~mask;
        out_uop->drive_vector_mask &= ~mask;
        out_uop->drive_one_mask &= ~mask;
        out_uop->drive_complement_mask &= ~mask;
        switch (op->drive_op) {
        case STPG_DRIVE_VECTOR: out_uop->drive_vector_mask |= mask; break;
        case STPG_DRIVE_ONE: out_uop->drive_one_mask |= mask; break;
        case STPG_DRIVE_COMPLEMENT: out_uop->drive_complement_mask |= mask; break;
        default: break; /* ZERO; NONE is not stored in ops[]. */
        }
    }
    const uint32_t stable_delta = group_index == 0u
        ? out_uop->gap_from_cycle_start_ticks : out_uop->gap_from_prev_ticks;
    /* An invalid stable wait disables the new path, not admission.  The old
     * emitter must still reject at the original offending event. */
    out_uop->stable_wait_valid = stpg_batch_compute_wait_word(
        stable_delta, out_uop->has_sample != 0u,
        &out_uop->stable_wait_word, 0) == ERR_NONE;
#endif
    return ERR_NONE;
}

/* Share identical UOPs before Core 1 starts meeting refill deadlines.  Once
 * the bounded cache is full, keep its existing entries and leave only new
 * shapes uncached.  Later groups can still share an already cached shape.
 * The builder zero-fills the whole object, including padding, so byte-wise
 * equality covers timing, pin packing, sample/dir flags, and ordered ops.
 * Use the existing current-group scratch rather than growing SRAM/stack. */
static stpg_err_t stpg_buffer_prepare_precomputed_rate_group_uops(
    stpg_rate_stream_state_t *state) {
    if (state == 0 || state->header.rate_count == 0u
        || state->header.rate_count > STPG_MAX_RATES) {
        return ERR_BAD_SIZE;
    }

    memset(state->precomputed_uop_index, 0xFF, sizeof(state->precomputed_uop_index));
    state->precomputed_uop_count = 0u;
    state->precomputed_uops_complete = false;
    g_stream_telemetry.precomputed_uop_cache_capacity =
        STPG_STREAM_PRECOMPUTED_UOP_CAPACITY;
    g_stream_telemetry.precomputed_uop_cache_entry_count = 0u;

    bool all_groups_cached = true;
    for (uint32_t rate_id = 0u; rate_id < state->header.rate_count; ++rate_id) {
        const stpg_rate_runtime_t *runtime = &state->rate_runtime[rate_id];
        if (runtime->group_count > STPG_MAX_SLOTS_PER_RATE) {
            return ERR_RATEVEC_BAD_HEADER;
        }
        for (uint32_t group_index = 0u; group_index < runtime->group_count; ++group_index) {
            stpg_err_t err = stpg_buffer_build_rate_group_uop(
                runtime,
                group_index,
                state->header.active_pins,
                &state->current_group_uop);
            if (err != ERR_NONE) {
                return err;
            }
            uint32_t cache_index = 0u;
            while (cache_index < state->precomputed_uop_count
                && memcmp(&state->precomputed_group_uops[cache_index],
                          &state->current_group_uop, sizeof(state->current_group_uop)) != 0) {
                cache_index += 1u;
            }
            if (cache_index == state->precomputed_uop_count) {
                if (cache_index >= STPG_STREAM_PRECOMPUTED_UOP_CAPACITY) {
                    all_groups_cached = false;
                    continue;
                }
                memcpy(&state->precomputed_group_uops[cache_index],
                       &state->current_group_uop, sizeof(state->current_group_uop));
                state->precomputed_uop_count = (uint8_t)(cache_index + 1u);
            }
            state->precomputed_uop_index[rate_id][group_index] = (uint8_t)cache_index;
        }
    }
    state->precomputed_uops_complete = all_groups_cached;
    g_stream_telemetry.precomputed_uop_cache_entry_count = state->precomputed_uop_count;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_resolve_rate_group_uop(
    stpg_rate_stream_state_t *state,
    const stpg_rate_runtime_t *runtime,
    uint32_t rate_id,
    uint32_t group_index,
    uint32_t active_pins,
    stpg_rate_group_uop_t *fallback_uop,
    const stpg_rate_group_uop_t **out_uop) {
    if (state == 0 || runtime == 0 || fallback_uop == 0 || out_uop == 0) {
        return ERR_BAD_SIZE;
    }
    /* A partial cache is useful too: completeness is diagnostic only. */
    if (rate_id < STPG_MAX_RATES
        && group_index < STPG_MAX_SLOTS_PER_RATE) {
        const uint8_t cache_index = state->precomputed_uop_index[rate_id][group_index];
        if (cache_index != 0xFFu && cache_index < state->precomputed_uop_count) {
            g_stream_telemetry.precomputed_uop_cache_hit_count += 1u;
            *out_uop = &state->precomputed_group_uops[cache_index];
            return ERR_NONE;
        }
    }

    g_stream_telemetry.precomputed_uop_cache_fallback_count += 1u;
    stpg_err_t err = stpg_buffer_build_rate_group_uop(
        runtime, group_index, active_pins, fallback_uop);
    if (err == ERR_NONE) {
        g_stream_telemetry.precomputed_uop_cache_build_count += 1u;
        *out_uop = fallback_uop;
    }
    return err;
}
/* Telemetry instrumentation and semantic fast-path eligibility are separate
 * concerns. HOT_TELEMETRY may add accounting to the hot loop, but it must not
 * compile out a proven-safe repeat path. Every non-eligible shape still falls
 * through to the generic emitter below. */
#if STPG_STREAM_REPEAT_BLOCK_FILL || STPG_STREAM_REPEAT_BOUNDARY_FILL
static bool stpg_buffer_stream_repeat_block_shape_eligible(
    const VectorRecord *vector,
    const stpg_rate_runtime_t *runtime,
    uint32_t logical_mask,
    bool pack_identity,
    bool compare_collecting) {
    if (vector == 0 || runtime == 0 || pack_identity || compare_collecting
        || runtime->group_count != 2u || vector->flags != 0u
        || vector->repeat <= 1u || vector->expect_value != 0u
        || vector->expect_mask != 0u) {
        return false;
    }

    stpg_rate_group_uop_t first_uop;
    stpg_rate_group_uop_t second_uop;
    const uint32_t active_pins = logical_mask == 0x00FFFFFFu
        ? STPG_ACTIVE_PINS_V11 : STPG_ACTIVE_PINS_V01;
    if (stpg_buffer_build_rate_group_uop(runtime, 0u, active_pins, &first_uop) != ERR_NONE
        || stpg_buffer_build_rate_group_uop(runtime, 1u, active_pins, &second_uop) != ERR_NONE) {
        return false;
    }
    const stpg_rate_group_uop_t *first = &first_uop;
    const stpg_rate_group_uop_t *second = &second_uop;
    if (first->time_ticks != 0u || second->time_ticks == 0u
        || first->has_sample != 0u || second->has_sample != 0u
        || first->sample_mask_or != 0u || second->sample_mask_or != 0u
        || first->op_count != 1u || second->op_count != 1u
        || first->dir_update_mask != logical_mask || second->dir_update_mask != 0u
        || second->gap_from_prev_ticks != second->time_ticks) {
        return false;
    }
    return first->ops[0].drive_op == STPG_DRIVE_VECTOR
        && second->ops[0].drive_op == STPG_DRIVE_VECTOR
        && first->ops[0].drive_mask == logical_mask
        && second->ops[0].drive_mask == logical_mask;
}

static bool stpg_buffer_stream_repeat_block_eligible(
    const stpg_rate_stream_state_t *state,
    const VectorRecord *vector,
    const stpg_rate_runtime_t *runtime,
    uint32_t logical_mask,
    bool pack_identity,
    bool compare_collecting) {
    if (state == 0 || !stpg_buffer_stream_repeat_block_shape_eligible(
            vector, runtime, logical_mask, pack_identity, compare_collecting)) {
        return false;
    }
    /* The diagnostic path is deliberately restricted to a repeated, already
     * established vector.  The first repeat of every vector retains the
     * generic path because its cycle-start delta can depend on the preceding
     * vector. */
    if (state->repeat_index == 0u || state->current_group_index != 0u
        || !state->have_prev_time || vector->repeat <= state->repeat_index) {
        return false;
    }
    stpg_rate_group_uop_t second_uop;
    const uint32_t active_pins = logical_mask == 0x00FFFFFFu
        ? STPG_ACTIVE_PINS_V11 : STPG_ACTIVE_PINS_V01;
    if (stpg_buffer_build_rate_group_uop(runtime, 1u, active_pins, &second_uop) != ERR_NONE) {
        return false;
    }
    if (state->prev_time_ticks != second_uop.time_ticks
        || state->prev_period_ticks != runtime->rate.period_ticks) {
        return false;
    }
    return true;
}

static stpg_err_t STPG_STREAM_PRODUCER_FUNC(stpg_buffer_stream_emit_repeat_block)(
    stpg_rate_stream_state_t *state,
    const VectorRecord *vector,
    uint32_t vector_index,
    const stpg_rate_runtime_t *runtime,
    uint32_t *dst_words,
    uint32_t max_events,
    uint32_t logical_mask,
    uint32_t *io_emitted_event_count,
    uint32_t *io_emitted_count,
    bool *out_completed_vector) {
    if (state == 0 || vector == 0 || runtime == 0 || dst_words == 0
        || io_emitted_event_count == 0 || io_emitted_count == 0
        || out_completed_vector == 0) {
        return ERR_BAD_SIZE;
    }
    *out_completed_vector = false;
    const uint32_t remaining_events = max_events - *io_emitted_count;
    const uint32_t repeat_capacity = remaining_events / runtime->group_count;
    const uint32_t remaining_repeats = (uint32_t)vector->repeat - state->repeat_index;
    const uint32_t repeats_to_emit = remaining_repeats < repeat_capacity
        ? remaining_repeats : repeat_capacity;
    if (repeats_to_emit == 0u) {
        return ERR_NONE;
    }

    stpg_rate_group_uop_t first_uop;
    stpg_rate_group_uop_t second_uop;
    const uint32_t active_pins = logical_mask == 0x00FFFFFFu
        ? STPG_ACTIVE_PINS_V11 : STPG_ACTIVE_PINS_V01;
    stpg_err_t uop_err = stpg_buffer_build_rate_group_uop(runtime, 0u, active_pins, &first_uop);
    if (uop_err != ERR_NONE) return uop_err;
    uop_err = stpg_buffer_build_rate_group_uop(runtime, 1u, active_pins, &second_uop);
    if (uop_err != ERR_NONE) return uop_err;
    const stpg_rate_group_uop_t *first = &first_uop;
    const stpg_rate_group_uop_t *second = &second_uop;
    uint32_t wait0 = 0u;
    uint32_t wait1 = 0u;
    uint32_t required_ticks = 0u;
    const uint32_t desired0 = stpg_stream_compute_first_group_delta_ticks(
        state, first, state->repeat_index, vector->repeat);
    stpg_err_t err = stpg_batch_compute_wait_word(desired0, false, &wait0, &required_ticks);
    if (err != ERR_NONE) {
        stpg_stream_record_rate_mismatch(
            STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
            vector->rate_id,
            0u,
            desired0,
            required_ticks,
            runtime->rate.period_ticks,
            state->prev_time_ticks,
            first->time_ticks);
        return err;
    }
    err = stpg_batch_compute_wait_word(
        second->gap_from_prev_ticks, false, &wait1, &required_ticks);
    if (err != ERR_NONE) {
        stpg_stream_record_rate_mismatch(
            STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
            vector->rate_id,
            1u,
            second->gap_from_prev_ticks,
            required_ticks,
            runtime->rate.period_ticks,
            first->time_ticks,
            second->time_ticks);
        return err;
    }

    const uint32_t packed_value = stpg_stream_pack_logical_to_window_fast(
        vector->drive_value, state->header.active_pins) & logical_mask;
    const uint32_t packed_enable = stpg_stream_pack_logical_to_window_fast(
        vector->drive_enable, state->header.active_pins) & logical_mask;
    const uint32_t template_words[8] = {
        wait0, 0u, packed_value, packed_enable,
        wait1, 0u, packed_value, packed_enable,
    };
    uint32_t *dst = &dst_words[*io_emitted_count * 4u];
    for (uint32_t repeat_index = 0u; repeat_index < repeats_to_emit; ++repeat_index) {
        dst[0] = template_words[0];
        dst[1] = template_words[1];
        dst[2] = template_words[2];
        dst[3] = template_words[3];
        dst[4] = template_words[4];
        dst[5] = template_words[5];
        dst[6] = template_words[6];
        dst[7] = template_words[7];
        dst += 8u;
    }

    /* The eligibility checks establish that both full-mask DRIVE_VECTOR
     * operations leave the logical state at the vector values. */
    state->current_value = vector->drive_value & logical_mask;
    state->current_dir = vector->drive_enable & logical_mask;
    state->have_prev_time = true;
    state->prev_time_ticks = (uint16_t)second->time_ticks;
    state->prev_period_ticks = runtime->rate.period_ticks;
    state->repeat_index += repeats_to_emit;
    state->current_group_index = 0u;

    const uint32_t emitted = repeats_to_emit * runtime->group_count;
    *io_emitted_count += emitted;
    *io_emitted_event_count += emitted;
    if (state->repeat_index >= vector->repeat) {
        state->repeat_index = 0u;
        state->vector_index = vector_index + 1u;
        state->vector_loaded = false;
        *out_completed_vector = true;
        g_stream_telemetry.fast_vector_count += 1u;
        if (state->vector_index >= state->header.vector_count) {
            state->eof = true;
        }
    }
    return ERR_NONE;
}
#endif

static stpg_err_t stpg_buffer_stream_emit_batch_vector_span(stpg_rate_stream_state_t *state,
                                                            uint32_t *dst_words,
                                                            uint32_t max_events,
                                                            uint32_t logical_mask,
                                                            bool pack_identity,
                                                            bool compare_collecting,
                                                            uint32_t *io_compare_event_count,
                                                            uint32_t *io_emitted_event_count,
                                                            uint32_t *io_sample_event_count,
                                                            uint32_t *io_emitted_count);
static stpg_err_t stpg_buffer_stream_emit_batch_vector_span_compare24(stpg_rate_stream_state_t *state,
                                                                      uint32_t *dst_words,
                                                                      uint32_t max_events,
                                                                      uint32_t logical_mask,
                                                                      uint32_t *io_compare_event_count,
                                                                      uint32_t *io_emitted_event_count,
                                                                      uint32_t *io_sample_event_count,
                                                                      uint32_t *io_emitted_count);

#if STPG_STREAM_REPEAT_BLOCK_FILL
static bool stpg_buffer_stream_repeat_block_eligible(
    const stpg_rate_stream_state_t *state,
    const VectorRecord *vector,
    const stpg_rate_runtime_t *runtime,
    uint32_t logical_mask,
    bool pack_identity,
    bool compare_collecting);
static stpg_err_t STPG_STREAM_PRODUCER_FUNC(stpg_buffer_stream_emit_repeat_block)(
    stpg_rate_stream_state_t *state,
    const VectorRecord *vector,
    uint32_t vector_index,
    const stpg_rate_runtime_t *runtime,
    uint32_t *dst_words,
    uint32_t max_events,
    uint32_t logical_mask,
    uint32_t *io_emitted_event_count,
    uint32_t *io_emitted_count,
    bool *out_completed_vector);
#endif

/*
 * Batch events use a wait-then-drive PIO program.  At a vector boundary the
 * first group's timestamp is relative to the new vector, while the previous
 * event has already completed at prev_time_ticks in the previous period.
 * Therefore the boundary wait is the remainder of the previous period plus
 * the new vector's first-group timestamp.  The compiled cycle-start gap is
 * the host-side cycle-origin distance and is not the elapsed wait from the
 * previous emitted event.
 */
static uint32_t stpg_stream_compute_first_group_delta_ticks(
    const stpg_rate_stream_state_t *state,
    const stpg_rate_group_uop_t *group_uop,
    uint32_t repeat_index,
    uint32_t repeat_count
) {
    (void)repeat_index;
    (void)repeat_count;
    if (state == 0 || group_uop == 0 || !state->have_prev_time) {
        return group_uop != 0 ? group_uop->time_ticks : 0u;
    }
    /* A repeat is a second cycle of the same vector, not a longer period.
     * The boundary delta is therefore always the completion remainder of the
     * previous cycle plus the new group's timestamp.  The repeat metadata is
     * carried separately in telemetry so an observable GPIO transition can be
     * distinguished from an internal same-output repeat without changing the
     * PIO schedule. */
    return (uint32_t)state->prev_period_ticks
        - (uint32_t)state->prev_time_ticks
        + (uint32_t)group_uop->time_ticks;
}

#if STPG_STREAM_DENSE_VECTOR_FILL && !STPG_STREAM_HOT_TELEMETRY
static bool stpg_buffer_stream_dense_vector_shape_eligible(
    const VectorRecord *vector,
    const stpg_rate_runtime_t *runtime,
    uint32_t logical_mask,
    bool pack_identity,
    bool compare_collecting) {
    if (vector == 0 || runtime == 0 || pack_identity || compare_collecting
        || runtime->group_count != 2u || vector->flags != 0u
        || vector->repeat != 1u || vector->expect_value != 0u
        || vector->expect_mask != 0u) {
        return false;
    }

    stpg_rate_group_uop_t first_uop;
    stpg_rate_group_uop_t second_uop;
    const uint32_t active_pins = logical_mask == 0x00FFFFFFu
        ? STPG_ACTIVE_PINS_V11 : STPG_ACTIVE_PINS_V01;
    if (stpg_buffer_build_rate_group_uop(runtime, 0u, active_pins, &first_uop) != ERR_NONE
        || stpg_buffer_build_rate_group_uop(runtime, 1u, active_pins, &second_uop) != ERR_NONE) {
        return false;
    }
    const stpg_rate_group_uop_t *first = &first_uop;
    const stpg_rate_group_uop_t *second = &second_uop;
    if (first->time_ticks != 0u || second->time_ticks == 0u
        || first->has_sample != 0u || second->has_sample != 0u
        || first->sample_mask_or != 0u || second->sample_mask_or != 0u
        || first->op_count != 1u || second->op_count != 1u
        || first->dir_update_mask != logical_mask || second->dir_update_mask != 0u
        || second->gap_from_prev_ticks != second->time_ticks) {
        return false;
    }
    return first->ops[0].drive_op == STPG_DRIVE_VECTOR
        && second->ops[0].drive_op == STPG_DRIVE_VECTOR
        && first->ops[0].drive_mask == logical_mask
        && second->ops[0].drive_mask == logical_mask;
}

static stpg_err_t STPG_STREAM_PRODUCER_FUNC(stpg_buffer_stream_emit_dense_vector)(
    stpg_rate_stream_state_t *state,
    const VectorRecord *vector,
    uint32_t vector_index,
    const stpg_rate_runtime_t *runtime,
    uint32_t *dst_words,
    uint32_t max_events,
    uint32_t logical_mask,
    bool pack_identity,
    bool compare_collecting,
    uint32_t packed_vector_value,
    uint32_t packed_vector_enable,
    uint32_t *io_emitted_event_count,
    uint32_t *io_emitted_count,
    bool *out_emitted_vector) {
    if (state == 0 || vector == 0 || runtime == 0 || dst_words == 0
        || io_emitted_event_count == 0 || io_emitted_count == 0
        || out_emitted_vector == 0) {
        return ERR_BAD_SIZE;
    }
    *out_emitted_vector = false;
    if (!stpg_buffer_stream_dense_vector_shape_eligible(
            vector, runtime, logical_mask, pack_identity, compare_collecting)
        || max_events - *io_emitted_count < 2u) {
        return ERR_NONE;
    }

    stpg_rate_group_uop_t first_uop;
    stpg_rate_group_uop_t second_uop;
    const uint32_t active_pins = logical_mask == 0x00FFFFFFu
        ? STPG_ACTIVE_PINS_V11 : STPG_ACTIVE_PINS_V01;
    stpg_err_t uop_err = stpg_buffer_build_rate_group_uop(runtime, 0u, active_pins, &first_uop);
    if (uop_err != ERR_NONE) return uop_err;
    uop_err = stpg_buffer_build_rate_group_uop(runtime, 1u, active_pins, &second_uop);
    if (uop_err != ERR_NONE) return uop_err;
    const stpg_rate_group_uop_t *first = &first_uop;
    const stpg_rate_group_uop_t *second = &second_uop;
    uint32_t wait0 = 0u;
    uint32_t wait1 = 0u;
    uint32_t required_ticks = 0u;
    const uint32_t desired0 = stpg_stream_compute_first_group_delta_ticks(
        state, first, 0u, 1u);
    stpg_err_t err = stpg_batch_compute_wait_word(desired0, false, &wait0, &required_ticks);
    if (err != ERR_NONE) {
        stpg_stream_record_rate_mismatch(
            STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
            vector->rate_id,
            0u,
            desired0,
            required_ticks,
            runtime->rate.period_ticks,
            state->prev_time_ticks,
            first->time_ticks);
        return err;
    }
    err = stpg_batch_compute_wait_word(
        second->gap_from_prev_ticks, false, &wait1, &required_ticks);
    if (err != ERR_NONE) {
        stpg_stream_record_rate_mismatch(
            STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
            vector->rate_id,
            1u,
            second->gap_from_prev_ticks,
            required_ticks,
            runtime->rate.period_ticks,
            first->time_ticks,
            second->time_ticks);
        return err;
    }

    uint32_t *dst = &dst_words[*io_emitted_count * 4u];
    dst[0] = wait0;
    dst[1] = 0u;
    dst[2] = packed_vector_value;
    dst[3] = packed_vector_enable;
    dst[4] = wait1;
    dst[5] = 0u;
    dst[6] = packed_vector_value;
    dst[7] = packed_vector_enable;

    /* Both full-mask DRIVE_VECTOR operations make the post-vector state equal
     * to the vector values.  The second group's timestamp is the last emitted
     * time used by the next vector boundary. */
    state->current_value = vector->drive_value & logical_mask;
    state->current_dir = vector->drive_enable & logical_mask;
    state->have_prev_time = true;
    state->prev_time_ticks = second->time_ticks;
    state->prev_period_ticks = runtime->rate.period_ticks;
    state->repeat_index = 0u;
    state->current_group_index = 0u;
    state->vector_index = vector_index + 1u;
    state->vector_loaded = false;
    if (state->vector_index >= state->header.vector_count) {
        state->eof = true;
    }

    *io_emitted_count += 2u;
    *io_emitted_event_count += 2u;
    g_stream_telemetry.fast_vector_count += 1u;
    *out_emitted_vector = true;
    return ERR_NONE;
}
#endif

#if STPG_STREAM_HOT_TELEMETRY
static void stpg_stream_record_repeat_aware_boundary(
    const stpg_rate_stream_state_t *state,
    uint32_t vector_index,
    uint32_t rate_id,
    uint32_t repeat_index,
    uint32_t repeat_count,
    const stpg_rate_group_uop_t *group_uop,
    uint32_t desired_delta_ticks,
    bool output_changed
) {
    if (state == 0 || group_uop == 0) {
        return;
    }
    g_stream_telemetry.boundary_observation_count += 1u;
    g_stream_telemetry.last_boundary_kind = repeat_index == 0u ? 1u : 2u;
    g_stream_telemetry.last_boundary_vector_index = vector_index;
    g_stream_telemetry.last_boundary_rate_id = rate_id;
    g_stream_telemetry.last_boundary_repeat_index = repeat_index;
    g_stream_telemetry.last_boundary_repeat_count = repeat_count;
    g_stream_telemetry.last_boundary_period_ticks = state->rate_runtime[rate_id].rate.period_ticks;
    g_stream_telemetry.last_boundary_prev_period_ticks = state->prev_period_ticks;
    g_stream_telemetry.last_boundary_prev_time_ticks = state->prev_time_ticks;
    g_stream_telemetry.last_boundary_group_time_ticks = group_uop->time_ticks;
    g_stream_telemetry.last_boundary_desired_delta_ticks = desired_delta_ticks;
    g_stream_telemetry.last_boundary_output_changed = output_changed ? 1u : 0u;
}
#else
#define stpg_stream_record_repeat_aware_boundary(...) ((void)0)
#endif
static stpg_err_t stpg_buffer_stream_emit_reduced_payload(stpg_rate_stream_state_t *state,
                                                          const VectorRecord *vector,
                                                          const stpg_rate_runtime_t *runtime,
                                                          uint32_t *dst_words,
                                                          uint32_t *dst_sample_waits,
                                                          uint32_t emitted_slot,
                                                          uint32_t logical_mask,
                                                          bool pack_identity,
                                                          uint32_t *io_sample_event_count,
                                                          uint32_t *io_compare_event_count,
                                                          uint32_t *io_emitted_event_count);
static stpg_err_t stpg_buffer_stream_emit_next_reduced_compare_fallback(stpg_rate_stream_state_t *state,
                                                                        uint32_t *dst_words,
                                                                        uint32_t *dst_sample_waits,
                                                                        uint32_t emitted_slot,
                                                                        uint32_t logical_mask,
                                                                        bool pack_identity,
                                                                        uint32_t *io_sample_event_count,
                                                                        uint32_t *io_compare_event_count,
                                                                        uint32_t *io_emitted_event_count);
static stpg_err_t stpg_buffer_stream_fill_next_chunk_reduced_compare_impl(stpg_rate_stream_state_t *state,
                                                                          uint32_t *dst_words,
                                                                          uint32_t *dst_sample_waits,
                                                                          uint32_t max_events,
                                                                          stpg_stream_chunk_result_t *out_result);
static stpg_err_t stpg_buffer_finalize_flash(uint32_t total_size, uint32_t total_crc);
static stpg_err_t stpg_buffer_prepare_flash_for_run(void);
static stpg_err_t stpg_buffer_expand_macro_vector_buffer(const uint8_t *buf, uint32_t size);
static stpg_err_t stpg_buffer_expand_macro_p2_vector_buffer(const uint8_t *buf, uint32_t size);
static stpg_err_t stpg_buffer_prepare_macro_streaming(stpg_rate_stream_state_t *out_state);
static stpg_err_t stpg_buffer_stream_fill_next_chunk_macro(stpg_rate_stream_state_t *state,
                                                           uint32_t *dst_words,
                                                           uint32_t max_events,
                                                           stpg_stream_chunk_result_t *out_result);
static stpg_err_t stpg_buffer_stream_fill_next_chunk_macro_fast_repeat_span(stpg_rate_stream_state_t *state,
                                                                            uint32_t *dst_words,
                                                                            uint32_t max_events,
                                                                            stpg_stream_chunk_result_t *out_result,
                                                                            bool *out_used_fast_path);
static stpg_err_t stpg_buffer_stream_emit_next_macro_event(stpg_rate_stream_state_t *state,
                                                           uint32_t *dst_words,
                                                           bool *out_sample_trigger,
                                                           bool *out_emitted);
static stpg_err_t stpg_buffer_read_macro_record_psram(const MacroVectorHeader *header,
                                                      uint32_t record_index,
                                                      MacroVectorRecord *out_record);

static uint32_t stpg_macro_load_u24(const uint8_t bytes[3]) {
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8u) | ((uint32_t)bytes[2] << 16u);
}

static void stpg_macro_build_event(ExecEvent *event,
                                   uint32_t *dst_words,
                                   uint16_t wait_ticks,
                                   uint16_t flags,
                                   uint32_t out_value,
                                   uint32_t out_enable,
                                   uint32_t sample_mask,
                                   uint32_t expect_value,
                                   uint32_t active_pins) {
    if (event == 0) {
        return;
    }
    event->wait_ticks = wait_ticks;
    event->flags = flags;
    event->out_value = out_value;
    event->out_enable = out_enable;
    event->sample_mask = sample_mask;
    event->expect_value = expect_value;

    if (dst_words != 0) {
        dst_words[0] = (uint32_t)wait_ticks;
        dst_words[1] = (flags & STPG_EVF_SAMPLE_POINT) != 0u ? 1u : 0u;
        dst_words[2] = stpg_board_pack_logical_to_window(out_value, active_pins);
        dst_words[3] = stpg_board_pack_logical_to_window(out_enable, active_pins);
    }
}

static void stpg_macro_write_event(uint32_t event_index,
                                   uint16_t wait_ticks,
                                   uint16_t flags,
                                   uint32_t out_value,
                                   uint32_t out_enable,
                                   uint32_t sample_mask,
                                   uint32_t expect_value,
                                   uint32_t active_pins) {
    stpg_macro_build_event(&g_expanded_events[event_index],
                           &g_pio_words[event_index * 4u],
                           wait_ticks,
                           flags,
                           out_value,
                           out_enable,
                           sample_mask,
                           expect_value,
                           active_pins);
}

static inline void stpg_macro_append_stream_compare_event(uint32_t event_index,
                                                          uint32_t sample_mask,
                                                          uint32_t expect_value) {
    const uint32_t compare_index = stpg_stream_compare_storage_index(g_stream_compare_event_count);
    g_pio_words[compare_index] = event_index;
    ExecEvent *compare_event = &g_expanded_events[compare_index];
    compare_event->flags = sample_mask != 0u ? STPG_EVF_COMPARE_POINT : 0u;
    compare_event->sample_mask = sample_mask;
    compare_event->expect_value = expect_value;
    g_stream_compare_event_count += 1u;
}

enum {
    STPG_MEASURE_FAMILY_M1_DRIVE_THEN_SAMPLE = 1u,
    STPG_MEASURE_FAMILY_M2_SAMPLE_THEN_DRIVE = 2u,
    STPG_MEASURE_FAMILY_M3_DRIVE_ONLY = 3u,
    STPG_MEASURE_FAMILY_M4_SAMPLE_ONLY = 4u,
    STPG_MEASURE_FAMILY_M5_SIMULTANEOUS_SAMPLE_DRIVE = 5u,
};

enum {
    STPG_MEASURE_SEQUENCE_S1_ALT_M1_M2 = 1u,
    STPG_MEASURE_SEQUENCE_S2_ALT_M2_M1 = 2u,
    STPG_MEASURE_SEQUENCE_S3_M1_M3_M4 = 3u,
    STPG_MEASURE_SEQUENCE_S4_M5_M1 = 4u,
    STPG_MEASURE_SEQUENCE_S5_M2_M5 = 5u,
    STPG_MEASURE_SEQUENCE_S6_M5_M3_M4 = 6u,
};

#define STPG_MEASURE_SEQUENCE_DEBUG_BASE 0x100u
#define STPG_MEASURE_BURST_DEBUG_BASE 0x200u

enum {
    STPG_MEASURE_BURST_B1_MIXED_SAMPLE = 1u,
    STPG_MEASURE_BURST_B2_DRIVE_ONLY = 2u,
    STPG_MEASURE_BURST_B3_M5_HEAVY = 3u,
};

/* Shape eligibility alone is not a runtime contract. Reduced output waits
 * for the delayed-sample PIO on every event, including chained successors.
 * Without the complete schedule/handoff pipeline it can deadlock after slot
 * zero. PSRAM sample/replay also preserves sample-only points, whereas the
 * diagnostic reduced schedule marks only collected compares. Keep that
 * production profile on the existing four-word/sample-trigger path. */
#define STPG_ENABLE_REDUCED_COMPARE_RUNTIME \
    (STPG_STREAM_COMPARE_SAMPLE_REPAIR && STPG_STREAM_COMPARE_EVENT_SCHEDULE \
     && STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF \
     && STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF \
     && !STPG_STREAM_PSRAM_COMPARE_SAMPLE)
#define STPG_BATCH_WAIT_OVERHEAD_NO_SAMPLE 10u
#define STPG_BATCH_WAIT_OVERHEAD_SAMPLE 11u

static inline uint32_t stpg_stream_pack_logical_to_window_fast(uint32_t logical_bits, uint32_t active_pins) {
    if (active_pins != STPG_ACTIVE_PINS_V11) {
        return logical_bits & 0x0000FFFFu;
    }
    return (logical_bits & 0x0001FFFFu)
        | ((logical_bits & 0x001E0000u) << 1u)
        | ((logical_bits & 0x00E00000u) << 3u);
}

#if STPG_STREAM_RATE_TEMPLATE_FILL && !STPG_STREAM_HOT_TELEMETRY && !STPG_STREAM_FAIL_STOP
/* Keep one small SRAM helper instead of duplicating the inverse mapping at
 * both held-value/held-direction exits of the experimental producer. */
static uint32_t STPG_STREAM_PRODUCER_FUNC(stpg_stream_unpack_window_to_logical_fast)(
    uint32_t window_bits, uint32_t active_pins) {
    if (active_pins != STPG_ACTIVE_PINS_V11) {
        return window_bits & 0x0000FFFFu;
    }
    /* Inverse of the DUT mapping. Window bits 17, 22, 23 are board pins,
     * not DUT bits, and are never carried into the logical held state. */
    return (window_bits & 0x0001FFFFu)
        | ((window_bits & 0x003C0000u) >> 1u)
        | ((window_bits & 0x07000000u) >> 3u);
}
#endif

static uint32_t stpg_batch_wait_overhead_ticks(bool sample_trigger) {
    return sample_trigger ? STPG_BATCH_WAIT_OVERHEAD_SAMPLE : STPG_BATCH_WAIT_OVERHEAD_NO_SAMPLE;
}

static void stpg_measurement_write_event(uint32_t event_index,
                                         uint16_t wait_ticks,
                                         uint16_t flags,
                                         uint32_t out_value,
                                         uint32_t out_enable,
                                         uint32_t sample_mask,
                                         uint32_t expect_value,
                                         uint32_t active_pins) {
    ExecEvent *event = &g_expanded_events[event_index];
    event->wait_ticks = wait_ticks;
    event->flags = flags;
    event->out_value = out_value;
    event->out_enable = out_enable;
    event->sample_mask = sample_mask;
    event->expect_value = expect_value;

    g_pio_words[event_index * 4u + 0u] = (uint32_t)wait_ticks;
    g_pio_words[event_index * 4u + 1u] = (flags & STPG_EVF_SAMPLE_POINT) != 0u ? 1u : 0u;
    g_pio_words[event_index * 4u + 2u] =
        stpg_board_pack_logical_to_window(out_value, active_pins);
    g_pio_words[event_index * 4u + 3u] =
        stpg_board_pack_logical_to_window(out_enable, active_pins);
}

static stpg_err_t stpg_measurement_family_count(uint32_t family_id,
                                                uint32_t period_ticks,
                                                uint32_t phase_ticks,
                                                bool first_event,
                                                uint32_t *out_event_add,
                                                uint32_t *out_sample_add) {
    switch (family_id) {
        case STPG_MEASURE_FAMILY_M1_DRIVE_THEN_SAMPLE:
        case STPG_MEASURE_FAMILY_M2_SAMPLE_THEN_DRIVE:
            if (phase_ticks == 0u || phase_ticks >= period_ticks) {
                return ERR_BAD_SIZE;
            }
            *out_event_add = 2u;
            *out_sample_add = 1u;
            return ERR_NONE;
        case STPG_MEASURE_FAMILY_M3_DRIVE_ONLY:
            *out_event_add = 1u;
            *out_sample_add = 0u;
            return ERR_NONE;
        case STPG_MEASURE_FAMILY_M4_SAMPLE_ONLY:
            *out_event_add = first_event ? 2u : 1u;
            *out_sample_add = 1u;
            return ERR_NONE;
        case STPG_MEASURE_FAMILY_M5_SIMULTANEOUS_SAMPLE_DRIVE:
            *out_event_add = 1u;
            *out_sample_add = 1u;
            return ERR_NONE;
        default:
            return ERR_UNSUPPORTED;
    }
}

static stpg_err_t stpg_measurement_append_family(uint32_t family_id,
                                                 uint32_t period_ticks,
                                                 uint32_t phase_ticks,
                                                 uint32_t active_pins,
                                                 uint32_t logical_mask,
                                                 uint32_t probe_mask,
                                                 uint32_t sample_flags,
                                                 uint32_t *event_index,
                                                 uint32_t *output_value,
                                                 bool *first_event) {
    const uint16_t first_wait = (uint16_t)(*first_event ? 0u : period_ticks - phase_ticks);
    const uint16_t second_wait = (uint16_t)phase_ticks;
    const uint16_t single_wait = (uint16_t)(*first_event ? 0u : period_ticks);
    const uint32_t next_value = (*output_value ^ probe_mask) & logical_mask;

    switch (family_id) {
        case STPG_MEASURE_FAMILY_M1_DRIVE_THEN_SAMPLE:
            stpg_measurement_write_event((*event_index)++, first_wait, 0u, next_value, probe_mask, 0u, 0u, active_pins);
            stpg_measurement_write_event(
                (*event_index)++, second_wait, sample_flags, next_value, probe_mask, probe_mask, next_value, active_pins);
            *output_value = next_value;
            *first_event = false;
            return ERR_NONE;
        case STPG_MEASURE_FAMILY_M2_SAMPLE_THEN_DRIVE:
            stpg_measurement_write_event(
                (*event_index)++, first_wait, sample_flags, *output_value, probe_mask, probe_mask, *output_value, active_pins);
            stpg_measurement_write_event((*event_index)++, second_wait, 0u, next_value, probe_mask, 0u, 0u, active_pins);
            *output_value = next_value;
            *first_event = false;
            return ERR_NONE;
        case STPG_MEASURE_FAMILY_M3_DRIVE_ONLY:
            stpg_measurement_write_event((*event_index)++, single_wait, 0u, next_value, probe_mask, 0u, 0u, active_pins);
            *output_value = next_value;
            *first_event = false;
            return ERR_NONE;
        case STPG_MEASURE_FAMILY_M4_SAMPLE_ONLY:
            if (*first_event) {
                stpg_measurement_write_event((*event_index)++, 0u, 0u, 0u, probe_mask, 0u, 0u, active_pins);
            }
            stpg_measurement_write_event(
                (*event_index)++,
                single_wait,
                sample_flags,
                0u,
                probe_mask,
                probe_mask,
                *output_value,
                active_pins);
            *output_value = 0u;
            *first_event = false;
            return ERR_NONE;
        case STPG_MEASURE_FAMILY_M5_SIMULTANEOUS_SAMPLE_DRIVE:
            stpg_measurement_write_event(
                (*event_index)++,
                single_wait,
                sample_flags,
                next_value,
                probe_mask,
                probe_mask,
                *output_value,
                active_pins);
            *output_value = next_value;
            *first_event = false;
            return ERR_NONE;
        default:
            return ERR_UNSUPPORTED;
    }
}

static const uint8_t *stpg_measurement_sequence_steps(uint32_t sequence_id,
                                                      uint32_t *out_step_count) {
    static const uint8_t s1[] = {
        STPG_MEASURE_FAMILY_M1_DRIVE_THEN_SAMPLE,
        STPG_MEASURE_FAMILY_M2_SAMPLE_THEN_DRIVE,
    };
    static const uint8_t s2[] = {
        STPG_MEASURE_FAMILY_M2_SAMPLE_THEN_DRIVE,
        STPG_MEASURE_FAMILY_M1_DRIVE_THEN_SAMPLE,
    };
    static const uint8_t s3[] = {
        STPG_MEASURE_FAMILY_M1_DRIVE_THEN_SAMPLE,
        STPG_MEASURE_FAMILY_M3_DRIVE_ONLY,
        STPG_MEASURE_FAMILY_M4_SAMPLE_ONLY,
    };
    static const uint8_t s4[] = {
        STPG_MEASURE_FAMILY_M5_SIMULTANEOUS_SAMPLE_DRIVE,
        STPG_MEASURE_FAMILY_M1_DRIVE_THEN_SAMPLE,
    };
    static const uint8_t s5[] = {
        STPG_MEASURE_FAMILY_M2_SAMPLE_THEN_DRIVE,
        STPG_MEASURE_FAMILY_M5_SIMULTANEOUS_SAMPLE_DRIVE,
    };
    static const uint8_t s6[] = {
        STPG_MEASURE_FAMILY_M5_SIMULTANEOUS_SAMPLE_DRIVE,
        STPG_MEASURE_FAMILY_M3_DRIVE_ONLY,
        STPG_MEASURE_FAMILY_M4_SAMPLE_ONLY,
    };

    switch (sequence_id) {
        case STPG_MEASURE_SEQUENCE_S1_ALT_M1_M2:
            *out_step_count = (uint32_t)(sizeof(s1) / sizeof(s1[0]));
            return s1;
        case STPG_MEASURE_SEQUENCE_S2_ALT_M2_M1:
            *out_step_count = (uint32_t)(sizeof(s2) / sizeof(s2[0]));
            return s2;
        case STPG_MEASURE_SEQUENCE_S3_M1_M3_M4:
            *out_step_count = (uint32_t)(sizeof(s3) / sizeof(s3[0]));
            return s3;
        case STPG_MEASURE_SEQUENCE_S4_M5_M1:
            *out_step_count = (uint32_t)(sizeof(s4) / sizeof(s4[0]));
            return s4;
        case STPG_MEASURE_SEQUENCE_S5_M2_M5:
            *out_step_count = (uint32_t)(sizeof(s5) / sizeof(s5[0]));
            return s5;
        case STPG_MEASURE_SEQUENCE_S6_M5_M3_M4:
            *out_step_count = (uint32_t)(sizeof(s6) / sizeof(s6[0]));
            return s6;
        default:
            *out_step_count = 0u;
            return NULL;
    }
}

static const uint8_t *stpg_measurement_burst_steps(uint32_t burst_id,
                                                   uint32_t *out_step_count) {
    static const uint8_t b1[] = {
        STPG_MEASURE_FAMILY_M1_DRIVE_THEN_SAMPLE,
        STPG_MEASURE_FAMILY_M2_SAMPLE_THEN_DRIVE,
        STPG_MEASURE_FAMILY_M5_SIMULTANEOUS_SAMPLE_DRIVE,
        STPG_MEASURE_FAMILY_M3_DRIVE_ONLY,
        STPG_MEASURE_FAMILY_M4_SAMPLE_ONLY,
    };
    static const uint8_t b2[] = {
        STPG_MEASURE_FAMILY_M3_DRIVE_ONLY,
        STPG_MEASURE_FAMILY_M3_DRIVE_ONLY,
        STPG_MEASURE_FAMILY_M3_DRIVE_ONLY,
        STPG_MEASURE_FAMILY_M3_DRIVE_ONLY,
    };
    static const uint8_t b3[] = {
        STPG_MEASURE_FAMILY_M5_SIMULTANEOUS_SAMPLE_DRIVE,
        STPG_MEASURE_FAMILY_M3_DRIVE_ONLY,
        STPG_MEASURE_FAMILY_M5_SIMULTANEOUS_SAMPLE_DRIVE,
        STPG_MEASURE_FAMILY_M4_SAMPLE_ONLY,
    };

    switch (burst_id) {
        case STPG_MEASURE_BURST_B1_MIXED_SAMPLE:
            *out_step_count = (uint32_t)(sizeof(b1) / sizeof(b1[0]));
            return b1;
        case STPG_MEASURE_BURST_B2_DRIVE_ONLY:
            *out_step_count = (uint32_t)(sizeof(b2) / sizeof(b2[0]));
            return b2;
        case STPG_MEASURE_BURST_B3_M5_HEAVY:
            *out_step_count = (uint32_t)(sizeof(b3) / sizeof(b3[0]));
            return b3;
        default:
            *out_step_count = 0u;
            return NULL;
    }
}

static void stpg_stream_hotspot_reset(void) {
    memset(&g_stream_hotspot_meta, 0, sizeof(g_stream_hotspot_meta));
    memset(g_stream_hotspot_chunks, 0, sizeof(g_stream_hotspot_chunks));
}

static stpg_stream_hotspot_snapshot_t STPG_STREAM_PRODUCER_FUNC(stpg_stream_hotspot_snapshot)(void) {
    const uint32_t branch_control_residual =
        g_stream_telemetry.fast_group_bookkeeping_total_us >
            (g_stream_telemetry.fast_compare_write_total_us
             + g_stream_telemetry.fast_counter_update_total_us
             + g_stream_telemetry.fast_state_commit_total_us
             + g_stream_telemetry.fast_loop_bookkeeping_total_us
             + g_stream_telemetry.fast_store_select_total_us)
        ? (g_stream_telemetry.fast_group_bookkeeping_total_us
           - (g_stream_telemetry.fast_compare_write_total_us
              + g_stream_telemetry.fast_counter_update_total_us
              + g_stream_telemetry.fast_state_commit_total_us
              + g_stream_telemetry.fast_loop_bookkeeping_total_us
              + g_stream_telemetry.fast_store_select_total_us))
        : 0u;
    stpg_stream_hotspot_snapshot_t snapshot = {
        .load_vector_total_us = g_stream_telemetry.load_vector_total_us,
        .vector_prep_total_us = g_stream_telemetry.load_vector_copy_total_us
            + g_stream_telemetry.load_state_setup_total_us
            + g_stream_telemetry.load_cache_lookup_total_us
            + g_stream_telemetry.load_prefetch_wait_total_us,
        .group_work_total_us = g_stream_telemetry.fast_group_work_total_us,
        .compare_write_total_us = g_stream_telemetry.fast_compare_write_total_us,
        .store_select_total_us = g_stream_telemetry.fast_store_select_total_us,
        .branch_control_total_us = branch_control_residual,
        .group_emit_count = g_stream_telemetry.fast_group_emit_count,
        .practical_group_apply_total_us = g_stream_telemetry.practical_group_apply_total_us,
        .practical_wait_store_total_us = g_stream_telemetry.practical_wait_store_total_us,
        .practical_compare_write_total_us = g_stream_telemetry.practical_compare_write_total_us,
        .practical_commit_control_total_us = g_stream_telemetry.practical_commit_control_total_us,
    };
    return snapshot;
}

static void STPG_STREAM_PRODUCER_FUNC(stpg_stream_hotspot_record)(uint32_t global_vector_start,
                                       uint32_t global_vector_end,
                                       uint32_t emitted_event_start,
                                       uint32_t emitted_event_end,
                                       const stpg_stream_hotspot_snapshot_t *before,
                                       const stpg_stream_hotspot_snapshot_t *after) {
    if (after == 0 || before == 0) {
        return;
    }
    stpg_stream_hotspot_entry_t entry = {
        .global_vector_start = global_vector_start,
        .global_vector_end = global_vector_end,
        .emitted_event_start = emitted_event_start,
        .emitted_event_end = emitted_event_end,
        .load_vector_total_us = after->load_vector_total_us - before->load_vector_total_us,
        .processed_vectors = global_vector_end >= global_vector_start ? (global_vector_end - global_vector_start) : 0u,
        .vector_prep_total_us = after->vector_prep_total_us - before->vector_prep_total_us,
        .group_work_total_us = after->group_work_total_us - before->group_work_total_us,
        .compare_write_total_us = after->compare_write_total_us - before->compare_write_total_us,
        .store_select_total_us = after->store_select_total_us - before->store_select_total_us,
        .branch_control_total_us = after->branch_control_total_us - before->branch_control_total_us,
        .group_emit_count = after->group_emit_count - before->group_emit_count,
        .practical_group_apply_total_us =
            after->practical_group_apply_total_us - before->practical_group_apply_total_us,
        .practical_wait_store_total_us =
            after->practical_wait_store_total_us - before->practical_wait_store_total_us,
        .practical_compare_write_total_us =
            after->practical_compare_write_total_us - before->practical_compare_write_total_us,
        .practical_commit_control_total_us =
            after->practical_commit_control_total_us - before->practical_commit_control_total_us,
    };

    stpg_stream_record_chunk_identity(global_vector_start,
                                      global_vector_end,
                                      emitted_event_start,
                                      emitted_event_end,
                                      entry.group_emit_count);

    g_stream_hotspot_meta.current_run_chunk_count += 1u;
    if (entry.load_vector_total_us == 0u && entry.group_work_total_us == 0u && entry.group_emit_count == 0u) {
        return;
    }
    if (g_stream_hotspot_meta.recorded_chunk_count < STPG_STREAM_HOTSPOT_MAX_RANKS) {
        g_stream_hotspot_chunks[g_stream_hotspot_meta.recorded_chunk_count] = entry;
        g_stream_hotspot_meta.recorded_chunk_count += 1u;
    } else if (entry.load_vector_total_us > g_stream_hotspot_chunks[g_stream_hotspot_meta.recorded_chunk_count - 1u].load_vector_total_us) {
        g_stream_hotspot_chunks[g_stream_hotspot_meta.recorded_chunk_count - 1u] = entry;
    } else {
        return;
    }

    for (uint32_t index = g_stream_hotspot_meta.recorded_chunk_count - 1u; index > 0u; --index) {
        if (g_stream_hotspot_chunks[index].load_vector_total_us > g_stream_hotspot_chunks[index - 1u].load_vector_total_us) {
            const stpg_stream_hotspot_entry_t tmp = g_stream_hotspot_chunks[index - 1u];
            g_stream_hotspot_chunks[index - 1u] = g_stream_hotspot_chunks[index];
            g_stream_hotspot_chunks[index] = tmp;
        }
    }
    g_stream_hotspot_meta.hottest_chunk_rank_count = g_stream_hotspot_meta.recorded_chunk_count;
}

static inline void stpg_stream_write_compare_event_fast(uint32_t compare_index,
                                                        uint32_t event_index,
                                                        uint32_t sample_mask_or,
                                                        uint32_t vector_effective_expect_mask,
                                                        uint32_t vector_expect_value_masked) {
    const uint32_t effective_mask = sample_mask_or & vector_effective_expect_mask;
    compare_index = stpg_stream_compare_storage_index(compare_index);
    g_pio_words[compare_index] = event_index;
    ExecEvent *compare_event = &g_expanded_events[compare_index];
    compare_event->flags =
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP
        STPG_EVF_SAMPLE_POINT |
#endif
        (effective_mask != 0u ? STPG_EVF_COMPARE_POINT : 0u);
    compare_event->sample_mask = effective_mask;
    compare_event->expect_value = vector_expect_value_masked;
}

static uint32_t stpg_buffer_stream_estimate_batch_prefetch_records(const stpg_rate_stream_state_t *state,
                                                                   uint32_t remaining_events) {
    if (state == 0 || remaining_events == 0u) {
        return 1u;
    }
    uint32_t min_groups_per_vector = 0u;
    for (uint32_t rate_index = 0u; rate_index < state->header.rate_count; ++rate_index) {
        const uint32_t group_count = state->rate_runtime[rate_index].group_count;
        if (group_count == 0u) {
            continue;
        }
        if (min_groups_per_vector == 0u || group_count < min_groups_per_vector) {
            min_groups_per_vector = group_count;
        }
    }
    if (min_groups_per_vector == 0u) {
        min_groups_per_vector = 1u;
    }
    uint32_t records = (remaining_events + min_groups_per_vector - 1u) / min_groups_per_vector;
    if (records == 0u) {
        records = 1u;
    }
    if (records > STPG_STREAM_VECTOR_CACHE_READ_RECORDS) {
        records = STPG_STREAM_VECTOR_CACHE_READ_RECORDS;
    }
    return records;
}

static stpg_err_t stpg_batch_compute_wait_word(uint32_t desired_delta_ticks,
                                               bool sample_trigger,
                                               uint32_t *out_wait_word,
                                               uint32_t *out_required_ticks) {
    if (out_wait_word == 0) {
        return ERR_BAD_SIZE;
    }
    if (desired_delta_ticks == 0u) {
        *out_wait_word = 0u;
        if (out_required_ticks != 0) {
            *out_required_ticks = 0u;
        }
        return ERR_NONE;
    }
    const uint32_t overhead_ticks = stpg_batch_wait_overhead_ticks(sample_trigger);
    if (out_required_ticks != 0) {
        *out_required_ticks = overhead_ticks;
    }
    if (desired_delta_ticks < overhead_ticks) {
        return ERR_RATE_PERIOD_MISMATCH;
    }
    *out_wait_word = desired_delta_ticks - overhead_ticks;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_stage_flash_payload_to_psram(const FlashSlotMeta *meta) {
    if (meta == 0) return ERR_BAD_SIZE;
    stpg_psram_init();
    if (!stpg_psram_is_available()) return ERR_PSRAM_UNAVAILABLE;
    if (!g_flash_selected_run_slot_valid) return ERR_BAD_STATE;
    if (meta->pattern_size > STPG_PSRAM_PATTERN_BYTES) return ERR_BAD_SIZE;

    uint8_t scratch[512];
    for (uint32_t cursor = 0u; cursor < meta->pattern_size; ) {
        const uint32_t chunk = (meta->pattern_size - cursor) > sizeof(scratch)
            ? (uint32_t)sizeof(scratch)
            : (meta->pattern_size - cursor);
        stpg_err_t err = stpg_flash_read_slot_payload(g_flash_selected_run_slot, cursor, scratch, chunk);
        if (err != ERR_NONE) return err;
        err = stpg_psram_write(STPG_PSRAM_PATTERN_BASE + cursor, scratch, chunk);
        if (err != ERR_NONE) return err;
        cursor += chunk;
    }

    uint32_t staged_crc = 0u;
    stpg_err_t err = stpg_psram_checksum(STPG_PSRAM_PATTERN_BASE, meta->pattern_size, &staged_crc);
    if (err != ERR_NONE) return err;
    return staged_crc == meta->pattern_crc32 ? ERR_NONE : ERR_BAD_CRC;
}

static stpg_err_t stpg_buffer_prepare_flash_raw_for_run(const FlashSlotMeta *meta) {
    if (meta == 0) return ERR_BAD_SIZE;
    if (!g_flash_selected_run_slot_valid) return ERR_BAD_STATE;
    if (meta->pattern_size > STPG_MAX_PATTERN_BYTES) return ERR_BAD_SIZE;

    stpg_err_t err = stpg_flash_read_slot_payload(g_flash_selected_run_slot, 0u, g_pattern_buffer, meta->pattern_size);
    if (err != ERR_NONE) return err;
    err = stpg_validate_pattern(g_pattern_buffer, meta->pattern_size, meta->pattern_crc32);
    if (err != ERR_NONE) return err;
    if (*(const uint32_t *)(const void *)g_pattern_buffer == STPG_MACROVEC_MAGIC) {
        return stpg_buffer_expand_macro_vector_buffer(g_pattern_buffer, meta->pattern_size);
    }
    if (*(const uint32_t *)(const void *)g_pattern_buffer == STPG_MACROP2VEC_MAGIC) {
        return stpg_buffer_expand_macro_p2_vector_buffer(g_pattern_buffer, meta->pattern_size);
    }

    uint32_t pio_word_count = 0u;
    uint32_t sample_event_count = 0u;
    err = stpg_expand_raw_events_to_pio_words(g_pattern_buffer, meta->pattern_size,
                                              g_pio_words, STPG_MAX_EVENT_COUNT * 4u,
                                              &pio_word_count, &sample_event_count);
    if (err != ERR_NONE) return err;

    const PatternHeader *h = (const PatternHeader *)(const void *)g_pattern_buffer;
    g_pattern_size = meta->pattern_size;
    g_event_count = h->event_count;
    g_sample_event_count = sample_event_count;
    g_pio_word_count = pio_word_count;
    g_use_expanded_event_view = false;
    memset(&g_rate_info, 0, sizeof(g_rate_info));
    g_active_pins = STPG_ACTIVE_PINS_V01;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_prepare_flash_rate_vector_for_run(const FlashSlotMeta *meta) {
    if (meta == 0) return ERR_BAD_SIZE;
    stpg_err_t err = stpg_buffer_stage_flash_payload_to_psram(meta);
    if (err != ERR_NONE) return err;
    g_stream_compare_collecting = false;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    g_stream_expected_sample_count = 0u;
    g_stream_has_samples = false;
    stpg_stream_telemetry_reset();
    stpg_stream_vector_cache_reset();

    RateVectorHeader header;
    stpg_loop_desc_t *const loop_descs = stpg_buffer_loop_desc_scratch();
    uint32_t loop_block_count = 0u;
    err = stpg_buffer_validate_rate_vector_psram(meta->pattern_size, meta->pattern_crc32, &header);
    if (err != ERR_NONE) return err;
    err = stpg_buffer_parse_loop_descriptors_psram(&header, loop_descs, &loop_block_count);
    if (err != ERR_NONE) return err;
    err = stpg_buffer_validate_loop_compare_compatibility(&header, loop_descs, loop_block_count);
    if (err != ERR_NONE) return err;
    err = stpg_buffer_check_full_expansion_capacity(&header, loop_descs, loop_block_count);
    if (err != ERR_NONE) return err;

    uint32_t expanded_estimate = 0u;
    err = stpg_buffer_estimate_expanded_vectors(&header, loop_descs, loop_block_count, &expanded_estimate);
    if (err != ERR_NONE) return err;

    g_rate_info.vector_count = header.vector_count;
    g_rate_info.rate_count = header.rate_count;
    g_rate_info.slot_count = header.slot_count;
    g_rate_info.loop_block_count = loop_block_count;
    g_rate_info.expanded_vector_count_estimate = expanded_estimate;
    g_rate_info.active_pins = header.active_pins;
    g_rate_info.loaded = true;
    g_active_pins = header.active_pins;

    return stpg_buffer_expand_rate_vector_psram(&header);
}

static stpg_err_t stpg_buffer_prepare_flash_for_run(void) {
    if (!g_flash_selected_run_slot_valid) return ERR_NO_PATTERN;
    if (g_flash_selected_run_meta.format == STPG_PATTERN_FORMAT_RAW_EVENT) {
        return stpg_buffer_prepare_flash_raw_for_run(&g_flash_selected_run_meta);
    }
    if (g_flash_selected_run_meta.format == STPG_PATTERN_FORMAT_MACRO_STREAM) {
        return stpg_buffer_prepare_flash_raw_for_run(&g_flash_selected_run_meta);
    }
    if (g_flash_selected_run_meta.format == STPG_PATTERN_FORMAT_MACRO_P2) {
        return stpg_buffer_prepare_flash_raw_for_run(&g_flash_selected_run_meta);
    }
    if (g_flash_selected_run_meta.format == STPG_PATTERN_FORMAT_RATE_VECTOR) {
        return stpg_buffer_prepare_flash_rate_vector_for_run(&g_flash_selected_run_meta);
    }
    return ERR_UNSUPPORTED;
}

static stpg_err_t stpg_buffer_parse_raw_header(const uint8_t *buf, uint32_t size, PatternHeader *out_header) {
    if (buf == 0 || size < STPG_HEADER_SIZE) return ERR_BAD_SIZE;
    const PatternHeader *header = (const PatternHeader *)(const void *)buf;
    if (header->magic != STPG_PATTERN_MAGIC) return ERR_BAD_MAGIC;
    if (header->version != STPG_PATTERN_VERSION && header->version != STPG_PATTERN_VERSION_WIDE_WAIT) return ERR_BAD_VERSION;
    if (header->header_size != STPG_HEADER_SIZE) return ERR_PATTERN_INVALID;
    const uint32_t event_size = header->version == STPG_PATTERN_VERSION
        ? STPG_EXEC_EVENT_SIZE : STPG_EXEC_EVENT32_SIZE;
    if (header->event_size != event_size) return ERR_PATTERN_INVALID;
    if (header->data_size != header->event_size * header->event_count) return ERR_PATTERN_INVALID;
    if ((size_t)header->data_size + STPG_HEADER_SIZE != size) return ERR_BAD_SIZE;
    if (out_header != 0) {
        *out_header = *header;
    }
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_parse_macro_header(const uint8_t *buf, uint32_t size, MacroVectorHeader *out_header) {
    if (buf == 0 || size < STPG_MACROVEC_HEADER_SIZE) return ERR_BAD_SIZE;
    const MacroVectorHeader *header = (const MacroVectorHeader *)(const void *)buf;
    if (header->magic != STPG_MACROVEC_MAGIC) return ERR_BAD_MAGIC;
    if (header->version != STPG_PATTERN_VERSION) return ERR_BAD_VERSION;
    if (header->header_size != STPG_MACROVEC_HEADER_SIZE) return ERR_PATTERN_INVALID;
    if (header->record_size != STPG_MACRO_RECORD_SIZE) return ERR_PATTERN_INVALID;
    if (header->data_size != header->record_size * header->record_count) return ERR_PATTERN_INVALID;
    if ((size_t)header->data_size + STPG_MACROVEC_HEADER_SIZE != size) return ERR_BAD_SIZE;
    if (out_header != 0) {
        *out_header = *header;
    }
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_parse_macro_p2_header(const uint8_t *buf, uint32_t size, MacroVectorHeader *out_header) {
    if (buf == 0 || size < STPG_MACROVEC_HEADER_SIZE) return ERR_BAD_SIZE;
    const MacroVectorHeader *header = (const MacroVectorHeader *)(const void *)buf;
    if (header->magic != STPG_MACROP2VEC_MAGIC) return ERR_BAD_MAGIC;
    if (header->version != STPG_PATTERN_VERSION) return ERR_BAD_VERSION;
    if (header->header_size != STPG_MACROVEC_HEADER_SIZE) return ERR_PATTERN_INVALID;
    if (header->record_size != STPG_MACRO_P2_RECORD_SIZE) return ERR_PATTERN_INVALID;
    if (header->data_size != header->record_size * header->record_count) return ERR_PATTERN_INVALID;
    if ((size_t)header->data_size + STPG_MACROVEC_HEADER_SIZE != size) return ERR_BAD_SIZE;
    if (out_header != 0) {
        *out_header = *header;
    }
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_read_macro_record_psram(const MacroVectorHeader *header,
                                                      uint32_t record_index,
                                                      MacroVectorRecord *out_record) {
    if (header == 0 || out_record == 0) {
        return ERR_BAD_SIZE;
    }
    if (record_index >= header->record_count) {
        return ERR_BAD_SIZE;
    }
    const uint32_t offset =
        g_psram_slot.base + STPG_MACROVEC_HEADER_SIZE + (record_index * STPG_MACRO_RECORD_SIZE);
    return stpg_psram_read(offset, out_record, sizeof(*out_record));
}

static stpg_err_t stpg_buffer_expand_macro_p2_vector_buffer(const uint8_t *buf, uint32_t size) {
    MacroVectorHeader header;
    stpg_err_t err = stpg_buffer_parse_macro_p2_header(buf, size, &header);
    if (err != ERR_NONE) return err;

    const uint8_t *record_data = buf + STPG_MACROVEC_HEADER_SIZE;
    uint32_t event_count = 0u;
    uint32_t sample_event_count = 0u;
    for (uint32_t index = 0u; index < header.record_count; ++index) {
        const MacroVectorRecordP2 *record =
            (const MacroVectorRecordP2 *)(const void *)(record_data + (index * STPG_MACRO_P2_RECORD_SIZE));
        const bool two_event_exec =
            (record->exec_family == 1u) || (record->exec_family == 2u) || (record->exec_family == 0x81u);
        const uint32_t samples_per_repeat = two_event_exec
            ? (uint32_t)(((stpg_macro_load_u24(record->sample0_mask) != 0u) ? 1u : 0u)
                + ((stpg_macro_load_u24(record->sample1_mask) != 0u) ? 1u : 0u))
            : (uint32_t)((stpg_macro_load_u24(record->sample0_mask) != 0u) ? 1u : 0u);
        const uint64_t event_add = (uint64_t)record->repeat * (uint64_t)(two_event_exec ? 2u : 1u);
        const uint64_t sample_add = (uint64_t)record->repeat * (uint64_t)samples_per_repeat;
        if ((uint64_t)event_count + event_add > STPG_MAX_EVENT_COUNT) return ERR_BAD_SIZE;
        if ((uint64_t)sample_event_count + sample_add > STPG_MAX_EVENT_COUNT) return ERR_BAD_SIZE;
        event_count += (uint32_t)event_add;
        sample_event_count += (uint32_t)sample_add;
    }

    uint32_t current_value = 0u;
    uint32_t current_enable = 0u;
    uint32_t event_index = 0u;
    bool have_prev_time = false;
    uint16_t prev_time_ticks = 0u;
    uint16_t prev_period_ticks = 0u;
    for (uint32_t index = 0u; index < header.record_count; ++index) {
        const MacroVectorRecordP2 *record =
            (const MacroVectorRecordP2 *)(const void *)(record_data + (index * STPG_MACRO_P2_RECORD_SIZE));
        const uint32_t drive0_value = stpg_macro_load_u24(record->drive0_value);
        const uint32_t drive0_enable = stpg_macro_load_u24(record->drive0_enable);
        const uint32_t drive1_value = stpg_macro_load_u24(record->drive1_value);
        const uint32_t drive1_enable = stpg_macro_load_u24(record->drive1_enable);
        const uint32_t sample0_mask = stpg_macro_load_u24(record->sample0_mask);
        const uint32_t sample1_mask = stpg_macro_load_u24(record->sample1_mask);
        const uint32_t expect0_value = stpg_macro_load_u24(record->expect0_value);
        const uint32_t expect1_value = stpg_macro_load_u24(record->expect1_value);
        for (uint32_t repeat_index = 0u; repeat_index < record->repeat; ++repeat_index) {
            bool first_in_cycle = true;
#define STPG_MACRO_P2_EMIT_EVENT(_time_ticks, _sample_trigger, _flags, _out_value, _out_enable, _sample_mask, _expect_value) \
            do { \
                const uint16_t time_ticks_ = (uint16_t)(_time_ticks); \
                uint32_t desired_delta_ticks = 0u; \
                if (!have_prev_time) { \
                    desired_delta_ticks = time_ticks_; \
                } else if (first_in_cycle) { \
                    desired_delta_ticks = (uint32_t)prev_period_ticks - (uint32_t)prev_time_ticks + (uint32_t)time_ticks_; \
                } else { \
                    desired_delta_ticks = (uint32_t)time_ticks_ - (uint32_t)prev_time_ticks; \
                } \
                uint32_t wait_word = 0u; \
                uint32_t required_ticks = 0u; \
                stpg_err_t wait_err = stpg_batch_compute_wait_word(desired_delta_ticks, (_sample_trigger), &wait_word, &required_ticks); \
                if (wait_err != ERR_NONE) { \
                    return wait_err; \
                } \
                stpg_macro_write_event(event_index++, (uint16_t)wait_word, (_flags), (_out_value), (_out_enable), (_sample_mask), (_expect_value), header.active_pins); \
                have_prev_time = true; \
                prev_time_ticks = time_ticks_; \
                prev_period_ticks = record->period_ticks; \
                first_in_cycle = false; \
            } while (0)

            const uint16_t sample_flags = (uint16_t)(record->flags & (STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT));
            switch (record->exec_family) {
                case 1u:
                    STPG_MACRO_P2_EMIT_EVENT(0u, false, 0u, drive0_value, drive0_enable, 0u, 0u);
                    STPG_MACRO_P2_EMIT_EVENT(record->wait_first_ticks,
                                             sample1_mask != 0u,
                                             sample_flags,
                                             drive1_value,
                                             drive1_enable,
                                             sample1_mask,
                                             expect1_value);
                    current_value = drive1_value;
                    current_enable = drive1_enable;
                    break;
                case 2u:
                case 0x81u:
                    STPG_MACRO_P2_EMIT_EVENT(0u,
                                             sample0_mask != 0u,
                                             sample_flags,
                                             current_value,
                                             current_enable,
                                             sample0_mask,
                                             expect0_value);
                    STPG_MACRO_P2_EMIT_EVENT(record->wait_first_ticks,
                                             false,
                                             0u,
                                             drive1_value,
                                             drive1_enable,
                                             0u,
                                             0u);
                    current_value = drive1_value;
                    current_enable = drive1_enable;
                    break;
                case 3u:
                    STPG_MACRO_P2_EMIT_EVENT(0u, false, 0u, drive0_value, drive0_enable, 0u, 0u);
                    current_value = drive0_value;
                    current_enable = drive0_enable;
                    break;
                case 4u:
                    STPG_MACRO_P2_EMIT_EVENT(0u,
                                             sample0_mask != 0u,
                                             sample_flags,
                                             current_value,
                                             current_enable,
                                             sample0_mask,
                                             expect0_value);
                    break;
                case 5u:
                    STPG_MACRO_P2_EMIT_EVENT(0u,
                                             sample0_mask != 0u,
                                             sample_flags,
                                             drive0_value,
                                             drive0_enable,
                                             sample0_mask,
                                             expect0_value);
                    current_value = drive0_value;
                    current_enable = drive0_enable;
                    break;
                default:
                    return ERR_UNSUPPORTED;
            }
#undef STPG_MACRO_P2_EMIT_EVENT
        }
    }

    g_pattern_size = size;
    g_event_count = event_count;
    g_sample_event_count = sample_event_count;
    g_pio_word_count = event_count * 4u;
    g_active_pins = header.active_pins;
    g_has_pattern = true;
    g_use_expanded_event_view = false;
    g_stream_compare_collecting = false;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    g_stream_has_samples = sample_event_count != 0u;
    stpg_stream_telemetry_reset();
    stpg_stream_vector_cache_reset();
    memset(&g_rate_info, 0, sizeof(g_rate_info));
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_expand_macro_vector_buffer(const uint8_t *buf, uint32_t size) {
    MacroVectorHeader header;
    stpg_err_t err = stpg_buffer_parse_macro_header(buf, size, &header);
    if (err != ERR_NONE) return err;

    const uint8_t *record_data = buf + STPG_MACROVEC_HEADER_SIZE;
    uint32_t event_count = 0u;
    uint32_t sample_event_count = 0u;
    for (uint32_t index = 0u; index < header.record_count; ++index) {
        const MacroVectorRecord *record =
            (const MacroVectorRecord *)(const void *)(record_data + (index * STPG_MACRO_RECORD_SIZE));
        const uint32_t events_per_repeat = (record->family == 1u || record->family == 2u) ? 2u : 1u;
        const uint32_t samples_per_repeat =
            ((record->flags & STPG_EVF_SAMPLE_POINT) != 0u) ? 1u : 0u;
        const uint64_t event_add = (uint64_t)record->repeat * (uint64_t)events_per_repeat;
        const uint64_t sample_add = (uint64_t)record->repeat * (uint64_t)samples_per_repeat;
        if ((uint64_t)event_count + event_add > STPG_MAX_EVENT_COUNT) {
            return ERR_BAD_SIZE;
        }
        if ((uint64_t)sample_event_count + sample_add > STPG_MAX_EVENT_COUNT) {
            return ERR_BAD_SIZE;
        }
        event_count += (uint32_t)event_add;
        sample_event_count += (uint32_t)sample_add;
    }

    uint32_t current_value = 0u;
    uint32_t current_enable = 0u;
    uint32_t event_index = 0u;
    bool have_prev_time = false;
    uint16_t prev_time_ticks = 0u;
    uint16_t prev_period_ticks = 0u;
    for (uint32_t index = 0u; index < header.record_count; ++index) {
        const MacroVectorRecord *record =
            (const MacroVectorRecord *)(const void *)(record_data + (index * STPG_MACRO_RECORD_SIZE));
        const uint32_t drive_value = stpg_macro_load_u24(record->drive_value);
        const uint32_t drive_enable = stpg_macro_load_u24(record->drive_enable);
        const uint32_t sample_mask = stpg_macro_load_u24(record->sample_mask);
        const uint32_t expect_value = stpg_macro_load_u24(record->expect_value);
        for (uint32_t repeat_index = 0u; repeat_index < record->repeat; ++repeat_index) {
            bool first_in_cycle = true;
#define STPG_MACRO_EMIT_EVENT(_time_ticks, _sample_trigger, _flags, _out_value, _out_enable, _sample_mask, _expect_value) \
            do { \
                const uint16_t time_ticks_ = (uint16_t)(_time_ticks); \
                uint32_t desired_delta_ticks = 0u; \
                if (!have_prev_time) { \
                    desired_delta_ticks = time_ticks_; \
                } else if (first_in_cycle) { \
                    desired_delta_ticks = (uint32_t)prev_period_ticks - (uint32_t)prev_time_ticks + (uint32_t)time_ticks_; \
                } else { \
                    desired_delta_ticks = (uint32_t)time_ticks_ - (uint32_t)prev_time_ticks; \
                } \
                uint32_t wait_word = 0u; \
                uint32_t required_ticks = 0u; \
                stpg_err_t wait_err = stpg_batch_compute_wait_word(desired_delta_ticks, (_sample_trigger), &wait_word, &required_ticks); \
                if (wait_err != ERR_NONE) { \
                    return wait_err; \
                } \
                stpg_macro_write_event(event_index++, (uint16_t)wait_word, (_flags), (_out_value), (_out_enable), (_sample_mask), (_expect_value), header.active_pins); \
                have_prev_time = true; \
                prev_time_ticks = time_ticks_; \
                prev_period_ticks = record->period_ticks; \
                first_in_cycle = false; \
            } while (0)
            switch (record->family) {
                case 1u:
                    STPG_MACRO_EMIT_EVENT(0u, false, 0u, drive_value, drive_enable, 0u, 0u);
                    STPG_MACRO_EMIT_EVENT(record->phase_ticks,
                                          (record->flags & STPG_EVF_SAMPLE_POINT) != 0u,
                                          record->flags,
                                          drive_value,
                                          drive_enable,
                                          sample_mask,
                                          expect_value);
                    current_value = drive_value;
                    current_enable = drive_enable;
                    break;
                case 2u:
                    STPG_MACRO_EMIT_EVENT(0u,
                                          (record->flags & STPG_EVF_SAMPLE_POINT) != 0u,
                                          record->flags,
                                          current_value,
                                          current_enable,
                                          sample_mask,
                                          expect_value);
                    STPG_MACRO_EMIT_EVENT(record->phase_ticks, false, 0u, drive_value, drive_enable, 0u, 0u);
                    current_value = drive_value;
                    current_enable = drive_enable;
                    break;
                case 3u:
                    STPG_MACRO_EMIT_EVENT(0u, false, 0u, drive_value, drive_enable, 0u, 0u);
                    current_value = drive_value;
                    current_enable = drive_enable;
                    break;
                case 4u:
                    STPG_MACRO_EMIT_EVENT(0u,
                                          (record->flags & STPG_EVF_SAMPLE_POINT) != 0u,
                                          record->flags,
                                          current_value,
                                          current_enable,
                                          sample_mask,
                                          expect_value);
                    break;
                case 5u:
                    STPG_MACRO_EMIT_EVENT(0u,
                                          (record->flags & STPG_EVF_SAMPLE_POINT) != 0u,
                                          record->flags,
                                          drive_value,
                                          drive_enable,
                                          sample_mask,
                                          expect_value);
                    current_value = drive_value;
                    current_enable = drive_enable;
                    break;
                default:
#undef STPG_MACRO_EMIT_EVENT
                    return ERR_UNSUPPORTED;
            }
#undef STPG_MACRO_EMIT_EVENT
        }
    }

    g_pattern_size = size;
    g_event_count = event_count;
    g_sample_event_count = sample_event_count;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    g_pio_word_count = event_count * 4u;
    g_active_pins = header.active_pins;
    g_has_pattern = true;
    g_use_expanded_event_view = true;
    g_stream_compare_collecting = false;
    g_stream_has_samples = sample_event_count != 0u;
    stpg_stream_vector_cache_reset();
    memset(&g_rate_info, 0, sizeof(g_rate_info));
    stpg_stream_telemetry_reset();
    stpg_stream_hotspot_reset();
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_parse_flash_staging_meta(uint32_t total_size, uint32_t total_crc,
                                                       FlashSlotMeta *out_meta,
                                                       uint32_t *out_entry_count,
                                                       uint32_t *out_aux_count) {
    if (out_meta == 0 || out_entry_count == 0 || out_aux_count == 0) {
        return ERR_BAD_SIZE;
    }
    uint8_t header_bytes[STPG_RATEVEC_HEADER_SIZE];
    stpg_err_t err = stpg_psram_read(STPG_PSRAM_STAGING_BASE, header_bytes, sizeof(header_bytes));
    if (err != ERR_NONE) return err;

    FlashSlotMeta *const meta = out_meta;
    memset(meta, 0, sizeof(*meta));
    meta->magic = STPG_FLASH_SLOT_MAGIC;
    meta->meta_version = STPG_FLASH_SLOT_META_VERSION;
    meta->pattern_offset = STPG_FLASH_SLOT_META_BYTES;
    meta->pattern_size = total_size;
    meta->pattern_crc32 = total_crc;
    meta->active_pins = g_active_pins;
    meta->tick_hz = STPG_TICK_HZ;
    *out_aux_count = 0u;

    const uint32_t magic = *(const uint32_t *)(const void *)header_bytes;
    if (magic == STPG_PATTERN_MAGIC) {
        PatternHeader header;
        err = stpg_psram_read(STPG_PSRAM_STAGING_BASE, &header, sizeof(header));
        if (err != ERR_NONE) return err;
        err = stpg_buffer_parse_raw_header((const uint8_t *)(const void *)&header, total_size, &header);
        if (err != ERR_NONE) return err;
        uint32_t payload_crc = 0u;
        err = stpg_psram_checksum(STPG_PSRAM_STAGING_BASE + STPG_HEADER_SIZE, header.data_size, &payload_crc);
        if (err != ERR_NONE) return err;
        if (payload_crc != header.data_crc32) return ERR_BAD_CRC;
        meta->format = STPG_PATTERN_FORMAT_RAW_EVENT;
        meta->active_pins = header.active_pins;
        meta->tick_hz = header.tick_hz;
        *out_entry_count = header.event_count;
    } else if (magic == STPG_MACROVEC_MAGIC) {
        MacroVectorHeader header;
        err = stpg_psram_read(STPG_PSRAM_STAGING_BASE, &header, sizeof(header));
        if (err != ERR_NONE) return err;
        err = stpg_buffer_parse_macro_header((const uint8_t *)(const void *)&header, total_size, &header);
        if (err != ERR_NONE) return err;
        if (stpg_crc32(&header, STPG_MACROVEC_HEADER_SIZE - sizeof(header.header_crc32)) != header.header_crc32) {
            return ERR_BAD_CRC;
        }
        uint32_t payload_crc = 0u;
        err = stpg_psram_checksum(STPG_PSRAM_STAGING_BASE + STPG_MACROVEC_HEADER_SIZE, header.data_size, &payload_crc);
        if (err != ERR_NONE) return err;
        if (payload_crc != header.data_crc32) return ERR_BAD_CRC;
        meta->format = STPG_PATTERN_FORMAT_MACRO_STREAM;
        meta->active_pins = header.active_pins;
        meta->tick_hz = header.tick_hz;
        *out_entry_count = header.record_count;
    } else if (magic == STPG_MACROP2VEC_MAGIC) {
        MacroVectorHeader header;
        err = stpg_psram_read(STPG_PSRAM_STAGING_BASE, &header, sizeof(header));
        if (err != ERR_NONE) return err;
        err = stpg_buffer_parse_macro_p2_header((const uint8_t *)(const void *)&header, total_size, &header);
        if (err != ERR_NONE) return err;
        if (stpg_crc32(&header, STPG_MACROVEC_HEADER_SIZE - sizeof(header.header_crc32)) != header.header_crc32) {
            return ERR_BAD_CRC;
        }
        uint32_t payload_crc = 0u;
        err = stpg_psram_checksum(STPG_PSRAM_STAGING_BASE + STPG_MACROVEC_HEADER_SIZE, header.data_size, &payload_crc);
        if (err != ERR_NONE) return err;
        if (payload_crc != header.data_crc32) return ERR_BAD_CRC;
        meta->format = STPG_PATTERN_FORMAT_MACRO_P2;
        meta->active_pins = header.active_pins;
        meta->tick_hz = header.tick_hz;
        *out_entry_count = header.record_count;
    } else if (magic == STPG_RATEVEC_MAGIC) {
        RateVectorHeader header;
        err = stpg_psram_read(STPG_PSRAM_STAGING_BASE, &header, sizeof(header));
        if (err != ERR_NONE) return err;
        if (header.version != STPG_PATTERN_VERSION && header.version != STPG_PATTERN_VERSION_WIDE_WAIT) return ERR_BAD_VERSION;
        if (header.header_size != STPG_RATEVEC_HEADER_SIZE) return ERR_RATEVEC_BAD_HEADER;
        if (header.active_pins != STPG_ACTIVE_PINS_V01 && header.active_pins != STPG_ACTIVE_PINS_V11) return ERR_UNSUPPORTED;
        if (header.tick_hz != STPG_TICK_HZ) return ERR_UNSUPPORTED;
        if (header.rate_count == 0u || header.slot_count == 0u || header.vector_count == 0u) return ERR_RATEVEC_BAD_HEADER;
        if (stpg_crc32(&header, STPG_RATEVEC_HEADER_SIZE - sizeof(header.header_crc32)) != header.header_crc32) {
            return ERR_RATEVEC_BAD_CRC;
        }
        meta->format = STPG_PATTERN_FORMAT_RATE_VECTOR;
        meta->active_pins = header.active_pins;
        meta->tick_hz = header.tick_hz;
        *out_entry_count = header.vector_count;
        *out_aux_count = header.rate_count;
    } else {
        return ERR_BAD_MAGIC;
    }

    meta->meta_crc32 = 0u;
    meta->meta_crc32 = stpg_crc32(meta, sizeof(*meta));
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_read_rate_vector(uint32_t offset, void *dst, uint32_t size) {
    g_stream_telemetry.rate_vector_read_call_count += 1u;
    g_stream_telemetry.rate_vector_read_byte_total += size;
    if (g_rate_vector_finalize_shadow != 0) {
        if (offset > g_rate_vector_finalize_shadow_size
            || size > g_rate_vector_finalize_shadow_size - offset) {
            return ERR_PSRAM_BOUNDS;
        }
        memcpy(dst, g_rate_vector_finalize_shadow + offset, size);
        return ERR_NONE;
    }
#ifdef PICO_ON_DEVICE
    const uint32_t start_us = time_us_32();
#endif
    stpg_err_t err = stpg_psram_read(STPG_PSRAM_PATTERN_BASE + offset, dst, size);
#ifdef PICO_ON_DEVICE
    const uint32_t elapsed_us = time_us_32() - start_us;
    g_stream_telemetry.rate_vector_read_total_us += elapsed_us;
    g_stream_telemetry.rate_vector_read_last_us = elapsed_us;
    if (elapsed_us > g_stream_telemetry.rate_vector_read_max_us) {
        g_stream_telemetry.rate_vector_read_max_us = elapsed_us;
    }
#else
    const uint32_t elapsed_us = 0u;
#endif
    g_stream_telemetry.rate_vector_read_last_byte_count = size;
    g_stream_telemetry.rate_vector_read_last_us = elapsed_us;
    return err;
}

static stpg_err_t stpg_buffer_validate_vector_record_fields(const VectorRecord *vector,
                                                            uint32_t rate_count,
                                                            bool allow_loop_ref,
                                                            uint32_t active_pins) {
    if ((vector->flags & ~STPG_VECTOR_VALID_FLAG_MASK) != 0u) return ERR_RATEVEC_BAD_HEADER;
    if (allow_loop_ref && vector->flags == STPG_VECTOR_FLAG_LOOP_REF) {
        const LoopRefRecord *loop_ref = (const LoopRefRecord *)(const void *)vector;
        if (loop_ref->reserved0 != 0u || loop_ref->reserved1 != 0u
            || loop_ref->reserved2 != 0u || loop_ref->reserved3 != 0u || loop_ref->reserved4 != 0u) {
            return ERR_RATEVEC_BAD_HEADER;
        }
        return ERR_NONE;
    }
    if (vector->rate_id >= rate_count) return ERR_VECTOR_BAD_RATE_ID;
    if (vector->flags != 0u && vector->flags != STPG_VECTOR_FLAG_EXPLICIT_WAIT_P4_END) return ERR_RATEVEC_BAD_HEADER;
    if (vector->repeat == 0u) return ERR_RATEVEC_BAD_HEADER;
    const uint32_t pin_mask = active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
    if ((vector->drive_value & ~pin_mask) != 0u
        || (vector->drive_enable & ~pin_mask) != 0u
        || (vector->expect_value & ~pin_mask) != 0u
        || (vector->expect_mask & ~pin_mask) != 0u) {
        return ERR_RATEVEC_BAD_HEADER;
    }
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_parse_loop_descriptors_psram(const RateVectorHeader *header,
                                                           stpg_loop_desc_t *out_descs,
                                                           uint32_t *out_count) {
    uint32_t count = 0u;
    uint32_t loop_offset = header->reserved0;
    if (loop_offset == 0u) {
        if (out_count != 0) *out_count = 0u;
        return ERR_NONE;
    }
    while (loop_offset < header->vector_offset) {
        if (count >= STPG_MAX_LOOP_BLOCKS) return ERR_RATE_TOO_COMPLEX;
        LoopBlockHeader loop_header;
        stpg_err_t err = stpg_buffer_read_rate_vector(loop_offset, &loop_header, sizeof(loop_header));
        if (err != ERR_NONE) return err;
        if (loop_header.loop_count == 0u || loop_header.body_record_count == 0u) return ERR_LOOP_UNSUPPORTED;
        const uint64_t body_end = (uint64_t)loop_offset + STPG_LOOP_BLOCK_HEADER_SIZE
            + ((uint64_t)loop_header.body_record_count * STPG_VECTOR_RECORD_SIZE);
        if (body_end > header->vector_offset) return ERR_RATEVEC_BAD_OFFSET;
        out_descs[count].body_offset = loop_offset + STPG_LOOP_BLOCK_HEADER_SIZE;
        out_descs[count].body_record_count = loop_header.body_record_count;
        out_descs[count].loop_count = loop_header.loop_count;
        for (uint32_t index = 0; index < loop_header.body_record_count; ++index) {
            VectorRecord record;
            err = stpg_buffer_read_rate_vector(out_descs[count].body_offset + (index * STPG_VECTOR_RECORD_SIZE),
                                               &record, sizeof(record));
            if (err != ERR_NONE) return err;
            err = stpg_buffer_validate_vector_record_fields(&record, header->rate_count, false, header->active_pins);
            if (err != ERR_NONE) return err;
        }
        loop_offset = (uint32_t)body_end;
        count += 1u;
    }
    if (loop_offset != header->vector_offset) return ERR_RATEVEC_BAD_OFFSET;
    if (out_count != 0) *out_count = count;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_read_rate_desc(const RateVectorHeader *header, uint32_t rate_id,
                                             stpg_rate_desc_runtime_t *out_rate) {
    if (header == 0 || out_rate == 0 || rate_id >= header->rate_count) return ERR_RATEVEC_BAD_OFFSET;
    memset(out_rate, 0, sizeof(*out_rate));
    if (header->version == STPG_PATTERN_VERSION_WIDE_WAIT) {
        RateDesc32 wire;
        stpg_err_t err = stpg_buffer_read_rate_vector(
            header->rate_table_offset + (rate_id * STPG_RATE_DESC32_SIZE), &wire, sizeof(wire));
        if (err != ERR_NONE) return err;
        if (wire.reserved != 0u) return ERR_RATEVEC_BAD_HEADER;
        out_rate->period_ticks = wire.period_ticks;
        out_rate->slot_count = wire.slot_count;
        out_rate->flags = wire.flags;
        out_rate->slot_offset = wire.slot_offset;
        return ERR_NONE;
    }
    RateDesc wire;
    stpg_err_t err = stpg_buffer_read_rate_vector(
        header->rate_table_offset + (rate_id * STPG_RATE_DESC_SIZE), &wire, sizeof(wire));
    if (err != ERR_NONE) return err;
    out_rate->period_ticks = wire.period_ticks;
    out_rate->slot_count = wire.slot_count;
    out_rate->flags = wire.flags;
    out_rate->slot_offset = wire.slot_offset;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_read_rate_slot(const RateVectorHeader *header, uint32_t slot_index,
                                             stpg_rate_slot_runtime_t *out_slot) {
    if (header == 0 || out_slot == 0 || slot_index >= header->slot_count) return ERR_RATEVEC_BAD_OFFSET;
    memset(out_slot, 0, sizeof(*out_slot));
    if (header->version == STPG_PATTERN_VERSION_WIDE_WAIT) {
        RateSlot32 wire;
        stpg_err_t err = stpg_buffer_read_rate_vector(
            header->slot_table_offset + (slot_index * STPG_RATE_SLOT32_SIZE), &wire, sizeof(wire));
        if (err != ERR_NONE) return err;
        if (wire.reserved != 0u) return ERR_RATEVEC_BAD_HEADER;
        out_slot->time_ticks = wire.time_ticks;
        out_slot->flags = wire.flags;
        out_slot->drive_mask = wire.drive_mask;
        out_slot->dir_mask = wire.dir_mask;
        out_slot->sample_mask = wire.sample_mask;
        return ERR_NONE;
    }
    RateSlot wire;
    stpg_err_t err = stpg_buffer_read_rate_vector(
        header->slot_table_offset + (slot_index * STPG_RATE_SLOT_SIZE), &wire, sizeof(wire));
    if (err != ERR_NONE) return err;
    out_slot->time_ticks = wire.time_ticks;
    out_slot->flags = wire.flags;
    out_slot->drive_mask = wire.drive_mask;
    out_slot->dir_mask = wire.dir_mask;
    out_slot->sample_mask = wire.sample_mask;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_read_vector_record(const RateVectorHeader *header, uint32_t vector_index, VectorRecord *out_vector) {
    return stpg_buffer_read_rate_vector(header->vector_offset + (vector_index * STPG_VECTOR_RECORD_SIZE), out_vector, sizeof(*out_vector));
}

static stpg_err_t stpg_buffer_stream_prefetch_vectors_into(const RateVectorHeader *header,
                                                           uint32_t vector_index,
                                                           uint32_t max_records,
                                                           uint32_t cache_capacity_records,
                                                           VectorRecord *dst_cache,
                                                           uint32_t *out_base_index,
                                                           uint32_t *out_count) {
#define STPG_STREAM_VECTOR_CACHE_READ_RETRY_COUNT 2u
#ifdef PICO_ON_DEVICE
    const uint32_t start_us = time_us_32();
#endif
    if (header == 0 || dst_cache == 0 || out_base_index == 0 || out_count == 0) {
        return ERR_BAD_SIZE;
    }
    *out_base_index = vector_index;
    *out_count = 0u;
    if (vector_index >= header->vector_count) {
        return ERR_NONE;
    }
    const uint32_t remaining = header->vector_count - vector_index;
    uint32_t fetch_limit = cache_capacity_records;
    if (fetch_limit > STPG_STREAM_VECTOR_CACHE_READ_RECORDS) {
        fetch_limit = STPG_STREAM_VECTOR_CACHE_READ_RECORDS;
    }
    if (max_records != 0u && max_records < fetch_limit) {
        fetch_limit = max_records;
    }
    const uint32_t fetch_count = remaining < fetch_limit ? remaining : fetch_limit;
    const uint32_t fetch_size = fetch_count * STPG_VECTOR_RECORD_SIZE;
    const bool allow_loop_ref = (header->flags & STPG_RATEVEC_FLAG_HAS_LOOP) != 0u;
    stpg_err_t err = ERR_RATEVEC_BAD_HEADER;
    for (uint32_t attempt = 0u; attempt < STPG_STREAM_VECTOR_CACHE_READ_RETRY_COUNT; ++attempt) {
        g_stream_telemetry.prefetch_diag_attempt_base_index = vector_index;
        g_stream_telemetry.prefetch_diag_attempt_record_count = fetch_count;
        g_stream_telemetry.prefetch_diag_attempt_number = attempt + 1u;
        g_stream_telemetry.prefetch_diag_destination_address =
            (uint32_t)(uintptr_t)dst_cache;
        g_stream_telemetry.prefetch_diag_destination_bytes = fetch_size;
        stpg_stream_capture_qmi_snapshot(true);
        err = stpg_buffer_read_rate_vector(header->vector_offset + (vector_index * STPG_VECTOR_RECORD_SIZE),
                                           dst_cache,
                                           fetch_size);
        stpg_stream_capture_qmi_snapshot(false);
        g_stream_telemetry.prefetch_diag_post_read_error = (uint32_t)err;
        g_stream_telemetry.prefetch_diag_post_read_complete = 1u;
        stpg_stream_capture_words(g_stream_telemetry.prefetch_diag_post_read_words, dst_cache);
        if (err != ERR_NONE) {
            stpg_stream_capture_mapped_source(header, vector_index);
            stpg_stream_record_prefetch_failure(
                STPG_STREAM_PREFETCH_DIAG_READ_ERROR,
                vector_index,
                fetch_count,
                attempt + 1u,
                UINT32_MAX,
                UINT32_MAX,
                err,
                0);
            return err;
        }
        err = ERR_NONE;
        uint32_t failure_offset = UINT32_MAX;
        const VectorRecord *failure_record = 0;
        for (uint32_t index = 0u; index < fetch_count; ++index) {
            err = stpg_buffer_validate_vector_record_fields(
                &dst_cache[index], header->rate_count, allow_loop_ref, header->active_pins);
            if (err != ERR_NONE) {
                failure_offset = index;
                failure_record = &dst_cache[index];
                break;
            }
        }
        if (err == ERR_NONE) {
            break;
        }
        stpg_stream_record_prefetch_failure(
            STPG_STREAM_PREFETCH_DIAG_VALIDATION_ERROR,
            vector_index,
            fetch_count,
            attempt + 1u,
            vector_index + failure_offset,
            failure_offset,
            err,
            failure_record);
        stpg_stream_capture_words(g_stream_telemetry.prefetch_diag_post_validation_words,
                                  failure_record);
        stpg_stream_capture_mapped_source(header, vector_index + failure_offset);
    }
    if (err != ERR_NONE) {
        return err;
    }
    *out_base_index = vector_index;
    *out_count = fetch_count;
    g_stream_telemetry.prefetch_call_count += 1u;
    g_stream_telemetry.prefetch_record_count_total += fetch_count;
    g_stream_telemetry.prefetch_byte_total += fetch_size;
    g_stream_telemetry.prefetch_last_record_count = fetch_count;
    g_stream_telemetry.prefetch_last_byte_count = fetch_size;
    g_stream_telemetry.prefetch_diag_last_success_base_index = vector_index;
    g_stream_telemetry.prefetch_diag_last_success_record_count = fetch_count;
#ifdef PICO_ON_DEVICE
    const uint32_t elapsed_us = time_us_32() - start_us;
    g_stream_telemetry.prefetch_total_us += elapsed_us;
    if (elapsed_us > g_stream_telemetry.prefetch_max_us) {
        g_stream_telemetry.prefetch_max_us = elapsed_us;
    }
#endif
    return ERR_NONE;
}
#undef STPG_STREAM_VECTOR_CACHE_READ_RETRY_COUNT

static stpg_err_t stpg_buffer_stream_prefetch_vectors_limited(const RateVectorHeader *header,
                                                              uint32_t vector_index,
                                                              uint32_t max_records) {
    return stpg_buffer_stream_prefetch_vectors_into(
        header,
        vector_index,
        max_records,
        STPG_STREAM_VECTOR_CACHE_RECORDS,
        g_stream_vector_cache,
        &g_stream_vector_cache_base_index,
        &g_stream_vector_cache_count);
}

static __attribute__((unused)) stpg_err_t stpg_buffer_stream_prefetch_vectors(
    const RateVectorHeader *header,
    uint32_t vector_index) {
    return stpg_buffer_stream_prefetch_vectors_limited(header, vector_index, 0u);
}

static __attribute__((unused)) stpg_err_t stpg_stream_vector_cache_maybe_prefetch_next(
    const stpg_rate_stream_state_t *state,
    uint32_t current_vector_index,
    uint32_t max_records) {
    if (state == 0 || state->loop_active) {
        return ERR_NONE;
    }
    if (!stpg_stream_vector_cache_current_contains(current_vector_index)) {
        return ERR_NONE;
    }
    if (g_stream_vector_cache_next_count != 0u) {
        return ERR_NONE;
    }
    const uint32_t current_offset = current_vector_index - g_stream_vector_cache_base_index;
    if (current_offset >= g_stream_vector_cache_count) {
        return ERR_NONE;
    }
    const uint32_t remaining_current = g_stream_vector_cache_count - current_offset;
    if (remaining_current > STPG_STREAM_VECTOR_CACHE_PREFETCH_WATERMARK_RECORDS) {
        return ERR_NONE;
    }
    const uint32_t next_base = g_stream_vector_cache_base_index + g_stream_vector_cache_count;
    if (next_base >= state->header.vector_count) {
        return ERR_NONE;
    }
    return stpg_buffer_stream_prefetch_vectors_into(
        &state->header,
        next_base,
        max_records,
        STPG_STREAM_VECTOR_CACHE_NEXT_RECORDS,
        g_stream_vector_cache_next,
        &g_stream_vector_cache_next_base_index,
        &g_stream_vector_cache_next_count);
}

#ifdef STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_HOOK
#ifndef STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_NOPS
#define STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_NOPS 76
#endif
#define STPG_STRINGIFY_INNER(x) #x
#define STPG_STRINGIFY(x) STPG_STRINGIFY_INNER(x)
static inline void stpg_stream_vector_cache_true_inert_pad_hook(void) {
    __asm volatile(
        ".rept " STPG_STRINGIFY(STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_NOPS) "\n"
        "nop\n"
        ".endr\n"
        :
        :
        : "memory");
}
#undef STPG_STRINGIFY
#undef STPG_STRINGIFY_INNER
#endif

static stpg_err_t STPG_STREAM_PRODUCER_FUNC(stpg_stream_vector_cache_ensure_current)(const stpg_rate_stream_state_t *state,
                                                          uint32_t current_vector_index,
                                                          uint32_t max_records,
                                                          bool *out_promoted_from_next) {
    if (out_promoted_from_next != 0) {
        *out_promoted_from_next = false;
    }
    if (state == 0) {
        return ERR_BAD_SIZE;
    }
    if (stpg_stream_vector_cache_current_contains(current_vector_index)) {
        return ERR_NONE;
    }
    if (stpg_stream_vector_cache_next_contains(current_vector_index)) {
        stpg_stream_vector_cache_promote_next();
        if (out_promoted_from_next != 0) {
            *out_promoted_from_next = true;
        }
        return ERR_NONE;
    }
    g_stream_vector_cache_next_base_index = 0u;
    g_stream_vector_cache_next_count = 0u;
    return stpg_buffer_stream_prefetch_vectors_limited(&state->header, current_vector_index, max_records);
}

static stpg_err_t stpg_buffer_read_loop_body_record(const stpg_loop_desc_t *loop_desc,
                                                    uint32_t body_index,
                                                    VectorRecord *out_vector) {
    return stpg_buffer_read_rate_vector(loop_desc->body_offset + (body_index * STPG_VECTOR_RECORD_SIZE),
                                        out_vector, sizeof(*out_vector));
}

static stpg_err_t STPG_STREAM_PRODUCER_FUNC(stpg_buffer_stream_read_loop_body_record)(
    const stpg_rate_stream_state_t *state,
    uint32_t loop_id,
    uint32_t body_index,
    VectorRecord *out_vector) {
#define STPG_STREAM_LOOP_BODY_CACHE_READ_RETRY_COUNT 2u
    if (state == 0 || out_vector == 0 || loop_id >= state->loop_block_count) {
        return ERR_BAD_SIZE;
    }
    const stpg_loop_desc_t *loop_desc = &state->loop_descs[loop_id];
    if (body_index >= loop_desc->body_record_count) {
        return ERR_RATEVEC_BAD_OFFSET;
    }

    g_stream_telemetry.loop_body_cache_lookup_count += 1u;
    if (g_stream_loop_body_cache_id == loop_id
        && body_index >= g_stream_loop_body_cache_base_index
        && body_index - g_stream_loop_body_cache_base_index < g_stream_loop_body_cache_count) {
        *out_vector = g_stream_vector_cache[
            body_index - g_stream_loop_body_cache_base_index];
        g_stream_telemetry.loop_body_cache_hit_count += 1u;
        return ERR_NONE;
    }

    g_stream_telemetry.loop_body_cache_miss_count += 1u;
    uint32_t cache_limit = STPG_STREAM_VECTOR_CACHE_RECORDS;
    if (cache_limit > STPG_STREAM_VECTOR_CACHE_READ_RECORDS) {
        cache_limit = STPG_STREAM_VECTOR_CACHE_READ_RECORDS;
    }
    const uint32_t window_base = body_index - (body_index % cache_limit);
    const uint32_t remaining = loop_desc->body_record_count - window_base;
    const uint32_t fetch_count = remaining < cache_limit ? remaining : cache_limit;
    const uint32_t fetch_size = fetch_count * STPG_VECTOR_RECORD_SIZE;
    const uint64_t read_offset = (uint64_t)loop_desc->body_offset
        + ((uint64_t)window_base * STPG_VECTOR_RECORD_SIZE);
    if (read_offset > UINT32_MAX) {
        return ERR_RATEVEC_BAD_OFFSET;
    }

    /* Revoke the active top-level interpretation before overwriting the
     * shared storage, and revoke any old loop publication until the new
     * window has been read and validated completely. */
    g_stream_vector_cache_base_index = 0u;
    g_stream_vector_cache_count = 0u;
    stpg_stream_loop_body_cache_reset();

    stpg_err_t err = ERR_RATEVEC_BAD_HEADER;
    for (uint32_t attempt = 0u;
         attempt < STPG_STREAM_LOOP_BODY_CACHE_READ_RETRY_COUNT;
         ++attempt) {
        err = stpg_buffer_read_rate_vector(
            (uint32_t)read_offset, g_stream_vector_cache, fetch_size);
        if (err != ERR_NONE) {
            return err;
        }
        err = ERR_NONE;
        for (uint32_t index = 0u; index < fetch_count; ++index) {
            err = stpg_buffer_validate_vector_record_fields(
                &g_stream_vector_cache[index],
                state->header.rate_count,
                false,
                state->header.active_pins);
            if (err != ERR_NONE) {
                break;
            }
        }
        if (err == ERR_NONE) {
            break;
        }
        if (attempt + 1u < STPG_STREAM_LOOP_BODY_CACHE_READ_RETRY_COUNT) {
            g_stream_telemetry.loop_body_cache_validation_retry_count += 1u;
        }
    }
    if (err != ERR_NONE) {
        return err;
    }

#if STPG_QMI_DIAGNOSTIC_CAPTURE
    __dmb();
    __compiler_memory_barrier();
#endif
    g_stream_loop_body_cache_base_index = window_base;
    g_stream_loop_body_cache_count = fetch_count;
    g_stream_loop_body_cache_id = loop_id;
    g_stream_telemetry.loop_body_cache_load_count += 1u;
    g_stream_telemetry.loop_body_cache_record_count += fetch_count;
    g_stream_telemetry.loop_body_cache_byte_count += fetch_size;
    *out_vector = g_stream_vector_cache[body_index - window_base];
    return ERR_NONE;
#undef STPG_STREAM_LOOP_BODY_CACHE_READ_RETRY_COUNT
}

static stpg_err_t stpg_buffer_validate_rate_vector_psram(uint32_t total_size, uint32_t total_crc,
                                                          RateVectorHeader *out_header) {
    RateVectorHeader header;
    stpg_err_t err;
    if (g_rate_vector_finalize_shadow != 0) {
        if (total_size != g_rate_vector_finalize_shadow_size
            || sizeof(header) > g_rate_vector_finalize_shadow_size) {
            return ERR_BAD_SIZE;
        }
        memcpy(&header, g_rate_vector_finalize_shadow, sizeof(header));
    } else {
        err = stpg_psram_read(STPG_PSRAM_PATTERN_BASE, &header, sizeof(header));
        if (err != ERR_NONE) return err;
    }
    if (header.magic != STPG_RATEVEC_MAGIC) return ERR_BAD_MAGIC;
    if (header.version != STPG_PATTERN_VERSION && header.version != STPG_PATTERN_VERSION_WIDE_WAIT) return ERR_BAD_VERSION;
    if (header.header_size != STPG_RATEVEC_HEADER_SIZE) return ERR_RATEVEC_BAD_HEADER;
    const uint32_t allowed_flags = STPG_RATEVEC_FLAG_HAS_LOOP | STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4;
    if ((header.flags & ~allowed_flags) != 0u) return ERR_UNSUPPORTED;
    if (header.active_pins != STPG_ACTIVE_PINS_V01 && header.active_pins != STPG_ACTIVE_PINS_V11) return ERR_UNSUPPORTED;
    if (header.tick_hz != STPG_TICK_HZ) return ERR_UNSUPPORTED;
    if (header.rate_count == 0u || header.rate_count > STPG_MAX_RATES) return ERR_RATEVEC_BAD_HEADER;
    if (header.slot_count == 0u || header.vector_count == 0u) return ERR_RATEVEC_BAD_HEADER;
    if (header.reserved1 != 0u || header.reserved2 != 0u) return ERR_UNSUPPORTED;
    const bool has_loop = (header.flags & STPG_RATEVEC_FLAG_HAS_LOOP) != 0u;
    if (has_loop != (header.reserved0 != 0u)) return ERR_RATEVEC_BAD_HEADER;
    if ((header.rate_table_offset & 3u) != 0u || (header.slot_table_offset & 3u) != 0u || (header.vector_offset & 3u) != 0u) {
        return ERR_RATEVEC_BAD_OFFSET;
    }
    if (header.rate_table_offset < STPG_RATEVEC_HEADER_SIZE || header.slot_table_offset < header.rate_table_offset) {
        return ERR_RATEVEC_BAD_OFFSET;
    }
    const uint32_t rate_desc_size = header.version == STPG_PATTERN_VERSION_WIDE_WAIT
        ? STPG_RATE_DESC32_SIZE : STPG_RATE_DESC_SIZE;
    const uint32_t rate_slot_size = header.version == STPG_PATTERN_VERSION_WIDE_WAIT
        ? STPG_RATE_SLOT32_SIZE : STPG_RATE_SLOT_SIZE;
    const uint64_t rate_end = (uint64_t)header.rate_table_offset + ((uint64_t)header.rate_count * rate_desc_size);
    const uint64_t slot_end = (uint64_t)header.slot_table_offset + ((uint64_t)header.slot_count * rate_slot_size);
    const uint64_t vector_end = (uint64_t)header.vector_offset + ((uint64_t)header.vector_count * STPG_VECTOR_RECORD_SIZE);
    const uint32_t loop_block_offset = header.reserved0 != 0u ? header.reserved0 : header.vector_offset;
    if ((loop_block_offset & 3u) != 0u) return ERR_RATEVEC_BAD_OFFSET;
    if (loop_block_offset < slot_end || loop_block_offset > header.vector_offset) return ERR_RATEVEC_BAD_OFFSET;
    if (rate_end > total_size || slot_end > total_size || vector_end > total_size) return ERR_RATEVEC_BAD_OFFSET;
    if (rate_end > header.slot_table_offset || slot_end > loop_block_offset) return ERR_RATEVEC_BAD_OFFSET;
    if (stpg_crc32(&header, STPG_RATEVEC_HEADER_SIZE - sizeof(header.header_crc32)) != header.header_crc32) {
        return ERR_RATEVEC_BAD_CRC;
    }
    if (g_rate_vector_finalize_shadow == 0 && total_crc != 0u) {
        uint32_t stored_crc = 0u;
        err = stpg_psram_checksum(STPG_PSRAM_PATTERN_BASE, total_size, &stored_crc);
        if (err != ERR_NONE) return err;
        if (stored_crc != total_crc) return ERR_BAD_CRC;
    }

    uint32_t payload_crc = 0u;
    if (g_rate_vector_finalize_shadow != 0) {
        payload_crc = stpg_crc32(
            g_rate_vector_finalize_shadow + header.rate_table_offset,
            (uint32_t)vector_end - header.rate_table_offset);
    } else {
        err = stpg_psram_checksum(STPG_PSRAM_PATTERN_BASE + header.rate_table_offset,
                                  (uint32_t)vector_end - header.rate_table_offset, &payload_crc);
        if (err != ERR_NONE) return err;
    }
    if (payload_crc != header.data_crc32) return ERR_RATEVEC_BAD_CRC;
    const uint32_t pin_mask = header.active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;

    stpg_rate_desc_runtime_t rate_desc;
    for (uint32_t i = 0; i < header.rate_count; ++i) {
        err = stpg_buffer_read_rate_desc(&header, i, &rate_desc);
        if (err != ERR_NONE) return err;
        if (rate_desc.period_ticks == 0u) {
            stpg_stream_record_rate_mismatch(
                STPG_MISMATCH_STAGE_RATE_PERIOD_ZERO, i, 0u, 0u, 1u, 0u, 0u, 0u);
            return ERR_RATE_PERIOD_MISMATCH;
        }
        if (rate_desc.slot_count == 0u || rate_desc.slot_count > STPG_MAX_SLOTS_PER_RATE) return ERR_RATE_TOO_COMPLEX;
        if (rate_desc.flags != 0u) return ERR_RATEVEC_BAD_HEADER;
        if ((uint64_t)rate_desc.slot_offset + rate_desc.slot_count > header.slot_count) return ERR_RATEVEC_BAD_OFFSET;

        uint32_t prev_time_ticks = 0u;
        for (uint32_t slot_index = 0; slot_index < rate_desc.slot_count; ++slot_index) {
            stpg_rate_slot_runtime_t slot;
            err = stpg_buffer_read_rate_slot(&header, rate_desc.slot_offset + slot_index, &slot);
            if (err != ERR_NONE) return err;
            const uint16_t drive_op = slot.flags & STPG_RATE_SLOT_DRIVE_OP_MASK;
            if (drive_op > STPG_DRIVE_COMPLEMENT) return ERR_RATEVEC_BAD_HEADER;
            if ((slot.flags & ~STPG_RATE_SLOT_VALID_FLAG_MASK) != 0u) return ERR_RATEVEC_BAD_HEADER;
            if ((slot.drive_mask & ~pin_mask) != 0u || (slot.dir_mask & ~pin_mask) != 0u || (slot.sample_mask & ~pin_mask) != 0u) {
                return ERR_RATEVEC_BAD_HEADER;
            }
            if (slot.time_ticks >= rate_desc.period_ticks) {
                stpg_stream_record_rate_mismatch(
                    STPG_MISMATCH_STAGE_RATE_SLOT_EXCEEDS_PERIOD,
                    i,
                    slot_index,
                    slot.time_ticks,
                    rate_desc.period_ticks,
                    rate_desc.period_ticks,
                    prev_time_ticks,
                    slot.time_ticks);
                return ERR_RATE_PERIOD_MISMATCH;
            }
            if (slot_index > 0u && slot.time_ticks < prev_time_ticks) {
                stpg_stream_record_rate_mismatch(
                    STPG_MISMATCH_STAGE_RATE_SLOT_NON_MONOTONIC,
                    i,
                    slot_index,
                    slot.time_ticks,
                    prev_time_ticks,
                    rate_desc.period_ticks,
                    prev_time_ticks,
                    slot.time_ticks);
                return ERR_RATE_PERIOD_MISMATCH;
            }
            prev_time_ticks = slot.time_ticks;
        }
    }
    stpg_loop_desc_t *const loop_descs = stpg_buffer_loop_desc_scratch();
    uint32_t loop_block_count = 0u;
    err = stpg_buffer_parse_loop_descriptors_psram(&header, loop_descs, &loop_block_count);
    if (err != ERR_NONE) return err;

    uint32_t p4_end_marker_count = 0u;
    for (uint32_t vector_index = 0; vector_index < header.vector_count; ++vector_index) {
        VectorRecord vector;
        err = stpg_buffer_read_vector_record(&header, vector_index, &vector);
        if (err != ERR_NONE) return err;
        err = stpg_buffer_validate_vector_record_fields(&vector, header.rate_count, true, header.active_pins);
        if (err != ERR_NONE) return err;
        if (vector.flags == STPG_VECTOR_FLAG_LOOP_REF) {
            const LoopRefRecord *loop_ref = (const LoopRefRecord *)(const void *)&vector;
            if (loop_ref->loop_id >= loop_block_count) return ERR_RATEVEC_BAD_OFFSET;
        }
        if (vector.flags == STPG_VECTOR_FLAG_EXPLICIT_WAIT_P4_END) {
            if ((header.flags & STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4) == 0u) return ERR_RATEVEC_BAD_HEADER;
            p4_end_marker_count += 1u;
        }
    }
    if ((header.flags & STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4) != 0u && p4_end_marker_count != 1u) return ERR_RATEVEC_BAD_HEADER;
    if (out_header != 0) {
        *out_header = header;
    }
    return ERR_NONE;
}

static uint32_t stpg_apply_rate_slot_drive(uint32_t current_value, const stpg_rate_slot_runtime_t *slot,
                                           const VectorRecord *vector, uint32_t active_pins) {
    const uint32_t drive_mask = active_pins == STPG_ACTIVE_PINS_V11 ? (slot->drive_mask & 0x00FFFFFFu) : (slot->drive_mask & 0x0000FFFFu);
    switch (slot->flags & STPG_RATE_SLOT_DRIVE_OP_MASK) {
    case STPG_DRIVE_VECTOR:
        return (current_value & ~drive_mask) | (vector->drive_value & drive_mask);
    case STPG_DRIVE_ZERO:
        return current_value & ~drive_mask;
    case STPG_DRIVE_ONE:
        return current_value | drive_mask;
    case STPG_DRIVE_COMPLEMENT:
        return (current_value & ~drive_mask) | ((~vector->drive_value) & drive_mask);
    case STPG_DRIVE_NONE:
    default:
        return current_value;
    }
}

static uint32_t stpg_apply_group_uop_drive(uint32_t current_value, const stpg_rate_group_op_t *op,
                                           const VectorRecord *vector) {
    switch (op->drive_op) {
    case STPG_DRIVE_VECTOR:
        return (current_value & ~op->drive_mask) | (vector->drive_value & op->drive_mask);
    case STPG_DRIVE_ZERO:
        return current_value & ~op->drive_mask;
    case STPG_DRIVE_ONE:
        return current_value | op->drive_mask;
    case STPG_DRIVE_COMPLEMENT:
        return (current_value & ~op->drive_mask) | ((~vector->drive_value) & op->drive_mask);
    case STPG_DRIVE_NONE:
    default:
        return current_value;
    }
}

static inline uint32_t stpg_apply_group_uop_drive_packed(uint32_t current_value,
                                                         const stpg_rate_group_op_t *op,
                                                         uint32_t packed_vector_value) {
    switch (op->drive_op) {
    case STPG_DRIVE_VECTOR:
        return (current_value & ~op->packed_drive_mask) | (packed_vector_value & op->packed_drive_mask);
    case STPG_DRIVE_ZERO:
        return current_value & ~op->packed_drive_mask;
    case STPG_DRIVE_ONE:
        return current_value | op->packed_drive_mask;
    case STPG_DRIVE_COMPLEMENT:
        return (current_value & ~op->packed_drive_mask) | ((~packed_vector_value) & op->packed_drive_mask);
    case STPG_DRIVE_NONE:
    default:
        return current_value;
    }
}

static stpg_err_t STPG_STREAM_PRODUCER_FUNC(stpg_buffer_stream_cache_rate_slots)(
    stpg_rate_stream_state_t *state,
    uint32_t rate_id) {
    if (state == 0 || rate_id >= state->header.rate_count) {
        return ERR_BAD_SIZE;
    }
    if (state->rate_cached && state->cached_rate_id == rate_id) {
        return ERR_NONE;
    }

    if (state->rate_runtime_prepared) {
        const stpg_rate_runtime_t *runtime = &state->rate_runtime[rate_id];
        if (runtime->rate.slot_count == 0u
            || runtime->rate.slot_count > STPG_MAX_SLOTS_PER_RATE) {
            return ERR_RATEVEC_BAD_HEADER;
        }
        state->current_rate = runtime->rate;
        memcpy(state->cached_slots,
               runtime->slots,
               (size_t)runtime->rate.slot_count * sizeof(state->cached_slots[0]));
        g_stream_telemetry.rate_runtime_cache_load_count += 1u;
        g_stream_telemetry.rate_runtime_cache_slot_copy_count += runtime->rate.slot_count;
    } else {
        stpg_err_t err = stpg_buffer_read_rate_desc(
            &state->header, rate_id, &state->current_rate);
        if (err != ERR_NONE) return err;
        for (uint32_t slot_index = 0;
             slot_index < state->current_rate.slot_count;
             ++slot_index) {
            err = stpg_buffer_read_rate_slot(
                &state->header,
                state->current_rate.slot_offset + slot_index,
                &state->cached_slots[slot_index]);
            if (err != ERR_NONE) return err;
        }
    }
    state->cached_rate_id = rate_id;
    state->rate_cached = true;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_stream_load_next_vector(stpg_rate_stream_state_t *state,
                                                      bool cache_rate_slots) {
#ifdef PICO_ON_DEVICE
    const uint32_t start_us = time_us_32();
#endif
    while (true) {
        stpg_err_t err = ERR_NONE;
        if (state->loop_active) {
            const stpg_loop_desc_t *loop_desc = &state->loop_descs[state->current_loop_id];
            if (state->current_loop_body_index >= loop_desc->body_record_count) {
                state->current_loop_cycle += 1u;
                if (state->current_loop_cycle >= loop_desc->loop_count) {
                    state->loop_active = false;
                    stpg_stream_loop_body_cache_reset();
                    state->vector_index += 1u;
                    continue;
                }
                state->current_loop_body_index = 0u;
            }
            err = stpg_buffer_stream_read_loop_body_record(
                state,
                state->current_loop_id,
                state->current_loop_body_index,
                &state->current_vector);
            if (err != ERR_NONE) return err;
            if (cache_rate_slots) {
                err = stpg_buffer_stream_cache_rate_slots(state, state->current_vector.rate_id);
                if (err != ERR_NONE) return err;
            }
            state->current_loop_body_index += 1u;
        } else {
            if (state->vector_index >= state->header.vector_count) {
                state->eof = true;
                state->vector_loaded = false;
                return ERR_NONE;
            }
            err = stpg_stream_vector_cache_ensure_current(
                state,
                state->vector_index,
                STPG_STREAM_VECTOR_CACHE_RECORDS,
                0);
            if (err != ERR_NONE) return err;
            if (!stpg_stream_vector_cache_current_contains(state->vector_index)) {
                return ERR_PSRAM_BOUNDS;
            }
            const VectorRecord raw_record =
                g_stream_vector_cache[state->vector_index - g_stream_vector_cache_base_index];
            if (raw_record.flags == STPG_VECTOR_FLAG_LOOP_REF) {
                const LoopRefRecord *loop_ref = (const LoopRefRecord *)(const void *)&raw_record;
                if (loop_ref->loop_id >= state->loop_block_count) return ERR_RATEVEC_BAD_OFFSET;
                state->loop_active = true;
                state->current_loop_id = loop_ref->loop_id;
                state->current_loop_cycle = 0u;
                state->current_loop_body_index = 0u;
                continue;
            }
            state->current_vector = raw_record;
            if (cache_rate_slots) {
                err = stpg_buffer_stream_cache_rate_slots(state, state->current_vector.rate_id);
                if (err != ERR_NONE) return err;
            }
        }
        if (state->current_vector.rate_id >= state->header.rate_count) {
            return ERR_RATEVEC_BAD_HEADER;
        }
        state->repeat_index = 0u;
        state->slot_cursor = 0u;
        state->first_in_cycle = true;
        state->vector_loaded = true;
        stpg_stream_record_vector_observation(
            state,
            &state->current_vector,
            state->vector_index,
            state->repeat_index,
            &state->rate_runtime[state->current_vector.rate_id]);
        g_stream_telemetry.load_vector_call_count += 1u;
#ifdef PICO_ON_DEVICE
        g_stream_telemetry.load_vector_total_us += time_us_32() - start_us;
#endif
        return ERR_NONE;
    }
}

static stpg_err_t stpg_buffer_stream_finish_cycle(stpg_rate_stream_state_t *state) {
    if (state->slot_cursor < state->current_rate.slot_count) {
        return ERR_NONE;
    }
    state->slot_cursor = 0u;
    state->repeat_index += 1u;
    state->first_in_cycle = true;
    if (state->repeat_index < state->current_vector.repeat) {
        return ERR_NONE;
    }
    if (!state->loop_active) {
        state->vector_index += 1u;
    }
    state->vector_loaded = false;
    return ERR_NONE;
}

#if STPG_STREAM_LOOP_BATCH
#define STPG_LOOP_BATCH_MAX_EVENTS 32u
static uint32_t g_loop_batch_template_words[STPG_LOOP_BATCH_MAX_EVENTS * 4u];
#if STPG_STREAM_FAIL_STOP
/* The fail-stop producer must publish per-sample compare metadata as well as
 * output words.  Cache only the compact expected-value tuple for each bounded
 * template event; event indices are regenerated for every repeated cycle. */
static uint16_t g_loop_batch_template_compare_flags[STPG_LOOP_BATCH_MAX_EVENTS];
static uint32_t g_loop_batch_template_sample_masks[STPG_LOOP_BATCH_MAX_EVENTS];
static uint32_t g_loop_batch_template_expect_values[STPG_LOOP_BATCH_MAX_EVENTS];
#endif
static uint32_t g_loop_batch_template_events;
static uint32_t g_loop_batch_template_samples;
static uint32_t g_loop_batch_template_cursor;
static uint32_t g_loop_batch_template_expected_events;
static bool g_loop_batch_template_valid;

static void stpg_loop_batch_template_reset(void) {
#if STPG_STREAM_FAIL_STOP
    memset(g_loop_batch_template_compare_flags, 0, sizeof(g_loop_batch_template_compare_flags));
    memset(g_loop_batch_template_sample_masks, 0, sizeof(g_loop_batch_template_sample_masks));
    memset(g_loop_batch_template_expect_values, 0, sizeof(g_loop_batch_template_expect_values));
#endif
    g_loop_batch_template_events = 0u;
    g_loop_batch_template_samples = 0u;
    g_loop_batch_template_cursor = 0u;
    g_loop_batch_template_expected_events = 0u;
    g_loop_batch_template_valid = false;
}

#endif

#if STPG_STREAM_RATE_TEMPLATE_FILL && !STPG_STREAM_HOT_TELEMETRY && !STPG_STREAM_FAIL_STOP
static bool stpg_buffer_rate_template_fill_eligible(const stpg_rate_stream_state_t *state) {
    const uint32_t pins = state->header.active_pins;
    const uint32_t logical_mask = pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
    if ((pins != STPG_ACTIVE_PINS_V01 && pins != STPG_ACTIVE_PINS_V11)
        || ((state->current_value | state->current_dir) & ~logical_mask) != 0u
        || state->loop_block_count != 0u || state->loop_active
        || g_stream_compare_collecting || !state->precomputed_uops_complete
        || state->precomputed_uop_count == 0u
        || state->precomputed_uop_count > STPG_STREAM_PRECOMPUTED_UOP_CAPACITY) return false;
    for (uint32_t i = 0u; i < state->precomputed_uop_count; ++i) {
        if (!state->precomputed_group_uops[i].stable_wait_valid) return false;
    }
    return true;
}

typedef struct {
    uint32_t value_keep, value_bits, dir_keep, dir_bits, wait_word, sample_word;
} stpg_vector_group_template_t;

/* Only invariant rate/group work and vector-dependent masks are hoisted.
 * The first wait still uses the previous vector's actual period/timestamp.
 * No shared ownership, DMA, wire-format or comparison-replay change. */
static stpg_err_t STPG_STREAM_PRODUCER_FUNC(stpg_buffer_stream_fill_next_chunk_templates)(
    stpg_rate_stream_state_t *state, uint32_t *dst_words, uint32_t max_events,
    stpg_stream_chunk_result_t *out_result) {
    memset(out_result, 0, sizeof(*out_result));
    const uint32_t pins = state->header.active_pins;
    uint32_t value = stpg_stream_pack_logical_to_window_fast(state->current_value, pins);
    uint32_t dir = stpg_stream_pack_logical_to_window_fast(state->current_dir, pins);
    stpg_err_t error = ERR_NONE;
    uint32_t emitted = 0u, samples = 0u;
    while (emitted < max_events && !state->eof) {
        const bool was_loaded = state->vector_loaded;
        if (!was_loaded) {
            if (state->vector_index >= state->header.vector_count) {
                state->eof = true;
                break;
            }
            const uint32_t request_records = stpg_buffer_stream_estimate_batch_prefetch_records(
                state, max_events - emitted);
            stpg_err_t err = stpg_stream_vector_cache_ensure_current(
                state, state->vector_index, request_records, 0);
            if (err != ERR_NONE) { error = err; goto save_held_state; }
            if (!stpg_stream_vector_cache_current_contains(state->vector_index)) {
                error = ERR_PSRAM_BOUNDS; goto save_held_state;
            }
            state->current_vector = g_stream_vector_cache[
                state->vector_index - g_stream_vector_cache_base_index];
            state->vector_loaded = true;
            /* A full-cycle continuation may have repeat_index > 0. */
            g_stream_telemetry.load_vector_call_count += 1u;
        }
        const VectorRecord *vector = &state->current_vector;
        if (vector->flags == STPG_VECTOR_FLAG_LOOP_REF) { error = ERR_UNSUPPORTED; goto save_held_state; }
        if (vector->rate_id >= state->header.rate_count || vector->repeat == 0u) {
            error = ERR_RATEVEC_BAD_HEADER; goto save_held_state;
        }
        const stpg_rate_runtime_t *runtime = &state->rate_runtime[vector->rate_id];
        const uint32_t group_count = runtime->group_count;
        uint32_t group_index = state->current_group_index, repeat_index = state->repeat_index;
        if (group_count == 0u || group_count > STPG_MAX_SLOTS_PER_RATE
            || group_index >= group_count || repeat_index >= vector->repeat) {
            error = ERR_RATEVEC_BAD_HEADER; goto save_held_state;
        }
        const uint32_t vector_value = stpg_stream_pack_logical_to_window_fast(vector->drive_value, pins);
        const uint32_t vector_dir = stpg_stream_pack_logical_to_window_fast(vector->drive_enable, pins);
        stpg_vector_group_template_t templates[STPG_MAX_SLOTS_PER_RATE];
        const stpg_rate_group_uop_t *uops[STPG_MAX_SLOTS_PER_RATE];
        for (uint32_t g = 0u; g < group_count; ++g) {
            const uint8_t index = state->precomputed_uop_index[vector->rate_id][g];
            if (index >= state->precomputed_uop_count) {
                error = ERR_RATEVEC_BAD_HEADER; goto save_held_state;
            }
            const stpg_rate_group_uop_t *uop = &state->precomputed_group_uops[index];
            uops[g] = uop;
            templates[g] = (stpg_vector_group_template_t){
                .value_keep = uop->drive_keep_mask,
                .value_bits = uop->drive_one_mask | (vector_value & uop->drive_vector_mask)
                    | (~vector_value & uop->drive_complement_mask),
                .dir_keep = ~uop->packed_dir_update_mask,
                .dir_bits = vector_dir & uop->packed_dir_update_mask,
                .wait_word = uop->stable_wait_word,
                .sample_word = uop->sample_word,
            };
        }
        uint32_t wait_word = templates[group_index].wait_word;
        if (!state->have_prev_time || group_index == 0u) {
            const stpg_rate_group_uop_t *first = uops[group_index];
            const uint32_t delta = !state->have_prev_time ? first->time_ticks
                : stpg_stream_compute_first_group_delta_ticks(state, first, repeat_index, vector->repeat);
            uint32_t required_ticks = 0u;
            stpg_err_t err = stpg_batch_compute_wait_word(
                delta, first->has_sample != 0u, &wait_word, &required_ticks);
            if (err != ERR_NONE) {
                /* The reference applies drive/dir before discovering an
                 * invalid wait. Preserve that failure state and no output. */
                value = (value & templates[group_index].value_keep) | templates[group_index].value_bits;
                dir = (dir & templates[group_index].dir_keep) | templates[group_index].dir_bits;
                stpg_stream_record_rate_mismatch(
                    was_loaded || max_events - emitted < group_count
                        ? STPG_MISMATCH_STAGE_BATCH_WAIT_GENERIC : STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
                    vector->rate_id, group_index, delta, required_ticks,
                    runtime->rate.period_ticks, state->prev_time_ticks, first->time_ticks);
                error = err; goto save_held_state;
            }
        }
        const uint32_t remaining = ((uint32_t)vector->repeat - repeat_index) * group_count - group_index;
        const uint32_t room = max_events - emitted;
        const uint32_t count = remaining < room ? remaining : room;
        uint32_t *dst = &dst_words[emitted * 4u];
        for (uint32_t i = 0u; i < count; ++i) {
            const stpg_vector_group_template_t *op = &templates[group_index];
            value = (value & op->value_keep) | op->value_bits;
            dir = (dir & op->dir_keep) | op->dir_bits;
            dst[0] = wait_word;
            dst[1] = op->sample_word;
            dst[2] = value;
            dst[3] = dir;
            dst += 4u;
            samples += op->sample_word;
            if (++group_index == group_count) {
                group_index = 0u;
                ++repeat_index;
            }
            wait_word = templates[group_index].wait_word;
        }
        state->have_prev_time = true;
        state->prev_time_ticks = uops[group_index == 0u ? group_count - 1u : group_index - 1u]->time_ticks;
        state->prev_period_ticks = runtime->rate.period_ticks;
        state->current_group_index = group_index;
        state->repeat_index = repeat_index;
        emitted += count;
        if (repeat_index == vector->repeat) {
            state->repeat_index = 0u;
            state->vector_loaded = false;
            ++state->vector_index;
            g_stream_telemetry.fast_vector_count += 1u;
            state->eof = state->vector_index >= state->header.vector_count;
        }
    }
save_held_state:
    /* Pack once per chunk, unpack once on every exit, including errors after
     * prior vectors were emitted. Cache prefetch only uses header/cursors;
     * no concurrent consumer owns this producer's held values while filling. */
    state->current_value = stpg_stream_unpack_window_to_logical_fast(value, pins);
    state->current_dir = stpg_stream_unpack_window_to_logical_fast(dir, pins);
    if (error != ERR_NONE) return error;
    g_stream_emitted_event_count += emitted;
    /* Retain cache-hit totals, but update once per chunk, not per event. */
    g_stream_telemetry.precomputed_uop_cache_hit_count += emitted;
    out_result->event_count = emitted;
    out_result->sample_event_count = samples;
    out_result->word_count = emitted * 4u;
    out_result->eof = state->eof;
    return ERR_NONE;
}
#endif

/* With the opt-in template path, keep the large fallback out of the SRAM
 * wrapper. The production OFF build retains its original placement/inlining;
 * no ring/cache/heap/stack reservation is reduced to fit the experiment. */
#if STPG_STREAM_RATE_TEMPLATE_FILL && !STPG_STREAM_HOT_TELEMETRY && !STPG_STREAM_FAIL_STOP
__attribute__((noinline))
#endif
static stpg_err_t stpg_buffer_stream_fill_next_chunk_batch_legacy(stpg_rate_stream_state_t *state, uint32_t *dst_words,
                                                           uint32_t max_events, stpg_stream_chunk_result_t *out_result) {
    if (state == 0 || dst_words == 0 || out_result == 0 || max_events == 0u) {
        return ERR_BAD_SIZE;
    }

    memset(out_result, 0, sizeof(*out_result));
    if (state->eof) {
        out_result->eof = true;
        return ERR_NONE;
    }

    const uint32_t logical_mask =
        state->header.active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
    const bool pack_identity = state->header.active_pins != STPG_ACTIVE_PINS_V11;
    const bool compare_collecting = g_stream_compare_collecting;
    uint32_t local_compare_event_count = g_stream_compare_event_count;
    uint32_t local_emitted_event_count = g_stream_emitted_event_count;
    uint32_t local_sample_event_count = 0u;
    uint32_t emitted_count = 0u;
    while (emitted_count < max_events && !state->eof) {
#if STPG_STREAM_LOOP_BATCH
        if ((!compare_collecting || STPG_STREAM_FAIL_STOP) && state->loop_active && !state->vector_loaded
            && state->current_loop_body_index >= state->loop_descs[state->current_loop_id].body_record_count
            && state->current_loop_cycle == 1u && !g_loop_batch_template_valid) {
            if (g_loop_batch_template_expected_events == 0u
                || g_loop_batch_template_events != g_loop_batch_template_expected_events) {
                return ERR_BAD_STATE;
            }
            g_loop_batch_template_valid = true;
            state->current_loop_cycle = 2u;
            state->current_loop_body_index = 0u;
        }
        if ((!compare_collecting || STPG_STREAM_FAIL_STOP) && state->loop_active && g_loop_batch_template_valid
            && !state->vector_loaded && state->current_loop_cycle >= 2u) {
            const stpg_loop_desc_t *const loop_desc = &state->loop_descs[state->current_loop_id];
            if (state->current_loop_cycle >= loop_desc->loop_count) {
                state->loop_active = false;
                stpg_stream_loop_body_cache_reset();
                state->vector_index += 1u;
                continue;
            }
            const uint32_t available = g_loop_batch_template_events - g_loop_batch_template_cursor;
            const uint32_t room = max_events - emitted_count;
            const uint32_t copy_events = available < room ? available : room;
            memcpy(&dst_words[emitted_count * 4u],
                   &g_loop_batch_template_words[g_loop_batch_template_cursor * 4u],
                   (size_t)copy_events * 4u * sizeof(uint32_t));
#if STPG_STREAM_FAIL_STOP
            if (compare_collecting) {
                for (uint32_t index = 0u; index < copy_events; ++index) {
                    const uint32_t template_index = g_loop_batch_template_cursor + index;
                    if (g_loop_batch_template_words[template_index * 4u + 1u] == 0u) continue;
                    if (stpg_stream_compare_capacity_exhausted(local_compare_event_count)) {
                        return ERR_BAD_SIZE;
                    }
                    const uint32_t compare_index =
                        stpg_stream_compare_storage_index(local_compare_event_count);
                    g_pio_words[compare_index] = local_emitted_event_count + index;
                    ExecEvent *const compare_event = &g_expanded_events[compare_index];
                    compare_event->flags = g_loop_batch_template_compare_flags[template_index];
                    compare_event->sample_mask = g_loop_batch_template_sample_masks[template_index];
                    compare_event->expect_value = g_loop_batch_template_expect_values[template_index];
                    local_compare_event_count += 1u;
                }
            }
#endif
            uint32_t copy_samples = 0u;
            if (g_loop_batch_template_cursor == 0u && copy_events == g_loop_batch_template_events) {
                copy_samples = g_loop_batch_template_samples;
            } else {
                for (uint32_t index = 0u; index < copy_events; ++index) {
                    if (g_loop_batch_template_words[(g_loop_batch_template_cursor + index) * 4u + 1u] != 0u) {
                        copy_samples += 1u;
                    }
                }
            }
            local_sample_event_count += copy_samples;
            local_emitted_event_count += copy_events;
            emitted_count += copy_events;
#if STPG_STREAM_HOT_TELEMETRY
            g_stream_telemetry.emit_call_count += copy_events;
            g_stream_telemetry.sample_event_count += copy_samples;
#endif
            g_loop_batch_template_cursor += copy_events;
            if (g_loop_batch_template_cursor == g_loop_batch_template_events) {
                g_loop_batch_template_cursor = 0u;
                state->current_loop_cycle += 1u;
                if (state->current_loop_cycle == loop_desc->loop_count) {
                    state->loop_active = false;
                    stpg_stream_loop_body_cache_reset();
                    state->vector_index += 1u;
                    if (state->vector_index >= state->header.vector_count) state->eof = true;
                }
            }
            continue;
        }
#endif
        if (!state->vector_loaded && !state->loop_active) {
#ifdef PICO_ON_DEVICE
            const uint32_t lookup_start_us = time_us_32();
#endif
            const bool cache_miss = !stpg_stream_vector_cache_current_contains(state->vector_index);
#ifdef PICO_ON_DEVICE
            g_stream_telemetry.load_cache_lookup_total_us += time_us_32() - lookup_start_us;
#endif
            if (cache_miss) {
                g_stream_telemetry.load_cache_miss_count += 1u;
#ifdef PICO_ON_DEVICE
                const uint32_t prefetch_wait_start_us = time_us_32();
#endif
                const uint32_t request_records = stpg_buffer_stream_estimate_batch_prefetch_records(
                    state,
                    max_events > emitted_count ? (max_events - emitted_count) : 1u);
                stpg_err_t err = stpg_stream_vector_cache_ensure_current(
                    state, state->vector_index, request_records, 0);
                if (err != ERR_NONE) {
                    return err;
                }
                g_stream_telemetry.load_prefetch_wait_count += 1u;
#ifdef PICO_ON_DEVICE
                g_stream_telemetry.load_prefetch_wait_total_us += time_us_32() - prefetch_wait_start_us;
#endif
            }
            stpg_err_t span_err = stpg_buffer_stream_emit_batch_vector_span(
                state,
                dst_words,
                max_events,
                logical_mask,
                pack_identity,
                compare_collecting,
                &local_compare_event_count,
                &local_emitted_event_count,
                &local_sample_event_count,
                &emitted_count);
            if (span_err != ERR_NONE) {
                return span_err;
            }
            if (emitted_count >= max_events || state->eof) {
                break;
            }
        }
#ifdef PICO_ON_DEVICE
        const uint32_t emit_start_us = time_us_32();
#endif
        if (!state->vector_loaded) {
            if (state->loop_block_count != 0u) {
                stpg_err_t load_err = stpg_buffer_stream_load_next_vector(state, false);
                if (load_err != ERR_NONE) {
                    return load_err;
                }
                if (state->eof) {
                    break;
                }
            } else {
#ifdef PICO_ON_DEVICE
            const uint32_t load_start_us = time_us_32();
            const uint32_t lookup_start_us = time_us_32();
#endif
            if (state->vector_index >= state->header.vector_count) {
                state->eof = true;
                break;
            }
            const bool cache_miss = !stpg_stream_vector_cache_current_contains(state->vector_index);
#ifdef PICO_ON_DEVICE
            g_stream_telemetry.load_cache_lookup_total_us += time_us_32() - lookup_start_us;
#endif
            if (cache_miss) {
                g_stream_telemetry.load_cache_miss_count += 1u;
#ifdef PICO_ON_DEVICE
                const uint32_t prefetch_wait_start_us = time_us_32();
#endif
                const uint32_t request_records = stpg_buffer_stream_estimate_batch_prefetch_records(
                    state,
                    max_events > emitted_count ? (max_events - emitted_count) : 1u);
                stpg_err_t err = stpg_stream_vector_cache_ensure_current(
                    state, state->vector_index, request_records, 0);
                if (err != ERR_NONE) {
                    return err;
                }
                g_stream_telemetry.load_prefetch_wait_count += 1u;
#ifdef PICO_ON_DEVICE
                g_stream_telemetry.load_prefetch_wait_total_us += time_us_32() - prefetch_wait_start_us;
#endif
            } else {
                stpg_err_t err = stpg_stream_vector_cache_ensure_current(
                    state,
                    state->vector_index,
                    STPG_STREAM_VECTOR_CACHE_RECORDS,
                    0);
                if (err != ERR_NONE) {
                    return err;
                }
#ifdef STPG_PREFETCH_PHASE1_MAP_HOOK
                err = stpg_stream_vector_cache_maybe_prefetch_next(
                    state,
                    state->vector_index,
                    STPG_STREAM_VECTOR_CACHE_NEXT_RECORDS);
                if (err != ERR_NONE) {
                    return err;
                }
#endif
#ifdef STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_HOOK
                stpg_stream_vector_cache_true_inert_pad_hook();
#endif
            }
            if (!stpg_stream_vector_cache_current_contains(state->vector_index)) {
                return ERR_PSRAM_BOUNDS;
            }
#ifdef PICO_ON_DEVICE
            const uint32_t copy_start_us = time_us_32();
#endif
            state->current_vector = g_stream_vector_cache[state->vector_index - g_stream_vector_cache_base_index];
            if (state->current_vector.flags == STPG_VECTOR_FLAG_LOOP_REF) {
                return ERR_UNSUPPORTED;
            }
            if (state->current_vector.rate_id >= state->header.rate_count) {
                return ERR_RATEVEC_BAD_HEADER;
            }
            g_stream_telemetry.load_vector_copy_byte_total += (uint32_t)sizeof(state->current_vector);
#ifdef PICO_ON_DEVICE
            g_stream_telemetry.load_vector_copy_total_us += time_us_32() - copy_start_us;
            const uint32_t setup_start_us = time_us_32();
#endif
            state->repeat_index = 0u;
            state->current_group_index = 0u;
            state->vector_loaded = true;
            g_stream_telemetry.fallback_vector_count += 1u;
#ifdef PICO_ON_DEVICE
            g_stream_telemetry.load_state_setup_total_us += time_us_32() - setup_start_us;
#endif
            g_stream_telemetry.load_vector_call_count += 1u;
#ifdef PICO_ON_DEVICE
            g_stream_telemetry.load_vector_total_us += time_us_32() - load_start_us;
#endif
            }
        }

        const stpg_rate_runtime_t *runtime = &state->rate_runtime[state->current_vector.rate_id];
        if (runtime->group_count == 0u || state->current_group_index >= runtime->group_count) {
            return ERR_RATEVEC_BAD_HEADER;
        }
        const stpg_rate_group_uop_t *group_uop = 0;
        stpg_err_t group_uop_err = stpg_buffer_resolve_rate_group_uop(
            state,
            runtime,
            state->current_vector.rate_id,
            state->current_group_index,
            state->header.active_pins,
            &state->current_group_uop,
            &group_uop);
        if (group_uop_err != ERR_NONE) {
            return group_uop_err;
        }
#ifdef PICO_ON_DEVICE
        const uint32_t apply_start_us = time_us_32();
#endif
        for (uint32_t op_index = 0u; op_index < group_uop->op_count; ++op_index) {
            state->current_value = stpg_apply_group_uop_drive(state->current_value,
                                                              &group_uop->ops[op_index],
                                                              &state->current_vector);
        }
        if (group_uop->dir_update_mask != 0u) {
            state->current_dir = (state->current_dir & ~group_uop->dir_update_mask)
                | (state->current_vector.drive_enable & group_uop->dir_update_mask);
        }
        const bool sample_trigger = group_uop->has_sample != 0u;
#ifdef PICO_ON_DEVICE
        g_stream_telemetry.vector_apply_total_us += time_us_32() - apply_start_us;
#endif

        uint32_t desired_delta_ticks = 0u;
        if (!state->have_prev_time) {
            desired_delta_ticks = group_uop->time_ticks;
        } else if (state->current_group_index == 0u) {
            desired_delta_ticks = stpg_stream_compute_first_group_delta_ticks(
                state, group_uop, state->repeat_index, state->current_vector.repeat);
        } else {
            desired_delta_ticks = group_uop->gap_from_prev_ticks;
        }
        stpg_stream_record_repeat_aware_boundary(
            state,
            state->vector_index,
            state->current_vector.rate_id,
            state->repeat_index,
            state->current_vector.repeat,
            group_uop,
            desired_delta_ticks,
            false);
        uint32_t wait_word = 0u;
        uint32_t required_ticks = 0u;
        stpg_err_t wait_err = stpg_batch_compute_wait_word(
            desired_delta_ticks, sample_trigger, &wait_word, &required_ticks);
        if (wait_err != ERR_NONE) {
            stpg_stream_record_rate_mismatch(
                STPG_MISMATCH_STAGE_BATCH_WAIT_GENERIC,
                state->current_vector.rate_id,
                state->current_group_index,
                desired_delta_ticks,
                required_ticks,
                runtime->rate.period_ticks,
                state->prev_time_ticks,
                group_uop->time_ticks);
            return wait_err;
        }
        uint32_t *dst = &dst_words[emitted_count * 4u];
        dst[0] = wait_word;
        dst[1] = sample_trigger ? 1u : 0u;
        if (pack_identity) {
            dst[2] = state->current_value;
            dst[3] = state->current_dir;
        } else {
            dst[2] = stpg_stream_pack_logical_to_window_fast(state->current_value, state->header.active_pins);
            dst[3] = stpg_stream_pack_logical_to_window_fast(state->current_dir, state->header.active_pins);
        }

        if (compare_collecting) {
#ifdef PICO_ON_DEVICE
            const uint32_t compare_start_us = time_us_32();
#endif
                const uint32_t event_index = local_emitted_event_count;
                if (sample_trigger) {
                    if (stpg_stream_compare_capacity_exhausted(local_compare_event_count)) {
                        return ERR_BAD_SIZE;
                    }
                    const uint32_t effective_mask = group_uop->sample_mask_or & state->current_vector.expect_mask & logical_mask;
                    const uint32_t compare_index =
                        stpg_stream_compare_storage_index(local_compare_event_count);
                    g_pio_words[compare_index] = event_index;
                    ExecEvent *compare_event = &g_expanded_events[compare_index];
                    compare_event->flags =
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP
                        STPG_EVF_SAMPLE_POINT |
#endif
                        (effective_mask != 0u ? STPG_EVF_COMPARE_POINT : 0u);
                    compare_event->sample_mask = effective_mask;
                    compare_event->expect_value = state->current_vector.expect_value & logical_mask;
                    local_compare_event_count += 1u;
                }
            local_emitted_event_count += 1u;
#ifdef PICO_ON_DEVICE
            g_stream_telemetry.compare_bookkeeping_total_us += time_us_32() - compare_start_us;
#endif
        } else if (sample_trigger) {
            local_emitted_event_count += 1u;
        } else {
            local_emitted_event_count += 1u;
        }
        if (sample_trigger) {
            local_sample_event_count += 1u;
        }

#if STPG_STREAM_LOOP_BATCH
        if ((!compare_collecting || STPG_STREAM_FAIL_STOP)
            && state->loop_active && state->current_loop_cycle == 1u) {
            if (g_loop_batch_template_events >= STPG_LOOP_BATCH_MAX_EVENTS) return ERR_BAD_SIZE;
            memcpy(&g_loop_batch_template_words[g_loop_batch_template_events * 4u], dst,
                   4u * sizeof(uint32_t));
#if STPG_STREAM_FAIL_STOP
            const uint32_t template_index = g_loop_batch_template_events;
            if (compare_collecting && sample_trigger) {
                const uint32_t effective_mask =
                    group_uop->sample_mask_or & state->current_vector.expect_mask & logical_mask;
                uint16_t flags = STPG_EVF_SAMPLE_POINT;
                if (effective_mask != 0u) flags |= STPG_EVF_COMPARE_POINT;
                g_loop_batch_template_compare_flags[template_index] = flags;
                g_loop_batch_template_sample_masks[template_index] = effective_mask;
                g_loop_batch_template_expect_values[template_index] =
                    state->current_vector.expect_value & logical_mask;
            }
#endif
            g_loop_batch_template_events += 1u;
            if (sample_trigger) g_loop_batch_template_samples += 1u;
        }
#endif

        state->have_prev_time = true;
        state->prev_time_ticks = group_uop->time_ticks;
        state->prev_period_ticks = runtime->rate.period_ticks;
        state->current_group_index += 1u;
        if (state->current_group_index >= runtime->group_count) {
            state->current_group_index = 0u;
            state->repeat_index += 1u;
            if (state->repeat_index >= state->current_vector.repeat) {
                state->repeat_index = 0u;
                if (!state->loop_active) {
                    state->vector_index += 1u;
                }
                state->vector_loaded = false;
                if (!state->loop_active && state->vector_index >= state->header.vector_count) {
                    state->eof = true;
                }
            }
        }

#if STPG_STREAM_HOT_TELEMETRY
        g_stream_telemetry.emit_call_count += 1u;
        if (sample_trigger) {
            g_stream_telemetry.sample_event_count += 1u;
        }
#endif
#ifdef PICO_ON_DEVICE
        g_stream_telemetry.emit_total_us += time_us_32() - emit_start_us;
#endif
        emitted_count += 1u;
#if STPG_STREAM_REPEAT_BOUNDARY_REARM && STPG_STREAM_REPEAT_BLOCK_FILL
        if (emitted_count >= max_events
            && state->vector_loaded
            && stpg_buffer_stream_repeat_block_eligible(
                state,
                &state->current_vector,
                runtime,
                logical_mask,
                pack_identity,
                compare_collecting)) {
            /* A preceding vector may have consumed all but a boundary prefix
             * of this chunk.  The generic prefix leaves a repeat continuation
             * in state; re-arm the span path before the next refill so that
             * continuation is not expanded event by event. */
            state->vector_loaded = false;
        }
#endif
    }

    g_stream_compare_event_count = local_compare_event_count;
    g_stream_emitted_event_count = local_emitted_event_count;
    out_result->event_count = emitted_count;
    out_result->sample_event_count = local_sample_event_count;
    out_result->word_count = emitted_count * 4u;
    out_result->eof = state->eof;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_stream_fill_next_chunk_batch(stpg_rate_stream_state_t *state, uint32_t *dst_words,
                                                           uint32_t max_events, stpg_stream_chunk_result_t *out_result) {
#if STPG_STREAM_RATE_TEMPLATE_FILL && !STPG_STREAM_HOT_TELEMETRY && !STPG_STREAM_FAIL_STOP
    if (state != 0 && dst_words != 0 && out_result != 0 && max_events != 0u
        && stpg_buffer_rate_template_fill_eligible(state)) {
        return stpg_buffer_stream_fill_next_chunk_templates(state, dst_words, max_events, out_result);
    }
#endif
    return stpg_buffer_stream_fill_next_chunk_batch_legacy(state, dst_words, max_events, out_result);
}

static stpg_err_t stpg_buffer_stream_emit_batch_vector_span(stpg_rate_stream_state_t *state,
                                                            uint32_t *dst_words,
                                                            uint32_t max_events,
                                                            uint32_t logical_mask,
                                                            bool pack_identity,
                                                            bool compare_collecting,
                                                            uint32_t *io_compare_event_count,
                                                            uint32_t *io_emitted_event_count,
                                                            uint32_t *io_sample_event_count,
                                                            uint32_t *io_emitted_count) {
    if (state == 0 || dst_words == 0 || io_compare_event_count == 0 || io_emitted_event_count == 0
        || io_sample_event_count == 0 || io_emitted_count == 0) {
        return ERR_BAD_SIZE;
    }
    if (state->vector_loaded || state->vector_index >= state->header.vector_count) {
        return ERR_NONE;
    }
    if (state->vector_index < g_stream_vector_cache_base_index
        || state->vector_index >= (g_stream_vector_cache_base_index + g_stream_vector_cache_count)) {
        return ERR_NONE;
    }

    const uint32_t cache_offset = state->vector_index - g_stream_vector_cache_base_index;
    const uint32_t cache_available = g_stream_vector_cache_count - cache_offset;
    uint32_t emitted_count = *io_emitted_count;
    uint32_t local_compare_event_count = *io_compare_event_count;
    uint32_t local_emitted_event_count = *io_emitted_event_count;
    uint32_t local_sample_events = *io_sample_event_count;
    uint32_t processed_vectors = 0u;
    const bool identity_compare_fast = pack_identity && compare_collecting;
    if (compare_collecting && !pack_identity && state->header.active_pins == STPG_ACTIVE_PINS_V11) {
        return stpg_buffer_stream_emit_batch_vector_span_compare24(
            state,
            dst_words,
            max_events,
            logical_mask,
            io_compare_event_count,
            io_emitted_event_count,
            io_sample_event_count,
            io_emitted_count);
    }
    uint32_t packed_current_value = 0u;
    uint32_t packed_current_dir = 0u;
    if (!pack_identity) {
        packed_current_value = stpg_stream_pack_logical_to_window_fast(state->current_value, state->header.active_pins);
        packed_current_dir = stpg_stream_pack_logical_to_window_fast(state->current_dir, state->header.active_pins);
    }

#ifdef PICO_ON_DEVICE
    const uint32_t span_start_us = time_us_32();
#endif

    for (uint32_t offset = 0u; offset < cache_available && emitted_count < max_events; ++offset) {
#ifdef PICO_ON_DEVICE
        const uint32_t cache_walk_start_us = time_us_32();
#endif
        const VectorRecord *vector = &g_stream_vector_cache[cache_offset + offset];
#if STPG_STREAM_DENSE_VECTOR_FILL
        /* The span walks cache records while the state advances after each
         * completed vector.  Keep the logical index tied to the cache record
         * so the dense path can safely emit more than one vector per span. */
        const uint32_t vector_index = g_stream_vector_cache_base_index + cache_offset + offset;
#else
        const uint32_t vector_index = state->vector_index + offset;
#endif
        uint32_t packed_vector_value = 0u;
        uint32_t packed_vector_enable = 0u;
        if (vector->flags == STPG_VECTOR_FLAG_LOOP_REF) break;
        if (vector->rate_id >= state->header.rate_count || vector->repeat < 1u) {
            return ERR_RATEVEC_BAD_HEADER;
        }
#ifdef PICO_ON_DEVICE
        const uint32_t vector_read_start_us = time_us_32();
#endif
        if (!pack_identity) {
#ifdef PICO_ON_DEVICE
            const uint32_t pack_start_us = time_us_32();
#endif
            packed_vector_value = stpg_stream_pack_logical_to_window_fast(vector->drive_value, state->header.active_pins);
            packed_vector_enable = stpg_stream_pack_logical_to_window_fast(vector->drive_enable, state->header.active_pins);
#ifdef PICO_ON_DEVICE
            g_stream_telemetry.fast_pack_total_us += time_us_32() - pack_start_us;
#endif
        }
#ifdef PICO_ON_DEVICE
        const uint32_t runtime_access_start_us = time_us_32();
#endif
        const stpg_rate_runtime_t *runtime = &state->rate_runtime[vector->rate_id];
        stpg_stream_record_vector_observation(
            state,
            vector,
            vector_index,
            state->repeat_index,
            runtime);
#ifdef PICO_ON_DEVICE
        const uint32_t runtime_access_done_us = time_us_32();
        g_stream_telemetry.fast_runtime_access_total_us += runtime_access_done_us - runtime_access_start_us;
        const uint32_t capacity_check_start_us = time_us_32();
#endif
        if (runtime->group_count == 0u) {
            return ERR_RATEVEC_BAD_HEADER;
        }
        const uint32_t remaining_events = max_events - emitted_count;
        const uint32_t remaining_repeats = (uint32_t)vector->repeat - state->repeat_index;
        const uint32_t groups_total = (uint32_t)runtime->group_count * remaining_repeats;
        uint32_t repeat_count = remaining_repeats;
#if STPG_STREAM_PARTIAL_REPEAT_FAST_PATH
        if (groups_total > remaining_events) {
            repeat_count = remaining_events / (uint32_t)runtime->group_count;
            if (repeat_count == 0u) {
                break;
            }
        }
#else
        if (groups_total > remaining_events) {
            break;
        }
#endif
#ifdef PICO_ON_DEVICE
        const uint32_t vector_read_done_us = time_us_32();
        g_stream_telemetry.fast_capacity_check_total_us += vector_read_done_us - capacity_check_start_us;
        g_stream_telemetry.fast_vector_read_total_us += vector_read_done_us - vector_read_start_us;
        g_stream_telemetry.fast_cache_walk_total_us += vector_read_start_us - cache_walk_start_us;
#endif

        g_stream_telemetry.load_vector_call_count += 1u;
#if STPG_STREAM_DENSE_VECTOR_FILL && !STPG_STREAM_HOT_TELEMETRY
        bool dense_vector_emitted = false;
        stpg_err_t dense_err = stpg_buffer_stream_emit_dense_vector(
            state,
            vector,
            vector_index,
            runtime,
            dst_words,
            max_events,
            logical_mask,
            pack_identity,
            compare_collecting,
            packed_vector_value,
            packed_vector_enable,
            &local_emitted_event_count,
            &emitted_count,
            &dense_vector_emitted);
        if (dense_err != ERR_NONE) {
            return dense_err;
        }
        if (dense_vector_emitted) {
            processed_vectors += 1u;
            continue;
        }
#endif
#if STPG_STREAM_REPEAT_BOUNDARY_FILL
        bool boundary_repeat_prefix = false;
        if (state->repeat_index == 0u
            && repeat_count > 1u
            && stpg_buffer_stream_repeat_block_shape_eligible(
                vector, runtime, logical_mask, pack_identity, compare_collecting)) {
            /* The first repeat may have a cross-vector delta.  Emit only that
             * prefix through the generic path, then use the established
             * same-vector state for the remaining repeats in this chunk. */
            boundary_repeat_prefix = true;
            repeat_count = 1u;
        }
#endif
#if STPG_STREAM_REPEAT_BLOCK_FILL
        if (stpg_buffer_stream_repeat_block_eligible(
                state, vector, runtime, logical_mask, pack_identity, compare_collecting)) {
            const uint32_t block_emitted_before = emitted_count;
            bool block_completed_vector = false;
            stpg_err_t block_err = stpg_buffer_stream_emit_repeat_block(
                state,
                vector,
                vector_index,
                runtime,
                dst_words,
                max_events,
                logical_mask,
                &local_emitted_event_count,
                &emitted_count,
                &block_completed_vector);
            if (block_err != ERR_NONE) {
                return block_err;
            }
            if (emitted_count != block_emitted_before) {
                if (block_completed_vector) {
                    processed_vectors += 1u;
                }
                /* Return to the caller after one block.  This avoids making
                 * the cache-span offset depend on a state->vector_index that
                 * may have advanced during a partial final block. */
                break;
            }
        }
#endif
        const uint32_t repeat_start = state->repeat_index;
        for (uint32_t repeat_index = repeat_start;
             repeat_index < repeat_start + repeat_count;
             ++repeat_index) {
            for (uint32_t group_index = 0u; group_index < runtime->group_count; ++group_index) {
                stpg_rate_group_uop_t group_uop_storage;
                const stpg_rate_group_uop_t *group_uop = 0;
                stpg_err_t group_uop_err = stpg_buffer_resolve_rate_group_uop(
                    state,
                    runtime,
                    vector->rate_id,
                    group_index,
                    state->header.active_pins,
                    &group_uop_storage,
                    &group_uop);
                if (group_uop_err != ERR_NONE) {
                    return group_uop_err;
                }
#if STPG_STREAM_HOT_TELEMETRY
                const uint32_t previous_value = state->current_value;
                const uint32_t previous_dir = state->current_dir;
#endif
#ifdef PICO_ON_DEVICE
                const uint32_t emit_start_us = time_us_32();
                const uint32_t group_work_start_us = emit_start_us;
                const uint32_t group_apply_start_us = emit_start_us;
#endif
                for (uint32_t op_index = 0u; op_index < group_uop->op_count; ++op_index) {
                    state->current_value = stpg_apply_group_uop_drive(
                        state->current_value, &group_uop->ops[op_index], vector);
                    if (!pack_identity) {
                        packed_current_value = stpg_apply_group_uop_drive_packed(
                            packed_current_value, &group_uop->ops[op_index], packed_vector_value);
                    }
                }
                if (group_uop->dir_update_mask != 0u) {
                    state->current_dir = (state->current_dir & ~group_uop->dir_update_mask)
                        | (vector->drive_enable & group_uop->dir_update_mask);
                    if (!pack_identity) {
                        packed_current_dir = (packed_current_dir & ~group_uop->packed_dir_update_mask)
                            | (packed_vector_enable & group_uop->packed_dir_update_mask);
                    }
                }
                const bool sample_trigger = group_uop->has_sample != 0u;
#ifdef PICO_ON_DEVICE
                const uint32_t group_apply_done_us = time_us_32();
                g_stream_telemetry.fast_group_apply_total_us += group_apply_done_us - group_apply_start_us;
                const uint32_t group_bookkeeping_start_us = group_apply_done_us;
#endif

                uint32_t desired_delta_ticks = 0u;
                if (!state->have_prev_time) {
                    desired_delta_ticks = group_uop->time_ticks;
                } else if (group_index == 0u) {
                    desired_delta_ticks = stpg_stream_compute_first_group_delta_ticks(
                        state, group_uop, repeat_index, vector->repeat);
                } else {
                    desired_delta_ticks = group_uop->gap_from_prev_ticks;
                }
#if STPG_STREAM_HOT_TELEMETRY
                stpg_stream_record_repeat_aware_boundary(
                    state,
                    vector_index,
                    vector->rate_id,
                    repeat_index,
                    vector->repeat,
                    group_uop,
                    desired_delta_ticks,
                    state->current_value != previous_value || state->current_dir != previous_dir);
#endif
                uint32_t wait_word = 0u;
                uint32_t required_ticks = 0u;
                stpg_err_t wait_err = stpg_batch_compute_wait_word(
                    desired_delta_ticks, sample_trigger, &wait_word, &required_ticks);
                if (wait_err != ERR_NONE) {
                    stpg_stream_record_rate_mismatch(
                        STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
                        vector->rate_id,
                        group_index,
                        desired_delta_ticks,
                        required_ticks,
                        runtime->rate.period_ticks,
                        state->prev_time_ticks,
                        group_uop->time_ticks);
                    return wait_err;
                }

#ifdef PICO_ON_DEVICE
                const uint32_t store_select_start_us = time_us_32();
#endif
                uint32_t *dst = &dst_words[emitted_count * 4u];
                uint32_t packed_value = state->current_value;
                uint32_t packed_dir = state->current_dir;
                if (!identity_compare_fast && !pack_identity) {
                    packed_value = packed_current_value;
                    packed_dir = packed_current_dir;
                }
                dst[0] = wait_word;
                dst[1] = sample_trigger ? 1u : 0u;
                dst[2] = packed_value;
                dst[3] = packed_dir;
#ifdef PICO_ON_DEVICE
                g_stream_telemetry.fast_store_select_total_us += time_us_32() - store_select_start_us;
#endif

                if (identity_compare_fast) {
#ifdef PICO_ON_DEVICE
                    const uint32_t compare_write_start_us = time_us_32();
#endif
                    const uint32_t event_index = local_emitted_event_count;
                    if (sample_trigger) {
                        if (stpg_stream_compare_capacity_exhausted(local_compare_event_count)) {
                            return ERR_BAD_SIZE;
                        }
                        const uint32_t effective_mask =
                            group_uop->sample_mask_or & vector->expect_mask & logical_mask;
                        const uint32_t compare_index =
                            stpg_stream_compare_storage_index(local_compare_event_count);
                        g_pio_words[compare_index] = event_index;
                        ExecEvent *compare_event = &g_expanded_events[compare_index];
                        compare_event->flags =
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP
                            STPG_EVF_SAMPLE_POINT |
#endif
                            (effective_mask != 0u ? STPG_EVF_COMPARE_POINT : 0u);
                        compare_event->sample_mask = effective_mask;
                        compare_event->expect_value = vector->expect_value & logical_mask;
                        local_compare_event_count += 1u;
#if STPG_STREAM_HOT_TELEMETRY
                        g_stream_telemetry.fast_compare_sample_count += 1u;
#endif
                    }
                    local_emitted_event_count += 1u;
#ifdef PICO_ON_DEVICE
                    g_stream_telemetry.fast_compare_write_total_us += time_us_32() - compare_write_start_us;
#endif
                } else if (compare_collecting) {
#ifdef PICO_ON_DEVICE
                    const uint32_t compare_write_start_us = time_us_32();
#endif
                    const uint32_t event_index = local_emitted_event_count;
                    if (sample_trigger) {
                        if (stpg_stream_compare_capacity_exhausted(local_compare_event_count)) {
                            return ERR_BAD_SIZE;
                        }
                        const uint32_t effective_mask =
                            group_uop->sample_mask_or & vector->expect_mask & logical_mask;
                        const uint32_t compare_index =
                            stpg_stream_compare_storage_index(local_compare_event_count);
                        g_pio_words[compare_index] = event_index;
                        ExecEvent *compare_event = &g_expanded_events[compare_index];
                        compare_event->flags =
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP
                            STPG_EVF_SAMPLE_POINT |
#endif
                            (effective_mask != 0u ? STPG_EVF_COMPARE_POINT : 0u);
                        compare_event->sample_mask = effective_mask;
                        compare_event->expect_value = vector->expect_value & logical_mask;
                        local_compare_event_count += 1u;
#if STPG_STREAM_HOT_TELEMETRY
                        g_stream_telemetry.fast_compare_sample_count += 1u;
#endif
                    }
                    local_emitted_event_count += 1u;
#ifdef PICO_ON_DEVICE
                    g_stream_telemetry.fast_compare_write_total_us += time_us_32() - compare_write_start_us;
#endif
                } else {
                    local_emitted_event_count += 1u;
                }
                if (sample_trigger) {
                    local_sample_events += 1u;
                }
#ifdef PICO_ON_DEVICE
                const uint32_t state_commit_start_us = time_us_32();
#endif
                state->have_prev_time = true;
                state->prev_time_ticks = group_uop->time_ticks;
                state->prev_period_ticks = runtime->rate.period_ticks;
#ifdef PICO_ON_DEVICE
                g_stream_telemetry.fast_state_commit_total_us += time_us_32() - state_commit_start_us;
                const uint32_t counter_update_start_us = time_us_32();
#endif
#if STPG_STREAM_HOT_TELEMETRY
                g_stream_telemetry.emit_call_count += 1u;
                if (sample_trigger) {
                    g_stream_telemetry.sample_event_count += 1u;
                }
                g_stream_telemetry.fast_group_emit_count += 1u;
#endif
#ifdef PICO_ON_DEVICE
                g_stream_telemetry.fast_counter_update_total_us += time_us_32() - counter_update_start_us;
                const uint32_t loop_bookkeeping_start_us = time_us_32();
#endif
#ifdef PICO_ON_DEVICE
                g_stream_telemetry.fast_group_bookkeeping_total_us += time_us_32() - group_bookkeeping_start_us;
                g_stream_telemetry.fast_loop_bookkeeping_total_us += time_us_32() - loop_bookkeeping_start_us;
                g_stream_telemetry.fast_group_work_total_us += time_us_32() - group_work_start_us;
                g_stream_telemetry.emit_total_us += time_us_32() - emit_start_us;
#endif
                emitted_count += 1u;
            }
        }

#if STPG_STREAM_PARTIAL_REPEAT_FAST_PATH
        const uint32_t repeat_end = repeat_start + repeat_count;
        if (repeat_end < vector->repeat) {
            state->repeat_index = repeat_end;
            state->current_group_index = 0u;
            /* Keep the vector loaded only when a sub-group-sized remainder
             * must be emitted by the generic path in this same chunk. */
            if (emitted_count < max_events) {
                state->current_vector = *vector;
                state->vector_loaded = true;
            }
#if STPG_STREAM_REPEAT_BOUNDARY_FILL
            if (boundary_repeat_prefix && emitted_count < max_events
                && stpg_buffer_stream_repeat_block_eligible(
                    state, vector, runtime, logical_mask, pack_identity, compare_collecting)) {
                bool block_completed_vector = false;
                stpg_err_t block_err = stpg_buffer_stream_emit_repeat_block(
                    state,
                    vector,
                    vector_index,
                    runtime,
                    dst_words,
                    max_events,
                    logical_mask,
                    &local_emitted_event_count,
                    &emitted_count,
                    &block_completed_vector);
                if (block_err != ERR_NONE) {
                    return block_err;
                }
                if (block_completed_vector) {
                    processed_vectors += 1u;
                }
#if STPG_STREAM_REPEAT_BOUNDARY_CONTINUE
                if (!block_completed_vector && emitted_count >= max_events) {
                    /* The boundary prefix has already been emitted.  If the
                     * repeat block filled this chunk, leave the next chunk
                     * to the span path instead of replaying the generic
                     * per-event loop with vector_loaded still set. */
                    state->vector_loaded = false;
                }
#endif
            }
#endif
            *io_compare_event_count = local_compare_event_count;
            *io_emitted_event_count = local_emitted_event_count;
            *io_sample_event_count = local_sample_events;
            *io_emitted_count = emitted_count;
            return ERR_NONE;
        }
#endif
        state->repeat_index = 0u;
        processed_vectors += 1u;
        g_stream_telemetry.fast_vector_count += 1u;
#if STPG_STREAM_DENSE_VECTOR_FILL
        state->vector_index = vector_index + 1u;
#else
        state->vector_index += 1u;
#endif
        if (state->vector_index >= state->header.vector_count) {
            state->eof = true;
            break;
        }
    }

    if (processed_vectors > 0u) {
        g_stream_telemetry.fast_chunk_cache_span_count += 1u;
        g_stream_telemetry.fast_chunk_cache_span_vectors += processed_vectors;
    }
#ifdef PICO_ON_DEVICE
    g_stream_telemetry.load_vector_total_us += time_us_32() - span_start_us;
#endif
    *io_compare_event_count = local_compare_event_count;
    *io_emitted_event_count = local_emitted_event_count;
    *io_sample_event_count = local_sample_events;
    *io_emitted_count = emitted_count;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_stream_emit_batch_vector_span_compare24(stpg_rate_stream_state_t *state,
                                                                      uint32_t *dst_words,
                                                                      uint32_t max_events,
                                                                      uint32_t logical_mask,
                                                                      uint32_t *io_compare_event_count,
                                                                      uint32_t *io_emitted_event_count,
                                                                      uint32_t *io_sample_event_count,
                                                                      uint32_t *io_emitted_count) {
    if (state == 0 || dst_words == 0 || io_compare_event_count == 0 || io_emitted_event_count == 0
        || io_sample_event_count == 0 || io_emitted_count == 0) {
        return ERR_BAD_SIZE;
    }
    if (state->vector_loaded || state->vector_index >= state->header.vector_count) {
        return ERR_NONE;
    }
    if (state->vector_index < g_stream_vector_cache_base_index
        || state->vector_index >= (g_stream_vector_cache_base_index + g_stream_vector_cache_count)) {
        return ERR_NONE;
    }

    const uint32_t cache_offset = state->vector_index - g_stream_vector_cache_base_index;
    const uint32_t cache_available = g_stream_vector_cache_count - cache_offset;
    uint32_t emitted_count = *io_emitted_count;
    uint32_t *dst_cursor = &dst_words[emitted_count * 4u];
    uint32_t local_compare_event_count = *io_compare_event_count;
    uint32_t local_emitted_event_count = *io_emitted_event_count;
    uint32_t local_sample_events = *io_sample_event_count;
    uint32_t processed_vectors = 0u;
    uint32_t packed_current_value =
        stpg_stream_pack_logical_to_window_fast(state->current_value, state->header.active_pins);
    uint32_t packed_current_dir =
        stpg_stream_pack_logical_to_window_fast(state->current_dir, state->header.active_pins);

#ifdef PICO_ON_DEVICE
    const uint32_t span_start_us = time_us_32();
#endif

    for (uint32_t offset = 0u; offset < cache_available && emitted_count < max_events; ++offset) {
#ifdef PICO_ON_DEVICE
        const uint32_t cache_walk_start_us = time_us_32();
#endif
        const VectorRecord *vector = &g_stream_vector_cache[cache_offset + offset];
        if (vector->flags == STPG_VECTOR_FLAG_LOOP_REF) break;
        if (vector->rate_id >= state->header.rate_count || vector->repeat < 1u) {
            return ERR_RATEVEC_BAD_HEADER;
        }
        const uint32_t vector_effective_expect_mask = vector->expect_mask & logical_mask;
        const uint32_t vector_expect_value_masked = vector->expect_value & logical_mask;
#ifdef PICO_ON_DEVICE
        const uint32_t vector_read_start_us = time_us_32();
        const uint32_t pack_start_us = vector_read_start_us;
#endif
        const uint32_t packed_vector_value =
            stpg_stream_pack_logical_to_window_fast(vector->drive_value, state->header.active_pins);
        const uint32_t packed_vector_enable =
            stpg_stream_pack_logical_to_window_fast(vector->drive_enable, state->header.active_pins);
        const uint32_t vector_index = state->vector_index;
#ifdef PICO_ON_DEVICE
        g_stream_telemetry.fast_pack_total_us += time_us_32() - pack_start_us;
        const uint32_t runtime_access_start_us = time_us_32();
#endif
        const stpg_rate_runtime_t *runtime = &state->rate_runtime[vector->rate_id];
        stpg_stream_record_vector_observation(
            state,
            vector,
            vector_index,
            0u,
            runtime);
#ifdef PICO_ON_DEVICE
        const uint32_t runtime_access_done_us = time_us_32();
        g_stream_telemetry.fast_runtime_access_total_us += runtime_access_done_us - runtime_access_start_us;
        const uint32_t capacity_check_start_us = runtime_access_done_us;
#endif
        if (runtime->group_count == 0u) {
            return ERR_RATEVEC_BAD_HEADER;
        }
        const uint32_t groups_total = (uint32_t)runtime->group_count * (uint32_t)vector->repeat;
        if (groups_total > (max_events - emitted_count)) {
            break;
        }
#ifdef PICO_ON_DEVICE
        const uint32_t vector_read_done_us = time_us_32();
        g_stream_telemetry.fast_capacity_check_total_us += vector_read_done_us - capacity_check_start_us;
        g_stream_telemetry.fast_vector_read_total_us += vector_read_done_us - vector_read_start_us;
        g_stream_telemetry.fast_cache_walk_total_us += vector_read_start_us - cache_walk_start_us;
#endif

        g_stream_telemetry.load_vector_call_count += 1u;
        uint32_t local_emit_calls = 0u;
        uint32_t vector_sample_events = 0u;
        uint32_t local_fast_group_emit_count = 0u;

        stpg_rate_group_uop_t first_group_uop_storage;
        stpg_rate_group_uop_t second_group_uop_storage;
        const stpg_rate_group_uop_t *first_group_uop = 0;
        stpg_err_t first_group_uop_err = stpg_buffer_resolve_rate_group_uop(
            state,
            runtime,
            vector->rate_id,
            0u,
            state->header.active_pins,
            &first_group_uop_storage,
            &first_group_uop);
        if (first_group_uop_err != ERR_NONE) {
            return first_group_uop_err;
        }
        const stpg_rate_group_uop_t *second_group_uop = 0;
        if (runtime->group_count >= 2u) {
            stpg_err_t second_group_uop_err = stpg_buffer_resolve_rate_group_uop(
                state,
                runtime,
                vector->rate_id,
                1u,
                state->header.active_pins,
                &second_group_uop_storage,
                &second_group_uop);
            if (second_group_uop_err != ERR_NONE) {
                return second_group_uop_err;
            }
        }
        const bool practical_two_group_compare_shape = runtime->group_count == 2u && second_group_uop != 0
            && first_group_uop->has_sample == 0u && second_group_uop->has_sample != 0u
            && first_group_uop->time_ticks == 0u;

        for (uint32_t repeat_index = 0u; repeat_index < vector->repeat; ++repeat_index) {
            uint32_t first_delta_ticks = first_group_uop->time_ticks;
            if (state->have_prev_time) {
                /* practical_two_group_compare_shape guarantees the first group
                 * is the cycle-start group (time_ticks == 0) on every repeat,
                 * so have_prev_time here means a real wrap from the previous
                 * terminal sample time. Use the same wrap-delta form already
                 * used by legacy first-in-cycle paths instead of the compiled
                 * cycle-start gap. */
                first_delta_ticks = stpg_stream_compute_first_group_delta_ticks(
                    state, first_group_uop, repeat_index, vector->repeat);
            }
            if (practical_two_group_compare_shape) {
#if STPG_STREAM_HOT_TELEMETRY
                const uint32_t previous_first_packed_value = packed_current_value;
                const uint32_t previous_first_packed_dir = packed_current_dir;
#endif
                uint32_t first_value = state->current_value;
                uint32_t first_dir = state->current_dir;
                uint32_t first_packed_value = packed_current_value;
                uint32_t first_packed_dir = packed_current_dir;
#ifdef PICO_ON_DEVICE
                const uint32_t first_emit_start_us = time_us_32();
                const uint32_t first_group_work_start_us = first_emit_start_us;
                const uint32_t first_group_apply_start_us = first_emit_start_us;
#endif
                for (uint32_t op_index = 0u; op_index < first_group_uop->op_count; ++op_index) {
                    first_value =
                        stpg_apply_group_uop_drive(first_value, &first_group_uop->ops[op_index], vector);
                    first_packed_value = stpg_apply_group_uop_drive_packed(
                        first_packed_value, &first_group_uop->ops[op_index], packed_vector_value);
                }
                if (first_group_uop->dir_update_mask != 0u) {
                    first_dir = (first_dir & ~first_group_uop->dir_update_mask)
                        | (vector->drive_enable & first_group_uop->dir_update_mask);
                    first_packed_dir = (first_packed_dir & ~first_group_uop->packed_dir_update_mask)
                        | (packed_vector_enable & first_group_uop->packed_dir_update_mask);
                }
#if STPG_STREAM_HOT_TELEMETRY
                const bool first_output_changed = first_packed_value != previous_first_packed_value
                    || first_packed_dir != previous_first_packed_dir;
                stpg_stream_record_repeat_aware_boundary(
                    state,
                    vector_index,
                    vector->rate_id,
                    repeat_index,
                    vector->repeat,
                    first_group_uop,
                    first_delta_ticks,
                    first_output_changed);
                stpg_stream_record_vector_emission(
                    vector_index,
                    repeat_index,
                    first_delta_ticks,
                    first_output_changed);
#endif
#ifdef PICO_ON_DEVICE
                const uint32_t first_group_apply_done_us = time_us_32();
                g_stream_telemetry.fast_group_apply_total_us +=
                    first_group_apply_done_us - first_group_apply_start_us;
                const uint32_t first_group_bookkeeping_start_us = first_group_apply_done_us;
#endif
                uint32_t wait_word = 0u;
                uint32_t required_ticks = 0u;
                stpg_err_t wait_err =
                    stpg_batch_compute_wait_word(first_delta_ticks, false, &wait_word, &required_ticks);
                if (wait_err != ERR_NONE) {
                    stpg_stream_record_rate_mismatch(
                        STPG_MISMATCH_STAGE_BATCH_WAIT_COMPARE24_FIRST,
                        vector->rate_id,
                        0u,
                        first_delta_ticks,
                        required_ticks,
                        runtime->rate.period_ticks,
                        state->prev_time_ticks,
                        first_group_uop->time_ticks);
                    stpg_stream_record_compare24_mismatch_detail(
                        STPG_MISMATCH_DETAIL_COMPARE24_PRACTICAL_TWO_GROUP
                            | (state->have_prev_time ? STPG_MISMATCH_DETAIL_COMPARE24_HAVE_PREV_TIME : 0u),
                        runtime->group_count,
                        first_group_uop->gap_from_cycle_start_ticks,
                        first_group_uop->gap_from_prev_ticks);
                    return wait_err;
                }
#ifdef PICO_ON_DEVICE
                const uint32_t first_store_select_start_us = time_us_32();
#endif
                uint32_t *dst = dst_cursor;
                dst[0] = wait_word;
                dst[1] = first_group_uop->sample_word;
                dst[2] = first_packed_value;
                dst[3] = first_packed_dir;
                dst_cursor += 4u;
#ifdef PICO_ON_DEVICE
                const uint32_t first_store_select_done_us = time_us_32();
                g_stream_telemetry.fast_store_select_total_us +=
                    first_store_select_done_us - first_store_select_start_us;
                g_stream_telemetry.practical_group_apply_total_us +=
                    first_group_apply_done_us - first_group_apply_start_us;
                g_stream_telemetry.practical_wait_store_total_us +=
                    first_store_select_done_us - first_group_bookkeeping_start_us;
                const uint32_t first_compare_write_start_us = first_store_select_done_us;
                const uint32_t first_compare_write_done_us = time_us_32();
                g_stream_telemetry.fast_compare_write_total_us +=
                    first_compare_write_done_us - first_compare_write_start_us;
                g_stream_telemetry.practical_compare_write_total_us +=
                    first_compare_write_done_us - first_compare_write_start_us;
                g_stream_telemetry.fast_group_bookkeeping_total_us +=
                    first_compare_write_done_us - first_group_bookkeeping_start_us;
                g_stream_telemetry.fast_group_work_total_us +=
                    first_compare_write_done_us - first_group_work_start_us;
                g_stream_telemetry.emit_total_us += first_compare_write_done_us - first_emit_start_us;
#endif
                const uint32_t second_event_index = local_emitted_event_count + 1u;
                emitted_count += 1u;

                uint32_t second_value = first_value;
                uint32_t second_dir = first_dir;
                uint32_t second_packed_value = first_packed_value;
                uint32_t second_packed_dir = first_packed_dir;

#ifdef PICO_ON_DEVICE
                const uint32_t second_emit_start_us = time_us_32();
                const uint32_t second_group_work_start_us = second_emit_start_us;
                const uint32_t second_group_apply_start_us = second_emit_start_us;
#endif
                for (uint32_t op_index = 0u; op_index < second_group_uop->op_count; ++op_index) {
                    second_value =
                        stpg_apply_group_uop_drive(second_value, &second_group_uop->ops[op_index], vector);
                    second_packed_value = stpg_apply_group_uop_drive_packed(
                        second_packed_value, &second_group_uop->ops[op_index], packed_vector_value);
                }
                if (second_group_uop->dir_update_mask != 0u) {
                    second_dir = (second_dir & ~second_group_uop->dir_update_mask)
                        | (vector->drive_enable & second_group_uop->dir_update_mask);
                    second_packed_dir = (second_packed_dir & ~second_group_uop->packed_dir_update_mask)
                        | (packed_vector_enable & second_group_uop->packed_dir_update_mask);
                }
#ifdef PICO_ON_DEVICE
                const uint32_t second_group_apply_done_us = time_us_32();
                g_stream_telemetry.fast_group_apply_total_us +=
                    second_group_apply_done_us - second_group_apply_start_us;
                const uint32_t second_group_bookkeeping_start_us = second_group_apply_done_us;
#endif
                wait_word = 0u;
                required_ticks = 0u;
                wait_err = stpg_batch_compute_wait_word(
                    second_group_uop->gap_from_prev_ticks, true, &wait_word, &required_ticks);
                if (wait_err != ERR_NONE) {
                    stpg_stream_record_rate_mismatch(
                        STPG_MISMATCH_STAGE_BATCH_WAIT_COMPARE24_GROUP,
                        vector->rate_id,
                        1u,
                        second_group_uop->gap_from_prev_ticks,
                        required_ticks,
                        runtime->rate.period_ticks,
                        state->prev_time_ticks,
                        second_group_uop->time_ticks);
                    return wait_err;
                }
#ifdef PICO_ON_DEVICE
                const uint32_t second_store_select_start_us = time_us_32();
#endif
                dst = dst_cursor;
                dst[0] = wait_word;
                dst[1] = second_group_uop->sample_word;
                dst[2] = second_packed_value;
                dst[3] = second_packed_dir;
                dst_cursor += 4u;
#ifdef PICO_ON_DEVICE
                const uint32_t second_store_select_done_us = time_us_32();
                g_stream_telemetry.fast_store_select_total_us +=
                    second_store_select_done_us - second_store_select_start_us;
                g_stream_telemetry.practical_group_apply_total_us +=
                    second_group_apply_done_us - second_group_apply_start_us;
                g_stream_telemetry.practical_wait_store_total_us +=
                    second_store_select_done_us - second_group_bookkeeping_start_us;
                const uint32_t second_compare_write_start_us = second_store_select_done_us;
#endif
                if (local_compare_event_count >= STPG_MAX_EVENT_COUNT) {
                    return ERR_BAD_SIZE;
                }
                stpg_stream_write_compare_event_fast(
                    local_compare_event_count,
                    second_event_index,
                    second_group_uop->sample_mask_or,
                    vector_effective_expect_mask,
                    vector_expect_value_masked);
                local_compare_event_count += 1u;
#if STPG_STREAM_HOT_TELEMETRY
                g_stream_telemetry.fast_compare_sample_count +=
                    (second_group_uop->group_flags & STPG_GROUP_FLAG_COMPARE_CANDIDATE) != 0u;
#endif
#ifdef PICO_ON_DEVICE
                const uint32_t second_compare_write_done_us = time_us_32();
                g_stream_telemetry.fast_compare_write_total_us +=
                    second_compare_write_done_us - second_compare_write_start_us;
                g_stream_telemetry.practical_compare_write_total_us +=
                    second_compare_write_done_us - second_compare_write_start_us;
                const uint32_t second_state_commit_start_us = second_compare_write_done_us;
#endif
                state->current_value = second_value;
                state->current_dir = second_dir;
                packed_current_value = second_packed_value;
                packed_current_dir = second_packed_dir;
                state->have_prev_time = true;
                state->prev_time_ticks = second_group_uop->time_ticks;
                state->prev_period_ticks = runtime->rate.period_ticks;
                local_emitted_event_count += 2u;
                local_sample_events += 1u;
                vector_sample_events += 1u;
#ifdef PICO_ON_DEVICE
                g_stream_telemetry.fast_state_commit_total_us += time_us_32() - second_state_commit_start_us;
                const uint32_t second_counter_update_start_us = time_us_32();
#endif
                local_emit_calls += 2u;
                local_fast_group_emit_count += 2u;
#ifdef PICO_ON_DEVICE
                g_stream_telemetry.fast_counter_update_total_us += time_us_32() - second_counter_update_start_us;
                const uint32_t second_loop_bookkeeping_start_us = time_us_32();
                const uint32_t second_loop_bookkeeping_done_us = time_us_32();
                g_stream_telemetry.fast_group_bookkeeping_total_us +=
                    second_loop_bookkeeping_done_us - second_group_bookkeeping_start_us;
                g_stream_telemetry.fast_loop_bookkeeping_total_us +=
                    second_loop_bookkeeping_done_us - second_loop_bookkeeping_start_us;
                g_stream_telemetry.practical_commit_control_total_us +=
                    second_loop_bookkeeping_done_us - second_state_commit_start_us;
                g_stream_telemetry.fast_group_work_total_us += second_loop_bookkeeping_done_us - second_group_work_start_us;
                g_stream_telemetry.emit_total_us += second_loop_bookkeeping_done_us - second_emit_start_us;
#endif
                emitted_count += 1u;
                continue;
            }
            {
#if STPG_STREAM_HOT_TELEMETRY
                const uint32_t previous_first_packed_value = packed_current_value;
                const uint32_t previous_first_packed_dir = packed_current_dir;
#endif
#ifdef PICO_ON_DEVICE
                const uint32_t emit_start_us = time_us_32();
                const uint32_t group_work_start_us = emit_start_us;
                const uint32_t group_apply_start_us = emit_start_us;
#endif
                for (uint32_t op_index = 0u; op_index < first_group_uop->op_count; ++op_index) {
                    state->current_value =
                        stpg_apply_group_uop_drive(state->current_value, &first_group_uop->ops[op_index], vector);
                    packed_current_value = stpg_apply_group_uop_drive_packed(
                        packed_current_value, &first_group_uop->ops[op_index], packed_vector_value);
                }
                if (first_group_uop->dir_update_mask != 0u) {
                    state->current_dir = (state->current_dir & ~first_group_uop->dir_update_mask)
                        | (vector->drive_enable & first_group_uop->dir_update_mask);
                    packed_current_dir = (packed_current_dir & ~first_group_uop->packed_dir_update_mask)
                        | (packed_vector_enable & first_group_uop->packed_dir_update_mask);
                }
#if STPG_STREAM_HOT_TELEMETRY
                const bool first_output_changed = packed_current_value != previous_first_packed_value
                    || packed_current_dir != previous_first_packed_dir;
                stpg_stream_record_repeat_aware_boundary(
                    state,
                    vector_index,
                    vector->rate_id,
                    repeat_index,
                    vector->repeat,
                    first_group_uop,
                    first_delta_ticks,
                    first_output_changed);
                stpg_stream_record_vector_emission(
                    vector_index,
                    repeat_index,
                    first_delta_ticks,
                    first_output_changed);
#endif
#ifdef PICO_ON_DEVICE
                const uint32_t group_apply_done_us = time_us_32();
                g_stream_telemetry.fast_group_apply_total_us += group_apply_done_us - group_apply_start_us;
                const uint32_t group_bookkeeping_start_us = group_apply_done_us;
#endif
                uint32_t wait_word = 0u;
                uint32_t required_ticks = 0u;
                stpg_err_t wait_err =
                    stpg_batch_compute_wait_word(first_delta_ticks, first_group_uop->has_sample != 0u, &wait_word, &required_ticks);
                if (wait_err != ERR_NONE) {
                    stpg_stream_record_rate_mismatch(
                        STPG_MISMATCH_STAGE_BATCH_WAIT_COMPARE24_FIRST,
                        vector->rate_id,
                        0u,
                        first_delta_ticks,
                        required_ticks,
                        runtime->rate.period_ticks,
                        state->prev_time_ticks,
                        first_group_uop->time_ticks);
                    stpg_stream_record_compare24_mismatch_detail(
                        (first_group_uop->has_sample != 0u ? STPG_MISMATCH_DETAIL_COMPARE24_EFFECTIVE_SAMPLE_TRIGGER : 0u)
                            | (state->have_prev_time ? STPG_MISMATCH_DETAIL_COMPARE24_HAVE_PREV_TIME : 0u),
                        runtime->group_count,
                        first_group_uop->gap_from_cycle_start_ticks,
                        first_group_uop->gap_from_prev_ticks);
                    return wait_err;
                }
#ifdef PICO_ON_DEVICE
                const uint32_t store_select_start_us = time_us_32();
#endif
                uint32_t *dst = dst_cursor;
                dst[0] = wait_word;
                dst[1] = first_group_uop->sample_word;
                dst[2] = packed_current_value;
                dst[3] = packed_current_dir;
                dst_cursor += 4u;
#ifdef PICO_ON_DEVICE
                const uint32_t store_select_done_us = time_us_32();
                g_stream_telemetry.fast_store_select_total_us += store_select_done_us - store_select_start_us;
                g_stream_telemetry.practical_group_apply_total_us += group_apply_done_us - group_apply_start_us;
                g_stream_telemetry.practical_wait_store_total_us +=
                    store_select_done_us - group_bookkeeping_start_us;
                const uint32_t compare_write_start_us = store_select_done_us;
#endif
                if (first_group_uop->has_sample != 0u) {
                    if (local_compare_event_count >= STPG_MAX_EVENT_COUNT) {
                        return ERR_BAD_SIZE;
                    }
                    stpg_stream_write_compare_event_fast(
                        local_compare_event_count,
                        local_emitted_event_count,
                        first_group_uop->sample_mask_or,
                        vector_effective_expect_mask,
                        vector_expect_value_masked);
                    local_compare_event_count += 1u;
#if STPG_STREAM_HOT_TELEMETRY
                    g_stream_telemetry.fast_compare_sample_count +=
                        (first_group_uop->group_flags & STPG_GROUP_FLAG_COMPARE_CANDIDATE) != 0u;
#endif
                    local_sample_events += 1u;
                    vector_sample_events += 1u;
                }
                local_emitted_event_count += 1u;
#ifdef PICO_ON_DEVICE
                const uint32_t compare_write_done_us = time_us_32();
                g_stream_telemetry.fast_compare_write_total_us += compare_write_done_us - compare_write_start_us;
                g_stream_telemetry.practical_compare_write_total_us +=
                    compare_write_done_us - compare_write_start_us;
                const uint32_t state_commit_start_us = compare_write_done_us;
#endif
                state->have_prev_time = true;
                state->prev_time_ticks = first_group_uop->time_ticks;
                state->prev_period_ticks = runtime->rate.period_ticks;
#ifdef PICO_ON_DEVICE
                g_stream_telemetry.fast_state_commit_total_us += time_us_32() - state_commit_start_us;
                const uint32_t counter_update_start_us = time_us_32();
#endif
                local_emit_calls += 1u;
                local_fast_group_emit_count += 1u;
#ifdef PICO_ON_DEVICE
                g_stream_telemetry.fast_counter_update_total_us += time_us_32() - counter_update_start_us;
                const uint32_t loop_bookkeeping_start_us = time_us_32();
                const uint32_t loop_bookkeeping_done_us = time_us_32();
                g_stream_telemetry.fast_group_bookkeeping_total_us +=
                    loop_bookkeeping_done_us - group_bookkeeping_start_us;
                g_stream_telemetry.fast_loop_bookkeeping_total_us +=
                    loop_bookkeeping_done_us - loop_bookkeeping_start_us;
                g_stream_telemetry.practical_commit_control_total_us +=
                    loop_bookkeeping_done_us - state_commit_start_us;
                g_stream_telemetry.fast_group_work_total_us += loop_bookkeeping_done_us - group_work_start_us;
                g_stream_telemetry.emit_total_us += loop_bookkeeping_done_us - emit_start_us;
#endif
                emitted_count += 1u;
            }
            for (uint32_t group_index = 1u; group_index < runtime->group_count; ++group_index) {
                stpg_rate_group_uop_t group_uop_storage;
                const stpg_rate_group_uop_t *group_uop = 0;
                stpg_err_t group_uop_err = stpg_buffer_resolve_rate_group_uop(
                    state,
                    runtime,
                    vector->rate_id,
                    group_index,
                    state->header.active_pins,
                    &group_uop_storage,
                    &group_uop);
                if (group_uop_err != ERR_NONE) {
                    return group_uop_err;
                }
                {
#ifdef PICO_ON_DEVICE
                    const uint32_t emit_start_us = time_us_32();
                    const uint32_t group_work_start_us = emit_start_us;
                    const uint32_t group_apply_start_us = emit_start_us;
#endif
                    for (uint32_t op_index = 0u; op_index < group_uop->op_count; ++op_index) {
                        state->current_value =
                            stpg_apply_group_uop_drive(state->current_value, &group_uop->ops[op_index], vector);
                        packed_current_value = stpg_apply_group_uop_drive_packed(
                            packed_current_value, &group_uop->ops[op_index], packed_vector_value);
                    }
                    if (group_uop->dir_update_mask != 0u) {
                        state->current_dir = (state->current_dir & ~group_uop->dir_update_mask)
                            | (vector->drive_enable & group_uop->dir_update_mask);
                        packed_current_dir = (packed_current_dir & ~group_uop->packed_dir_update_mask)
                            | (packed_vector_enable & group_uop->packed_dir_update_mask);
                    }
#ifdef PICO_ON_DEVICE
                    const uint32_t group_apply_done_us = time_us_32();
                    g_stream_telemetry.fast_group_apply_total_us += group_apply_done_us - group_apply_start_us;
                    const uint32_t group_bookkeeping_start_us = group_apply_done_us;
#endif
                    uint32_t wait_word = 0u;
                    uint32_t required_ticks = 0u;
                    stpg_err_t wait_err =
                        stpg_batch_compute_wait_word(group_uop->gap_from_prev_ticks, group_uop->has_sample != 0u, &wait_word, &required_ticks);
                    if (wait_err != ERR_NONE) {
                        stpg_stream_record_rate_mismatch(
                            STPG_MISMATCH_STAGE_BATCH_WAIT_COMPARE24_GROUP,
                            vector->rate_id,
                            group_index,
                            group_uop->gap_from_prev_ticks,
                            required_ticks,
                            runtime->rate.period_ticks,
                            state->prev_time_ticks,
                            group_uop->time_ticks);
                        return wait_err;
                    }
#ifdef PICO_ON_DEVICE
                    const uint32_t store_select_start_us = time_us_32();
#endif
                    uint32_t *dst = dst_cursor;
                    dst[0] = wait_word;
                    dst[1] = group_uop->sample_word;
                    dst[2] = packed_current_value;
                    dst[3] = packed_current_dir;
                    dst_cursor += 4u;
#ifdef PICO_ON_DEVICE
                    const uint32_t store_select_done_us = time_us_32();
                    g_stream_telemetry.fast_store_select_total_us += store_select_done_us - store_select_start_us;
                    g_stream_telemetry.practical_group_apply_total_us += group_apply_done_us - group_apply_start_us;
                    g_stream_telemetry.practical_wait_store_total_us +=
                        store_select_done_us - group_bookkeeping_start_us;
                    const uint32_t compare_write_start_us = store_select_done_us;
#endif
                    if (group_uop->has_sample != 0u) {
                        if (local_compare_event_count >= STPG_MAX_EVENT_COUNT) {
                            return ERR_BAD_SIZE;
                        }
                        stpg_stream_write_compare_event_fast(
                            local_compare_event_count,
                            local_emitted_event_count,
                            group_uop->sample_mask_or,
                            vector_effective_expect_mask,
                            vector_expect_value_masked);
                        local_compare_event_count += 1u;
#if STPG_STREAM_HOT_TELEMETRY
                        g_stream_telemetry.fast_compare_sample_count +=
                            (group_uop->group_flags & STPG_GROUP_FLAG_COMPARE_CANDIDATE) != 0u;
#endif
                        local_sample_events += 1u;
                        vector_sample_events += 1u;
                    }
                    local_emitted_event_count += 1u;
#ifdef PICO_ON_DEVICE
                    const uint32_t compare_write_done_us = time_us_32();
                    g_stream_telemetry.fast_compare_write_total_us += compare_write_done_us - compare_write_start_us;
                    g_stream_telemetry.practical_compare_write_total_us +=
                        compare_write_done_us - compare_write_start_us;
                    const uint32_t state_commit_start_us = compare_write_done_us;
#endif
                    state->have_prev_time = true;
                    state->prev_time_ticks = group_uop->time_ticks;
                    state->prev_period_ticks = runtime->rate.period_ticks;
#ifdef PICO_ON_DEVICE
                    g_stream_telemetry.fast_state_commit_total_us += time_us_32() - state_commit_start_us;
                    const uint32_t counter_update_start_us = time_us_32();
#endif
                    local_emit_calls += 1u;
                    local_fast_group_emit_count += 1u;
#ifdef PICO_ON_DEVICE
                    g_stream_telemetry.fast_counter_update_total_us += time_us_32() - counter_update_start_us;
                    const uint32_t loop_bookkeeping_start_us = time_us_32();
                    const uint32_t loop_bookkeeping_done_us = time_us_32();
                    g_stream_telemetry.fast_group_bookkeeping_total_us +=
                        loop_bookkeeping_done_us - group_bookkeeping_start_us;
                    g_stream_telemetry.fast_loop_bookkeeping_total_us +=
                        loop_bookkeeping_done_us - loop_bookkeeping_start_us;
                    g_stream_telemetry.practical_commit_control_total_us +=
                        loop_bookkeeping_done_us - state_commit_start_us;
                    g_stream_telemetry.fast_group_work_total_us += loop_bookkeeping_done_us - group_work_start_us;
                    g_stream_telemetry.emit_total_us += loop_bookkeeping_done_us - emit_start_us;
#endif
                    emitted_count += 1u;
                }
            }
        }

#if STPG_STREAM_HOT_TELEMETRY
        g_stream_telemetry.emit_call_count += local_emit_calls;
        g_stream_telemetry.sample_event_count += vector_sample_events;
        g_stream_telemetry.fast_group_emit_count += local_fast_group_emit_count;
#endif
        processed_vectors += 1u;
        g_stream_telemetry.fast_vector_count += 1u;
        state->vector_index += 1u;
        if (state->vector_index >= state->header.vector_count) {
            state->eof = true;
            break;
        }
    }

    if (processed_vectors > 0u) {
        g_stream_telemetry.fast_chunk_cache_span_count += 1u;
        g_stream_telemetry.fast_chunk_cache_span_vectors += processed_vectors;
    }
#ifdef PICO_ON_DEVICE
    g_stream_telemetry.load_vector_total_us += time_us_32() - span_start_us;
#endif
    *io_compare_event_count = local_compare_event_count;
    *io_emitted_event_count = local_emitted_event_count;
    *io_sample_event_count = local_sample_events;
    *io_emitted_count = emitted_count;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_stream_emit_reduced_payload(stpg_rate_stream_state_t *state,
                                                          const VectorRecord *vector,
                                                          const stpg_rate_runtime_t *runtime,
                                                          uint32_t *dst_words,
                                                          uint32_t *dst_sample_waits,
                                                          uint32_t emitted_slot,
                                                          uint32_t logical_mask,
                                                          bool pack_identity,
                                                          uint32_t *io_sample_event_count,
                                                          uint32_t *io_compare_event_count,
                                                          uint32_t *io_emitted_event_count) {
    if (state == 0 || vector == 0 || runtime == 0 || dst_words == 0 || dst_sample_waits == 0
        || io_sample_event_count == 0 || io_compare_event_count == 0 || io_emitted_event_count == 0) {
        return ERR_BAD_SIZE;
    }

    const uint32_t vector_index = state->vector_index;
    const uint32_t repeat_index = state->repeat_index;
#if STPG_STREAM_HOT_TELEMETRY
    const uint32_t previous_value = state->current_value;
    const uint32_t previous_dir = state->current_dir;
#endif
    stpg_stream_record_vector_observation(state, vector, vector_index, repeat_index, runtime);

    stpg_rate_group_uop_t drive_group_storage;
    stpg_rate_group_uop_t sample_group_storage;
    const stpg_rate_group_uop_t *drive_group = 0;
    stpg_err_t drive_group_err = stpg_buffer_resolve_rate_group_uop(
        state,
        runtime,
        vector->rate_id,
        0u,
        state->header.active_pins,
        &drive_group_storage,
        &drive_group);
    if (drive_group_err != ERR_NONE) {
        return drive_group_err;
    }
    bool has_sample_group = runtime->group_count >= 2u;
    const stpg_rate_group_uop_t *sample_group = 0;
    if (has_sample_group) {
        stpg_err_t sample_group_err = stpg_buffer_resolve_rate_group_uop(
            state,
            runtime,
            vector->rate_id,
            1u,
            state->header.active_pins,
            &sample_group_storage,
            &sample_group);
        if (sample_group_err != ERR_NONE) {
            return sample_group_err;
        }
        has_sample_group = sample_group->has_sample != 0u;
        if (!has_sample_group) {
            sample_group = 0;
        }
    }

#ifdef PICO_ON_DEVICE
    const uint32_t apply_start_us = time_us_32();
#endif
    uint32_t next_value = state->current_value;
    for (uint32_t op_index = 0u; op_index < drive_group->op_count; ++op_index) {
        next_value = stpg_apply_group_uop_drive(next_value, &drive_group->ops[op_index], vector);
    }
    uint32_t next_dir = state->current_dir;
    if (drive_group->dir_update_mask != 0u) {
        next_dir = (next_dir & ~drive_group->dir_update_mask)
            | (vector->drive_enable & drive_group->dir_update_mask);
    }
#ifdef PICO_ON_DEVICE
    g_stream_telemetry.vector_apply_total_us += time_us_32() - apply_start_us;
#endif

    const uint32_t desired_delta_ticks = state->have_prev_time ? state->prev_period_ticks : 0u;
#if STPG_STREAM_HOT_TELEMETRY
    stpg_stream_record_repeat_aware_boundary(
        state,
        vector_index,
        vector->rate_id,
        repeat_index,
        vector->repeat,
        drive_group,
        desired_delta_ticks,
        next_value != previous_value || next_dir != previous_dir);
#endif
    if (desired_delta_ticks != 0u && desired_delta_ticks < STPG_T_MIN_TICKS) {
        stpg_stream_record_rate_mismatch(
            STPG_MISMATCH_STAGE_REDUCED_PREV_PERIOD,
            vector->rate_id,
            0u,
            desired_delta_ticks,
            STPG_T_MIN_TICKS,
            runtime->rate.period_ticks,
            state->prev_time_ticks,
            drive_group->time_ticks);
        return ERR_RATE_PERIOD_MISMATCH;
    }
    dst_words[0] = desired_delta_ticks == 0u ? 0u : desired_delta_ticks - STPG_T_MIN_TICKS;

#ifdef PICO_ON_DEVICE
    const uint32_t pack_start_us = time_us_32();
#endif
    if (pack_identity) {
        dst_words[1] = next_value;
        dst_words[2] = next_dir;
    } else {
        dst_words[1] = stpg_stream_pack_logical_to_window_fast(next_value, state->header.active_pins);
        dst_words[2] = stpg_stream_pack_logical_to_window_fast(next_dir, state->header.active_pins);
    }
#ifdef PICO_ON_DEVICE
    g_stream_telemetry.pack_total_us += time_us_32() - pack_start_us;
#endif

    const uint32_t effective_mask = has_sample_group
        ? sample_group->sample_mask_or & vector->expect_mask & logical_mask : 0u;
    const bool compare_collecting = g_stream_compare_collecting;
#ifdef PICO_ON_DEVICE
    const uint32_t compare_start_us = time_us_32();
#endif
#if STPG_STREAM_COMPARE_SAMPLE_REPAIR && STPG_STREAM_COMPARE_EVENT_SCHEDULE
    /* Reduced output and delayed-sample PIO share IRQ4 as a handshake for
     * every output event.  Keep one schedule word per output event and use
     * bit zero as the capture marker.  A compact compare-only schedule would
     * deadlock at the first non-sampled event after a sampled event. */
    (void)emitted_slot;
    const bool capture_sample = compare_collecting && effective_mask != 0u;
    if (*io_sample_event_count >= STPG_MAX_EVENT_COUNT) {
        return ERR_BAD_SIZE;
    }
#ifdef PICO_ON_DEVICE
    const uint32_t sample_wait_start_us = time_us_32();
#endif
    const uint32_t delay_ticks = has_sample_group && sample_group->time_ticks != 0u
        ? (uint32_t)sample_group->time_ticks - 1u : 0u;
    dst_sample_waits[*io_sample_event_count] =
        (delay_ticks << 1u) | (capture_sample ? 1u : 0u);
    *io_sample_event_count += 1u;
#ifdef PICO_ON_DEVICE
    g_stream_telemetry.sample_wait_total_us += time_us_32() - sample_wait_start_us;
#endif
#elif STPG_STREAM_COMPARE_SAMPLE_REPAIR
    dst_sample_waits[emitted_slot] =
        has_sample_group && sample_group->time_ticks != 0u
            ? (uint32_t)sample_group->time_ticks - 1u : 0u;
#else
    dst_sample_waits[emitted_slot] =
        has_sample_group && sample_group->time_ticks != 0u
            ? (uint32_t)sample_group->time_ticks - 1u : 0u;
#endif
    bool record_compare_event = compare_collecting;
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP
    /* Preserve sample-only points for the post-run replay pass. */
    record_compare_event = compare_collecting && has_sample_group;
#endif
#if STPG_STREAM_COMPARE_SAMPLE_REPAIR
#if !STPG_STREAM_PSRAM_COMPARE_SAMPLE
    if (effective_mask == 0u) {
        record_compare_event = false;
    }
#endif
#endif
    if (record_compare_event) {
        if (stpg_stream_compare_capacity_exhausted(*io_compare_event_count)) {
            return ERR_BAD_SIZE;
        }
        const uint32_t compare_index =
            stpg_stream_compare_storage_index(*io_compare_event_count);
        g_pio_words[compare_index] = *io_emitted_event_count;
        ExecEvent *compare_event = &g_expanded_events[compare_index];
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP
        compare_event->flags = STPG_EVF_SAMPLE_POINT
            | (effective_mask != 0u ? STPG_EVF_COMPARE_POINT : 0u);
#elif STPG_STREAM_COMPARE_SAMPLE_REPAIR
        compare_event->flags = STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT;
#else
        compare_event->flags = effective_mask != 0u ? STPG_EVF_COMPARE_POINT : 0u;
#endif
        compare_event->sample_mask = effective_mask;
        compare_event->expect_value = vector->expect_value & logical_mask;
        *io_compare_event_count += 1u;
    }
    *io_emitted_event_count += 1u;
#ifdef PICO_ON_DEVICE
    if (compare_collecting) {
        g_stream_telemetry.compare_bookkeeping_total_us += time_us_32() - compare_start_us;
    }
#endif

    state->current_value = next_value;
    state->current_dir = next_dir;
    state->have_prev_time = true;
    state->prev_time_ticks = 0u;
    state->prev_period_ticks = runtime->rate.period_ticks;
#if STPG_STREAM_HOT_TELEMETRY
    stpg_stream_record_vector_emission(
        vector_index,
        repeat_index,
        desired_delta_ticks,
        next_value != previous_value || next_dir != previous_dir);
#endif
#if STPG_STREAM_HOT_TELEMETRY
    g_stream_telemetry.emit_call_count += 1u;
#if STPG_STREAM_COMPARE_SAMPLE_REPAIR
    if (compare_collecting && effective_mask != 0u) {
        g_stream_telemetry.sample_event_count += 1u;
    }
#else
    g_stream_telemetry.sample_event_count += 1u;
#endif
#endif
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_stream_emit_next_reduced_compare_fallback(stpg_rate_stream_state_t *state,
                                                                        uint32_t *dst_words,
                                                                        uint32_t *dst_sample_waits,
                                                                        uint32_t emitted_slot,
                                                                        uint32_t logical_mask,
                                                                        bool pack_identity,
                                                                        uint32_t *io_sample_event_count,
                                                                        uint32_t *io_compare_event_count,
                                                                        uint32_t *io_emitted_event_count) {
    if (state == 0 || dst_words == 0 || dst_sample_waits == 0 || io_sample_event_count == 0
        || io_compare_event_count == 0 || io_emitted_event_count == 0) {
        return ERR_BAD_SIZE;
    }

    if (!state->vector_loaded) {
#ifdef PICO_ON_DEVICE
        const uint32_t load_start_us = time_us_32();
        const uint32_t lookup_start_us = time_us_32();
#endif
        if (state->vector_index >= state->header.vector_count) {
            state->eof = true;
            return ERR_NONE;
        }
        const bool cache_miss = !stpg_stream_vector_cache_current_contains(state->vector_index);
#ifdef PICO_ON_DEVICE
        g_stream_telemetry.load_cache_lookup_total_us += time_us_32() - lookup_start_us;
#endif
        if (cache_miss) {
            g_stream_telemetry.load_cache_miss_count += 1u;
#ifdef PICO_ON_DEVICE
            const uint32_t prefetch_wait_start_us = time_us_32();
#endif
            stpg_err_t err = stpg_stream_vector_cache_ensure_current(state, state->vector_index, 0u, 0);
            if (err != ERR_NONE) {
                return err;
            }
            g_stream_telemetry.load_prefetch_wait_count += 1u;
#ifdef PICO_ON_DEVICE
            g_stream_telemetry.load_prefetch_wait_total_us += time_us_32() - prefetch_wait_start_us;
#endif
        }
        if (state->vector_index < g_stream_vector_cache_base_index
            || state->vector_index >= (g_stream_vector_cache_base_index + g_stream_vector_cache_count)) {
            return ERR_PSRAM_BOUNDS;
        }
#ifdef PICO_ON_DEVICE
        const uint32_t copy_start_us = time_us_32();
#endif
        state->current_vector = g_stream_vector_cache[state->vector_index - g_stream_vector_cache_base_index];
        if (state->current_vector.flags == STPG_VECTOR_FLAG_LOOP_REF) {
            return ERR_UNSUPPORTED;
        }
        if (state->current_vector.rate_id >= state->header.rate_count) {
            return ERR_RATEVEC_BAD_HEADER;
        }
        g_stream_telemetry.load_vector_copy_byte_total += (uint32_t)sizeof(state->current_vector);
#ifdef PICO_ON_DEVICE
        g_stream_telemetry.load_vector_copy_total_us += time_us_32() - copy_start_us;
        const uint32_t setup_start_us = time_us_32();
#endif
        state->repeat_index = 0u;
        state->current_group_index = 0u;
        state->vector_loaded = true;
#ifdef PICO_ON_DEVICE
        g_stream_telemetry.load_state_setup_total_us += time_us_32() - setup_start_us;
        g_stream_telemetry.load_vector_total_us += time_us_32() - load_start_us;
#endif
    }

    const stpg_rate_runtime_t *runtime = &state->rate_runtime[state->current_vector.rate_id];
    stpg_err_t err = stpg_buffer_stream_emit_reduced_payload(
        state,
        &state->current_vector,
        runtime,
        dst_words,
        dst_sample_waits,
        emitted_slot,
        logical_mask,
        pack_identity,
        io_sample_event_count,
        io_compare_event_count,
        io_emitted_event_count);
    if (err != ERR_NONE) {
        return err;
    }

    state->repeat_index += 1u;
    if (state->repeat_index >= state->current_vector.repeat) {
        state->repeat_index = 0u;
        state->vector_index += 1u;
        state->vector_loaded = false;
        if (state->vector_index >= state->header.vector_count) {
            state->eof = true;
        }
    }
    g_stream_telemetry.load_vector_call_count += 1u;
    g_stream_telemetry.fallback_vector_count += 1u;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_stream_fill_next_chunk_reduced_compare_impl(stpg_rate_stream_state_t *state,
                                                                          uint32_t *dst_words,
                                                                          uint32_t *dst_sample_waits,
                                                                          uint32_t max_events,
                                                                          stpg_stream_chunk_result_t *out_result) {
    if (state == 0 || dst_words == 0 || dst_sample_waits == 0 || out_result == 0 || max_events == 0u) {
        return ERR_BAD_SIZE;
    }

    memset(out_result, 0, sizeof(*out_result));
    if (state->eof) {
        out_result->eof = true;
        return ERR_NONE;
    }

    const uint32_t logical_mask =
        state->header.active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
    const bool pack_identity = state->header.active_pins != STPG_ACTIVE_PINS_V11;
    uint32_t local_compare_event_count = g_stream_compare_event_count;
    uint32_t local_emitted_event_count = g_stream_emitted_event_count;
    uint32_t local_sample_event_count = 0u;
    uint32_t emitted_count = 0u;

    while (emitted_count < max_events && !state->eof) {
#ifdef PICO_ON_DEVICE
        const uint32_t emit_start_us = time_us_32();
#endif
        if (state->vector_loaded) {
            stpg_err_t err = stpg_buffer_stream_emit_next_reduced_compare_fallback(
                state,
                &dst_words[emitted_count * 3u],
                dst_sample_waits,
                emitted_count,
                logical_mask,
                pack_identity,
                &local_sample_event_count,
                &local_compare_event_count,
                &local_emitted_event_count);
            if (err != ERR_NONE) {
                return err;
            }
            if (state->eof && state->vector_index >= state->header.vector_count) {
                break;
            }
            emitted_count += 1u;
        } else {
#ifdef PICO_ON_DEVICE
            const uint32_t lookup_start_us = time_us_32();
#endif
            if (state->vector_index >= state->header.vector_count) {
                state->eof = true;
                break;
            }
            const bool cache_miss = !stpg_stream_vector_cache_current_contains(state->vector_index);
#ifdef PICO_ON_DEVICE
            g_stream_telemetry.load_cache_lookup_total_us += time_us_32() - lookup_start_us;
#endif
            if (cache_miss) {
                g_stream_telemetry.load_cache_miss_count += 1u;
#ifdef PICO_ON_DEVICE
                const uint32_t prefetch_wait_start_us = time_us_32();
#endif
                const uint32_t request_records = max_events > emitted_count ? (max_events - emitted_count) : 1u;
                stpg_err_t err = stpg_stream_vector_cache_ensure_current(
                    state, state->vector_index, request_records, 0);
                if (err != ERR_NONE) {
                    return err;
                }
                g_stream_telemetry.load_prefetch_wait_count += 1u;
#ifdef PICO_ON_DEVICE
                g_stream_telemetry.load_prefetch_wait_total_us += time_us_32() - prefetch_wait_start_us;
#endif
            }
            if (state->vector_index < g_stream_vector_cache_base_index
                || state->vector_index >= (g_stream_vector_cache_base_index + g_stream_vector_cache_count)) {
                return ERR_PSRAM_BOUNDS;
            }
#ifdef PICO_ON_DEVICE
            const uint32_t load_done_us = time_us_32();
            g_stream_telemetry.load_vector_total_us += load_done_us - lookup_start_us;
#endif

            const uint32_t cache_offset = state->vector_index - g_stream_vector_cache_base_index;
            const uint32_t cache_available = g_stream_vector_cache_count - cache_offset;
            uint32_t span = max_events - emitted_count;
            if (span > cache_available) {
                span = cache_available;
            }
            uint32_t processed = 0u;
            for (; processed < span; ++processed) {
                const VectorRecord *vector = &g_stream_vector_cache[cache_offset + processed];
                if (vector->flags == STPG_VECTOR_FLAG_LOOP_REF) {
                    return ERR_UNSUPPORTED;
                }
                if (vector->rate_id >= state->header.rate_count) {
                    return ERR_RATEVEC_BAD_HEADER;
                }
                if (vector->repeat != 1u) {
                    break;
                }
                g_stream_telemetry.load_vector_copy_byte_total += (uint32_t)sizeof(*vector);
                const stpg_rate_runtime_t *runtime = &state->rate_runtime[vector->rate_id];
                stpg_err_t err = stpg_buffer_stream_emit_reduced_payload(
                    state,
                    vector,
                    runtime,
                    &dst_words[emitted_count * 3u],
                    dst_sample_waits,
                    emitted_count,
                    logical_mask,
                    pack_identity,
                    &local_sample_event_count,
                    &local_compare_event_count,
                    &local_emitted_event_count);
                if (err != ERR_NONE) {
                    return err;
                }
                g_stream_telemetry.fast_vector_count += 1u;
                g_stream_telemetry.load_vector_call_count += 1u;
                emitted_count += 1u;
                state->vector_index += 1u;
            }
            if (state->vector_index >= state->header.vector_count) {
                state->eof = true;
            }
            if (processed < span && emitted_count < max_events && !state->eof) {
                stpg_err_t err = stpg_buffer_stream_emit_next_reduced_compare_fallback(
                    state,
                    &dst_words[emitted_count * 3u],
                    dst_sample_waits,
                    emitted_count,
                    logical_mask,
                    pack_identity,
                    &local_sample_event_count,
                    &local_compare_event_count,
                    &local_emitted_event_count);
                if (err != ERR_NONE) {
                    return err;
                }
                emitted_count += 1u;
            }
        }
#ifdef PICO_ON_DEVICE
        g_stream_telemetry.emit_total_us += time_us_32() - emit_start_us;
#endif
    }

    g_stream_compare_event_count = local_compare_event_count;
    g_stream_emitted_event_count = local_emitted_event_count;
    out_result->event_count = emitted_count;
    /* The reduced output and delayed-sample PIOs share IRQ4.  The delayed
     * sample PIO consumes one wait word for every output event, including
     * events whose schedule word requests no capture in the event-schedule
     * variant.  This field is the per-slot schedule-DMA length here; the
     * actual capture count is tracked separately by the sample DMA. */
    out_result->sample_event_count = emitted_count;
    out_result->word_count = emitted_count * 3u;
    out_result->eof = state->eof;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_stream_emit_next_event(stpg_rate_stream_state_t *state,
                                                     uint32_t *dst_words,
                                                     ExecEvent *out_event,
                                                     bool *out_sample_trigger,
                                                     bool *out_emitted) {
#ifdef PICO_ON_DEVICE
    const uint32_t start_us = time_us_32();
#endif
    if (out_emitted != 0) *out_emitted = false;
    if (state->eof) return ERR_NONE;

    if (!state->vector_loaded) {
        stpg_err_t err = stpg_buffer_stream_load_next_vector(state, true);
        if (err != ERR_NONE || state->eof) return err;
    }

    stpg_rate_slot_runtime_t slot = state->cached_slots[state->slot_cursor];
    const uint32_t merged_time_ticks = slot.time_ticks;
    uint32_t merged_sample_mask = 0u;
    bool sample_trigger = false;
    do {
        state->current_value = stpg_apply_rate_slot_drive(state->current_value, &slot, &state->current_vector, state->header.active_pins);
        if ((slot.flags & STPG_RATE_SLOT_DIR_UPDATE) != 0u) {
            state->current_dir = (state->current_dir & ~slot.dir_mask) | (state->current_vector.drive_enable & slot.dir_mask);
        }
        if ((slot.flags & STPG_RATE_SLOT_SAMPLE) != 0u) {
            sample_trigger = true;
            merged_sample_mask |= slot.sample_mask;
        }
        state->slot_cursor += 1u;
        if (state->slot_cursor >= state->current_rate.slot_count) {
            break;
        }
        slot = state->cached_slots[state->slot_cursor];
    } while (slot.time_ticks == merged_time_ticks);

    uint32_t desired_delta_ticks = 0u;
    if (!state->have_prev_time) {
        desired_delta_ticks = merged_time_ticks;
    } else if (state->first_in_cycle) {
        desired_delta_ticks = (uint32_t)state->prev_period_ticks - (uint32_t)state->prev_time_ticks + (uint32_t)merged_time_ticks;
    } else {
        desired_delta_ticks = (uint32_t)merged_time_ticks - (uint32_t)state->prev_time_ticks;
    }
    uint32_t wait_word = 0u;
    uint32_t required_ticks = 0u;
    stpg_err_t wait_err = stpg_batch_compute_wait_word(
        desired_delta_ticks, sample_trigger, &wait_word, &required_ticks);
    if (wait_err != ERR_NONE) {
        stpg_stream_record_rate_mismatch(
            STPG_MISMATCH_STAGE_LEGACY_SLOT_MERGE,
            state->current_vector.rate_id,
            state->slot_cursor,
            desired_delta_ticks,
            required_ticks,
            state->current_rate.period_ticks,
            state->prev_time_ticks,
            merged_time_ticks);
        return ERR_RATE_PERIOD_MISMATCH;
    }
    if (dst_words != 0) {
        dst_words[0] = wait_word;
        dst_words[1] = sample_trigger ? 1u : 0u;
        dst_words[2] = stpg_stream_pack_logical_to_window_fast(state->current_value, state->header.active_pins);
        dst_words[3] = stpg_stream_pack_logical_to_window_fast(state->current_dir, state->header.active_pins);
    }
    ExecEvent generated_event;
    ExecEvent *event_ref = out_event != 0 ? out_event : &generated_event;
    memset(event_ref, 0, sizeof(*event_ref));
    event_ref->wait_ticks = (uint16_t)(wait_word > STPG_MAX_WAIT_TICKS ? STPG_MAX_WAIT_TICKS : wait_word);
    event_ref->out_value = state->current_value;
    event_ref->out_enable = state->current_dir;
    if (sample_trigger) {
        const uint32_t logical_mask = state->header.active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
        const uint32_t effective_mask = merged_sample_mask & state->current_vector.expect_mask & logical_mask;
        event_ref->flags |= STPG_EVF_SAMPLE_POINT;
        event_ref->sample_mask = effective_mask;
        event_ref->expect_value = state->current_vector.expect_value & logical_mask;
        if (effective_mask != 0u) {
            event_ref->flags |= STPG_EVF_COMPARE_POINT;
        }
    }
    if (g_stream_compare_collecting) {
#ifdef PICO_ON_DEVICE
        const uint32_t compare_start_us = time_us_32();
#endif
        const uint32_t event_index = g_stream_emitted_event_count;
        if (sample_trigger) {
            if (stpg_stream_compare_capacity_exhausted(g_stream_compare_event_count)) {
                return ERR_BAD_SIZE;
            }
            const uint32_t compare_index =
                stpg_stream_compare_storage_index(g_stream_compare_event_count);
            g_pio_words[compare_index] = event_index;
            ExecEvent *compare_event = &g_expanded_events[compare_index];
            compare_event->flags =
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP
                event_ref->flags & (STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT);
#else
                event_ref->flags & STPG_EVF_COMPARE_POINT;
#endif
            compare_event->sample_mask = event_ref->sample_mask;
            compare_event->expect_value = event_ref->expect_value;
            g_stream_compare_event_count += 1u;
        }
        g_stream_emitted_event_count += 1u;
#ifdef PICO_ON_DEVICE
        g_stream_telemetry.compare_bookkeeping_total_us += time_us_32() - compare_start_us;
#endif
    }
    if (out_sample_trigger != 0) {
        *out_sample_trigger = sample_trigger;
    }

    state->have_prev_time = true;
    state->prev_time_ticks = merged_time_ticks;
    state->prev_period_ticks = state->current_rate.period_ticks;
    state->first_in_cycle = false;
#if STPG_STREAM_HOT_TELEMETRY
        g_stream_telemetry.emit_call_count += 1u;
        if (sample_trigger) {
            g_stream_telemetry.sample_event_count += 1u;
        }
#endif
#ifdef PICO_ON_DEVICE
    g_stream_telemetry.emit_total_us += time_us_32() - start_us;
#endif
    if (out_emitted != 0) *out_emitted = true;
    return stpg_buffer_stream_finish_cycle(state);
}

static stpg_err_t stpg_buffer_expand_rate_vector_psram(const RateVectorHeader *header) {
    stpg_rate_stream_state_t *const state = stpg_buffer_stream_state_storage();
    stpg_buffer_stream_state_init(state, header);
    stpg_err_t err = stpg_buffer_parse_loop_descriptors_psram(
        header, state->loop_descs, &state->loop_block_count);
    if (err != ERR_NONE) return err;

    uint32_t emitted_count = 0u;
    uint32_t sample_count = 0u;
    while (!state->eof) {
        bool sample_trigger = false;
        bool emitted = false;
        uint32_t overflow_probe_words[4];
        ExecEvent overflow_probe_event;
        uint32_t *const output_words = emitted_count < STPG_MAX_EVENT_COUNT
            ? &g_pio_words[emitted_count * 4u]
            : overflow_probe_words;
        ExecEvent *const output_event = emitted_count < STPG_MAX_EVENT_COUNT
            ? &g_expanded_events[emitted_count]
            : &overflow_probe_event;
        err = stpg_buffer_stream_emit_next_event(state,
                                                 output_words,
                                                 output_event,
                                                 &sample_trigger,
                                                 &emitted);
        if (err != ERR_NONE) return err;
        if (!emitted) break;
        /* The capacity preflight permits exactly STPG_MAX_EVENT_COUNT events.
         * At that boundary the stream may still need one call to establish
         * EOF. Probe into local scratch so an exact-capacity pattern succeeds
         * without ever writing past the expanded buffers; a real extra event
         * remains a fail-closed size error. */
        if (emitted_count >= STPG_MAX_EVENT_COUNT) return ERR_BAD_SIZE;
        if (sample_trigger) sample_count += 1u;
        emitted_count += 1u;
    }

    g_use_expanded_event_view = true;
    g_pattern_size = g_psram_slot.size;
    g_event_count = emitted_count;
    g_sample_event_count = sample_count;
    g_pio_word_count = emitted_count * 4u;
    g_has_pattern = true;
    return ERR_NONE;
}
static stpg_err_t stpg_buffer_validate_streaming_compatibility(const RateVectorHeader *header) {
    stpg_loop_desc_t *const loop_descs = stpg_buffer_loop_desc_scratch();
    uint32_t loop_block_count = 0u;
    stpg_err_t err = stpg_buffer_parse_loop_descriptors_psram(header, loop_descs, &loop_block_count);
    if (err != ERR_NONE) return err;
    err = stpg_buffer_validate_loop_compare_compatibility(header, loop_descs, loop_block_count);
    if (err != ERR_NONE) return err;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_rate_vector_has_samples_psram(const RateVectorHeader *header, bool *out_has_samples) {
    if (header == 0 || out_has_samples == 0) {
        return ERR_BAD_SIZE;
    }

    *out_has_samples = false;
    for (uint32_t rate_index = 0; rate_index < header->rate_count; ++rate_index) {
        stpg_rate_desc_runtime_t rate_desc;
        stpg_err_t err = stpg_buffer_read_rate_desc(header, rate_index, &rate_desc);
        if (err != ERR_NONE) return err;
        for (uint32_t slot_index = 0; slot_index < rate_desc.slot_count; ++slot_index) {
            stpg_rate_slot_runtime_t slot;
            err = stpg_buffer_read_rate_slot(header, rate_desc.slot_offset + slot_index, &slot);
            if (err != ERR_NONE) return err;
            if ((slot.flags & STPG_RATE_SLOT_SAMPLE) != 0u) {
                *out_has_samples = true;
                return ERR_NONE;
            }
        }
    }
    return ERR_NONE;
}

static void stpg_buffer_stream_state_init(stpg_rate_stream_state_t *state, const RateVectorHeader *header) {
    memset(state, 0, sizeof(*state));
    state->header = *header;
    state->stream_kind = STPG_STREAM_KIND_RATE_VECTOR;
    state->cached_rate_id = 0u;
    state->rate_cached = false;
    state->first_in_cycle = true;
    state->eof = header->vector_count == 0u;
}

stpg_rate_stream_state_t *stpg_buffer_stream_state_storage(void) {
    return &g_rate_stream_state_storage;
}

static stpg_err_t stpg_buffer_prepare_rate_runtime_cache(const RateVectorHeader *header,
                                                         stpg_rate_runtime_t *runtime,
                                                         uint32_t runtime_count) {
    if (header == 0 || runtime == 0 || runtime_count < header->rate_count) {
        return ERR_BAD_SIZE;
    }
    const uint32_t pin_mask = header->active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
    for (uint32_t rate_index = 0u; rate_index < header->rate_count; ++rate_index) {
        stpg_rate_runtime_t *entry = &runtime[rate_index];
        memset(entry, 0, sizeof(*entry));
        stpg_err_t err = stpg_buffer_read_rate_desc(header, rate_index, &entry->rate);
        if (err != ERR_NONE) {
            return err;
        }
        uint32_t group_count = 0u;
        uint32_t slot_index = 0u;
        while (slot_index < entry->rate.slot_count) {
            err = stpg_buffer_read_rate_slot(header, entry->rate.slot_offset + slot_index, &entry->slots[slot_index]);
            if (err != ERR_NONE) {
                return err;
            }
            stpg_rate_slot_group_t *group = &entry->groups[group_count];
            group->time_ticks = entry->slots[slot_index].time_ticks;
            group->first_slot_index = (uint8_t)slot_index;
            group->slot_count = 1u;
            group->sample_mask_or = entry->slots[slot_index].sample_mask & pin_mask;
            slot_index += 1u;
            while (slot_index < entry->rate.slot_count) {
                err = stpg_buffer_read_rate_slot(header, entry->rate.slot_offset + slot_index, &entry->slots[slot_index]);
                if (err != ERR_NONE) {
                    return err;
                }
                if (entry->slots[slot_index].time_ticks != group->time_ticks) {
                    break;
                }
                group->slot_count += 1u;
                group->sample_mask_or |= entry->slots[slot_index].sample_mask & pin_mask;
                slot_index += 1u;
            }
            if (group_count == 0u) {
                group->gap_from_prev_ticks = group->time_ticks;
                group->gap_from_cycle_start_ticks = entry->rate.period_ticks + group->time_ticks
                    - entry->slots[entry->rate.slot_count - 1u].time_ticks;
            } else {
                const stpg_rate_slot_group_t *prev_group = &entry->groups[group_count - 1u];
                group->gap_from_prev_ticks = group->time_ticks - prev_group->time_ticks;
                group->gap_from_cycle_start_ticks = 0u;
            }
            group_count += 1u;
        }
        if (group_count > 0u) {
            const uint32_t last_time_ticks = entry->groups[group_count - 1u].time_ticks;
            entry->groups[0].gap_from_cycle_start_ticks =
                entry->rate.period_ticks - last_time_ticks + entry->groups[0].time_ticks;
        }
        entry->group_count = (uint8_t)group_count;
    }
    return ERR_NONE;
}

static bool stpg_buffer_batch_path_eligible(const RateVectorHeader *header,
                                            uint32_t loop_block_count) {
    if (header == 0) {
        return false;
    }
    return loop_block_count == 0u && header->rate_count > 0u && header->slot_count > 0u;
}

static bool stpg_buffer_loop_batch_path_eligible(const stpg_rate_stream_state_t *state) {
#if STPG_STREAM_LOOP_BATCH
    if (state == 0 || !state->rate_runtime_prepared || state->loop_block_count != 1u
        || state->header.active_pins != STPG_ACTIVE_PINS_V11
        || state->header.vector_count > 32u
        || (state->header.flags & STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4) != 0u) {
        return false;
    }
    uint32_t loop_refs = 0u;
    for (uint32_t index = 0u; index < state->header.vector_count; ++index) {
        VectorRecord top_record;
        if (stpg_buffer_read_vector_record(&state->header, index, &top_record) != ERR_NONE)
            return false;
        if (top_record.flags == STPG_VECTOR_FLAG_LOOP_REF) {
            const LoopRefRecord *const loop_ref = (const LoopRefRecord *)(const void *)&top_record;
            if (loop_ref->loop_id != 0u) return false;
            loop_refs += 1u;
        } else if (top_record.flags != 0u) {
            return false;
        }
    }
    if (loop_refs != 1u) return false;
    const stpg_loop_desc_t *const desc = &state->loop_descs[0];
    if (desc->loop_count < 4096u || desc->body_record_count < 2u
        || desc->body_record_count > 16u) {
        return false;
    }
    uint32_t template_event_count = 0u;
    for (uint32_t index = 0u; index < desc->body_record_count; ++index) {
        VectorRecord vector;
        if (stpg_buffer_read_loop_body_record(desc, index, &vector) != ERR_NONE
            || vector.flags != 0u || vector.repeat != 1u
            || vector.rate_id >= state->header.rate_count) {
            return false;
        }
        const stpg_rate_runtime_t *runtime = &state->rate_runtime[vector.rate_id];
        const uint32_t group_count = runtime->group_count;
        if (group_count == 0u || group_count > STPG_LOOP_BATCH_MAX_EVENTS - template_event_count) {
            return false;
        }
        template_event_count += group_count;
        for (uint32_t slot = 0u; slot < runtime->rate.slot_count; ++slot) {
            if ((runtime->slots[slot].flags & STPG_RATE_SLOT_DRIVE_OP_MASK) == STPG_DRIVE_COMPLEMENT) {
                return false;
            }
        }
    }
    if (template_event_count == 0u) return false;
    g_loop_batch_template_expected_events = template_event_count;
    return true;
#else
    (void)state;
    return false;
#endif
}

static bool stpg_buffer_reduced_density_compare_eligible(const RateVectorHeader *header,
                                                         const stpg_rate_runtime_t *runtime,
                                                         uint32_t loop_block_count) {
    if (header == 0 || runtime == 0) {
        return false;
    }
    if (loop_block_count != 0u || header->rate_count == 0u || header->slot_count == 0u) {
        return false;
    }
    for (uint32_t rate_index = 0u; rate_index < header->rate_count; ++rate_index) {
        const stpg_rate_runtime_t *entry = &runtime[rate_index];
        if (entry->group_count == 1u) {
#if STPG_STREAM_COMPARE_MIXED_RATE_REPAIR
            stpg_rate_group_uop_t drive_group_storage;
            if (stpg_buffer_build_rate_group_uop(
                    entry, 0u, header->active_pins, &drive_group_storage) != ERR_NONE) {
                return false;
            }
            const stpg_rate_group_uop_t *drive_group = &drive_group_storage;
            if (drive_group->time_ticks != 0u
                || drive_group->has_sample != 0u
                || drive_group->sample_mask_or != 0u
                || drive_group->op_count == 0u) {
                return false;
            }
            continue;
#else
            return false;
#endif
        }
        if (entry->group_count != 2u) {
            return false;
        }
        stpg_rate_group_uop_t drive_group_storage;
        stpg_rate_group_uop_t sample_group_storage;
        if (stpg_buffer_build_rate_group_uop(
                entry, 0u, header->active_pins, &drive_group_storage) != ERR_NONE
            || stpg_buffer_build_rate_group_uop(
                entry, 1u, header->active_pins, &sample_group_storage) != ERR_NONE) {
            return false;
        }
        const stpg_rate_group_uop_t *drive_group = &drive_group_storage;
        const stpg_rate_group_uop_t *sample_group = &sample_group_storage;
        if (drive_group->time_ticks != 0u) {
            return false;
        }
        if (drive_group->has_sample != 0u) {
            return false;
        }
        if (sample_group->has_sample == 0u) {
            return false;
        }
        if (sample_group->op_count != 0u) {
            return false;
        }
        if (sample_group->dir_update_mask != 0u) {
            return false;
        }
        if (sample_group->time_ticks == 0u || sample_group->time_ticks >= entry->rate.period_ticks) {
            return false;
        }
    }
    return true;
}

#if STPG_STREAM_COMPARE_SAMPLE_REPAIR
static stpg_err_t stpg_buffer_count_reduced_compare_samples(const RateVectorHeader *header,
                                                            const stpg_rate_runtime_t *runtime,
                                                            uint32_t *out_count) {
    if (header == 0 || runtime == 0 || out_count == 0) {
        return ERR_BAD_SIZE;
    }

    const uint32_t logical_mask =
        header->active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
    uint64_t total = 0u;
    for (uint32_t vector_index = 0u; vector_index < header->vector_count; ++vector_index) {
        VectorRecord vector;
        stpg_err_t err = stpg_buffer_read_vector_record(header, vector_index, &vector);
        if (err != ERR_NONE) {
            return err;
        }
        if (vector.flags == STPG_VECTOR_FLAG_LOOP_REF
            || vector.rate_id >= header->rate_count
            || vector.repeat == 0u) {
            return ERR_UNSUPPORTED;
        }

        const stpg_rate_runtime_t *rate = &runtime[vector.rate_id];
        if (rate->group_count == 1u) {
#if STPG_STREAM_COMPARE_MIXED_RATE_REPAIR
            continue;
#else
            return ERR_UNSUPPORTED;
#endif
        }
        if (rate->group_count != 2u) {
            return ERR_UNSUPPORTED;
        }
        stpg_rate_group_uop_t sample_group;
        stpg_err_t sample_group_err = stpg_buffer_build_rate_group_uop(
            rate, 1u, header->active_pins, &sample_group);
        if (sample_group_err != ERR_NONE || sample_group.has_sample == 0u) {
            return sample_group_err != ERR_NONE ? sample_group_err : ERR_UNSUPPORTED;
        }
        const uint32_t effective_mask = sample_group.sample_mask_or
            & vector.expect_mask & logical_mask;
        if (effective_mask != 0u) {
            total += vector.repeat;
            if (total > STPG_MAX_EVENT_COUNT) {
                return ERR_BAD_SIZE;
            }
        }
    }
    *out_count = (uint32_t)total;
    return ERR_NONE;
}
#endif

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP
static stpg_err_t stpg_buffer_count_rate_vector_samples_psram(
    const RateVectorHeader *header,
    const stpg_rate_runtime_t *runtime,
    const stpg_loop_desc_t *loop_descs,
    uint32_t loop_block_count,
    uint32_t *out_count) {
    if (header == 0 || runtime == 0 || loop_descs == 0 || out_count == 0) {
        return ERR_BAD_SIZE;
    }

    uint64_t total = 0u;
    const uint64_t sample_capacity = STPG_PSRAM_RESULT_BYTES / sizeof(uint32_t);
    for (uint32_t vector_index = 0u; vector_index < header->vector_count; ++vector_index) {
        VectorRecord vector;
        stpg_err_t err = stpg_buffer_read_vector_record(header, vector_index, &vector);
        if (err != ERR_NONE) {
            return err;
        }

        if (vector.flags == STPG_VECTOR_FLAG_LOOP_REF) {
            const LoopRefRecord *loop_ref = (const LoopRefRecord *)(const void *)&vector;
            if (loop_ref->loop_id >= loop_block_count) {
                return ERR_RATEVEC_BAD_OFFSET;
            }
            const stpg_loop_desc_t *loop_desc = &loop_descs[loop_ref->loop_id];
            uint64_t body_samples = 0u;
            for (uint32_t body_index = 0u; body_index < loop_desc->body_record_count; ++body_index) {
                VectorRecord body_record;
                err = stpg_buffer_read_loop_body_record(loop_desc, body_index, &body_record);
                if (err != ERR_NONE) {
                    return err;
                }
                if (body_record.rate_id >= header->rate_count || body_record.repeat == 0u) {
                    return ERR_RATEVEC_BAD_HEADER;
                }
                const stpg_rate_runtime_t *rate = &runtime[body_record.rate_id];
                uint32_t rate_samples = 0u;
                for (uint32_t group_index = 0u; group_index < rate->group_count; ++group_index) {
                    stpg_rate_group_uop_t group_uop;
                    err = stpg_buffer_build_rate_group_uop(
                        rate, group_index, header->active_pins, &group_uop);
                    if (err != ERR_NONE) {
                        return err;
                    }
                    rate_samples += group_uop.has_sample != 0u ? 1u : 0u;
                }
                body_samples += (uint64_t)body_record.repeat * rate_samples;
            }
            total += body_samples * loop_desc->loop_count;
        } else {
            if (vector.rate_id >= header->rate_count || vector.repeat == 0u) {
                return ERR_RATEVEC_BAD_HEADER;
            }
            const stpg_rate_runtime_t *rate = &runtime[vector.rate_id];
            uint32_t rate_samples = 0u;
            for (uint32_t group_index = 0u; group_index < rate->group_count; ++group_index) {
                stpg_rate_group_uop_t group_uop;
                err = stpg_buffer_build_rate_group_uop(
                    rate, group_index, header->active_pins, &group_uop);
                if (err != ERR_NONE) {
                    return err;
                }
                rate_samples += group_uop.has_sample != 0u ? 1u : 0u;
            }
            total += (uint64_t)vector.repeat * rate_samples;
        }

        if (total > sample_capacity) {
            return ERR_BAD_SIZE;
        }
    }

    *out_count = (uint32_t)total;
    return ERR_NONE;
}
#endif

static void stpg_buffer_set_active_source(stpg_pattern_source_t source) {
    g_active_source = source;
    g_has_pattern = source != STPG_PATTERN_SOURCE_NONE;
}

static stpg_err_t stpg_buffer_estimate_expanded_vectors(const RateVectorHeader *header,
                                                        const stpg_loop_desc_t *loop_descs,
                                                        uint32_t loop_block_count,
                                                        uint32_t *out_estimate) {
    uint64_t total = 0u;
    for (uint32_t entry_index = 0; entry_index < header->vector_count; ++entry_index) {
        VectorRecord entry;
        stpg_err_t err = stpg_buffer_read_vector_record(header, entry_index, &entry);
        if (err != ERR_NONE) return err;
        if (entry.flags == STPG_VECTOR_FLAG_LOOP_REF) {
            const LoopRefRecord *loop_ref = (const LoopRefRecord *)(const void *)&entry;
            if (loop_ref->loop_id >= loop_block_count) return ERR_RATEVEC_BAD_OFFSET;
            const stpg_loop_desc_t *loop_desc = &loop_descs[loop_ref->loop_id];
            uint64_t body_total = 0u;
            for (uint32_t body_index = 0; body_index < loop_desc->body_record_count; ++body_index) {
                VectorRecord body_record;
                err = stpg_buffer_read_loop_body_record(loop_desc, body_index, &body_record);
                if (err != ERR_NONE) return err;
                body_total += body_record.repeat;
            }
            total += body_total * loop_desc->loop_count;
        } else {
            total += entry.repeat;
        }
        if (total > 0xFFFFFFFFu) {
            total = 0xFFFFFFFFu;
            break;
        }
    }
    if (out_estimate != 0) *out_estimate = (uint32_t)total;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_count_rate_emitted_slots(const RateVectorHeader *header,
                                                       uint32_t rate_id,
                                                       uint32_t *out_emitted_slots) {
    stpg_rate_desc_runtime_t rate;
    stpg_err_t err = stpg_buffer_read_rate_desc(header, rate_id, &rate);
    if (err != ERR_NONE) return err;
    uint32_t count = 0u;
    bool have_prev = false;
    uint32_t prev_time = 0u;
    for (uint32_t slot_index = 0; slot_index < rate.slot_count; ++slot_index) {
        stpg_rate_slot_runtime_t slot;
        err = stpg_buffer_read_rate_slot(header, rate.slot_offset + slot_index, &slot);
        if (err != ERR_NONE) return err;
        if (!have_prev || slot.time_ticks != prev_time) {
            count += 1u;
            have_prev = true;
            prev_time = slot.time_ticks;
        }
    }
    if (out_emitted_slots != 0) *out_emitted_slots = count;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_estimate_emitted_events(const RateVectorHeader *header,
                                                      const stpg_loop_desc_t *loop_descs,
                                                      uint32_t loop_block_count,
                                                      uint32_t *out_estimate) {
    if (header == 0 || loop_descs == 0 || out_estimate == 0) {
        return ERR_BAD_SIZE;
    }

    uint64_t total_events = 0u;
    for (uint32_t entry_index = 0u; entry_index < header->vector_count; ++entry_index) {
        VectorRecord entry;
        stpg_err_t err = stpg_buffer_read_vector_record(header, entry_index, &entry);
        if (err != ERR_NONE) return err;

        if (entry.flags == STPG_VECTOR_FLAG_LOOP_REF) {
            const LoopRefRecord *loop_ref = (const LoopRefRecord *)(const void *)&entry;
            if (loop_ref->loop_id >= loop_block_count) return ERR_RATEVEC_BAD_OFFSET;
            const stpg_loop_desc_t *loop_desc = &loop_descs[loop_ref->loop_id];
            for (uint32_t body_index = 0u; body_index < loop_desc->body_record_count; ++body_index) {
                VectorRecord body_record;
                uint32_t emitted_slots = 0u;
                err = stpg_buffer_read_loop_body_record(loop_desc, body_index, &body_record);
                if (err != ERR_NONE) return err;
                err = stpg_buffer_count_rate_emitted_slots(header, body_record.rate_id, &emitted_slots);
                if (err != ERR_NONE) return err;
                total_events += (uint64_t)loop_desc->loop_count
                    * (uint64_t)body_record.repeat * (uint64_t)emitted_slots;
            }
        } else {
            uint32_t emitted_slots = 0u;
            err = stpg_buffer_count_rate_emitted_slots(header, entry.rate_id, &emitted_slots);
            if (err != ERR_NONE) return err;
            total_events += (uint64_t)entry.repeat * (uint64_t)emitted_slots;
        }
        if (total_events > STPG_STREAM_RATE_VECTOR_EVENT_LIMIT || total_events > 0xFFFFFFFFu) {
            return ERR_BAD_SIZE;
        }
    }

    *out_estimate = (uint32_t)total_events;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_check_full_expansion_capacity(const RateVectorHeader *header,
                                                            const stpg_loop_desc_t *loop_descs,
                                                            uint32_t loop_block_count) {
    uint32_t total_events = 0u;
    stpg_err_t err = stpg_buffer_estimate_emitted_events(
        header, loop_descs, loop_block_count, &total_events);
    if (err != ERR_NONE) return err;
    return total_events <= STPG_MAX_EVENT_COUNT ? ERR_NONE : ERR_BAD_SIZE;
}

static stpg_err_t stpg_buffer_validate_loop_compare_compatibility(const RateVectorHeader *header,
                                                                  const stpg_loop_desc_t *loop_descs,
                                                                  uint32_t loop_block_count) {
    if (loop_block_count == 0u) return ERR_NONE;
    for (uint32_t loop_index = 0; loop_index < loop_block_count; ++loop_index) {
        const stpg_loop_desc_t *loop_desc = &loop_descs[loop_index];
        for (uint32_t body_index = 0; body_index < loop_desc->body_record_count; ++body_index) {
            VectorRecord body_record;
            stpg_err_t err = stpg_buffer_read_loop_body_record(loop_desc, body_index, &body_record);
            if (err != ERR_NONE) return err;
            stpg_rate_desc_runtime_t rate;
            err = stpg_buffer_read_rate_desc(header, body_record.rate_id, &rate);
            if (err != ERR_NONE) return err;
        }
    }
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_finalize_sram(uint32_t total_size, uint32_t total_crc) {
    const uint32_t magic = *(const uint32_t *)(const void *)g_pattern_buffer;
    if (magic == STPG_RATEVEC_MAGIC || magic == STPG_MACROVEC_MAGIC || magic == STPG_MACROP2VEC_MAGIC) {
        return ERR_UNSUPPORTED;
    }
    stpg_err_t err = stpg_validate_pattern(g_pattern_buffer, g_received_size, g_expected_total_crc);
    if (err != ERR_NONE) return err;

    uint32_t pio_word_count = 0;
    uint32_t sample_event_count = 0u;
    err = stpg_expand_raw_events_to_pio_words(g_pattern_buffer, g_received_size,
                                              g_pio_words, STPG_MAX_EVENT_COUNT * 4u,
                                              &pio_word_count, &sample_event_count);
    if (err != ERR_NONE) return err;

    const PatternHeader *h = (const PatternHeader *)(const void *)g_pattern_buffer;
    g_pattern_size = g_received_size;
    g_event_count = h->event_count;
    g_sample_event_count = sample_event_count;
    g_pio_word_count = pio_word_count;
    g_active_pins = h->active_pins;
    memset(&g_rate_info, 0, sizeof(g_rate_info));
    g_use_expanded_event_view = false;
    if (h->version == STPG_PATTERN_VERSION_WIDE_WAIT) {
        err = stpg_buffer_materialize_wide_compare_events(g_pattern_buffer, g_received_size, h->event_count);
        if (err != ERR_NONE) return err;
        g_use_expanded_event_view = true;
    }
    g_stream_compare_collecting = false;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    g_stream_has_samples = false;
    stpg_stream_telemetry_reset();
    stpg_stream_vector_cache_reset();
    stpg_buffer_set_active_source(STPG_PATTERN_SOURCE_SRAM);
    (void)total_size;
    (void)total_crc;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_finalize_psram_impl(uint32_t total_size, uint32_t total_crc) {
    const bool use_verified_shadow = g_rate_vector_finalize_shadow != 0;
    if (use_verified_shadow && total_size != g_rate_vector_finalize_shadow_size) {
        return ERR_BAD_SIZE;
    }

    /* The SRAM mirror validates the received bytes and avoids redundant
     * metadata reads, but it is not authoritative for the stored image.  A
     * post-write PSRAM mutation must still fail closed before publishing the
     * PSRAM slot.  The non-shadow Rate Vector path performs this whole-image
     * checksum in its validator already, so do not scan it twice. */
    if (use_verified_shadow) {
        uint32_t stored_crc = 0u;
        stpg_err_t checksum_err = stpg_psram_checksum(
            STPG_PSRAM_PATTERN_BASE, total_size, &stored_crc);
        if (checksum_err != ERR_NONE) return checksum_err;
        if (stored_crc != total_crc) return ERR_BAD_CRC;
    }

    memset(&g_rate_info, 0, sizeof(g_rate_info));
    g_stream_compare_collecting = false;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    g_stream_has_samples = false;
    stpg_stream_vector_cache_reset();

    uint8_t header_bytes[STPG_RATEVEC_HEADER_SIZE];
    stpg_err_t err;
    if (use_verified_shadow) {
        const uint32_t header_bytes_size = total_size < sizeof(header_bytes)
            ? total_size : (uint32_t)sizeof(header_bytes);
        memcpy(header_bytes, g_rate_vector_finalize_shadow, header_bytes_size);
        err = ERR_NONE;
    } else {
        err = stpg_psram_read(STPG_PSRAM_PATTERN_BASE, header_bytes, sizeof(header_bytes));
    }
    if (err != ERR_NONE) return err;
    const uint32_t magic = *(const uint32_t *)(const void *)header_bytes;
    uint32_t format = STPG_PATTERN_FORMAT_RAW_EVENT;
    uint32_t entry_count = 0u;
    uint32_t aux_count = 0u;
    uint32_t emitted_event_estimate = 0u;
    if (magic == STPG_PATTERN_MAGIC) {
        PatternHeader header;
        err = stpg_buffer_parse_raw_header(header_bytes, total_size, &header);
        if (err != ERR_NONE) return err;
        entry_count = header.event_count;
        g_active_pins = header.active_pins;
    } else if (magic == STPG_MACROVEC_MAGIC) {
        MacroVectorHeader macro_header;
        err = stpg_buffer_parse_macro_header(header_bytes, total_size, &macro_header);
        if (err != ERR_NONE) return err;
        if (total_size > STPG_MAX_PATTERN_BYTES) return ERR_BAD_SIZE;
        if (!use_verified_shadow) {
            err = stpg_psram_read(STPG_PSRAM_PATTERN_BASE, g_pattern_buffer, total_size);
            if (err != ERR_NONE) return err;
        }
        err = stpg_validate_macro_vector(g_pattern_buffer, total_size, total_crc, 0);
        if (err != ERR_NONE) return err;
        format = STPG_PATTERN_FORMAT_MACRO_STREAM;
        entry_count = macro_header.record_count;
        g_active_pins = macro_header.active_pins;
    } else if (magic == STPG_MACROP2VEC_MAGIC) {
        MacroVectorHeader macro_header;
        err = stpg_buffer_parse_macro_p2_header(header_bytes, total_size, &macro_header);
        if (err != ERR_NONE) return err;
        if (total_size > STPG_MAX_PATTERN_BYTES) return ERR_BAD_SIZE;
        if (!use_verified_shadow) {
            err = stpg_psram_read(STPG_PSRAM_PATTERN_BASE, g_pattern_buffer, total_size);
            if (err != ERR_NONE) return err;
        }
        err = stpg_validate_macro_p2_vector(g_pattern_buffer, total_size, total_crc, 0);
        if (err != ERR_NONE) return err;
        format = STPG_PATTERN_FORMAT_MACRO_P2;
        entry_count = macro_header.record_count;
        g_active_pins = macro_header.active_pins;
    } else if (magic == STPG_RATEVEC_MAGIC) {
        RateVectorHeader rate_header;
        err = stpg_buffer_validate_rate_vector_psram(total_size, total_crc, &rate_header);
        if (err != ERR_NONE) return err;
        stpg_loop_desc_t *const loop_descs = stpg_buffer_loop_desc_scratch();
        uint32_t loop_block_count = 0u;
        uint32_t expanded_estimate = 0u;
        err = stpg_buffer_parse_loop_descriptors_psram(&rate_header, loop_descs, &loop_block_count);
        if (err != ERR_NONE) return err;
        err = stpg_buffer_estimate_expanded_vectors(&rate_header, loop_descs, loop_block_count, &expanded_estimate);
        if (err != ERR_NONE) return err;
        err = stpg_buffer_estimate_emitted_events(
            &rate_header, loop_descs, loop_block_count, &emitted_event_estimate);
        if (err != ERR_NONE) return err;
#if STPG_STREAM_RATE_VECTOR_EVENT_LIMIT > STPG_MAX_EVENT_COUNT && !STPG_STREAM_PSRAM_COMPARE_SAMPLE
        if (emitted_event_estimate > STPG_MAX_EVENT_COUNT) {
#if STPG_STREAM_FAIL_STOP
            stpg_rate_runtime_t *const runtime = g_rate_stream_state_storage.rate_runtime;
            err = stpg_buffer_prepare_rate_runtime_cache(&rate_header, runtime, STPG_MAX_RATES);
            if (err != ERR_NONE) return err;
            uint32_t sample_count = 0u;
            err = stpg_buffer_count_rate_vector_samples_psram(
                &rate_header, runtime, loop_descs, loop_block_count, &sample_count);
            if (err != ERR_NONE) return err;
            if (sample_count > STPG_MAX_EVENT_COUNT) return ERR_BAD_SIZE;
#else
            bool has_samples = false;
            err = stpg_buffer_rate_vector_has_samples_psram(&rate_header, &has_samples);
            if (err != ERR_NONE) return err;
            if (has_samples) return ERR_BAD_SIZE;
#endif
        }
#endif
        format = STPG_PATTERN_FORMAT_RATE_VECTOR;
        entry_count = rate_header.vector_count;
        aux_count = rate_header.rate_count;
        g_rate_info.vector_count = rate_header.vector_count;
        g_rate_info.rate_count = rate_header.rate_count;
        g_rate_info.slot_count = rate_header.slot_count;
        g_rate_info.loop_block_count = loop_block_count;
        g_rate_info.expanded_vector_count_estimate = expanded_estimate;
        g_rate_info.active_pins = rate_header.active_pins;
        g_rate_info.loaded = true;
        g_active_pins = rate_header.active_pins;
    } else {
        return ERR_BAD_MAGIC;
    }

    memset(&g_psram_slot, 0, sizeof(g_psram_slot));
    g_psram_slot.magic = STPG_PSRAM_SLOT_MAGIC;
    g_psram_slot.base = STPG_PSRAM_PATTERN_BASE;
    g_psram_slot.size = total_size;
    g_psram_slot.crc32 = total_crc;
    g_psram_slot.format = format;
    err = stpg_psram_write(STPG_PSRAM_META_BASE, &g_psram_slot, sizeof(g_psram_slot));
    if (err != ERR_NONE) return err;

    g_pattern_size = total_size;
    g_event_count = entry_count;
    g_sample_event_count = 0u;
    g_pio_word_count = 0u;
    g_use_expanded_event_view = false;
    if (format == STPG_PATTERN_FORMAT_RATE_VECTOR) {
#if STPG_STREAM_STATUS_LOGICAL_EVENTS
        (void)aux_count;
        /* Rate Vector status must describe the emitted logical stream.  The
         * vector repeat total counts records, not the slot-time groups that
         * become output events (for F50: 4500 records -> 6500 events). */
        g_event_count = emitted_event_estimate;
#else
        g_event_count = aux_count;
#endif
    }
    stpg_buffer_set_active_source(STPG_PATTERN_SOURCE_PSRAM);
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_finalize_psram(uint32_t total_size, uint32_t total_crc) {
    if (g_psram_upload_shadow_size == 0u || total_size != g_psram_upload_shadow_size) {
        return stpg_buffer_finalize_psram_impl(total_size, total_crc);
    }

    /* The received SRAM mirror is made only from chunks that passed their
     * per-chunk CRC and PSRAM write/read-back verification.  Verify the whole
     * advertised image and all format semantics before using it to finalize. */
    uint32_t magic = 0u;
    memcpy(&magic, g_pattern_buffer, sizeof(magic));
    stpg_err_t err;
    if (magic == STPG_RATEVEC_MAGIC) {
        /* Use the PSRAM finalizer's Rate Vector validator below: the generic
         * SRAM validator intentionally has a narrower flag set and would
         * reject valid STPG explicit-wait-P4 Rate Vectors. */
        if (stpg_crc32(g_pattern_buffer, total_size) != total_crc) return ERR_BAD_CRC;
    } else {
        err = stpg_validate_pattern(g_pattern_buffer, total_size, total_crc);
        if (err != ERR_NONE) return err;
    }

    g_rate_vector_finalize_shadow = g_pattern_buffer;
    g_rate_vector_finalize_shadow_size = total_size;
    err = stpg_buffer_finalize_psram_impl(total_size, total_crc);
    g_rate_vector_finalize_shadow = 0;
    g_rate_vector_finalize_shadow_size = 0u;
    return err;
}

void stpg_buffer_reset(void) {
    g_expected_total_size = 0;
    g_expected_total_crc = 0;
    g_received_size = 0;
    g_pattern_size = 0;
    g_event_count = 0;
    g_sample_event_count = 0;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    g_pio_word_count = 0;
    g_has_pattern = false;
    g_use_expanded_event_view = false;
    g_stream_compare_collecting = false;
    g_stream_has_samples = false;
    stpg_stream_telemetry_reset();
    stpg_stream_hotspot_reset();
    stpg_stream_vector_cache_reset();
    g_measurement_family_id = 0u;
    g_measurement_period_ticks = 0u;
    g_measurement_phase_ticks = 0u;
    g_measurement_cycle_count = 0u;
    g_upload_target = STPG_STORAGE_SRAM;
    g_active_source = STPG_PATTERN_SOURCE_NONE;
    memset(&g_psram_slot, 0, sizeof(g_psram_slot));
    g_flash_selected_run_slot = 0u;
    g_flash_selected_run_slot_valid = false;
    memset(&g_flash_selected_run_meta, 0, sizeof(g_flash_selected_run_meta));
    g_flash_selected_write_slot = 0u;
    g_flash_selected_write_slot_valid = false;
    g_flash_selected_write_slot_erased = false;
    memset(&g_rate_info, 0, sizeof(g_rate_info));
    g_active_pins = STPG_ACTIVE_PINS_V01;
    g_psram_upload_shadow_size = 0u;
    g_rate_vector_finalize_shadow = 0;
    g_rate_vector_finalize_shadow_size = 0u;
}

stpg_err_t stpg_buffer_begin(uint32_t storage_target, uint32_t total_size,
                              uint32_t total_crc, uint32_t flags) {
    if (flags != 0u) return ERR_UNSUPPORTED;
    if (total_size < STPG_HEADER_SIZE + STPG_EXEC_EVENT_SIZE) return ERR_BAD_SIZE;
    if (storage_target == STPG_STORAGE_SRAM) {
        if (total_size > STPG_MAX_PATTERN_BYTES) return ERR_BAD_SIZE;
    } else if (storage_target == STPG_STORAGE_PSRAM) {
        if (stpg_psram_prepare_access() != ERR_NONE) return ERR_PSRAM_UNAVAILABLE;
        if (total_size > STPG_PSRAM_PATTERN_BYTES) return ERR_BAD_SIZE;
    } else if (storage_target == STPG_STORAGE_FLASH) {
        const stpg_board_flash_profile_t *profile = stpg_flash_profile();
        if (stpg_psram_prepare_access() != ERR_NONE) return ERR_PSRAM_UNAVAILABLE;
        if (!g_flash_selected_write_slot_valid || !g_flash_selected_write_slot_erased) return ERR_BAD_STATE;
        if (profile == 0 || g_flash_selected_write_slot >= profile->slot_count) return ERR_BAD_STATE;
        if (total_size > STPG_PSRAM_STAGING_BYTES) return ERR_BAD_SIZE;
        if (total_size > profile->slot_size - STPG_FLASH_SLOT_META_BYTES) return ERR_BAD_SIZE;
    } else {
        return ERR_UNSUPPORTED;
    }

    const uint32_t keep_write_slot = g_flash_selected_write_slot;
    const bool keep_write_slot_valid = g_flash_selected_write_slot_valid;
    const bool keep_write_slot_erased = g_flash_selected_write_slot_erased;
    stpg_buffer_reset();
    g_flash_selected_write_slot = keep_write_slot;
    g_flash_selected_write_slot_valid = keep_write_slot_valid;
    g_flash_selected_write_slot_erased = keep_write_slot_erased;
    g_upload_target = (stpg_storage_t)storage_target;
    g_expected_total_size = total_size;
    g_expected_total_crc = total_crc;
    if (storage_target == STPG_STORAGE_PSRAM && total_size <= STPG_MAX_PATTERN_BYTES) {
        g_psram_upload_shadow_size = total_size;
    }
    return ERR_NONE;
}

stpg_err_t stpg_buffer_precheck_chunk(uint32_t offset, uint32_t size) {
    if (g_expected_total_size == 0u) return ERR_BAD_STATE;
    if (size == 0u || size > STPG_UPLOAD_CHUNK_SIZE) return ERR_BAD_SIZE;
    if (offset != g_received_size) return ERR_BAD_SIZE;
    if (offset > g_expected_total_size || size > g_expected_total_size - offset) return ERR_BAD_SIZE;
    return ERR_NONE;
}

stpg_err_t stpg_buffer_write_chunk(uint32_t offset, const void *data,
                                    uint32_t size, uint32_t chunk_crc) {
    stpg_err_t err = stpg_buffer_precheck_chunk(offset, size);
    if (err != ERR_NONE) return err;
    if (data == 0) return ERR_BAD_SIZE;
    if (stpg_crc32(data, size) != chunk_crc) return ERR_BAD_CRC;

    if (g_upload_target == STPG_STORAGE_SRAM) {
        memcpy(g_pattern_buffer + offset, data, size);
    } else if (g_upload_target == STPG_STORAGE_PSRAM) {
        err = stpg_psram_write(STPG_PSRAM_PATTERN_BASE + offset, data, size);
        if (err != ERR_NONE) return err;
        if (g_psram_upload_shadow_size != 0u) {
            memcpy(g_pattern_buffer + offset, data, size);
        }
    } else if (g_upload_target == STPG_STORAGE_FLASH) {
        err = stpg_psram_write(STPG_PSRAM_STAGING_BASE + offset, data, size);
        if (err != ERR_NONE) return err;
    } else {
        return ERR_UNSUPPORTED;
    }
    g_received_size = offset + size;
    return ERR_NONE;
}

stpg_err_t stpg_buffer_finalize(uint32_t total_size, uint32_t total_crc) {
    if (g_expected_total_size == 0u) return ERR_BAD_STATE;
    if (g_received_size != g_expected_total_size) return ERR_BAD_SIZE;
    if (total_size != g_expected_total_size) return ERR_BAD_SIZE;
    if (total_crc != g_expected_total_crc) return ERR_BAD_CRC;

    if (g_upload_target == STPG_STORAGE_SRAM) {
        return stpg_buffer_finalize_sram(total_size, total_crc);
    }
    if (g_upload_target == STPG_STORAGE_PSRAM) {
        return stpg_buffer_finalize_psram(total_size, total_crc);
    }
    if (g_upload_target == STPG_STORAGE_FLASH) {
        return stpg_buffer_finalize_flash(total_size, total_crc);
    }
    return ERR_UNSUPPORTED;
}

uint32_t stpg_buffer_received_size(void) {
    return g_received_size;
}

uint32_t stpg_buffer_expected_size(void) {
    return g_expected_total_size;
}

uint32_t stpg_buffer_pattern_size(void) {
    return g_pattern_size;
}

uint32_t stpg_buffer_event_count(void) {
    return g_event_count;
}

uint32_t stpg_buffer_sample_event_count(void) {
    return g_sample_event_count;
}

uint32_t stpg_buffer_active_pins(void) {
    if (!g_has_pattern) {
        return 0u;
    }
    return g_active_pins;
}

bool stpg_buffer_has_pattern(void) {
    return g_has_pattern;
}

stpg_pattern_source_t stpg_buffer_active_source(void) {
    return g_active_source;
}

stpg_pattern_format_t stpg_buffer_active_format(void) {
    if (!g_has_pattern) {
        return STPG_PATTERN_FORMAT_RAW_EVENT;
    }
    if (g_active_source == STPG_PATTERN_SOURCE_PSRAM) {
        return (stpg_pattern_format_t)g_psram_slot.format;
    }
    if (g_active_source == STPG_PATTERN_SOURCE_FLASH) {
        return (stpg_pattern_format_t)g_flash_selected_run_meta.format;
    }
    if (g_pattern_size >= STPG_HEADER_SIZE) {
        const PatternHeader *h = (const PatternHeader *)(const void *)g_pattern_buffer;
        if (h->magic == STPG_PATTERN_MAGIC) {
            return STPG_PATTERN_FORMAT_RAW_EVENT;
        }
        if (h->magic == STPG_MACROVEC_MAGIC) {
            return STPG_PATTERN_FORMAT_MACRO_STREAM;
        }
        if (h->magic == STPG_MACROP2VEC_MAGIC) {
            return STPG_PATTERN_FORMAT_MACRO_P2;
        }
        if (h->magic == STPG_RATEVEC_MAGIC) {
            return STPG_PATTERN_FORMAT_RATE_VECTOR;
        }
    }
    return STPG_PATTERN_FORMAT_RAW_EVENT;
}

stpg_err_t stpg_buffer_prepare_measurement_family(uint32_t family_id,
                                                  uint32_t period_ticks,
                                                  uint32_t phase_ticks,
                                                  uint32_t cycle_count) {
    const uint32_t active_pins = STPG_ACTIVE_PINS_V11;
    const uint32_t logical_mask = 0x00FFFFFFu;
    const uint32_t probe_mask = 0x00000001u;
    const uint32_t sample_flags = STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT;

    if (cycle_count == 0u) {
        return ERR_BAD_SIZE;
    }
    if (period_ticks < STPG_T_MIN_TICKS || period_ticks > STPG_MAX_WAIT_TICKS) {
        return ERR_BAD_SIZE;
    }

    uint32_t event_count = 0u;
    uint32_t sample_event_count = 0u;
    bool first_event = true;
    for (uint32_t cycle = 0u; cycle < cycle_count; ++cycle) {
        uint32_t event_add = 0u;
        uint32_t sample_add = 0u;
        stpg_err_t err =
            stpg_measurement_family_count(family_id, period_ticks, phase_ticks, first_event, &event_add, &sample_add);
        if (err != ERR_NONE) {
            return err;
        }
        if (event_count > (STPG_MAX_EVENT_COUNT - event_add)) {
            return ERR_BAD_SIZE;
        }
        event_count += event_add;
        sample_event_count += sample_add;
        first_event = false;
    }

    memset(g_pattern_buffer, 0, sizeof(g_pattern_buffer));
    memset(g_expanded_events, 0, event_count * sizeof(g_expanded_events[0]));
    memset(g_pio_words, 0, event_count * 4u * sizeof(g_pio_words[0]));
    stpg_stream_telemetry_reset();
    stpg_stream_hotspot_reset();

    uint32_t output_value = 0u;
    uint32_t event_index = 0u;
    first_event = true;
    for (uint32_t cycle = 0u; cycle < cycle_count; ++cycle) {
        stpg_err_t err = stpg_measurement_append_family(
            family_id,
            period_ticks,
            phase_ticks,
            active_pins,
            logical_mask,
            probe_mask,
            sample_flags,
            &event_index,
            &output_value,
            &first_event);
        if (err != ERR_NONE) {
            return err;
        }
    }

    PatternHeader *header = (PatternHeader *)(void *)g_pattern_buffer;
    header->magic = STPG_PATTERN_MAGIC;
    header->version = STPG_PATTERN_VERSION;
    header->header_size = STPG_HEADER_SIZE;
    header->flags = 0u;
    header->active_pins = active_pins;
    header->tick_hz = STPG_TICK_HZ;
    header->event_size = STPG_EXEC_EVENT_SIZE;
    header->event_count = event_count;
    header->data_size = event_count * STPG_EXEC_EVENT_SIZE;
    header->data_crc32 = 0u;
    header->header_crc32 = 0u;

    g_expected_total_size = 0u;
    g_expected_total_crc = 0u;
    g_received_size = 0u;
    g_pattern_size = STPG_HEADER_SIZE + header->data_size;
    g_event_count = event_count;
    g_sample_event_count = sample_event_count;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    g_pio_word_count = event_count * 4u;
    g_active_pins = active_pins;
    g_has_pattern = true;
    g_use_expanded_event_view = true;
    g_stream_compare_collecting = false;
    g_stream_has_samples = sample_event_count != 0u;
    stpg_stream_vector_cache_reset();
    g_measurement_family_id = family_id;
    g_measurement_period_ticks = period_ticks;
    g_measurement_phase_ticks = phase_ticks;
    g_measurement_cycle_count = cycle_count;
    memset(&g_rate_info, 0, sizeof(g_rate_info));
    stpg_buffer_set_active_source(STPG_PATTERN_SOURCE_SRAM);
    return ERR_NONE;
}

stpg_err_t stpg_buffer_prepare_measurement_sequence(uint32_t sequence_id,
                                                    uint32_t period_ticks,
                                                    uint32_t phase_ticks,
                                                    uint32_t cycle_count) {
    const uint32_t active_pins = STPG_ACTIVE_PINS_V11;
    const uint32_t logical_mask = 0x00FFFFFFu;
    const uint32_t probe_mask = 0x00000001u;
    const uint32_t sample_flags = STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT;
    uint32_t step_count = 0u;
    const uint8_t *steps = stpg_measurement_sequence_steps(sequence_id, &step_count);

    if (cycle_count == 0u) {
        return ERR_BAD_SIZE;
    }
    if (period_ticks < STPG_T_MIN_TICKS || period_ticks > STPG_MAX_WAIT_TICKS) {
        return ERR_BAD_SIZE;
    }
    if (steps == NULL || step_count == 0u) {
        return ERR_UNSUPPORTED;
    }

    uint32_t event_count = 0u;
    uint32_t sample_event_count = 0u;
    bool first_event = true;
    for (uint32_t cycle = 0u; cycle < cycle_count; ++cycle) {
        for (uint32_t step_index = 0u; step_index < step_count; ++step_index) {
            uint32_t event_add = 0u;
            uint32_t sample_add = 0u;
            stpg_err_t err = stpg_measurement_family_count(
                (uint32_t)steps[step_index], period_ticks, phase_ticks, first_event, &event_add, &sample_add);
            if (err != ERR_NONE) {
                return err;
            }
            if (event_count > (STPG_MAX_EVENT_COUNT - event_add)) {
                return ERR_BAD_SIZE;
            }
            event_count += event_add;
            sample_event_count += sample_add;
            first_event = false;
        }
    }

    memset(g_pattern_buffer, 0, sizeof(g_pattern_buffer));
    memset(g_expanded_events, 0, event_count * sizeof(g_expanded_events[0]));
    memset(g_pio_words, 0, event_count * 4u * sizeof(g_pio_words[0]));
    stpg_stream_telemetry_reset();
    stpg_stream_hotspot_reset();

    uint32_t output_value = 0u;
    uint32_t event_index = 0u;
    first_event = true;
    for (uint32_t cycle = 0u; cycle < cycle_count; ++cycle) {
        for (uint32_t step_index = 0u; step_index < step_count; ++step_index) {
            stpg_err_t err = stpg_measurement_append_family(
                (uint32_t)steps[step_index],
                period_ticks,
                phase_ticks,
                active_pins,
                logical_mask,
                probe_mask,
                sample_flags,
                &event_index,
                &output_value,
                &first_event);
            if (err != ERR_NONE) {
                return err;
            }
        }
    }

    PatternHeader *header = (PatternHeader *)(void *)g_pattern_buffer;
    header->magic = STPG_PATTERN_MAGIC;
    header->version = STPG_PATTERN_VERSION;
    header->header_size = STPG_HEADER_SIZE;
    header->flags = 0u;
    header->active_pins = active_pins;
    header->tick_hz = STPG_TICK_HZ;
    header->event_size = STPG_EXEC_EVENT_SIZE;
    header->event_count = event_count;
    header->data_size = event_count * STPG_EXEC_EVENT_SIZE;
    header->data_crc32 = 0u;
    header->header_crc32 = 0u;

    g_expected_total_size = 0u;
    g_expected_total_crc = 0u;
    g_received_size = 0u;
    g_pattern_size = STPG_HEADER_SIZE + header->data_size;
    g_event_count = event_count;
    g_sample_event_count = sample_event_count;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    g_pio_word_count = event_count * 4u;
    g_active_pins = active_pins;
    g_has_pattern = true;
    g_use_expanded_event_view = true;
    g_stream_compare_collecting = false;
    g_stream_has_samples = sample_event_count != 0u;
    stpg_stream_vector_cache_reset();
    g_measurement_family_id = STPG_MEASURE_SEQUENCE_DEBUG_BASE | sequence_id;
    g_measurement_period_ticks = period_ticks;
    g_measurement_phase_ticks = phase_ticks;
    g_measurement_cycle_count = cycle_count;
    memset(&g_rate_info, 0, sizeof(g_rate_info));
    stpg_buffer_set_active_source(STPG_PATTERN_SOURCE_SRAM);
    return ERR_NONE;
}

stpg_err_t stpg_buffer_prepare_measurement_burst(uint32_t burst_id,
                                                 uint32_t period_ticks,
                                                 uint32_t phase_ticks,
                                                 uint32_t cycle_count) {
    const uint32_t active_pins = STPG_ACTIVE_PINS_V11;
    const uint32_t logical_mask = 0x00FFFFFFu;
    const uint32_t probe_mask = 0x00000001u;
    const uint32_t sample_flags = STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT;
    uint32_t step_count = 0u;
    const uint8_t *steps = stpg_measurement_burst_steps(burst_id, &step_count);

    if (cycle_count == 0u) {
        return ERR_BAD_SIZE;
    }
    if (period_ticks < STPG_T_MIN_TICKS || period_ticks > STPG_MAX_WAIT_TICKS) {
        return ERR_BAD_SIZE;
    }
    if (steps == NULL || step_count == 0u) {
        return ERR_UNSUPPORTED;
    }

    uint32_t event_count = 0u;
    uint32_t sample_event_count = 0u;
    bool first_event = true;
    for (uint32_t cycle = 0u; cycle < cycle_count; ++cycle) {
        for (uint32_t step_index = 0u; step_index < step_count; ++step_index) {
            uint32_t event_add = 0u;
            uint32_t sample_add = 0u;
            stpg_err_t err = stpg_measurement_family_count(
                (uint32_t)steps[step_index], period_ticks, phase_ticks, first_event, &event_add, &sample_add);
            if (err != ERR_NONE) {
                return err;
            }
            if (event_count > (STPG_MAX_EVENT_COUNT - event_add)) {
                return ERR_BAD_SIZE;
            }
            event_count += event_add;
            sample_event_count += sample_add;
            first_event = false;
        }
    }

    memset(g_pattern_buffer, 0, sizeof(g_pattern_buffer));
    memset(g_expanded_events, 0, event_count * sizeof(g_expanded_events[0]));
    memset(g_pio_words, 0, event_count * 4u * sizeof(g_pio_words[0]));
    stpg_stream_telemetry_reset();
    stpg_stream_hotspot_reset();

    uint32_t output_value = 0u;
    uint32_t event_index = 0u;
    first_event = true;
    for (uint32_t cycle = 0u; cycle < cycle_count; ++cycle) {
        for (uint32_t step_index = 0u; step_index < step_count; ++step_index) {
            stpg_err_t err = stpg_measurement_append_family(
                (uint32_t)steps[step_index],
                period_ticks,
                phase_ticks,
                active_pins,
                logical_mask,
                probe_mask,
                sample_flags,
                &event_index,
                &output_value,
                &first_event);
            if (err != ERR_NONE) {
                return err;
            }
        }
    }

    PatternHeader *header = (PatternHeader *)(void *)g_pattern_buffer;
    header->magic = STPG_PATTERN_MAGIC;
    header->version = STPG_PATTERN_VERSION;
    header->header_size = STPG_HEADER_SIZE;
    header->flags = 0u;
    header->active_pins = active_pins;
    header->tick_hz = STPG_TICK_HZ;
    header->event_size = STPG_EXEC_EVENT_SIZE;
    header->event_count = event_count;
    header->data_size = event_count * STPG_EXEC_EVENT_SIZE;
    header->data_crc32 = 0u;
    header->header_crc32 = 0u;

    g_expected_total_size = 0u;
    g_expected_total_crc = 0u;
    g_received_size = 0u;
    g_pattern_size = STPG_HEADER_SIZE + header->data_size;
    g_event_count = event_count;
    g_sample_event_count = sample_event_count;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    g_pio_word_count = event_count * 4u;
    g_active_pins = active_pins;
    g_has_pattern = true;
    g_use_expanded_event_view = true;
    g_stream_compare_collecting = false;
    g_stream_has_samples = sample_event_count != 0u;
    stpg_stream_vector_cache_reset();
    g_measurement_family_id = STPG_MEASURE_BURST_DEBUG_BASE | burst_id;
    g_measurement_period_ticks = period_ticks;
    g_measurement_phase_ticks = phase_ticks;
    g_measurement_cycle_count = cycle_count;
    memset(&g_rate_info, 0, sizeof(g_rate_info));
    stpg_buffer_set_active_source(STPG_PATTERN_SOURCE_SRAM);
    return ERR_NONE;
}

stpg_err_t stpg_buffer_prepare_for_run(void) {
    stpg_err_t err = ERR_NONE;
    if (g_active_source == STPG_PATTERN_SOURCE_NONE) {
        return ERR_NO_PATTERN;
    }
    if (g_active_source == STPG_PATTERN_SOURCE_SRAM) {
        return ERR_NONE;
    }
    if (g_active_source == STPG_PATTERN_SOURCE_FLASH) {
        return stpg_buffer_prepare_flash_for_run();
    }
    if (g_active_source != STPG_PATTERN_SOURCE_PSRAM) {
        return ERR_UNSUPPORTED;
    }
    /* Upload finalization validates metadata and checksum, but direct RUN
     * performs the first full pattern read after that point.  Re-establish
     * the live QMI contract immediately before this read so a prior SDK or
     * reset path cannot leave XIP access in a stale transaction format. */
    if (stpg_psram_prepare_access() != ERR_NONE) {
        return ERR_PSRAM_UNAVAILABLE;
    }
    if (g_psram_slot.magic != STPG_PSRAM_SLOT_MAGIC) {
        return ERR_NO_PATTERN;
    }
    if (g_psram_slot.format == STPG_PATTERN_FORMAT_MACRO_STREAM) {
        if (g_psram_slot.size > STPG_MAX_PATTERN_BYTES) {
            return ERR_BAD_SIZE;
        }
        err = stpg_psram_read(g_psram_slot.base, g_pattern_buffer, g_psram_slot.size);
        if (err != ERR_NONE) return err;
        err = stpg_validate_macro_vector(g_pattern_buffer, g_psram_slot.size, g_psram_slot.crc32, 0);
        if (err != ERR_NONE) return err;
        return stpg_buffer_expand_macro_vector_buffer(g_pattern_buffer, g_psram_slot.size);
    }
    if (g_psram_slot.format == STPG_PATTERN_FORMAT_MACRO_P2) {
        if (g_psram_slot.size > STPG_MAX_PATTERN_BYTES) {
            return ERR_BAD_SIZE;
        }
        err = stpg_psram_read(g_psram_slot.base, g_pattern_buffer, g_psram_slot.size);
        if (err != ERR_NONE) return err;
        err = stpg_validate_macro_p2_vector(g_pattern_buffer, g_psram_slot.size, g_psram_slot.crc32, 0);
        if (err != ERR_NONE) return err;
        return stpg_buffer_expand_macro_p2_vector_buffer(g_pattern_buffer, g_psram_slot.size);
    }
    if (g_psram_slot.format != STPG_PATTERN_FORMAT_RAW_EVENT) {
        RateVectorHeader header;
        stpg_loop_desc_t *const loop_descs = stpg_buffer_loop_desc_scratch();
        uint32_t loop_block_count = 0u;
        g_stream_compare_collecting = false;
        g_stream_compare_event_count = 0u;
        g_stream_compare_event_base = 0u;
        g_stream_emitted_event_count = 0u;
        stpg_stream_telemetry_reset();
        stpg_stream_vector_cache_reset();
        err = stpg_buffer_validate_rate_vector_psram(g_psram_slot.size, g_psram_slot.crc32, &header);
        if (err != ERR_NONE) return err;
        err = stpg_buffer_parse_loop_descriptors_psram(&header, loop_descs, &loop_block_count);
        if (err != ERR_NONE) return err;
        err = stpg_buffer_validate_loop_compare_compatibility(&header, loop_descs, loop_block_count);
        if (err != ERR_NONE) return err;
        err = stpg_buffer_check_full_expansion_capacity(&header, loop_descs, loop_block_count);
        if (err != ERR_NONE) return err;
        return stpg_buffer_expand_rate_vector_psram(&header);
    }
    if (g_psram_slot.size > STPG_MAX_PATTERN_BYTES) {
        return ERR_BAD_SIZE;
    }

    err = stpg_psram_read(g_psram_slot.base, g_pattern_buffer, g_psram_slot.size);
    if (err != ERR_NONE) return err;
    err = stpg_validate_pattern(g_pattern_buffer, g_psram_slot.size, g_psram_slot.crc32);
    if (err != ERR_NONE) return err;

    uint32_t pio_word_count = 0u;
    uint32_t sample_event_count = 0u;
    err = stpg_expand_raw_events_to_pio_words(g_pattern_buffer, g_psram_slot.size,
                                              g_pio_words, STPG_MAX_EVENT_COUNT * 4u,
                                              &pio_word_count, &sample_event_count);
    if (err != ERR_NONE) return err;

    const PatternHeader *h = (const PatternHeader *)(const void *)g_pattern_buffer;
    g_pattern_size = g_psram_slot.size;
    g_event_count = h->event_count;
    g_sample_event_count = sample_event_count;
    g_pio_word_count = pio_word_count;
    g_active_pins = h->active_pins;
    g_has_pattern = true;
    g_use_expanded_event_view = false;
    if (h->version == STPG_PATTERN_VERSION_WIDE_WAIT) {
        err = stpg_buffer_materialize_wide_compare_events(g_pattern_buffer, g_psram_slot.size, h->event_count);
        if (err != ERR_NONE) return err;
        g_use_expanded_event_view = true;
    }
    g_stream_compare_collecting = false;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    stpg_stream_telemetry_reset();
    stpg_stream_vector_cache_reset();
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_prepare_macro_streaming(stpg_rate_stream_state_t *out_state) {
    if (g_psram_slot.size > STPG_MAX_PATTERN_BYTES) {
        return ERR_BAD_SIZE;
    }

    stpg_err_t err = stpg_psram_read(g_psram_slot.base, g_pattern_buffer, g_psram_slot.size);
    if (err != ERR_NONE) {
        return err;
    }

    MacroVectorHeader header;
    err = stpg_validate_macro_vector(g_pattern_buffer, g_psram_slot.size, g_psram_slot.crc32, &header);
    if (err != ERR_NONE) {
        return err;
    }

    const uint8_t *record_data = g_pattern_buffer + STPG_MACROVEC_HEADER_SIZE;
    uint32_t event_count = 0u;
    uint32_t sample_event_count = 0u;
    for (uint32_t index = 0u; index < header.record_count; ++index) {
        const MacroVectorRecord *record =
            (const MacroVectorRecord *)(const void *)(record_data + (index * STPG_MACRO_RECORD_SIZE));
        const uint32_t events_per_repeat = (record->family == 1u || record->family == 2u) ? 2u : 1u;
        const uint32_t samples_per_repeat =
            ((record->flags & STPG_EVF_SAMPLE_POINT) != 0u) ? 1u : 0u;
        event_count += (uint32_t)((uint64_t)record->repeat * (uint64_t)events_per_repeat);
        sample_event_count += (uint32_t)((uint64_t)record->repeat * (uint64_t)samples_per_repeat);
    }

    memset(out_state, 0, sizeof(*out_state));
    out_state->stream_kind = STPG_STREAM_KIND_MACRO_STREAM;
    out_state->macro_header = header;
    out_state->first_in_cycle = true;
    out_state->eof = header.record_count == 0u;
    out_state->macro_record_index = 0u;
    out_state->macro_repeat_index = 0u;
    out_state->macro_event_phase = 0u;
    out_state->macro_record_loaded = false;

    g_use_expanded_event_view = false;
    g_stream_compare_collecting = false;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    stpg_stream_telemetry_reset();
    stpg_stream_hotspot_reset();
    stpg_stream_vector_cache_reset();
    g_stream_has_samples = sample_event_count != 0u;
    g_pattern_size = g_psram_slot.size;
    g_event_count = event_count;
    g_sample_event_count = sample_event_count;
    g_pio_word_count = 0u;
    g_has_pattern = true;
    g_active_pins = header.active_pins;
    memset(&g_rate_info, 0, sizeof(g_rate_info));
    return ERR_NONE;
}

stpg_err_t stpg_buffer_prepare_for_streaming(stpg_rate_stream_state_t *out_state, uint32_t run_flags) {
    if (out_state == 0) {
        return ERR_BAD_SIZE;
    }
    if (run_flags != STPG_RUN_FLAG_STREAMING_POC) {
        return ERR_UNSUPPORTED;
    }
    if (g_active_source != STPG_PATTERN_SOURCE_PSRAM) {
        return ERR_BAD_STATE;
    }
    if (g_psram_slot.magic != STPG_PSRAM_SLOT_MAGIC) {
        return ERR_NO_PATTERN;
    }
    if (g_psram_slot.format == STPG_PATTERN_FORMAT_MACRO_STREAM) {
        return stpg_buffer_prepare_macro_streaming(out_state);
    }
    if (g_psram_slot.format != STPG_PATTERN_FORMAT_RATE_VECTOR) {
        return ERR_UNSUPPORTED;
    }

    RateVectorHeader header;
    stpg_err_t err = stpg_buffer_validate_rate_vector_psram(g_psram_slot.size, g_psram_slot.crc32, &header);
    if (err != ERR_NONE) {
        return err;
    }
    err = stpg_buffer_validate_streaming_compatibility(&header);
    if (err != ERR_NONE) {
        return err;
    }

    stpg_buffer_stream_state_init(out_state, &header);
    stpg_err_t parse_err = stpg_buffer_parse_loop_descriptors_psram(&header, out_state->loop_descs, &out_state->loop_block_count);
    if (parse_err != ERR_NONE) {
        return parse_err;
    }
    parse_err = stpg_buffer_prepare_rate_runtime_cache(&header, out_state->rate_runtime, STPG_MAX_RATES);
    if (parse_err != ERR_NONE) {
        return parse_err;
    }
    out_state->rate_runtime_prepared = true;
#if STPG_STREAM_LOOP_BATCH
    stpg_loop_batch_template_reset();
#endif
    out_state->batch_path_enabled = stpg_buffer_batch_path_eligible(&header, out_state->loop_block_count)
        || stpg_buffer_loop_batch_path_eligible(out_state);
    out_state->reduced_density_compare_enabled =
        stpg_buffer_reduced_density_compare_eligible(&header, out_state->rate_runtime, out_state->loop_block_count);
#if !STPG_ENABLE_REDUCED_COMPARE_RUNTIME
    out_state->reduced_density_compare_enabled = false;
#endif
    g_use_expanded_event_view = false;
    g_stream_compare_collecting = true;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    g_stream_expected_sample_count = 0u;
    stpg_stream_telemetry_reset();
    stpg_stream_hotspot_reset();
    stpg_stream_vector_cache_reset();
    err = stpg_buffer_prepare_precomputed_rate_group_uops(out_state);
    if (err != ERR_NONE) {
        return err;
    }
    err = stpg_buffer_rate_vector_has_samples_psram(&header, &g_stream_has_samples);
    if (err != ERR_NONE) {
        return err;
    }
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP
    if (g_stream_has_samples) {
        err = stpg_buffer_count_rate_vector_samples_psram(
            &header,
            out_state->rate_runtime,
            out_state->loop_descs,
            out_state->loop_block_count,
            &g_stream_expected_sample_count);
        if (err != ERR_NONE) {
            return err;
        }
    }
#endif
#if STPG_STREAM_COMPARE_SAMPLE_REPAIR
    if (out_state->reduced_density_compare_enabled && g_stream_has_samples) {
        err = stpg_buffer_count_reduced_compare_samples(
            &header, out_state->rate_runtime, &g_stream_expected_sample_count);
        if (err != ERR_NONE) {
            return err;
        }
    }
#endif
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
    /* Large sample/compare streams keep only the output chunk in flight.  The
     * bounded compare metadata is rebuilt after capture by the replay API. */
    g_stream_compare_collecting = false;
#elif STPG_STREAM_COMPARE_FREE_BLOCK_FILL
    /* A compare-free rate vector does not need the compare24 span or compare
     * bookkeeping branch. Keep the normal collecting behavior for patterns
     * that actually contain sample/compare events. */
    g_stream_compare_collecting = g_stream_has_samples;
#else
    g_stream_compare_collecting = true;
#endif
    g_pattern_size = g_psram_slot.size;
    g_sample_event_count = 0u;
    g_pio_word_count = 0u;
    g_has_pattern = true;
    g_active_pins = header.active_pins;
    return ERR_NONE;
}

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
stpg_err_t stpg_buffer_stream_compare_replay_init(stpg_rate_stream_state_t *out_state) {
    if (out_state == 0) {
        return ERR_BAD_SIZE;
    }
    stpg_err_t err = stpg_buffer_prepare_for_streaming(out_state, STPG_RUN_FLAG_STREAMING_POC);
    if (err != ERR_NONE) {
        return err;
    }
    if (out_state->stream_kind != STPG_STREAM_KIND_RATE_VECTOR) {
        return ERR_UNSUPPORTED;
    }
#if STPG_STREAM_LOOP_BATCH
    if (out_state->loop_block_count != 0u) out_state->batch_path_enabled = false;
#endif
    g_stream_compare_collecting = true;
    g_stream_compare_event_count = 0u;
    g_stream_compare_event_base = 0u;
    g_stream_emitted_event_count = 0u;
    return ERR_NONE;
}

stpg_err_t stpg_buffer_stream_compare_replay_fill_next(
    stpg_rate_stream_state_t *state,
    stpg_stream_chunk_result_t *out_result) {
    if (state == 0 || out_result == 0) {
        return ERR_BAD_SIZE;
    }
    g_stream_compare_event_count = 0u;
    return stpg_buffer_stream_fill_next_chunk(
        state,
        stpg_buffer_streaming_scratch_words(),
        STPG_STREAM_BUFFER_EVENTS,
        out_result);
}
#endif

static stpg_err_t stpg_buffer_stream_emit_next_macro_event(stpg_rate_stream_state_t *state,
                                                           uint32_t *dst_words,
                                                           bool *out_sample_trigger,
                                                           bool *out_emitted) {
    if (out_emitted != 0) *out_emitted = false;
    if (out_sample_trigger != 0) *out_sample_trigger = false;
    if (state == 0) {
        return ERR_BAD_SIZE;
    }
    if (state->eof) {
        return ERR_NONE;
    }

    if (!state->macro_record_loaded) {
        if (state->macro_record_index >= state->macro_header.record_count) {
            state->eof = true;
            return ERR_NONE;
        }
#ifdef PICO_ON_DEVICE
        const uint32_t record_read_start_us = time_us_32();
#endif
        stpg_err_t read_err = stpg_buffer_read_macro_record_psram(
            &state->macro_header, state->macro_record_index, &state->current_macro_record);
        if (read_err != ERR_NONE) {
            return read_err;
        }
#ifdef PICO_ON_DEVICE
        const uint32_t record_read_elapsed_us = time_us_32() - record_read_start_us;
        g_stream_telemetry.macro_record_read_call_count += 1u;
        g_stream_telemetry.macro_record_read_total_us += record_read_elapsed_us;
        g_stream_telemetry.macro_record_read_last_us = record_read_elapsed_us;
        if (record_read_elapsed_us > g_stream_telemetry.macro_record_read_max_us) {
            g_stream_telemetry.macro_record_read_max_us = record_read_elapsed_us;
        }
#endif
        state->macro_drive_value_cached = stpg_macro_load_u24(state->current_macro_record.drive_value);
        state->macro_drive_enable_cached = stpg_macro_load_u24(state->current_macro_record.drive_enable);
        state->macro_sample_mask_cached = stpg_macro_load_u24(state->current_macro_record.sample_mask);
        state->macro_expect_value_cached = stpg_macro_load_u24(state->current_macro_record.expect_value);
        stpg_macro_fill_compare_proto(state, &state->macro_compare_proto);
        state->macro_packed_drive_value_cached =
            stpg_stream_pack_logical_to_window_fast(state->macro_drive_value_cached, state->macro_header.active_pins);
        state->macro_packed_drive_enable_cached =
            stpg_stream_pack_logical_to_window_fast(state->macro_drive_enable_cached, state->macro_header.active_pins);
        memset(state->macro_phase_time_ticks, 0, sizeof(state->macro_phase_time_ticks));
        memset(state->macro_phase_initial_wait_word, 0, sizeof(state->macro_phase_initial_wait_word));
        memset(state->macro_phase_cycle_wait_word, 0, sizeof(state->macro_phase_cycle_wait_word));
        memset(state->macro_phase_intra_wait_word, 0, sizeof(state->macro_phase_intra_wait_word));
        memset(state->macro_phase_initial_desired_delta, 0, sizeof(state->macro_phase_initial_desired_delta));
        memset(state->macro_phase_cycle_desired_delta, 0, sizeof(state->macro_phase_cycle_desired_delta));
        memset(state->macro_phase_intra_desired_delta, 0, sizeof(state->macro_phase_intra_desired_delta));
        memset(state->macro_phase_initial_required_ticks, 0, sizeof(state->macro_phase_initial_required_ticks));
        memset(state->macro_phase_cycle_required_ticks, 0, sizeof(state->macro_phase_cycle_required_ticks));
        memset(state->macro_phase_intra_required_ticks, 0, sizeof(state->macro_phase_intra_required_ticks));
        memset(state->macro_phase_flags, 0, sizeof(state->macro_phase_flags));
        memset(state->macro_phase_sample_trigger, 0, sizeof(state->macro_phase_sample_trigger));
        memset(state->macro_phase_logical_complete, 0, sizeof(state->macro_phase_logical_complete));
        memset(state->macro_phase_updates_output, 0, sizeof(state->macro_phase_updates_output));
        memset(state->macro_phase_initial_wait_valid, 0, sizeof(state->macro_phase_initial_wait_valid));
        memset(state->macro_phase_cycle_wait_valid, 0, sizeof(state->macro_phase_cycle_wait_valid));
        memset(state->macro_phase_intra_wait_valid, 0, sizeof(state->macro_phase_intra_wait_valid));
        state->macro_phase_packed_value[0] =
            stpg_stream_pack_logical_to_window_fast(state->current_value, state->macro_header.active_pins);
        state->macro_phase_packed_enable[0] =
            stpg_stream_pack_logical_to_window_fast(state->current_dir, state->macro_header.active_pins);
        state->macro_phase_packed_value[1] = state->macro_packed_drive_value_cached;
        state->macro_phase_packed_enable[1] = state->macro_packed_drive_enable_cached;
        switch (state->current_macro_record.family) {
            case 1u:
                state->macro_phase_count = 2u;
                state->macro_phase_packed_value[0] = state->macro_packed_drive_value_cached;
                state->macro_phase_packed_enable[0] = state->macro_packed_drive_enable_cached;
                state->macro_phase_time_ticks[1] = state->current_macro_record.phase_ticks;
                state->macro_phase_flags[1] = state->current_macro_record.flags;
                state->macro_phase_sample_trigger[1] =
                    (uint8_t)(((state->current_macro_record.flags & STPG_EVF_SAMPLE_POINT) != 0u) ? 1u : 0u);
                state->macro_phase_logical_complete[1] = 1u;
                state->macro_phase_updates_output[1] = 1u;
                break;
            case 2u:
                state->macro_phase_count = 2u;
                state->macro_phase_flags[0] = state->current_macro_record.flags;
                state->macro_phase_sample_trigger[0] =
                    (uint8_t)(((state->current_macro_record.flags & STPG_EVF_SAMPLE_POINT) != 0u) ? 1u : 0u);
                state->macro_phase_time_ticks[1] = state->current_macro_record.phase_ticks;
                state->macro_phase_logical_complete[1] = 1u;
                state->macro_phase_updates_output[1] = 1u;
                break;
            case 3u:
                state->macro_phase_count = 1u;
                state->macro_phase_packed_value[0] = state->macro_packed_drive_value_cached;
                state->macro_phase_packed_enable[0] = state->macro_packed_drive_enable_cached;
                state->macro_phase_logical_complete[0] = 1u;
                state->macro_phase_updates_output[0] = 1u;
                break;
            case 4u:
                state->macro_phase_count = 1u;
                state->macro_phase_flags[0] = state->current_macro_record.flags;
                state->macro_phase_sample_trigger[0] =
                    (uint8_t)(((state->current_macro_record.flags & STPG_EVF_SAMPLE_POINT) != 0u) ? 1u : 0u);
                state->macro_phase_logical_complete[0] = 1u;
                break;
            case 5u:
                state->macro_phase_count = 1u;
                state->macro_phase_flags[0] = state->current_macro_record.flags;
                state->macro_phase_sample_trigger[0] =
                    (uint8_t)(((state->current_macro_record.flags & STPG_EVF_SAMPLE_POINT) != 0u) ? 1u : 0u);
                state->macro_phase_packed_value[0] = state->macro_packed_drive_value_cached;
                state->macro_phase_packed_enable[0] = state->macro_packed_drive_enable_cached;
                state->macro_phase_logical_complete[0] = 1u;
                state->macro_phase_updates_output[0] = 1u;
                break;
            default:
                return ERR_UNSUPPORTED;
        }
        for (uint32_t cache_phase = 0u; cache_phase < state->macro_phase_count; ++cache_phase) {
            const bool cache_sample_trigger = state->macro_phase_sample_trigger[cache_phase] != 0u;
            const uint16_t cache_time_ticks = state->macro_phase_time_ticks[cache_phase];
            state->macro_phase_initial_desired_delta[cache_phase] = cache_time_ticks;
            state->macro_phase_intra_desired_delta[cache_phase] = cache_time_ticks;
            stpg_macro_prepare_wait_cache_entry(
                cache_time_ticks,
                cache_sample_trigger,
                &state->macro_phase_initial_wait_word[cache_phase],
                &state->macro_phase_initial_required_ticks[cache_phase],
                &state->macro_phase_initial_wait_valid[cache_phase]);
            stpg_macro_prepare_wait_cache_entry(
                /* Intra-phase deltas are only used for macro families whose phase0 time is fixed at 0. */
                cache_time_ticks,
                cache_sample_trigger,
                &state->macro_phase_intra_wait_word[cache_phase],
                &state->macro_phase_intra_required_ticks[cache_phase],
                &state->macro_phase_intra_wait_valid[cache_phase]);
        }
        if (state->macro_phase_count > 0u) {
            const uint32_t phase0_cycle_delta =
                (uint32_t)state->current_macro_record.period_ticks - (uint32_t)state->macro_phase_time_ticks[state->macro_phase_count - 1u] +
                (uint32_t)state->macro_phase_time_ticks[0];
            state->macro_phase_cycle_desired_delta[0] = (uint16_t)phase0_cycle_delta;
            stpg_macro_prepare_wait_cache_entry(
                (uint16_t)phase0_cycle_delta,
                state->macro_phase_sample_trigger[0] != 0u,
                &state->macro_phase_cycle_wait_word[0],
                &state->macro_phase_cycle_required_ticks[0],
                &state->macro_phase_cycle_wait_valid[0]);
        }
        for (uint32_t cache_phase = 1u; cache_phase < state->macro_phase_count; ++cache_phase) {
            const uint16_t phase_delta =
                (uint16_t)((uint32_t)state->macro_phase_time_ticks[cache_phase] -
                           (uint32_t)state->macro_phase_time_ticks[cache_phase - 1u]);
            state->macro_phase_cycle_desired_delta[cache_phase] = phase_delta;
            stpg_macro_prepare_wait_cache_entry(
                phase_delta,
                state->macro_phase_sample_trigger[cache_phase] != 0u,
                &state->macro_phase_cycle_wait_word[cache_phase],
                &state->macro_phase_cycle_required_ticks[cache_phase],
                &state->macro_phase_cycle_wait_valid[cache_phase]);
        }
        state->macro_repeat_index = 0u;
        state->macro_event_phase = 0u;
        state->macro_record_loaded = true;
    }

    if (state->current_macro_record.family == 1u || state->current_macro_record.family == 2u) {
        return stpg_buffer_stream_emit_next_macro_event_family12(state, dst_words, out_sample_trigger, out_emitted);
    }

#ifdef PICO_ON_DEVICE
    const uint32_t start_us = time_us_32();
#endif
    const MacroVectorRecord *record = &state->current_macro_record;
    const uint32_t phase_index = state->macro_event_phase;
    if (phase_index >= state->macro_phase_count) {
        return ERR_BAD_STATE;
    }
    const uint16_t time_ticks = state->macro_phase_time_ticks[phase_index];
    const bool sample_trigger = state->macro_phase_sample_trigger[phase_index] != 0u;
    const bool logical_vector_complete = state->macro_phase_logical_complete[phase_index] != 0u;
    const bool updates_output = state->macro_phase_updates_output[phase_index] != 0u;
    const uint32_t packed_out_value = state->macro_phase_packed_value[phase_index];
    const uint32_t packed_out_enable = state->macro_phase_packed_enable[phase_index];

    uint32_t wait_word = 0u;
    uint32_t required_ticks = 0u;
    uint32_t desired_delta_ticks = 0u;
    bool wait_valid = false;
#ifdef PICO_ON_DEVICE
    const uint32_t wait_compute_start_us = time_us_32();
#endif
    if (!state->have_prev_time) {
        desired_delta_ticks = state->macro_phase_initial_desired_delta[phase_index];
        required_ticks = state->macro_phase_initial_required_ticks[phase_index];
        wait_word = state->macro_phase_initial_wait_word[phase_index];
        wait_valid = state->macro_phase_initial_wait_valid[phase_index] != 0u;
    } else if (state->first_in_cycle) {
        desired_delta_ticks = state->macro_phase_cycle_desired_delta[phase_index];
        required_ticks = state->macro_phase_cycle_required_ticks[phase_index];
        wait_word = state->macro_phase_cycle_wait_word[phase_index];
        wait_valid = state->macro_phase_cycle_wait_valid[phase_index] != 0u;
    } else {
        desired_delta_ticks = state->macro_phase_intra_desired_delta[phase_index];
        required_ticks = state->macro_phase_intra_required_ticks[phase_index];
        wait_word = state->macro_phase_intra_wait_word[phase_index];
        wait_valid = state->macro_phase_intra_wait_valid[phase_index] != 0u;
    }
#ifdef PICO_ON_DEVICE
    g_stream_telemetry.macro_wait_compute_total_us += time_us_32() - wait_compute_start_us;
#endif
    if (!wait_valid) {
        stpg_stream_record_rate_mismatch(
            STPG_MISMATCH_STAGE_LEGACY_SLOT_MERGE,
            0u,
            state->macro_record_index,
            desired_delta_ticks,
            required_ticks,
            record->period_ticks,
            state->prev_time_ticks,
            time_ticks);
        return ERR_RATE_PERIOD_MISMATCH;
    }

#ifdef PICO_ON_DEVICE
    const uint32_t build_event_start_us = time_us_32();
#endif
    dst_words[0] = (uint32_t)wait_word;
    dst_words[1] = sample_trigger ? 1u : 0u;
    dst_words[2] = packed_out_value;
    dst_words[3] = packed_out_enable;
#ifdef PICO_ON_DEVICE
    g_stream_telemetry.macro_build_event_total_us += time_us_32() - build_event_start_us;
#endif

    if (g_stream_compare_collecting) {
#ifdef PICO_ON_DEVICE
        const uint32_t compare_start_us = time_us_32();
#endif
        const uint32_t event_index = g_stream_emitted_event_count;
        if (sample_trigger) {
            if (stpg_stream_compare_capacity_exhausted(g_stream_compare_event_count)) {
                return ERR_BAD_SIZE;
            }
            const uint32_t compare_index = g_stream_compare_event_count;
            const uint32_t storage_index = stpg_stream_compare_storage_index(compare_index);
            g_pio_words[storage_index] = event_index;
            g_expanded_events[storage_index] = state->macro_compare_proto;
            g_stream_compare_event_count = compare_index + 1u;
        }
        g_stream_emitted_event_count += 1u;
#ifdef PICO_ON_DEVICE
        const uint32_t compare_elapsed_us = time_us_32() - compare_start_us;
        g_stream_telemetry.compare_bookkeeping_total_us += compare_elapsed_us;
        g_stream_telemetry.macro_compare_write_total_us += compare_elapsed_us;
#endif
    }

#ifdef PICO_ON_DEVICE
    const uint32_t state_commit_start_us = time_us_32();
#endif
    state->have_prev_time = true;
    state->prev_time_ticks = time_ticks;
    state->prev_period_ticks = record->period_ticks;
    state->first_in_cycle = false;
    if (state->macro_phase_count > 1u) {
        state->macro_event_phase = (uint8_t)(phase_index + 1u);
        if (state->macro_event_phase >= state->macro_phase_count) {
            state->macro_event_phase = 0u;
        }
    }
    if (updates_output) {
        state->current_value = state->macro_drive_value_cached;
        state->current_dir = state->macro_drive_enable_cached;
    }
#ifdef PICO_ON_DEVICE
    g_stream_telemetry.macro_state_commit_total_us += time_us_32() - state_commit_start_us;
#endif
#if STPG_STREAM_HOT_TELEMETRY
    g_stream_telemetry.emit_call_count += 1u;
    if (sample_trigger) {
        g_stream_telemetry.sample_event_count += 1u;
    }
#endif

    if (logical_vector_complete) {
#ifdef PICO_ON_DEVICE
        const uint32_t repeat_advance_start_us = time_us_32();
#endif
        state->vector_index += 1u;
        state->macro_repeat_index += 1u;
        state->first_in_cycle = true;
        if (state->macro_repeat_index >= record->repeat) {
            state->macro_record_index += 1u;
            state->macro_repeat_index = 0u;
            state->macro_record_loaded = false;
            if (state->macro_record_index >= state->macro_header.record_count) {
                state->eof = true;
            }
        }
#ifdef PICO_ON_DEVICE
        g_stream_telemetry.macro_repeat_advance_total_us += time_us_32() - repeat_advance_start_us;
#endif
    }

#ifdef PICO_ON_DEVICE
    g_stream_telemetry.emit_total_us += time_us_32() - start_us;
#endif
    if (out_sample_trigger != 0) *out_sample_trigger = sample_trigger;
    if (out_emitted != 0) *out_emitted = true;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_stream_fill_next_chunk_macro_fast_repeat_span(stpg_rate_stream_state_t *state,
                                                                            uint32_t *dst_words,
                                                                            uint32_t max_events,
                                                                            stpg_stream_chunk_result_t *out_result,
                                                                            bool *out_used_fast_path) {
    if (out_used_fast_path != 0) {
        *out_used_fast_path = false;
    }
    if (state == 0 || dst_words == 0 || out_result == 0 || max_events == 0u) {
        return ERR_BAD_SIZE;
    }
    if (!state->macro_record_loaded || !state->have_prev_time || !state->first_in_cycle || state->eof) {
        return ERR_NONE;
    }

    const MacroVectorRecord *record = &state->current_macro_record;
    const uint32_t remaining_repeats = (uint32_t)record->repeat - state->macro_repeat_index;
    if (remaining_repeats == 0u) {
        return ERR_NONE;
    }

    uint32_t events_per_repeat = 0u;
    uint32_t sample_events_per_repeat = 0u;
    uint32_t wait0 = 0u;
    uint32_t wait1 = 0u;
    uint32_t time_ticks_terminal = 0u;
    bool phase0_sample = false;
    bool phase1_sample = false;
    uint32_t packed_value0 = 0u;
    uint32_t packed_enable0 = 0u;
    uint32_t packed_value1 = 0u;
    uint32_t packed_enable1 = 0u;

    switch (record->family) {
        case 1u:
            events_per_repeat = 2u;
            sample_events_per_repeat = 1u;
            if (record->period_ticks < record->phase_ticks + STPG_BATCH_WAIT_OVERHEAD_NO_SAMPLE) {
                stpg_stream_record_rate_mismatch(
                    STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
                    0u,
                    state->macro_record_index,
                    (uint32_t)record->period_ticks - (uint32_t)record->phase_ticks,
                    STPG_BATCH_WAIT_OVERHEAD_NO_SAMPLE,
                    record->period_ticks,
                    0u,
                    record->phase_ticks);
                return ERR_RATE_PERIOD_MISMATCH;
            }
            if (record->phase_ticks < STPG_BATCH_WAIT_OVERHEAD_SAMPLE) {
                stpg_stream_record_rate_mismatch(
                    STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
                    0u,
                    state->macro_record_index,
                    record->phase_ticks,
                    STPG_BATCH_WAIT_OVERHEAD_SAMPLE,
                    record->period_ticks,
                    0u,
                    record->phase_ticks);
                return ERR_RATE_PERIOD_MISMATCH;
            }
            wait0 = (uint32_t)record->period_ticks - (uint32_t)record->phase_ticks - STPG_BATCH_WAIT_OVERHEAD_NO_SAMPLE;
            wait1 = (uint32_t)record->phase_ticks - STPG_BATCH_WAIT_OVERHEAD_SAMPLE;
            time_ticks_terminal = record->phase_ticks;
            packed_value0 = state->macro_packed_drive_value_cached;
            packed_enable0 = state->macro_packed_drive_enable_cached;
            packed_value1 = state->macro_packed_drive_value_cached;
            packed_enable1 = state->macro_packed_drive_enable_cached;
            phase1_sample = true;
            break;
        case 2u:
            events_per_repeat = 2u;
            sample_events_per_repeat = 1u;
            if (record->period_ticks < record->phase_ticks + STPG_BATCH_WAIT_OVERHEAD_SAMPLE) {
                stpg_stream_record_rate_mismatch(
                    STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
                    0u,
                    state->macro_record_index,
                    (uint32_t)record->period_ticks - (uint32_t)record->phase_ticks,
                    STPG_BATCH_WAIT_OVERHEAD_SAMPLE,
                    record->period_ticks,
                    0u,
                    record->phase_ticks);
                return ERR_RATE_PERIOD_MISMATCH;
            }
            if (record->phase_ticks < STPG_BATCH_WAIT_OVERHEAD_NO_SAMPLE) {
                stpg_stream_record_rate_mismatch(
                    STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
                    0u,
                    state->macro_record_index,
                    record->phase_ticks,
                    STPG_BATCH_WAIT_OVERHEAD_NO_SAMPLE,
                    record->period_ticks,
                    0u,
                    record->phase_ticks);
                return ERR_RATE_PERIOD_MISMATCH;
            }
            wait0 = (uint32_t)record->period_ticks - (uint32_t)record->phase_ticks - STPG_BATCH_WAIT_OVERHEAD_SAMPLE;
            wait1 = (uint32_t)record->phase_ticks - STPG_BATCH_WAIT_OVERHEAD_NO_SAMPLE;
            time_ticks_terminal = record->phase_ticks;
            packed_value0 = stpg_stream_pack_logical_to_window_fast(state->current_value, state->macro_header.active_pins);
            packed_enable0 = stpg_stream_pack_logical_to_window_fast(state->current_dir, state->macro_header.active_pins);
            packed_value1 = state->macro_packed_drive_value_cached;
            packed_enable1 = state->macro_packed_drive_enable_cached;
            phase0_sample = true;
            break;
        case 3u:
            events_per_repeat = 1u;
            sample_events_per_repeat = 0u;
            if (record->period_ticks < STPG_BATCH_WAIT_OVERHEAD_NO_SAMPLE) {
                stpg_stream_record_rate_mismatch(
                    STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
                    0u,
                    state->macro_record_index,
                    record->period_ticks,
                    STPG_BATCH_WAIT_OVERHEAD_NO_SAMPLE,
                    record->period_ticks,
                    0u,
                    0u);
                return ERR_RATE_PERIOD_MISMATCH;
            }
            wait0 = (uint32_t)record->period_ticks - STPG_BATCH_WAIT_OVERHEAD_NO_SAMPLE;
            packed_value0 = state->macro_packed_drive_value_cached;
            packed_enable0 = state->macro_packed_drive_enable_cached;
            break;
        case 4u:
            events_per_repeat = 1u;
            sample_events_per_repeat = 1u;
            if (record->period_ticks < STPG_BATCH_WAIT_OVERHEAD_SAMPLE) {
                stpg_stream_record_rate_mismatch(
                    STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
                    0u,
                    state->macro_record_index,
                    record->period_ticks,
                    STPG_BATCH_WAIT_OVERHEAD_SAMPLE,
                    record->period_ticks,
                    0u,
                    0u);
                return ERR_RATE_PERIOD_MISMATCH;
            }
            wait0 = (uint32_t)record->period_ticks - STPG_BATCH_WAIT_OVERHEAD_SAMPLE;
            packed_value0 = stpg_stream_pack_logical_to_window_fast(state->current_value, state->macro_header.active_pins);
            packed_enable0 = stpg_stream_pack_logical_to_window_fast(state->current_dir, state->macro_header.active_pins);
            phase0_sample = true;
            break;
        case 5u:
            events_per_repeat = 1u;
            sample_events_per_repeat = 1u;
            if (record->period_ticks < STPG_BATCH_WAIT_OVERHEAD_SAMPLE) {
                stpg_stream_record_rate_mismatch(
                    STPG_MISMATCH_STAGE_BATCH_WAIT_FAST_SPAN,
                    0u,
                    state->macro_record_index,
                    record->period_ticks,
                    STPG_BATCH_WAIT_OVERHEAD_SAMPLE,
                    record->period_ticks,
                    0u,
                    0u);
                return ERR_RATE_PERIOD_MISMATCH;
            }
            wait0 = (uint32_t)record->period_ticks - STPG_BATCH_WAIT_OVERHEAD_SAMPLE;
            packed_value0 = state->macro_packed_drive_value_cached;
            packed_enable0 = state->macro_packed_drive_enable_cached;
            phase0_sample = true;
            break;
        default:
            return ERR_NONE;
    }

    uint32_t repeat_capacity = max_events / events_per_repeat;
    if (repeat_capacity == 0u) {
        return ERR_NONE;
    }
    if (sample_events_per_repeat != 0u) {
        if (stpg_stream_compare_capacity_exhausted(g_stream_compare_event_count)) {
            return ERR_BAD_SIZE;
        }
        const uint32_t compare_capacity =
            STPG_MAX_EVENT_COUNT - g_stream_compare_event_base - g_stream_compare_event_count;
        const uint32_t compare_repeat_capacity = compare_capacity / sample_events_per_repeat;
        if (compare_repeat_capacity < repeat_capacity) {
            repeat_capacity = compare_repeat_capacity;
        }
    }
    if (repeat_capacity == 0u) {
        return ERR_BAD_SIZE;
    }
    uint32_t repeats_to_emit = remaining_repeats < repeat_capacity ? remaining_repeats : repeat_capacity;
    if (repeats_to_emit == 0u) {
        return ERR_NONE;
    }

#ifdef PICO_ON_DEVICE
    const uint32_t build_start_us = time_us_32();
#endif
    uint32_t emitted_event_index = g_stream_emitted_event_count;
    uint32_t compare_index = g_stream_compare_event_count;
    uint32_t *dst_cursor = dst_words;
    const ExecEvent compare_proto = state->macro_compare_proto;

    if (events_per_repeat == 2u) {
        if (phase0_sample) {
            uint32_t current_event_index = emitted_event_index;
            for (uint32_t repeat_i = 0u; repeat_i < repeats_to_emit; ++repeat_i) {
                dst_cursor[0] = wait0;
                dst_cursor[1] = 1u;
                dst_cursor[2] = packed_value0;
                dst_cursor[3] = packed_enable0;
                const uint32_t storage_index = stpg_stream_compare_storage_index(compare_index);
                g_pio_words[storage_index] = current_event_index;
                g_expanded_events[storage_index] = compare_proto;
                compare_index += 1u;

                dst_cursor[4] = wait1;
                dst_cursor[5] = 0u;
                dst_cursor[6] = packed_value1;
                dst_cursor[7] = packed_enable1;
                current_event_index += 2u;
                dst_cursor += 8u;
            }
            emitted_event_index = current_event_index;
        } else if (phase1_sample) {
            uint32_t current_event_index = emitted_event_index;
            for (uint32_t repeat_i = 0u; repeat_i < repeats_to_emit; ++repeat_i) {
                dst_cursor[0] = wait0;
                dst_cursor[1] = 0u;
                dst_cursor[2] = packed_value0;
                dst_cursor[3] = packed_enable0;

                dst_cursor[4] = wait1;
                dst_cursor[5] = 1u;
                dst_cursor[6] = packed_value1;
                dst_cursor[7] = packed_enable1;
                const uint32_t storage_index = stpg_stream_compare_storage_index(compare_index);
                g_pio_words[storage_index] = current_event_index + 1u;
                g_expanded_events[storage_index] = compare_proto;
                compare_index += 1u;
                current_event_index += 2u;
                dst_cursor += 8u;
            }
            emitted_event_index = current_event_index;
        } else {
            for (uint32_t repeat_i = 0u; repeat_i < repeats_to_emit; ++repeat_i) {
                dst_cursor[0] = wait0;
                dst_cursor[1] = 0u;
                dst_cursor[2] = packed_value0;
                dst_cursor[3] = packed_enable0;
                dst_cursor[4] = wait1;
                dst_cursor[5] = 0u;
                dst_cursor[6] = packed_value1;
                dst_cursor[7] = packed_enable1;
                emitted_event_index += 2u;
                dst_cursor += 8u;
            }
        }
    } else {
        const uint32_t sample_word = phase0_sample ? 1u : 0u;
        if (phase0_sample) {
            uint32_t current_event_index = emitted_event_index;
            for (uint32_t repeat_i = 0u; repeat_i < repeats_to_emit; ++repeat_i) {
                dst_cursor[0] = wait0;
                dst_cursor[1] = sample_word;
                dst_cursor[2] = packed_value0;
                dst_cursor[3] = packed_enable0;
                const uint32_t storage_index = stpg_stream_compare_storage_index(compare_index);
                g_pio_words[storage_index] = current_event_index;
                g_expanded_events[storage_index] = compare_proto;
                compare_index += 1u;
                current_event_index += 1u;
                dst_cursor += 4u;
            }
            emitted_event_index = current_event_index;
        } else {
            for (uint32_t repeat_i = 0u; repeat_i < repeats_to_emit; ++repeat_i) {
                dst_cursor[0] = wait0;
                dst_cursor[1] = sample_word;
                dst_cursor[2] = packed_value0;
                dst_cursor[3] = packed_enable0;
                emitted_event_index += 1u;
                dst_cursor += 4u;
            }
        }
    }
    const uint32_t emitted = repeats_to_emit * events_per_repeat;
    const uint32_t sample_events = repeats_to_emit * sample_events_per_repeat;
    g_stream_emitted_event_count = emitted_event_index;
    g_stream_compare_event_count = compare_index;
#ifdef PICO_ON_DEVICE
    const uint32_t build_elapsed_us = time_us_32() - build_start_us;
    g_stream_telemetry.macro_build_event_total_us += build_elapsed_us;
    g_stream_telemetry.macro_compare_write_total_us += build_elapsed_us;
    g_stream_telemetry.compare_bookkeeping_total_us += build_elapsed_us;
#endif
#if STPG_STREAM_HOT_TELEMETRY
    g_stream_telemetry.emit_call_count += emitted;
    g_stream_telemetry.sample_event_count += sample_events;
#endif

    state->vector_index += repeats_to_emit;
    state->macro_repeat_index += repeats_to_emit;
    state->prev_time_ticks = (uint16_t)time_ticks_terminal;
    state->prev_period_ticks = record->period_ticks;
    state->first_in_cycle = true;
    if ((record->family == 1u || record->family == 2u || record->family == 3u || record->family == 5u) && state->macro_repeat_index != 0u) {
        state->current_value = state->macro_drive_value_cached;
        state->current_dir = state->macro_drive_enable_cached;
    }
    if (state->macro_repeat_index >= record->repeat) {
        state->macro_record_index += 1u;
        state->macro_repeat_index = 0u;
        state->macro_record_loaded = false;
        state->macro_event_phase = 0u;
        if (state->macro_record_index >= state->macro_header.record_count) {
            state->eof = true;
        }
    }

    out_result->event_count += emitted;
    out_result->sample_event_count += sample_events;
    out_result->word_count += emitted * 4u;
    out_result->eof = state->eof;
    if (out_used_fast_path != 0) {
        *out_used_fast_path = true;
    }
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_stream_fill_next_chunk_macro(stpg_rate_stream_state_t *state,
                                                           uint32_t *dst_words,
                                                           uint32_t max_events,
                                                           stpg_stream_chunk_result_t *out_result) {
    if (state == 0 || dst_words == 0 || out_result == 0 || max_events == 0u) {
        return ERR_BAD_SIZE;
    }
    memset(out_result, 0, sizeof(*out_result));
    if (state->eof) {
        out_result->eof = true;
        return ERR_NONE;
    }

    uint32_t emitted_count = 0u;
    uint32_t sample_event_count = 0u;
    stpg_err_t err = ERR_NONE;
    while (emitted_count < max_events && !state->eof) {
        stpg_stream_chunk_result_t fast_result = {0};
        bool used_fast_path = false;
        err = stpg_buffer_stream_fill_next_chunk_macro_fast_repeat_span(
            state, &dst_words[emitted_count * 4u], max_events - emitted_count, &fast_result, &used_fast_path);
        if (err != ERR_NONE) {
            return err;
        }
        if (used_fast_path) {
            emitted_count += fast_result.event_count;
            sample_event_count += fast_result.sample_event_count;
            out_result->word_count += fast_result.word_count;
            out_result->eof = fast_result.eof;
            continue;
        }
        bool emitted = false;
        bool sample_trigger = false;
        err = stpg_buffer_stream_emit_next_macro_event(
            state, &dst_words[emitted_count * 4u], &sample_trigger, &emitted);
        if (err != ERR_NONE) {
            break;
        }
        if (!emitted) {
            break;
        }
        if (sample_trigger) {
            sample_event_count += 1u;
        }
        emitted_count += 1u;
    }
    if (err != ERR_NONE) {
        return err;
    }
    out_result->event_count = emitted_count;
    out_result->sample_event_count = sample_event_count;
    out_result->word_count = emitted_count * 4u;
    out_result->eof = state->eof;
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_stream_note_p4_end_marker(const stpg_rate_stream_state_t *state,
                                                        uint32_t vector_start,
                                                        uint32_t vector_end,
                                                        stpg_stream_chunk_result_t *out_result) {
    if (state == 0 || out_result == 0 || state->stream_kind != STPG_STREAM_KIND_RATE_VECTOR
        || (state->header.flags & STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4) == 0u) {
        return ERR_NONE;
    }
    for (uint32_t vector_index = vector_start; vector_index < vector_end; ++vector_index) {
        VectorRecord vector;
        stpg_err_t err = stpg_buffer_read_vector_record(&state->header, vector_index, &vector);
        if (err != ERR_NONE) return err;
        if (vector.flags == STPG_VECTOR_FLAG_EXPLICIT_WAIT_P4_END) {
            out_result->p4_explicit_wait_end_present = true;
            out_result->p4_explicit_wait_end_vector_index = vector_index;
            return ERR_NONE;
        }
    }
    return ERR_NONE;
}

stpg_err_t STPG_STREAM_PRODUCER_FUNC(stpg_buffer_stream_fill_next_chunk)(stpg_rate_stream_state_t *state, uint32_t *dst_words,
                                              uint32_t max_events, stpg_stream_chunk_result_t *out_result) {
    if (state == 0 || dst_words == 0 || out_result == 0 || max_events == 0u) {
        return ERR_BAD_SIZE;
    }

    const uint32_t vector_start = state->vector_index;
    const uint32_t emitted_start = g_stream_emitted_event_count;
    const stpg_stream_hotspot_snapshot_t hotspot_before = stpg_stream_hotspot_snapshot();
    stpg_err_t err = ERR_NONE;

    if (state->stream_kind == STPG_STREAM_KIND_MACRO_STREAM) {
        err = stpg_buffer_stream_fill_next_chunk_macro(state, dst_words, max_events, out_result);
    } else if (state->batch_path_enabled) {
        err = stpg_buffer_stream_fill_next_chunk_batch(state, dst_words, max_events, out_result);
    } else {
        memset(out_result, 0, sizeof(*out_result));
        if (state->eof) {
            out_result->eof = true;
            return ERR_NONE;
        }

        uint32_t emitted_count = 0u;
        uint32_t sample_event_count = 0u;
        while (emitted_count < max_events && !state->eof) {
            bool emitted = false;
            bool sample_trigger = false;
            err = stpg_buffer_stream_emit_next_event(state,
                                                     &dst_words[emitted_count * 4u],
                                                     0,
                                                     &sample_trigger,
                                                     &emitted);
            if (err != ERR_NONE) break;
            if (!emitted) break;
            if (sample_trigger) {
                sample_event_count += 1u;
            }
            emitted_count += 1u;
        }

        if (err == ERR_NONE) {
            out_result->event_count = emitted_count;
            out_result->sample_event_count = sample_event_count;
            out_result->word_count = emitted_count * 4u;
            out_result->eof = state->eof;
        }
    }
    if (err == ERR_NONE && out_result->event_count > 0u) {
        err = stpg_buffer_stream_note_p4_end_marker(state, vector_start, state->vector_index, out_result);
    }
    if (err == ERR_NONE && out_result->event_count > 0u) {
        const stpg_stream_hotspot_snapshot_t hotspot_after = stpg_stream_hotspot_snapshot();
        stpg_stream_hotspot_record(vector_start,
                                   state->vector_index,
                                   emitted_start,
                                   g_stream_emitted_event_count,
                                   &hotspot_before,
                                   &hotspot_after);
    }
    return err;
}

stpg_err_t STPG_STREAM_PRODUCER_FUNC(stpg_buffer_stream_fill_next_chunk_reduced_compare)(stpg_rate_stream_state_t *state, uint32_t *dst_words,
                                                              uint32_t *dst_sample_waits,
                                                              uint32_t max_events,
                                                              stpg_stream_chunk_result_t *out_result) {
    if (state == 0 || !state->reduced_density_compare_enabled) {
        return ERR_UNSUPPORTED;
    }
    const uint32_t vector_start = state->vector_index;
    const uint32_t emitted_start = g_stream_emitted_event_count;
    const stpg_stream_hotspot_snapshot_t hotspot_before = stpg_stream_hotspot_snapshot();
    stpg_err_t err = stpg_buffer_stream_fill_next_chunk_reduced_compare_impl(
        state, dst_words, dst_sample_waits, max_events, out_result);
    if (err == ERR_NONE && out_result->event_count > 0u) {
        err = stpg_buffer_stream_note_p4_end_marker(state, vector_start, state->vector_index, out_result);
    }
    if (err == ERR_NONE && out_result->event_count > 0u) {
        const stpg_stream_hotspot_snapshot_t hotspot_after = stpg_stream_hotspot_snapshot();
        stpg_stream_hotspot_record(vector_start,
                                   state->vector_index,
                                   emitted_start,
                                   g_stream_emitted_event_count,
                                   &hotspot_before,
                                   &hotspot_after);
    }
    return err;
}

const ExecEvent *stpg_buffer_events(void) {
    if (!g_has_pattern) {
        return 0;
    }
    if (g_use_expanded_event_view) {
        return g_expanded_events;
    }
    return (const ExecEvent *)(const void *)(g_pattern_buffer + STPG_HEADER_SIZE);
}

const uint32_t *stpg_buffer_pio_words(void) {
    return g_pio_words;
}

uint32_t stpg_buffer_pio_word_count(void) {
    return g_pio_word_count;
}

stpg_pio_word_view_t stpg_buffer_pio_word_view(void) {
    stpg_pio_word_view_t view = {
        .words = g_pio_words,
        .word_count = g_pio_word_count,
        .event_count = g_event_count,
        .sample_event_count = g_sample_event_count,
    };
    return view;
}

const ExecEvent *stpg_buffer_stream_compare_events(void) {
    return g_expanded_events;
}

const uint32_t *stpg_buffer_stream_compare_event_indices(void) {
    return g_pio_words;
}

uint32_t stpg_buffer_stream_compare_event_count(void) {
    return g_stream_compare_event_count;
}

stpg_err_t STPG_STREAM_PRODUCER_FUNC(stpg_buffer_stream_compare_begin_metadata_slot)(
    uint32_t destination_offset) {
    if (destination_offset > STPG_MAX_EVENT_COUNT
        || STPG_STREAM_BUFFER_EVENTS > STPG_MAX_EVENT_COUNT - destination_offset) {
        return ERR_FAIL_STOP_METADATA_OVERRUN;
    }
    g_stream_compare_event_base = destination_offset;
    g_stream_compare_event_count = 0u;
    return ERR_NONE;
}

const ExecEvent *stpg_buffer_stream_compare_events_at(uint32_t offset) {
    if (offset >= STPG_MAX_EVENT_COUNT) {
        return 0;
    }
    return &g_expanded_events[offset];
}

const uint32_t *stpg_buffer_stream_compare_event_indices_at(uint32_t offset) {
    if (offset >= STPG_MAX_EVENT_COUNT) {
        return 0;
    }
    return &g_pio_words[offset];
}

bool stpg_buffer_stream_has_samples(void) {
    return g_stream_has_samples;
}

uint32_t stpg_buffer_stream_expected_sample_count(void) {
    if (!g_stream_has_samples) {
        return 0u;
    }
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_COMPARE_SAMPLE_REPAIR || STPG_STREAM_FAIL_STOP
    if (g_stream_expected_sample_count != 0u) {
        return g_stream_expected_sample_count;
    }
#endif
    return g_rate_info.expanded_vector_count_estimate;
}

void stpg_buffer_snapshot_stream_telemetry(void) {
    g_stream_telemetry_snapshot = g_stream_telemetry;
    g_stream_telemetry_snapshot_valid = true;
}

void stpg_buffer_clear_stream_telemetry_snapshot(void) {
    memset(&g_stream_telemetry_snapshot, 0, sizeof(g_stream_telemetry_snapshot));
    g_stream_telemetry_snapshot_valid = false;
}

void stpg_buffer_get_stream_telemetry(stpg_stream_buffer_telemetry_t *out) {
    if (out == 0) {
        return;
    }
    *out = g_stream_telemetry_snapshot_valid ? g_stream_telemetry_snapshot : g_stream_telemetry;
}

void stpg_buffer_get_stream_hotspot_meta(stpg_stream_hotspot_meta_t *out) {
    if (out == 0) {
        return;
    }
    *out = g_stream_hotspot_meta;
    const uint32_t valid_rank_count = stpg_stream_hotspot_valid_rank_count();
    out->recorded_chunk_count = valid_rank_count;
    out->hottest_chunk_rank_count = valid_rank_count;
}

bool stpg_buffer_get_stream_hotspot_chunk(uint32_t rank, stpg_stream_hotspot_entry_t *out) {
    if (out == 0 || rank >= STPG_STREAM_HOTSPOT_MAX_RANKS) {
        return false;
    }
    if (!stpg_stream_hotspot_entry_valid(&g_stream_hotspot_chunks[rank])) {
        return false;
    }
    *out = g_stream_hotspot_chunks[rank];
    return true;
}

uint32_t *stpg_buffer_streaming_scratch_words(void) {
    return (uint32_t *)(void *)g_pattern_buffer;
}

uint32_t stpg_buffer_streaming_scratch_word_capacity(void) {
    return STPG_MAX_PATTERN_BYTES / sizeof(uint32_t);
}

stpg_err_t stpg_buffer_psram_checksum(uint32_t offset, uint32_t size, uint32_t *out_crc32) {
    stpg_psram_init();
    if (!stpg_psram_is_available()) return ERR_PSRAM_UNAVAILABLE;
    return stpg_psram_checksum(offset, size, out_crc32);
}

stpg_err_t stpg_buffer_psram_read_prepare(uint32_t offset, uint32_t size, uint32_t *out_crc32) {
    stpg_psram_init();
    if (!stpg_psram_is_available()) return ERR_PSRAM_UNAVAILABLE;
    return stpg_psram_checksum(offset, size, out_crc32);
}

stpg_err_t stpg_buffer_psram_read_bytes(uint32_t offset, void *dst, uint32_t size) {
    stpg_psram_init();
    if (!stpg_psram_is_available()) return ERR_PSRAM_UNAVAILABLE;
    return stpg_psram_read(offset, dst, size);
}

stpg_err_t stpg_buffer_psram_erase(uint32_t offset, uint32_t size) {
    stpg_psram_init();
    if (!stpg_psram_is_available()) return ERR_PSRAM_UNAVAILABLE;

    if (size == 0u) {
        offset = STPG_PSRAM_PATTERN_BASE;
        size = STPG_PSRAM_PATTERN_BYTES;
    }
    stpg_err_t err = stpg_psram_fill(offset, 0u, size);
    if (err != ERR_NONE) return err;

    memset(&g_psram_slot, 0, sizeof(g_psram_slot));
    err = stpg_psram_write(STPG_PSRAM_META_BASE, &g_psram_slot, sizeof(g_psram_slot));
    if (err != ERR_NONE) return err;

    if (g_active_source == STPG_PATTERN_SOURCE_PSRAM) {
        g_active_source = STPG_PATTERN_SOURCE_NONE;
        g_has_pattern = false;
        g_use_expanded_event_view = false;
        g_pattern_size = 0u;
        g_event_count = 0u;
        g_sample_event_count = 0u;
        g_pio_word_count = 0u;
    }
    memset(&g_rate_info, 0, sizeof(g_rate_info));
    return ERR_NONE;
}

stpg_err_t stpg_buffer_flash_list(uint32_t *out_valid_bitmap, uint32_t *out_slot_count, uint32_t *out_slot_size) {
    return stpg_flash_list_slots(out_valid_bitmap, out_slot_count, out_slot_size);
}

stpg_err_t stpg_buffer_flash_get_slot_info(uint32_t slot_id, stpg_flash_slot_view_t *out_view) {
    if (out_view == 0) {
        return ERR_BAD_SIZE;
    }
    stpg_flash_slot_info_t info;
    stpg_err_t err = stpg_flash_get_slot_info(slot_id, &info);
    if (err != ERR_NONE) {
        return err;
    }
    out_view->valid = info.valid;
    out_view->pattern_size = info.pattern_size;
    out_view->format = info.format;
    out_view->active_pins = info.active_pins;
    out_view->tick_hz = info.tick_hz;
    return ERR_NONE;
}

stpg_err_t stpg_buffer_flash_select_run(uint32_t slot_id) {
    stpg_err_t err = stpg_flash_get_slot_meta(slot_id, &g_flash_pending_meta);
    if (err != ERR_NONE) {
        return err;
    }
    g_flash_selected_run_slot = slot_id;
    g_flash_selected_run_slot_valid = true;
    memcpy(&g_flash_selected_run_meta, &g_flash_pending_meta, sizeof(g_flash_selected_run_meta));
    g_pattern_size = g_flash_selected_run_meta.pattern_size;
    g_event_count = g_flash_selected_run_meta.format;
    g_sample_event_count = 0u;
    g_pio_word_count = 0u;
    memset(&g_rate_info, 0, sizeof(g_rate_info));
    g_use_expanded_event_view = false;
    stpg_buffer_set_active_source(STPG_PATTERN_SOURCE_FLASH);
    return ERR_NONE;
}

stpg_err_t stpg_buffer_flash_select_write(uint32_t slot_id) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (profile == 0 || slot_id >= profile->slot_count) {
        return ERR_BAD_SIZE;
    }
    g_flash_selected_write_slot = slot_id;
    g_flash_selected_write_slot_valid = true;
    g_flash_selected_write_slot_erased = false;
    return ERR_NONE;
}

stpg_err_t stpg_buffer_flash_erase_slot(uint32_t slot_id) {
    stpg_err_t err = stpg_flash_erase_slot(slot_id);
    if (err != ERR_NONE) {
        return err;
    }
    if (g_flash_selected_write_slot_valid && g_flash_selected_write_slot == slot_id) {
        g_flash_selected_write_slot_erased = true;
    }
    return ERR_NONE;
}

static stpg_err_t stpg_buffer_finalize_flash(uint32_t total_size, uint32_t total_crc) {
    uint32_t stored_crc = 0u;
    stpg_err_t err = stpg_psram_checksum(STPG_PSRAM_STAGING_BASE, total_size, &stored_crc);
    if (err != ERR_NONE) return err;
    if (stored_crc != total_crc) return ERR_BAD_CRC;
    if (!g_flash_selected_write_slot_valid || !g_flash_selected_write_slot_erased) return ERR_BAD_STATE;

    FlashSlotMeta *const meta = &g_flash_pending_meta;
    uint32_t entry_count = 0u;
    uint32_t aux_count = 0u;
    err = stpg_buffer_parse_flash_staging_meta(total_size, total_crc, meta, &entry_count, &aux_count);
    if (err != ERR_NONE) return err;
    err = stpg_flash_write_slot_payload_from_psram(g_flash_selected_write_slot, meta->pattern_offset,
                                                   total_size, STPG_PSRAM_STAGING_BASE, total_crc);
    if (err != ERR_NONE) return err;
    err = stpg_flash_write_slot_meta(g_flash_selected_write_slot, meta);
    if (err != ERR_NONE) return err;

    g_flash_selected_run_slot = g_flash_selected_write_slot;
    g_flash_selected_run_slot_valid = true;
    memcpy(&g_flash_selected_run_meta, meta, sizeof(g_flash_selected_run_meta));
    g_pattern_size = total_size;
    g_event_count = (meta->format == STPG_PATTERN_FORMAT_RATE_VECTOR) ? aux_count : entry_count;
    g_sample_event_count = 0u;
    g_pio_word_count = 0u;
    g_active_pins = meta->active_pins;
    g_use_expanded_event_view = false;
    memset(&g_rate_info, 0, sizeof(g_rate_info));
    g_flash_selected_write_slot_erased = false;
    stpg_buffer_set_active_source(STPG_PATTERN_SOURCE_FLASH);
    return ERR_NONE;
}

stpg_err_t stpg_buffer_get_rate_info(stpg_rate_info_t *out_info) {
    if (out_info == 0) {
        return ERR_BAD_SIZE;
    }
    memset(out_info, 0, sizeof(*out_info));
    if (!g_rate_info.loaded || g_active_source != STPG_PATTERN_SOURCE_PSRAM || g_psram_slot.format != STPG_PATTERN_FORMAT_RATE_VECTOR) {
        return ERR_NONE;
    }
    *out_info = g_rate_info;
    return ERR_NONE;
}

void stpg_buffer_get_measurement_debug(uint32_t *out_family_id,
                                       uint32_t *out_period_ticks,
                                       uint32_t *out_phase_ticks,
                                       uint32_t *out_cycle_count) {
    if (out_family_id != 0) {
        *out_family_id = g_measurement_family_id;
    }
    if (out_period_ticks != 0) {
        *out_period_ticks = g_measurement_period_ticks;
    }
    if (out_phase_ticks != 0) {
        *out_phase_ticks = g_measurement_phase_ticks;
    }
    if (out_cycle_count != 0) {
        *out_cycle_count = g_measurement_cycle_count;
    }
}
