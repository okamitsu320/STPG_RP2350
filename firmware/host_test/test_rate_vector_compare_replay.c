#include "stpg_compare.h"
#include "stpg_pattern_buffer.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_pattern.h"
#include "stpg_protocol.h"
#include "stpg_samples.h"

enum {
    TEST_VECTOR_COUNT = 7000u,
    TEST_SAMPLE_MASK = 0x003Fu,
    TEST_PERIOD_TICKS = 24u,
    TEST_SAMPLE_TIME_TICKS = 12u,
};

static int expect_err(stpg_err_t actual, stpg_err_t expected, const char *label) {
    if (actual != expected) {
        fprintf(stderr, "%s: actual=%u expected=%u\n", label, (unsigned)actual, (unsigned)expected);
        return 1;
    }
    return 0;
}

static uint8_t *build_pattern(uint32_t *out_size, uint32_t *out_crc) {
    const uint32_t payload_size =
        STPG_RATE_DESC_SIZE
        + STPG_RATE_SLOT_SIZE * 2u
        + STPG_VECTOR_RECORD_SIZE * TEST_VECTOR_COUNT;
    const uint32_t total_size = STPG_RATEVEC_HEADER_SIZE + payload_size;
    uint8_t *buffer = (uint8_t *)calloc(1u, total_size);
    if (buffer == NULL) {
        return NULL;
    }

    RateVectorHeader *header = (RateVectorHeader *)(void *)buffer;
    header->magic = STPG_RATEVEC_MAGIC;
    header->version = STPG_PATTERN_VERSION;
    header->header_size = STPG_RATEVEC_HEADER_SIZE;
    header->active_pins = STPG_ACTIVE_PINS_V01;
    header->tick_hz = STPG_TICK_HZ;
    header->rate_count = 1u;
    header->slot_count = 2u;
    header->vector_count = TEST_VECTOR_COUNT;
    header->rate_table_offset = STPG_RATEVEC_HEADER_SIZE;
    header->slot_table_offset = header->rate_table_offset + STPG_RATE_DESC_SIZE;
    header->vector_offset = header->slot_table_offset + STPG_RATE_SLOT_SIZE * 2u;

    RateDesc *rate = (RateDesc *)(void *)(buffer + header->rate_table_offset);
    rate->period_ticks = TEST_PERIOD_TICKS;
    rate->slot_count = 2u;
    rate->slot_offset = 0u;

    RateSlot *slots = (RateSlot *)(void *)(buffer + header->slot_table_offset);
    slots[0].time_ticks = 0u;
    slots[0].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    slots[0].drive_mask = TEST_SAMPLE_MASK;
    slots[0].dir_mask = TEST_SAMPLE_MASK;
    slots[1].time_ticks = TEST_SAMPLE_TIME_TICKS;
    slots[1].flags = STPG_RATE_SLOT_SAMPLE;
    slots[1].sample_mask = TEST_SAMPLE_MASK;

    VectorRecord *vectors = (VectorRecord *)(void *)(buffer + header->vector_offset);
    for (uint32_t index = 0u; index < TEST_VECTOR_COUNT; ++index) {
        vectors[index].rate_id = 0u;
        vectors[index].repeat = 1u;
        vectors[index].drive_value = (index & 1u) != 0u ? 0x0015u : 0x002Au;
        vectors[index].drive_enable = TEST_SAMPLE_MASK;
        vectors[index].expect_value = vectors[index].drive_value;
        vectors[index].expect_mask = TEST_SAMPLE_MASK;
    }

    header->data_crc32 = stpg_crc32(buffer + STPG_RATEVEC_HEADER_SIZE, payload_size);
    header->header_crc32 = stpg_crc32(header, STPG_RATEVEC_HEADER_SIZE - sizeof(header->header_crc32));
    *out_size = total_size;
    *out_crc = stpg_crc32(buffer, total_size);
    return buffer;
}

static uint8_t *build_mixed_rate_pattern(
    uint32_t vector_count,
    uint32_t *out_size,
    uint32_t *out_crc,
    uint32_t *out_sample_count) {
    const uint32_t rate_count = 2u;
    const uint32_t slot_count = 3u;
    const uint32_t logical_mask = 0x003Fu;
    const uint32_t payload_size =
        STPG_RATE_DESC_SIZE * rate_count
        + STPG_RATE_SLOT_SIZE * slot_count
        + STPG_VECTOR_RECORD_SIZE * vector_count;
    const uint32_t total_size = STPG_RATEVEC_HEADER_SIZE + payload_size;
    uint8_t *buffer = (uint8_t *)calloc(1u, total_size);
    if (buffer == NULL) {
        return NULL;
    }

    RateVectorHeader *header = (RateVectorHeader *)(void *)buffer;
    header->magic = STPG_RATEVEC_MAGIC;
    header->version = STPG_PATTERN_VERSION;
    header->header_size = STPG_RATEVEC_HEADER_SIZE;
    header->active_pins = STPG_ACTIVE_PINS_V01;
    header->tick_hz = STPG_TICK_HZ;
    header->rate_count = rate_count;
    header->slot_count = slot_count;
    header->vector_count = vector_count;
    header->rate_table_offset = STPG_RATEVEC_HEADER_SIZE;
    header->slot_table_offset = header->rate_table_offset + STPG_RATE_DESC_SIZE * rate_count;
    header->vector_offset = header->slot_table_offset + STPG_RATE_SLOT_SIZE * slot_count;

    RateDesc *rates = (RateDesc *)(void *)(buffer + header->rate_table_offset);
    rates[0].period_ticks = 30u;
    rates[0].slot_count = 1u;
    rates[0].slot_offset = 0u;
    rates[1].period_ticks = 30u;
    rates[1].slot_count = 2u;
    rates[1].slot_offset = 1u;

    RateSlot *slots = (RateSlot *)(void *)(buffer + header->slot_table_offset);
    slots[0].time_ticks = 0u;
    slots[0].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    slots[0].drive_mask = logical_mask;
    slots[0].dir_mask = logical_mask;
    slots[1].time_ticks = 0u;
    slots[1].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    slots[1].drive_mask = logical_mask;
    slots[1].dir_mask = logical_mask;
    slots[2].time_ticks = 15u;
    slots[2].flags = STPG_RATE_SLOT_SAMPLE;
    slots[2].sample_mask = logical_mask;

    VectorRecord *vectors = (VectorRecord *)(void *)(buffer + header->vector_offset);
    uint32_t sample_count = 0u;
    for (uint32_t index = 0u; index < vector_count; ++index) {
        const bool sampled = (index % 3u) != 0u;
        vectors[index].rate_id = sampled ? 1u : 0u;
        vectors[index].repeat = 1u;
        vectors[index].drive_value = (index & 1u) != 0u ? 0x0015u : 0x002Au;
        vectors[index].drive_enable = logical_mask;
        vectors[index].expect_value = vectors[index].drive_value;
        vectors[index].expect_mask = sampled ? logical_mask : 0u;
        sample_count += sampled ? 1u : 0u;
    }

    header->data_crc32 = stpg_crc32(buffer + STPG_RATEVEC_HEADER_SIZE, payload_size);
    header->header_crc32 = stpg_crc32(header, STPG_RATEVEC_HEADER_SIZE - sizeof(header->header_crc32));
    *out_size = total_size;
    *out_crc = stpg_crc32(buffer, total_size);
    *out_sample_count = sample_count;
    return buffer;
}

static int load_psram(const uint8_t *pattern, uint32_t size, uint32_t crc) {
    if (expect_err(stpg_buffer_begin(STPG_STORAGE_PSRAM, size, crc, 0u), ERR_NONE, "begin")) {
        return 1;
    }
    uint32_t offset = 0u;
    while (offset < size) {
        uint32_t chunk_size = size - offset;
        if (chunk_size > STPG_UPLOAD_CHUNK_SIZE) {
            chunk_size = STPG_UPLOAD_CHUNK_SIZE;
        }
        if (expect_err(stpg_buffer_write_chunk(
                offset, pattern + offset, chunk_size, stpg_crc32(pattern + offset, chunk_size)),
                ERR_NONE, "write")) {
            return 1;
        }
        offset += chunk_size;
    }
    return expect_err(stpg_buffer_finalize(size, crc), ERR_NONE, "finalize");
}

static uint32_t produced_samples;

static uint32_t sample_frontier(void) {
    return produced_samples;
}

static int fill_expected_samples(uint32_t mismatch_index) {
    uint32_t *const samples = stpg_samples_dma_buffer();
    const bool staged = stpg_samples_dma_ring_bits() != 0u;
    const uint32_t mask = staged ? 2047u : UINT32_MAX;
    produced_samples = 0u;
    for (uint32_t index = 0u; index < TEST_VECTOR_COUNT; ++index) {
        samples[index & mask] = index == mismatch_index
            ? 0u : ((index & 1u) != 0u ? 0x0015u : 0x002Au);
        ++produced_samples;
        /* Model DMA wrapping plus bounded Core0 service, rather than writing
         * the entire run past the end of the 2048-word SRAM ring. Leave the
         * final suffix for the quiescent terminal flush. */
        if (staged && produced_samples % 64u == 0u && produced_samples < TEST_VECTOR_COUNT) {
            if (expect_err(stpg_samples_service(sample_frontier), ERR_NONE, "sample service")) {
                return 1;
            }
        }
    }
    if (expect_err(stpg_samples_finish_staging(sample_frontier), ERR_NONE, "sample flush")) {
        return 1;
    }
    stpg_samples_finish_run(TEST_VECTOR_COUNT, false);
    if (stpg_samples_sample_count() != TEST_VECTOR_COUNT || stpg_samples_result_flags() != 0u) {
        fprintf(stderr, "sample capture incomplete or overflowed\n");
        return 1;
    }
    return 0;
}

static int test_mixed_rate_compare_sample_replay(void) {
    const uint32_t vector_count = 41u;
    uint32_t pattern_size = 0u;
    uint32_t pattern_crc = 0u;
    uint32_t expected_sample_count = 0u;
    uint8_t *pattern = build_mixed_rate_pattern(
        vector_count, &pattern_size, &pattern_crc, &expected_sample_count);
    if (pattern == NULL) {
        return 1;
    }

    stpg_buffer_reset();
    if (load_psram(pattern, pattern_size, pattern_crc)) {
        free(pattern);
        return 1;
    }
    const uint32_t expected_event_count = vector_count + expected_sample_count;
    if (stpg_buffer_event_count() != expected_event_count) {
        fprintf(stderr, "mixed event count mismatch: actual=%u expected=%u\n",
                (unsigned)stpg_buffer_event_count(), (unsigned)expected_event_count);
        free(pattern);
        return 1;
    }
    if (expect_err(
            stpg_samples_prepare_for_run(expected_sample_count, STPG_ACTIVE_PINS_V01),
            ERR_NONE,
            "mixed sample prepare")) {
        free(pattern);
        return 1;
    }

    uint32_t *samples = stpg_samples_dma_buffer();
    if (samples == NULL || stpg_samples_dma_capacity_words() < expected_sample_count) {
        fprintf(stderr, "mixed sample PSRAM capacity is too small\n");
        free(pattern);
        return 1;
    }
    uint32_t sample_cursor = 0u;
    for (uint32_t index = 0u; index < vector_count; ++index) {
        if ((index % 3u) == 0u) {
            continue;
        }
        samples[sample_cursor++] = (index & 1u) != 0u ? 0x0015u : 0x002Au;
    }
    stpg_samples_finish_run(expected_sample_count, false);

    stpg_rate_stream_state_t state;
    uint32_t compare_count = 0u;
    if (expect_err(
            stpg_compare_run_stream_replayed(&state, 0u, 0u, &compare_count),
            ERR_NONE,
            "mixed replay match")) {
        free(pattern);
        return 1;
    }
    if (compare_count != expected_sample_count || stpg_compare_fail_count() != 0u) {
        fprintf(stderr, "mixed replay mismatch: compares=%u expected=%u fails=%u\n",
                (unsigned)compare_count,
                (unsigned)expected_sample_count,
                (unsigned)stpg_compare_fail_count());
        free(pattern);
        return 1;
    }

    /* The third sampled vector is vector 4, whose event index is 7 after
     * vector 0's drive-only event and the two-event sampled vectors 1 and 2. */
    samples[2u] = 0u;
    if (expect_err(
            stpg_compare_run_stream_replayed(&state, 0u, 0u, &compare_count),
            ERR_NONE,
            "mixed replay mismatch")) {
        free(pattern);
        return 1;
    }
    if (compare_count != expected_sample_count || stpg_compare_fail_count() != 1u) {
        fprintf(stderr, "mixed mismatch accounting failed: compares=%u fails=%u\n",
                (unsigned)compare_count, (unsigned)stpg_compare_fail_count());
        free(pattern);
        return 1;
    }

    stpg_fail_log_read_view_t view;
    if (expect_err(stpg_compare_prepare_read(0u, 1u, &view), ERR_NONE, "mixed fail log read")) {
        free(pattern);
        return 1;
    }
    FailLogEntry entry;
    memset(&entry, 0, sizeof(entry));
    stpg_compare_read_bytes(0u, (uint8_t *)&entry, sizeof(entry));
    const int failed = entry.event_index != 7u
        || entry.actual != 0u
        || entry.expected != 0x002Au
        || entry.mask != 0x003Fu;
    if (failed) {
        fprintf(stderr, "mixed fail entry mismatch: index=%u actual=%u expected=%u mask=%u\n",
                (unsigned)entry.event_index,
                (unsigned)entry.actual,
                (unsigned)entry.expected,
                (unsigned)entry.mask);
    }
    free(pattern);
    return failed ? 1 : 0;
}

int main(void) {
    if (test_mixed_rate_compare_sample_replay()) return 1;
    uint32_t pattern_size = 0u;
    uint32_t pattern_crc = 0u;
    uint8_t *pattern = build_pattern(&pattern_size, &pattern_crc);
    if (pattern == NULL) {
        return 1;
    }

    stpg_buffer_reset();
    if (load_psram(pattern, pattern_size, pattern_crc)) {
        free(pattern);
        return 1;
    }
    if (stpg_buffer_event_count() != TEST_VECTOR_COUNT * 2u) {
        fprintf(stderr, "event count mismatch: actual=%u expected=%u\n",
                (unsigned)stpg_buffer_event_count(), (unsigned)(TEST_VECTOR_COUNT * 2u));
        free(pattern);
        return 1;
    }

    if (expect_err(
            stpg_samples_prepare_for_run(TEST_VECTOR_COUNT, STPG_ACTIVE_PINS_V01),
            ERR_NONE,
            "sample prepare")) {
        free(pattern);
        return 1;
    }
    uint32_t *samples = stpg_samples_dma_buffer();
    if (samples == NULL || stpg_samples_dma_capacity_words() < TEST_VECTOR_COUNT) {
        fprintf(stderr, "sample PSRAM capacity is too small\n");
        free(pattern);
        return 1;
    }
    if (fill_expected_samples(UINT32_MAX)) {
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect_err(stpg_compare_run_stream_replayed(&state, 0u, 0u, NULL), ERR_NONE, "replay match")) {
        free(pattern);
        return 1;
    }
    if (stpg_compare_fail_count() != 0u) {
        fprintf(stderr, "unexpected mismatch count: %u\n", (unsigned)stpg_compare_fail_count());
        free(pattern);
        return 1;
    }

    if (expect_err(
            stpg_samples_prepare_for_run(TEST_VECTOR_COUNT, STPG_ACTIVE_PINS_V01),
            ERR_NONE,
            "mismatch sample prepare") || fill_expected_samples(3000u)) {
        free(pattern);
        return 1;
    }
    if (expect_err(stpg_compare_run_stream_replayed(&state, 0u, 0u, NULL), ERR_NONE, "replay mismatch")) {
        free(pattern);
        return 1;
    }
    if (stpg_compare_fail_count() != 1u) {
        fprintf(stderr, "expected one mismatch: %u\n", (unsigned)stpg_compare_fail_count());
        free(pattern);
        return 1;
    }

    stpg_fail_log_read_view_t view;
    if (expect_err(stpg_compare_prepare_read(0u, 1u, &view), ERR_NONE, "fail log read")) {
        free(pattern);
        return 1;
    }
    FailLogEntry entry;
    memset(&entry, 0, sizeof(entry));
    stpg_compare_read_bytes(0u, (uint8_t *)&entry, sizeof(entry));
    if (entry.event_index != 6001u || entry.actual != 0u
        || entry.expected != 0x002Au || entry.mask != TEST_SAMPLE_MASK) {
        fprintf(stderr, "fail entry mismatch: index=%u actual=%u expected=%u mask=%u\n",
                (unsigned)entry.event_index,
                (unsigned)entry.actual,
                (unsigned)entry.expected,
                (unsigned)entry.mask);
        free(pattern);
        return 1;
    }

    free(pattern);
    return 0;
}
