/* Non-contact regression for the R388 prepared producer-route defect. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../src/pattern_buffer.c"

#define CHECK(c) do { if (!(c)) { fprintf(stderr, "%s:%d: %s\n", __func__, __LINE__, #c); return 1; } } while (0)

static int test_route(uint32_t pins) {
    /* The problematic shape: separate drive and sample-only groups. */
    uint8_t bytes[64u + sizeof(RateDesc32) + 2u * sizeof(RateSlot32) + 4u * sizeof(VectorRecord)] = {0};
    RateVectorHeader *h = (RateVectorHeader *)(void *)bytes;
    *h = (RateVectorHeader){ .magic=STPG_RATEVEC_MAGIC, .version=STPG_PATTERN_VERSION_WIDE_WAIT,
        .header_size=64u, .active_pins=pins, .tick_hz=STPG_TICK_HZ,
        .rate_count=1u, .slot_count=2u, .vector_count=4u, .rate_table_offset=64u,
        .slot_table_offset=64u + sizeof(RateDesc32),
        .vector_offset=64u + sizeof(RateDesc32) + 2u * sizeof(RateSlot32) };
    *(RateDesc32 *)(void *)(bytes + h->rate_table_offset) =
        (RateDesc32){ .period_ticks=40u, .slot_count=2u, .slot_offset=0u };
    RateSlot32 *slots=(RateSlot32 *)(void *)(bytes + h->slot_table_offset);
    slots[0]=(RateSlot32){ .flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, .drive_mask=1u, .dir_mask=1u };
    slots[1]=(RateSlot32){ .time_ticks=16u, .flags=STPG_RATE_SLOT_SAMPLE, .sample_mask=1u };
    VectorRecord *v=(VectorRecord *)(void *)(bytes + h->vector_offset);
    for (uint32_t i=0u; i<4u; ++i)
        v[i]=(VectorRecord){ .repeat=1u, .drive_value=i % 2u, .drive_enable=1u, .expect_mask=1u };
    h->data_crc32=stpg_crc32(bytes + 64u, sizeof(bytes) - 64u);
    h->header_crc32=stpg_crc32(bytes, 60u);
    stpg_buffer_reset();
    const uint32_t crc=stpg_crc32(bytes, sizeof(bytes));
    CHECK(stpg_buffer_begin(STPG_STORAGE_PSRAM, sizeof(bytes), crc, 0u) == ERR_NONE);
    CHECK(stpg_buffer_write_chunk(0u, bytes, sizeof(bytes), crc) == ERR_NONE);
    CHECK(stpg_buffer_finalize(sizeof(bytes), crc) == ERR_NONE);
    stpg_rate_stream_state_t *s=stpg_buffer_stream_state_storage();
    CHECK(stpg_buffer_prepare_for_streaming(s, STPG_RUN_FLAG_STREAMING_POC) == ERR_NONE);
    CHECK(s->reduced_density_compare_enabled == (bool)EXPECT_REDUCED);
    if (!EXPECT_REDUCED) {
        uint32_t words[8u * 4u];
        stpg_stream_chunk_result_t result;
        CHECK(stpg_buffer_stream_fill_next_chunk(s, words, 8u, &result) == ERR_NONE);
        CHECK(result.word_count == 32u && result.event_count == 8u && result.sample_event_count == 4u && result.eof);
        for (uint32_t i=0u; i<8u; ++i) CHECK((words[i * 4u + 1u] != 0u) == (i % 2u != 0u));
    }
    return 0;
}

int main(void) {
    stpg_psram_init();
    if (test_route(16u) || test_route(24u)) return 1;
    printf("prepared runtime route: reduced=%u, 16/24 pin PASS\n", (unsigned)EXPECT_REDUCED);
    return 0;
}
