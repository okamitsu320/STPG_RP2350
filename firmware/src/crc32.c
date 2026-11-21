#include "stpg_crc32.h"

#include <stdint.h>

uint32_t stpg_crc32_continue(uint32_t crc, const void *buf, size_t len) {
    const uint8_t *p = (const uint8_t *)buf;
    crc = ~crc;
    for (size_t i = 0; i < len; ++i) {
        crc ^= p[i];
        for (unsigned bit = 0; bit < 8; ++bit) {
            uint32_t mask = 0u - (crc & 1u);
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
        }
    }
    return ~crc;
}

uint32_t stpg_crc32(const void *buf, size_t len) {
    return stpg_crc32_continue(0u, buf, len);
}
