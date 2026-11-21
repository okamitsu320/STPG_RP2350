#include "stpg_app.h"
#include "stpg_board.h"
#include "stpg_common.h"

#include <stddef.h>

static const uint8_t g_dut_gpio_map_24[STPG_ACTIVE_PINS_V11] = {
    2u, 3u, 4u, 5u, 6u, 7u, 8u, 9u,
    10u, 11u, 12u, 13u, 14u, 15u, 16u, 17u,
    18u, 20u, 21u, 22u, 23u, 26u, 27u, 28u,
};

static const uint32_t g_psram_cs1_candidates[] = {
    19u,
};

static const stpg_board_flash_profile_t g_flash_profile = {
    .flash_total_bytes = (4u * 1024u * 1024u),
    .flash_fw_end = 0x00100000u,
    .boot_config_offset = 0x00100000u,
    .boot_config_size = 0x00001000u,
    .slot_base = 0x00101000u,
    .slot_size = 0x000FF000u,
    .slot_count = 3u,
};

void stpg_board_init_safe_gpio(void) {
}

void stpg_board_init_platform_gpio(void) {
}

void stpg_board_init_psram_cs1(void) {
}

uint32_t stpg_board_gpio_for_dut_pin(uint32_t logical_bit) {
    if (logical_bit >= STPG_ACTIVE_PINS_V11) {
        return STPG_DUT_PIN_BASE;
    }
    return g_dut_gpio_map_24[logical_bit];
}

bool stpg_board_gpio_is_dut_pin(uint32_t gpio, uint32_t active_pins) {
    if (active_pins != STPG_ACTIVE_PINS_V01 && active_pins != STPG_ACTIVE_PINS_V11) {
        return false;
    }
    for (uint32_t logical_bit = 0u; logical_bit < active_pins; ++logical_bit) {
        if ((uint32_t)g_dut_gpio_map_24[logical_bit] == gpio) {
            return true;
        }
    }
    return false;
}

uint32_t stpg_board_out_pin_base(uint32_t active_pins) {
    (void)active_pins;
    return STPG_DUT_WINDOW_BASE;
}

uint32_t stpg_board_out_pin_span(uint32_t active_pins) {
    return active_pins == STPG_ACTIVE_PINS_V11 ? STPG_DUT_WINDOW_SPAN_24 : STPG_DUT_WINDOW_SPAN_16;
}

uint32_t stpg_board_out_gap_mask(uint32_t active_pins) {
    return active_pins == STPG_ACTIVE_PINS_V11 ? STPG_PACKED27_GAP_MASK : 0u;
}

uint32_t stpg_board_pack_logical_to_window(uint32_t logical_bits, uint32_t active_pins) {
    if (active_pins != STPG_ACTIVE_PINS_V11) {
        return logical_bits & 0xFFFFu;
    }

    uint32_t packed = logical_bits & 0x0001FFFFu;
    packed |= (logical_bits & 0x001E0000u) << 1u;
    packed |= (logical_bits & 0x00E00000u) << 3u;
    packed &= ~STPG_PACKED27_GAP_MASK;
    return packed;
}

uint32_t stpg_board_unpack_window_to_logical(uint32_t packed_bits, uint32_t active_pins) {
    if (active_pins != STPG_ACTIVE_PINS_V11) {
        return packed_bits & 0xFFFFu;
    }

    uint32_t logical = packed_bits & 0x0001FFFFu;
    logical |= (packed_bits >> 1u) & 0x001E0000u;
    logical |= (packed_bits >> 3u) & 0x00E00000u;
    return logical;
}

const uint32_t *stpg_board_psram_cs1_candidates(size_t *count) {
    if (count != 0) {
        *count = sizeof(g_psram_cs1_candidates) / sizeof(g_psram_cs1_candidates[0]);
    }
    return g_psram_cs1_candidates;
}

void stpg_board_psram_configure_cs1(uint32_t gpio) {
    (void)gpio;
}

void stpg_board_psram_restore_cs1_mux(uint32_t gpio) {
    (void)gpio;
}

const stpg_board_flash_profile_t *stpg_board_flash_profile(void) {
    return &g_flash_profile;
}
