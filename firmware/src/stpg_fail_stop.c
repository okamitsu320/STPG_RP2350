#include "stpg_fail_stop.h"

stpg_err_t stpg_fail_stop_resolve_limit(bool enabled,
                                        uint32_t requested,
                                        uint32_t *out_limit) {
    if (out_limit == 0) {
        return ERR_BAD_SIZE;
    }
    if (!enabled) {
        if (requested != 0u) {
            return ERR_UNSUPPORTED;
        }
        *out_limit = 0u;
        return ERR_NONE;
    }
    if (requested > STPG_FAIL_LOG_MAX) {
        return ERR_BAD_SIZE;
    }
    *out_limit = requested == 0u ? 1u : requested;
    return ERR_NONE;
}

void stpg_fail_stop_metadata_init(stpg_fail_stop_metadata_t *metadata,
                                  uint32_t generation) {
    if (metadata == 0) {
        return;
    }
    metadata->generation = generation;
    metadata->sample_start = 0u;
    metadata->sample_count = 0u;
    metadata->consumed_count = 0u;
    metadata->state = STPG_FAIL_STOP_METADATA_FREE;
    metadata->refill_deferred = false;
}

bool stpg_fail_stop_metadata_reusable(const stpg_fail_stop_metadata_t *metadata) {
    if (metadata == 0 || metadata->refill_deferred) {
        return false;
    }
    return metadata->state == STPG_FAIL_STOP_METADATA_FREE
        || metadata->state == STPG_FAIL_STOP_METADATA_CONSUMED;
}

stpg_err_t stpg_fail_stop_metadata_sample_cursor(
    const stpg_fail_stop_metadata_t *metadata,
    uint32_t *out_sample_cursor) {
    if (metadata == 0 || out_sample_cursor == 0) {
        return ERR_BAD_SIZE;
    }
    if (metadata->consumed_count > metadata->sample_count
        || metadata->sample_start > STPG_MAX_EVENT_COUNT
        || metadata->consumed_count > STPG_MAX_EVENT_COUNT - metadata->sample_start) {
        return ERR_FAIL_STOP_METADATA_OVERRUN;
    }
    *out_sample_cursor = metadata->sample_start + metadata->consumed_count;
    return ERR_NONE;
}

stpg_err_t stpg_fail_stop_metadata_publish(stpg_fail_stop_metadata_t *metadata,
                                           uint32_t generation,
                                           uint32_t sample_start,
                                           uint32_t sample_count) {
    if (metadata == 0) {
        return ERR_BAD_SIZE;
    }
    if (!stpg_fail_stop_metadata_reusable(metadata)) {
        return ERR_FAIL_STOP_METADATA_OVERRUN;
    }
    if (sample_count > STPG_STREAM_BUFFER_EVENTS
        || sample_start > STPG_MAX_EVENT_COUNT
        || sample_count > STPG_MAX_EVENT_COUNT - sample_start) {
        return ERR_FAIL_STOP_METADATA_OVERRUN;
    }
    metadata->generation = generation;
    metadata->sample_start = sample_start;
    metadata->sample_count = sample_count;
    metadata->consumed_count = 0u;
    metadata->refill_deferred = false;
    metadata->state = STPG_FAIL_STOP_METADATA_PUBLISHED;
    return ERR_NONE;
}

stpg_err_t stpg_fail_stop_metadata_consume(stpg_fail_stop_metadata_t *metadata,
                                            uint32_t sample_count) {
    if (metadata == 0 || metadata->state != STPG_FAIL_STOP_METADATA_PUBLISHED) {
        return ERR_FAIL_STOP_METADATA_OVERRUN;
    }
    if (metadata->consumed_count > metadata->sample_count) {
        return ERR_FAIL_STOP_METADATA_OVERRUN;
    }
    if (sample_count > metadata->sample_count - metadata->consumed_count) {
        return ERR_FAIL_STOP_METADATA_OVERRUN;
    }
    metadata->consumed_count += sample_count;
    if (metadata->consumed_count == metadata->sample_count) {
        metadata->state = STPG_FAIL_STOP_METADATA_CONSUMED;
    }
    return ERR_NONE;
}

stpg_err_t stpg_fail_stop_metadata_defer_refill(stpg_fail_stop_metadata_t *metadata) {
    if (metadata == 0 || metadata->state != STPG_FAIL_STOP_METADATA_PUBLISHED
        || metadata->refill_deferred) {
        return ERR_FAIL_STOP_METADATA_OVERRUN;
    }
    metadata->refill_deferred = true;
    return ERR_NONE;
}

bool stpg_fail_stop_metadata_take_deferred_refill(stpg_fail_stop_metadata_t *metadata) {
    if (metadata == 0 || metadata->state != STPG_FAIL_STOP_METADATA_CONSUMED
        || !metadata->refill_deferred) {
        return false;
    }
    metadata->refill_deferred = false;
    return true;
}
