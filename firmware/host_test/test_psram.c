#include "stpg_pattern_buffer.h"

#include <stdint.h>
#include <string.h>

#include "stpg_board.h"
#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_pattern.h"
#include "stpg_psram.h"

static void build_one_event_pattern(uint8_t *buf, uint32_t *total_crc) {
    PatternHeader *h = (PatternHeader *)(void *)buf;
    ExecEvent *event = (ExecEvent *)(void *)(buf + STPG_HEADER_SIZE);

    memset(buf, 0, STPG_HEADER_SIZE + STPG_EXEC_EVENT_SIZE);
    event->wait_ticks = 4;
    event->out_value = 0x00000001u;
    event->out_enable = 0x0000FFFFu;

    h->magic = STPG_PATTERN_MAGIC;
    h->version = STPG_PATTERN_VERSION;
    h->header_size = STPG_HEADER_SIZE;
    h->active_pins = STPG_ACTIVE_PINS_V01;
    h->tick_hz = STPG_TICK_HZ;
    h->event_size = STPG_EXEC_EVENT_SIZE;
    h->event_count = 1u;
    h->data_size = STPG_EXEC_EVENT_SIZE;
    h->data_crc32 = stpg_crc32(event, h->data_size);
    h->header_crc32 = stpg_crc32(buf, STPG_HEADER_SIZE - sizeof(h->header_crc32));
    *total_crc = stpg_crc32(buf, STPG_HEADER_SIZE + h->data_size);
}

static int expect(stpg_err_t actual, stpg_err_t expected) {
    return actual == expected ? 0 : 1;
}

int main(void) {
    uint8_t pattern[STPG_HEADER_SIZE + STPG_EXEC_EVENT_SIZE];
    uint32_t total_crc = 0u;
    build_one_event_pattern(pattern, &total_crc);

    stpg_buffer_reset();
    if (expect(stpg_buffer_begin(STPG_STORAGE_PSRAM, sizeof(pattern), total_crc, 0), ERR_NONE)) return 1;
    if (expect(stpg_buffer_write_chunk(0, pattern, sizeof(pattern), total_crc), ERR_NONE)) return 1;
    if (expect(stpg_buffer_finalize(sizeof(pattern), total_crc), ERR_NONE)) return 1;
    if (stpg_buffer_active_source() != STPG_PATTERN_SOURCE_PSRAM) return 1;
    if (stpg_buffer_pattern_size() != sizeof(pattern)) return 1;
    if (stpg_buffer_event_count() != 1u) return 1;

    uint32_t selected_cs1 = UINT32_MAX;
    if (!stpg_psram_get_selected_cs1_gpio(&selected_cs1)) return 1;
    if (selected_cs1 != 19u) return 1;
    if (!stpg_psram_selected_cs1_mux_matches()) return 1;
    if (stpg_psram_get_selected_cs1_function() != 0u) return 1;
    if (stpg_board_gpio_is_dut_pin(19u, STPG_ACTIVE_PINS_V11)) return 1;
    if (stpg_board_gpio_is_dut_pin(24u, STPG_ACTIVE_PINS_V11)) return 1;
    if (stpg_board_gpio_is_dut_pin(25u, STPG_ACTIVE_PINS_V11)) return 1;
    if (!stpg_board_gpio_is_dut_pin(8u, STPG_ACTIVE_PINS_V11)) return 1;
    if (stpg_board_gpio_is_dut_pin(18u, STPG_ACTIVE_PINS_V01)) return 1;

    uint32_t crc = 0u;
    if (expect(stpg_buffer_psram_checksum(STPG_PSRAM_PATTERN_BASE, sizeof(pattern), &crc), ERR_NONE)) return 1;
    if (crc != total_crc) return 1;

    uint8_t readback[sizeof(pattern)];
    if (expect(stpg_buffer_psram_read_bytes(STPG_PSRAM_PATTERN_BASE, readback, sizeof(readback)), ERR_NONE)) return 1;
    if (memcmp(readback, pattern, sizeof(pattern)) != 0) return 1;

    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) return 1;
    if (stpg_buffer_pio_word_count() != 4u) return 1;

    if (expect(stpg_buffer_psram_erase(0u, 0u), ERR_NONE)) return 1;
    if (stpg_buffer_active_source() != STPG_PATTERN_SOURCE_NONE) return 1;
    uint8_t meta[sizeof(PsramPatternSlot)];
    if (expect(stpg_buffer_psram_read_bytes(STPG_PSRAM_META_BASE, meta, sizeof(meta)), ERR_NONE)) return 1;
    for (uint32_t i = 0; i < sizeof(meta); ++i) {
        if (meta[i] != 0u) return 1;
    }

    return 0;
}
