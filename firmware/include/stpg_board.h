#ifndef STPG_BOARD_H
#define STPG_BOARD_H

#include <stdbool.h>
#include <stdint.h>

#define STPG_CLOCK_MODE_150MHZ 0
#define STPG_CLOCK_MODE_200MHZ 1

#ifndef STPG_CLOCK_MODE
#define STPG_CLOCK_MODE STPG_CLOCK_MODE_200MHZ
#endif

#if STPG_CLOCK_MODE != STPG_CLOCK_MODE_150MHZ && STPG_CLOCK_MODE != STPG_CLOCK_MODE_200MHZ
#error "STPG_CLOCK_MODE must be 0 (150MHz) or 1 (200MHz)"
#endif

#if STPG_CLOCK_MODE == STPG_CLOCK_MODE_200MHZ
#define STPG_TICK_HZ 200000000u
#define STPG_SYS_CLK_HZ 200000000u
#else
#define STPG_TICK_HZ 150000000u
#define STPG_SYS_CLK_HZ 150000000u
#endif

#define STPG_DUT_PIN_BASE 2u
#define STPG_DUT_PIN_COUNT 16u
#define STPG_DUT_PIN_LAST (STPG_DUT_PIN_BASE + STPG_DUT_PIN_COUNT - 1u)
#define STPG_UART_TX_PIN 0u
#define STPG_UART_RX_PIN 1u

#define STPG_DUT_WINDOW_BASE 2u
#define STPG_DUT_WINDOW_SPAN_16 16u
#define STPG_DUT_WINDOW_SPAN_24 27u
#define STPG_PACKED27_GAP_MASK ((1u << 17) | (1u << 22) | (1u << 23))

uint32_t stpg_board_gpio_for_dut_pin(uint32_t logical_bit);
bool stpg_board_gpio_is_dut_pin(uint32_t gpio, uint32_t active_pins);
uint32_t stpg_board_out_pin_base(uint32_t active_pins);
uint32_t stpg_board_out_pin_span(uint32_t active_pins);
uint32_t stpg_board_out_gap_mask(uint32_t active_pins);
uint32_t stpg_board_pack_logical_to_window(uint32_t logical_bits, uint32_t active_pins);
uint32_t stpg_board_unpack_window_to_logical(uint32_t packed_bits, uint32_t active_pins);

#endif
