#ifndef STPG_STREAM_HANDOFF_H
#define STPG_STREAM_HANDOFF_H

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    STPG_STREAM_CHAINED_DMA_INVALID = 0,
    STPG_STREAM_CHAINED_DMA_IN_FLIGHT = 1,
    STPG_STREAM_CHAINED_DMA_COMPLETE = 2,
} stpg_stream_chained_dma_state_t;

/*
 * A chained successor may be very short.  By the time the predecessor's IRQ
 * is serviced, the successor can already have consumed its entire transfer.
 * BUSY is therefore not, by itself, an admission requirement for the
 * successor.  The two coherent observations are an active transfer with a
 * nonzero remaining count, or a completed transfer with a zero count.
 */
static inline stpg_stream_chained_dma_state_t stpg_stream_classify_chained_dma(
    bool busy,
    uint32_t remaining
) {
    if (busy && remaining != 0u) {
        return STPG_STREAM_CHAINED_DMA_IN_FLIGHT;
    }
    if (!busy && remaining == 0u) {
        return STPG_STREAM_CHAINED_DMA_COMPLETE;
    }
    return STPG_STREAM_CHAINED_DMA_INVALID;
}

#endif
