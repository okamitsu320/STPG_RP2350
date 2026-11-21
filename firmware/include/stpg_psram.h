#ifndef STPG_PSRAM_H
#define STPG_PSRAM_H

#include <stdbool.h>
#include <stdint.h>

#include "stpg_common.h"
#include "stpg_error.h"

typedef struct {
    uint32_t payload_offset;
    uint32_t payload_length;
    uint32_t payload_crc32;
} stpg_psram_read_view_t;

typedef struct {
    uint32_t offset;
    uint32_t size;
    uint32_t attempt;
    uint32_t mismatch_index;
    uint8_t expected;
    uint8_t actual;
    bool valid;
} stpg_psram_write_diag_t;

void stpg_psram_init(void);
bool stpg_psram_is_available(void);
/* Re-apply and verify the QMI M1 PSRAM access contract before a data transfer. */
stpg_err_t stpg_psram_prepare_access(void);
/* Report the CS1 pin selected by the successful initialization self-test. */
bool stpg_psram_get_selected_cs1_gpio(uint32_t *out_gpio);
/* Read the selected pin's live mux value, or UINT32_MAX when unavailable. */
uint32_t stpg_psram_get_selected_cs1_function(void);
/* Verify that the selected pin still routes the QMI/XIP CS1 function. */
bool stpg_psram_selected_cs1_mux_matches(void);
void *stpg_psram_mapped_ptr(uint32_t offset, uint32_t size);
stpg_err_t stpg_psram_read(uint32_t offset, void *dst, uint32_t size);
stpg_err_t stpg_psram_write(uint32_t offset, const void *src, uint32_t size);
stpg_err_t stpg_psram_fill(uint32_t offset, uint8_t value, uint32_t size);
stpg_err_t stpg_psram_checksum(uint32_t offset, uint32_t size, uint32_t *out_crc32);
void stpg_psram_clear_write_diag(void);
stpg_psram_write_diag_t stpg_psram_get_write_diag(void);

#ifndef PICO_ON_DEVICE
typedef struct {
    uint32_t read_calls;
    uint32_t checksum_calls;
} stpg_psram_host_read_stats_t;

void stpg_psram_host_reset_read_stats(void);
stpg_psram_host_read_stats_t stpg_psram_host_get_read_stats(void);
#endif

#endif
