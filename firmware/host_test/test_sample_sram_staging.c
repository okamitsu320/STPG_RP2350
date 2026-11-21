#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "stpg_board.h"
#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_protocol.h"
#include "stpg_psram.h"
#include "stpg_samples.h"

static uint32_t produced, pins, calls, after_extra, after_override, store_extra;
static bool override_enabled;

static uint32_t logical_value(uint32_t index) {
    return ((index * 1664525u + 1013904223u) ^ (index >> 3u))
        & (pins == 24u ? 0xffffffu : 0xffffu);
}

static void produce(uint32_t count) {
    uint32_t *const buffer = stpg_samples_dma_buffer();
    const uint32_t mask = stpg_samples_dma_ring_bits() ? 2047u : UINT32_MAX;
    for (uint32_t i = 0u; i < count; ++i) {
        const uint32_t value = logical_value(produced);
        buffer[produced & mask] = pins == 24u
            ? stpg_board_pack_logical_to_window(value, pins) : value;
        ++produced;
    }
}

/* Injected into the actual-C CPU-store helper by the host test only. No
 * hardware/test hook is compiled into firmware. Simulates RX continuing
 * while destination writes wait for QMI. */
void stpg_samples_model_destination_delay(void) {
    if (store_extra != 0u) {
        const uint32_t count = store_extra;
        store_extra = 0u;
        produce(count);
    }
}

static uint32_t frontier(void) {
    ++calls;
    if (calls == 2u) {
        if (after_extra) produce(after_extra);
        if (override_enabled) return after_override;
    }
    return produced;
}

static void prepare(uint32_t count, uint32_t active_pins) {
    pins = active_pins;
    produced = calls = after_extra = after_override = store_extra = 0u;
    override_enabled = false;
    assert(stpg_samples_prepare_for_run(count, pins) == ERR_NONE);
    assert(stpg_samples_result_flags() == 0u);
    uint32_t words[6] = {0};
    assert(stpg_samples_staging_diagnostics(70u, words) == (bool)STPG_SAMPLE_PSRAM_SRAM_STAGING);
    if (STPG_SAMPLE_PSRAM_SRAM_STAGING) {
        for (uint32_t i = 0u; i < 5u; ++i) assert(words[i] == 0u);
        assert(words[5] == (count > STPG_MAX_EVENT_COUNT));
    }
    assert(stpg_samples_sample_count() == 0u);
    assert(stpg_samples_dma_capacity_words() == STPG_PSRAM_RESULT_BYTES / 4u);
    const bool staged = STPG_SAMPLE_PSRAM_SRAM_STAGING && count > STPG_MAX_EVENT_COUNT;
    assert(stpg_samples_dma_ring_bits() == (staged ? 13u : 0u));
    if (staged) {
        assert(((uintptr_t)stpg_samples_dma_buffer() & 8191u) == 0u);
        assert(stpg_samples_dma_buffer() != stpg_psram_mapped_ptr(STPG_PSRAM_RESULT_BASE, count * 4u));
    }
}

static void drain(void) {
    if (stpg_samples_dma_ring_bits()) {
        while (stpg_samples_flushed_count() < produced) {
            const uint32_t before = stpg_samples_flushed_count();
            calls = 0u;
            assert(stpg_samples_service(frontier) == ERR_NONE);
            const uint32_t after = stpg_samples_flushed_count();
            assert(after > before && after - before <= 64u);
        }
    } else {
        assert(stpg_samples_service(0) == ERR_NONE);
    }
}

static void check_values(uint32_t count) {
    assert(stpg_samples_sample_count() == count);
    assert(stpg_samples_result_flags() == 0u);
    for (uint32_t i = 0u; i < count; ++i) assert(stpg_samples_value_at(i) == logical_value(i));
    const uint32_t element = pins == 24u ? 4u : 2u;
    uint8_t actual[128], expected[128];
    for (uint32_t i = 0u; i < sizeof(expected); ++i)
        expected[i] = (uint8_t)(logical_value(i / element) >> ((i % element) * 8u));
    stpg_sample_read_view_t view;
    assert(stpg_samples_prepare_read(0u, sizeof(actual), &view) == ERR_NONE);
    stpg_samples_read_bytes(0u, actual, sizeof(actual));
    assert(memcmp(actual, expected, sizeof(actual)) == 0);
    assert(view.payload_crc32 == stpg_crc32(expected, sizeof(expected)));
    assert(view.total_sample_count == count && view.result_flags == 0u);
}

static void successful_run(uint32_t count, uint32_t active_pins, uint32_t burst) {
    prepare(count, active_pins);
    while (produced < count) {
        const uint32_t amount = count - produced < burst ? count - produced : burst;
        produce(amount);
        /* Leave the last suffix for the quiescent terminal flush. */
        if (produced < count) drain();
    }
    assert(stpg_samples_finish_staging(frontier) == ERR_NONE);
    stpg_samples_finish_run(count, false);
    check_values(count);
    /* STOP/RESET reuse must not retain wrap/failed/frontier state. */
    stpg_samples_reset();
    assert(stpg_samples_dma_ring_bits() == 0u);
    assert(stpg_samples_flushed_count() == 0u);
}

#if STPG_SAMPLE_PSRAM_SRAM_STAGING
static void reject_and_latch(void) {
    assert(stpg_samples_service(frontier) == ERR_DMA_ERROR);
    assert(stpg_samples_result_flags() == STPG_RESULT_FLAG_SAMPLE_OVERFLOW);
    const uint32_t valid = stpg_samples_flushed_count();
    uint32_t first[6], again[6];
    assert(stpg_samples_staging_diagnostics(71u, first));
    assert(first[0] != 0u && first[1] != 0u);
    assert(stpg_samples_service(frontier) == ERR_DMA_ERROR);
    stpg_samples_finish_run(produced, false);
    assert(stpg_samples_sample_count() == (produced < valid ? produced : valid));
    assert(stpg_samples_result_flags() == STPG_RESULT_FLAG_SAMPLE_OVERFLOW);
    assert(stpg_samples_staging_diagnostics(71u, again));
    assert(memcmp(first, again, sizeof(first)) == 0);
}

#ifdef STPG_STAGE_DESTINATION_DELAY
static void observed_family_stop_frontiers(uint32_t active_pins) {
    /* e54cf4f2's saved counters are consistent with one 64-word commit,
     * frontier 493, then frontier 2727 at the next pre-snapshot check.
     * Pick an admissible first frontier (76) and model the intervening RX.
     * This reproduces the software rejection, NOT N-4, physical DMA or the
     * reported timing. Host service timestamps are deliberately zero. */
    prepare(100006u, active_pins);
    produce(76u);
    store_extra = 417u;
    assert(stpg_samples_service(frontier) == ERR_NONE);
    assert(produced == 493u && stpg_samples_flushed_count() == 64u);
    for (uint32_t i = 0u; i < 64u; ++i)
        assert(stpg_samples_value_at(i) == logical_value(i));

    produce(2234u); /* No service between the two completed-write frontiers. */
    assert(produced == 2727u);
    assert(stpg_samples_service(frontier) == ERR_DMA_ERROR);
    assert(stpg_samples_result_flags() == STPG_RESULT_FLAG_SAMPLE_OVERFLOW);
    uint32_t words[6], first_fault[6];
    assert(stpg_samples_staging_diagnostics(70u, words));
    assert(words[0] == 2u && words[1] == 64u);
    assert(words[4] == 2663u && words[5] == 1u);
    assert(stpg_samples_staging_diagnostics(71u, first_fault));
    const uint32_t expected_fault[6] = {5u, 1u, 64u, 2727u, 100006u, 493u};
    assert(memcmp(first_fault, expected_fault, sizeof(first_fault)) == 0);
    assert(stpg_samples_staging_diagnostics(72u, words));
    const uint32_t expected_frontiers[6] = {64u, 493u, 1u, 0u, 2048u, 64u};
    assert(memcmp(words, expected_frontiers, sizeof(words)) == 0);
    assert(stpg_samples_service(frontier) == ERR_DMA_ERROR);
    assert(stpg_samples_staging_diagnostics(71u, words));
    assert(memcmp(words, first_fault, sizeof(words)) == 0);

    /* Fresh preparation must clear the failure while retaining normal
     * staging, value/order checks and the overrun guard on the next run. */
    successful_run(8192u, active_pins, 1024u);
}
#endif

static void failure_cases(void) {
    prepare(8192u, 24u);
    produce(2033u);
    reject_and_latch();
    assert(stpg_samples_flushed_count() == 0u);
    uint32_t words[6];
    assert(stpg_samples_staging_diagnostics(71u, words));
    assert(words[0] == 5u && words[1] == 1u && words[3] == 2033u);

    prepare(8192u, 24u);
    produce(2032u);
    after_extra = 1u; /* overwrite risk arises during the CPU copy */
    reject_and_latch();
    assert(stpg_samples_flushed_count() == 0u);
    assert(stpg_samples_staging_diagnostics(71u, words));
    assert(words[0] == 5u && words[1] == 2u);

    prepare(8192u, 16u);
    produce(128u);
    override_enabled = true;
    after_override = 127u; /* DMA frontier must never go backward */
    reject_and_latch();

#ifdef STPG_STAGE_DESTINATION_DELAY
    /* Once captured in disjoint SRAM, 64 oldest words may wrap while QMI
     * writes are outstanding. The remaining 2032 unread words are still safe.
     * Reusing the old whole-prefix guard AFTER adding the disjoint snapshot
     * would reject this now-safe case. The original interleaved-copy design
     * still needed that old guard; its rejection was not proven spurious. */
    prepare(8192u, 24u);
    produce(2032u);
    store_extra = 64u;
    assert(stpg_samples_service(frontier) == ERR_NONE);
    assert(produced == 2096u && stpg_samples_flushed_count() == 64u);
    for (uint32_t i = 0u; i < 64u; ++i) assert(stpg_samples_value_at(i) == logical_value(i));
    assert(stpg_samples_staging_diagnostics(71u, words) && words[0] == 0u);
    drain();
    produce(64u);
    drain();

    prepare(8192u, 16u);
    produce(2032u);
    store_extra = 65u; /* The still-unread suffix now exceeds the margin. */
    reject_and_latch();
    assert(stpg_samples_staging_diagnostics(71u, words));
    assert(words[0] == 5u && words[1] == 3u && words[2] == 64u && words[3] == 2097u);
    assert(stpg_samples_flushed_count() == 0u);
#endif

    prepare(8192u, 16u);
    produced = 8193u; /* invalid count; reject before touching the ring */
    reject_and_latch();

    prepare(8192u, 24u);
    produce(128u);
    assert(stpg_samples_finish_staging(frontier) == ERR_DMA_ERROR);
    assert(stpg_samples_flushed_count() == 0u);
    assert(stpg_samples_result_flags() == STPG_RESULT_FLAG_SAMPLE_OVERFLOW);

    prepare(8192u, 24u);
    assert(stpg_samples_service(0) == ERR_DMA_ERROR);
    assert(stpg_samples_finish_staging(frontier) == ERR_DMA_ERROR);

    prepare(8192u, 24u);
    produce(128u);
    after_extra = 64u; /* safe growth while copying, flushed prefix stays exact */
    assert(stpg_samples_service(frontier) == ERR_NONE);
    assert(produced == 192u && stpg_samples_flushed_count() == 64u);
    after_extra = 0u;
    drain();
    produced = 191u;
    calls = 0u;
    reject_and_latch();
    assert(!stpg_samples_staging_diagnostics(69u, words));
    assert(!stpg_samples_staging_diagnostics(70u, 0));
}
#endif

int main(void) {
    stpg_samples_reset();
    for (uint32_t p = 16u; p <= 24u; p += 8u) {
        successful_run(4096u, p, 1024u);
        successful_run(6551u, p, 1024u);
        successful_run(6552u, p, 2032u);
        successful_run(8192u, p, 1024u);
        successful_run(100000u, p, 127u);
        successful_run(STPG_PSRAM_RESULT_BYTES / 4u, p, 1024u);
    }
    assert(stpg_samples_prepare_for_run(STPG_PSRAM_RESULT_BYTES / 4u + 1u, 24u) == ERR_BAD_SIZE);
    assert(stpg_samples_dma_ring_bits() == 0u);
#if STPG_SAMPLE_PSRAM_SRAM_STAGING
    failure_cases();
#ifdef STPG_STAGE_DESTINATION_DELAY
    observed_family_stop_frontiers(16u);
    observed_family_stop_frontiers(24u);
#endif
#endif
    puts("sample SRAM staging values/frontiers/terminal/reset PASS");
    return 0;
}
