#include "stpg_compare.h"

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_pattern_buffer.h"
#include "stpg_protocol.h"
#include "stpg_samples.h"

#ifndef STPG_STREAM_COMPARE_SAMPLE_REPAIR
#define STPG_STREAM_COMPARE_SAMPLE_REPAIR 0
#endif

static FailLogEntry g_fail_log[STPG_FAIL_LOG_MAX];
static uint32_t g_fail_count;
static uint32_t g_result_flags;

void stpg_compare_reset(void) {
    g_fail_count = 0u;
    g_result_flags = 0u;
}

static void stpg_compare_append_fail(uint32_t event_index, uint32_t actual, uint32_t expected, uint32_t mask) {
    if (g_fail_count < STPG_FAIL_LOG_MAX) {
        FailLogEntry *entry = &g_fail_log[g_fail_count];
        entry->event_index = event_index;
        entry->actual = actual;
        entry->expected = expected;
        entry->mask = mask;
    } else {
        g_result_flags |= STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW;
    }
    ++g_fail_count;
    g_result_flags |= STPG_RESULT_FLAG_COMPARE_MISMATCH;
}

bool stpg_compare_check_value(uint32_t event_index,
                              uint32_t actual,
                              uint32_t expected,
                              uint32_t mask) {
    const uint32_t fail_bits = (actual ^ expected) & mask;
    if (fail_bits == 0u) {
        return false;
    }
    stpg_compare_append_fail(event_index, actual, expected, mask);
    return true;
}

void stpg_compare_run(const ExecEvent *events, uint32_t event_count) {
    stpg_compare_reset();
    if (events == 0) {
        return;
    }

    uint32_t sample_index = 0u;
    const uint32_t sample_count = stpg_samples_sample_count();
    for (uint32_t event_index = 0; event_index < event_count; ++event_index) {
        const ExecEvent *event = &events[event_index];
        const bool has_sample = (event->flags & STPG_EVF_SAMPLE_POINT) != 0u;
        const bool has_compare = (event->flags & STPG_EVF_COMPARE_POINT) != 0u;
        if (!has_sample) {
            continue;
        }

        if (sample_index >= sample_count) {
            break;
        }

        if (has_compare) {
            const uint32_t actual = stpg_samples_value_at(sample_index);
            const uint32_t logical_mask = stpg_samples_active_pins() == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
            const uint32_t expected = event->expect_value & logical_mask;
            const uint32_t mask = event->sample_mask & logical_mask;
            (void)stpg_compare_check_value(event_index, actual, expected, mask);
        }

        ++sample_index;
    }
}

void stpg_compare_run_streamed(const ExecEvent *events, const uint32_t *event_indices, uint32_t event_count) {
    stpg_compare_reset();
    if (events == 0 || event_indices == 0) {
        return;
    }

    const uint32_t sample_count = stpg_samples_sample_count();
    const uint32_t logical_mask = stpg_samples_active_pins() == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
#if STPG_STREAM_COMPARE_SAMPLE_REPAIR
    uint32_t sample_index = 0u;
    for (uint32_t compare_index = 0u;
         compare_index < event_count && sample_index < sample_count;
         ++compare_index) {
        const ExecEvent *event = &events[compare_index];
        const bool has_sample = (event->flags & STPG_EVF_SAMPLE_POINT) != 0u;
        if (!has_sample) {
            continue;
        }
        const uint32_t actual = stpg_samples_value_at(sample_index);
        if ((event->flags & STPG_EVF_COMPARE_POINT) != 0u) {
            const uint32_t expected = event->expect_value & logical_mask;
            const uint32_t mask = event->sample_mask & logical_mask;
            (void)stpg_compare_check_value(event_indices[compare_index], actual, expected, mask);
        }
        ++sample_index;
    }
#else
    const uint32_t limit = event_count < sample_count ? event_count : sample_count;
    for (uint32_t sample_index = 0u; sample_index < limit; ++sample_index) {
        const ExecEvent *event = &events[sample_index];
        const uint32_t actual = stpg_samples_value_at(sample_index);
        const uint32_t expected = event->expect_value & logical_mask;
        const uint32_t mask = event->sample_mask & logical_mask;
        (void)stpg_compare_check_value(event_indices[sample_index], actual, expected, mask);
    }
#endif
}

stpg_err_t stpg_compare_replay_begin(stpg_compare_replay_t *replay,
                                    stpg_rate_stream_state_t *state,
                                    uint32_t initial_value,
                                    uint32_t initial_dir) {
    if (replay == NULL) return ERR_BAD_SIZE;
    memset(replay, 0, sizeof(*replay));
#if !STPG_STREAM_PSRAM_COMPARE_SAMPLE
    (void)state;
    (void)initial_value;
    (void)initial_dir;
    return ERR_UNSUPPORTED;
#else
    stpg_compare_reset();
    if (state == NULL) {
        return ERR_BAD_SIZE;
    }
    stpg_err_t err = stpg_buffer_stream_compare_replay_init(state);
    if (err != ERR_NONE) {
        return err;
    }
    state->current_value = initial_value;
    state->current_dir = initial_dir;
    replay->state = state;
    replay->sample_count = stpg_samples_sample_count();
    replay->logical_mask = stpg_samples_active_pins() == STPG_ACTIVE_PINS_V11
        ? 0x00FFFFFFu : 0x0000FFFFu;
    replay->active = true;
    return ERR_NONE;
#endif
}

stpg_err_t stpg_compare_replay_step(stpg_compare_replay_t *replay, bool *done) {
    if (done == NULL) {
        if (replay != NULL) replay->active = false;
        return ERR_BAD_SIZE;
    }
    *done = false;
#if !STPG_STREAM_PSRAM_COMPARE_SAMPLE
    (void)replay;
    return ERR_UNSUPPORTED;
#else
    if (replay == NULL || !replay->active || replay->state == NULL) return ERR_BAD_STATE;
    /* Seal on every failure, including a partial metadata slice. */
    replay->active = false;
    stpg_stream_chunk_result_t result;
    stpg_err_t err = stpg_buffer_stream_compare_replay_fill_next(replay->state, &result);
    if (err != ERR_NONE) return err;
    const uint32_t expected_events = stpg_buffer_event_count();
    const uint32_t metadata_count = stpg_buffer_stream_compare_event_count();
    if (replay->event_count > expected_events
        || result.event_count > STPG_STREAM_BUFFER_EVENTS
        || result.event_count > expected_events - replay->event_count
        || metadata_count > result.event_count
        || replay->sample_index > replay->sample_count
        || metadata_count > replay->sample_count - replay->sample_index) {
        return ERR_BAD_SIZE;
    }
    const ExecEvent *metadata = stpg_buffer_stream_compare_events();
    const uint32_t *event_indices = stpg_buffer_stream_compare_event_indices();
    if (metadata_count != 0u && (metadata == NULL || event_indices == NULL)) return ERR_BAD_STATE;
    for (uint32_t index = 0u; index < metadata_count; ++index) {
        const ExecEvent *event = &metadata[index];
        if ((event->flags & STPG_EVF_COMPARE_POINT) != 0u) {
            ++replay->compare_count;
            const uint32_t actual = stpg_samples_value_at(replay->sample_index);
            const uint32_t expected = event->expect_value & replay->logical_mask;
            const uint32_t mask = event->sample_mask & replay->logical_mask;
            (void)stpg_compare_check_value(event_indices[index], actual, expected, mask);
        }
        ++replay->sample_index;
    }
    replay->event_count += result.event_count;
    if (result.eof) {
        if (replay->event_count != expected_events || replay->sample_index != replay->sample_count) {
            return ERR_BAD_SIZE;
        }
        *done = true;
    } else {
        if (result.event_count == 0u) return ERR_BAD_STATE;
        replay->active = true;
    }
    return ERR_NONE;
#endif
}

stpg_err_t stpg_compare_run_stream_replayed(stpg_rate_stream_state_t *state,
                                            uint32_t initial_value,
                                            uint32_t initial_dir,
                                            uint32_t *out_compare_count) {
    if (out_compare_count != NULL) *out_compare_count = 0u;
    stpg_compare_replay_t replay;
    stpg_err_t err = stpg_compare_replay_begin(&replay, state, initial_value, initial_dir);
    if (err != ERR_NONE) return err;
    bool done = false;
    while (!done) {
        err = stpg_compare_replay_step(&replay, &done);
        if (err != ERR_NONE) return err;
    }
    if (out_compare_count != NULL) *out_compare_count = replay.compare_count;
    return ERR_NONE;
}

uint32_t stpg_compare_fail_count(void) {
    return g_fail_count;
}

uint32_t stpg_compare_result_flags(void) {
    return g_result_flags;
}

stpg_err_t stpg_compare_prepare_read(uint32_t offset_entries, uint32_t count, stpg_fail_log_read_view_t *out) {
    if (out == 0) {
        return ERR_BAD_SIZE;
    }

    const uint32_t stored_count = g_fail_count > STPG_FAIL_LOG_MAX ? STPG_FAIL_LOG_MAX : g_fail_count;
    if (offset_entries > stored_count || count > stored_count - offset_entries) {
        return ERR_BAD_SIZE;
    }

    const uint32_t length_bytes = count * (uint32_t)sizeof(FailLogEntry);
    const uint32_t offset_bytes = offset_entries * (uint32_t)sizeof(FailLogEntry);
    const uint32_t payload_crc32 = length_bytes == 0u
                                       ? 0u
                                       : stpg_crc32((const uint8_t *)g_fail_log + offset_bytes, length_bytes);

    out->payload_offset_entries = offset_entries;
    out->payload_length_bytes = length_bytes;
    out->payload_crc32 = payload_crc32;
    out->element_count = count;
    out->total_fail_count = g_fail_count;
    out->result_flags = g_result_flags;
    return ERR_NONE;
}

void stpg_compare_read_bytes(uint32_t offset_bytes, uint8_t *dst, size_t length) {
    if (dst == 0 || length == 0u) {
        return;
    }
    memcpy(dst, (const uint8_t *)g_fail_log + offset_bytes, length);
}
