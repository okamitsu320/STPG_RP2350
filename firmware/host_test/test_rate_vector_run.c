#include "stpg_pattern_buffer.h"

#include <stdint.h>
#include <string.h>

#include "stpg_board.h"
#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_pattern.h"

typedef struct {
    RateVectorHeader header;
    RateDesc rate;
    RateSlot slots[2];
    VectorRecord vector;
} rate_vector_fixture_t;

static void build_rate_vector(rate_vector_fixture_t *fixture, uint32_t *total_crc) {
    memset(fixture, 0, sizeof(*fixture));

    fixture->rate.period_ticks = 24u;
    fixture->rate.slot_count = 2u;
    fixture->rate.slot_offset = 0u;

    fixture->slots[0].time_ticks = 0u;
    fixture->slots[0].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    fixture->slots[0].drive_mask = 0x0003u;
    fixture->slots[0].dir_mask = 0x0003u;

    fixture->slots[1].time_ticks = 12u;
    fixture->slots[1].flags = STPG_DRIVE_ZERO | STPG_RATE_SLOT_SAMPLE;
    fixture->slots[1].drive_mask = 0x0003u;
    fixture->slots[1].sample_mask = 0x0003u;

    fixture->vector.rate_id = 0u;
    fixture->vector.repeat = 2u;
    fixture->vector.drive_value = 0x0003u;
    fixture->vector.drive_enable = 0x0003u;
    fixture->vector.expect_value = 0u;
    fixture->vector.expect_mask = 0x0003u;

    fixture->header.magic = STPG_RATEVEC_MAGIC;
    fixture->header.version = STPG_PATTERN_VERSION;
    fixture->header.header_size = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.active_pins = STPG_ACTIVE_PINS_V01;
    fixture->header.tick_hz = STPG_TICK_HZ;
    fixture->header.rate_count = 1u;
    fixture->header.slot_count = 2u;
    fixture->header.vector_count = 1u;
    fixture->header.rate_table_offset = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.slot_table_offset = STPG_RATEVEC_HEADER_SIZE + STPG_RATE_DESC_SIZE;
    fixture->header.vector_offset = fixture->header.slot_table_offset + (2u * STPG_RATE_SLOT_SIZE);

    uint32_t data_crc = 0u;
    data_crc = stpg_crc32_continue(data_crc, &fixture->rate, sizeof(fixture->rate));
    data_crc = stpg_crc32_continue(data_crc, &fixture->slots[0], sizeof(fixture->slots[0]));
    data_crc = stpg_crc32_continue(data_crc, &fixture->slots[1], sizeof(fixture->slots[1]));
    data_crc = stpg_crc32_continue(data_crc, &fixture->vector, sizeof(fixture->vector));
    fixture->header.data_crc32 = data_crc;
    fixture->header.header_crc32 = stpg_crc32(&fixture->header, STPG_RATEVEC_HEADER_SIZE - sizeof(fixture->header.header_crc32));
    *total_crc = stpg_crc32(fixture, sizeof(*fixture));
}

static int expect(stpg_err_t actual, stpg_err_t expected) {
    return actual == expected ? 0 : 1;
}

int main(void) {
    rate_vector_fixture_t fixture;
    uint32_t total_crc = 0u;
    build_rate_vector(&fixture, &total_crc);

    stpg_buffer_reset();
    if (expect(stpg_buffer_begin(STPG_STORAGE_PSRAM, sizeof(fixture), total_crc, 0u), ERR_NONE)) return 1;
    if (expect(stpg_buffer_write_chunk(0u, &fixture, sizeof(fixture), total_crc), ERR_NONE)) return 1;
    if (expect(stpg_buffer_finalize(sizeof(fixture), total_crc), ERR_NONE)) return 1;
    if (stpg_buffer_active_source() != STPG_PATTERN_SOURCE_PSRAM) return 1;

    stpg_rate_info_t info;
    if (expect(stpg_buffer_get_rate_info(&info), ERR_NONE)) return 1;
    if (!info.loaded || info.vector_count != 1u || info.rate_count != 1u || info.slot_count != 2u) return 1;

    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) return 1;
    if (stpg_buffer_event_count() != 4u) return 1;
    if (stpg_buffer_sample_event_count() != 2u) return 1;
    if (stpg_buffer_pio_word_count() != 16u) return 1;

    const uint32_t *words = stpg_buffer_pio_words();
    if (words[0] != 0u || words[1] != 0u || words[2] != 0x0003u || words[3] != 0x0003u) return 1;
    if (words[4] != 1u || words[5] != 1u || words[6] != 0x0000u || words[7] != 0x0003u) return 1;
    if (words[8] != 2u || words[9] != 0u || words[10] != 0x0003u || words[11] != 0x0003u) return 1;
    if (words[12] != 1u || words[13] != 1u || words[14] != 0x0000u || words[15] != 0x0003u) return 1;

    const ExecEvent *events = stpg_buffer_events();
    if (events == 0) return 1;
    if ((events[1].flags & (STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT)) != (STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT)) return 1;
    if (events[1].sample_mask != 0x0003u || events[1].expect_value != 0u) return 1;

    return 0;
}
