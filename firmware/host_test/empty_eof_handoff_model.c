/* Extracted actual runner functions with controlled IRQ/producer ordering.
 * Not a physical DMA/QMI, timing, interrupt-latency or sample-content model. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include "stpg_runner.h"
#include "stpg_stream_handoff.h"
#include "stpg_common.h"

typedef unsigned int uint;
#define __no_inline_not_in_flash_func(name) name
#define STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF 0
#define NUM_DMA_CHANNELS 2u
#define DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB 0
#define DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS 0xffu
enum { STPG_OUTPUT_HANDOFF_OWNER_NONE, STPG_OUTPUT_HANDOFF_OWNER_IRQ,
       STPG_OUTPUT_HANDOFF_OWNER_MAIN_LOOP };
enum { STPG_UNDERFLOW_REASON_FILL_ERROR = 3, STPG_UNDERFLOW_REASON_NO_READY_SLOT = 1,
       STPG_UNDERFLOW_REASON_STREAM_STALL = 4 };

#include "empty-eof-types.inc"
static struct {
    bool active, streaming, draining, reduced_density_streaming, p4_explicit_wait_enabled;
    bool output_dma_chain_enabled, pending_noready_prod_recheck;
    int output_dma_channel, output_dma_spare_channel, output_dma_armed_slot_index;
    uint32_t event_count, tail_drain_ticks;
    struct {
        uint32_t active_slot_index, queued_event_count;
        stpg_stream_slot_t slots[STPG_STREAM_SLOT_COUNT];
        stpg_stream_telemetry_t telemetry;
    } stream;
} g_runner;
static volatile uint32_t g_chained_irq_reject[6];
static struct { struct { uint32_t al1_ctrl, transfer_count; } ch[2]; } dma_storage;
#define dma_hw (&dma_storage)
static bool busy[2], pending[2], irq_enabled[2], publish_early;
static uint32_t tick, arms, drains, requests, progress, fail_error, fail_calls;
static uint32_t payload[3][8];

static void __dmb(void) {}
static uint32_t time_us_32(void) { return ++tick; }
static bool dma_channel_get_irq0_status(uint c) { assert(c < 2); return pending[c]; }
static bool dma_channel_is_busy(uint c) { assert(c < 2); return busy[c]; }
static void dma_channel_acknowledge_irq0(uint c) { pending[c] = false; }
static void dma_channel_set_irq0_enabled(uint c, bool enabled) { assert(c < 2); irq_enabled[c] = enabled; }
static void hw_write_masked(uint32_t *p, uint32_t value, uint32_t mask) { *p = (*p & ~mask) | (value & mask); }
static uint32_t save_and_disable_interrupts(void) { return 0; }
static void restore_interrupts(uint32_t state) { assert(state == 0); }
static void stpg_runner_configure_chained_output_dma(uint c, uint chain, const uint32_t *words,
                                                    uint32_t count, bool trigger) {
    assert(c < 2 && chain < 2 && words != 0 && count > 0 && !trigger);
    ++arms;
    dma_hw->ch[c].transfer_count = count;
}
static stpg_err_t stpg_stream_find_fill_error(void) { return ERR_NONE; }
static stpg_err_t stpg_runner_stream_promote_ready_slots(bool irq) { (void)irq; return ERR_NONE; }
static void stpg_runner_request_stream_fail(stpg_err_t err, bool irq) {
    assert(irq); fail_error = err; ++fail_calls;
}
static void stpg_runner_fail_streaming(stpg_err_t err, bool irq) {
    assert(!irq); fail_error = err; ++fail_calls;
}
static void stpg_runner_record_dma_completion(uint32_t path, uint32_t completed, int32_t armed,
                                             int32_t next, uint32_t restart) {
    (void)path; (void)completed; (void)armed; (void)next; (void)restart;
}
static void stpg_runner_capture_underflow_snapshot(uint32_t completed, int32_t next, uint32_t reason) {
    (void)completed; (void)next; (void)reason;
}
static void stpg_runner_begin_draining(uint64_t ticks) {
    assert(ticks == 999 && !busy[g_runner.output_dma_channel]);
    ++drains; g_runner.draining = true;
}
static uint64_t stpg_runner_compute_tail_ticks_from_words(const uint32_t *words, uint32_t events) {
    assert(words != 0 && events > 0); return 999;
}
static bool stpg_runner_try_defer_production_noready(uint32_t completed, bool irq) {
    (void)completed; (void)irq; return false;
}
static void stpg_runner_note_stream_progress(void) {}
static void stpg_status_set_run_progress(uint32_t value) { progress = value; }
static void stpg_runner_stream_update_min_ready_slots(void) {}
static stpg_err_t stpg_runner_start_sample_sched_dma(const uint32_t *words, uint32_t count) {
    (void)words; (void)count; return ERR_NONE;
}
static uint32_t *stpg_runner_stream_sample_wait_buffer(uint32_t slot) { return payload[slot]; }
static void stpg_runner_start_output_dma(const uint32_t *words, uint32_t count) {
    assert(words != 0 && count != 0); ++arms;
}
static void publish_marker(uint32_t slot) {
    stpg_stream_slot_t *s = &g_runner.stream.slots[slot];
    s->state = STPG_STREAM_SLOT_READY;
    s->event_count = s->word_count = s->sample_count = 0;
    s->eof = true;
}
static stpg_err_t stpg_runner_request_stream_fill_safe(uint32_t slot) {
    ++requests;
    g_runner.stream.slots[slot].state = STPG_STREAM_SLOT_FILLING;
    if (publish_early) publish_marker(slot);
    return ERR_NONE;
}
static void stpg_runner_stream_swap_or_finish(bool from_irq);
static void stpg_runner_neutralize_active_output_dma_chain(void);
#include "chain-irq-diagnostics.inc"
#include "empty-eof-functions.inc"

static void setup(void) {
    memset(&g_runner, 0, sizeof(g_runner));
    memset((void *)g_chained_irq_reject, 0, sizeof(g_chained_irq_reject));
    memset(&dma_storage, 0, sizeof(dma_storage));
    g_runner.active = g_runner.streaming = g_runner.output_dma_chain_enabled = true;
    g_runner.output_dma_channel = 0; g_runner.output_dma_spare_channel = 1;
    g_runner.output_dma_armed_slot_index = 1;
    g_runner.stream.queued_event_count = 63488;
    for (uint32_t i = 0; i < 3; ++i) {
        stpg_stream_slot_t *s = &g_runner.stream.slots[i];
        s->words = payload[i]; s->event_count = 2048; s->word_count = 8192; s->sample_count = 1024;
        s->state = i == 0 ? STPG_STREAM_SLOT_ACTIVE : i == 1 ? STPG_STREAM_SLOT_ARMED : STPG_STREAM_SLOT_FILLING;
    }
    busy[0] = false; busy[1] = true;
    pending[0] = true; pending[1] = false;
    dma_channel_set_irq0_enabled(0, true);
    dma_channel_set_irq0_enabled(1, true);
    dma_hw->ch[1].transfer_count = 8000;
    arms = drains = requests = progress = fail_error = fail_calls = tick = 0;
    publish_early = true;
}

int main(int argc, char **argv) {
    const bool historical = argc == 2 && strcmp(argv[1], "historical-rejection") == 0;
    if (argc == 2 && strcmp(argv[1], "transition-rejection") == 0) {
        setup();
        assert(stpg_runner_handle_chained_output_completion());
        assert(g_chained_irq_reject[0] == STPG_CHAIN_IRQ_REJECT_SLOT_TRANSITION);
        assert(fail_error == ERR_BAD_STATE && fail_calls == 1 && !arms && !drains);
        return 0;
    }
    setup();
    assert(stpg_runner_handle_chained_output_completion());
    assert(progress == 65536 && requests == 1);
    if (historical) {
        assert(fail_error == ERR_BAD_STATE && fail_calls == 1 && !drains && !arms);
        return 0;
    }
    assert(!fail_error && !fail_calls && !drains && !arms);
    assert(!g_runner.output_dma_chain_enabled && g_runner.output_dma_armed_slot_index == -1);
    assert(dma_hw->ch[1].al1_ctrl == 1u && dma_hw->ch[0].transfer_count == 0u);
    assert(g_runner.stream.slots[0].state == STPG_STREAM_SLOT_READY);
    stpg_runner_try_main_loop_rearm_ready_slot();
    assert(!g_runner.output_dma_chain_enabled && !arms && !drains);
    busy[1] = false; dma_hw->ch[1].transfer_count = 0;
    stpg_runner_stream_swap_or_finish(true);
    assert(drains == 1 && !fail_calls && !arms);

    /* The same marker arriving after the predecessor IRQ must also drain. */
    setup(); publish_early = false;
    assert(stpg_runner_handle_chained_output_completion());
    publish_marker(0);
    stpg_runner_try_main_loop_rearm_ready_slot();
    assert(!fail_calls && !arms && !drains);
    busy[1] = false; dma_hw->ch[1].transfer_count = 0;
    stpg_runner_stream_swap_or_finish(false);
    assert(drains == 1 && !fail_calls);

    /* Contradictory zero-event metadata must never become normal drain. */
    for (uint32_t corrupt = 0; corrupt < 3; ++corrupt) {
        setup(); publish_early = false;
        assert(stpg_runner_handle_chained_output_completion());
        publish_marker(0);
        if (corrupt == 0) g_runner.stream.slots[0].word_count = 4;
        if (corrupt == 1) g_runner.stream.slots[0].sample_count = 1;
        if (corrupt == 2) g_runner.stream.slots[0].eof = false;
        busy[1] = false; dma_hw->ch[1].transfer_count = 0;
        stpg_runner_stream_swap_or_finish(false);
        assert(fail_calls == 1 && fail_error != ERR_NONE && !drains && !arms);
    }
    setup(); publish_marker(2); g_runner.stream.slots[2].word_count = 4;
    assert(stpg_runner_arm_ready_output_slot(2) == ERR_BAD_STATE && !arms);

    /* Nonempty terminal successor retains the existing chain behavior. */
    setup(); publish_early = false;
    g_runner.stream.slots[2].state = STPG_STREAM_SLOT_READY;
    g_runner.stream.slots[2].eof = true;
    assert(stpg_runner_handle_chained_output_completion());
    assert(!fail_calls && arms == 1 && !drains);
    assert(g_runner.stream.slots[2].state == STPG_STREAM_SLOT_ARMED);
    assert(g_runner.output_dma_armed_slot_index == 2);

    /* An already completed nonempty terminal successor keeps the old drain. */
    setup();
    g_runner.stream.slots[1].eof = true;
    busy[1] = false; dma_hw->ch[1].transfer_count = 0; pending[1] = true;
    assert(stpg_runner_handle_chained_output_completion());
    assert(drains == 1 && !fail_calls && !arms && !pending[1]);
    assert(progress == 65536 && !requests);

    /* A normal nonterminal successor is still prearmed. */
    setup(); publish_early = false;
    g_runner.stream.slots[2].state = STPG_STREAM_SLOT_READY;
    assert(stpg_runner_handle_chained_output_completion());
    assert(!fail_calls && !drains && arms == 1 && g_runner.output_dma_chain_enabled);
    assert(g_runner.stream.slots[2].state == STPG_STREAM_SLOT_ARMED);

    /* Delayed predecessor IRQ: a completed nonterminal successor must not
     * be adopted or restarted. This models a schedule, not physical halt time. */
    setup();
    busy[1] = false; dma_hw->ch[1].transfer_count = 0; pending[1] = true;
    assert(stpg_runner_handle_chained_output_completion());
    assert(fail_error == ERR_BAD_STATE && fail_calls == 1 && !arms && !drains && !progress);
    assert(!irq_enabled[1] && !pending[1] && !g_runner.output_dma_chain_enabled);
    assert(g_runner.stream.slots[0].state == STPG_STREAM_SLOT_ACTIVE);
    assert(g_runner.stream.slots[1].state == STPG_STREAM_SLOT_ARMED);
    uint32_t words[6], repeat[6];
    assert(!stpg_runner_chained_irq_diagnostics(NULL));
    assert(stpg_runner_chained_irq_diagnostics(words));
    assert(words[0] == STPG_CHAIN_IRQ_REJECT_NONTERMINAL_COMPLETE && words[1] == ERR_BAD_STATE);
    assert(words[2] == 0 && words[3] == 1 && words[4] == (1u | 16u | (4u << 8) | (3u << 16)) && words[5] == 0);
    stpg_runner_reject_chained_output(STPG_CHAIN_IRQ_REJECT_DMA_OBSERVATION, true, 777);
    assert(stpg_runner_chained_irq_diagnostics(repeat) && memcmp(words, repeat, sizeof(words)) == 0);
    assert(stpg_runner_chained_irq_diagnostics(repeat) && memcmp(words, repeat, sizeof(words)) == 0);

    /* Distinguish inconsistent hardware observations from slot ownership. */
    const bool invalid_busy[] = {false, true, false};
    const uint32_t invalid_remaining[] = {42u, 0u, UINT32_MAX};
    for (uint32_t i = 0; i < 3; ++i) {
        setup(); busy[1] = invalid_busy[i]; dma_hw->ch[1].transfer_count = invalid_remaining[i];
        assert(stpg_runner_handle_chained_output_completion());
        assert(g_chained_irq_reject[0] == STPG_CHAIN_IRQ_REJECT_DMA_OBSERVATION);
        assert(g_chained_irq_reject[5] == invalid_remaining[i]);
        assert((g_chained_irq_reject[4] & 1u) != 0);
        assert(fail_error == ERR_BAD_STATE && fail_calls == 1 && !arms && !drains);
    }
    for (uint32_t i = 0; i < 2; ++i) {
        setup(); g_runner.stream.slots[i].state = STPG_STREAM_SLOT_READY;
        assert(stpg_runner_handle_chained_output_completion());
        assert(g_chained_irq_reject[0] == STPG_CHAIN_IRQ_REJECT_SLOT_OWNERSHIP);
        assert(fail_error == ERR_BAD_STATE && fail_calls == 1 && !arms && !drains);
    }
    setup(); g_runner.stream.slots[1].eof = true; g_runner.reduced_density_streaming = true;
    busy[1] = false; dma_hw->ch[1].transfer_count = 0;
    assert(stpg_runner_handle_chained_output_completion());
    assert(g_chained_irq_reject[0] == STPG_CHAIN_IRQ_REJECT_REDUCED_COMPLETE);
    assert((g_chained_irq_reject[4] & 12u) == 12u && fail_calls == 1 && !drains);

    /* Invalid metadata must be rejected before *any* slot dereference.
     * UBSan/bounds turns a removed index guard into a failing executable. */
    const uint32_t completed_indices[] = {STPG_STREAM_SLOT_COUNT, UINT32_MAX};
    for (uint32_t i = 0; i < 2; ++i) {
        setup(); g_runner.stream.active_slot_index = completed_indices[i];
        assert(stpg_runner_handle_chained_output_completion());
        assert(g_chained_irq_reject[0] == STPG_CHAIN_IRQ_REJECT_SLOT_INDEX);
        assert(g_chained_irq_reject[2] == completed_indices[i] && g_chained_irq_reject[5] == UINT32_MAX);
        assert((g_chained_irq_reject[4] & 1u) == 0);
        assert(((g_chained_irq_reject[4] >> 8) & 255u) == 255u);
        assert(fail_calls == 1 && !arms && !drains);
    }
    const int32_t armed_indices[] = {STPG_STREAM_SLOT_COUNT, INT32_MAX, -2, INT32_MIN};
    for (uint32_t i = 0; i < 4; ++i) {
        setup(); g_runner.output_dma_armed_slot_index = armed_indices[i];
        assert(stpg_runner_handle_chained_output_completion());
        assert(g_chained_irq_reject[0] == STPG_CHAIN_IRQ_REJECT_SLOT_INDEX);
        assert(g_chained_irq_reject[3] == (uint32_t)armed_indices[i] && g_chained_irq_reject[5] == UINT32_MAX);
        assert((g_chained_irq_reject[4] & 1u) == 0);
        assert(((g_chained_irq_reject[4] >> 16) & 255u) == 255u);
        assert(fail_calls == 1 && !arms && !drains);
    }
    /* An uncommitted record must never be exposed as partial evidence. */
    setup(); g_chained_irq_reject[1] = ERR_BAD_STATE;
    assert(stpg_runner_chained_irq_diagnostics(words));
    for (uint32_t i = 0; i < 6; ++i) assert(words[i] == 0);
    return 0;
}
