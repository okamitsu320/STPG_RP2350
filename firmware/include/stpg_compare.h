#ifndef STPG_COMPARE_H
#define STPG_COMPARE_H

#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>

#include "stpg_error.h"
#include "stpg_pattern.h"

typedef struct stpg_rate_stream_state stpg_rate_stream_state_t;

typedef struct {
    uint32_t payload_offset_entries;
    uint32_t payload_length_bytes;
    uint32_t payload_crc32;
    uint32_t element_count;
    uint32_t total_fail_count;
    uint32_t result_flags;
} stpg_fail_log_read_view_t;

void stpg_compare_reset(void);
/* Compare one already-captured logical sample. expected and mask must already
 * be restricted to the active logical pins. */
bool stpg_compare_check_value(uint32_t event_index,
                              uint32_t actual,
                              uint32_t expected,
                              uint32_t mask);
void stpg_compare_run(const ExecEvent *events, uint32_t event_count);
void stpg_compare_run_streamed(const ExecEvent *events, const uint32_t *event_indices, uint32_t event_count);
/* Reuses the existing stream state and bounded metadata scratch. Only the
 * owning main-loop task may advance it, after output/RX are quiescent. */
typedef struct {
    stpg_rate_stream_state_t *state;
    uint32_t event_count;
    uint32_t sample_index;
    uint32_t compare_count;
    uint32_t sample_count;
    uint32_t logical_mask;
    bool active;
} stpg_compare_replay_t;
stpg_err_t stpg_compare_replay_begin(stpg_compare_replay_t *replay,
                                    stpg_rate_stream_state_t *state,
                                    uint32_t initial_value,
                                    uint32_t initial_dir);
/* At most one STPG_STREAM_BUFFER_EVENTS fill. done is true only after exact
 * EOF/event/sample validation. An error seals this replay until a new begin. */
stpg_err_t stpg_compare_replay_step(stpg_compare_replay_t *replay, bool *done);
stpg_err_t stpg_compare_run_stream_replayed(stpg_rate_stream_state_t *state,
                                            uint32_t initial_value,
                                            uint32_t initial_dir,
                                            uint32_t *out_compare_count);
uint32_t stpg_compare_fail_count(void);
uint32_t stpg_compare_result_flags(void);
stpg_err_t stpg_compare_prepare_read(uint32_t offset_entries, uint32_t count, stpg_fail_log_read_view_t *out);
void stpg_compare_read_bytes(uint32_t offset_bytes, uint8_t *dst, size_t length);

#endif
