/* Actual extracted arm code; modeled documented DMA reload/advanced-address
 * semantics. No physical DMA arbitration, bus read or waveform measurement. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "stpg_common.h"
#include "stpg_runner.h"

typedef unsigned int uint;
#define __no_inline_not_in_flash_func(name) name
#define DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB 0u
#define DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS 0xffu
#include "empty-eof-types.inc"

static struct {
    int output_dma_channel, output_dma_spare_channel, output_dma_armed_slot_index;
    struct {
        uint32_t queued_event_count;
        stpg_stream_slot_t slots[STPG_STREAM_SLOT_COUNT];
        struct {
            uint32_t p3_chain_arm_count, p3_terminal_chain_neutralize_count;
            uint32_t p3_telemetry_sequence, p3_stream_run_sequence;
        } telemetry;
    } stream;
} g_runner;
static struct { struct { uint32_t al1_ctrl, transfer_count; } ch[2]; } dma_regs;
#define dma_hw (&dma_regs)
static struct {
    uintptr_t read_next, owned_start, owned_end;
    uint32_t reload, generation, starts;
    bool busy, pending;
} channels[2];
static uint32_t payload[3][8];
static uint configurations, last_config_channel, bad_reads;

static void __dmb(void) {}
static bool stpg_runner_stream_slot_transition(stpg_stream_slot_t *slot,
                                              stpg_stream_slot_state_t next) {
    assert(slot->state == STPG_STREAM_SLOT_READY && next == STPG_STREAM_SLOT_ARMED);
    slot->state = next;
    return true;
}
static void stpg_runner_configure_chained_output_dma(uint c, uint chain,
                                                    const uint32_t *words,
                                                    uint32_t count, bool trigger) {
    assert(c < 2 && chain < 2 && words && count && !trigger && !channels[c].busy);
    dma_hw->ch[c].al1_ctrl = chain;
    dma_hw->ch[c].transfer_count = count;
    channels[c].reload = count;
    channels[c].read_next = channels[c].owned_start = (uintptr_t)words;
    channels[c].owned_end = (uintptr_t)(words + count);
    channels[c].pending = false;
    ++channels[c].generation;
    ++configurations;
    last_config_channel = c;
}
static void hw_write_masked(uint32_t *reg, uint32_t value, uint32_t mask) {
    assert(reg == &dma_hw->ch[g_runner.output_dma_channel].al1_ctrl);
    assert(mask == DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS);
    /* New payload/count must be programmed BEFORE exposing its incoming link. */
    assert(last_config_channel == value && channels[value].generation > 0);
    *reg = (*reg & ~mask) | (value & mask);
}
#include "chain-return-arm.inc"

static void trigger(uint c) {
    assert(c < 2 && !channels[c].busy && channels[c].reload > 0);
    /* Trigger reloads count, not READ_ADDR. Self-chain is not a trigger. */
    dma_hw->ch[c].transfer_count = channels[c].reload;
    channels[c].busy = true;
    ++channels[c].starts;
}
static void complete(uint c) {
    assert(c < 2 && channels[c].busy);
    const uintptr_t end = channels[c].read_next + 4u * dma_hw->ch[c].transfer_count;
    if (channels[c].read_next < channels[c].owned_start || end > channels[c].owned_end) {
        ++bad_reads; /* No invalid host pointer is dereferenced. */
    }
    channels[c].read_next = end;
    dma_hw->ch[c].transfer_count = 0;
    channels[c].busy = false;
    channels[c].pending = true;
    const uint next = dma_hw->ch[c].al1_ctrl;
    if (next != c) trigger(next);
}
static void setup(bool terminal) {
    memset(&g_runner, 0, sizeof(g_runner));
    memset(&dma_regs, 0, sizeof(dma_regs));
    memset(channels, 0, sizeof(channels));
    configurations = bad_reads = 0;
    g_runner.output_dma_channel = 0;
    g_runner.output_dma_spare_channel = 1;
    g_runner.output_dma_armed_slot_index = -1;
    for (uint i = 0; i < 3; ++i) {
        stpg_stream_slot_t *s = &g_runner.stream.slots[i];
        s->words = payload[i]; s->word_count = 8; s->event_count = 2;
        s->state = i ? STPG_STREAM_SLOT_READY : STPG_STREAM_SLOT_ACTIVE;
    }
    g_runner.stream.slots[1].eof = terminal;
    stpg_runner_configure_chained_output_dma(0, 0, payload[0], 8, false);
    trigger(0);
}

int main(int argc, char **argv) {
    const bool old = argc == 2 && strcmp(argv[1], "historical-cycle") == 0;
    setup(false);
    assert(stpg_runner_arm_ready_output_slot(1) == ERR_NONE);
    assert(configurations == 2 && dma_hw->ch[0].al1_ctrl == 1);
    assert(dma_hw->ch[1].al1_ctrl == (old ? 0u : 1u));
    complete(0);
    assert(channels[1].busy && channels[0].starts == 1 && channels[1].starts == 1);
    complete(1); /* No ownership IRQ turn occurs between either completion. */
    if (old) {
        assert(channels[0].busy && channels[0].starts == 2);
        assert(channels[0].read_next == channels[0].owned_end);
        complete(0);
        assert(bad_reads == 1); /* Controlled reproduction, not a physical read. */
        puts("OLD_PREMATURE_RETURN_REPRODUCED");
        return 0;
    }
    assert(!channels[0].busy && !channels[1].busy && !bad_reads);
    assert(channels[0].pending && channels[1].pending);
    assert(channels[0].starts == 1 && channels[1].starts == 1);

    /* Timely ownership turn: reconfigure A for C, THEN expose B->A. */
    setup(false);
    assert(stpg_runner_arm_ready_output_slot(1) == ERR_NONE);
    complete(0);
    g_runner.output_dma_channel = 1; g_runner.output_dma_spare_channel = 0;
    g_runner.output_dma_armed_slot_index = -1;
    g_runner.stream.slots[0].state = STPG_STREAM_SLOT_EMPTY;
    g_runner.stream.slots[1].state = STPG_STREAM_SLOT_ACTIVE;
    assert(stpg_runner_arm_ready_output_slot(2) == ERR_NONE);
    assert(dma_hw->ch[0].al1_ctrl == 0 && dma_hw->ch[1].al1_ctrl == 0);
    assert(channels[0].generation == 2 && channels[0].read_next == (uintptr_t)payload[2]);
    complete(1); complete(0);
    assert(!bad_reads && !channels[0].busy && !channels[1].busy);
    assert(channels[0].starts == 2 && channels[1].starts == 1);

    setup(true);
    assert(stpg_runner_arm_ready_output_slot(1) == ERR_NONE);
    complete(0); complete(1);
    assert(!bad_reads && !channels[0].busy && !channels[1].busy);
    assert(g_runner.stream.telemetry.p3_terminal_chain_neutralize_count == 1);

    setup(true);
    g_runner.stream.slots[1].event_count = g_runner.stream.slots[1].word_count = 0;
    assert(stpg_runner_arm_ready_output_slot(1) == ERR_BAD_STATE);
    assert(configurations == 1); /* Empty EOF is never programmed as DMA. */
    puts("CHAIN_RETURN_OWNERSHIP_MODEL_PASS");
    return 0;
}
