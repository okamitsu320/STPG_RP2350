/* Private emitter A/B on synthetic, non-contact fixtures only. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "../src/pattern_buffer.c"

#define CHECK(c) do { if (!(c)) { fprintf(stderr, "%s:%d: %s\n", __func__, __LINE__, #c); return 1; } } while (0)

static uint32_t rng = 1u;
static uint32_t next_random(void) { rng = rng * 1664525u + 1013904223u; return rng; }

static uint8_t *pattern(uint32_t vectors, bool huge_repeat, uint32_t pins,
                        bool no_repeat, uint32_t *out_size) {
    const uint32_t slots = 15u;
    const bool wide = pins == 24u;
    const uint32_t rate_size = wide ? sizeof(RateDesc32) : sizeof(RateDesc);
    const uint32_t slot_size = wide ? sizeof(RateSlot32) : sizeof(RateSlot);
    const uint32_t mask = wide ? 0x00ffffffu : 0xffffu;
    *out_size = 64u + 4u * rate_size + slots * slot_size + vectors * sizeof(VectorRecord);
    uint8_t *bytes = calloc(1, *out_size);
    if (!bytes) return NULL;
    RateVectorHeader *h = (RateVectorHeader *)(void *)bytes;
    *h = (RateVectorHeader){ .magic=STPG_RATEVEC_MAGIC,
        .version=wide ? STPG_PATTERN_VERSION_WIDE_WAIT : STPG_PATTERN_VERSION,
        .header_size=64u, .active_pins=pins, .tick_hz=STPG_TICK_HZ, .rate_count=4u,
        .slot_count=slots, .vector_count=vectors, .rate_table_offset=64u,
        .slot_table_offset=64u + 4u * rate_size,
        .vector_offset=64u + 4u * rate_size + slots * slot_size };
    uint32_t offset = 0u;
    const uint32_t counts[] = {1u, 2u, 4u, 8u};
    for (uint32_t r = 0u; r < 4u; ++r) {
        uint32_t period = 64u * (counts[r] + 2u);
        if (wide && r == 3u) period += 800000u; /* 4ms wait; remains a 4-word event. */
        if (wide) ((RateDesc32 *)(void *)(bytes + h->rate_table_offset))[r] =
            (RateDesc32){ .period_ticks=period, .slot_count=counts[r], .slot_offset=offset };
        else ((RateDesc *)(void *)(bytes + h->rate_table_offset))[r] =
            (RateDesc){ .period_ticks=(uint16_t)period, .slot_count=counts[r], .slot_offset=offset };
        for (uint32_t g = 0u; g < counts[r]; ++g) {
            uint32_t group = r == 2u && g > 0u ? g - 1u : g;
            uint32_t time = group * 32u + (r == 1u ? 16u : 0u);
            uint16_t flags = (uint16_t)(1u + g % 4u) | STPG_RATE_SLOT_DIR_UPDATE
                | (g % 2u ? STPG_RATE_SLOT_SAMPLE : 0u);
            uint32_t drive = (wide ? 0xa5a55au : 0xa55au) >> (g % 3u);
            uint32_t dir = mask >> (g % 4u), sample = g % 2u ? mask : 0u;
            if (wide) ((RateSlot32 *)(void *)(bytes + h->slot_table_offset))[offset + g] =
                (RateSlot32){ .time_ticks=time, .flags=flags, .drive_mask=drive,
                    .dir_mask=dir, .sample_mask=sample };
            else ((RateSlot *)(void *)(bytes + h->slot_table_offset))[offset + g] =
                (RateSlot){ .time_ticks=(uint16_t)time, .flags=flags, .drive_mask=drive,
                    .dir_mask=dir, .sample_mask=sample };
        }
        offset += counts[r];
    }
    VectorRecord *v = (VectorRecord *)(void *)(bytes + h->vector_offset);
    for (uint32_t i = 0u; i < vectors; ++i) {
        v[i] = (VectorRecord){ .rate_id=i % 4u,
            .repeat=(uint16_t)(no_repeat ? 1u : 1u + next_random() % 9u),
            .drive_value=next_random() & mask, .drive_enable=next_random() & mask,
            .expect_value=next_random() & mask, .expect_mask=mask };
    }
    if (huge_repeat) v[0].repeat = 65535u;
    h->data_crc32 = stpg_crc32(bytes + 64u, *out_size - 64u);
    h->header_crc32 = stpg_crc32(bytes, 60u);
    return bytes;
}

static int upload(const uint8_t *bytes, uint32_t size) {
    stpg_buffer_reset();
    uint32_t crc = stpg_crc32(bytes, size);
    CHECK(stpg_buffer_begin(STPG_STORAGE_PSRAM, size, crc, 0u) == ERR_NONE);
    for (uint32_t off = 0u; off < size;) {
        uint32_t n = size - off < 4096u ? size - off : 4096u;
        CHECK(stpg_buffer_write_chunk(off, bytes + off, n, stpg_crc32(bytes + off, n)) == ERR_NONE);
        off += n;
    }
    CHECK(stpg_buffer_finalize(size, crc) == ERR_NONE);
    return 0;
}

typedef struct {
    uint32_t vector, repeat, group, value, dir, prev_time, prev_period, emitted;
    bool previous, eof;
    stpg_stream_chunk_result_t chunk;
} checkpoint_t;

static checkpoint_t checkpoint(const stpg_rate_stream_state_t *s, stpg_stream_chunk_result_t result) {
    checkpoint_t c = {0};
    c.vector=s->vector_index; c.repeat=s->repeat_index; c.group=s->current_group_index;
    c.value=s->current_value; c.dir=s->current_dir; c.prev_time=s->prev_time_ticks;
    c.prev_period=s->prev_period_ticks; c.emitted=g_stream_emitted_event_count;
    c.previous=s->have_prev_time; c.eof=s->eof; c.chunk=result;
    return c;
}

static int test_ab(uint32_t capacity, bool huge_repeat, uint32_t pins, bool no_repeat) {
    uint32_t size;
    uint8_t *bytes = pattern(1025u, huge_repeat, pins, no_repeat, &size);
    CHECK(bytes != NULL);
    const uint32_t limit = huge_repeat ? 200000u : 100000u;
    uint32_t *expected = malloc((size_t)limit * 4u * sizeof(uint32_t));
    checkpoint_t *checkpoints = calloc(limit + 1u, sizeof(checkpoint_t));
    uint32_t *words = malloc((size_t)capacity * 4u * sizeof(uint32_t));
    CHECK(expected && checkpoints && words);
    uint32_t expected_events = 0u, expected_chunks = 0u;
    for (uint32_t pass = 0u; pass < 3u; ++pass) {
        CHECK(upload(bytes, size) == 0);
        stpg_rate_stream_state_t *s = stpg_buffer_stream_state_storage();
        CHECK(stpg_buffer_prepare_for_streaming(s, STPG_RUN_FLAG_STREAMING_POC) == ERR_NONE);
        /* A segmented continuation may enter with nonzero held outputs. */
        s->current_value = pins == 24u ? 0xfaa55au : 0xa55au;
        s->current_dir = pins == 24u ? 0xe66ff0u : 0x6ff0u;
        CHECK(s->batch_path_enabled && stpg_buffer_rate_template_fill_eligible(s));
        uint32_t events = 0u, chunks = 0u;
        do {
            stpg_stream_chunk_result_t result;
            /* A partial-vector chunk must also hand logical held state back
             * correctly when the next chunk uses a legacy fallback. */
            if (pass == 2u) s->precomputed_uops_complete = chunks % 3u != 1u;
            stpg_err_t err = pass == 0u
                ? stpg_buffer_stream_fill_next_chunk_batch_legacy(s, words, capacity, &result)
                : stpg_buffer_stream_fill_next_chunk_batch(s, words, capacity, &result);
            CHECK(err == ERR_NONE);
            CHECK(result.event_count > 0u && events + result.event_count <= limit);
            if (pins == 24u) {
                for (uint32_t i = 0u; i < result.event_count; ++i) {
                    CHECK(((words[i * 4u + 2u] | words[i * 4u + 3u]) & STPG_PACKED27_GAP_MASK) == 0u);
                }
                CHECK(((s->current_value | s->current_dir) & ~0x00ffffffu) == 0u);
            }
            checkpoint_t c = checkpoint(s, result);
            if (pass == 0u) {
                memcpy(expected + events * 4u, words, result.word_count * sizeof(uint32_t));
                checkpoints[chunks] = c;
            } else {
                CHECK(chunks < expected_chunks);
                CHECK(memcmp(&c, &checkpoints[chunks], sizeof(c)) == 0);
                CHECK(memcmp(expected + events * 4u, words, result.word_count * sizeof(uint32_t)) == 0);
            }
            events += result.event_count;
            ++chunks;
        } while (!s->eof);
        if (pass == 0u) { expected_events=events; expected_chunks=chunks; }
        else CHECK(events == expected_events && chunks == expected_chunks);
    }
    free(words); free(checkpoints); free(expected); free(bytes);
    return 0;
}

static int test_masks_and_exclusions(uint32_t pins) {
    stpg_rate_runtime_t r = {0};
    r.rate.slot_count=8u; r.rate.period_ticks=128u; r.group_count=1u;
    r.groups[0].slot_count=8u; r.groups[0].gap_from_cycle_start_ticks=128u;
    for (uint32_t trial = 0u; trial < 1000u; ++trial) {
        for (uint32_t i = 0u; i < 8u; ++i) {
            r.slots[i].flags=(uint16_t)(next_random() % 5u);
            r.slots[i].drive_mask=next_random() & (pins == 24u ? 0x00ffffffu : 0xffffu);
        }
        stpg_rate_group_uop_t u;
        CHECK(stpg_buffer_build_rate_group_uop(&r, 0u, pins, &u) == ERR_NONE);
        VectorRecord v = { .drive_value=next_random() & (pins == 24u ? 0x00ffffffu : 0xffffu) };
        uint32_t initial=next_random() & (pins == 24u ? 0x00ffffffu : 0xffffu), expected=initial;
        for (uint32_t i = 0u; i < u.op_count; ++i) expected=stpg_apply_group_uop_drive(expected, &u.ops[i], &v);
        uint32_t packed_initial=stpg_stream_pack_logical_to_window_fast(initial, pins);
        uint32_t packed_vector=stpg_stream_pack_logical_to_window_fast(v.drive_value, pins);
        CHECK(stpg_stream_pack_logical_to_window_fast(expected, pins) ==
            ((packed_initial & u.drive_keep_mask) | (packed_vector & u.drive_vector_mask)
             | (~packed_vector & u.drive_complement_mask) | u.drive_one_mask));
    }
    uint32_t size;
    uint8_t *bytes = pattern(8u, false, pins, false, &size);
    CHECK(bytes && upload(bytes, size) == 0);
    free(bytes);
    stpg_rate_stream_state_t *s = stpg_buffer_stream_state_storage();
    CHECK(stpg_buffer_prepare_for_streaming(s, STPG_RUN_FLAG_STREAMING_POC) == ERR_NONE);
    CHECK(stpg_buffer_rate_template_fill_eligible(s));
    uint32_t outside = 1u << pins;
    s->current_value=outside; CHECK(!stpg_buffer_rate_template_fill_eligible(s)); s->current_value=0u;
    s->current_dir=outside; CHECK(!stpg_buffer_rate_template_fill_eligible(s)); s->current_dir=0u;
    s->header.active_pins=17u; CHECK(!stpg_buffer_rate_template_fill_eligible(s)); s->header.active_pins=pins;
    s->loop_block_count=1u; CHECK(!stpg_buffer_rate_template_fill_eligible(s)); s->loop_block_count=0u;
    s->precomputed_uops_complete=false; CHECK(!stpg_buffer_rate_template_fill_eligible(s)); s->precomputed_uops_complete=true;
    s->precomputed_group_uops[0].stable_wait_valid=false; CHECK(!stpg_buffer_rate_template_fill_eligible(s));
    s->precomputed_group_uops[0].stable_wait_valid=true;
    g_stream_compare_collecting=true; CHECK(!stpg_buffer_rate_template_fill_eligible(s)); g_stream_compare_collecting=false;
    return 0;
}

static int test_invalid_cross_vector_wait_keeps_failure_boundary(uint32_t pins) {
    uint32_t size;
    uint8_t *bytes = pattern(2u, false, pins, false, &size);
    CHECK(bytes != NULL);
    RateVectorHeader *h = (RateVectorHeader *)(void *)bytes;
    VectorRecord *v = (VectorRecord *)(void *)(bytes + h->vector_offset);
    /* last=48, first=16: valid cycle=18, next rate first=0: delta=2 */
    if (pins == 24u) ((RateDesc32 *)(void *)(bytes + h->rate_table_offset))[1].period_ticks=50u;
    else ((RateDesc *)(void *)(bytes + h->rate_table_offset))[1].period_ticks=50u;
    v[0].rate_id=1u; v[0].repeat=1u; v[1].rate_id=0u; v[1].repeat=1u;
    h->data_crc32=stpg_crc32(bytes + 64u, size - 64u); h->header_crc32=stpg_crc32(bytes, 60u);
    uint32_t expected[12], words[12]; checkpoint_t original={0};
    stpg_stream_buffer_telemetry_t diagnostic={0};
    for (uint32_t pass=0u; pass<2u; ++pass) {
        CHECK(upload(bytes, size) == 0);
        stpg_rate_stream_state_t *s=stpg_buffer_stream_state_storage();
        CHECK(stpg_buffer_prepare_for_streaming(s, STPG_RUN_FLAG_STREAMING_POC) == ERR_NONE);
        s->current_value=pins == 24u ? 0xda55aau : 0x55aau;
        s->current_dir=pins == 24u ? 0xe65aa5u : 0x5aa5u;
        CHECK(stpg_buffer_rate_template_fill_eligible(s));
        memset(words, 0xa5, sizeof(words));
        stpg_stream_chunk_result_t result;
        stpg_err_t err=pass == 0u ? stpg_buffer_stream_fill_next_chunk_batch_legacy(s, words, 3u, &result)
            : stpg_buffer_stream_fill_next_chunk_batch(s, words, 3u, &result);
        CHECK(err == ERR_RATE_PERIOD_MISMATCH && result.event_count == 0u);
        CHECK(s->vector_index == 1u && g_stream_telemetry.mismatch_desired_delta_ticks == 2u);
        checkpoint_t now=checkpoint(s, result);
        if (pass == 0u) { original=now; memcpy(expected, words, sizeof(words)); diagnostic=g_stream_telemetry; }
        else {
            CHECK(memcmp(&now, &original, sizeof(now)) == 0 && memcmp(words, expected, sizeof(words)) == 0);
            CHECK(g_stream_telemetry.mismatch_stage == diagnostic.mismatch_stage);
            CHECK(g_stream_telemetry.mismatch_required_ticks == diagnostic.mismatch_required_ticks);
        }
    }
    free(bytes);
    return 0;
}

static int test_pack_roundtrip(void) {
    for (uint32_t pins=16u; pins<=24u; pins+=8u) {
        for (uint32_t bit=0u; bit<pins; ++bit) {
            uint32_t logical=1u << bit;
            uint32_t packed=stpg_stream_pack_logical_to_window_fast(logical, pins);
            CHECK(packed == 1u << (stpg_board_gpio_for_dut_pin(bit) - STPG_DUT_WINDOW_BASE));
            CHECK(stpg_stream_unpack_window_to_logical_fast(packed, pins) == logical);
        }
        for (uint32_t i=0u; i<1000u; ++i) {
            uint32_t logical=next_random() & (pins == 24u ? 0x00ffffffu : 0xffffu);
            uint32_t packed=stpg_stream_pack_logical_to_window_fast(logical, pins);
            CHECK(packed == stpg_board_pack_logical_to_window(logical, pins));
            CHECK(stpg_stream_unpack_window_to_logical_fast(packed, pins) == logical);
            if (pins == 24u) CHECK(stpg_stream_unpack_window_to_logical_fast(packed | STPG_PACKED27_GAP_MASK, pins) == logical);
        }
    }
    return 0;
}

static int test_prefetch_error_keeps_logical_held_state(uint32_t pins) {
    uint32_t size;
    uint8_t *bytes=pattern(600u, false, pins, true, &size);
    CHECK(bytes != NULL);
    RateVectorHeader *h=(RateVectorHeader *)(void *)bytes;
    const uint32_t damaged_offset=h->vector_offset + 257u * sizeof(VectorRecord);
    uint8_t invalid_rate=4u;
    uint32_t expected[2048u * 4u], words[2048u * 4u];
    checkpoint_t original={0};
    for (uint32_t pass=0u; pass<2u; ++pass) {
        CHECK(upload(bytes, size) == 0);
        stpg_rate_stream_state_t *s=stpg_buffer_stream_state_storage();
        CHECK(stpg_buffer_prepare_for_streaming(s, STPG_RUN_FLAG_STREAMING_POC) == ERR_NONE);
        s->current_value=pins == 24u ? 0xda55aau : 0x55aau;
        s->current_dir=pins == 24u ? 0xe65aa5u : 0x5aa5u;
        CHECK(stpg_psram_write(STPG_PSRAM_PATTERN_BASE + damaged_offset, &invalid_rate, 1u) == ERR_NONE);
        memset(words, 0xa5, sizeof(words));
        stpg_stream_chunk_result_t result;
        stpg_err_t err=pass == 0u
            ? stpg_buffer_stream_fill_next_chunk_batch_legacy(s, words, 2048u, &result)
            : stpg_buffer_stream_fill_next_chunk_batch(s, words, 2048u, &result);
        CHECK(err == ERR_VECTOR_BAD_RATE_ID && result.event_count == 0u);
        if (s->vector_index == 0u || s->vector_index > 257u) {
            fprintf(stderr, "prefetch failure pins=%u pass=%u vector=%u loaded=%u cache=%u/%u\n",
                    pins, pass, s->vector_index, s->vector_loaded,
                    g_stream_vector_cache_base_index, g_stream_vector_cache_count);
            free(bytes); return 1;
        }
        checkpoint_t now=checkpoint(s, result);
        if (pass == 0u) { original=now; memcpy(expected, words, sizeof(words)); }
        else {
            CHECK(memcmp(&now, &original, sizeof(now)) == 0);
            CHECK(memcmp(words, expected, sizeof(words)) == 0);
        }
    }
    free(bytes);
    return 0;
}

static int benchmark(void) {
    for (uint32_t pins=16u; pins<=24u; pins+=8u) {
    for (uint32_t shape=0u; shape<3u; ++shape) {
        uint32_t size;
        uint8_t *bytes=pattern(4097u, shape == 2u, pins, shape == 0u, &size);
        CHECK(bytes != NULL && upload(bytes, size) == 0);
        double times[2]={0}; uint32_t crc[2]={0}, events[2]={0};
        uint32_t words[2048u * 4u];
        for (uint32_t pass=0u; pass<2u; ++pass) {
            for (uint32_t trial=0u; trial<7u; ++trial) {
                stpg_rate_stream_state_t *s=stpg_buffer_stream_state_storage();
                CHECK(stpg_buffer_prepare_for_streaming(s, STPG_RUN_FLAG_STREAMING_POC) == ERR_NONE);
                CHECK(stpg_buffer_rate_template_fill_eligible(s));
                uint32_t checksum=0u, count=0u;
                do {
                    stpg_stream_chunk_result_t result;
                    clock_t start=clock();
                    stpg_err_t err=pass == 0u ? stpg_buffer_stream_fill_next_chunk_batch_legacy(s, words, 2048u, &result)
                        : stpg_buffer_stream_fill_next_chunk_batch(s, words, 2048u, &result);
                    times[pass] += (double)(clock() - start) / CLOCKS_PER_SEC;
                    CHECK(err == ERR_NONE && result.event_count > 0u);
                    checksum=stpg_crc32_continue(checksum, words, result.word_count * sizeof(uint32_t));
                    count += result.event_count;
                } while (!s->eof);
                crc[pass]=checksum; events[pass]=count;
            }
        }
        CHECK(crc[0] == crc[1] && events[0] == events[1]);
        printf("{\"pins\":%u,\"shape\":\"%s\",\"events_per_trial\":%u,\"trials\":7,\"legacy_cpu_ms\":%.6f,"
               "\"template_cpu_ms\":%.6f,\"ratio\":%.6f,\"pio_crc32\":%u,\"physical_io\":false}\n",
               pins, shape == 0u ? "random-repeat1" : shape == 1u ? "random-repeat1..9" : "random-repeat65535+1..9",
               events[0], times[0]*1000/7,
               times[1]*1000/7, times[1]/times[0], crc[0]);
        free(bytes);
    }
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--benchmark") == 0) return benchmark();
    if (argc != 1) return 2;
    const uint32_t capacities[] = {1u, 2u, 3u, 7u, 8u, 31u, 2047u, 2048u, 2049u};
    for (uint32_t pins=16u; pins<=24u; pins+=8u) {
        for (uint32_t i = 0u; i < sizeof(capacities)/sizeof(capacities[0]); ++i) {
            if (test_ab(capacities[i], false, pins, false)
                || test_ab(capacities[i], false, pins, true)) return 1;
        }
        if (test_ab(2048u, true, pins, false) || test_masks_and_exclusions(pins)
            || test_invalid_cross_vector_wait_keeps_failure_boundary(pins)
            || test_prefetch_error_keeps_logical_held_state(pins)) return 1;
    }
    if (test_pack_roundtrip()) return 1;
    puts("rate templates: 16/24-pin ordered masks, GPIO holes, logical chunk/error states, fallback transitions PASS");
    return 0;
}
