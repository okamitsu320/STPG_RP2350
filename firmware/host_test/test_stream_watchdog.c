#include <assert.h>

#include "stpg_protocol.h"
#include "stpg_stream_watchdog.h"

int main(void) {
    const uint32_t start = UINT32_MAX - 100000u; /* crosses timer wrap */
    assert(STPG_STREAM_STALL_TIMEOUT_US == 250000u);
    assert((STPG_CAPABILITY_CURRENT & CAP_STREAM_WAIT_WATCHDOG) != 0u);
    assert(stpg_stream_watchdog_budget_us(STPG_TICK_HZ / 1000u, 0u) == 250000u);
    assert(stpg_stream_watchdog_budget_us(3u * STPG_TICK_HZ, 0u) == 3250000u);
    assert(stpg_stream_watchdog_budget_us(9u * STPG_TICK_HZ, STPG_TICK_HZ) == 10250000u);
    assert(stpg_stream_watchdog_budget_us(UINT32_MAX, 1u) ==
           stpg_stream_watchdog_budget_us(UINT32_MAX, 0u));
    assert(stpg_stream_watchdog_budget_us(UINT32_MAX, 0u) < 30000000u);

    bool seen = false;
    const uint32_t budget = stpg_stream_watchdog_budget_us(3u * STPG_TICK_HZ, 0u);
    assert(!stpg_stream_watchdog_expired(start + 250000u, start, budget, true, true, &seen));
    assert(!seen);
    /* A real 3 s countdown is not a 250 ms stall, even across timer wrap. */
    assert(!stpg_stream_watchdog_expired(start + 250001u, start, budget, true, true, &seen));
    assert(seen);
    assert(!stpg_stream_watchdog_expired(start + 3000000u, start, budget, true, false, &seen));
    assert(!stpg_stream_watchdog_expired(start + budget, start, budget, true, true, &seen));
    /* Repeated observations of the timer loop cannot renew the deadline. */
    assert(stpg_stream_watchdog_expired(start + budget + 1u, start, budget, true, true, &seen));
    assert(stpg_stream_watchdog_expired(start + 3000000u, start, budget, false, true, &seen));

    seen = false;
    /* A FIFO/IRQ wait without timer evidence still faults at the old deadline. */
    assert(stpg_stream_watchdog_expired(250001u, 0u, budget, true, false, &seen));
    assert(!seen);
    assert(stpg_stream_watchdog_expired(250001u, 0u, budget, false, true, &seen));
    assert(!seen);
    assert(stpg_stream_watchdog_expired(250001u, 0u, 250000u, true, true, &seen));
    return 0;
}
