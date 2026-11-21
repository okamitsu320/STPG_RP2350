#include "stpg_crc32.h"

#include <stdint.h>
#include <string.h>

static int expect_crc(const void *buf, unsigned len, uint32_t expected) {
    return stpg_crc32(buf, len) == expected ? 0 : 1;
}

int main(void) {
    static const uint8_t zero = 0x00;
    static const char digits[] = "123456789";
    uint8_t ff32[32];
    uint8_t range256[256];

    memset(ff32, 0xFF, sizeof(ff32));
    for (unsigned i = 0; i < sizeof(range256); ++i) {
        range256[i] = (uint8_t)i;
    }

    if (expect_crc("", 0, 0x00000000u)) return 1;
    if (expect_crc(&zero, 1, 0xD202EF8Du)) return 1;
    if (expect_crc(digits, 9, 0xCBF43926u)) return 1;
    if (expect_crc(ff32, sizeof(ff32), 0xFF6CAB0Bu)) return 1;
    if (expect_crc(range256, sizeof(range256), 0x29058C73u)) return 1;
    return 0;
}
