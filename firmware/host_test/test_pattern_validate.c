#include "stpg_pattern_validate.h"

#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "stpg_board.h"
#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_pattern.h"

static uint8_t minimal_pattern[STPG_HEADER_SIZE + STPG_EXEC_EVENT_SIZE] = {
    0x53, 0x54, 0x46, 0x47, 0x01, 0x00, 0x28, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00, 0x00,
    0x80, 0xD1, 0xF0, 0x08, 0x14, 0x00, 0x00, 0x00,
    0x01, 0x00, 0x00, 0x00, 0x14, 0x00, 0x00, 0x00,
    0x8D, 0x9B, 0xD5, 0x0F, 0xF2, 0xA4, 0x4D, 0x9E,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
};

typedef struct {
    RateVectorHeader header;
    RateDesc rate;
    RateSlot slot;
    VectorRecord vector;
} ratevec_fixture_t;

typedef struct {
    PatternHeader header;
    ExecEvent32 event;
} wide_pattern_fixture_t;

static void build_minimal_wide_pattern(wide_pattern_fixture_t *fixture) {
    memset(fixture, 0, sizeof(*fixture));
    fixture->event.wait_ticks = 480000u;
    fixture->event.out_value = 0x0003u;
    fixture->event.out_enable = 0x0003u;
    fixture->header.magic = STPG_PATTERN_MAGIC;
    fixture->header.version = STPG_PATTERN_VERSION_WIDE_WAIT;
    fixture->header.header_size = STPG_HEADER_SIZE;
    fixture->header.active_pins = STPG_ACTIVE_PINS_V01;
    fixture->header.tick_hz = STPG_TICK_HZ;
    fixture->header.event_size = STPG_EXEC_EVENT32_SIZE;
    fixture->header.event_count = 1u;
    fixture->header.data_size = STPG_EXEC_EVENT32_SIZE;
    fixture->header.data_crc32 = stpg_crc32(&fixture->event, sizeof(fixture->event));
    fixture->header.header_crc32 = stpg_crc32(&fixture->header, STPG_HEADER_SIZE - sizeof(fixture->header.header_crc32));
}

static void build_minimal_rate_vector(ratevec_fixture_t *fixture, uint16_t slot_time_ticks) {
    memset(fixture, 0, sizeof(*fixture));
    fixture->rate.period_ticks = 16u;
    fixture->rate.slot_count = 1u;
    fixture->rate.flags = 0u;
    fixture->rate.slot_offset = 0u;

    fixture->slot.time_ticks = slot_time_ticks;
    fixture->slot.flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    fixture->slot.drive_mask = 0x0001u;
    fixture->slot.dir_mask = 0x0001u;
    fixture->slot.sample_mask = 0u;

    fixture->vector.rate_id = 0u;
    fixture->vector.flags = 0u;
    fixture->vector.repeat = 1u;
    fixture->vector.drive_value = 0x0001u;
    fixture->vector.drive_enable = 0x0001u;
    fixture->vector.expect_value = 0u;
    fixture->vector.expect_mask = 0u;

    fixture->header.magic = STPG_RATEVEC_MAGIC;
    fixture->header.version = STPG_PATTERN_VERSION;
    fixture->header.header_size = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.flags = 0u;
    fixture->header.active_pins = STPG_ACTIVE_PINS_V01;
    fixture->header.tick_hz = STPG_TICK_HZ;
    fixture->header.rate_count = 1u;
    fixture->header.slot_count = 1u;
    fixture->header.vector_count = 1u;
    fixture->header.rate_table_offset = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.slot_table_offset = STPG_RATEVEC_HEADER_SIZE + STPG_RATE_DESC_SIZE;
    fixture->header.vector_offset = fixture->header.slot_table_offset + STPG_RATE_SLOT_SIZE;
    fixture->header.data_crc32 = 0u;
    fixture->header.reserved0 = 0u;
    fixture->header.reserved1 = 0u;
    fixture->header.reserved2 = 0u;

    uint32_t data_crc = 0u;
    data_crc = stpg_crc32_continue(data_crc, &fixture->rate, sizeof(fixture->rate));
    data_crc = stpg_crc32_continue(data_crc, &fixture->slot, sizeof(fixture->slot));
    data_crc = stpg_crc32_continue(data_crc, &fixture->vector, sizeof(fixture->vector));
    fixture->header.data_crc32 = data_crc;
    fixture->header.header_crc32 = stpg_crc32(&fixture->header, STPG_RATEVEC_HEADER_SIZE - sizeof(fixture->header.header_crc32));
}

static int expect(stpg_err_t actual, stpg_err_t expected) {
    if (actual != expected) {
        fprintf(stderr, "expected %u got %u\n", (unsigned)expected, (unsigned)actual);
    }
    return actual == expected ? 0 : 1;
}

/* Isolate each CRC layer: recompute every outer CRC so that another guard
 * cannot accidentally stand in for the check under test. No target hardware. */
static int check_raw_crc_layers(const uint8_t *pattern, size_t size) {
    uint8_t bad[sizeof(wide_pattern_fixture_t)];
    if (size > sizeof(bad)) return 1;
    const uint32_t total_crc = stpg_crc32(pattern, size);
    if (expect(stpg_validate_pattern(pattern, size, total_crc), ERR_NONE)) return 1;
    if (expect(stpg_validate_pattern(pattern, size, total_crc ^ 1u), ERR_BAD_CRC)) return 1;

    memcpy(bad, pattern, size);
    PatternHeader *header = (PatternHeader *)(void *)bad;
    header->header_crc32 ^= 1u;
    if (expect(stpg_validate_pattern(bad, size, stpg_crc32(bad, size)), ERR_BAD_CRC)) return 1;

    memcpy(bad, pattern, size);
    header->data_crc32 ^= 1u;
    header->header_crc32 = stpg_crc32(bad, STPG_HEADER_SIZE - sizeof(header->header_crc32));
    if (expect(stpg_validate_pattern(bad, size, stpg_crc32(bad, size)), ERR_BAD_CRC)) return 1;
    return 0;
}

static int check_rate_vector_crc_layers(const ratevec_fixture_t *valid) {
    ratevec_fixture_t bad = *valid;
    const uint32_t crc = stpg_crc32(valid, sizeof(*valid));
    if (expect(stpg_validate_pattern((const uint8_t *)valid, sizeof(*valid), crc ^ 1u), ERR_RATEVEC_BAD_CRC)) return 1;

    bad.header.header_crc32 ^= 1u;
    if (expect(stpg_validate_pattern((const uint8_t *)&bad, sizeof(bad), stpg_crc32(&bad, sizeof(bad))), ERR_RATEVEC_BAD_CRC)) return 1;

    bad = *valid;
    bad.header.data_crc32 ^= 1u;
    bad.header.header_crc32 = stpg_crc32(&bad.header, STPG_RATEVEC_HEADER_SIZE - sizeof(bad.header.header_crc32));
    if (expect(stpg_validate_pattern((const uint8_t *)&bad, sizeof(bad), stpg_crc32(&bad, sizeof(bad))), ERR_RATEVEC_BAD_CRC)) return 1;
    return 0;
}

int main(void) {
    if (STPG_DUT_PIN_BASE != 2u) return 1;
    if (STPG_DUT_PIN_COUNT != 16u) return 1;
    if (STPG_DUT_PIN_LAST != 17u) return 1;
    if (STPG_UART_TX_PIN != 0u || STPG_UART_RX_PIN != 1u) return 1;
    if (stpg_board_gpio_for_dut_pin(0u) != 2u) return 1;
    if (stpg_board_gpio_for_dut_pin(15u) != 17u) return 1;

    uint32_t pio_words[4] = {0xFFFFFFFFu, 0xFFFFFFFFu, 0xFFFFFFFFu, 0xFFFFFFFFu};
    uint32_t word_count = 0;
    uint32_t sample_event_count = 0;

    if (stpg_crc32(minimal_pattern, sizeof(minimal_pattern)) != 0xA3C1CA20u) return 1;
    if (expect(stpg_validate_pattern(minimal_pattern, sizeof(minimal_pattern), 0xA3C1CA20u), ERR_NONE)) return 1;
    if (expect(stpg_expand_raw_events_to_pio_words(minimal_pattern, sizeof(minimal_pattern),
                                                   pio_words, 4, &word_count, &sample_event_count), ERR_NONE)) return 1;
    if (word_count != 4u || sample_event_count != 0u) return 1;
    if (pio_words[0] != 0u || pio_words[1] != 0u || pio_words[2] != 0u || pio_words[3] != 0u) return 1;

    wide_pattern_fixture_t wide;
    build_minimal_wide_pattern(&wide);
    if (check_raw_crc_layers(minimal_pattern, sizeof(minimal_pattern))) return 1;
    if (check_raw_crc_layers((const uint8_t *)&wide, sizeof(wide))) return 1;
    memset(pio_words, 0, sizeof(pio_words));
    if (expect(stpg_validate_pattern((const uint8_t *)&wide, sizeof(wide), stpg_crc32(&wide, sizeof(wide))), ERR_NONE)) return 1;
    if (expect(stpg_expand_raw_events_to_pio_words((const uint8_t *)&wide, sizeof(wide),
                                                   pio_words, 4, &word_count, &sample_event_count), ERR_NONE)) return 1;
    if (word_count != 4u || pio_words[0] != 480000u || pio_words[2] != 3u || pio_words[3] != 3u) return 1;

    uint8_t bad[sizeof(minimal_pattern)];
    memcpy(bad, minimal_pattern, sizeof(bad));
    ((PatternHeader *)(void *)bad)->header_crc32 ^= 1u;
    if (expect(stpg_validate_pattern(bad, sizeof(bad), stpg_crc32(bad, sizeof(bad))), ERR_BAD_CRC)) return 1;

    memcpy(bad, minimal_pattern, sizeof(bad));
    ((PatternHeader *)(void *)bad)->active_pins = 24u;
    ((PatternHeader *)(void *)bad)->header_crc32 = stpg_crc32(bad, STPG_HEADER_SIZE - 4u);
    if (expect(stpg_validate_pattern(bad, sizeof(bad), stpg_crc32(bad, sizeof(bad))), ERR_NONE)) return 1;

    memcpy(bad, minimal_pattern, sizeof(bad));
    ExecEvent *ev = (ExecEvent *)(void *)(bad + STPG_HEADER_SIZE);
    ev->sample_mask = 0x00010000u;
    PatternHeader *h = (PatternHeader *)(void *)bad;
    h->data_crc32 = stpg_crc32(bad + STPG_HEADER_SIZE, STPG_EXEC_EVENT_SIZE);
    h->header_crc32 = stpg_crc32(bad, STPG_HEADER_SIZE - 4u);
    if (expect(stpg_validate_pattern(bad, sizeof(bad), stpg_crc32(bad, sizeof(bad))), ERR_UNSUPPORTED)) return 1;

    memcpy(bad, minimal_pattern, sizeof(bad));
    ev = (ExecEvent *)(void *)(bad + STPG_HEADER_SIZE);
    ev->flags = STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT;
    ev->expect_value = 0x0001u;
    h = (PatternHeader *)(void *)bad;
    h->data_crc32 = stpg_crc32(bad + STPG_HEADER_SIZE, STPG_EXEC_EVENT_SIZE);
    h->header_crc32 = stpg_crc32(bad, STPG_HEADER_SIZE - 4u);
    if (expect(stpg_validate_pattern(bad, sizeof(bad), stpg_crc32(bad, sizeof(bad))), ERR_NONE)) return 1;

    ratevec_fixture_t ratevec;
    build_minimal_rate_vector(&ratevec, 0u);
    if (expect(stpg_validate_pattern((const uint8_t *)&ratevec, sizeof(ratevec), stpg_crc32(&ratevec, sizeof(ratevec))), ERR_NONE)) return 1;
    if (check_rate_vector_crc_layers(&ratevec)) return 1;

    build_minimal_rate_vector(&ratevec, 16u);
    if (expect(stpg_validate_pattern((const uint8_t *)&ratevec, sizeof(ratevec), stpg_crc32(&ratevec, sizeof(ratevec))), ERR_RATE_PERIOD_MISMATCH)) return 1;

    build_minimal_rate_vector(&ratevec, 0u);
    ratevec.header.slot_table_offset ^= 2u;
    ratevec.header.header_crc32 = stpg_crc32(&ratevec.header, STPG_RATEVEC_HEADER_SIZE - sizeof(ratevec.header.header_crc32));
    if (expect(stpg_validate_pattern((const uint8_t *)&ratevec, sizeof(ratevec), stpg_crc32(&ratevec, sizeof(ratevec))), ERR_RATEVEC_BAD_OFFSET)) return 1;

    return 0;
}
