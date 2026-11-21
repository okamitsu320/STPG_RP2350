#include "stpg_pattern_buffer.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "stpg_board.h"
#include "stpg_common.h"
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

static uint32_t build_r52_pattern(uint8_t *buffer, uint32_t capacity) {
    const uint32_t rate_offset = STPG_RATEVEC_HEADER_SIZE;
    const uint32_t slot_offset = rate_offset + STPG_RATE_DESC32_SIZE;
    const uint32_t vector_offset = slot_offset + (8u * STPG_RATE_SLOT32_SIZE);
    const uint32_t total_size = vector_offset + (8u * STPG_VECTOR_RECORD_SIZE);
    if (buffer == NULL || capacity < total_size) return 0u;
    memset(buffer, 0, total_size);

    RateVectorHeader *header = (RateVectorHeader *)(void *)buffer;
    RateDesc32 *rate = (RateDesc32 *)(void *)(buffer + rate_offset);
    RateSlot32 *slots = (RateSlot32 *)(void *)(buffer + slot_offset);
    VectorRecord *vectors = (VectorRecord *)(void *)(buffer + vector_offset);

    header->magic = STPG_RATEVEC_MAGIC;
    header->version = STPG_PATTERN_VERSION_WIDE_WAIT;
    header->header_size = STPG_RATEVEC_HEADER_SIZE;
    header->active_pins = STPG_ACTIVE_PINS_V11;
    header->tick_hz = STPG_TICK_HZ;
    header->rate_count = 1u;
    header->slot_count = 8u;
    header->vector_count = 8u;
    header->rate_table_offset = rate_offset;
    header->slot_table_offset = slot_offset;
    header->vector_offset = vector_offset;

    rate->period_ticks = 480000u;
    rate->slot_count = 8u;
    rate->slot_offset = 0u;

    for (uint32_t index = 0u; index < 8u; ++index) {
        slots[index].time_ticks = index * 60000u;
        slots[index].flags = index == 0u
            ? (STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE)
            : 0u;
        slots[index].drive_mask = index == 0u ? 0x00FFFFFFu : 0u;
        slots[index].dir_mask = index == 0u ? 0x00FFFFFFu : 0u;
    }

    for (uint32_t index = 0u; index < 8u; ++index) {
        vectors[index].rate_id = 0u;
        vectors[index].repeat = 2u;
        vectors[index].drive_value = (index & 1u) != 0u ? 0x00FFFFFFu : 0u;
        vectors[index].drive_enable = 0x00FFFFFFu;
    }

    header->data_crc32 = stpg_crc32(buffer + rate_offset, total_size - rate_offset);
    header->header_crc32 = stpg_crc32(header, STPG_RATEVEC_HEADER_SIZE - sizeof(header->header_crc32));
    return total_size;
}

static int load_psram(const uint8_t *pattern, uint32_t size, uint32_t total_crc) {
    if (expect(stpg_buffer_begin(STPG_STORAGE_PSRAM, size, total_crc, 0u), ERR_NONE, "begin")) return 1;
    if (expect(stpg_buffer_write_chunk(0u, pattern, size, total_crc), ERR_NONE, "write")) return 1;
    if (expect(stpg_buffer_finalize(size, total_crc), ERR_NONE, "finalize")) return 1;
    return 0;
}

int main(void) {
    uint8_t pattern[396u];
    const uint32_t size = build_r52_pattern(pattern, sizeof(pattern));
    if (size != sizeof(pattern)) return 1;
    const uint32_t total_crc = stpg_crc32(pattern, size);

    stpg_buffer_reset();
    if (load_psram(pattern, size, total_crc)) return 1;

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC),
               ERR_NONE, "prepare_for_streaming")) return 1;

    uint32_t words[STPG_STREAM_BUFFER_EVENTS * 4u];
    stpg_stream_chunk_result_t chunk;
    if (expect(stpg_buffer_stream_fill_next_chunk(&state, words, STPG_STREAM_BUFFER_EVENTS, &chunk),
               ERR_NONE, "stream_fill")) return 1;
    if (chunk.event_count != 128u || chunk.word_count != 512u || !chunk.eof) {
        fprintf(stderr, "R52 stream shape: events=%u words=%u eof=%u\n",
                (unsigned)chunk.event_count, (unsigned)chunk.word_count, chunk.eof ? 1u : 0u);
        return 1;
    }

    /* The direction word is the physical contract: GPIO2 (logical bit 0)
     * must be an output before the first value transition is emitted. */
    for (uint32_t vector = 0u; vector < 8u; ++vector) {
        const uint32_t first_event = vector * 16u;
        const uint32_t expected_value = (vector & 1u) != 0u ? 1u : 0u;
        if ((words[first_event * 4u + 3u] & 1u) != 1u) {
            fprintf(stderr, "R52 GPIO2 direction missing at vector %u: dir=0x%08x\n",
                    (unsigned)vector, (unsigned)words[first_event * 4u + 3u]);
            return 1;
        }
        if ((words[first_event * 4u + 2u] & 1u) != expected_value) {
            fprintf(stderr, "R52 GPIO2 vector %u: value=0x%08x expected=%u\n",
                    (unsigned)vector, (unsigned)words[first_event * 4u + 2u],
                    (unsigned)expected_value);
            return 1;
        }
        for (uint32_t event = 1u; event < 16u; ++event) {
            if ((words[(first_event + event) * 4u + 2u] & 1u) != expected_value) {
                fprintf(stderr, "R52 GPIO2 vector %u changed inside slot group\n", (unsigned)vector);
                return 1;
            }
        }
    }
    return 0;
}
