#include "stpg_pattern_buffer.h"

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "stpg_board.h"
#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_pattern.h"
#include "stpg_psram.h"
#include "stpg_protocol.h"
#if STPG_STREAM_LOOP_BATCH && STPG_STREAM_PSRAM_COMPARE_SAMPLE
#include "f311_pattern_hex.h"
#endif
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP
#include "stpg_compare.h"
#endif
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
#include "stpg_samples.h"
#endif

typedef struct {
    RateVectorHeader header;
    RateDesc rate;
    RateSlot slots[2];
    LoopBlockHeader loop_header;
    VectorRecord loop_body[2];
    LoopRefRecord loop_ref;
} loop_fixture_t;

typedef struct {
    RateVectorHeader header;
    RateDesc rate;
    RateSlot slot;
    LoopBlockHeader loop_header;
    VectorRecord loop_body[2];
    LoopRefRecord loop_ref;
} boundary_fixture_t;

#if STPG_STREAM_LOOP_BATCH && (STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP)
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
typedef struct {
    RateVectorHeader header;
    RateDesc rate;
    RateSlot slots[2];
    LoopBlockHeader loop_header;
    VectorRecord loop_body[8];
    VectorRecord top[3];
} long_loop_fixture_t;
#endif

typedef struct {
    RateVectorHeader header;
    RateDesc rates[4];
    RateSlot slots[6];
    LoopBlockHeader loop_header;
    VectorRecord loop_body[16];
    VectorRecord top[5];
} mixed_group_loop_fixture_t;

typedef struct {
    uint64_t word_hash;
    uint64_t metadata_hash;
    uint32_t events;
    uint32_t samples;
    uint32_t chunks;
} stream_digest_t;
#endif

typedef struct {
    RateVectorHeader header;
    RateDesc rate;
    RateSlot slot;
    VectorRecord vector;
} direct_capacity_fixture_t;

static int load_fixture_psram(const void *fixture, uint32_t total_size);

static int expect(stpg_err_t actual, stpg_err_t expected) {
    return actual == expected ? 0 : 1;
}

static void finalize_header_crc(RateVectorHeader *header) {
    header->header_crc32 = stpg_crc32(header, STPG_RATEVEC_HEADER_SIZE - sizeof(header->header_crc32));
}

static uint32_t compute_total_crc(const void *fixture, uint32_t total_size) {
    return stpg_crc32(fixture, total_size);
}

static int begin_write_psram_upload(const void *pattern, uint32_t size, uint32_t total_crc) {
    if (expect(stpg_buffer_begin(STPG_STORAGE_PSRAM, size, total_crc, 0u), ERR_NONE)) return 1;
    const uint8_t *bytes = (const uint8_t *)pattern;
    for (uint32_t offset = 0u; offset < size;) {
        uint32_t chunk_size = size - offset;
        if (chunk_size > STPG_UPLOAD_CHUNK_SIZE) chunk_size = STPG_UPLOAD_CHUNK_SIZE;
        if (expect(stpg_buffer_write_chunk(offset, bytes + offset, chunk_size,
                                           stpg_crc32(bytes + offset, chunk_size)), ERR_NONE)) {
            return 1;
        }
        offset += chunk_size;
    }
    return 0;
}

static void finalize_data_crc_generic(void *fixture, RateVectorHeader *header, uint32_t total_size) {
    const uint8_t *payload = ((const uint8_t *)fixture) + header->rate_table_offset;
    const uint32_t payload_size = total_size - header->rate_table_offset;
    header->data_crc32 = stpg_crc32(payload, payload_size);
    finalize_header_crc(header);
}

static uint32_t build_direct_capacity_fixture(direct_capacity_fixture_t *fixture,
                                              uint16_t repeat) {
    memset(fixture, 0, sizeof(*fixture));
    fixture->header.magic = STPG_RATEVEC_MAGIC;
    fixture->header.version = STPG_PATTERN_VERSION;
    fixture->header.header_size = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.active_pins = STPG_ACTIVE_PINS_V01;
    fixture->header.tick_hz = STPG_TICK_HZ;
    fixture->header.rate_count = 1u;
    fixture->header.slot_count = 1u;
    fixture->header.vector_count = 1u;
    fixture->header.rate_table_offset = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.slot_table_offset = fixture->header.rate_table_offset + STPG_RATE_DESC_SIZE;
    fixture->header.vector_offset = fixture->header.slot_table_offset + STPG_RATE_SLOT_SIZE;

    fixture->rate.period_ticks = 24u;
    fixture->rate.slot_count = 1u;
    fixture->rate.slot_offset = 0u;
    fixture->slot.time_ticks = 0u;
    fixture->slot.flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    fixture->slot.drive_mask = 0x0003u;
    fixture->slot.dir_mask = 0x0003u;
    fixture->vector.rate_id = 0u;
    fixture->vector.repeat = repeat;
    fixture->vector.drive_value = 0x0001u;
    fixture->vector.drive_enable = 0x0003u;

    const uint32_t total_size = fixture->header.vector_offset + STPG_VECTOR_RECORD_SIZE;
    finalize_data_crc_generic(fixture, &fixture->header, total_size);
    return total_size;
}

static int test_direct_expansion_accepts_exact_event_capacity(void) {
    direct_capacity_fixture_t fixture;
    const uint32_t total_size = build_direct_capacity_fixture(
        &fixture, (uint16_t)STPG_MAX_EVENT_COUNT);

    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, total_size)) return 1;
    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) return 1;
    if (stpg_buffer_event_count() != STPG_MAX_EVENT_COUNT
        || stpg_buffer_pio_word_count() != STPG_MAX_EVENT_COUNT * 4u) {
        fprintf(stderr, "exact direct capacity mismatch: events=%u words=%u\n",
                (unsigned)stpg_buffer_event_count(),
                (unsigned)stpg_buffer_pio_word_count());
        return 1;
    }

    const uint32_t over_size = build_direct_capacity_fixture(
        &fixture, (uint16_t)(STPG_MAX_EVENT_COUNT + 1u));
    const uint32_t total_crc = compute_total_crc(&fixture, over_size);
    stpg_buffer_reset();
    if (expect(stpg_buffer_begin(STPG_STORAGE_PSRAM, over_size, total_crc, 0u), ERR_NONE)
        || expect(stpg_buffer_write_chunk(0u, &fixture, over_size,
                                          stpg_crc32(&fixture, over_size)), ERR_NONE)) {
        return 1;
    }
    stpg_err_t err = stpg_buffer_finalize(over_size, total_crc);
    if (err == ERR_NONE) err = stpg_buffer_prepare_for_run();
    if (expect(err, ERR_BAD_SIZE)) {
        fprintf(stderr, "direct expansion did not reject event count above capacity\n");
        return 1;
    }
    return 0;
}

static int test_small_psram_upload_finalizes_from_verified_shadow(void) {
    direct_capacity_fixture_t fixture;
    const uint16_t repeat = 17u;
    const uint32_t size = build_direct_capacity_fixture(&fixture, repeat);
    const uint32_t total_crc = compute_total_crc(&fixture, size);

    stpg_buffer_reset();
    if (begin_write_psram_upload(&fixture, size, total_crc)) return 1;

    stpg_psram_host_reset_read_stats();
    if (expect(stpg_buffer_finalize(size, total_crc), ERR_NONE)) return 1;
    const stpg_psram_host_read_stats_t stats = stpg_psram_host_get_read_stats();
    if (stats.read_calls != 0u || stats.checksum_calls != 1u) {
        fprintf(stderr, "small PSRAM finalize reread storage: reads=%u checksums=%u\n",
                (unsigned)stats.read_calls, (unsigned)stats.checksum_calls);
        return 1;
    }
    uint32_t expected_status_events = 1u;
#if STPG_STREAM_STATUS_LOGICAL_EVENTS
    expected_status_events = repeat;
#endif
    if (stpg_buffer_active_source() != STPG_PATTERN_SOURCE_PSRAM
        || stpg_buffer_event_count() != expected_status_events) {
        fprintf(stderr, "small PSRAM finalize changed source/estimate: source=%u events=%u\n",
                (unsigned)stpg_buffer_active_source(),
                (unsigned)stpg_buffer_event_count());
        return 1;
    }
    stpg_rate_info_t info;
    if (expect(stpg_buffer_get_rate_info(&info), ERR_NONE)
        || !info.loaded || info.vector_count != 1u || info.rate_count != 1u
        || info.expanded_vector_count_estimate != repeat) {
        fprintf(stderr, "small PSRAM finalize rate metadata mismatch\n");
        return 1;
    }

    /* The PSRAM Rate Vector validator accepts the STPG explicit-wait-P4
     * extension; the generic SRAM validator deliberately does not. Finalizing
     * from the bounded shadow must preserve that PSRAM behavior. */
    const uint32_t explicit_wait_size = build_direct_capacity_fixture(&fixture, repeat);
    fixture.header.flags = STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4;
    fixture.vector.flags = STPG_VECTOR_FLAG_EXPLICIT_WAIT_P4_END;
    finalize_data_crc_generic(&fixture, &fixture.header, explicit_wait_size);
    const uint32_t explicit_wait_crc = compute_total_crc(&fixture, explicit_wait_size);
    stpg_buffer_reset();
    if (begin_write_psram_upload(&fixture, explicit_wait_size, explicit_wait_crc)) return 1;
    stpg_psram_host_reset_read_stats();
    if (expect(stpg_buffer_finalize(explicit_wait_size, explicit_wait_crc), ERR_NONE)) {
        fprintf(stderr, "explicit-wait-P4 Rate Vector rejected by shadow finalizer\n");
        return 1;
    }
    const stpg_psram_host_read_stats_t explicit_wait_stats = stpg_psram_host_get_read_stats();
    if (explicit_wait_stats.read_calls != 0u || explicit_wait_stats.checksum_calls != 1u) {
        fprintf(stderr, "explicit-wait-P4 finalization PSRAM verification mismatch\n");
        return 1;
    }

    /* A semantically corrupt image with internally recomputed transport CRC
     * must still fail on the SRAM validation path, before any PSRAM reread. */
    fixture.header.data_crc32 ^= 1u;
    finalize_header_crc(&fixture.header);
    const uint32_t corrupt_crc = compute_total_crc(&fixture, size);
    stpg_buffer_reset();
    if (begin_write_psram_upload(&fixture, size, corrupt_crc)) return 1;
    stpg_psram_host_reset_read_stats();
    if (expect(stpg_buffer_finalize(size, corrupt_crc), ERR_RATEVEC_BAD_CRC)) return 1;
    const stpg_psram_host_read_stats_t corrupt_stats = stpg_psram_host_get_read_stats();
    if (corrupt_stats.read_calls != 0u || corrupt_stats.checksum_calls != 1u) {
        fprintf(stderr, "corrupt shadow validation PSRAM verification mismatch\n");
        return 1;
    }

    /* Mutate the backing PSRAM after its upload-chunk readback verification,
     * but before UPLOAD_END.  The SRAM mirror still contains the valid source
     * bytes; finalization must reject the corrupted stored image rather than
     * accepting the mirror and later executing different bytes. */
    const uint32_t backing_corrupt_size = build_direct_capacity_fixture(&fixture, repeat);
    const uint32_t backing_corrupt_crc = compute_total_crc(&fixture, backing_corrupt_size);
    stpg_buffer_reset();
    if (begin_write_psram_upload(&fixture, backing_corrupt_size, backing_corrupt_crc)) return 1;
    uint8_t *stored_pattern = (uint8_t *)stpg_psram_mapped_ptr(
        STPG_PSRAM_PATTERN_BASE, backing_corrupt_size);
    if (stored_pattern == NULL) {
        fprintf(stderr, "host PSRAM mapped pointer unavailable for corruption injection\n");
        return 1;
    }
    stored_pattern[fixture.header.vector_offset + offsetof(VectorRecord, drive_value)] ^= 1u;
    stpg_psram_host_reset_read_stats();
    if (expect(stpg_buffer_finalize(backing_corrupt_size, backing_corrupt_crc), ERR_BAD_CRC)) {
        fprintf(stderr, "post-write PSRAM corruption was not rejected at finalize\n");
        return 1;
    }
    const stpg_psram_host_read_stats_t backing_corrupt_stats = stpg_psram_host_get_read_stats();
    if (stpg_buffer_has_pattern() || backing_corrupt_stats.checksum_calls != 1u) {
        fprintf(stderr, "corrupt PSRAM image was published or was not rechecked\n");
        return 1;
    }

    static uint8_t exact_shadow_pattern[STPG_MAX_PATTERN_BYTES];
    static uint8_t over_shadow_pattern[STPG_MAX_PATTERN_BYTES + 1u];
    memset(exact_shadow_pattern, 0, sizeof(exact_shadow_pattern));
    memset(over_shadow_pattern, 0, sizeof(over_shadow_pattern));
    const uint32_t base_size = build_direct_capacity_fixture(&fixture, repeat);
    memcpy(exact_shadow_pattern, &fixture, base_size);
    memcpy(over_shadow_pattern, &fixture, base_size);
    const uint32_t exact_shadow_crc = compute_total_crc(
        exact_shadow_pattern, (uint32_t)sizeof(exact_shadow_pattern));
    stpg_buffer_reset();
    if (begin_write_psram_upload(exact_shadow_pattern,
                                 (uint32_t)sizeof(exact_shadow_pattern), exact_shadow_crc)) {
        return 1;
    }
    stpg_psram_host_reset_read_stats();
    if (expect(stpg_buffer_finalize((uint32_t)sizeof(exact_shadow_pattern), exact_shadow_crc), ERR_NONE)) {
        return 1;
    }
    const stpg_psram_host_read_stats_t exact_stats = stpg_psram_host_get_read_stats();
    if (exact_stats.read_calls != 0u || exact_stats.checksum_calls != 1u) {
        fprintf(stderr, "max-capacity shadow finalize verification mismatch: reads=%u checksums=%u\n",
                (unsigned)exact_stats.read_calls, (unsigned)exact_stats.checksum_calls);
        return 1;
    }

    const uint32_t over_shadow_crc = compute_total_crc(over_shadow_pattern,
                                                       (uint32_t)sizeof(over_shadow_pattern));
    stpg_buffer_reset();
    if (begin_write_psram_upload(over_shadow_pattern,
                                 (uint32_t)sizeof(over_shadow_pattern), over_shadow_crc)) {
        return 1;
    }
    stpg_psram_host_reset_read_stats();
    if (expect(stpg_buffer_finalize((uint32_t)sizeof(over_shadow_pattern), over_shadow_crc), ERR_NONE)) {
        return 1;
    }
    const stpg_psram_host_read_stats_t fallback_stats = stpg_psram_host_get_read_stats();
    if (fallback_stats.read_calls == 0u || fallback_stats.checksum_calls == 0u) {
        fprintf(stderr, "PSRAM upload above shadow capacity did not retain readback validation\n");
        return 1;
    }
    return 0;
}

static uint32_t build_loop_fixture(loop_fixture_t *fixture, int compare_free) {
    memset(fixture, 0, sizeof(*fixture));

    fixture->rate.period_ticks = 24u;
    fixture->rate.slot_count = 2u;
    fixture->rate.slot_offset = 0u;

    fixture->slots[0].time_ticks = 0u;
    fixture->slots[0].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    fixture->slots[0].drive_mask = 0x0003u;
    fixture->slots[0].dir_mask = 0x0003u;

    fixture->slots[1].time_ticks = 12u;
    fixture->slots[1].flags = compare_free ? STPG_DRIVE_ZERO : (STPG_DRIVE_ZERO | STPG_RATE_SLOT_SAMPLE);
    fixture->slots[1].drive_mask = 0x0003u;
    fixture->slots[1].sample_mask = compare_free ? 0u : 0x0003u;

    fixture->loop_header.loop_count = 2u;
    fixture->loop_header.body_record_count = 2u;

    fixture->loop_body[0].rate_id = 0u;
    fixture->loop_body[0].repeat = 1u;
    fixture->loop_body[0].drive_value = 0x0001u;
    fixture->loop_body[0].drive_enable = 0x0003u;
    fixture->loop_body[0].expect_value = compare_free ? 0u : 0x0001u;
    fixture->loop_body[0].expect_mask = compare_free ? 0u : 0x0003u;

    fixture->loop_body[1].rate_id = 0u;
    fixture->loop_body[1].repeat = 1u;
    fixture->loop_body[1].drive_value = 0x0002u;
    fixture->loop_body[1].drive_enable = 0x0003u;
    fixture->loop_body[1].expect_value = compare_free ? 0u : 0x0002u;
    fixture->loop_body[1].expect_mask = compare_free ? 0u : 0x0003u;

    fixture->loop_ref.flags = STPG_VECTOR_FLAG_LOOP_REF;
    fixture->loop_ref.loop_id = 0u;

    fixture->header.magic = STPG_RATEVEC_MAGIC;
    fixture->header.version = STPG_PATTERN_VERSION;
    fixture->header.header_size = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.flags = STPG_RATEVEC_FLAG_HAS_LOOP;
    fixture->header.active_pins = STPG_ACTIVE_PINS_V01;
    fixture->header.tick_hz = STPG_TICK_HZ;
    fixture->header.rate_count = 1u;
    fixture->header.slot_count = 2u;
    fixture->header.vector_count = 1u;
    fixture->header.rate_table_offset = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.slot_table_offset = STPG_RATEVEC_HEADER_SIZE + STPG_RATE_DESC_SIZE;
    fixture->header.reserved0 = fixture->header.slot_table_offset + (2u * STPG_RATE_SLOT_SIZE);
    fixture->header.vector_offset = fixture->header.reserved0 + STPG_LOOP_BLOCK_HEADER_SIZE + (2u * STPG_VECTOR_RECORD_SIZE);

    const uint32_t total_size = fixture->header.vector_offset + STPG_VECTOR_RECORD_SIZE;
    finalize_data_crc_generic(fixture, &fixture->header, total_size);
    return total_size;
}

static uint32_t build_streaming_boundary_fixture(boundary_fixture_t *fixture, uint32_t loop_count,
                                                 uint16_t repeat0, uint16_t repeat1) {
    memset(fixture, 0, sizeof(*fixture));

    fixture->rate.period_ticks = 16u;
    fixture->rate.slot_count = 1u;
    fixture->rate.slot_offset = 0u;

    fixture->slot.time_ticks = 0u;
    fixture->slot.flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    fixture->slot.drive_mask = 0x0003u;
    fixture->slot.dir_mask = 0x0003u;

    fixture->loop_header.loop_count = loop_count;
    fixture->loop_header.body_record_count = 2u;
    fixture->loop_body[0].rate_id = 0u;
    fixture->loop_body[0].repeat = repeat0;
    fixture->loop_body[0].drive_value = 0x0001u;
    fixture->loop_body[0].drive_enable = 0x0003u;
    fixture->loop_body[1].rate_id = 0u;
    fixture->loop_body[1].repeat = repeat1;
    fixture->loop_body[1].drive_value = 0x0002u;
    fixture->loop_body[1].drive_enable = 0x0003u;

    fixture->loop_ref.flags = STPG_VECTOR_FLAG_LOOP_REF;
    fixture->loop_ref.loop_id = 0u;

    fixture->header.magic = STPG_RATEVEC_MAGIC;
    fixture->header.version = STPG_PATTERN_VERSION;
    fixture->header.header_size = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.flags = STPG_RATEVEC_FLAG_HAS_LOOP;
    fixture->header.active_pins = STPG_ACTIVE_PINS_V01;
    fixture->header.tick_hz = STPG_TICK_HZ;
    fixture->header.rate_count = 1u;
    fixture->header.slot_count = 1u;
    fixture->header.vector_count = 1u;
    fixture->header.rate_table_offset = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.slot_table_offset = STPG_RATEVEC_HEADER_SIZE + STPG_RATE_DESC_SIZE;
    fixture->header.reserved0 = fixture->header.slot_table_offset + STPG_RATE_SLOT_SIZE;
    fixture->header.vector_offset = fixture->header.reserved0 + STPG_LOOP_BLOCK_HEADER_SIZE
        + (2u * STPG_VECTOR_RECORD_SIZE);

    const uint32_t total_size = fixture->header.vector_offset + STPG_VECTOR_RECORD_SIZE;
    finalize_data_crc_generic(fixture, &fixture->header, total_size);
    return total_size;
}

#if STPG_STREAM_LOOP_BATCH && (STPG_STREAM_PSRAM_COMPARE_SAMPLE || STPG_STREAM_FAIL_STOP)
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
static uint32_t build_long_loop_fixture(long_loop_fixture_t *fixture, uint32_t loop_count) {
    memset(fixture, 0, sizeof(*fixture));
    fixture->rate.period_ticks = 750u;
    fixture->rate.slot_count = 2u;
    fixture->slots[0].time_ticks = 0u;
    fixture->slots[0].flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE;
    fixture->slots[0].drive_mask = 0x00FFFFFFu;
    fixture->slots[0].dir_mask = 0x00FFFFFFu;
    fixture->slots[1].time_ticks = 15u;
    fixture->slots[1].flags = STPG_DRIVE_NONE | STPG_RATE_SLOT_SAMPLE;
    fixture->slots[1].sample_mask = 0x00F00000u;
    fixture->loop_header.loop_count = loop_count;
    fixture->loop_header.body_record_count = 8u;
    for (uint32_t index = 0u; index < 8u; ++index) {
        fixture->loop_body[index].rate_id = 0u;
        fixture->loop_body[index].repeat = 1u;
        fixture->loop_body[index].drive_value = index & 1u ? 0x55u : 0xAAu;
        fixture->loop_body[index].drive_enable = 0x00FFFFFFu;
        fixture->loop_body[index].expect_mask = 0x00F00000u;
        fixture->loop_body[index].expect_value = index & 1u ? 0x00100000u : 0u;
    }
    fixture->top[0] = fixture->loop_body[0];
    fixture->top[2] = fixture->loop_body[1];
    LoopRefRecord ref = {.loop_id = 0u, .flags = STPG_VECTOR_FLAG_LOOP_REF};
    memcpy(&fixture->top[1], &ref, sizeof(ref));

    fixture->header.magic = STPG_RATEVEC_MAGIC;
    fixture->header.version = STPG_PATTERN_VERSION;
    fixture->header.header_size = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.flags = STPG_RATEVEC_FLAG_HAS_LOOP;
    fixture->header.active_pins = STPG_ACTIVE_PINS_V11;
    fixture->header.tick_hz = STPG_TICK_HZ;
    fixture->header.rate_count = 1u;
    fixture->header.slot_count = 2u;
    fixture->header.vector_count = 3u;
    fixture->header.rate_table_offset = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.slot_table_offset = STPG_RATEVEC_HEADER_SIZE + STPG_RATE_DESC_SIZE;
    fixture->header.reserved0 = fixture->header.slot_table_offset + (2u * STPG_RATE_SLOT_SIZE);
    fixture->header.vector_offset = fixture->header.reserved0 + STPG_LOOP_BLOCK_HEADER_SIZE
        + (8u * STPG_VECTOR_RECORD_SIZE);
    const uint32_t total_size = fixture->header.vector_offset + (3u * STPG_VECTOR_RECORD_SIZE);
    finalize_data_crc_generic(fixture, &fixture->header, total_size);
    return total_size;
}
#endif

static uint32_t build_mixed_group_loop_fixture(mixed_group_loop_fixture_t *fixture,
                                               uint32_t body_record_count,
                                               bool all_two_group_rates,
                                               uint32_t loop_count) {
    if (fixture == NULL || body_record_count < 2u || body_record_count > 16u
        || (body_record_count & 1u) != 0u) {
        return 0u;
    }
    memset(fixture, 0, sizeof(*fixture));

    static const uint8_t rate_slot_counts[4] = {1u, 2u, 1u, 2u};
    static const uint8_t rate_slot_offsets[4] = {0u, 1u, 3u, 4u};
    for (uint32_t index = 0u; index < 4u; ++index) {
        fixture->rates[index].period_ticks = 750u;
        fixture->rates[index].slot_count = rate_slot_counts[index];
        fixture->rates[index].slot_offset = rate_slot_offsets[index];
    }

    const uint32_t drive_mask = 0x00FFFFFFu;
    const uint32_t sample_mask = 0x00F00000u;
    fixture->slots[0] = (RateSlot){
        .time_ticks = 0u,
        .flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
        .drive_mask = drive_mask,
        .dir_mask = drive_mask,
    };
    fixture->slots[1] = fixture->slots[0];
    fixture->slots[2] = (RateSlot){
        .time_ticks = 15u,
        .flags = STPG_DRIVE_ZERO,
        .drive_mask = 0x00000001u,
    };
    fixture->slots[3] = (RateSlot){
        .time_ticks = 0u,
        .flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE | STPG_RATE_SLOT_SAMPLE,
        .drive_mask = drive_mask,
        .dir_mask = drive_mask,
        .sample_mask = sample_mask,
    };
    fixture->slots[4] = (RateSlot){
        .time_ticks = 0u,
        .flags = STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE,
        .drive_mask = drive_mask,
        .dir_mask = drive_mask,
    };
    fixture->slots[5] = (RateSlot){
        .time_ticks = 15u,
        .flags = STPG_DRIVE_NONE | STPG_RATE_SLOT_SAMPLE,
        .sample_mask = sample_mask,
    };
    fixture->loop_header.loop_count = loop_count;
    fixture->loop_header.body_record_count = body_record_count;
    for (uint32_t index = 0u; index < body_record_count; ++index) {
        fixture->loop_body[index].rate_id = all_two_group_rates
            ? 3u : (uint8_t)(2u + (index & 1u));
        fixture->loop_body[index].repeat = 1u;
        fixture->loop_body[index].drive_value = index & 1u ? 0x55u : 0xAAu;
        fixture->loop_body[index].drive_enable = drive_mask;
        fixture->loop_body[index].expect_mask = sample_mask;
        fixture->loop_body[index].expect_value = index & 1u ? 0x00100000u : 0u;
    }

    fixture->top[0].rate_id = 0u;
    fixture->top[0].repeat = 1u;
    fixture->top[0].drive_enable = drive_mask;
    fixture->top[1] = fixture->top[0];
    fixture->top[2].rate_id = 1u;
    fixture->top[2].repeat = 1u;
    fixture->top[2].drive_enable = drive_mask;
    fixture->top[3] = fixture->top[2];
    LoopRefRecord loop_ref = {.loop_id = 0u, .flags = STPG_VECTOR_FLAG_LOOP_REF};
    memcpy(&fixture->top[4], &loop_ref, sizeof(loop_ref));

    fixture->header.magic = STPG_RATEVEC_MAGIC;
    fixture->header.version = STPG_PATTERN_VERSION;
    fixture->header.header_size = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.flags = STPG_RATEVEC_FLAG_HAS_LOOP;
    fixture->header.active_pins = STPG_ACTIVE_PINS_V11;
    fixture->header.tick_hz = STPG_TICK_HZ;
    fixture->header.rate_count = 4u;
    fixture->header.slot_count = 6u;
    fixture->header.vector_count = 5u;
    fixture->header.rate_table_offset = STPG_RATEVEC_HEADER_SIZE;
    fixture->header.slot_table_offset = fixture->header.rate_table_offset + 4u * STPG_RATE_DESC_SIZE;
    fixture->header.reserved0 = fixture->header.slot_table_offset + 6u * STPG_RATE_SLOT_SIZE;
    fixture->header.vector_offset = fixture->header.reserved0 + STPG_LOOP_BLOCK_HEADER_SIZE
        + body_record_count * STPG_VECTOR_RECORD_SIZE;

    const uint32_t total_size = fixture->header.vector_offset + 5u * STPG_VECTOR_RECORD_SIZE;
    /* The fixture reserves room for 16 body records, but the wire format places
       top-level records immediately after the actual body length. */
    memmove((uint8_t *)fixture + fixture->header.vector_offset,
            fixture->top,
            5u * STPG_VECTOR_RECORD_SIZE);
    finalize_data_crc_generic(fixture, &fixture->header, total_size);
    return total_size;
}

static uint64_t digest_word(uint64_t hash, uint32_t word) {
    return (hash ^ word) * UINT64_C(1099511628211);
}

static int run_long_loop_stream(bool force_generic, uint32_t chunk_events, stream_digest_t *out) {
    stpg_rate_stream_state_t state;
    stpg_err_t prepare_error = stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC);
    if (expect(prepare_error, ERR_NONE)) {
        fprintf(stderr, "long loop prepare error=%u\n", (unsigned)prepare_error);
        return 1;
    }
    if (!state.batch_path_enabled) {
        fprintf(stderr, "long loop batch eligibility missing\n");
        return 1;
    }
    if (force_generic) state.batch_path_enabled = false;
    memset(out, 0, sizeof(*out));
    out->word_hash = UINT64_C(1469598103934665603);
    out->metadata_hash = UINT64_C(1469598103934665603);
    uint32_t chunk_words[1024u * 4u];
    const clock_t started = clock();
    while (true) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_compare_begin_metadata_slot(0u), ERR_NONE)) return 1;
        stpg_err_t fill_error = stpg_buffer_stream_fill_next_chunk(&state, chunk_words, chunk_events, &result);
        if (expect(fill_error, ERR_NONE)) {
            fprintf(stderr, "long loop fill error=%u chunk=%u batch=%u\n",
                    (unsigned)fill_error, (unsigned)out->chunks, force_generic ? 0u : 1u);
            return 1;
        }
        if (result.event_count == 0u && !result.eof) return 1;
        for (uint32_t index = 0u; index < result.word_count; ++index) {
            out->word_hash = digest_word(out->word_hash, chunk_words[index]);
        }
        const uint32_t metadata_count = stpg_buffer_stream_compare_event_count();
#if STPG_STREAM_FAIL_STOP
        if (metadata_count != result.sample_event_count) return 1;
        const uint32_t *metadata_indices = stpg_buffer_stream_compare_event_indices();
        const ExecEvent *metadata_events = stpg_buffer_stream_compare_events();
        for (uint32_t index = 0u; index < metadata_count; ++index) {
            const ExecEvent *const event = &metadata_events[index];
            if (metadata_indices[index] < out->events
                || metadata_indices[index] >= out->events + result.event_count
                || (event->flags & STPG_EVF_SAMPLE_POINT) == 0u
                || ((event->flags & STPG_EVF_COMPARE_POINT) != 0u)
                    != (event->sample_mask != 0u)) {
                return 1;
            }
            out->metadata_hash = digest_word(out->metadata_hash, metadata_indices[index]);
            out->metadata_hash = digest_word(out->metadata_hash, event->flags);
            out->metadata_hash = digest_word(out->metadata_hash, event->sample_mask);
            out->metadata_hash = digest_word(out->metadata_hash, event->expect_value);
        }
#else
        if (metadata_count != 0u) return 1;
#endif
        out->events += result.event_count;
        out->samples += result.sample_event_count;
        out->chunks += 1u;
        if (result.eof) break;
        if (out->chunks > (131100u / chunk_events) + 2u) return 1;
    }
    fprintf(stderr, "long loop %s host_cpu_ms=%.3f\n", force_generic ? "generic" : "batch",
            1000.0 * (double)(clock() - started) / (double)CLOCKS_PER_SEC);
    return 0;
}

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
static int verify_long_loop_replay(uint32_t expected_events, uint32_t expected_samples) {
    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_stream_compare_replay_init(&state), ERR_NONE)
        || state.batch_path_enabled) return 1;
    uint32_t total_events = 0u;
    uint32_t total_samples = 0u;
    for (uint32_t chunk = 0u; chunk < 200u; ++chunk) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_compare_begin_metadata_slot(0u), ERR_NONE)
            || expect(stpg_buffer_stream_compare_replay_fill_next(&state, &result), ERR_NONE)) return 1;
        const uint32_t metadata_count = stpg_buffer_stream_compare_event_count();
        if (metadata_count != result.sample_event_count) return 1;
        const uint32_t *indices = stpg_buffer_stream_compare_event_indices();
        const ExecEvent *events = stpg_buffer_stream_compare_events();
        for (uint32_t index = 0u; index < metadata_count; ++index) {
            if (indices[index] >= expected_events || (events[index].flags & STPG_EVF_SAMPLE_POINT) == 0u)
                return 1;
        }
        total_events += result.event_count;
        total_samples += result.sample_event_count;
        if (result.eof) return total_events == expected_events && total_samples == expected_samples ? 0 : 1;
    }
    return 1;
}
#endif

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
static int test_long_loop_batch_matches_generic(void) {
    long_loop_fixture_t fixture;
    const uint32_t total_size = build_long_loop_fixture(&fixture, 8192u);
    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, total_size)) return 1;
    stream_digest_t batch;
    stream_digest_t generic;
    if (run_long_loop_stream(false, 1024u, &batch)
        || run_long_loop_stream(true, 1024u, &generic)) return 1;
    if (batch.events != 131076u || batch.samples != 65538u
        || memcmp(&batch, &generic, sizeof(batch)) != 0) {
        fprintf(stderr, "long loop mismatch: batch events=%u samples=%u chunks=%u; generic events=%u samples=%u chunks=%u\n",
                (unsigned)batch.events, (unsigned)batch.samples, (unsigned)batch.chunks,
                (unsigned)generic.events, (unsigned)generic.samples, (unsigned)generic.chunks);
        return 1;
    }
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
    return verify_long_loop_replay(131076u, 65538u);
#else
    return 0;
#endif
}

static int test_mixed_group_loop_batch_matches_generic(void) {
    static const uint32_t body_record_counts[] = {2u, 4u, 8u, 16u};
    for (uint32_t fixture_index = 0u;
         fixture_index < sizeof(body_record_counts) / sizeof(body_record_counts[0]);
         ++fixture_index) {
        const uint32_t body_record_count = body_record_counts[fixture_index];
        const uint32_t loop_count = 4096u;
        const uint32_t expected_events = 6u + (body_record_count / 2u) * 3u * loop_count;
        const uint32_t expected_samples = body_record_count * loop_count;
        mixed_group_loop_fixture_t fixture;
        const uint32_t total_size = build_mixed_group_loop_fixture(
            &fixture, body_record_count, false, loop_count);
        if (total_size == 0u) return 1;

        stpg_buffer_reset();
        if (load_fixture_psram(&fixture, total_size)) return 1;
        stream_digest_t batch;
        if (run_long_loop_stream(false, 1024u, &batch)
            || batch.events != expected_events || batch.samples != expected_samples) {
            fprintf(stderr,
                    "mixed-group batch count mismatch: body=%u events=%u/%u samples=%u/%u\n",
                    (unsigned)body_record_count,
                    (unsigned)batch.events,
                    (unsigned)expected_events,
                    (unsigned)batch.samples,
                    (unsigned)expected_samples);
            return 1;
        }

        stpg_buffer_reset();
        if (load_fixture_psram(&fixture, total_size)) return 1;
        stream_digest_t generic;
        if (run_long_loop_stream(true, 1024u, &generic)
            || memcmp(&batch, &generic, sizeof(batch)) != 0) {
            fprintf(stderr, "mixed-group batch/generic mismatch: body=%u\n",
                    (unsigned)body_record_count);
            return 1;
        }
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
        if (verify_long_loop_replay(expected_events, expected_samples)) {
            fprintf(stderr, "mixed-group compare replay mismatch: body=%u\n",
                    (unsigned)body_record_count);
            return 1;
        }
#endif
    }

    /* Exercise the full 32-event template capacity, not just the F325 24-event body. */
    const uint32_t body_record_count = 16u;
    const uint32_t loop_count = 4096u;
    const uint32_t expected_events = 6u + body_record_count * 2u * loop_count;
    const uint32_t expected_samples = body_record_count * loop_count;
    mixed_group_loop_fixture_t fixture;
    const uint32_t total_size = build_mixed_group_loop_fixture(
        &fixture, body_record_count, true, loop_count);
    if (total_size == 0u) return 1;

    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, total_size)) return 1;
    stream_digest_t batch;
    if (run_long_loop_stream(false, 1024u, &batch)
        || batch.events != expected_events || batch.samples != expected_samples) {
        fprintf(stderr, "32-event loop template batch count mismatch\n");
        return 1;
    }

    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, total_size)) return 1;
    stream_digest_t generic;
    if (run_long_loop_stream(true, 1024u, &generic)
        || memcmp(&batch, &generic, sizeof(batch)) != 0) {
        fprintf(stderr, "32-event loop template/generic mismatch\n");
        return 1;
    }
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
    if (verify_long_loop_replay(expected_events, expected_samples)) return 1;
#endif

    return 0;
}
#endif

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
static int hex_nibble(char character) {
    if (character >= '0' && character <= '9') return character - '0';
    if (character >= 'a' && character <= 'f') return character - 'a' + 10;
    return -1;
}

static int test_pinned_f311_batch_matches_generic(void) {
    uint8_t pattern[476u];
    if (strlen(stpg_f311_pattern_hex) != sizeof(pattern) * 2u) return 1;
    for (uint32_t index = 0u; index < sizeof(pattern); ++index) {
        const int high = hex_nibble(stpg_f311_pattern_hex[index * 2u]);
        const int low = hex_nibble(stpg_f311_pattern_hex[index * 2u + 1u]);
        if (high < 0 || low < 0) return 1;
        pattern[index] = (uint8_t)((high << 4) | low);
    }
    stpg_buffer_reset();
    if (load_fixture_psram(pattern, sizeof(pattern))) return 1;
    stream_digest_t batch;
    stream_digest_t generic;
    if (run_long_loop_stream(false, 1024u, &batch)
        || run_long_loop_stream(true, 1024u, &generic)) return 1;
    if (batch.events != 131078u || batch.samples != 65536u
        || memcmp(&batch, &generic, sizeof(batch)) != 0) {
        fprintf(stderr, "F311 batch/generic mismatch: batch=%u/%u, generic=%u/%u\n",
                (unsigned)batch.events, (unsigned)batch.samples,
                (unsigned)generic.events, (unsigned)generic.samples);
        return 1;
    }
    return verify_long_loop_replay(131078u, 65536u);
}
#endif

#if STPG_STREAM_FAIL_STOP
static int test_fail_stop_loop_batch_metadata_matches_generic(void) {
    const uint32_t loop_count = 4096u;
    const uint32_t body_record_count = 16u;
    const uint32_t expected_events = 6u + 32u * loop_count;
    const uint32_t expected_samples = loop_count;
    mixed_group_loop_fixture_t fixture;
    const uint32_t size = build_mixed_group_loop_fixture(
        &fixture, body_record_count, true, loop_count);
    if (size == 0u) return 1;
    for (uint32_t index = 0u; index < body_record_count; ++index) {
        fixture.loop_body[index].rate_id = 1u; /* two drive-only groups */
    }
    fixture.loop_body[body_record_count - 1u].rate_id = 3u; /* one sample per cycle */
    finalize_data_crc_generic(&fixture, &fixture.header, size);

    stream_digest_t batch;
    stream_digest_t generic;
    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, size)
        || run_long_loop_stream(false, 1024u, &batch)) return 1;
    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, size)
        || run_long_loop_stream(true, 1024u, &generic)) return 1;

    if (batch.events != expected_events || batch.samples != expected_samples
        || memcmp(&batch, &generic, sizeof(batch)) != 0) {
        fprintf(stderr,
                "fail-stop batch metadata mismatch: events=%u/%u samples=%u/%u\n",
                (unsigned)batch.events, (unsigned)expected_events,
                (unsigned)batch.samples, (unsigned)expected_samples);
        return 1;
    }
    return 0;
}

static int run_fail_stop_loop_compare_probe(bool force_generic,
                                            bool inject_mismatch,
                                            uint32_t *out_injected_event) {
    const uint32_t loop_count = 4096u;
    const uint32_t body_record_count = 16u;
    const uint32_t expected_events = 6u + 32u * loop_count;
    const uint32_t expected_samples = loop_count;
    const uint32_t injected_sample = 17u;
    mixed_group_loop_fixture_t fixture;
    const uint32_t size = build_mixed_group_loop_fixture(
        &fixture, body_record_count, true, loop_count);
    if (size == 0u) return 1;
    for (uint32_t index = 0u; index < body_record_count; ++index) {
        fixture.loop_body[index].rate_id = 1u;
    }
    fixture.loop_body[body_record_count - 1u].rate_id = 3u;
    finalize_data_crc_generic(&fixture, &fixture.header, size);

    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, size)) return 1;
    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)
        || !state.batch_path_enabled) return 1;
    if (force_generic) state.batch_path_enabled = false;

    stpg_compare_reset();
    uint32_t events = 0u;
    uint32_t samples = 0u;
    uint32_t compare_count = 0u;
    uint32_t last_event_index = 0u;
    bool have_last_event = false;
    uint32_t injected_event = UINT32_MAX;
    uint32_t chunk_words[1024u * 4u];
    for (uint32_t chunk = 0u; chunk < 200u; ++chunk) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_compare_begin_metadata_slot(0u), ERR_NONE)
            || expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 1024u, &result), ERR_NONE)) {
            return 1;
        }
        const uint32_t metadata_count = stpg_buffer_stream_compare_event_count();
        if (metadata_count != result.sample_event_count) return 1;
        const uint32_t *indices = stpg_buffer_stream_compare_event_indices();
        const ExecEvent *metadata = stpg_buffer_stream_compare_events();
        for (uint32_t index = 0u; index < metadata_count; ++index) {
            const ExecEvent *const event = &metadata[index];
            const uint32_t event_index = indices[index];
            if ((event->flags & STPG_EVF_SAMPLE_POINT) == 0u
                || (event->flags & STPG_EVF_COMPARE_POINT) == 0u
                || event->sample_mask != 0x00F00000u
                || event->expect_value != 0x00100000u
                || event_index < events
                || event_index >= events + result.event_count
                || (have_last_event && event_index <= last_event_index)) {
                return 1;
            }
            uint32_t actual = event->expect_value;
            if (inject_mismatch && samples + index == injected_sample) {
                actual ^= 0x00100000u;
                injected_event = event_index;
            }
            (void)stpg_compare_check_value(
                event_index, actual, event->expect_value, event->sample_mask);
            last_event_index = event_index;
            have_last_event = true;
            compare_count += 1u;
        }
        events += result.event_count;
        samples += result.sample_event_count;
        if (result.eof) break;
        if (result.event_count == 0u || chunk == 199u) return 1;
    }

    if (events != expected_events || samples != expected_samples
        || compare_count != expected_samples
        || stpg_compare_fail_count() != (inject_mismatch ? 1u : 0u)
        || stpg_compare_result_flags() != (inject_mismatch ? STPG_RESULT_FLAG_COMPARE_MISMATCH : 0u)
        || (inject_mismatch && injected_event == UINT32_MAX)) {
        return 1;
    }
    if (inject_mismatch) {
        stpg_fail_log_read_view_t view;
        FailLogEntry entry;
        if (expect(stpg_compare_prepare_read(0u, 1u, &view), ERR_NONE)
            || view.element_count != 1u) return 1;
        stpg_compare_read_bytes(0u, (uint8_t *)&entry, sizeof(entry));
        if (entry.event_index != injected_event
            || entry.actual != 0u
            || entry.expected != 0x00100000u
            || entry.mask != 0x00F00000u) return 1;
    }
    if (out_injected_event != NULL) {
        *out_injected_event = injected_event;
    }
    return 0;
}

static int test_fail_stop_loop_compare_metadata_and_injected_mismatch(void) {
    for (uint32_t generic = 0u; generic < 2u; ++generic) {
        uint32_t clean_event = UINT32_MAX;
        uint32_t fail_event = UINT32_MAX;
        if (run_fail_stop_loop_compare_probe(generic != 0u, false, &clean_event)
            || clean_event != UINT32_MAX
            || run_fail_stop_loop_compare_probe(generic != 0u, true, &fail_event)
            || fail_event == UINT32_MAX) {
            fprintf(stderr, "fail-stop loop compare metadata/injection failed route=%s\n",
                    generic != 0u ? "generic" : "batch");
            return 1;
        }
    }
    return 0;
}

static int test_fail_stop_loop_batch_sample_only_metadata_matches_generic(void) {
    const uint32_t loop_count = 4096u;
    const uint32_t body_record_count = 16u;
    const uint32_t expected_events = 6u + 32u * loop_count;
    const uint32_t expected_samples = loop_count;
    mixed_group_loop_fixture_t fixture;
    const uint32_t size = build_mixed_group_loop_fixture(
        &fixture, body_record_count, true, loop_count);
    if (size == 0u) return 1;
    for (uint32_t index = 0u; index < body_record_count; ++index) {
        fixture.loop_body[index].rate_id = 1u;
        fixture.loop_body[index].expect_mask = 0u;
        fixture.loop_body[index].expect_value = 0u;
    }
    fixture.loop_body[body_record_count - 1u].rate_id = 3u;
    finalize_data_crc_generic(&fixture, &fixture.header, size);

    stream_digest_t batch;
    stream_digest_t generic;
    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, size)
        || run_long_loop_stream(false, 1024u, &batch)) return 1;
    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, size)
        || run_long_loop_stream(true, 1024u, &generic)) return 1;

    if (batch.events != expected_events || batch.samples != expected_samples
        || memcmp(&batch, &generic, sizeof(batch)) != 0) {
        fprintf(stderr,
                "fail-stop sample-only metadata mismatch: events=%u/%u samples=%u/%u\n",
                (unsigned)batch.events, (unsigned)expected_events,
                (unsigned)batch.samples, (unsigned)expected_samples);
        return 1;
    }
    return 0;
}

static int test_fail_stop_loop_sample_capacity_rejects_oversize(void) {
    const uint32_t loop_count = STPG_MAX_EVENT_COUNT + 1u;
    const uint32_t body_record_count = 16u;
    mixed_group_loop_fixture_t fixture;
    const uint32_t size = build_mixed_group_loop_fixture(
        &fixture, body_record_count, true, loop_count);
    if (size == 0u) return 1;
    for (uint32_t index = 0u; index < body_record_count; ++index) {
        fixture.loop_body[index].rate_id = 1u;
    }
    fixture.loop_body[body_record_count - 1u].rate_id = 3u;
    finalize_data_crc_generic(&fixture, &fixture.header, size);

    const uint32_t total_crc = compute_total_crc(&fixture, size);
    stpg_buffer_reset();
    if (expect(stpg_buffer_begin(STPG_STORAGE_PSRAM, size, total_crc, 0u), ERR_NONE)
        || expect(stpg_buffer_write_chunk(0u, &fixture, size, stpg_crc32(&fixture, size)), ERR_NONE)) {
        return 1;
    }
    return expect(stpg_buffer_finalize(size, total_crc), ERR_BAD_SIZE);
}
#endif

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
static int test_loop_batch_guard(void) {
    long_loop_fixture_t fixture;
    uint32_t size = build_long_loop_fixture(&fixture, 1024u);
    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, size)) return 1;
    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)
        || state.batch_path_enabled) return 1;

    size = build_long_loop_fixture(&fixture, 8192u);
    fixture.loop_body[0].repeat = 2u;
    finalize_data_crc_generic(&fixture, &fixture.header, size);
    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, size)) return 1;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)
        || state.batch_path_enabled) return 1;

    size = build_long_loop_fixture(&fixture, 8192u);
    LoopRefRecord duplicate_ref = {.loop_id = 0u, .flags = STPG_VECTOR_FLAG_LOOP_REF};
    memcpy(&fixture.top[2], &duplicate_ref, sizeof(duplicate_ref));
    finalize_data_crc_generic(&fixture, &fixture.header, size);
    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, size)) return 1;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)
        || state.batch_path_enabled) return 1;
    return 0;
}

static int test_loop_batch_partial_chunk_boundary(void) {
    long_loop_fixture_t fixture;
    const uint32_t size = build_long_loop_fixture(&fixture, 4096u);
    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, size)) return 1;
    stream_digest_t batch;
    stream_digest_t generic;
    if (run_long_loop_stream(false, 17u, &batch)
        || run_long_loop_stream(true, 17u, &generic)) return 1;
    return batch.events == 65540u && batch.samples == 32770u
        && memcmp(&batch, &generic, sizeof(batch)) == 0 ? 0 : 1;
}
#endif
#endif

static int load_fixture_psram(const void *fixture, uint32_t total_size) {
    const uint32_t total_crc = compute_total_crc(fixture, total_size);
    stpg_err_t begin_error = stpg_buffer_begin(STPG_STORAGE_PSRAM, total_size, total_crc, 0u);
    if (expect(begin_error, ERR_NONE)) {
        fprintf(stderr, "load_fixture_psram begin error=%u\n", (unsigned)begin_error);
        return 1;
    }
    const uint8_t *bytes = (const uint8_t *)fixture;
    uint32_t offset = 0u;
    while (offset < total_size) {
        uint32_t chunk_size = total_size - offset;
        if (chunk_size > STPG_UPLOAD_CHUNK_SIZE) {
            chunk_size = STPG_UPLOAD_CHUNK_SIZE;
        }
        if (expect(stpg_buffer_write_chunk(offset, bytes + offset, chunk_size,
                                           stpg_crc32(bytes + offset, chunk_size)), ERR_NONE)) {
            fprintf(stderr, "load_fixture_psram failed at offset=%u size=%u\n",
                    (unsigned)offset, (unsigned)chunk_size);
            return 1;
        }
        offset += chunk_size;
    }
    stpg_err_t finalize_error = stpg_buffer_finalize(total_size, total_crc);
    if (expect(finalize_error, ERR_NONE)) {
        fprintf(stderr, "load_fixture_psram finalize error=%u\n", (unsigned)finalize_error);
        return 1;
    }
    return 0;
}

static int test_loop_full_expansion(void) {
    loop_fixture_t fixture;
    const uint32_t total_size = build_loop_fixture(&fixture, 1);

    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, total_size)) return 1;
    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) return 1;

    stpg_rate_info_t info;
    if (expect(stpg_buffer_get_rate_info(&info), ERR_NONE)) return 1;
    if (!info.loaded || info.loop_block_count != 1u || info.expanded_vector_count_estimate != 4u) return 1;

    if (stpg_buffer_event_count() != 8u) return 1;
    if (stpg_buffer_sample_event_count() != 0u) return 1;
    if (stpg_buffer_pio_word_count() != 32u) return 1;

    const uint32_t *words = stpg_buffer_pio_words();
    if (words[2] != 0x0001u || words[6] != 0x0000u) return 1;
    if (words[10] != 0x0002u || words[14] != 0x0000u) return 1;
    if (words[18] != 0x0001u || words[26] != 0x0002u) return 1;
    return 0;
}

static int test_loop_compare_supported(void) {
    loop_fixture_t fixture;
    const uint32_t total_size = build_loop_fixture(&fixture, 0);

    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, total_size)) return 1;
    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) return 1;
    if (stpg_buffer_sample_event_count() != 4u) return 1;
    return 0;
}

static int test_loop_streaming_matches_full_expansion(void) {
    loop_fixture_t fixture;
    const uint32_t total_size = build_loop_fixture(&fixture, 1);

    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, total_size)) return 1;
    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) return 1;
    const uint32_t *expanded_words = stpg_buffer_pio_words();
    const uint32_t expanded_word_count = stpg_buffer_pio_word_count();

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) return 1;

    uint32_t chunk_words[2u * 4u];
    uint32_t collected_words[32u];
    uint32_t collected_word_count = 0u;
    while (1) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 2u, &result), ERR_NONE)) return 1;
        if (result.event_count == 0u) break;
        memcpy(collected_words + collected_word_count, chunk_words, result.event_count * 4u * sizeof(uint32_t));
        collected_word_count += result.event_count * 4u;
        if (result.eof) break;
    }
    if (collected_word_count != expanded_word_count) return 1;
    if (memcmp(collected_words, expanded_words, expanded_word_count * sizeof(uint32_t)) != 0) return 1;
    return 0;
}

static int test_loop_streaming_boundary_iteration_edge(void) {
    boundary_fixture_t fixture;
    const uint32_t total_size = build_streaming_boundary_fixture(&fixture, 65u, 2u, 2u);

    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, total_size)) return 1;
    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) return 1;
    const uint32_t *expanded_words = stpg_buffer_pio_words();
    const uint32_t expanded_word_count = stpg_buffer_pio_word_count();

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) return 1;
    stpg_stream_buffer_telemetry_t telemetry_before;
    stpg_buffer_get_stream_telemetry(&telemetry_before);

    uint32_t chunk_words[256u * 4u];
    uint32_t collected_words[260u * 4u];
    uint32_t collected_word_count = 0u;
    uint32_t chunks = 0u;
    while (1) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 256u, &result), ERR_NONE)) return 1;
        if (result.event_count == 0u) break;
        memcpy(collected_words + collected_word_count, chunk_words, result.event_count * 4u * sizeof(uint32_t));
        collected_word_count += result.event_count * 4u;
        chunks += 1u;
        if (result.eof) break;
    }
    if (chunks != 2u) return 1;
    if (collected_word_count != expanded_word_count) return 1;
    if (memcmp(collected_words, expanded_words, expanded_word_count * sizeof(uint32_t)) != 0) return 1;
    stpg_stream_buffer_telemetry_t telemetry_after;
    stpg_buffer_get_stream_telemetry(&telemetry_after);
    if (telemetry_after.loop_body_cache_lookup_count != 130u
        || telemetry_after.loop_body_cache_miss_count != 1u
        || telemetry_after.loop_body_cache_load_count != 1u
        || telemetry_after.loop_body_cache_hit_count != 129u
        || telemetry_after.loop_body_cache_record_count != 2u
        || telemetry_after.loop_body_cache_byte_count != 2u * STPG_VECTOR_RECORD_SIZE
        || telemetry_after.loop_body_cache_validation_retry_count != 0u
        || telemetry_after.rate_runtime_cache_load_count != 1u
        || telemetry_after.rate_runtime_cache_slot_copy_count != 1u
        || telemetry_after.rate_vector_read_call_count
            - telemetry_before.rate_vector_read_call_count != 2u) {
        fprintf(stderr,
                "loop cache counters lookup=%u miss=%u load=%u hit=%u records=%u bytes=%u retry=%u rate_load=%u rate_slots=%u read_delta=%u\n",
                (unsigned)telemetry_after.loop_body_cache_lookup_count,
                (unsigned)telemetry_after.loop_body_cache_miss_count,
                (unsigned)telemetry_after.loop_body_cache_load_count,
                (unsigned)telemetry_after.loop_body_cache_hit_count,
                (unsigned)telemetry_after.loop_body_cache_record_count,
                (unsigned)telemetry_after.loop_body_cache_byte_count,
                (unsigned)telemetry_after.loop_body_cache_validation_retry_count,
                (unsigned)telemetry_after.rate_runtime_cache_load_count,
                (unsigned)telemetry_after.rate_runtime_cache_slot_copy_count,
                (unsigned)(telemetry_after.rate_vector_read_call_count
                    - telemetry_before.rate_vector_read_call_count));
        return 1;
    }
    return 0;
}

static int test_loop_streaming_runtime_cache_validation_is_fail_closed(void) {
    boundary_fixture_t fixture;
    const uint32_t total_size = build_streaming_boundary_fixture(&fixture, 65u, 2u, 2u);

    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, total_size)) return 1;
    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) return 1;

    VectorRecord *body = (VectorRecord *)stpg_psram_mapped_ptr(
        STPG_PSRAM_PATTERN_BASE + state.loop_descs[0].body_offset,
        STPG_VECTOR_RECORD_SIZE);
    if (body == NULL) return 1;
    body->repeat = 0u;

    uint32_t chunk_words[8u];
    stpg_stream_chunk_result_t result;
    if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 2u, &result),
               ERR_RATEVEC_BAD_HEADER)) {
        return 1;
    }
    stpg_stream_buffer_telemetry_t telemetry;
    stpg_buffer_get_stream_telemetry(&telemetry);
    if (telemetry.loop_body_cache_miss_count != 1u
        || telemetry.loop_body_cache_load_count != 0u
        || telemetry.loop_body_cache_validation_retry_count != 1u) {
        return 1;
    }
    return 0;
}

static int test_loop_streaming_boundary_inside_body(void) {
    boundary_fixture_t fixture;
    const uint32_t total_size = build_streaming_boundary_fixture(&fixture, 52u, 2u, 3u);

    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, total_size)) return 1;
    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) return 1;
    const uint32_t *expanded_words = stpg_buffer_pio_words();
    const uint32_t expanded_word_count = stpg_buffer_pio_word_count();

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) return 1;

    uint32_t chunk_words[256u * 4u];
    uint32_t collected_words[260u * 4u];
    uint32_t collected_word_count = 0u;
    uint32_t chunks = 0u;
    while (1) {
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 256u, &result), ERR_NONE)) return 1;
        if (result.event_count == 0u) break;
        memcpy(collected_words + collected_word_count, chunk_words, result.event_count * 4u * sizeof(uint32_t));
        collected_word_count += result.event_count * 4u;
        chunks += 1u;
        if (result.eof) break;
    }
    if (chunks != 2u) return 1;
    if (collected_word_count != expanded_word_count) return 1;
    if (memcmp(collected_words, expanded_words, expanded_word_count * sizeof(uint32_t)) != 0) return 1;
    return 0;
}

#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
static int test_loop_compare_sample_stream_replay(void) {
    loop_fixture_t fixture;
    const uint32_t total_size = build_loop_fixture(&fixture, 0);

    stpg_buffer_reset();
    if (load_fixture_psram(&fixture, total_size)) return 1;
    if (expect(stpg_buffer_prepare_for_run(), ERR_NONE)) return 1;
    const uint32_t *expanded_words = stpg_buffer_pio_words();
    const uint32_t expanded_word_count = stpg_buffer_pio_word_count();
    if (stpg_buffer_sample_event_count() != 4u) return 1;

    stpg_rate_stream_state_t state;
    if (expect(stpg_buffer_prepare_for_streaming(&state, STPG_RUN_FLAG_STREAMING_POC), ERR_NONE)) return 1;
    if (state.reduced_density_compare_enabled) return 1;
    if (stpg_buffer_stream_expected_sample_count() != 4u) return 1;

    uint32_t collected_words[32u];
    uint32_t collected_word_count = 0u;
    uint32_t collected_sample_count = 0u;
    while (true) {
        uint32_t chunk_words[2u * 4u];
        stpg_stream_chunk_result_t result;
        if (expect(stpg_buffer_stream_fill_next_chunk(&state, chunk_words, 2u, &result), ERR_NONE)) return 1;
        if (result.event_count == 0u && result.eof) break;
        memcpy(collected_words + collected_word_count,
               chunk_words,
               result.event_count * 4u * sizeof(uint32_t));
        collected_word_count += result.event_count * 4u;
        collected_sample_count += result.sample_event_count;
        if (result.eof) break;
    }
    if (collected_word_count != expanded_word_count
        || collected_sample_count != 4u
        || memcmp(collected_words, expanded_words, expanded_word_count * sizeof(uint32_t)) != 0) {
        return 1;
    }

    if (expect(stpg_samples_prepare_for_run(4u, STPG_ACTIVE_PINS_V01), ERR_NONE)) return 1;
    uint32_t *samples = stpg_samples_dma_buffer();
    if (samples == NULL || stpg_samples_dma_capacity_words() < 4u) return 1;
    samples[0] = 0x0001u;
    samples[1] = 0x0002u;
    samples[2] = 0x0001u;
    samples[3] = 0x0002u;
    stpg_samples_finish_run(4u, false);

    uint32_t compare_count = 0u;
    if (expect(stpg_compare_run_stream_replayed(&state, 0u, 0u, &compare_count), ERR_NONE)) return 1;
    if (compare_count != 4u || stpg_compare_fail_count() != 0u) return 1;
    return 0;
}
#endif

int main(void) {
    if (test_direct_expansion_accepts_exact_event_capacity()) {
        fprintf(stderr, "test_direct_expansion_accepts_exact_event_capacity failed\n");
        return 1;
    }
    if (test_small_psram_upload_finalizes_from_verified_shadow()) {
        fprintf(stderr, "test_small_psram_upload_finalizes_from_verified_shadow failed\n");
        return 1;
    }
#if STPG_STREAM_LOOP_BATCH && STPG_STREAM_FAIL_STOP
    if (test_fail_stop_loop_batch_metadata_matches_generic()) {
        fprintf(stderr, "test_fail_stop_loop_batch_metadata_matches_generic failed\n");
        return 1;
    }
    if (test_fail_stop_loop_compare_metadata_and_injected_mismatch()) {
        fprintf(stderr, "test_fail_stop_loop_compare_metadata_and_injected_mismatch failed\n");
        return 1;
    }
    if (test_fail_stop_loop_batch_sample_only_metadata_matches_generic()) {
        fprintf(stderr, "test_fail_stop_loop_batch_sample_only_metadata_matches_generic failed\n");
        return 1;
    }
    if (test_fail_stop_loop_sample_capacity_rejects_oversize()) {
        fprintf(stderr, "test_fail_stop_loop_sample_capacity_rejects_oversize failed\n");
        return 1;
    }
#elif STPG_STREAM_LOOP_BATCH && STPG_STREAM_PSRAM_COMPARE_SAMPLE
    if (test_long_loop_batch_matches_generic()) {
        fprintf(stderr, "test_long_loop_batch_matches_generic failed\n");
        return 1;
    }
    if (test_mixed_group_loop_batch_matches_generic()) {
        fprintf(stderr, "test_mixed_group_loop_batch_matches_generic failed\n");
        return 1;
    }
    if (test_pinned_f311_batch_matches_generic()) {
        fprintf(stderr, "test_pinned_f311_batch_matches_generic failed\n");
        return 1;
    }
    if (test_loop_batch_guard()) {
        fprintf(stderr, "test_loop_batch_guard failed\n");
        return 1;
    }
    if (test_loop_batch_partial_chunk_boundary()) {
        fprintf(stderr, "test_loop_batch_partial_chunk_boundary failed\n");
        return 1;
    }
#endif
    if (test_loop_full_expansion()) {
        fprintf(stderr, "test_loop_full_expansion failed\n");
        return 1;
    }
    if (test_loop_compare_supported()) {
        fprintf(stderr, "test_loop_compare_supported failed\n");
        return 1;
    }
    if (test_loop_streaming_matches_full_expansion()) {
        fprintf(stderr, "test_loop_streaming_matches_full_expansion failed\n");
        return 1;
    }
    if (test_loop_streaming_boundary_iteration_edge()) {
        fprintf(stderr, "test_loop_streaming_boundary_iteration_edge failed\n");
        return 1;
    }
    if (test_loop_streaming_boundary_inside_body()) {
        fprintf(stderr, "test_loop_streaming_boundary_inside_body failed\n");
        return 1;
    }
    if (test_loop_streaming_runtime_cache_validation_is_fail_closed()) {
        fprintf(stderr, "test_loop_streaming_runtime_cache_validation_is_fail_closed failed\n");
        return 1;
    }
#if STPG_STREAM_PSRAM_COMPARE_SAMPLE
    if (test_loop_compare_sample_stream_replay()) {
        fprintf(stderr, "test_loop_compare_sample_stream_replay failed\n");
        return 1;
    }
#endif
    return 0;
}
