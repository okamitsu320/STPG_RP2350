#ifndef STPG_PATTERN_VALIDATE_H
#define STPG_PATTERN_VALIDATE_H

#include <stddef.h>
#include <stdint.h>

#include "stpg_error.h"
#include "stpg_pattern.h"

typedef struct {
    stpg_pattern_format_t format;
    uint32_t pattern_size;
    uint32_t entry_count;
    uint32_t aux_count;
} stpg_pattern_summary_t;

stpg_err_t stpg_validate_pattern(const uint8_t *buf, size_t size, uint32_t expected_total_crc);
stpg_err_t stpg_validate_rate_vector(const uint8_t *buf, size_t size, uint32_t expected_total_crc,
                                      RateVectorHeader *out_header);
stpg_err_t stpg_validate_macro_vector(const uint8_t *buf, size_t size, uint32_t expected_total_crc,
                                      MacroVectorHeader *out_header);
stpg_err_t stpg_validate_macro_p2_vector(const uint8_t *buf, size_t size, uint32_t expected_total_crc,
                                         MacroVectorHeader *out_header);
stpg_err_t stpg_pattern_summary_from_buffer(const uint8_t *buf, size_t size, stpg_pattern_summary_t *out_summary);
stpg_err_t stpg_expand_raw_events_to_pio_words(const uint8_t *buf, size_t size,
                                                uint32_t *pio_words,
                                                uint32_t max_word_count,
                                                uint32_t *out_word_count,
                                                uint32_t *out_sample_event_count);

#endif
