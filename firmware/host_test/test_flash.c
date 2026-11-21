#include "stpg_flash.h"

#include <stdint.h>
#include <string.h>

#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_pattern.h"
#include "stpg_pattern_buffer.h"
#include "stpg_protocol.h"

typedef struct {
    RateVectorHeader header;
    RateDesc rate;
    RateSlot slots[2];
    VectorRecord vector;
} rate_vector_fixture_t;

static int expect(stpg_err_t actual, stpg_err_t expected) {
    return actual == expected ? 0 : 1;
}

static void build_valid_meta(FlashSlotMeta *meta, uint32_t pattern_size) {
    memset(meta, 0xFF, sizeof(*meta));
    meta->magic = STPG_FLASH_SLOT_MAGIC;
    meta->meta_version = STPG_FLASH_SLOT_META_VERSION;
    meta->meta_crc32 = 0u;
    meta->pattern_offset = STPG_FLASH_SLOT_META_BYTES;
    meta->pattern_size = pattern_size;
    meta->pattern_crc32 = 0x12345678u;
    meta->format = STPG_PATTERN_FORMAT_RAW_EVENT;
    meta->active_pins = STPG_ACTIVE_PINS_V01;
    meta->tick_hz = STPG_TICK_HZ;
    meta->flags = 0u;
    memcpy(meta->name, "slot0", 5u);
    meta->created_at = 0u;
    meta->meta_crc32 = stpg_crc32(meta, sizeof(*meta));
}

static void build_one_event_pattern(uint8_t *buf, uint32_t *total_crc) {
    PatternHeader *h = (PatternHeader *)(void *)buf;
    ExecEvent *event = (ExecEvent *)(void *)(buf + STPG_HEADER_SIZE);

    memset(buf, 0, STPG_HEADER_SIZE + STPG_EXEC_EVENT_SIZE);
    event->wait_ticks = 4u;
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
    fixture->slots[1].flags = STPG_DRIVE_ZERO;
    fixture->slots[1].drive_mask = 0x0003u;

    fixture->vector.rate_id = 0u;
    fixture->vector.repeat = 2u;
    fixture->vector.drive_value = 0x0003u;
    fixture->vector.drive_enable = 0x0003u;

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

int main(void) {
    stpg_flash_test_reset();

    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (profile == 0) return 1;
    if (profile->slot_count != 3u) return 1;
    if (profile->slot_size != 0x000FF000u) return 1;
    if (profile->boot_config_offset != 0x00100000u) return 1;
    if ((stpg_flash_capability_flags() & (CAP_FLASH_SLOTS | CAP_UPLOAD_FLASH | CAP_HEADLESS_BOOT)) != (CAP_FLASH_SLOTS | CAP_UPLOAD_FLASH | CAP_HEADLESS_BOOT)) return 1;

    uint32_t bitmap = 0u;
    uint32_t slot_count = 0u;
    uint32_t slot_size = 0u;
    if (expect(stpg_flash_list_slots(&bitmap, &slot_count, &slot_size), ERR_NONE)) return 1;
    if (bitmap != 0u) return 1;
    if (slot_count != profile->slot_count) return 1;
    if (slot_size != profile->slot_size) return 1;

    stpg_flash_slot_info_t info;
    if (expect(stpg_flash_get_slot_info(0u, &info), ERR_NONE)) return 1;
    if (info.valid) return 1;

    FlashSlotMeta meta;
    build_valid_meta(&meta, 512u);
    if (expect(stpg_flash_test_write(profile->slot_base, &meta, sizeof(meta)), ERR_NONE)) return 1;

    if (expect(stpg_flash_list_slots(&bitmap, &slot_count, &slot_size), ERR_NONE)) return 1;
    if (bitmap != 0x1u) return 1;

    if (expect(stpg_flash_get_slot_info(0u, &info), ERR_NONE)) return 1;
    if (!info.valid) return 1;
    if (info.pattern_size != 512u) return 1;
    if (info.tick_hz != STPG_TICK_HZ) return 1;
    FlashSlotMeta read_meta;
    if (expect(stpg_flash_get_slot_meta(0u, &read_meta), ERR_NONE)) return 1;
    if (memcmp(&read_meta, &meta, sizeof(meta)) != 0) return 1;

    /* The stack-free CRC verifier must still reject even a one-byte
     * modification outside the stored CRC field. */
    read_meta.name[0] ^= 1;
    if (expect(stpg_flash_test_write(profile->slot_base, &read_meta, sizeof(read_meta)), ERR_NONE)) return 1;
    if (expect(stpg_flash_get_slot_info(0u, &info), ERR_FLASH_BAD_META)) return 1;
    if (expect(stpg_flash_test_write(profile->slot_base, &meta, sizeof(meta)), ERR_NONE)) return 1;

    meta.tick_hz = STPG_TICK_HZ + 1u;
    meta.meta_crc32 = 0u;
    meta.meta_crc32 = stpg_crc32(&meta, sizeof(meta));
    if (expect(stpg_flash_test_write(profile->slot_base + profile->slot_size, &meta, sizeof(meta)), ERR_NONE)) return 1;
    if (expect(stpg_flash_get_slot_info(1u, &info), ERR_FLASH_BAD_META)) return 1;

    uint8_t pattern[STPG_HEADER_SIZE + STPG_EXEC_EVENT_SIZE];
    uint32_t total_crc = 0u;
    build_one_event_pattern(pattern, &total_crc);

    stpg_buffer_reset();
    if (expect(stpg_buffer_flash_select_write(2u), ERR_NONE)) return 1;
    if (expect(stpg_buffer_flash_erase_slot(2u), ERR_NONE)) return 1;
    if (expect(stpg_buffer_begin(STPG_STORAGE_FLASH, sizeof(pattern), total_crc, 0u), ERR_NONE)) return 1;
    if (expect(stpg_buffer_write_chunk(0u, pattern, sizeof(pattern), total_crc), ERR_NONE)) return 1;
    if (expect(stpg_buffer_finalize(sizeof(pattern), total_crc), ERR_NONE)) return 1;
    if (stpg_buffer_active_source() != STPG_PATTERN_SOURCE_FLASH) return 1;

    if (expect(stpg_flash_list_slots(&bitmap, &slot_count, &slot_size), ERR_NONE)) return 1;
    if (bitmap != 0x5u) return 1;
    if (expect(stpg_flash_get_slot_info(2u, &info), ERR_NONE)) return 1;
    if (!info.valid) return 1;
    if (info.pattern_size != sizeof(pattern)) return 1;
    if (info.format != STPG_PATTERN_FORMAT_RAW_EVENT) return 1;
    stpg_buffer_reset();
    if (expect(stpg_buffer_flash_select_run(2u), ERR_NONE)) return 1;
    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) return 1;
    if (stpg_buffer_event_count() != 1u) return 1;
    if (stpg_buffer_pio_word_count() != 4u) return 1;
    if (stpg_buffer_pio_words()[2] != 0x0001u) return 1;

    rate_vector_fixture_t rate_fixture;
    build_rate_vector(&rate_fixture, &total_crc);
    stpg_buffer_reset();
    if (expect(stpg_buffer_flash_select_write(1u), ERR_NONE)) return 1;
    if (expect(stpg_buffer_flash_erase_slot(1u), ERR_NONE)) return 1;
    if (expect(stpg_buffer_begin(STPG_STORAGE_FLASH, sizeof(rate_fixture), total_crc, 0u), ERR_NONE)) return 1;
    if (expect(stpg_buffer_write_chunk(0u, &rate_fixture, sizeof(rate_fixture), total_crc), ERR_NONE)) return 1;
    if (expect(stpg_buffer_finalize(sizeof(rate_fixture), total_crc), ERR_NONE)) return 1;
    stpg_buffer_reset();
    if (expect(stpg_buffer_flash_select_run(1u), ERR_NONE)) return 1;
    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) return 1;
    if (stpg_buffer_event_count() != 4u) return 1;
    if (stpg_buffer_sample_event_count() != 0u) return 1;
    if (stpg_buffer_pio_word_count() != 16u) return 1;
    if (stpg_buffer_pio_words()[2] != 0x0003u || stpg_buffer_pio_words()[3] != 0x0003u) return 1;

    stpg_flash_boot_config_info_t boot_info;
    if (expect(stpg_flash_get_boot_config(&boot_info), ERR_NONE)) return 1;
    if (boot_info.valid) return 1;
    if (expect(stpg_flash_set_boot_slot(1u, STPG_BOOT_FLAG_ENABLE_AUTO_RUN), ERR_NONE)) return 1;
    if (expect(stpg_flash_get_boot_config(&boot_info), ERR_NONE)) return 1;
    if (!boot_info.valid) return 1;
    if (boot_info.boot_slot_id != 1u) return 1;
    if (boot_info.boot_flags != STPG_BOOT_FLAG_ENABLE_AUTO_RUN) return 1;
    if (expect(stpg_flash_set_boot_slot(0u, 0x2u), ERR_UNSUPPORTED)) return 1;
    if (expect(stpg_flash_disable_boot_slot(), ERR_NONE)) return 1;
    if (expect(stpg_flash_get_boot_config(&boot_info), ERR_NONE)) return 1;
    if (!boot_info.valid) return 1;
    if (boot_info.boot_slot_id != STPG_BOOT_SLOT_DISABLED) return 1;
    if (boot_info.boot_flags != 0u) return 1;

    return 0;
}
