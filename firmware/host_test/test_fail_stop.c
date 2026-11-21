#include "stpg_fail_stop.h"

#include <stdio.h>

static int expect_error(stpg_err_t actual, stpg_err_t expected, const char *label) {
    if (actual != expected) {
        fprintf(stderr, "%s: expected error %d, got %d\n", label, expected, actual);
        return 1;
    }
    return 0;
}

static int expect_bool(bool actual, bool expected, const char *label) {
    if (actual != expected) {
        fprintf(stderr, "%s: expected %s, got %s\n", label,
                expected ? "true" : "false", actual ? "true" : "false");
        return 1;
    }
    return 0;
}

int main(void) {
    uint32_t limit = 0u;
    stpg_fail_stop_metadata_t metadata;

    if (expect_error(stpg_fail_stop_resolve_limit(false, 0u, &limit), ERR_NONE,
                     "disabled zero")) return 1;
    if (limit != 0u) return 1;
    if (expect_error(stpg_fail_stop_resolve_limit(false, 1u, &limit), ERR_UNSUPPORTED,
                     "disabled nonzero")) return 1;
    if (expect_error(stpg_fail_stop_resolve_limit(true, 0u, &limit), ERR_NONE,
                     "enabled zero")) return 1;
    if (limit != 1u) return 1;
    if (expect_error(stpg_fail_stop_resolve_limit(true, STPG_FAIL_LOG_MAX, &limit), ERR_NONE,
                     "enabled maximum")) return 1;
    if (limit != STPG_FAIL_LOG_MAX) return 1;
    if (expect_error(stpg_fail_stop_resolve_limit(true, STPG_FAIL_LOG_MAX + 1u, &limit),
                     ERR_BAD_SIZE, "enabled oversized")) return 1;
    if (expect_error(stpg_fail_stop_resolve_limit(true, 0u, NULL), ERR_BAD_SIZE,
                     "null limit")) return 1;

    stpg_fail_stop_metadata_init(&metadata, 7u);
    if (expect_bool(stpg_fail_stop_metadata_reusable(&metadata), true, "initial reusable")) return 1;
    if (expect_error(stpg_fail_stop_metadata_publish(&metadata, 7u, 0u, 4u), ERR_NONE,
                     "first publish")) return 1;
    if (metadata.generation != 7u || metadata.sample_start != 0u
        || metadata.sample_count != 4u || metadata.consumed_count != 0u) return 1;
    if (expect_bool(stpg_fail_stop_metadata_reusable(&metadata), false, "published not reusable")) return 1;
    if (expect_error(stpg_fail_stop_metadata_publish(&metadata, 8u, 4u, 1u),
                     ERR_FAIL_STOP_METADATA_OVERRUN,
                     "double publish")) return 1;
    if (expect_error(stpg_fail_stop_metadata_defer_refill(&metadata), ERR_NONE,
                     "defer refill")) return 1;
    if (expect_error(stpg_fail_stop_metadata_defer_refill(&metadata),
                     ERR_FAIL_STOP_METADATA_OVERRUN,
                     "double defer")) return 1;
    if (expect_error(stpg_fail_stop_metadata_consume(&metadata, 3u), ERR_NONE,
                     "partial consume")) return 1;
    if (metadata.state != STPG_FAIL_STOP_METADATA_PUBLISHED || metadata.consumed_count != 3u) return 1;
    uint32_t sample_cursor = 0u;
    if (expect_error(stpg_fail_stop_metadata_sample_cursor(&metadata, &sample_cursor),
                     ERR_NONE, "partial consume cursor")) return 1;
    if (sample_cursor != 3u) return 1;
    if (expect_error(stpg_fail_stop_metadata_consume(&metadata, 2u),
                     ERR_FAIL_STOP_METADATA_OVERRUN, "consume oversized")) return 1;
    if (expect_error(stpg_fail_stop_metadata_consume(&metadata, 1u), ERR_NONE,
                     "final consume")) return 1;
    if (metadata.state != STPG_FAIL_STOP_METADATA_CONSUMED) return 1;
    if (expect_bool(stpg_fail_stop_metadata_reusable(&metadata), false,
                    "deferred consumed not reusable")) return 1;
    if (expect_bool(stpg_fail_stop_metadata_take_deferred_refill(&metadata), true,
                    "take deferred refill")) return 1;
    if (expect_bool(stpg_fail_stop_metadata_take_deferred_refill(&metadata), false,
                    "take deferred refill once")) return 1;
    if (expect_bool(stpg_fail_stop_metadata_reusable(&metadata), true, "reusable after refill")) return 1;
    if (expect_error(stpg_fail_stop_metadata_consume(&metadata, 0u),
                     ERR_FAIL_STOP_METADATA_OVERRUN,
                     "consume after consumed")) return 1;

    stpg_fail_stop_metadata_init(&metadata, 8u);
    if (expect_error(stpg_fail_stop_metadata_publish(&metadata, 4u, 4u, 9u), ERR_NONE,
                     "incremental publish")) return 1;
    if (expect_error(stpg_fail_stop_metadata_sample_cursor(&metadata, &sample_cursor),
                     ERR_NONE, "incremental initial cursor")) return 1;
    if (sample_cursor != 4u) return 1;
    if (expect_error(stpg_fail_stop_metadata_consume(&metadata, 1u), ERR_NONE,
                     "incremental consume one")) return 1;
    if (expect_error(stpg_fail_stop_metadata_sample_cursor(&metadata, &sample_cursor),
                     ERR_NONE, "incremental cursor one")) return 1;
    if (sample_cursor != 5u) return 1;
    if (expect_error(stpg_fail_stop_metadata_consume(&metadata, 2u), ERR_NONE,
                     "incremental consume two")) return 1;
    if (expect_error(stpg_fail_stop_metadata_sample_cursor(&metadata, &sample_cursor),
                     ERR_NONE, "incremental cursor three")) return 1;
    if (sample_cursor != 7u) return 1;

    stpg_fail_stop_metadata_t corrupt = metadata;
    corrupt.consumed_count = corrupt.sample_count + 1u;
    if (expect_error(stpg_fail_stop_metadata_sample_cursor(&corrupt, &sample_cursor),
                     ERR_FAIL_STOP_METADATA_OVERRUN, "corrupt cursor")) return 1;
    if (expect_error(stpg_fail_stop_metadata_sample_cursor(NULL, &sample_cursor),
                     ERR_BAD_SIZE, "null cursor metadata")) return 1;
    if (expect_error(stpg_fail_stop_metadata_sample_cursor(&metadata, NULL),
                     ERR_BAD_SIZE, "null cursor output")) return 1;

    if (expect_error(stpg_fail_stop_metadata_consume(&metadata, 6u), ERR_NONE,
                     "finish incremental consume")) return 1;
    if (expect_error(stpg_fail_stop_metadata_publish(&metadata, 8u, 4u, 1024u), ERR_NONE,
                     "new generation publish")) return 1;
    if (metadata.generation != 8u) return 1;
    if (expect_error(stpg_fail_stop_metadata_publish(&metadata, 8u, 0u, 1025u),
                     ERR_FAIL_STOP_METADATA_OVERRUN,
                     "publish while active")) return 1;
    if (expect_error(stpg_fail_stop_metadata_consume(&metadata, 1024u), ERR_NONE,
                     "consume full slice")) return 1;

    if (expect_error(stpg_fail_stop_metadata_publish(&metadata, 9u, STPG_MAX_EVENT_COUNT, 1u),
                     ERR_FAIL_STOP_METADATA_OVERRUN, "sample range overflow")) return 1;
    if (expect_error(stpg_fail_stop_metadata_publish(&metadata, 9u, STPG_MAX_EVENT_COUNT + 1u, 0u),
                     ERR_FAIL_STOP_METADATA_OVERRUN, "sample start overflow")) return 1;
    if (expect_error(stpg_fail_stop_metadata_publish(&metadata, 9u, 0u,
                                                     STPG_STREAM_BUFFER_EVENTS + 1u),
                     ERR_FAIL_STOP_METADATA_OVERRUN, "slice overflow")) return 1;

    return 0;
}
