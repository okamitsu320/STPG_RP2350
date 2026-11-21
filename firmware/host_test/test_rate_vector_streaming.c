#include "stpg_pattern_buffer.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_pattern.h"
#include "stpg_protocol.h"
#include "stpg_psram.h"
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
#include "stpg_compare.h"
#include "stpg_samples.h"
#endif

static int expect(stpg_err_t actual, stpg_err_t expected) {
    if (actual != expected) {
        fprintf(stderr, "expect failed: actual=%u expected=%u\n", (unsigned)actual, (unsigned)expected);
    }
    return actual == expected ? 0 : 1;
}

static int test_stream_state_storage_is_single_and_stable(void) {
    stpg_rate_stream_state_t *const first = stpg_buffer_stream_state_storage();
    stpg_rate_stream_state_t *const second = stpg_buffer_stream_state_storage();
    if (first == NULL || first != second) {
        fprintf(stderr, "Rate Vector stream-state storage is not a stable singleton\n");
        return 1;
    }
    first->current_value = 0x5AA55AA5u;
    if (second->current_value != 0x5AA55AA5u) {
        fprintf(stderr, "Rate Vector stream-state storage is not shared\n");
        return 1;
    }
    memset(first, 0, sizeof(*first));
    return 0;
}

static void finalize_header_crc(RateVectorHeader *header) {
    header->header_crc32 = stpg_crc32(header, STPG_RATEVEC_HEADER_SIZE - sizeof(header->header_crc32));
}

static uint32_t compute_crc32(const void *data, size_t size) {
    return stpg_crc32(data, (uint32_t)size);
}

static uint8_t *build_pattern(uint32_t vector_count,
                              int compare_enabled,
                              uint32_t repeat,
                              uint32_t *out_size,
                              uint32_t *out_total_crc,
                              uint32_t *out_expected_word_count,
                              uint32_t *out_expected_sample_count) {
    const uint32_t rate_count = 1u;
    const uint32_t slot_count = 2u;
    const uint32_t expected_word_count = vector_count * repeat * 2u * 4u;
    const uint32_t expected_sample_count = compare_enabled ? vector_count * repeat : 0u;
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
    header->slot_table_offset = header->rate_table_offset + (STPG_RATE_DESC_SIZE * rate_count);
    header->vector_offset = header->slot_table_offset + (STPG_RATE_SLOT_SIZE * slot_count);

    RateDesc *rate = (RateDesc *)(void *)(buffer + header->rate_table_offset);
    rate->period_ticks = 24u;
    rate->slot_count = 2u;
    rate->slot_offset = 0u;

    RateSlot *slots = (RateSlot *)(void *)(buffer + header->slot_table_offset);
    slots[0].time_ticks = 0u;
    slots[0].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    slots[0].drive_mask = 0x003Fu;
    slots[0].dir_mask = 0x003Fu;
    slots[1].time_ticks = 12u;
    slots[1].flags = STPG_DRIVE_ZERO | (compare_enabled ? STPG_RATE_SLOT_SAMPLE : 0u);
    slots[1].drive_mask = 0x003Fu;
    slots[1].sample_mask = compare_enabled ? 0x003Fu : 0u;

    VectorRecord *vectors = (VectorRecord *)(void *)(buffer + header->vector_offset);
    for (uint32_t index = 0u; index < vector_count; ++index) {
        vectors[index].rate_id = 0u;
        vectors[index].repeat = repeat;
        vectors[index].drive_value = (index & 1u) ? 0x0015u : 0x002Au;
        vectors[index].drive_enable = 0x003Fu;
        vectors[index].expect_value = vectors[index].drive_value;
        vectors[index].expect_mask = compare_enabled ? 0x003Fu : 0u;
    }

    header->data_crc32 = compute_crc32(buffer + STPG_RATEVEC_HEADER_SIZE, payload_size);
    finalize_header_crc(header);

    *out_size = total_size;
    *out_total_crc = compute_crc32(buffer, total_size);
    *out_expected_word_count = expected_word_count;
    *out_expected_sample_count = expected_sample_count;
    return buffer;
}

#if !STPG_STREAM_PSRAM_COMPARE_SAMPLE
static uint8_t *build_reduced_pattern(uint32_t vector_count,
                                      int mixed_repeat,
                                      uint32_t *out_size,
                                      uint32_t *out_total_crc,
                                      uint32_t *out_expected_event_count,
                                      uint32_t *out_expected_fast_count,
                                      uint32_t *out_expected_fallback_count) {
    const uint32_t rate_count = 1u;
    const uint32_t slot_count = 2u;
    uint32_t expected_event_count = 0u;
    uint32_t expected_fast_count = 0u;
    uint32_t expected_fallback_count = 0u;
    for (uint32_t index = 0u; index < vector_count; ++index) {
        const uint32_t repeat = (mixed_repeat && (index % 17u) == 0u) ? 2u : 1u;
        expected_event_count += repeat;
        if (repeat == 1u) {
            expected_fast_count += 1u;
        } else {
            expected_fallback_count += repeat;
        }
    }

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
    header->slot_table_offset = header->rate_table_offset + (STPG_RATE_DESC_SIZE * rate_count);
    header->vector_offset = header->slot_table_offset + (STPG_RATE_SLOT_SIZE * slot_count);

    RateDesc *rate = (RateDesc *)(void *)(buffer + header->rate_table_offset);
    rate->period_ticks = 45u;
    rate->slot_count = 2u;
    rate->slot_offset = 0u;

    RateSlot *slots = (RateSlot *)(void *)(buffer + header->slot_table_offset);
    slots[0].time_ticks = 0u;
    slots[0].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    slots[0].drive_mask = 0x003Fu;
    slots[0].dir_mask = 0x003Fu;
    slots[1].time_ticks = 22u;
    slots[1].flags = STPG_RATE_SLOT_SAMPLE;
    slots[1].sample_mask = 0x003Fu;

    VectorRecord *vectors = (VectorRecord *)(void *)(buffer + header->vector_offset);
    for (uint32_t index = 0u; index < vector_count; ++index) {
        vectors[index].rate_id = 0u;
        vectors[index].repeat = (mixed_repeat && (index % 17u) == 0u) ? 2u : 1u;
        vectors[index].drive_value = (index & 1u) ? 0x0015u : 0x002Au;
        vectors[index].drive_enable = 0x003Fu;
        vectors[index].expect_value = vectors[index].drive_value;
        vectors[index].expect_mask = 0x003Fu;
    }

    header->data_crc32 = compute_crc32(buffer + STPG_RATEVEC_HEADER_SIZE, payload_size);
    finalize_header_crc(header);

    *out_size = total_size;
    *out_total_crc = compute_crc32(buffer, total_size);
    *out_expected_event_count = expected_event_count;
    *out_expected_fast_count = expected_fast_count;
    *out_expected_fallback_count = expected_fallback_count;
    return buffer;
}
#endif

static int load_fixture_psram(const void *fixture, uint32_t size, uint32_t total_crc) {
    stpg_err_t err = stpg_buffer_begin(STPG_STORAGE_PSRAM, size, total_crc, 0u);
    if (expect(err, ERR_NONE)) {
        fprintf(stderr, "load_fixture_psram: begin failed\n");
        return 1;
    }
    const uint8_t *bytes = (const uint8_t *)fixture;
    uint32_t offset = 0u;
    while (offset < size) {
        uint32_t chunk_size = size - offset;
        if (chunk_size > STPG_UPLOAD_CHUNK_SIZE) {
            chunk_size = STPG_UPLOAD_CHUNK_SIZE;
        }
        err = stpg_buffer_write_chunk(offset, bytes + offset, chunk_size, compute_crc32(bytes + offset, chunk_size));
        if (expect(err, ERR_NONE)) {
            fprintf(stderr, "load_fixture_psram: write_chunk failed at offset=%u size=%u\n",
                    (unsigned)offset, (unsigned)chunk_size);
            return 1;
        }
        offset += chunk_size;
    }
    err = stpg_buffer_finalize(size, total_crc);
    if (expect(err, ERR_NONE)) {
        fprintf(stderr, "load_fixture_psram: finalize failed\n");
        return 1;
    }
    return 0;
}

static int test_precomputed_uop_cache_and_inline_sample_count(void) {
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_word_count = 0u;
    uint32_t expected_sample_count = 0u;
    const uint32_t vector_count = 37u;
    uint8_t *pattern = build_pattern(vector_count,
                                     1,
                                     1u,
                                     &total_size,
                                     &total_crc,
                                     &expected_word_count,
                                     &expected_sample_count);
    if (pattern == NULL) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    stpg_stream_buffer_telemetry_t telemetry;
    stpg_buffer_get_stream_telemetry(&telemetry);
    if (!state.precomputed_uops_complete
        || state.precomputed_uop_count != 2u
        || telemetry.precomputed_uop_cache_entry_count != 2u
        || telemetry.precomputed_uop_cache_fallback_count != 0u
        || telemetry.precomputed_uop_cache_build_count != 0u) {
        fprintf(stderr,
                "precomputed UOP setup mismatch: complete=%d entries=%u telemetry_entries=%u fallback=%u build=%u\n",
                state.precomputed_uops_complete ? 1 : 0,
                (unsigned)state.precomputed_uop_count,
                (unsigned)telemetry.precomputed_uop_cache_entry_count,
                (unsigned)telemetry.precomputed_uop_cache_fallback_count,
                (unsigned)telemetry.precomputed_uop_cache_build_count);
        free(pattern);
        return 1;
    }

    uint32_t chunk_words[7u * 4u];
    uint32_t collected_word_count = 0u;
    uint32_t collected_sample_count = 0u;
    while (true) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 7u, &result), ERR_NONE)) {
            free(pattern);
            return 1;
        }
        collected_word_count += result.word_count;
        collected_sample_count += result.sample_event_count;
        if (result.eof) break;
    }

    stpg_buffer_get_stream_telemetry(&telemetry);
    const int failed = collected_word_count != expected_word_count
        || collected_sample_count != expected_sample_count
        || telemetry.precomputed_uop_cache_hit_count == 0u
        || telemetry.precomputed_uop_cache_fallback_count != 0u
        || telemetry.precomputed_uop_cache_build_count != 0u
        || telemetry.sample_event_scan_count != 0u;
    if (failed) {
        fprintf(stderr,
                "precomputed UOP stream mismatch: words=%u/%u samples=%u/%u hits=%u fallback=%u build=%u scans=%u\n",
                (unsigned)collected_word_count,
                (unsigned)expected_word_count,
                (unsigned)collected_sample_count,
                (unsigned)expected_sample_count,
                (unsigned)telemetry.precomputed_uop_cache_hit_count,
                (unsigned)telemetry.precomputed_uop_cache_fallback_count,
                (unsigned)telemetry.precomputed_uop_cache_build_count,
                (unsigned)telemetry.sample_event_scan_count);
    }
    free(pattern);
    return failed ? 1 : 0;
}

static int test_chunked_stream_matches_full_expansion_across_cache_refill(void) {
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_word_count = 0u;
    uint32_t expected_sample_count = 0u;
    uint8_t *pattern = build_pattern(STPG_STREAM_VECTOR_CACHE_RECORDS + 37u,
                                     0,
                                     1u,
                                     &total_size,
                                     &total_crc,
                                     &expected_word_count,
                                     &expected_sample_count);
    if (pattern == NULL) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }

#if STPG_STREAM_STATUS_LOGICAL_EVENTS
    /* Upload STATUS must use emitted slot-time groups, before the run path
     * materializes the complete event view.  This catches the distinction
     * between vector-record count and logical event count. */
    if (stpg_buffer_event_count() != expected_word_count / 4u) {
        fprintf(stderr, "upload event_count mismatch: actual=%u expected=%u\n",
                (unsigned)stpg_buffer_event_count(), (unsigned)(expected_word_count / 4u));
        free(pattern);
        return 1;
    }
#endif

    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) {
        fprintf(stderr, "test_chunked_stream: prepare_for_run failed\n");
        free(pattern);
        return 1;
    }
    const uint32_t *expanded_words = stpg_buffer_pio_words();
    const uint32_t expanded_word_count = stpg_buffer_pio_word_count();
    if (expanded_word_count != expected_word_count) {
        fprintf(stderr, "expanded_word_count mismatch: actual=%u expected=%u\n",
                (unsigned)expanded_word_count, (unsigned)expected_word_count);
        free(pattern);
        return 1;
    }
    if (expected_sample_count != 0u || stpg_buffer_sample_event_count() != 0u) {
        fprintf(stderr, "unexpected sample count: expected=%u actual=%u\n",
                (unsigned)expected_sample_count, (unsigned)stpg_buffer_sample_event_count());
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        fprintf(stderr, "test_chunked_stream: prepare_for_streaming failed\n");
        free(pattern);
        return 1;
    }

    uint32_t chunk_words[7u * 4u];
    uint32_t *collected_words = (uint32_t *)calloc(expanded_word_count, sizeof(uint32_t));
    if (collected_words == NULL) {
        free(pattern);
        return 1;
    }

    uint32_t collected_word_count = 0u;
    while (true) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 7u, &result), ERR_NONE)) {
            fprintf(stderr, "test_chunked_stream: fill_next_chunk failed at collected=%u\n",
                    (unsigned)collected_word_count);
            free(collected_words);
            free(pattern);
            return 1;
        }
        if (result.event_count == 0u && result.eof) {
            break;
        }
        memcpy(collected_words + collected_word_count,
               chunk_words,
               result.event_count * 4u * sizeof(uint32_t));
        collected_word_count += result.event_count * 4u;
        if (result.eof) {
            break;
        }
    }

    const int failed =
        (collected_word_count != expanded_word_count)
        || (memcmp(collected_words, expanded_words, expanded_word_count * sizeof(uint32_t)) != 0)
        || (stpg_buffer_stream_compare_event_count() != 0u);
    if (failed) {
        fprintf(stderr,
                "chunked mismatch: collected=%u expanded=%u compare_count=%u\n",
                (unsigned)collected_word_count,
                (unsigned)expanded_word_count,
                (unsigned)stpg_buffer_stream_compare_event_count());
    }

    free(collected_words);
    free(pattern);
    return failed ? 1 : 0;
}

static int test_compare_stream_metadata_matches_full_expansion(void) {
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_word_count = 0u;
    uint32_t expected_sample_count = 0u;
    const uint32_t vector_count = 41u;
    uint8_t *pattern = build_pattern(vector_count,
                                     1,
                                     1u,
                                     &total_size,
                                     &total_crc,
                                     &expected_word_count,
                                     &expected_sample_count);
    if (pattern == NULL) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }

    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) {
        fprintf(stderr, "test_compare_stream: prepare_for_run failed\n");
        free(pattern);
        return 1;
    }

    const ExecEvent *expanded_events = stpg_buffer_events();
    const uint32_t expanded_event_count = stpg_buffer_event_count();
    if (expanded_event_count != vector_count * 2u) {
        fprintf(stderr, "expanded_event_count mismatch: actual=%u expected=%u\n",
                (unsigned)expanded_event_count, (unsigned)(vector_count * 2u));
        free(pattern);
        return 1;
    }
    if (stpg_buffer_sample_event_count() != expected_sample_count) {
        fprintf(stderr, "sample_event_count mismatch: actual=%u expected=%u\n",
                (unsigned)stpg_buffer_sample_event_count(), (unsigned)expected_sample_count);
        free(pattern);
        return 1;
    }

    ExecEvent *expected_compare_events = (ExecEvent *)calloc(expected_sample_count, sizeof(ExecEvent));
    uint32_t *expected_compare_indices = (uint32_t *)calloc(expected_sample_count, sizeof(uint32_t));
    if (expected_compare_events == NULL || expected_compare_indices == NULL) {
        free(expected_compare_events);
        free(expected_compare_indices);
        free(pattern);
        return 1;
    }

    uint32_t compare_cursor = 0u;
    for (uint32_t event_index = 0u; event_index < expanded_event_count; ++event_index) {
        if ((expanded_events[event_index].flags & STPG_EVF_SAMPLE_POINT) == 0u) {
            continue;
        }
        expected_compare_events[compare_cursor] = expanded_events[event_index];
        expected_compare_events[compare_cursor].flags =
#if STPG_STREAM_FAIL_STOP
            STPG_EVF_SAMPLE_POINT |
#endif
            (expanded_events[event_index].sample_mask != 0u ? STPG_EVF_COMPARE_POINT : 0u);
        expected_compare_indices[compare_cursor] = event_index;
        compare_cursor += 1u;
    }
    if (compare_cursor != expected_sample_count) {
        fprintf(stderr, "compare cursor mismatch: actual=%u expected=%u\n",
                (unsigned)compare_cursor, (unsigned)expected_sample_count);
        free(expected_compare_events);
        free(expected_compare_indices);
        free(pattern);
        return 1;
    }

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
    const uint32_t expanded_word_count = stpg_buffer_pio_word_count();
#endif

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        fprintf(stderr, "test_compare_stream: prepare_for_streaming failed\n");
        free(expected_compare_events);
        free(expected_compare_indices);
        free(pattern);
        return 1;
    }

    uint32_t chunk_words[5u * 4u];
    uint32_t *collected_words = (uint32_t *)calloc(expected_word_count, sizeof(uint32_t));
    if (collected_words == NULL) {
        free(expected_compare_events);
        free(expected_compare_indices);
        free(pattern);
        return 1;
    }
    uint32_t collected_word_count = 0u;
    while (true) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 5u, &result), ERR_NONE)) {
            fprintf(stderr, "test_compare_stream: fill_next_chunk failed\n");
            free(collected_words);
            free(expected_compare_events);
            free(expected_compare_indices);
            free(pattern);
            return 1;
        }
        if (result.event_count == 0u && result.eof) {
            break;
        }
        memcpy(collected_words + collected_word_count,
               chunk_words,
               result.event_count * 4u * sizeof(uint32_t));
        collected_word_count += result.event_count * 4u;
        if (result.eof) {
            break;
        }
    }

    const uint32_t actual_compare_count = stpg_buffer_stream_compare_event_count();
#if !STPG_STREAM_PSRAM_COMPARE_SAMPLE
    const ExecEvent *actual_compare_events = stpg_buffer_stream_compare_events();
    const uint32_t *actual_compare_indices = stpg_buffer_stream_compare_event_indices();
#endif
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
    /* The practical device profile intentionally defers compare metadata
     * collection until the PSRAM sample replay pass.  The dedicated replay
     * target validates that pass; this streaming test validates that the
     * output path does not overflow the SRAM metadata buffer. */
    int failed = (actual_compare_count != 0u)
        || (collected_word_count != expanded_word_count)
        || (memcmp(collected_words, stpg_buffer_pio_words(), expanded_word_count * sizeof(uint32_t)) != 0);
#else
    int failed = (actual_compare_count != expected_sample_count);
    if (!failed) {
        for (uint32_t i = 0; i < expected_sample_count; ++i) {
            if (actual_compare_events[i].flags != expected_compare_events[i].flags
                || actual_compare_events[i].sample_mask != expected_compare_events[i].sample_mask
                || actual_compare_events[i].expect_value != expected_compare_events[i].expect_value
                || actual_compare_indices[i] != expected_compare_indices[i]) {
                fprintf(stderr,
                        "compare mismatch at %u: actual(flags=0x%X mask=0x%X expect=0x%X idx=%u) expected(flags=0x%X mask=0x%X expect=0x%X idx=%u)\n",
                        (unsigned)i,
                        (unsigned)actual_compare_events[i].flags,
                        (unsigned)actual_compare_events[i].sample_mask,
                        (unsigned)actual_compare_events[i].expect_value,
                        (unsigned)actual_compare_indices[i],
                        (unsigned)expected_compare_events[i].flags,
                        (unsigned)expected_compare_events[i].sample_mask,
                        (unsigned)expected_compare_events[i].expect_value,
                        (unsigned)expected_compare_indices[i]);
                failed = 1;
                break;
            }
        }
    }
#endif
    if (failed) {
        fprintf(stderr,
                "compare/output mismatch: actual_count=%u expected_count=%u collected_words=%u expanded_words=%u\n",
                (unsigned)actual_compare_count,
                (unsigned)expected_sample_count,
                (unsigned)collected_word_count,
                (unsigned)stpg_buffer_pio_word_count());
    }

    free(collected_words);
    free(expected_compare_events);
    free(expected_compare_indices);
    free(pattern);
    return failed ? 1 : 0;
}

#if STPG_STREAM_FAIL_STOP
static int test_compare24_fail_stop_slot_sample_count(void) {
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_word_count = 0u;
    uint32_t expected_sample_count = 0u;
    const uint32_t vector_count = 13u;
    uint8_t *pattern = build_pattern(vector_count,
                                     1,
                                     1u,
                                     &total_size,
                                     &total_crc,
                                     &expected_word_count,
                                     &expected_sample_count);
    if (pattern == NULL) return 1;

    RateVectorHeader *header = (RateVectorHeader *)(void *)pattern;
    header->active_pins = STPG_ACTIVE_PINS_V11;
    finalize_header_crc(header);
    total_crc = compute_crc32(pattern, total_size);

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)
        || expect(stpg_buffer_stream_compare_begin_metadata_slot(0u), ERR_NONE)) {
        free(pattern);
        return 1;
    }

    uint32_t chunk_words[64u * 4u];
    stpg_stream_chunk_result_t result;
    if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 64u, &result), ERR_NONE)) {
        free(pattern);
        return 1;
    }

    int failed = !result.eof
        || result.event_count != vector_count * 2u
        || result.word_count != expected_word_count
        || result.sample_event_count != expected_sample_count
        || stpg_buffer_stream_compare_event_count() != expected_sample_count;
    const ExecEvent *events = stpg_buffer_stream_compare_events();
    for (uint32_t index = 0u; !failed && index < expected_sample_count; ++index) {
        if ((events[index].flags & STPG_EVF_SAMPLE_POINT) == 0u) {
            failed = 1;
        }
    }
    if (failed) {
        fprintf(stderr,
                "compare24 fail-stop count mismatch: events=%u words=%u samples=%u metadata=%u expected=%u\n",
                (unsigned)result.event_count,
                (unsigned)result.word_count,
                (unsigned)result.sample_event_count,
                (unsigned)stpg_buffer_stream_compare_event_count(),
                (unsigned)expected_sample_count);
    }
    free(pattern);
    return failed ? 1 : 0;
}
#endif

static int test_repeat_block_stream_matches_full_expansion(void) {
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_word_count = 0u;
    uint32_t expected_sample_count = 0u;
    uint8_t *pattern = build_pattern(7u,
                                     0,
                                     9u,
                                     &total_size,
                                     &total_crc,
                                     &expected_word_count,
                                     &expected_sample_count);
    if (pattern == NULL) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }
    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    const uint32_t *expanded_words = stpg_buffer_pio_words();
    const uint32_t expanded_word_count = stpg_buffer_pio_word_count();
    if (expanded_word_count != expected_word_count) {
        fprintf(stderr, "repeat expansion mismatch: actual=%u expected=%u\n",
                (unsigned)expanded_word_count, (unsigned)expected_word_count);
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    uint32_t chunk_words[11u * 4u];
    uint32_t *collected_words = (uint32_t *)calloc(expanded_word_count, sizeof(uint32_t));
    if (collected_words == NULL) {
        free(pattern);
        return 1;
    }
    uint32_t collected_word_count = 0u;
    while (true) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 11u, &result), ERR_NONE)) {
            free(collected_words);
            free(pattern);
            return 1;
        }
        memcpy(collected_words + collected_word_count,
               chunk_words,
               result.event_count * 4u * sizeof(uint32_t));
        collected_word_count += result.event_count * 4u;
        if (result.eof) break;
    }

    stpg_stream_buffer_telemetry_t telemetry;
    stpg_buffer_get_stream_telemetry(&telemetry);
    int failed = collected_word_count != expanded_word_count
        || memcmp(collected_words, expanded_words, expanded_word_count * sizeof(uint32_t)) != 0
        || stpg_buffer_stream_compare_event_count() != 0u;
#if STPG_STREAM_REPEAT_BLOCK_FILL
    failed = failed || telemetry.fast_vector_count == 0u;
#endif
    if (failed) {
        fprintf(stderr,
                "repeat output mismatch: collected_words=%u expanded_words=%u fast_vectors=%u compare=%u\n",
                (unsigned)collected_word_count,
                (unsigned)expanded_word_count,
                (unsigned)telemetry.fast_vector_count,
                (unsigned)stpg_buffer_stream_compare_event_count());
    }
    free(collected_words);
    free(pattern);
    return failed ? 1 : 0;
}

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
static uint8_t *build_repeat_boundary_exit_pattern(uint32_t *out_size,
                                                   uint32_t *out_crc) {
    const uint32_t rate_count = 3u;
    const uint32_t slot_count = 5u;
    const uint32_t vector_count = 7u;
    const uint32_t payload_size =
        STPG_RATE_DESC_SIZE * rate_count
        + STPG_RATE_SLOT_SIZE * slot_count
        + STPG_VECTOR_RECORD_SIZE * vector_count;
    const uint32_t total_size = STPG_RATEVEC_HEADER_SIZE + payload_size;
    uint8_t *pattern = (uint8_t *)calloc(1u, total_size);
    if (pattern == NULL) return NULL;

    RateVectorHeader *header = (RateVectorHeader *)(void *)pattern;
    header->magic = STPG_RATEVEC_MAGIC;
    header->version = STPG_PATTERN_VERSION;
    header->header_size = STPG_RATEVEC_HEADER_SIZE;
    header->active_pins = STPG_ACTIVE_PINS_V11;
    header->tick_hz = STPG_TICK_HZ;
    header->rate_count = rate_count;
    header->slot_count = slot_count;
    header->vector_count = vector_count;
    header->rate_table_offset = STPG_RATEVEC_HEADER_SIZE;
    header->slot_table_offset =
        header->rate_table_offset + STPG_RATE_DESC_SIZE * rate_count;
    header->vector_offset =
        header->slot_table_offset + STPG_RATE_SLOT_SIZE * slot_count;

    RateDesc *rates = (RateDesc *)(void *)(pattern + header->rate_table_offset);
    rates[0] = (RateDesc){.period_ticks = 30u, .slot_count = 1u, .slot_offset = 0u};
    rates[1] = (RateDesc){.period_ticks = 30u, .slot_count = 2u, .slot_offset = 1u};
    rates[2] = (RateDesc){.period_ticks = 30u, .slot_count = 2u, .slot_offset = 3u};

    RateSlot *slots = (RateSlot *)(void *)(pattern + header->slot_table_offset);
    slots[0] = (RateSlot){
        .time_ticks = 0u,
        .flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
        .drive_mask = 0x0001FFFFu,
        .dir_mask = 0x0001FFFFu,
    };
    slots[1] = (RateSlot){
        .time_ticks = 0u,
        .flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
        .drive_mask = 0x0001FFFEu,
        .dir_mask = 0x0001FFFEu,
    };
    slots[2] = (RateSlot){
        .time_ticks = 15u,
        .flags = STPG_DRIVE_ONE | STPG_RATE_SLOT_DIR_UPDATE,
        .drive_mask = 0x00000001u,
        .dir_mask = 0x00000001u,
    };
    slots[3] = (RateSlot){
        .time_ticks = 0u,
        .flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
        .drive_mask = 0x0001FFFFu,
        .dir_mask = 0x0001FFFFu,
    };
    slots[4] = (RateSlot){
        .time_ticks = 15u,
        .flags = STPG_RATE_SLOT_SAMPLE,
        .sample_mask = 0x00FE0000u,
    };

    VectorRecord *vectors = (VectorRecord *)(void *)(pattern + header->vector_offset);
    vectors[0] = (VectorRecord){
        .rate_id = 0u, .repeat = 1u, .drive_value = 0u, .drive_enable = 0x0001FFFFu,
    };
    vectors[1] = (VectorRecord){
        .rate_id = 1u, .repeat = 1u, .drive_value = 1u, .drive_enable = 0x0001FFFFu,
    };
    vectors[2] = (VectorRecord){
        .rate_id = 0u, .repeat = 1u, .drive_value = 0x42u, .drive_enable = 0x0001FFFFu,
    };
    vectors[3] = (VectorRecord){
        .rate_id = 1u, .repeat = 1u, .drive_value = 0x43u, .drive_enable = 0x0001FFFFu,
    };
    for (uint32_t index = 4u; index < vector_count; ++index) {
        vectors[index] = (VectorRecord){
            .rate_id = 2u,
            .repeat = index == 5u ? 1u : 1024u,
            .drive_value = 0x42u,
            .drive_enable = 0x0001FFFFu,
            .expect_value = 0x00140000u,
            .expect_mask = 0x00FE0000u,
        };
    }

    header->data_crc32 = compute_crc32(pattern + STPG_RATEVEC_HEADER_SIZE, payload_size);
    finalize_header_crc(header);
    *out_size = total_size;
    *out_crc = compute_crc32(pattern, total_size);
    return pattern;
}

static int test_generic_repeat_boundary_exit_progress(void) {
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint8_t *pattern = build_repeat_boundary_exit_pattern(&total_size, &total_crc);
    if (pattern == NULL) return 1;
    if (total_size != 308u) {
        fprintf(stderr, "repeat-boundary fixture size mismatch: %u/308\n",
                (unsigned)total_size);
        free(pattern);
        return 1;
    }

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }
    if (stpg_buffer_event_count() != 4104u) {
        fprintf(stderr, "repeat-boundary upload event mismatch: %u/4104\n",
                (unsigned)stpg_buffer_event_count());
        free(pattern);
        return 1;
    }

    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) {
        fprintf(stderr, "repeat-boundary direct preparation failed\n");
        free(pattern);
        return 1;
    }
    if (stpg_buffer_event_count() != 4104u
        || stpg_buffer_pio_word_count() != 4104u * 4u
        || stpg_buffer_sample_event_count() != 2049u) {
        fprintf(stderr,
                "repeat-boundary direct result mismatch: events=%u words=%u samples=%u\n",
                (unsigned)stpg_buffer_event_count(),
                (unsigned)stpg_buffer_pio_word_count(),
                (unsigned)stpg_buffer_sample_event_count());
        free(pattern);
        return 1;
    }

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }
    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    if (state.reduced_density_compare_enabled
        || state.loop_block_count != 0u
        || stpg_buffer_stream_expected_sample_count() != 2049u) {
        fprintf(stderr,
                "repeat-boundary generic preparation mismatch: reduced=%d loops=%u samples=%u\n",
                state.reduced_density_compare_enabled ? 1 : 0,
                (unsigned)state.loop_block_count,
                (unsigned)stpg_buffer_stream_expected_sample_count());
        free(pattern);
        return 1;
    }

    uint32_t chunk_words[1024u * 4u];
    static const uint32_t expected_chunk_events[] = {1024u, 1024u, 1024u, 1024u, 8u};
    uint32_t total_output_events = 0u;
    for (uint32_t chunk = 0u; chunk < 5u; ++chunk) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(
                &state, chunk_words, 1024u, &result), ERR_NONE)) {
            free(pattern);
            return 1;
        }
        if (result.event_count != expected_chunk_events[chunk]
            || result.word_count != result.event_count * 4u
            || result.eof != (chunk == 4u)) {
            fprintf(stderr,
                    "repeat-boundary generic chunk %u mismatch: events=%u words=%u samples=%u eof=%d\n",
                    (unsigned)chunk,
                    (unsigned)result.event_count,
                    (unsigned)result.word_count,
                    (unsigned)result.sample_event_count,
                    result.eof ? 1 : 0);
            free(pattern);
            return 1;
        }
        total_output_events += result.event_count;
    }

    free(pattern);
    if (total_output_events != 4104u
        || state.vector_index != 7u
        || !state.eof) {
        fprintf(stderr,
                "repeat-boundary generic final mismatch: events=%u vector=%u eof=%d\n",
                (unsigned)total_output_events,
                (unsigned)state.vector_index,
                state.eof ? 1 : 0);
        return 1;
    }
    return 0;
}

static int test_sample_only_production_route_matches_output_events(void) {
    const uint32_t vector_count = 29u;
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_word_count = 0u;
    uint32_t expected_sample_count = 0u;
    uint8_t *pattern = build_pattern(
        vector_count,
        1,
        1u,
        &total_size,
        &total_crc,
        &expected_word_count,
        &expected_sample_count);
    if (pattern == NULL) return 1;

    /* Match the 4-1 reproduction: the delayed group is sample-only.  The
     * generic helper also exercises a drive-zero group, which intentionally
     * selects the generic four-word path instead of Reduced streaming. */
    RateVectorHeader *header = (RateVectorHeader *)(void *)pattern;
    RateSlot *slots = (RateSlot *)(void *)(pattern + header->slot_table_offset);
    slots[1].flags = STPG_RATE_SLOT_SAMPLE;
    slots[1].drive_mask = 0u;
    slots[1].dir_mask = 0u;
    slots[1].sample_mask = 0x003Fu;
    const uint32_t payload_size = total_size - STPG_RATEVEC_HEADER_SIZE;
    header->data_crc32 = compute_crc32(pattern + STPG_RATEVEC_HEADER_SIZE, payload_size);
    finalize_header_crc(header);
    total_crc = compute_crc32(pattern, total_size);

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    if (state.reduced_density_compare_enabled
        || stpg_buffer_stream_expected_sample_count() != expected_sample_count) {
        fprintf(stderr, "sample-only PSRAM route is not standard: reduced=%d expected=%u actual=%u\n",
                state.reduced_density_compare_enabled ? 1 : 0,
                (unsigned)expected_sample_count,
                (unsigned)stpg_buffer_stream_expected_sample_count());
        free(pattern);
        return 1;
    }

    uint32_t chunk_words[7u * 4u];
    uint32_t total_events = 0u;
    uint32_t total_schedule_words = 0u;
    while (true) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(
                &state, chunk_words, 7u, &result), ERR_NONE)) {
            free(pattern);
            return 1;
        }
        total_events += result.event_count;
        total_schedule_words += result.sample_event_count;
        if (result.word_count != result.event_count * 4u) {
            fprintf(stderr, "sample-only four-word result mismatch: events=%u samples=%u words=%u\n",
                    (unsigned)result.event_count,
                    (unsigned)result.sample_event_count,
                    (unsigned)result.word_count);
            free(pattern);
            return 1;
        }
        for (uint32_t offset = 0u; offset < result.event_count; ++offset) {
            if (chunk_words[offset * 4u + 1u] != ((total_events - result.event_count + offset) % 2u)) {
                fprintf(stderr, "sample-only trigger mismatch: event=%u trigger=%u\n",
                        (unsigned)(total_events - result.event_count + offset), (unsigned)chunk_words[offset * 4u + 1u]);
                free(pattern);
                return 1;
            }
        }
        if (result.eof) break;
    }

    const int failed = total_events != vector_count * 2u
        || total_schedule_words != expected_sample_count;
    if (failed) {
        fprintf(stderr, "sample-only four-word totals mismatch: events=%u/%u samples=%u/%u\n",
                (unsigned)total_events, (unsigned)(vector_count * 2u),
                (unsigned)total_schedule_words, (unsigned)expected_sample_count);
    }
    free(pattern);
    return failed ? 1 : 0;
}

static uint8_t *build_generic_compare_pattern(
    uint32_t vector_count,
    uint32_t *out_size,
    uint32_t *out_total_crc,
    uint32_t *out_expected_word_count,
    uint32_t *out_expected_sample_count) {
    const uint32_t rate_count = 1u;
    const uint32_t slot_count = 3u;
    const uint32_t logical_mask = 0x003Fu;
    const uint32_t expected_word_count = vector_count * 3u * 4u;
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

    RateDesc *rate = (RateDesc *)(void *)(buffer + header->rate_table_offset);
    rate->period_ticks = 45u;
    rate->slot_count = slot_count;
    rate->slot_offset = 0u;

    RateSlot *slots = (RateSlot *)(void *)(buffer + header->slot_table_offset);
    slots[0].time_ticks = 0u;
    slots[0].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    slots[0].drive_mask = logical_mask;
    slots[0].dir_mask = logical_mask;
    slots[1].time_ticks = 15u;
    slots[1].flags = STPG_DRIVE_ZERO;
    slots[1].drive_mask = logical_mask;
    slots[2].time_ticks = 30u;
    slots[2].flags = STPG_RATE_SLOT_SAMPLE;
    slots[2].sample_mask = logical_mask;

    VectorRecord *vectors = (VectorRecord *)(void *)(buffer + header->vector_offset);
    for (uint32_t index = 0u; index < vector_count; ++index) {
        vectors[index].rate_id = 0u;
        vectors[index].repeat = 1u;
        vectors[index].drive_value = (index & 1u) != 0u ? 0x0015u : 0x002Au;
        vectors[index].drive_enable = logical_mask;
        /* The third group drives zero before sampling, so compare against 0. */
        vectors[index].expect_value = 0u;
        vectors[index].expect_mask = logical_mask;
    }

    header->data_crc32 = compute_crc32(buffer + STPG_RATEVEC_HEADER_SIZE, payload_size);
    finalize_header_crc(header);
    *out_size = total_size;
    *out_total_crc = compute_crc32(buffer, total_size);
    *out_expected_word_count = expected_word_count;
    *out_expected_sample_count = vector_count;
    return buffer;
}

static int test_generic_compare_sample_stream_matches_full_expansion(void) {
    const uint32_t vector_count = 19u;
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_word_count = 0u;
    uint32_t expected_sample_count = 0u;
    uint8_t *pattern = build_generic_compare_pattern(
        vector_count,
        &total_size,
        &total_crc,
        &expected_word_count,
        &expected_sample_count);
    if (pattern == NULL) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }
    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    const uint32_t *expanded_words = stpg_buffer_pio_words();
    if (stpg_buffer_pio_word_count() != expected_word_count
        || stpg_buffer_sample_event_count() != expected_sample_count) {
        fprintf(stderr, "generic full expansion metadata mismatch: words=%u samples=%u\n",
                (unsigned)stpg_buffer_pio_word_count(),
                (unsigned)stpg_buffer_sample_event_count());
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    if (state.reduced_density_compare_enabled) {
        fprintf(stderr, "generic three-group pattern incorrectly selected reduced path\n");
        free(pattern);
        return 1;
    }

    uint32_t chunk_words[4u * 4u];
    uint32_t *collected_words = (uint32_t *)calloc(expected_word_count, sizeof(uint32_t));
    if (collected_words == NULL) {
        free(pattern);
        return 1;
    }
    uint32_t collected_word_count = 0u;
    uint32_t collected_sample_count = 0u;
    while (true) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 4u, &result), ERR_NONE)) {
            free(collected_words);
            free(pattern);
            return 1;
        }
        collected_sample_count += result.sample_event_count;
        memcpy(collected_words + collected_word_count,
               chunk_words,
               result.event_count * 4u * sizeof(uint32_t));
        collected_word_count += result.event_count * 4u;
        if (result.eof) break;
    }

    int failed = collected_word_count != expected_word_count
        || collected_sample_count != expected_sample_count
        || memcmp(collected_words, expanded_words, expected_word_count * sizeof(uint32_t)) != 0
        || stpg_buffer_stream_compare_event_count() != 0u;

    if (!failed) {
        if (expect(stpg_samples_prepare_for_run(expected_sample_count, STPG_ACTIVE_PINS_V01), ERR_NONE)) {
            failed = 1;
        } else {
            uint32_t *samples = stpg_samples_dma_buffer();
            if (samples == NULL || stpg_samples_dma_capacity_words() < expected_sample_count) {
                failed = 1;
            } else {
                memset(samples, 0, expected_sample_count * sizeof(uint32_t));
                stpg_samples_finish_run(expected_sample_count, false);
                uint32_t compare_count = 0u;
                if (stpg_compare_run_stream_replayed(&state, 0u, 0u, &compare_count) != ERR_NONE
                    || compare_count != expected_sample_count
                    || stpg_compare_fail_count() != 0u) {
                    failed = 1;
                }
            }
        }
    }
    if (failed) {
        fprintf(stderr,
                "generic stream mismatch: words=%u/%u samples=%u/%u compare=%u fails=%u\n",
                (unsigned)collected_word_count,
                (unsigned)expected_word_count,
                (unsigned)collected_sample_count,
                (unsigned)expected_sample_count,
                (unsigned)stpg_buffer_stream_compare_event_count(),
                (unsigned)stpg_compare_fail_count());
    }
    free(collected_words);
    free(pattern);
    return failed ? 1 : 0;
}

static uint8_t *build_mixed_generic_compare_pattern(
    uint32_t vector_count,
    uint32_t *out_size,
    uint32_t *out_total_crc,
    uint32_t *out_expected_word_count,
    uint32_t *out_expected_sample_count) {
    const uint32_t rate_count = 5u;
    const uint32_t slot_count = 7u;
    const uint32_t slots_per_rate[5] = {2u, 2u, 1u, 1u, 1u};
    const uint32_t events_per_rate_cycle = 7u;
    const uint32_t expected_word_count = (vector_count / rate_count) * events_per_rate_cycle * 4u;
    const uint32_t expected_sample_count = (vector_count / rate_count) * 4u;
    const uint32_t payload_size =
        STPG_RATE_DESC_SIZE * rate_count
        + STPG_RATE_SLOT_SIZE * slot_count
        + STPG_VECTOR_RECORD_SIZE * vector_count;
    const uint32_t total_size = STPG_RATEVEC_HEADER_SIZE + payload_size;
    const uint32_t logical_mask = 0x003Fu;
    uint8_t *buffer = (uint8_t *)calloc(1u, total_size);
    if (buffer == NULL) return NULL;

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
    RateSlot *slots = (RateSlot *)(void *)(buffer + header->slot_table_offset);
    uint32_t slot_offset = 0u;
    for (uint32_t rate_id = 0u; rate_id < rate_count; ++rate_id) {
        rates[rate_id].period_ticks = 60u;
        rates[rate_id].slot_count = (uint8_t)slots_per_rate[rate_id];
        rates[rate_id].slot_offset = (uint16_t)slot_offset;
        slot_offset += slots_per_rate[rate_id];
    }

    slots[0].time_ticks = 0u;
    slots[0].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    slots[0].drive_mask = logical_mask;
    slots[0].dir_mask = logical_mask;
    slots[1].time_ticks = 30u;
    slots[1].flags = STPG_RATE_SLOT_SAMPLE;
    slots[1].sample_mask = logical_mask;

    slots[2].time_ticks = 0u;
    slots[2].flags = STPG_RATE_SLOT_SAMPLE;
    slots[2].sample_mask = logical_mask;
    slots[3].time_ticks = 30u;
    slots[3].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    slots[3].drive_mask = logical_mask;
    slots[3].dir_mask = logical_mask;

    slots[4].time_ticks = 0u;
    slots[4].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    slots[4].drive_mask = logical_mask;
    slots[4].dir_mask = logical_mask;

    slots[5].time_ticks = 30u;
    slots[5].flags = STPG_RATE_SLOT_SAMPLE;
    slots[5].sample_mask = logical_mask;

    slots[6].time_ticks = 0u;
    slots[6].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE | STPG_RATE_SLOT_SAMPLE;
    slots[6].drive_mask = logical_mask;
    slots[6].dir_mask = logical_mask;
    slots[6].sample_mask = logical_mask;

    VectorRecord *vectors = (VectorRecord *)(void *)(buffer + header->vector_offset);
    for (uint32_t index = 0u; index < vector_count; ++index) {
        vectors[index].rate_id = (uint8_t)(index % rate_count);
        vectors[index].repeat = 1u;
        vectors[index].drive_value = (index & 1u) != 0u ? 0x0015u : 0x002Au;
        vectors[index].drive_enable = logical_mask;
        vectors[index].expect_value = 0x002Au;
        vectors[index].expect_mask = logical_mask;
    }

    header->data_crc32 = compute_crc32(buffer + STPG_RATEVEC_HEADER_SIZE, payload_size);
    finalize_header_crc(header);
    *out_size = total_size;
    *out_total_crc = compute_crc32(buffer, total_size);
    *out_expected_word_count = expected_word_count;
    *out_expected_sample_count = expected_sample_count;
    return buffer;
}

static int test_all_supported_generic_compare_rate_families(void) {
    const uint32_t vector_count = 25u;
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_word_count = 0u;
    uint32_t expected_sample_count = 0u;
    uint8_t *pattern = build_mixed_generic_compare_pattern(
        vector_count,
        &total_size,
        &total_crc,
        &expected_word_count,
        &expected_sample_count);
    if (pattern == NULL) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }
    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    if (stpg_buffer_pio_word_count() != expected_word_count
        || stpg_buffer_sample_event_count() != expected_sample_count) {
        stpg_rate_info_t debug_info;
        (void)stpg_buffer_get_rate_info(&debug_info);
        fprintf(stderr, "mixed full metadata: words=%u/%u samples=%u/%u events=%u\n",
                (unsigned)stpg_buffer_pio_word_count(),
                (unsigned)expected_word_count,
                (unsigned)stpg_buffer_sample_event_count(),
                (unsigned)expected_sample_count,
                (unsigned)stpg_buffer_event_count());
        fprintf(stderr, "mixed info: vectors=%u rates=%u slots=%u estimate=%u loaded=%u\n",
                (unsigned)debug_info.vector_count,
                (unsigned)debug_info.rate_count,
                (unsigned)debug_info.slot_count,
                (unsigned)debug_info.expanded_vector_count_estimate,
                debug_info.loaded ? 1u : 0u);
        free(pattern);
        return 1;
    }
    const uint32_t *expanded_words = stpg_buffer_pio_words();

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    if (state.reduced_density_compare_enabled) {
        fprintf(stderr, "mixed generic families incorrectly selected reduced path\n");
        free(pattern);
        return 1;
    }

    uint32_t *collected_words = (uint32_t *)calloc(expected_word_count, sizeof(uint32_t));
    if (collected_words == NULL) {
        free(pattern);
        return 1;
    }
    uint32_t collected_word_count = 0u;
    uint32_t collected_sample_count = 0u;
    while (true) {
        uint32_t chunk_words[3u * 4u];
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 3u, &result), ERR_NONE)) {
            free(collected_words);
            free(pattern);
            return 1;
        }
        memcpy(collected_words + collected_word_count,
               chunk_words,
               result.event_count * 4u * sizeof(uint32_t));
        collected_word_count += result.event_count * 4u;
        collected_sample_count += result.sample_event_count;
        if (result.eof) break;
        if (result.event_count == 0u) {
            free(collected_words);
            free(pattern);
            return 1;
        }
    }

    int failed = collected_word_count != expected_word_count
        || collected_sample_count != expected_sample_count
        || memcmp(collected_words, expanded_words, expected_word_count * sizeof(uint32_t)) != 0
        || stpg_buffer_stream_compare_event_count() != 0u;
    if (failed) {
        fprintf(stderr, "mixed stream output: words=%u/%u samples=%u/%u compare=%u\n",
                (unsigned)collected_word_count,
                (unsigned)expected_word_count,
                (unsigned)collected_sample_count,
                (unsigned)expected_sample_count,
                (unsigned)stpg_buffer_stream_compare_event_count());
    }
    if (!failed) {
        if (expect(stpg_samples_prepare_for_run(expected_sample_count, STPG_ACTIVE_PINS_V01), ERR_NONE)) {
            failed = 1;
        } else {
            uint32_t *samples = stpg_samples_dma_buffer();
            if (samples == NULL || stpg_samples_dma_capacity_words() < expected_sample_count) {
                failed = 1;
            } else {
                for (uint32_t index = 0u; index < expected_sample_count; ++index) {
                    samples[index] = 0x002Au;
                }
                stpg_samples_finish_run(expected_sample_count, false);
                uint32_t compare_count = 0u;
                if (expect(stpg_compare_run_stream_replayed(&state, 0u, 0u, &compare_count), ERR_NONE)
                    || compare_count != expected_sample_count
                    || stpg_compare_fail_count() != 0u) {
                    failed = 1;
                }
            }
        }
    }
    if (failed) {
        fprintf(stderr,
                "mixed generic family mismatch: words=%u/%u samples=%u/%u fails=%u\n",
                (unsigned)collected_word_count,
                (unsigned)expected_word_count,
                (unsigned)collected_sample_count,
                (unsigned)expected_sample_count,
                (unsigned)stpg_compare_fail_count());
    }
    free(collected_words);
    free(pattern);
    return failed ? 1 : 0;
}
#endif

static int test_large_compare_free_rate_vector_is_streaming_only(void) {
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_word_count = 0u;
    uint32_t expected_sample_count = 0u;
    uint8_t *pattern = build_pattern(5u,
                                     0,
                                     4096u,
                                     &total_size,
                                     &total_crc,
                                     &expected_word_count,
                                     &expected_sample_count);
    if (pattern == NULL) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }
    if (expected_word_count / 4u != 40960u || expected_sample_count != 0u
        || stpg_buffer_event_count() != 40960u) {
        fprintf(stderr, "large upload metadata mismatch: event=%u expected=40960\n",
                (unsigned)stpg_buffer_event_count());
        free(pattern);
        return 1;
    }
    if (expect(stpg_buffer_prepare_for_run(), ERR_BAD_SIZE)) {
        fprintf(stderr, "large compare-free vector unexpectedly allowed full expansion\n");
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        fprintf(stderr, "large compare-free vector rejected by streaming preparation\n");
        free(pattern);
        return 1;
    }
    uint32_t chunk_words[1024u * 4u];
    uint32_t streamed_events = 0u;
    while (true) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 1024u, &result), ERR_NONE)) {
            fprintf(stderr, "large compare-free stream fill failed\n");
            free(pattern);
            return 1;
        }
        streamed_events += result.event_count;
        if (result.eof) break;
    }
    free(pattern);
    if (streamed_events != 40960u || stpg_buffer_stream_compare_event_count() != 0u) {
        fprintf(stderr, "large stream mismatch: events=%u compare=%u\n",
                (unsigned)streamed_events,
                (unsigned)stpg_buffer_stream_compare_event_count());
        return 1;
    }
    return 0;
}

static int test_five_thousand_vector_cache_refill_and_fail_closed_validation(void) {
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_word_count = 0u;
    uint32_t expected_sample_count = 0u;
    const uint32_t vector_count = 5000u;
    uint8_t *pattern = build_pattern(vector_count,
                                     0,
                                     1u,
                                     &total_size,
                                     &total_crc,
                                     &expected_word_count,
                                     &expected_sample_count);
    if (pattern == NULL) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    uint32_t chunk_words[1024u * 4u];
    uint32_t streamed_events = 0u;
    while (true) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 1024u, &result), ERR_NONE)) {
            fprintf(stderr, "5000-vector stream fill failed before corruption injection\n");
            free(pattern);
            return 1;
        }
        streamed_events += result.event_count;
        if (result.eof) break;
    }
    if (streamed_events != expected_word_count / 4u || expected_sample_count != 0u
        || expected_word_count / 4u != vector_count * 2u) {
        fprintf(stderr, "5000-vector stream mismatch: events=%u expected=%u\n",
                (unsigned)streamed_events, (unsigned)(vector_count * 2u));
        free(pattern);
        return 1;
    }

    /* A malformed record in a later cache window must be rejected before the
     * cache is published to the producer.  This is a host-side fail-closed
     * contract test; the physical campaign supplies the QMI/SRAM fault case. */
    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }
    RateVectorHeader *header = (RateVectorHeader *)stpg_psram_mapped_ptr(
        STPG_PSRAM_PATTERN_BASE, sizeof(RateVectorHeader));
    if (header == NULL) {
        free(pattern);
        return 1;
    }
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        fprintf(stderr, "5000-vector diagnostic setup failed\n");
        free(pattern);
        return 1;
    }
    VectorRecord *corrupt = (VectorRecord *)stpg_psram_mapped_ptr(
        STPG_PSRAM_PATTERN_BASE + header->vector_offset + (3074u * STPG_VECTOR_RECORD_SIZE),
        sizeof(VectorRecord));
    if (corrupt == NULL) {
        free(pattern);
        return 1;
    }
    corrupt->rate_id = header->rate_count;

    bool rejected = false;
    streamed_events = 0u;
    while (true) {
        stpg_stream_chunk_result_t result;
        const stpg_err_t err = stpg_buffer_stream_fill_next_chunk(
            &state, chunk_words, 1024u, &result);
        if (err != ERR_NONE) {
            rejected = true;
            break;
        }
        streamed_events += result.event_count;
        if (result.eof) break;
    }
    stpg_stream_buffer_telemetry_t telemetry;
    stpg_buffer_get_stream_telemetry(&telemetry);
    if (!rejected
        || telemetry.prefetch_diag_failure_stage != STPG_STREAM_PREFETCH_DIAG_VALIDATION_ERROR
        || telemetry.prefetch_diag_failure_error != ERR_VECTOR_BAD_RATE_ID
        || telemetry.prefetch_diag_failure_record_index != 3074u
        || telemetry.prefetch_diag_failure_record_offset == UINT32_MAX
        || (telemetry.prefetch_diag_failure_words[0] & 0xFFu) != header->rate_count
        || telemetry.prefetch_diag_last_success_record_count == 0u
        || telemetry.prefetch_diag_attempt_record_count == 0u) {
        fprintf(stderr,
                "prefetch diagnostic mismatch: rejected=%d stage=%u error=%u index=%u offset=%u raw0=%u last_success=%u attempted=%u\n",
                rejected,
                (unsigned)telemetry.prefetch_diag_failure_stage,
                (unsigned)telemetry.prefetch_diag_failure_error,
                (unsigned)telemetry.prefetch_diag_failure_record_index,
                (unsigned)telemetry.prefetch_diag_failure_record_offset,
                (unsigned)telemetry.prefetch_diag_failure_words[0],
                (unsigned)telemetry.prefetch_diag_last_success_record_count,
                (unsigned)telemetry.prefetch_diag_attempt_record_count);
        free(pattern);
        return 1;
    }
    free(pattern);
    return rejected ? 0 : 1;
}

#if STPG_STREAM_RATE_VECTOR_EVENT_LIMIT > 40960u
static int test_psram_capacity_compare_free_rate_vector(void) {
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_word_count = 0u;
    uint32_t expected_sample_count = 0u;
    const uint32_t vector_count = 20481u;
    uint8_t *pattern = build_pattern(vector_count,
                                     0,
                                     1u,
                                     &total_size,
                                     &total_crc,
                                     &expected_word_count,
                                     &expected_sample_count);
    if (pattern == NULL) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }
    if (expected_word_count / 4u != 40962u || expected_sample_count != 0u
        || stpg_buffer_event_count() != 40962u) {
        fprintf(stderr, "PSRAM capacity metadata mismatch: event=%u expected=40962\n",
                (unsigned)stpg_buffer_event_count());
        free(pattern);
        return 1;
    }
    if (expect(stpg_buffer_prepare_for_run(), ERR_BAD_SIZE)) {
        fprintf(stderr, "PSRAM capacity vector unexpectedly allowed full expansion\n");
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        fprintf(stderr, "PSRAM capacity compare-free vector rejected by streaming preparation\n");
        free(pattern);
        return 1;
    }
    uint32_t chunk_words[1024u * 4u];
    uint32_t streamed_events = 0u;
    while (true) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 1024u, &result), ERR_NONE)) {
            fprintf(stderr, "PSRAM capacity stream fill failed\n");
            free(pattern);
            return 1;
        }
        streamed_events += result.event_count;
        if (result.eof) break;
    }
    free(pattern);
    if (streamed_events != 40962u || stpg_buffer_stream_compare_event_count() != 0u) {
        fprintf(stderr, "PSRAM capacity stream mismatch: events=%u compare=%u\n",
                (unsigned)streamed_events,
                (unsigned)stpg_buffer_stream_compare_event_count());
        return 1;
    }
    return 0;
}
#endif

#if !STPG_STREAM_PSRAM_COMPARE_SAMPLE
static int test_reduced_compare_fast_path_across_cache_refill(void) {
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_event_count = 0u;
    uint32_t expected_fast_count = 0u;
    uint32_t expected_fallback_count = 0u;
    uint8_t *pattern = build_reduced_pattern(STPG_STREAM_VECTOR_CACHE_RECORDS + 37u,
                                             0,
                                             &total_size,
                                             &total_crc,
                                             &expected_event_count,
                                             &expected_fast_count,
                                             &expected_fallback_count);
    if (pattern == NULL) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    state.reduced_density_compare_enabled = true;

    uint32_t chunk_words[11u * 3u];
    uint32_t sample_waits[11u];
    uint32_t total_events = 0u;
    while (true) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk_reduced_compare(&state, chunk_words, sample_waits, 11u, &result), ERR_NONE)) {
            free(pattern);
            return 1;
        }
        total_events += result.event_count;
        if (result.event_count == 0u && result.eof) break;
        if (result.eof) break;
    }

    stpg_stream_buffer_telemetry_t telemetry;
    stpg_buffer_get_stream_telemetry(&telemetry);
    const int failed =
        (total_events != expected_event_count)
        || (stpg_buffer_stream_compare_event_count() != expected_event_count)
        || (telemetry.fast_vector_count != expected_fast_count)
        || (telemetry.fallback_vector_count != expected_fallback_count);
    if (failed) {
        fprintf(stderr, "reduced fast mismatch: events=%u compare=%u fast=%u fallback=%u\n",
                (unsigned)total_events,
                (unsigned)stpg_buffer_stream_compare_event_count(),
                (unsigned)telemetry.fast_vector_count,
                (unsigned)telemetry.fallback_vector_count);
    }

    free(pattern);
    return failed ? 1 : 0;
}

static int test_reduced_compare_mixed_repeat_uses_fallback(void) {
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_event_count = 0u;
    uint32_t expected_fast_count = 0u;
    uint32_t expected_fallback_count = 0u;
    uint8_t *pattern = build_reduced_pattern(97u,
                                             1,
                                             &total_size,
                                             &total_crc,
                                             &expected_event_count,
                                             &expected_fast_count,
                                             &expected_fallback_count);
    if (pattern == NULL) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    state.reduced_density_compare_enabled = true;

    uint32_t chunk_words[9u * 3u];
    uint32_t sample_waits[9u];
    uint32_t total_events = 0u;
    while (true) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk_reduced_compare(&state, chunk_words, sample_waits, 9u, &result), ERR_NONE)) {
            free(pattern);
            return 1;
        }
        total_events += result.event_count;
        if (result.event_count == 0u && result.eof) break;
        if (result.eof) break;
    }

    stpg_stream_buffer_telemetry_t telemetry;
    stpg_buffer_get_stream_telemetry(&telemetry);
    const int failed =
        (total_events != expected_event_count)
        || (stpg_buffer_stream_compare_event_count() != expected_event_count)
        || (telemetry.fast_vector_count != expected_fast_count)
        || (telemetry.fallback_vector_count != expected_fallback_count)
        || (telemetry.fallback_vector_count == 0u);
    if (failed) {
        fprintf(stderr, "reduced mixed mismatch: events=%u compare=%u fast=%u fallback=%u expected_fast=%u expected_fallback=%u\n",
                (unsigned)total_events,
                (unsigned)stpg_buffer_stream_compare_event_count(),
                (unsigned)telemetry.fast_vector_count,
                (unsigned)telemetry.fallback_vector_count,
                (unsigned)expected_fast_count,
                (unsigned)expected_fallback_count);
    }

    free(pattern);
    return failed ? 1 : 0;
}

static uint8_t *build_mixed_rate_compare_pattern(
    uint32_t vector_count,
    uint32_t *out_size,
    uint32_t *out_total_crc,
    uint32_t *out_expected_sample_count) {
    const uint32_t rate_count = 2u;
    const uint32_t slot_count = 3u;
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
    slots[0].drive_mask = 0x003Fu;
    slots[0].dir_mask = 0x003Fu;
    slots[1].time_ticks = 0u;
    slots[1].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    slots[1].drive_mask = 0x003Fu;
    slots[1].dir_mask = 0x003Fu;
    slots[2].time_ticks = 15u;
    slots[2].flags = STPG_RATE_SLOT_SAMPLE;
    slots[2].sample_mask = 0x003Fu;

    VectorRecord *vectors = (VectorRecord *)(void *)(buffer + header->vector_offset);
    uint32_t expected_sample_count = 0u;
    for (uint32_t index = 0u; index < vector_count; ++index) {
        const bool sampled = (index % 3u) != 0u;
        vectors[index].rate_id = sampled ? 1u : 0u;
        vectors[index].repeat = 1u;
        vectors[index].drive_value = (index & 1u) ? 0x0015u : 0x002Au;
        vectors[index].drive_enable = 0x003Fu;
        vectors[index].expect_value = vectors[index].drive_value;
        vectors[index].expect_mask = sampled ? 0x003Fu : 0u;
        expected_sample_count += sampled ? 1u : 0u;
    }

    header->data_crc32 = compute_crc32(buffer + STPG_RATEVEC_HEADER_SIZE, payload_size);
    finalize_header_crc(header);
    *out_size = total_size;
    *out_total_crc = compute_crc32(buffer, total_size);
    *out_expected_sample_count = expected_sample_count;
    return buffer;
}

static int test_reduced_compare_mixed_rate_shapes(void) {
    uint32_t total_size = 0u;
    uint32_t total_crc = 0u;
    uint32_t expected_sample_count = 0u;
    const uint32_t vector_count = 73u;
    uint8_t *pattern = build_mixed_rate_compare_pattern(
        vector_count, &total_size, &total_crc, &expected_sample_count);
    if (pattern == NULL) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(pattern, total_size, total_crc)) {
        free(pattern);
        return 1;
    }

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) {
        free(pattern);
        return 1;
    }
    if (!state.reduced_density_compare_enabled
        || stpg_buffer_stream_expected_sample_count() != expected_sample_count) {
        fprintf(stderr, "mixed-rate reduced path not enabled: enabled=%d expected=%u actual=%u\n",
                state.reduced_density_compare_enabled ? 1 : 0,
                (unsigned)expected_sample_count,
                (unsigned)stpg_buffer_stream_expected_sample_count());
        free(pattern);
        return 1;
    }

    uint32_t chunk_words[13u * 3u];
    uint32_t sample_waits[13u];
    uint32_t total_events = 0u;
    uint32_t total_schedule_words = 0u;
    while (true) {
        const uint32_t vector_start = state.vector_index;
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk_reduced_compare(
                &state, chunk_words, sample_waits, 13u, &result), ERR_NONE)) {
            free(pattern);
            return 1;
        }
        total_events += result.event_count;
        total_schedule_words += result.sample_event_count;
        if (result.sample_event_count != result.event_count) {
            fprintf(stderr, "mixed-rate schedule mismatch: schedule=%u events=%u\n",
                    (unsigned)result.sample_event_count, (unsigned)result.event_count);
            free(pattern);
            return 1;
        }
        for (uint32_t offset = 0u; offset < result.event_count; ++offset) {
            const bool sampled = ((vector_start + offset) % 3u) != 0u;
            const uint32_t expected_schedule = sampled ? ((15u - 1u) << 1u) | 1u : 0u;
            if (sample_waits[offset] != expected_schedule) {
                fprintf(stderr, "mixed-rate schedule marker mismatch at vector=%u\n",
                        (unsigned)(vector_start + offset));
                free(pattern);
                return 1;
            }
        }
        if (result.eof) break;
    }

    const uint32_t expected_compare_count = expected_sample_count;
    int failed = total_events != vector_count
        || total_schedule_words != vector_count
        || stpg_buffer_stream_compare_event_count() != expected_compare_count;
    if (!failed) {
        const ExecEvent *compare_events = stpg_buffer_stream_compare_events();
        const uint32_t *compare_indices = stpg_buffer_stream_compare_event_indices();
        uint32_t compare_cursor = 0u;
        for (uint32_t vector_index = 0u; vector_index < vector_count; ++vector_index) {
            if ((vector_index % 3u) == 0u) {
                continue;
            }
            if (compare_indices[compare_cursor] != vector_index
                || compare_events[compare_cursor].flags != (STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT)
                || compare_events[compare_cursor].sample_mask != 0x003Fu) {
                fprintf(stderr, "mixed-rate compare metadata mismatch at cursor=%u vector=%u\n",
                        (unsigned)compare_cursor, (unsigned)vector_index);
                failed = 1;
                break;
            }
            compare_cursor += 1u;
        }
    }
    if (failed) {
        fprintf(stderr, "mixed-rate reduced mismatch: events=%u expected_events=%u compare=%u expected_compare=%u\n",
                (unsigned)total_events, (unsigned)vector_count,
                (unsigned)stpg_buffer_stream_compare_event_count(),
                (unsigned)expected_compare_count);
    }
    free(pattern);
    return failed ? 1 : 0;
}
#endif

int main(void) {
    if (test_stream_state_storage_is_single_and_stable()) return 1;
    if (test_precomputed_uop_cache_and_inline_sample_count()) return 1;
    if (test_chunked_stream_matches_full_expansion_across_cache_refill()) return 1;
    if (test_compare_stream_metadata_matches_full_expansion()) return 1;
#if STPG_STREAM_FAIL_STOP
    if (test_compare24_fail_stop_slot_sample_count()) return 1;
#endif
    if (test_repeat_block_stream_matches_full_expansion()) return 1;
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
    if (test_generic_repeat_boundary_exit_progress()) return 1;
    if (test_sample_only_production_route_matches_output_events()) return 1;
    if (test_generic_compare_sample_stream_matches_full_expansion()) return 1;
    if (test_all_supported_generic_compare_rate_families()) return 1;
#endif
    if (test_large_compare_free_rate_vector_is_streaming_only()) return 1;
    if (test_five_thousand_vector_cache_refill_and_fail_closed_validation()) return 1;
#if STPG_STREAM_RATE_VECTOR_EVENT_LIMIT > 40960u
    if (test_psram_capacity_compare_free_rate_vector()) return 1;
#endif
#if !STPG_STREAM_PSRAM_COMPARE_SAMPLE
    /* PSRAM compare/sample streaming defers this metadata to replay; the
     * legacy reduced-compare helpers intentionally remain covered by their
     * non-PSRAM diagnostic target. */
    if (test_reduced_compare_fast_path_across_cache_refill()) return 1;
    if (test_reduced_compare_mixed_repeat_uses_fallback()) return 1;
    if (test_reduced_compare_mixed_rate_shapes()) return 1;
#endif
    return 0;
}
