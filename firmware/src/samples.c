#include "stpg_samples.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_board.h"
#include "stpg_psram.h"
#include "stpg_protocol.h"

#ifndef STPG_STREAM_PSRAM_COMPARE_SAMPLE
#define STPG_STREAM_PSRAM_COMPARE_SAMPLE 0
#endif

#if STPG_SAMPLE_PSRAM_SRAM_STAGING && !STPG_STREAM_PSRAM_COMPARE_SAMPLE
#error "sample SRAM staging requires PSRAM sample storage"
#endif
#if STPG_SAMPLE_PSRAM_SRAM_STAGING && defined(PICO_ON_DEVICE)
#include "pico.h"
#include "hardware/sync.h"
#include "pico/time.h"
#define STPG_SAMPLE_SERVICE_FUNC(name) __no_inline_not_in_flash_func(name)
#define STPG_SAMPLE_BARRIER() __dsb()
#define STPG_SAMPLE_TIME_US() time_us_32()
#else
#define STPG_SAMPLE_SERVICE_FUNC(name) name
#define STPG_SAMPLE_BARRIER() ((void)0)
#define STPG_SAMPLE_TIME_US() 0u
#endif

enum {
    STPG_SAMPLE_STAGE_RING_BITS = 13u,
    STPG_SAMPLE_STAGE_WORDS = 2048u,
    STPG_SAMPLE_STAGE_GUARD_WORDS = 16u,
    STPG_SAMPLE_STAGE_BATCH_WORDS = 64u,
};

static uint32_t g_sample_sram_buf[STPG_MAX_EVENT_COUNT];
static uint32_t *g_sample_buf = g_sample_sram_buf;
static uint32_t g_expected_sample_count;
static uint32_t g_sample_count;
static uint32_t g_result_flags;
static uint32_t g_active_pins;
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
static uint32_t *g_sample_stage;
static uint32_t g_sample_flushed_count;
static uint32_t g_sample_last_frontier;
static bool g_sample_stage_failed;
static struct {
    uint32_t calls, words, total_us, max_us, max_pending;
    uint32_t fault_reason, fault_phase, fault_oldest, fault_observed;
    uint32_t fault_expected, fault_last_frontier, snapshots, last_us;
    uint32_t service_end_us, gap_count, gap_total_us, gap_max_us, gap_last_us;
    uint32_t gap_frontier, gap_oldest;
} g_stage_diag;
_Static_assert(STPG_MAX_EVENT_COUNT >= 2u * STPG_SAMPLE_STAGE_WORDS - 1u + STPG_SAMPLE_STAGE_BATCH_WORDS,
               "existing sample SRAM must contain a disjoint aligned ring and snapshot");
#endif

void stpg_samples_reset(void) {
    g_expected_sample_count = 0u;
    g_sample_count = 0u;
    g_result_flags = 0u;
    g_active_pins = STPG_ACTIVE_PINS_V01;
    g_sample_buf = g_sample_sram_buf;
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    g_sample_stage = 0;
    g_sample_flushed_count = 0u;
    g_sample_last_frontier = 0u;
    g_sample_stage_failed = false;
    memset(&g_stage_diag, 0, sizeof(g_stage_diag));
#endif
}

stpg_err_t stpg_samples_prepare_for_run(uint32_t expected_sample_count, uint32_t active_pins) {
    stpg_samples_reset();
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
    if (expected_sample_count > STPG_MAX_EVENT_COUNT) {
        const uint64_t sample_bytes = (uint64_t)expected_sample_count * sizeof(uint32_t);
        if (sample_bytes > STPG_PSRAM_RESULT_BYTES) {
            return ERR_BAD_SIZE;
        }
        if (stpg_psram_prepare_access() != ERR_NONE) {
            return ERR_PSRAM_UNAVAILABLE;
        }
        g_sample_buf = (uint32_t *)stpg_psram_mapped_ptr(
            STPG_PSRAM_RESULT_BASE, (uint32_t)sample_bytes);
        if (g_sample_buf == 0) {
            return ERR_PSRAM_BOUNDS;
        }
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
        /* Reuse the existing array; no extra 8KiB alignment padding or new
         * sample array is reserved by the linker. The static size proof above
         * covers the worst possible alignment offset within this allocation. */
        const uintptr_t mask = STPG_SAMPLE_STAGE_WORDS * sizeof(uint32_t) - 1u;
        g_sample_stage = (uint32_t *)(((uintptr_t)g_sample_sram_buf + mask) & ~mask);
#endif
    }
#else
    if (expected_sample_count > STPG_MAX_EVENT_COUNT) {
        return ERR_BAD_SIZE;
    }
#endif
    g_expected_sample_count = expected_sample_count;
    g_sample_count = 0u;
    g_result_flags = 0u;
    g_active_pins = active_pins;
    return ERR_NONE;
}

uint32_t *stpg_samples_dma_buffer(void) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    if (g_sample_stage != 0) {
        return g_sample_stage;
    }
#endif
    return g_sample_buf;
}

uint32_t stpg_samples_dma_ring_bits(void) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    return g_sample_stage != 0 ? STPG_SAMPLE_STAGE_RING_BITS : 0u;
#else
    return 0u;
#endif
}

uint32_t stpg_samples_flushed_count(void) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    return g_sample_flushed_count;
#else
    return 0u;
#endif
}

#if STPG_SAMPLE_PSRAM_SRAM_STAGING
static stpg_err_t stpg_samples_stage_reject(uint32_t reason, uint32_t phase,
                                          uint32_t observed, uint32_t oldest) {
    if (!g_sample_stage_failed) {
        g_stage_diag.fault_reason = reason;
        g_stage_diag.fault_phase = phase;
        g_stage_diag.fault_oldest = oldest;
        g_stage_diag.fault_observed = observed;
        g_stage_diag.fault_expected = g_expected_sample_count;
        g_stage_diag.fault_last_frontier = g_sample_last_frontier;
    }
    g_sample_stage_failed = true;
    g_result_flags |= STPG_RESULT_FLAG_SAMPLE_OVERFLOW;
    return ERR_DMA_ERROR;
}

static uint32_t stpg_samples_frontier_fault(uint32_t frontier, uint32_t oldest) {
    if (frontier < g_sample_last_frontier) return 2u;
    if (frontier < oldest) return 3u;
    if (frontier > g_expected_sample_count) return 4u;
    const uint32_t pending = frontier - oldest;
    if (pending > g_stage_diag.max_pending) g_stage_diag.max_pending = pending;
    return pending > STPG_SAMPLE_STAGE_WORDS - STPG_SAMPLE_STAGE_GUARD_WORDS ? 5u : 0u;
}

/* Keep every active CPU store off FLASH/XIP. Four sequential stores remove
 * the ring-index/load/branch gaps between destination words. Do not use DMA
 * here: its shared write manager is the cause of the direct-route concern.
 * IRQs remain enabled. STM writes are idempotent if an exception restarts one.
 */
static inline void stpg_samples_store_snapshot(volatile uint32_t *destination,
                                             const uint32_t *source, uint32_t count) {
#if defined(__GNUC__)
#pragma GCC unroll 1
#endif
    while (count >= 4u) {
#if defined(PICO_ON_DEVICE) && defined(__arm__)
        __asm volatile("ldmia %1!, {r2, r3, r4, r5}\n"
                       "stmia %0!, {r2, r3, r4, r5}\n"
                       : "+&r"(destination), "+&r"(source)
                       : : "r2", "r3", "r4", "r5", "memory");
#else
        const uint32_t a = source[0], b = source[1], c = source[2], d = source[3];
        destination[0] = a; destination[1] = b;
        destination[2] = c; destination[3] = d;
        destination += 4u;
        source += 4u;
#endif
        count -= 4u;
    }
#if defined(__GNUC__)
#pragma GCC unroll 1
#endif
    while (count-- != 0u) *destination++ = *source++;
}

static void stpg_samples_note_service_time(uint32_t started) {
    const uint32_t ended = STPG_SAMPLE_TIME_US();
    const uint32_t elapsed = ended - started;
    g_stage_diag.service_end_us = ended;
    g_stage_diag.last_us = elapsed;
    g_stage_diag.total_us += elapsed;
    if (elapsed > g_stage_diag.max_us) g_stage_diag.max_us = elapsed;
}
#endif

stpg_err_t STPG_SAMPLE_SERVICE_FUNC(stpg_samples_service)(stpg_sample_frontier_fn read_frontier) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    if (g_sample_stage == 0) {
        return ERR_NONE;
    }
    if (g_sample_stage_failed) return ERR_DMA_ERROR;
    if (read_frontier == 0) {
        return stpg_samples_stage_reject(1u, 1u, 0u, g_sample_flushed_count);
    }
    const uint32_t started = STPG_SAMPLE_TIME_US();
    const uint32_t gap = started - g_stage_diag.service_end_us;
    const bool has_gap = g_stage_diag.calls != 0u;
    ++g_stage_diag.calls;
    const uint32_t oldest = g_sample_flushed_count;
    const uint32_t before = read_frontier();
    if (has_gap) {
        ++g_stage_diag.gap_count;
        g_stage_diag.gap_total_us += gap;
        g_stage_diag.gap_last_us = gap;
        if (gap > g_stage_diag.gap_max_us) {
            g_stage_diag.gap_max_us = gap;
            g_stage_diag.gap_frontier = before;
            g_stage_diag.gap_oldest = oldest;
        }
    }
    STPG_SAMPLE_BARRIER();
    uint32_t fault = stpg_samples_frontier_fault(before, oldest);
    if (fault != 0u) {
        stpg_samples_note_service_time(started);
        return stpg_samples_stage_reject(fault, 1u, before, oldest);
    }
    g_sample_last_frontier = before;
    uint32_t count = before - oldest;
    if (count > STPG_SAMPLE_STAGE_BATCH_WORDS) {
        count = STPG_SAMPLE_STAGE_BATCH_WORDS;
    }
    if (count == 0u) {
        stpg_samples_note_service_time(started);
        return ERR_NONE;
    }
    /* The static proof includes worst-case ring alignment. The tail is
     * outside the DMA ring and is unused by large-sample linear storage.
     * No additional array, link alignment padding, or 256-byte stack buffer.
     */
    uint32_t *const snapshot = g_sample_sram_buf + STPG_MAX_EVENT_COUNT - STPG_SAMPLE_STAGE_BATCH_WORDS;
    const volatile uint32_t *const source = g_sample_stage;
    for (uint32_t index = 0u; index < count; ++index) {
        snapshot[index] = source[(oldest + index) & (STPG_SAMPLE_STAGE_WORDS - 1u)];
    }
    STPG_SAMPLE_BARRIER();
    const uint32_t captured = read_frontier();
    fault = stpg_samples_frontier_fault(captured, oldest);
    if (fault != 0u) {
        stpg_samples_note_service_time(started);
        return stpg_samples_stage_reject(fault, 2u, captured, oldest);
    }
    g_sample_last_frontier = captured;
    ++g_stage_diag.snapshots;
    /* CPU writes can wait for QMI without occupying the DMA write manager.
     * Do not call the verified/retrying upload writer on this hot path. */
    stpg_samples_store_snapshot(g_sample_buf + oldest, snapshot, count);
    STPG_SAMPLE_BARRIER();
    const uint32_t after = read_frontier();
    const uint32_t committed = oldest + count;
    fault = stpg_samples_frontier_fault(after, committed);
    if (fault != 0u) {
        stpg_samples_note_service_time(started);
        return stpg_samples_stage_reject(fault, 3u, after, committed);
    }
    g_sample_last_frontier = after;
    /* The original prefix is safe in the disjoint snapshot during the slow
     * writes. The post-write guard protects ONLY the unread suffix. */
    g_sample_flushed_count = committed;
    g_stage_diag.words += count;
    stpg_samples_note_service_time(started);
#else
    (void)read_frontier;
#endif
    return ERR_NONE;
}

stpg_err_t stpg_samples_finish_staging(stpg_sample_frontier_fn read_frontier) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    if (g_sample_stage == 0) {
        return ERR_NONE;
    }
    if (g_sample_stage_failed) return ERR_DMA_ERROR;
    const uint32_t terminal = read_frontier != 0 ? read_frontier() : UINT32_MAX;
    if (read_frontier == 0 || terminal != g_expected_sample_count) {
        return stpg_samples_stage_reject(6u, 4u, terminal, g_sample_flushed_count);
    }
    /* RX is stopped. At most one ring remains; never wait for more samples. */
    for (uint32_t batch = 0u; batch < STPG_SAMPLE_STAGE_WORDS / STPG_SAMPLE_STAGE_BATCH_WORDS; ++batch) {
        const stpg_err_t err = stpg_samples_service(read_frontier);
        if (err != ERR_NONE) {
            return err;
        }
        if (g_sample_flushed_count == g_expected_sample_count) {
            return ERR_NONE;
        }
    }
    return stpg_samples_stage_reject(7u, 4u, g_sample_last_frontier, g_sample_flushed_count);
#else
    (void)read_frontier;
    return ERR_NONE;
#endif
}

bool stpg_samples_staging_diagnostics(uint32_t page, uint32_t words[6]) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    if (words == 0) return false;
    switch (page) {
    case 70u:
        words[0] = g_stage_diag.calls; words[1] = g_stage_diag.words;
        words[2] = g_stage_diag.total_us; words[3] = g_stage_diag.max_us;
        words[4] = g_stage_diag.max_pending; words[5] = g_sample_stage != 0;
        return true;
    case 71u:
        words[0] = g_stage_diag.fault_reason; words[1] = g_stage_diag.fault_phase;
        words[2] = g_stage_diag.fault_oldest; words[3] = g_stage_diag.fault_observed;
        words[4] = g_stage_diag.fault_expected; words[5] = g_stage_diag.fault_last_frontier;
        return true;
    case 72u:
        words[0] = g_sample_flushed_count; words[1] = g_sample_last_frontier;
        words[2] = g_stage_diag.snapshots; words[3] = g_stage_diag.last_us;
        words[4] = STPG_SAMPLE_STAGE_WORDS; words[5] = STPG_SAMPLE_STAGE_BATCH_WORDS;
        return true;
    case 73u:
        words[0] = g_stage_diag.gap_count; words[1] = g_stage_diag.gap_total_us;
        words[2] = g_stage_diag.gap_max_us; words[3] = g_stage_diag.gap_last_us;
        words[4] = g_stage_diag.gap_frontier; words[5] = g_stage_diag.gap_oldest;
        return true;
    default: return false;
    }
#else
    (void)page; (void)words;
    return false;
#endif
}

uint32_t stpg_samples_dma_capacity_words(void) {
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
    return STPG_PSRAM_RESULT_BYTES / sizeof(uint32_t);
#else
    return STPG_MAX_EVENT_COUNT;
#endif
}

void stpg_samples_finish_run(uint32_t actual_sample_count, bool overflowed) {
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    if (g_sample_stage != 0 && actual_sample_count > g_sample_flushed_count) {
        actual_sample_count = g_sample_flushed_count;
        overflowed = true;
    }
#endif
    g_sample_count = actual_sample_count;
    g_result_flags |= overflowed ? STPG_RESULT_FLAG_SAMPLE_OVERFLOW : 0u;
}

uint32_t stpg_samples_sample_count(void) {
    return g_sample_count;
}

uint32_t stpg_samples_result_flags(void) {
    return g_result_flags;
}

uint32_t stpg_samples_active_pins(void) {
    return g_active_pins;
}

uint32_t stpg_samples_value_at(uint32_t sample_index) {
    if (g_active_pins == STPG_ACTIVE_PINS_V11) {
        return stpg_board_unpack_window_to_logical(g_sample_buf[sample_index], g_active_pins) & 0x00FFFFFFu;
    }
    return g_sample_buf[sample_index] & 0x0000FFFFu;
}

static uint32_t stpg_samples_element_size_bytes(void) {
    return g_active_pins == STPG_ACTIVE_PINS_V11 ? 4u : 2u;
}

static uint8_t stpg_samples_read_byte(uint32_t byte_offset) {
    const uint32_t elem_size = stpg_samples_element_size_bytes();
    const uint32_t sample_index = byte_offset / elem_size;
    const uint32_t value = stpg_samples_value_at(sample_index);
    const uint32_t byte_index = byte_offset % elem_size;
    return (uint8_t)((value >> (byte_index * 8u)) & 0xFFu);
}

stpg_err_t stpg_samples_prepare_read(uint32_t offset, uint32_t length, stpg_sample_read_view_t *out) {
    if (out == 0) {
        return ERR_BAD_SIZE;
    }

    const uint32_t element_size = stpg_samples_element_size_bytes();
    const uint32_t total_size = g_sample_count * element_size;
    if (offset > total_size || length > total_size - offset) {
        return ERR_BAD_SIZE;
    }

    uint32_t crc = 0u;
    if (length > 0u) {
        uint8_t scratch[64];
        uint32_t remaining = length;
        uint32_t cursor = offset;
        while (remaining > 0u) {
            const uint32_t chunk = remaining > sizeof(scratch) ? (uint32_t)sizeof(scratch) : remaining;
            for (uint32_t i = 0; i < chunk; ++i) {
                scratch[i] = stpg_samples_read_byte(cursor + i);
            }
            crc = stpg_crc32_continue(crc, scratch, chunk);
            cursor += chunk;
            remaining -= chunk;
        }
    }

    out->payload_offset = offset;
    out->payload_length = length;
    out->payload_crc32 = crc;
    out->element_count = length / element_size;
    out->total_sample_count = g_sample_count;
    out->result_flags = g_result_flags;
    return ERR_NONE;
}

void stpg_samples_read_bytes(uint32_t offset, uint8_t *dst, size_t length) {
    if (dst == 0 || length == 0u) {
        return;
    }
    for (size_t i = 0; i < length; ++i) {
        dst[i] = stpg_samples_read_byte(offset + (uint32_t)i);
    }
}
