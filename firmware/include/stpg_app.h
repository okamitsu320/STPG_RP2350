#ifndef STPG_APP_H
#define STPG_APP_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint32_t flash_total_bytes;
    uint32_t flash_fw_end;
    uint32_t boot_config_offset;
    uint32_t boot_config_size;
    uint32_t slot_base;
    uint32_t slot_size;
    uint32_t slot_count;
} stpg_board_flash_profile_t;

void stpg_board_init_platform_gpio(void);
void stpg_board_init_psram_cs1(void);
void stpg_board_init_safe_gpio(void);
const uint32_t *stpg_board_psram_cs1_candidates(size_t *count);
void stpg_board_psram_configure_cs1(uint32_t gpio);
/* Restore only the accepted CS1 GPIO mux; do not rewrite flash metadata. */
void stpg_board_psram_restore_cs1_mux(uint32_t gpio);
const stpg_board_flash_profile_t *stpg_board_flash_profile(void);

#endif
