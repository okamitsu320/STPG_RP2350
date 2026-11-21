#ifndef STPG_FLASH_H
#define STPG_FLASH_H

#include <stdbool.h>
#include <stdint.h>

#include "stpg_app.h"
#include "stpg_error.h"
#include "stpg_pattern.h"

typedef struct {
    bool valid;
    uint32_t pattern_size;
    uint32_t pattern_offset;
    uint32_t format;
    uint32_t active_pins;
    uint32_t tick_hz;
} stpg_flash_slot_info_t;

typedef struct {
    bool valid;
    uint32_t version;
    uint32_t boot_slot_id;
    uint32_t boot_flags;
} stpg_flash_boot_config_info_t;

#define STPG_BOOT_FLAG_ENABLE_AUTO_RUN   (1u << 0)

#define STPG_BOOTCFG_READ_CURRENT 0u
#define STPG_BOOTCFG_SET_SLOT     1u
#define STPG_BOOTCFG_DISABLE      2u

void stpg_flash_init(void);
uint32_t stpg_flash_capability_flags(void);
const stpg_board_flash_profile_t *stpg_flash_profile(void);
stpg_err_t stpg_flash_list_slots(uint32_t *out_valid_bitmap, uint32_t *out_slot_count, uint32_t *out_slot_size);
stpg_err_t stpg_flash_get_slot_info(uint32_t slot_id, stpg_flash_slot_info_t *out_info);
stpg_err_t stpg_flash_get_slot_meta(uint32_t slot_id, FlashSlotMeta *out_meta);
stpg_err_t stpg_flash_erase_slot(uint32_t slot_id);
stpg_err_t stpg_flash_read_slot_payload(uint32_t slot_id, uint32_t payload_offset, void *dst, uint32_t size);
stpg_err_t stpg_flash_write_slot_payload_from_psram(uint32_t slot_id, uint32_t pattern_offset,
                                                    uint32_t pattern_size, uint32_t psram_offset,
                                                    uint32_t expected_crc32);
stpg_err_t stpg_flash_write_slot_meta(uint32_t slot_id, const FlashSlotMeta *meta);
stpg_err_t stpg_flash_get_boot_config(stpg_flash_boot_config_info_t *out_info);
stpg_err_t stpg_flash_set_boot_slot(uint32_t slot_id, uint32_t boot_flags);
stpg_err_t stpg_flash_disable_boot_slot(void);

#ifndef PICO_ON_DEVICE
void stpg_flash_test_reset(void);
stpg_err_t stpg_flash_test_write(uint32_t offset, const void *src, uint32_t size);
#endif

#endif
