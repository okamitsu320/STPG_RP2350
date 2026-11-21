/* Synthetic fixtures only: no device, repository artifacts, or external paths. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Keep the private cache contract testable without adding a production API. */
#include "../src/pattern_buffer.c"

#define CHECK(condition) do { \
    if (!(condition)) { \
        fprintf(stderr, "%s:%d: %s\n", __func__, __LINE__, #condition); \
        return 1; \
    } \
} while (0)

static stpg_rate_stream_state_t *fixture(uint32_t rates, uint32_t pins) {
    stpg_rate_stream_state_t *state = stpg_buffer_stream_state_storage();
    memset(state, 0, sizeof(*state));
    memset(&g_stream_telemetry, 0, sizeof(g_stream_telemetry));
    state->header.rate_count = rates;
    state->header.active_pins = pins;
    for (uint32_t r = 0; r < rates; ++r) {
        stpg_rate_runtime_t *runtime = &state->rate_runtime[r];
        runtime->rate.period_ticks = 128u + r * 8u;
        runtime->rate.slot_count = 1u;
        runtime->group_count = 1u;
        runtime->slots[0].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
        runtime->slots[0].drive_mask = runtime->slots[0].dir_mask = 3u;
        runtime->groups[0].slot_count = 1u;
        runtime->groups[0].gap_from_cycle_start_ticks = runtime->rate.period_ticks;
    }
    return state;
}

static int check_resolution(stpg_rate_stream_state_t *state) {
    for (uint32_t r = 0; r < state->header.rate_count; ++r) {
        for (uint32_t g = 0; g < state->rate_runtime[r].group_count; ++g) {
            stpg_rate_group_uop_t expected, fallback;
            const stpg_rate_group_uop_t *resolved = NULL;
            CHECK(stpg_buffer_build_rate_group_uop(&state->rate_runtime[r], g,
                state->header.active_pins, &expected) == ERR_NONE);
            CHECK(stpg_buffer_resolve_rate_group_uop(state, &state->rate_runtime[r], r, g,
                state->header.active_pins, &fallback, &resolved) == ERR_NONE);
            CHECK(memcmp(&expected, resolved, sizeof(expected)) == 0);
            const uint8_t index = state->precomputed_uop_index[r][g];
            CHECK(index == 0xffu ? resolved == &fallback
                  : resolved == &state->precomputed_group_uops[index]);
        }
    }
    return 0;
}

static int test_distinct_capacity_boundaries(void) {
    for (uint32_t required = 31u; required <= 33u; ++required) {
        stpg_rate_stream_state_t *state = fixture(required, 16u);
        CHECK(stpg_buffer_prepare_precomputed_rate_group_uops(state) == ERR_NONE);
        CHECK(state->precomputed_uop_count == (required < 32u ? required : 32u));
        CHECK(state->precomputed_uops_complete == (required <= 32u));
        CHECK(check_resolution(state) == 0);
        CHECK(g_stream_telemetry.precomputed_uop_cache_hit_count == (required < 32u ? required : 32u));
        CHECK(g_stream_telemetry.precomputed_uop_cache_fallback_count == (required > 32u ? 1u : 0u));
    }
    return 0;
}

static int test_duplicate_after_capacity_and_full_sharing(void) {
    stpg_rate_stream_state_t *state = fixture(34u, 24u);
    state->rate_runtime[33] = state->rate_runtime[0];
    CHECK(stpg_buffer_prepare_precomputed_rate_group_uops(state) == ERR_NONE);
    CHECK(state->precomputed_uop_count == 32u && !state->precomputed_uops_complete);
    CHECK(state->precomputed_uop_index[32][0] == 0xffu);
    CHECK(state->precomputed_uop_index[33][0] == state->precomputed_uop_index[0][0]);
    CHECK(check_resolution(state) == 0);
    CHECK(g_stream_telemetry.precomputed_uop_cache_hit_count == 33u);
    CHECK(g_stream_telemetry.precomputed_uop_cache_build_count == 1u);

    state = fixture(64u, 16u);
    for (uint32_t r = 1; r < 64u; ++r) state->rate_runtime[r] = state->rate_runtime[0];
    CHECK(stpg_buffer_prepare_precomputed_rate_group_uops(state) == ERR_NONE);
    CHECK(state->precomputed_uop_count == 1u && state->precomputed_uops_complete);
    CHECK(check_resolution(state) == 0);
    CHECK(g_stream_telemetry.precomputed_uop_cache_hit_count == 64u);
    CHECK(g_stream_telemetry.precomputed_uop_cache_fallback_count == 0u);
    return 0;
}

static int test_maximum_group_table_and_late_invalid_group(void) {
    stpg_rate_stream_state_t *state = fixture(64u, 24u);
    for (uint32_t r = 0; r < 64u; ++r) {
        stpg_rate_runtime_t *runtime = &state->rate_runtime[r];
        runtime->rate.slot_count = runtime->group_count = 8u;
        for (uint32_t g = 0; g < 8u; ++g) {
            runtime->slots[g] = runtime->slots[0];
            runtime->slots[g].time_ticks = g * 8u;
            runtime->slots[g].drive_mask = r + 1u;
            runtime->groups[g] = runtime->groups[0];
            runtime->groups[g].first_slot_index = (uint8_t)g;
            runtime->groups[g].time_ticks = g * 8u;
            runtime->groups[g].gap_from_prev_ticks = g ? 8u : 0u;
        }
    }
    CHECK(stpg_buffer_prepare_precomputed_rate_group_uops(state) == ERR_NONE);
    CHECK(state->precomputed_uop_count == 32u && !state->precomputed_uops_complete);
    CHECK(check_resolution(state) == 0);
    CHECK(g_stream_telemetry.precomputed_uop_cache_hit_count == 32u);
    CHECK(g_stream_telemetry.precomputed_uop_cache_fallback_count == 480u);
    state = fixture(33u, 16u);
    state->rate_runtime[32].groups[0].first_slot_index = 8u;
    CHECK(stpg_buffer_prepare_precomputed_rate_group_uops(state) == ERR_RATEVEC_BAD_HEADER);
    return 0;
}

static int test_micro_op_differences_and_reprepare(void) {
    for (uint32_t difference = 0; difference < 7u; ++difference) {
        stpg_rate_stream_state_t *state = fixture(2u, 24u);
        state->rate_runtime[1] = state->rate_runtime[0];
        stpg_rate_runtime_t *second = &state->rate_runtime[1];
        switch (difference) {
        case 0: second->slots[0].drive_mask = 0x20000u; break;
        case 1: second->slots[0].dir_mask = 0x40000u; break;
        case 2: second->slots[0].flags = STPG_DRIVE_COMPLEMENT | STPG_RATE_SLOT_DIR_UPDATE; break;
        case 3: second->slots[0].flags |= STPG_RATE_SLOT_SAMPLE; break;
        case 4: second->groups[0].sample_mask_or = second->slots[0].sample_mask = 0x80000u; break;
        case 5: second->groups[0].time_ticks = 16u; break;
        default: second->groups[0].gap_from_prev_ticks = 8u; break;
        }
        CHECK(stpg_buffer_prepare_precomputed_rate_group_uops(state) == ERR_NONE);
        CHECK(state->precomputed_uop_count == 2u && state->precomputed_uops_complete);
        CHECK(check_resolution(state) == 0);
        state->header.rate_count = 1u;
        state->header.active_pins = 16u;
        state->rate_runtime[0].slots[0].drive_mask = 0x10001u;
        CHECK(stpg_buffer_prepare_precomputed_rate_group_uops(state) == ERR_NONE);
        CHECK(state->precomputed_uop_count == 1u && state->precomputed_uops_complete);
        CHECK(state->precomputed_uop_index[1][0] == 0xffu);
        CHECK(state->precomputed_group_uops[0].ops[0].drive_mask == 1u);
        CHECK(check_resolution(state) == 0);
    }
    stpg_rate_stream_state_t *state = fixture(2u, 16u);
    state->rate_runtime[0].rate.slot_count = 2u;
    state->rate_runtime[0].groups[0].slot_count = 2u;
    state->rate_runtime[0].slots[0].flags = STPG_DRIVE_ZERO;
    state->rate_runtime[0].slots[1] = state->rate_runtime[0].slots[0];
    state->rate_runtime[0].slots[1].flags = STPG_DRIVE_ONE;
    state->rate_runtime[1] = state->rate_runtime[0];
    state->rate_runtime[1].slots[0].flags = STPG_DRIVE_ONE;
    state->rate_runtime[1].slots[1].flags = STPG_DRIVE_ZERO;
    CHECK(stpg_buffer_prepare_precomputed_rate_group_uops(state) == ERR_NONE);
    CHECK(state->precomputed_uop_count == 2u); /* Opposite op order cannot share. */
    CHECK(check_resolution(state) == 0);
    return 0;
}

static int test_partial_repeat_samples_match_words(void) {
    struct __attribute__((packed)) {
        RateVectorHeader header; RateDesc rate; RateSlot slots[3]; VectorRecord vector;
    } pattern;
    memset(&pattern, 0, sizeof(pattern));
    pattern.header = (RateVectorHeader){ .magic=STPG_RATEVEC_MAGIC, .version=STPG_PATTERN_VERSION,
        .header_size=STPG_RATEVEC_HEADER_SIZE, .active_pins=16u, .tick_hz=STPG_TICK_HZ,
        .rate_count=1u, .slot_count=3u, .vector_count=1u, .rate_table_offset=64u,
        .slot_table_offset=72u, .vector_offset=120u };
    pattern.rate = (RateDesc){ .period_ticks=64u, .slot_count=3u };
    pattern.slots[0] = (RateSlot){ .flags=STPG_DRIVE_ONE | STPG_RATE_SLOT_DIR_UPDATE, .drive_mask=1u, .dir_mask=1u };
    pattern.slots[1] = (RateSlot){ .time_ticks=16u, .flags=STPG_DRIVE_ZERO, .drive_mask=1u };
    pattern.slots[2] = (RateSlot){ .time_ticks=32u, .flags=STPG_RATE_SLOT_SAMPLE, .sample_mask=1u };
    pattern.vector = (VectorRecord){ .repeat=1000u, .drive_enable=1u };
    pattern.header.data_crc32 = stpg_crc32((uint8_t *)&pattern + 64u, sizeof(pattern) - 64u);
    pattern.header.header_crc32 = stpg_crc32(&pattern.header, 60u);
    stpg_buffer_reset();
    const uint32_t crc = stpg_crc32(&pattern, sizeof(pattern));
    CHECK(stpg_buffer_begin(STPG_STORAGE_PSRAM, sizeof(pattern), crc, 0u) == ERR_NONE);
    CHECK(stpg_buffer_write_chunk(0u, &pattern, sizeof(pattern), crc) == ERR_NONE);
    CHECK(stpg_buffer_finalize(sizeof(pattern), crc) == ERR_NONE);
    stpg_rate_stream_state_t *state = stpg_buffer_stream_state_storage();
    CHECK(stpg_buffer_prepare_for_streaming(state, STPG_RUN_FLAG_STREAMING_POC) == ERR_NONE);
    CHECK(state->batch_path_enabled);
    uint32_t words[2048u * 4u], events = 0u, samples = 0u;
    for (;;) {
        stpg_stream_chunk_result_t chunk;
        CHECK(stpg_buffer_stream_fill_next_chunk(state, words, 2048u, &chunk) == ERR_NONE);
        uint32_t observed = 0u;
        for (uint32_t i = 0; i < chunk.event_count; ++i) observed += words[i * 4u + 1u] != 0u;
        CHECK(observed == chunk.sample_event_count);
        events += chunk.event_count; samples += observed;
        if (chunk.eof) break;
        CHECK(chunk.event_count != 0u);
    }
    CHECK(events == 3000u && samples == 1000u);
    return 0;
}

static int test_shared_uop_keeps_vector_and_held_state_dynamic(void) {
    struct __attribute__((packed)) {
        RateVectorHeader header; RateDesc rates[2]; RateSlot slots[2]; VectorRecord vectors[4];
    } pattern;
    memset(&pattern, 0, sizeof(pattern));
    pattern.header = (RateVectorHeader){ .magic=STPG_RATEVEC_MAGIC, .version=STPG_PATTERN_VERSION,
        .header_size=64u, .active_pins=16u, .tick_hz=STPG_TICK_HZ, .rate_count=2u,
        .slot_count=2u, .vector_count=4u, .rate_table_offset=64u, .slot_table_offset=80u, .vector_offset=112u };
    for (uint32_t r = 0u; r < 2u; ++r) {
        pattern.rates[r] = (RateDesc){ .period_ticks=64u, .slot_count=1u, .slot_offset=(uint16_t)r };
        pattern.slots[r] = (RateSlot){ .flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE | STPG_RATE_SLOT_SAMPLE,
            .drive_mask=3u, .dir_mask=3u, .sample_mask=3u };
    }
    for (uint32_t v = 0u; v < 4u; ++v) {
        pattern.vectors[v] = (VectorRecord){ .rate_id=(uint16_t)(v & 1u), .repeat=1u,
            .drive_value=v, .drive_enable=1u + (v & 1u), .expect_value=3u - v, .expect_mask=3u };
    }
    pattern.header.data_crc32 = stpg_crc32((uint8_t *)&pattern + 64u, sizeof(pattern) - 64u);
    pattern.header.header_crc32 = stpg_crc32(&pattern.header, 60u);
    stpg_buffer_reset();
    const uint32_t crc = stpg_crc32(&pattern, sizeof(pattern));
    CHECK(stpg_buffer_begin(STPG_STORAGE_PSRAM, sizeof(pattern), crc, 0u) == ERR_NONE);
    CHECK(stpg_buffer_write_chunk(0u, &pattern, sizeof(pattern), crc) == ERR_NONE);
    CHECK(stpg_buffer_finalize(sizeof(pattern), crc) == ERR_NONE);
    const uint32_t values[] = {0u, 0xffffu, 0x32f8u};
    const uint32_t enables[] = {0u, 0xffffu, 0x3affu};
    for (uint32_t seed = 0u; seed < 3u; ++seed) {
        uint32_t expected_words[16], expected_indices[4];
        ExecEvent expected_compare[4];
        for (uint32_t variant = 0u; variant < 3u; ++variant) {
            stpg_rate_stream_state_t *state = stpg_buffer_stream_state_storage();
            CHECK(stpg_buffer_prepare_for_streaming(state, STPG_RUN_FLAG_STREAMING_POC) == ERR_NONE);
            CHECK(state->precomputed_uop_count == 1u && state->precomputed_uops_complete);
            state->current_value = values[seed]; state->current_dir = enables[seed];
            /* Exercise per-vector comparison, independently of sample storage. */
            g_stream_compare_collecting = true;
            if (variant != 0u) {
                memset(state->precomputed_uop_index, 0xff, sizeof(state->precomputed_uop_index));
                state->precomputed_uop_count = 0u; state->precomputed_uops_complete = false;
            }
            if (variant == 2u) state->batch_path_enabled = false;
            uint32_t words[16]; stpg_stream_chunk_result_t result;
            CHECK(stpg_buffer_stream_fill_next_chunk(state, words, 4u, &result) == ERR_NONE);
            CHECK(result.event_count == 4u && result.sample_event_count == 4u);
            CHECK(stpg_buffer_stream_compare_event_count() == 4u);
            const uint32_t *indices = stpg_buffer_stream_compare_event_indices();
            const ExecEvent *compare = stpg_buffer_stream_compare_events();
            if (variant == 0u) {
                memcpy(expected_words, words, sizeof(words));
                memcpy(expected_indices, indices, sizeof(expected_indices));
                memcpy(expected_compare, compare, sizeof(expected_compare));
                for (uint32_t v = 0u; v < 4u; ++v) {
                    CHECK(compare[v].expect_value == 3u - v && compare[v].sample_mask == 3u);
                }
            } else {
                CHECK(memcmp(expected_words, words, sizeof(words)) == 0);
                CHECK(memcmp(expected_indices, indices, sizeof(expected_indices)) == 0);
                CHECK(memcmp(expected_compare, compare, sizeof(expected_compare)) == 0);
            }
        }
    }
    return 0;
}

int main(void) {
    _Static_assert(STPG_STREAM_PRECOMPUTED_UOP_CAPACITY == 32, "exercise the shipped cache capacity");
    stpg_psram_init();
    if (test_distinct_capacity_boundaries()
        || test_duplicate_after_capacity_and_full_sharing()
        || test_maximum_group_table_and_late_invalid_group()
        || test_micro_op_differences_and_reprepare()
        || test_partial_repeat_samples_match_words()
        || test_shared_uop_keeps_vector_and_held_state_dynamic()) return 1;
    puts("UOP sharing, partial retention, fallback, and sample accounting PASS");
    return 0;
}
