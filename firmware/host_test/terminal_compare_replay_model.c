/* Actual compare.c with bounded synthetic metadata and deterministic work
 * costs. A scheduling/semantics model, not physical USB/PSRAM/timing evidence. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "stpg_compare.h"
#include "stpg_pattern_buffer.h"
#include "stpg_protocol.h"
#include "stpg_samples.h"

/* Exact R405 synchronous function, extracted from the pinned source copy. */
#include "historical-terminal-replay.inc"

static ExecEvent metadata[STPG_STREAM_BUFFER_EVENTS];
static uint32_t indices[STPG_STREAM_BUFFER_EVENTS];
static uint32_t total_events, total_samples, pins, emitted, meta_count;
static uint32_t fills, reads, clock_us, mismatch_every;
static unsigned fault;
uint32_t stpg_samples_sample_count(void) { return total_samples; }
uint32_t stpg_samples_active_pins(void) { return pins; }
uint32_t stpg_samples_value_at(uint32_t sample) {
    assert(sample < total_samples);
    ++reads;
    return mismatch_every && sample % mismatch_every == 0 ? 0 : 0x01000015;
}
stpg_err_t stpg_buffer_stream_compare_replay_init(stpg_rate_stream_state_t *state) {
    assert(state);
    emitted = fills = reads = 0;
    return fault == 1 ? ERR_BAD_STATE : ERR_NONE;
}
uint32_t stpg_buffer_event_count(void) { return total_events; }
uint32_t stpg_buffer_stream_compare_event_count(void) { return meta_count; }
const ExecEvent *stpg_buffer_stream_compare_events(void) { return fault == 7 ? NULL : metadata; }
const uint32_t *stpg_buffer_stream_compare_event_indices(void) { return indices; }
stpg_err_t stpg_buffer_stream_compare_replay_fill_next(
    stpg_rate_stream_state_t *state, stpg_stream_chunk_result_t *result) {
    assert(state->current_value == 0x1234 && state->current_dir == 0x5678);
    ++fills;
    clock_us += 10000; /* deterministic10ms work model, NOT a measured fill */
    if (fault == 2 && fills == 2) return ERR_BAD_STATE;
    uint32_t count = total_events - emitted;
    if (count > STPG_STREAM_BUFFER_EVENTS) count = STPG_STREAM_BUFFER_EVENTS;
    memset(result, 0, sizeof(*result));
    result->event_count = count;
    result->eof = emitted + count == total_events;
    meta_count = count / 2;
    for (uint32_t i = 0; i < meta_count; ++i) {
        metadata[i] = (ExecEvent){.flags = STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT,
            .expect_value = 0x01000015, .sample_mask = 0xffffffff};
        indices[i] = emitted + 2 * i + 1;
    }
    emitted += count;
    if (fault == 3) { result->event_count = 0; result->eof = false; meta_count = 0; }
    if (fault == 4) result->eof = true; /* early EOF */
    if (fault == 5) result->event_count = STPG_STREAM_BUFFER_EVENTS + 1;
    if (fault == 6) meta_count = result->event_count + 1;
    return ERR_NONE;
}

static void prepare(uint32_t samples, uint32_t active_pins) {
    total_samples = samples;
    total_events = samples * 2;
    pins = active_pins;
    fault = mismatch_every = clock_us = 0;
}

static void check_equivalence(void) {
    const uint32_t counts[] = {0, 4, 1024, 1025, 6551, 7000, 524288};
    for (uint32_t p = 16; p <= 24; p += 8) {
        for (uint32_t n = 0; n < sizeof(counts)/sizeof(counts[0]); ++n) {
            for (uint32_t mismatch = 0; mismatch < 2; ++mismatch) {
                prepare(counts[n], p);
                mismatch_every = mismatch ? 3 : 0;
                stpg_rate_stream_state_t state;
                uint32_t compared = 999;
                assert(historical_compare_replayed(&state,0x1234,0x5678,&compared) == ERR_NONE);
                assert(compared == counts[n] && reads == counts[n]);
                const uint32_t expected_fails = mismatch ? (counts[n] + 2) / 3 : 0;
                assert(stpg_compare_fail_count() == expected_fails);
                const uint32_t flags = stpg_compare_result_flags();
                assert(flags == ((expected_fails ? STPG_RESULT_FLAG_COMPARE_MISMATCH : 0)
                    | (expected_fails > STPG_FAIL_LOG_MAX ? STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW : 0)));
                FailLogEntry expected[STPG_FAIL_LOG_MAX], actual[STPG_FAIL_LOG_MAX];
                const uint32_t stored = expected_fails > STPG_FAIL_LOG_MAX ? STPG_FAIL_LOG_MAX : expected_fails;
                stpg_compare_read_bytes(0,(uint8_t*)expected,stored * sizeof(*expected));
                stpg_compare_replay_t replay;
                assert(stpg_compare_replay_begin(&replay,&state,0x1234,0x5678) == ERR_NONE);
                bool done = false;
                uint32_t turns = 0;
                while (!done) {
                    uint32_t before = fills;
                    assert(stpg_compare_replay_step(&replay,&done) == ERR_NONE);
                    assert(fills == before + 1); /* exactly one bounded fill */
                    ++turns; /* a USB/main-loop opportunity between every step */
                }
                assert(replay.event_count == total_events && replay.sample_index == counts[n]);
                assert(replay.compare_count == compared && stpg_compare_fail_count() == expected_fails);
                assert(stpg_compare_result_flags() == flags && turns >= 1);
                stpg_compare_read_bytes(0,(uint8_t*)actual,stored * sizeof(*actual));
                assert(!memcmp(expected,actual,stored * sizeof(*actual)));
                for (uint32_t i = 0; i < stored; ++i) {
                    assert(actual[i].event_index == i * 6 + 1);
                    assert(actual[i].actual == 0 && actual[i].expected == 0x15);
                    assert(actual[i].mask == (p == 24 ? 0xffffff : 0xffff));
                }
                assert(stpg_compare_replay_step(&replay,&done) == ERR_BAD_STATE && !done);
            }
        }
    }
}

static void check_fail_closed(void) {
    stpg_rate_stream_state_t state;
    stpg_compare_replay_t replay;
    bool done = true;
    assert(stpg_compare_replay_begin(NULL,&state,0,0) == ERR_BAD_SIZE);
    assert(stpg_compare_replay_begin(&replay,NULL,0,0) == ERR_BAD_SIZE && !replay.active);
    assert(stpg_compare_replay_step(NULL,&done) == ERR_BAD_STATE && !done);
    assert(stpg_compare_replay_step(&replay,NULL) == ERR_BAD_SIZE);
    prepare(7000,24);
    assert(stpg_compare_replay_begin(&replay,&state,0x1234,0x5678) == ERR_NONE);
    assert(replay.active && stpg_compare_replay_step(&replay,NULL) == ERR_BAD_SIZE && !replay.active);
    assert(stpg_compare_replay_step(&replay,&done) == ERR_BAD_STATE && !done);
    for (unsigned f = 1; f <= 8; ++f) {
        prepare(7000,24);
        fault = f;
        if (f == 8) --total_samples; /* incomplete sample frontier */
        stpg_err_t err = stpg_compare_replay_begin(&replay,&state,0x1234,0x5678);
        done = false;
        for (unsigned i = 0; err == ERR_NONE && !done && i < 20; ++i)
            err = stpg_compare_replay_step(&replay,&done);
        assert(err != ERR_NONE && !done && !replay.active);
        const uint32_t before = fills;
        assert(stpg_compare_replay_step(&replay,&done) == ERR_BAD_STATE);
        assert(fills == before && !done);
    }
}

int main(void) {
    check_equivalence();
    check_fail_closed();
    /* Old synchronous API:524288 samples occupy5120ms in this work model.
     * Incremental API: one10ms slice per opportunity. This demonstrates the
     * control-flow defect, not the physical R405 timeout's unique cause. */
    prepare(524288,24);
    stpg_rate_stream_state_t state;
    assert(historical_compare_replayed(&state,0x1234,0x5678,NULL) == ERR_NONE);
    assert(clock_us > 2000000);
    clock_us = 0;
    stpg_compare_replay_t replay;
    assert(stpg_compare_replay_begin(&replay,&state,0x1234,0x5678) == ERR_NONE);
    bool done = false;
    uint32_t opportunities = 0;
    while (!done) {
        uint32_t before = clock_us;
        assert(stpg_compare_replay_step(&replay,&done) == ERR_NONE);
        assert(clock_us - before == 10000);
        ++opportunities;
    }
    assert(opportunities > 1);
    puts("actual-C replay equivalence/fail-closed/one-fill scheduling PASS");
    return 0;
}
