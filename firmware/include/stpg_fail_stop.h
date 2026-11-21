#ifndef STPG_FAIL_STOP_H
#define STPG_FAIL_STOP_H

#include <stdbool.h>
#include <stdint.h>

#include "stpg_common.h"
#include "stpg_error.h"

/*
 * Streaming fail-stop metadata is deliberately an ownership side channel for
 * the existing output slots.  It is not an alternative output-slot state
 * machine: Core 1 publishes, Core 0 consumes, and a slot cannot be refilled
 * while its metadata is still published.
 */
typedef enum {
    STPG_FAIL_STOP_METADATA_FREE = 0u,
    STPG_FAIL_STOP_METADATA_PUBLISHED = 1u,
    STPG_FAIL_STOP_METADATA_CONSUMED = 2u,
} stpg_fail_stop_metadata_state_t;

typedef struct {
    uint32_t generation;
    uint32_t sample_start;
    uint32_t sample_count;
    uint32_t consumed_count;
    stpg_fail_stop_metadata_state_t state;
    bool refill_deferred;
} stpg_fail_stop_metadata_t;

/* Zero means the legacy fail-stop threshold of one when enabled. */
stpg_err_t stpg_fail_stop_resolve_limit(bool enabled,
                                        uint32_t requested,
                                        uint32_t *out_limit);

void stpg_fail_stop_metadata_init(stpg_fail_stop_metadata_t *metadata,
                                  uint32_t generation);
bool stpg_fail_stop_metadata_reusable(const stpg_fail_stop_metadata_t *metadata);
stpg_err_t stpg_fail_stop_metadata_sample_cursor(
    const stpg_fail_stop_metadata_t *metadata,
    uint32_t *out_sample_cursor);
stpg_err_t stpg_fail_stop_metadata_publish(stpg_fail_stop_metadata_t *metadata,
                                           uint32_t generation,
                                           uint32_t sample_start,
                                           uint32_t sample_count);
stpg_err_t stpg_fail_stop_metadata_consume(stpg_fail_stop_metadata_t *metadata,
                                            uint32_t sample_count);
stpg_err_t stpg_fail_stop_metadata_defer_refill(stpg_fail_stop_metadata_t *metadata);
bool stpg_fail_stop_metadata_take_deferred_refill(stpg_fail_stop_metadata_t *metadata);

#endif
