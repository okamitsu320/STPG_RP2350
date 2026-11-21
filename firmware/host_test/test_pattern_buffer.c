#include "stpg_pattern_buffer.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "stpg_board.h"
#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_pattern.h"

static void build_two_event_pattern(uint8_t *buf, uint32_t *total_crc) {
    PatternHeader *h = (PatternHeader *)(void *)buf;
    ExecEvent *events = (ExecEvent *)(void *)(buf + STPG_HEADER_SIZE);

    memset(buf, 0, STPG_HEADER_SIZE + STPG_EXEC_EVENT_SIZE * 2u);
    events[0].wait_ticks = 5;
    events[0].out_value = 0x00000003u;
    events[0].out_enable = 0x0000000Fu;
    events[1].wait_ticks = 10;
    events[1].out_value = 0x000000AAu;
    events[1].out_enable = 0x00000055u;

    h->magic = STPG_PATTERN_MAGIC;
    h->version = STPG_PATTERN_VERSION;
    h->header_size = STPG_HEADER_SIZE;
    h->active_pins = STPG_ACTIVE_PINS_V01;
    h->tick_hz = STPG_TICK_HZ;
    h->event_size = STPG_EXEC_EVENT_SIZE;
    h->event_count = 2;
    h->data_size = STPG_EXEC_EVENT_SIZE * 2u;
    h->data_crc32 = stpg_crc32(events, h->data_size);
    h->header_crc32 = stpg_crc32(buf, STPG_HEADER_SIZE - sizeof(h->header_crc32));
    *total_crc = stpg_crc32(buf, STPG_HEADER_SIZE + h->data_size);
}

static int expect(stpg_err_t actual, stpg_err_t expected) {
    if (actual != expected) {
        fprintf(stderr, "expect failed: actual=%u expected=%u\n", (unsigned)actual, (unsigned)expected);
    }
    return actual == expected ? 0 : 1;
}

int main(void) {
    uint8_t pattern[STPG_HEADER_SIZE + STPG_EXEC_EVENT_SIZE * 2u];
    uint32_t total_crc = 0;
    build_two_event_pattern(pattern, &total_crc);
    const uint32_t total_size = sizeof(pattern);

    stpg_buffer_reset();
    if (expect(stpg_buffer_begin(STPG_STORAGE_PSRAM, total_size, total_crc, 0), ERR_NONE)) {
        fprintf(stderr, "begin psram failed\n");
        return 1;
    }
    stpg_buffer_reset();
    if (expect(stpg_buffer_begin(STPG_STORAGE_SRAM, total_size, total_crc, 1), ERR_UNSUPPORTED)) {
        fprintf(stderr, "begin sram with flags failed\n");
        return 1;
    }
    if (expect(stpg_buffer_begin(STPG_STORAGE_SRAM, total_size, total_crc, 0), ERR_NONE)) {
        fprintf(stderr, "begin sram failed\n");
        return 1;
    }
    if (expect(stpg_buffer_precheck_chunk(1, 4), ERR_BAD_SIZE)) {
        fprintf(stderr, "precheck offset failed\n");
        return 1;
    }
    if (expect(stpg_buffer_precheck_chunk(0, STPG_UPLOAD_CHUNK_SIZE + 1u), ERR_BAD_SIZE)) {
        fprintf(stderr, "precheck oversize failed\n");
        return 1;
    }
    if (expect(stpg_buffer_write_chunk(0, pattern, 16, stpg_crc32(pattern, 16)), ERR_NONE)) {
        fprintf(stderr, "write_chunk first failed\n");
        return 1;
    }
    if (stpg_buffer_received_size() != 16u) {
        fprintf(stderr, "received_size mismatch: %u\n", (unsigned)stpg_buffer_received_size());
        return 1;
    }
    if (expect(stpg_buffer_write_chunk(16, pattern + 16, total_size - 16u, 0), ERR_BAD_CRC)) {
        fprintf(stderr, "write_chunk bad crc case failed\n");
        return 1;
    }
    if (expect(stpg_buffer_write_chunk(16, pattern + 16, total_size - 16u,
                                       stpg_crc32(pattern + 16, total_size - 16u)), ERR_NONE)) {
        fprintf(stderr, "write_chunk second failed\n");
        return 1;
    }
    if (expect(stpg_buffer_finalize(total_size, total_crc), ERR_NONE)) {
        fprintf(stderr, "finalize failed\n");
        return 1;
    }
    if (!stpg_buffer_has_pattern()) {
        fprintf(stderr, "has_pattern false\n");
        return 1;
    }
    if (stpg_buffer_pattern_size() != total_size) {
        fprintf(stderr, "pattern_size mismatch: %u\n", (unsigned)stpg_buffer_pattern_size());
        return 1;
    }
    if (stpg_buffer_event_count() != 2u) {
        fprintf(stderr, "event_count mismatch: %u\n", (unsigned)stpg_buffer_event_count());
        return 1;
    }
    if (stpg_buffer_pio_word_count() != 8u) {
        fprintf(stderr, "pio_word_count mismatch: %u\n", (unsigned)stpg_buffer_pio_word_count());
        return 1;
    }
    if (stpg_buffer_sample_event_count() != 0u) {
        fprintf(stderr, "sample_event_count mismatch: %u\n", (unsigned)stpg_buffer_sample_event_count());
        return 1;
    }

    const uint32_t *words = stpg_buffer_pio_words();
    stpg_pio_word_view_t view = stpg_buffer_pio_word_view();
    if (view.words != words || view.word_count != 8u || view.event_count != 2u || view.sample_event_count != 0u) {
        fprintf(stderr, "word_view mismatch\n");
        return 1;
    }
    if (words[0] != 5u || words[1] != 0u || words[2] != 0x0003u || words[3] != 0x000Fu) {
        fprintf(stderr, "first word tuple mismatch: %u %u 0x%08x 0x%08x\n",
                (unsigned)words[0], (unsigned)words[1], (unsigned)words[2], (unsigned)words[3]);
        return 1;
    }
    if (words[4] != 10u || words[5] != 0u || words[6] != 0x00AAu || words[7] != 0x0055u) {
        fprintf(stderr, "second word tuple mismatch: %u %u 0x%08x 0x%08x\n",
                (unsigned)words[4], (unsigned)words[5], (unsigned)words[6], (unsigned)words[7]);
        return 1;
    }

    stpg_buffer_reset();
    if (stpg_buffer_has_pattern()) {
        fprintf(stderr, "has_pattern still true after reset\n");
        return 1;
    }
    if (stpg_buffer_pattern_size() != 0u || stpg_buffer_event_count() != 0u) {
        fprintf(stderr, "reset counters mismatch: size=%u events=%u\n",
                (unsigned)stpg_buffer_pattern_size(), (unsigned)stpg_buffer_event_count());
        return 1;
    }
    return 0;
}
