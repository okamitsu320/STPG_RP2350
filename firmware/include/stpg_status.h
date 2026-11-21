#ifndef STPG_STATUS_H
#define STPG_STATUS_H

#include <stdint.h>

#include "stpg_error.h"
#include "stpg_protocol.h"
#include "stpg_state.h"

typedef struct {
    stpg_state_t state;
    stpg_err_t error_code;
    stpg_err_t last_boot_error;
    uint32_t last_boot_slot;
    uint32_t pattern_size;
    uint32_t event_count;
    uint32_t run_progress;
    uint32_t result_flags;
    uint32_t fail_count;
    uint32_t sample_count;
} stpg_status_context_t;

void stpg_status_init(void);
void stpg_status_set_state(stpg_state_t state);
stpg_state_t stpg_status_get_state(void);
void stpg_status_set_error(stpg_err_t error_code);
void stpg_status_clear_error(void);
void stpg_status_set_boot_error(stpg_err_t error_code, uint32_t slot_id);
void stpg_status_clear_boot_error(void);
void stpg_status_reset_pattern_info(void);
void stpg_status_set_pattern_info(uint32_t pattern_size, uint32_t event_count);
void stpg_status_set_run_progress(uint32_t run_progress);
void stpg_status_reset_result_info(void);
void stpg_status_set_result_flags(uint32_t result_flags);
void stpg_status_set_fail_count(uint32_t fail_count);
void stpg_status_set_sample_count(uint32_t sample_count);
void stpg_status_refresh_crc(StpgStatusResponse *out);
void stpg_status_build(uint16_t rsp, uint32_t seq, StpgStatusResponse *out);
void stpg_status_build_clock_info(uint32_t seq, StpgStatusResponse *out);

#endif
