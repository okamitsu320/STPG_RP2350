/* Non-contact audit of an operator-supplied binary using the real emitter.
 * No external fixture paths are hardcoded; this utility is not a physical run. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "stpg_pattern_buffer.h"
#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_protocol.h"
#include "stpg_psram.h"
/* Inspect the same prepared route and private eligibility predicate as the
 * producer. The audit must not silently force the legacy 4-word route. */
#include "../src/pattern_buffer.c"

static void require_ok(stpg_err_t err) {
    if (err != ERR_NONE) { fprintf(stderr, "firmware emitter error=%u\n", err); exit(1); }
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: audit_rate_vector_file pattern.bin [--no-uop-cache] [--generic] [--pio-out path] [--compare-out path]\n");
        return 2;
    }
    bool cache_disabled = false, generic = false;
    const char *pio_path = NULL, *compare_path = NULL;
    for (int arg = 2; arg < argc; ++arg) {
        if (!strcmp(argv[arg], "--no-uop-cache")) cache_disabled = true;
        else if (!strcmp(argv[arg], "--generic")) generic = true;
        else if (!strcmp(argv[arg], "--pio-out") && arg + 1 < argc) pio_path = argv[++arg];
        else if (!strcmp(argv[arg], "--compare-out") && arg + 1 < argc) compare_path = argv[++arg];
        else return 2;
    }
    FILE *input = fopen(argv[1], "rb");
    if (!input || fseek(input, 0, SEEK_END)) return 2;
    const long length = ftell(input);
    if (length <= 0 || length > STPG_PSRAM_PATTERN_BYTES || fseek(input, 0, SEEK_SET)) return 2;
    const uint32_t size = (uint32_t)length;
    uint8_t *bytes = malloc(size);
    if (!bytes || fread(bytes, 1, size, input) != size) return 2;
    fclose(input);
    stpg_psram_init();
    stpg_buffer_reset();
    const uint32_t crc = stpg_crc32(bytes, size);
    require_ok(stpg_buffer_begin(STPG_STORAGE_PSRAM, size, crc, 0));
    for (uint32_t offset = 0; offset < size; ) {
        const uint32_t count = size - offset < 4096 ? size - offset : 4096;
        require_ok(stpg_buffer_write_chunk(offset, bytes + offset, count, stpg_crc32(bytes + offset, count)));
        offset += count;
    }
    require_ok(stpg_buffer_finalize(size, crc));
    free(bytes);
    stpg_rate_stream_state_t *state = stpg_buffer_stream_state_storage();
    require_ok(stpg_buffer_prepare_for_streaming(state, STPG_RUN_FLAG_STREAMING_POC));
    uint32_t required_groups = 0u;
    for (uint32_t r = 0; r < state->header.rate_count; ++r) required_groups += state->rate_runtime[r].group_count;
    const uint32_t prepared_entries = state->precomputed_uop_count;
    const bool prepared_complete = state->precomputed_uops_complete;
    if (cache_disabled) {
        memset(state->precomputed_uop_index, 0xff, sizeof(state->precomputed_uop_index));
        state->precomputed_uop_count = 0u;
        state->precomputed_uops_complete = false;
    }
    if (generic) state->batch_path_enabled = false;
    const bool reduced = state->reduced_density_compare_enabled;
    const bool batch_path = state->batch_path_enabled;
    const uint32_t stride = reduced ? 3u : 4u;
    bool template_eligible = false;
#if STPG_STREAM_RATE_TEMPLATE_FILL && !STPG_STREAM_HOT_TELEMETRY && !STPG_STREAM_FAIL_STOP
    template_eligible = state->batch_path_enabled && !reduced && stpg_buffer_rate_template_fill_eligible(state);
#endif
    FILE *pio_output = pio_path ? fopen(pio_path, "wbx") : NULL;
    if (pio_path && !pio_output) return 2;
    uint32_t words[2048 * 4];
    uint32_t sample_waits[2048];
    uint32_t events = 0, samples = 0, pio_samples = 0, chunks = 0, long_waits = 0, nonboundary_waits = 0;
    uint32_t pio_crc = 0u, fill_calls = 0u;
    bool terminal_empty_eof = false;
    printf("{\"runner_route\":\"%s\",\"words_per_output_event\":%u,\"template_fill_eligible\":%s,"
           "\"required_groups\":%u,\"prepared_entries\":%u,\"prepared_complete\":%s,\"cache_disabled\":%s,\"chunks\":[",
           reduced ? "reduced" : "standard", stride, template_eligible ? "true" : "false",
           required_groups, prepared_entries, prepared_complete ? "true" : "false", cache_disabled ? "true" : "false");
    for (;;) {
        stpg_stream_chunk_result_t result;
        require_ok(reduced
            ? stpg_buffer_stream_fill_next_chunk_reduced_compare(state, words, sample_waits, 2048, &result)
            : stpg_buffer_stream_fill_next_chunk(state, words, 2048, &result));
        ++fill_calls;
        if (result.event_count == 0 && result.eof) {
            if (result.word_count != 0 || result.sample_event_count != 0) return 1;
            terminal_empty_eof = true;
            break;
        }
        if (result.word_count != result.event_count * stride || result.event_count == 0) return 1;
        uint32_t observed_samples = 0u;
        for (uint32_t i = 0; i < result.event_count; ++i) {
            if (!reduced) observed_samples += words[i * stride + 1] != 0u;
            if (words[i * stride] > 100000) { /* Includes both public 4ms and 8ms waits. */
                ++long_waits;
                nonboundary_waits += (events + i) % 2048 != 0;
            }
        }
        if (reduced) observed_samples = result.sample_event_count;
        if (!reduced && observed_samples != result.sample_event_count) {
            fprintf(stderr, "chunk sample count mismatch at event %u: reported=%u actual=%u\n",
                    events, result.sample_event_count, observed_samples);
            return 1;
        }
        if (pio_output && fwrite(words, sizeof(uint32_t), result.word_count, pio_output) != result.word_count) return 1;
        pio_crc = stpg_crc32_continue(pio_crc, words, result.word_count * sizeof(uint32_t));
        printf("%s{\"event_start\":%u,\"events\":%u,\"samples\":%u,\"first_wait_word\":%u,\"eof\":%s}",
               chunks ? "," : "", events, result.event_count, observed_samples, words[0], result.eof ? "true" : "false");
        ++chunks; events += result.event_count; samples += result.sample_event_count;
        pio_samples += observed_samples;
        if (result.eof) break;
    }
    if (pio_output && fclose(pio_output)) return 1;
    stpg_stream_buffer_telemetry_t telemetry;
    stpg_buffer_get_stream_telemetry(&telemetry);
    uint32_t metadata_count = 0u, metadata_crc = 0u;
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
    if (compare_path) {
        FILE *metadata = fopen(compare_path, "wbx");
        if (!metadata) return 2;
        require_ok(stpg_buffer_stream_compare_replay_init(state));
        if (cache_disabled) {
            memset(state->precomputed_uop_index, 0xff, sizeof(state->precomputed_uop_index));
            state->precomputed_uop_count = 0u;
            state->precomputed_uops_complete = false;
        }
        if (generic) state->batch_path_enabled = false;
        for (;;) {
            stpg_stream_chunk_result_t result;
            require_ok(stpg_buffer_stream_compare_replay_fill_next(state, &result));
            const uint32_t count = stpg_buffer_stream_compare_event_count();
            const uint32_t *indices = stpg_buffer_stream_compare_event_indices();
            const ExecEvent *compare_events = stpg_buffer_stream_compare_events();
            for (uint32_t i = 0; i < count; ++i) {
                if (fwrite(&indices[i], sizeof(uint32_t), 1u, metadata) != 1u
                    || fwrite(&compare_events[i], sizeof(ExecEvent), 1u, metadata) != 1u) return 1;
                metadata_crc = stpg_crc32_continue(metadata_crc, &indices[i], sizeof(uint32_t));
                metadata_crc = stpg_crc32_continue(metadata_crc, &compare_events[i], sizeof(ExecEvent));
            }
            metadata_count += count;
            if (result.eof) break;
            if (!result.event_count) return 1;
        }
        if (fclose(metadata) || metadata_count != pio_samples) return 1;
    }
#else
    if (compare_path) return 2;
#endif
    printf("],\"events\":%u,\"samples\":%u,\"pio_samples\":%u,\"long_waits\":%u,\"nonboundary_long_waits\":%u,"
           "\"uop_hits\":%u,\"uop_fallbacks\":%u,\"uop_builds\":%u,\"pio_crc32\":%u,"
           "\"metadata_count\":%u,\"metadata_crc32\":%u,\"fill_calls\":%u,\"terminal_empty_eof\":%s,"
           "\"batch_path_enabled\":%s,\"physical_io_performed\":false}\n",
           events, samples, pio_samples, long_waits, nonboundary_waits,
           telemetry.precomputed_uop_cache_hit_count, telemetry.precomputed_uop_cache_fallback_count,
           telemetry.precomputed_uop_cache_build_count, pio_crc, metadata_count, metadata_crc,
           fill_calls, terminal_empty_eof ? "true" : "false", batch_path ? "true" : "false");
    return 0;
}
