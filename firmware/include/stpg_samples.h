#ifndef STPG_SAMPLES_H
#define STPG_SAMPLES_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "stpg_error.h"

typedef struct {
    uint32_t payload_offset;
    uint32_t payload_length;
    uint32_t payload_crc32;
    uint32_t element_count;
    uint32_t total_sample_count;
    uint32_t result_flags;
} stpg_sample_read_view_t;

void stpg_samples_reset(void);
stpg_err_t stpg_samples_prepare_for_run(uint32_t expected_sample_count, uint32_t active_pins);
uint32_t *stpg_samples_dma_buffer(void);
uint32_t stpg_samples_dma_capacity_words(void);
/* Zero keeps the existing linear DMA destination; otherwise wrap writes only. */
uint32_t stpg_samples_dma_ring_bits(void);
typedef uint32_t (*stpg_sample_frontier_fn)(void);
/* Core0 only: bounded CPU copy, with stable DMA frontiers before/after. */
stpg_err_t stpg_samples_service(stpg_sample_frontier_fn read_frontier);
/* Only after RX DMA/PIO are quiescent; require the full expected frontier. */
stpg_err_t stpg_samples_finish_staging(stpg_sample_frontier_fn read_frontier);
uint32_t stpg_samples_flushed_count(void);
/* Read-only, Core0-owned staging evidence. Pages contain six uint32 words.
 * 70: calls, committed words, total us, max us, max pending, enabled.
 * 71: first fault reason, phase, oldest, observed, expected, last frontier.
 * 72: committed frontier, last observed, snapshots, last us, ring, batch.
 * Service wall time includes preemption/contention, not just CPU cycles.
 * Reasons: 1=no reader, 2=regressed, 3=behind committed, 4=out of range,
 * 5=ring margin exceeded, 6=terminal count, 7=incomplete flush.
 * Phases: 1=before snapshot, 2=after snapshot, 3=after writes, 4=terminal.
 * The first fault remains sticky until RESET/prepare; reading never clears it.
 */
bool stpg_samples_staging_diagnostics(uint32_t page, uint32_t words[6]);
void stpg_samples_finish_run(uint32_t actual_sample_count, bool overflowed);
uint32_t stpg_samples_sample_count(void);
uint32_t stpg_samples_result_flags(void);
uint32_t stpg_samples_active_pins(void);
uint32_t stpg_samples_value_at(uint32_t sample_index);
stpg_err_t stpg_samples_prepare_read(uint32_t offset, uint32_t length, stpg_sample_read_view_t *out);
void stpg_samples_read_bytes(uint32_t offset, uint8_t *dst, size_t length);

#endif
