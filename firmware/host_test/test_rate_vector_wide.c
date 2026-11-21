#include "stpg_pattern_buffer.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "stpg_common.h"
#include "stpg_board.h"
#include "stpg_crc32.h"
#include "stpg_pattern.h"
#include "stpg_protocol.h"

static int expect(stpg_err_t actual, stpg_err_t wanted, const char *label) {
    if (actual != wanted) {
        fprintf(stderr, "%s: actual=%u wanted=%u\n", label, (unsigned)actual, (unsigned)wanted);
        return 1;
    }
    return 0;
}

static uint32_t build_wide_pattern(uint8_t *buffer, uint32_t capacity) {
    const uint32_t rate_offset = STPG_RATEVEC_HEADER_SIZE;
    const uint32_t slot_offset = rate_offset + STPG_RATE_DESC32_SIZE;
    const uint32_t vector_offset = slot_offset + (2u * STPG_RATE_SLOT32_SIZE);
    const uint32_t total_size = vector_offset + STPG_VECTOR_RECORD_SIZE;
    if (buffer == NULL || capacity < total_size) return 0u;
    memset(buffer, 0, total_size);

    RateVectorHeader *header = (RateVectorHeader *)(void *)buffer;
    RateDesc32 *rate = (RateDesc32 *)(void *)(buffer + rate_offset);
    RateSlot32 *slots = (RateSlot32 *)(void *)(buffer + slot_offset);
    VectorRecord *vector = (VectorRecord *)(void *)(buffer + vector_offset);

    header->magic = STPG_RATEVEC_MAGIC;
    header->version = STPG_PATTERN_VERSION_WIDE_WAIT;
    header->header_size = STPG_RATEVEC_HEADER_SIZE;
    header->active_pins = STPG_ACTIVE_PINS_V01;
    header->tick_hz = STPG_TICK_HZ;
    header->rate_count = 1u;
    header->slot_count = 2u;
    header->vector_count = 1u;
    header->rate_table_offset = rate_offset;
    header->slot_table_offset = slot_offset;
    header->vector_offset = vector_offset;

    rate->period_ticks = 480000u;
    rate->slot_count = 2u;
    rate->slot_offset = 0u;

    slots[0].time_ticks = 0u;
    slots[0].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    slots[0].drive_mask = 0x0003u;
    slots[0].dir_mask = 0x0003u;
    slots[1].time_ticks = 240000u;
    slots[1].flags = STPG_DRIVE_ZERO | STPG_RATE_SLOT_SAMPLE;
    slots[1].drive_mask = 0x0003u;
    slots[1].sample_mask = 0x0003u;

    vector->rate_id = 0u;
    vector->repeat = 2u;
    vector->drive_value = 0x0003u;
    vector->drive_enable = 0x0003u;
    vector->expect_mask = 0x0003u;

    header->data_crc32 = stpg_crc32(buffer + rate_offset, total_size - rate_offset);
    header->header_crc32 = stpg_crc32(header, STPG_RATEVEC_HEADER_SIZE - sizeof(header->header_crc32));
    return total_size;
}

int main(void) {
    if ((STPG_CAPABILITY_BASE_V06A & CAP_WIDE_RATE_VECTOR_32) == 0u) return 1;
    uint8_t pattern[STPG_RATEVEC_HEADER_SIZE + STPG_RATE_DESC32_SIZE
                    + (2u * STPG_RATE_SLOT32_SIZE) + STPG_VECTOR_RECORD_SIZE];
    const uint32_t size = build_wide_pattern(pattern, sizeof(pattern));
    if (size == 0u) return 1;
    const uint32_t total_crc = stpg_crc32(pattern, size);

    stpg_buffer_reset();
    if (expect(stpg_buffer_begin(STPG_STORAGE_PSRAM, size, total_crc, 0u), ERR_NONE, "begin")) return 1;
    if (expect(stpg_buffer_write_chunk(0u, pattern, size, total_crc), ERR_NONE, "write")) return 1;
    if (expect(stpg_buffer_finalize(size, total_crc), ERR_NONE, "finalize")) return 1;
    if (stpg_buffer_active_source() != STPG_PATTERN_SOURCE_PSRAM) return 1;

    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE, "prepare_for_run")) return 1;
    if (stpg_buffer_event_count() != 4u || stpg_buffer_sample_event_count() != 2u) return 1;
    if (stpg_buffer_pio_word_count() != 16u) return 1;

    const uint32_t *words = stpg_buffer_pio_words();
    /* These values must stay wide; a legacy uint16_t path would wrap below 65535. */
    if (words[4] < 200000u || words[8] < 200000u || words[12] < 200000u) return 1;
    if (words[5] != 1u || words[13] != 1u) return 1;

    /* Exercise the PSRAM ring-loader, not only the full materializer. */
    stpg_buffer_reset();
    if (expect(stpg_buffer_begin(STPG_STORAGE_PSRAM, size, total_crc, 0u), ERR_NONE, "stream begin")) return 1;
    if (expect(stpg_buffer_write_chunk(0u, pattern, size, total_crc), ERR_NONE, "stream write")) return 1;
    if (expect(stpg_buffer_finalize(size, total_crc), ERR_NONE, "stream finalize")) return 1;
    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE, "stream prepare")) return 1;
    uint32_t chunk_words[16];
    stpg_stream_chunk_result_t chunk;
    if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 4u, &chunk), ERR_NONE, "stream fill")) return 1;
    if (chunk.event_count != 4u || chunk_words[0] != 0u || chunk_words[4] < 200000u
        || chunk_words[8] < 200000u || chunk_words[12] < 200000u) {
        fprintf(stderr, "stream values: events=%u words=%u,%u,%u,%u\n",
                (unsigned)chunk.event_count, (unsigned)chunk_words[0], (unsigned)chunk_words[4],
                (unsigned)chunk_words[8], (unsigned)chunk_words[12]);
        return 1;
    }
    return 0;
}
