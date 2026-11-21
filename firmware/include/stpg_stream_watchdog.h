#ifndef STPG_STREAM_WATCHDOG_H
#define STPG_STREAM_WATCHDOG_H

#include <stdbool.h>
#include <stdint.h>

#include "stpg_board.h"

#define STPG_STREAM_STALL_TIMEOUT_US 250000u

/*
 * Rate Vector events use wait words of at most UINT32_MAX ticks. The
 * active rate table bounds both the intra-period gaps and the previous
 * period remainder + next first timestamp. Clocks are 150/200 MHz.
 */
static inline uint32_t stpg_stream_watchdog_budget_us(uint32_t max_period,
                                                     uint32_t max_first_time) {
    const uint32_t max_ticks = max_period > UINT32_MAX - max_first_time
        ? UINT32_MAX : max_period + max_first_time;
    const uint32_t ticks_per_us = STPG_TICK_HZ / 1000000u;
    const uint32_t wait_us = max_ticks / ticks_per_us + (max_ticks % ticks_per_us != 0u);
    return wait_us < STPG_STREAM_STALL_TIMEOUT_US
        ? STPG_STREAM_STALL_TIMEOUT_US : wait_us + STPG_STREAM_STALL_TIMEOUT_US;
}

/*
 * Called only for the same progress epoch (DMA count, slot, queued events).
 * Observing a real countdown grants one bounded allowance, never a new
 * start time. Latch the observation through the short interval between
 * countdown exit and the next DMA beat. Normal stalls and timer wrap
 * remain bounded; progress_at_us == 0 is a valid timestamp.
 */
static inline bool stpg_stream_watchdog_expired(uint32_t now_us, uint32_t progress_at_us,
                                               uint32_t timed_budget_us, bool dma_in_flight,
                                               bool in_timer_loop, bool *timed_wait_seen) {
    const uint32_t elapsed_us = now_us - progress_at_us;
    if (elapsed_us <= STPG_STREAM_STALL_TIMEOUT_US) {
        return false;
    }
    if (dma_in_flight && in_timer_loop) {
        *timed_wait_seen = true;
    }
    return !dma_in_flight || !*timed_wait_seen || elapsed_us > timed_budget_us;
}

#endif
