#include "stpg_samples.h"

#include <stdint.h>
#include <string.h>

#include "stpg_protocol.h"

static int expect_u32(uint32_t actual, uint32_t expected) {
    return actual == expected ? 0 : 1;
}

int main(void) {
    stpg_samples_reset();
    if (stpg_samples_prepare_for_run(2u, STPG_ACTIVE_PINS_V01) != ERR_NONE) return 1;
    uint32_t *buf = stpg_samples_dma_buffer();
    buf[0] = 0x000000A5u;
    buf[1] = 0x00005A5Au;
    stpg_samples_finish_run(2u, true);

    if (expect_u32(stpg_samples_sample_count(), 2u)) return 1;
    if (expect_u32(stpg_samples_result_flags(), STPG_RESULT_FLAG_SAMPLE_OVERFLOW)) return 1;

    stpg_sample_read_view_t view;
    if (stpg_samples_prepare_read(0u, 4u, &view) != ERR_NONE) return 1;
    if (expect_u32(view.payload_length, 4u)) return 1;
    if (expect_u32(view.element_count, 2u)) return 1;
    if (expect_u32(view.total_sample_count, 2u)) return 1;

    uint8_t bytes[4] = {0};
    stpg_samples_read_bytes(0u, bytes, sizeof(bytes));
    if (bytes[0] != 0xA5u || bytes[1] != 0x00u || bytes[2] != 0x5Au || bytes[3] != 0x5Au) return 1;

    if (stpg_samples_prepare_read(5u, 1u, &view) != ERR_BAD_SIZE) return 1;
    return 0;
}
