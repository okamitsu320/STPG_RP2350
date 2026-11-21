#include "stpg_app.h"

#include "hardware/flash.h"
#include "hardware/gpio.h"
#include "stpg_board.h"
#include "stpg_common.h"

static const uint8_t g_dut_gpio_map_24[STPG_ACTIVE_PINS_V11] = {
    2u, 3u, 4u, 5u, 6u, 7u, 8u, 9u,
    10u, 11u, 12u, 13u, 14u, 15u, 16u, 17u,
    18u, 20u, 21u, 22u, 23u, 26u, 27u, 28u,
};

static const uint32_t g_psram_cs1_candidates[] = {
    19u,
};

#define STPG_PSRAM_CS1_GPIO 19u

#ifndef PICO_FLASH_SIZE_BYTES
#define PICO_FLASH_SIZE_BYTES (4u * 1024u * 1024u)
#endif

static const stpg_board_flash_profile_t g_flash_profile = {
    .flash_total_bytes = PICO_FLASH_SIZE_BYTES,
    .flash_fw_end = 0x00100000u,
    .boot_config_offset = 0x00100000u,
    .boot_config_size = 0x00001000u,
    .slot_base = 0x00101000u,
    .slot_size = 0x000FF000u,
    .slot_count = (PICO_FLASH_SIZE_BYTES >= 0x003FE000u)
        ? 3u
        : 0u,
};

void stpg_board_init_platform_gpio(void) {
    /* Pico 2's user LED is a board-owned output, not a STPG DUT pin.  Give it
     * a quiet low initial level once at boot; RESET/STOP must not reconfigure
     * it as an input or otherwise change its state. */
    const uint32_t led_gpio = 25u;
    gpio_init(led_gpio);
    gpio_disable_pulls(led_gpio);
    gpio_put(led_gpio, 0u);
    gpio_set_dir(led_gpio, GPIO_OUT);
}

void stpg_board_init_psram_cs1(void) {
    /* GPIO19 is this board's wired PSRAM CS1. Select it before the first USB
     * command can trigger PSRAM discovery, and remove the reset-default
     * pull-down before exposing the pad to the QMI mux. This is not a DUT
     * RESET/STOP pin; QMI owns it continuously after this one-time init. */
    gpio_disable_pulls(STPG_PSRAM_CS1_GPIO);
    stpg_board_psram_configure_cs1(STPG_PSRAM_CS1_GPIO);
}

void stpg_board_init_safe_gpio(void) {
    /* Only pins explicitly mapped to the STPG DUT connector are owned by
     * RESET/STOP. GPIO19 is initialized once as this board's QMI/XIP CS1 and
     * remains outside this release policy, as do GPIO24 (VBUS sense), GPIO25
     * (board LED), and GPIO29 (VSYS sense). */
    for (uint32_t i = 0; i < STPG_ACTIVE_PINS_V11; ++i) {
        const uint32_t gpio = stpg_board_gpio_for_dut_pin(i);
        gpio_init(gpio);
        gpio_set_dir(gpio, GPIO_IN);
        gpio_pull_down(gpio);
        gpio_set_input_enabled(gpio, false);
    }
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

    uint32_t packed = logical_bits & 0x0001FFFFu;          // bits 0..16 -> positions 0..16
    packed |= (logical_bits & 0x001E0000u) << 1u;          // bits 17..20 -> positions 18..21
    packed |= (logical_bits & 0x00E00000u) << 3u;          // bits 21..23 -> positions 24..26
    packed &= ~STPG_PACKED27_GAP_MASK;
    return packed;
}

uint32_t stpg_board_unpack_window_to_logical(uint32_t packed_bits, uint32_t active_pins) {
    if (active_pins != STPG_ACTIVE_PINS_V11) {
        return packed_bits & 0xFFFFu;
    }

    uint32_t logical = packed_bits & 0x0001FFFFu;          // positions 0..16 -> bits 0..16
    logical |= (packed_bits >> 1u) & 0x001E0000u;          // positions 18..21 -> bits 17..20
    logical |= (packed_bits >> 3u) & 0x00E00000u;          // positions 24..26 -> bits 21..23
    return logical;
}

const uint32_t *stpg_board_psram_cs1_candidates(size_t *count) {
    if (count != 0) {
        *count = sizeof(g_psram_cs1_candidates) / sizeof(g_psram_cs1_candidates[0]);
    }
    return g_psram_cs1_candidates;
}

void stpg_board_psram_configure_cs1(uint32_t gpio) {
    /* Reassert the CS1 pad policy at every mux handoff, not just at boot.
     * The R380 post-run snapshot observed a pull enabled after the complete
     * upload/RUN/RESET sequence, but its runner did not preserve raw register
     * values on validation failure, so the exact intervening writer is unknown.
     * Clear both before and after selecting QMI to keep the transition and the
     * resulting pad state explicit. */
    gpio_disable_pulls(gpio);
    flash_devinfo_set_cs_size(1u, FLASH_DEVINFO_SIZE_8M);
    flash_devinfo_set_cs_gpio(1u, gpio);
    gpio_set_function(gpio, GPIO_FUNC_XIP_CS1);
    gpio_disable_pulls(gpio);
}

void stpg_board_psram_restore_cs1_mux(uint32_t gpio) {
    /* RUN returns mapped DUT pins to SIO; when a later PSRAM operation restores
     * CS1, reassert the pad's no-internal-pulls invariant as well as its mux. */
    gpio_disable_pulls(gpio);
    gpio_set_function(gpio, GPIO_FUNC_XIP_CS1);
    gpio_disable_pulls(gpio);
}

const stpg_board_flash_profile_t *stpg_board_flash_profile(void) {
    return &g_flash_profile;
}
