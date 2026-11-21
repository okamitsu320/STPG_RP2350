#include "stpg_compare.h"

#include <string.h>
#include <stdint.h>

#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_protocol.h"
#include "stpg_samples.h"

static int expect_u32(uint32_t actual, uint32_t expected) {
    return actual == expected ? 0 : 1;
}

static int check_fail_log_boundary(void) {
    stpg_compare_reset();
    /* Exactly full is not overflow; every mismatch still increments the total. */
    for (uint32_t i = 0u; i < STPG_FAIL_LOG_MAX; ++i) {
        if (!stpg_compare_check_value(i + 100u, i + 1u, 0u, 0xFFFFu)) return 1;
        if (expect_u32(stpg_compare_fail_count(), i + 1u)) return 1;
        if (expect_u32(stpg_compare_result_flags(), STPG_RESULT_FLAG_COMPARE_MISMATCH)) return 1;
    }
    static uint8_t original[STPG_FAIL_LOG_MAX * sizeof(FailLogEntry)];
    static uint8_t after_overflow[sizeof(original)];
    stpg_compare_read_bytes(0u, original, sizeof(original));
    const FailLogEntry *first = (const FailLogEntry *)(const void *)original;
    const FailLogEntry *last = first + STPG_FAIL_LOG_MAX - 1u;
    if (first->event_index != 100u || first->actual != 1u) return 1;
    if (last->event_index != STPG_FAIL_LOG_MAX + 99u || last->actual != STPG_FAIL_LOG_MAX) return 1;

    for (uint32_t extra = 1u; extra <= 2u; ++extra) {
        if (!stpg_compare_check_value(10000u + extra, 1u, 0u, 1u)) return 1;
        stpg_fail_log_read_view_t view;
        if (stpg_compare_prepare_read(0u, STPG_FAIL_LOG_MAX, &view) != ERR_NONE) return 1;
        if (expect_u32(view.element_count, STPG_FAIL_LOG_MAX)) return 1;
        if (expect_u32(view.total_fail_count, STPG_FAIL_LOG_MAX + extra)) return 1;
        if (expect_u32(view.result_flags, STPG_RESULT_FLAG_COMPARE_MISMATCH | STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW)) return 1;
        if (expect_u32(view.payload_length_bytes, sizeof(original))) return 1;
        if (expect_u32(view.payload_crc32, stpg_crc32(original, sizeof(original)))) return 1;
        stpg_compare_read_bytes(0u, after_overflow, sizeof(after_overflow));
        if (memcmp(original, after_overflow, sizeof(original)) != 0) return 1;
        if (stpg_compare_prepare_read(STPG_FAIL_LOG_MAX, 1u, &view) != ERR_BAD_SIZE) return 1;
    }
    stpg_compare_reset();
    if (stpg_compare_fail_count() != 0u || stpg_compare_result_flags() != 0u) return 1;
    if (stpg_compare_check_value(0u, 1u, 1u, 1u)) return 1;
    return stpg_compare_fail_count() != 0u || stpg_compare_result_flags() != 0u;
}

int main(void) {
    if (check_fail_log_boundary()) return 1;
    ExecEvent events[4] = {0};
    uint32_t *samples = stpg_samples_dma_buffer();

    stpg_samples_reset();
    stpg_compare_reset();

    events[0].flags = STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT;
    events[0].sample_mask = 0x000Fu;
    events[0].expect_value = 0x0001u;

    events[1].flags = STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT;
    events[1].sample_mask = 0x000Fu;
    events[1].expect_value = 0x0002u;

    events[2].flags = STPG_EVF_SAMPLE_POINT;
    events[3].flags = STPG_EVF_COMPARE_POINT;
    events[3].sample_mask = 0xFFFFu;
    events[3].expect_value = 0xFFFFu;

    stpg_samples_prepare_for_run(3u, STPG_ACTIVE_PINS_V01);
    samples[0] = 0x0001u;
    samples[1] = 0x0003u;
    samples[2] = 0x0004u;
    stpg_samples_finish_run(3u, false);

    stpg_compare_run(events, 4u);
    if (expect_u32(stpg_compare_fail_count(), 1u)) return 1;
    if (expect_u32(stpg_compare_result_flags(), STPG_RESULT_FLAG_COMPARE_MISMATCH)) return 1;

    stpg_fail_log_read_view_t view;
    if (stpg_compare_prepare_read(0u, 1u, &view) != ERR_NONE) return 1;
    if (expect_u32(view.payload_length_bytes, sizeof(FailLogEntry))) return 1;
    if (expect_u32(view.element_count, 1u)) return 1;
    if (expect_u32(view.total_fail_count, 1u)) return 1;

    uint8_t payload[sizeof(FailLogEntry)] = {0};
    stpg_compare_read_bytes(0u, payload, sizeof(payload));
    const FailLogEntry *entry = (const FailLogEntry *)(const void *)payload;
    if (expect_u32(entry->event_index, 1u)) return 1;
    if (entry->actual != 0x0003u || entry->expected != 0x0002u || entry->mask != 0x000Fu) return 1;

    stpg_compare_reset();
    if (stpg_compare_check_value(12u, 0x0003u, 0x0002u, 0x000Fu) != true) return 1;
    if (expect_u32(stpg_compare_fail_count(), 1u)) return 1;
    memset(payload, 0, sizeof(payload));
    stpg_compare_read_bytes(0u, payload, sizeof(payload));
    const FailLogEntry *single_check_entry = (const FailLogEntry *)(const void *)payload;
    if (expect_u32(single_check_entry->event_index, 12u)) return 1;
    if (single_check_entry->actual != 0x0003u
        || single_check_entry->expected != 0x0002u
        || single_check_entry->mask != 0x000Fu) return 1;
    if (stpg_compare_check_value(13u, 0x0002u, 0x0002u, 0x000Fu) != false) return 1;
    if (expect_u32(stpg_compare_fail_count(), 1u)) return 1;

    stpg_compare_reset();
    events[0].flags = STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT;
    events[0].sample_mask = 0x0000u;
    events[0].expect_value = 0xFFFFu;
    stpg_compare_run(events, 1u);
    if (expect_u32(stpg_compare_fail_count(), 0u)) return 1;
    if (expect_u32(stpg_compare_result_flags(), 0u)) return 1;

    stpg_compare_reset();
    events[0].sample_mask = 0x0003u;
    events[0].expect_value = 0x0001u;
    samples[0] = 0x0005u;
    stpg_samples_finish_run(1u, false);
    stpg_compare_run(events, 1u);
    if (expect_u32(stpg_compare_fail_count(), 0u)) return 1;

    static ExecEvent overflow_events[STPG_FAIL_LOG_MAX + 2u];
    stpg_samples_prepare_for_run(STPG_FAIL_LOG_MAX + 2u, STPG_ACTIVE_PINS_V01);
    for (uint32_t i = 0; i < STPG_FAIL_LOG_MAX + 2u; ++i) {
        overflow_events[i].flags = STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT;
        overflow_events[i].sample_mask = 0xFFFFu;
        overflow_events[i].expect_value = 0u;
        samples[i] = (uint32_t)(i + 1u);
    }
    stpg_samples_finish_run(STPG_FAIL_LOG_MAX + 2u, false);
    stpg_compare_run(overflow_events, STPG_FAIL_LOG_MAX + 2u);
    if (expect_u32(stpg_compare_fail_count(), STPG_FAIL_LOG_MAX + 2u)) return 1;
    if ((stpg_compare_result_flags() & STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW) == 0u) return 1;
    if ((stpg_compare_result_flags() & STPG_RESULT_FLAG_COMPARE_MISMATCH) == 0u) return 1;
    if (stpg_compare_prepare_read(0u, STPG_FAIL_LOG_MAX + 1u, &view) != ERR_BAD_SIZE) return 1;

    stpg_samples_reset();
    stpg_compare_reset();
    ExecEvent streamed_events[2] = {0};
    uint32_t streamed_indices[2] = {7u, 9u};
    stpg_samples_prepare_for_run(2u, STPG_ACTIVE_PINS_V01);
    samples[0] = 0x0001u;
    samples[1] = 0x0003u;
    stpg_samples_finish_run(2u, false);
    streamed_events[0].sample_mask = 0x0001u;
    streamed_events[0].expect_value = 0x0001u;
    streamed_events[1].sample_mask = 0x0003u;
    streamed_events[1].expect_value = 0x0001u;
    stpg_compare_run_streamed(streamed_events, streamed_indices, 2u);
    if (expect_u32(stpg_compare_fail_count(), 1u)) return 1;
    if (expect_u32(stpg_compare_result_flags(), STPG_RESULT_FLAG_COMPARE_MISMATCH)) return 1;
    if (stpg_compare_prepare_read(0u, 1u, &view) != ERR_NONE) return 1;
    if (expect_u32(view.element_count, 1u)) return 1;
    memset(payload, 0, sizeof(payload));
    stpg_compare_read_bytes(0u, payload, sizeof(payload));
    const FailLogEntry *streamed_entry = (const FailLogEntry *)(const void *)payload;
    if (expect_u32(streamed_entry->event_index, 9u)) return 1;
    if (streamed_entry->actual != 0x0003u || streamed_entry->expected != 0x0001u || streamed_entry->mask != 0x0003u) return 1;

    return 0;
}
