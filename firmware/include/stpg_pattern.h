#ifndef STPG_PATTERN_H
#define STPG_PATTERN_H

#include <stdint.h>

#include "stpg_common.h"

typedef struct __attribute__((packed)) {
    uint16_t wait_ticks;
    uint16_t flags;
    uint32_t out_value;
    uint32_t out_enable;
    uint32_t sample_mask;
    uint32_t expect_value;
} ExecEvent;

typedef struct __attribute__((packed)) {
    uint32_t wait_ticks;
    uint16_t flags;
    uint16_t reserved;
    uint32_t out_value;
    uint32_t out_enable;
    uint32_t sample_mask;
    uint32_t expect_value;
} ExecEvent32;

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t version;
    uint16_t header_size;
    uint32_t flags;
    uint32_t active_pins;
    uint32_t tick_hz;
    uint32_t event_size;
    uint32_t event_count;
    uint32_t data_size;
    uint32_t data_crc32;
    uint32_t header_crc32;
} PatternHeader;

typedef struct __attribute__((packed)) {
    uint32_t w_wait;
    uint32_t w_sample_trigger;
    uint32_t w_value;
    uint32_t w_dir;
} PioWord4;

typedef struct __attribute__((packed)) {
    uint32_t event_index;
    uint32_t actual;
    uint32_t expected;
    uint32_t mask;
} FailLogEntry;

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint32_t base;
    uint32_t size;
    uint32_t crc32;
    uint32_t format;
    uint32_t flags;
    uint32_t reserved0;
    uint32_t reserved1;
} PsramPatternSlot;

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t meta_version;
    uint16_t reserved_a;
    uint32_t meta_crc32;
    uint32_t pattern_offset;
    uint32_t pattern_size;
    uint32_t pattern_crc32;
    uint32_t format;
    uint32_t active_pins;
    uint32_t tick_hz;
    char     name[32];
    uint64_t created_at;
    uint32_t flags;
    uint8_t  reserved_b[4016];
} FlashSlotMeta;

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t version;
    uint16_t reserved0;
    uint32_t boot_slot_id;
    uint32_t boot_flags;
    uint32_t config_crc32;
    uint8_t  reserved1[4076];
} StpgBootConfig;

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t version;
    uint16_t header_size;
    uint32_t flags;
    uint32_t active_pins;
    uint32_t tick_hz;
    uint32_t rate_count;
    uint32_t slot_count;
    uint32_t vector_count;
    uint32_t rate_table_offset;
    uint32_t slot_table_offset;
    uint32_t vector_offset;
    uint32_t data_crc32;
    uint32_t reserved0;
    uint32_t reserved1;
    uint32_t reserved2;
    uint32_t header_crc32;
} RateVectorHeader;

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t version;
    uint16_t header_size;
    uint32_t flags;
    uint32_t active_pins;
    uint32_t tick_hz;
    uint32_t record_size;
    uint32_t record_count;
    uint32_t data_size;
    uint32_t data_crc32;
    uint32_t header_crc32;
} MacroVectorHeader;

typedef struct __attribute__((packed)) {
    uint16_t period_ticks;
    uint8_t slot_count;
    uint8_t flags;
    uint32_t slot_offset;
} RateDesc;

typedef struct __attribute__((packed)) {
    uint16_t time_ticks;
    uint16_t flags;
    uint32_t drive_mask;
    uint32_t dir_mask;
    uint32_t sample_mask;
} RateSlot;

/* Version-2 rate-vector wire records.  The v1 records above are immutable. */
typedef struct __attribute__((packed)) {
    uint32_t period_ticks;
    uint8_t slot_count;
    uint8_t flags;
    uint16_t reserved;
    uint32_t slot_offset;
} RateDesc32;

typedef struct __attribute__((packed)) {
    uint32_t time_ticks;
    uint16_t flags;
    uint16_t reserved;
    uint32_t drive_mask;
    uint32_t dir_mask;
    uint32_t sample_mask;
} RateSlot32;

typedef struct __attribute__((packed)) {
    uint8_t rate_id;
    uint8_t flags;
    uint16_t repeat;
    uint32_t drive_value;
    uint32_t drive_enable;
    uint32_t expect_value;
    uint32_t expect_mask;
} VectorRecord;

typedef struct __attribute__((packed)) {
    uint16_t period_ticks;
    uint16_t phase_ticks;
    uint16_t repeat;
    uint8_t family;
    uint8_t flags;
    uint8_t drive_value[3];
    uint8_t drive_enable[3];
    uint8_t sample_mask[3];
    uint8_t expect_value[3];
} MacroVectorRecord;

typedef struct __attribute__((packed)) {
    uint16_t period_ticks;
    uint16_t repeat;
    uint16_t wait_first_ticks;
    uint8_t source_family;
    uint8_t exec_family;
    uint8_t flags;
    uint8_t wait_second_ticks;
    uint8_t drive0_value[3];
    uint8_t drive0_enable[3];
    uint8_t drive1_value[3];
    uint8_t drive1_enable[3];
    uint8_t sample0_mask[3];
    uint8_t sample1_mask[3];
    uint8_t expect0_value[3];
    uint8_t expect1_value[3];
} MacroVectorRecordP2;

typedef struct __attribute__((packed)) {
    uint32_t loop_count;
    uint32_t body_record_count;
} LoopBlockHeader;

typedef struct __attribute__((packed)) {
    uint8_t reserved0;
    uint8_t flags;
    uint16_t reserved1;
    uint32_t loop_id;
    uint32_t reserved2;
    uint32_t reserved3;
    uint32_t reserved4;
} LoopRefRecord;

_Static_assert(sizeof(ExecEvent) == STPG_EXEC_EVENT_SIZE, "ExecEvent size must be 20");
_Static_assert(sizeof(ExecEvent32) == STPG_EXEC_EVENT32_SIZE, "ExecEvent32 size must be 24");
_Static_assert(sizeof(RateDesc32) == STPG_RATE_DESC32_SIZE, "RateDesc32 size must be 12");
_Static_assert(sizeof(RateSlot32) == STPG_RATE_SLOT32_SIZE, "RateSlot32 size must be 20");
_Static_assert(sizeof(PatternHeader) == STPG_HEADER_SIZE, "PatternHeader size must be 40");
_Static_assert(sizeof(PioWord4) == 16u, "PioWord4 size must be 16");
_Static_assert(sizeof(FailLogEntry) == 16u, "FailLogEntry size must be 16");
_Static_assert(sizeof(PsramPatternSlot) == 32u, "PsramPatternSlot size must be 32");
_Static_assert(sizeof(FlashSlotMeta) == 4096u, "FlashSlotMeta size must be 4096");
_Static_assert(sizeof(StpgBootConfig) == 4096u, "StpgBootConfig size must be 4096");
_Static_assert(sizeof(RateVectorHeader) == STPG_RATEVEC_HEADER_SIZE, "RateVectorHeader size must be 64");
_Static_assert(sizeof(MacroVectorHeader) == STPG_MACROVEC_HEADER_SIZE, "MacroVectorHeader size must be 40");
_Static_assert(sizeof(RateDesc) == STPG_RATE_DESC_SIZE, "RateDesc size must be 8");
_Static_assert(sizeof(RateSlot) == STPG_RATE_SLOT_SIZE, "RateSlot size must be 16");
_Static_assert(sizeof(VectorRecord) == STPG_VECTOR_RECORD_SIZE, "VectorRecord size must be 20");
_Static_assert(sizeof(MacroVectorRecord) == STPG_MACRO_RECORD_SIZE, "MacroVectorRecord size must be 20");
_Static_assert(sizeof(MacroVectorRecordP2) == STPG_MACRO_P2_RECORD_SIZE, "MacroVectorRecordP2 size must be 34");
_Static_assert(sizeof(LoopBlockHeader) == STPG_LOOP_BLOCK_HEADER_SIZE, "LoopBlockHeader size must be 8");
_Static_assert(sizeof(LoopRefRecord) == STPG_VECTOR_RECORD_SIZE, "LoopRefRecord size must be 20");

#endif
