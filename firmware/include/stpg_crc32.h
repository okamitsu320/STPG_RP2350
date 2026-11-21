#ifndef STPG_CRC32_H
#define STPG_CRC32_H

#include <stddef.h>
#include <stdint.h>

uint32_t stpg_crc32(const void *buf, size_t len);
uint32_t stpg_crc32_continue(uint32_t crc, const void *buf, size_t len);

#endif
