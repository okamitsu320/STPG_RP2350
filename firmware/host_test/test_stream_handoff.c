#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "stpg_stream_handoff.h"

static int expect_state(
    bool busy,
    uint32_t remaining,
    stpg_stream_chained_dma_state_t expected,
    const char *label
) {
    const stpg_stream_chained_dma_state_t actual =
        stpg_stream_classify_chained_dma(busy, remaining);
    if (actual != expected) {
        fprintf(stderr, "%s: expected %u, got %u\n",
                label, (unsigned)expected, (unsigned)actual);
        return 1;
    }
    return 0;
}

int main(void) {
    if (expect_state(true, 32u, STPG_STREAM_CHAINED_DMA_IN_FLIGHT,
                     "ordinary chained successor")) return 1;
    if (expect_state(false, 0u, STPG_STREAM_CHAINED_DMA_COMPLETE,
                     "short successor completed before predecessor IRQ")) return 1;
    if (expect_state(true, 0u, STPG_STREAM_CHAINED_DMA_INVALID,
                     "busy with no remaining transfer")) return 1;
    if (expect_state(false, 32u, STPG_STREAM_CHAINED_DMA_INVALID,
                     "idle successor not triggered")) return 1;
    return 0;
}
