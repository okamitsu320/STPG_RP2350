#ifndef STPG_PROTOCOL_H
#define STPG_PROTOCOL_H

#include <stdint.h>

#include "stpg_board.h"
#include "stpg_common.h"

#ifndef STPG_FAIL_STOP
#define STPG_FAIL_STOP 0
#endif

#ifndef STPG_STREAM_FAIL_STOP
#define STPG_STREAM_FAIL_STOP 0
#endif

typedef enum {
    CMD_HELLO = 0x0001,
    CMD_INFO = 0x0002,
    CMD_GET_STATUS = 0x0003,
    CMD_GET_CLOCK_INFO = 0x0004,
    CMD_GET_RATE_INFO = 0x0005,
    CMD_GET_STREAM_TELEMETRY = 0x0006,
    CMD_UPLOAD_BEGIN = 0x0010,
    CMD_UPLOAD_DATA = 0x0011,
    CMD_UPLOAD_END = 0x0012,
    CMD_RUN = 0x0020,
    CMD_STOP = 0x0021,
    CMD_RESET = 0x0022,
    CMD_GET_SAMPLE_INFO = 0x0030,
    CMD_READ_SAMPLES = 0x0031,
    CMD_GET_FAIL_SUMMARY = 0x0032,
    CMD_READ_FAIL_LOG = 0x0033,
    CMD_LIST_FLASH_SLOTS = 0x0040,
    CMD_SELECT_FLASH_SLOT = 0x0041,
    CMD_ERASE_FLASH_SLOT = 0x0042,
    CMD_GET_FLASH_SLOT_INFO = 0x0043,
    CMD_SET_BOOT_SLOT = 0x0044,
    CMD_PSRAM_CHECKSUM = 0x0050,
    CMD_PSRAM_READ = 0x0051,
    CMD_PSRAM_ERASE = 0x0052,
} stpg_cmd_t;

typedef enum {
    RSP_OK = 0x8000,
    RSP_ERROR = 0x8001,
    RSP_INFO = 0x8002,
    RSP_STATUS = 0x8003,
    RSP_DATA_ACK = 0x8004,
    RSP_READY_FOR_PAYLOAD = 0x8005,
    RSP_CLOCK_INFO = 0x8006,
    RSP_READY_FOR_READ = 0x8007,
    RSP_DATA_END = 0x8008,
    RSP_FLASH_SLOT_INFO = 0x8009,
} stpg_rsp_t;

#define CAP_UPLOAD_SRAM      (1u << 0)
#define CAP_UPLOAD_PSRAM     (1u << 1)
#define CAP_UPLOAD_FLASH     (1u << 2)
#define CAP_RUN_16PIN        (1u << 3)
#define CAP_RUN_24PIN        (1u << 4)
#define CAP_OUTPUT_ENABLE    (1u << 5)
#define CAP_SAMPLE_CAPTURE   (1u << 6)
#define CAP_EXPECT_COMPARE   (1u << 7)
#define CAP_FLASH_SLOTS      (1u << 8)
#define CAP_PSRAM_STREAMING  (1u << 9)
#define CAP_STIL_PATTERN     (1u << 10)
#define CAP_OVERCLOCK_200MHZ (1u << 11)
#define CAP_RATE_VECTOR_MODE (1u << 12)
#define CAP_RATE_REPEAT      (1u << 13)
#define CAP_RATE_LOOP_LIMITED (1u << 14)
#define CAP_RATE_DELTA_EXPERIMENTAL (1u << 15)
#define CAP_HEADLESS_BOOT   (1u << 16)
#define CAP_WIDE_WAIT_32    (1u << 18)
#define CAP_WIDE_RATE_VECTOR_32 (1u << 19)
#define CAP_PRACTICAL_STREAMING (1u << 20)
#define CAP_SEGMENTED_STREAMING (1u << 17)
#define CAP_FAIL_STOP       (1u << 21)
#define CAP_STREAM_FAIL_STOP (1u << 22)
#define CAP_STREAM_WAIT_WATCHDOG (1u << 23)

/* RESET arg0; zero retains the legacy output-hold behavior after DONE. */
#define STPG_RESET_FLAG_RELEASE_OUTPUTS (1u << 0)
#define STPG_RUN_FLAG_STREAMING_POC (1u << 8)
#define STPG_RUN_FLAG_PRESERVE_GPIO_STATE (1u << 9)
#define STPG_RUN_FLAG_SEGMENT_END_HOLD (1u << 10)
#define STPG_RUN_FLAG_MEASURE_FAMILY (1u << 11)
#define STPG_RUN_FLAG_MEASURE_SEQUENCE (1u << 12)
#define STPG_RUN_FLAG_MEASURE_BURST (1u << 13)
#define STPG_RUN_FLAG_MEASURE_VISIBILITY (1u << 14)
#define STPG_RUN_FLAG_MEASURE_HANDSHAKE (1u << 15)
#define STPG_RUN_FLAG_EXPLICIT_WAIT_P4 (1u << 16)
#define STPG_RUN_FLAG_FAIL_STOP (1u << 17)
#define STPG_RESULT_FLAG_SAMPLE_OVERFLOW (1u << 0)
#define STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW (1u << 1)
#define STPG_RESULT_FLAG_COMPARE_MISMATCH (1u << 2)
#define STPG_RESULT_TYPE_SAMPLE 0u
#define STPG_RESULT_TYPE_FAIL_LOG 1u

#if STPG_STREAM_PRACTICAL_PROFILE
#define STPG_CAPABILITY_PRACTICAL CAP_PRACTICAL_STREAMING
#else
#define STPG_CAPABILITY_PRACTICAL 0u
#endif

#if STPG_FAIL_STOP
#define STPG_CAPABILITY_FAIL_STOP CAP_FAIL_STOP
#else
#define STPG_CAPABILITY_FAIL_STOP 0u
#endif

#if STPG_STREAM_FAIL_STOP
#define STPG_CAPABILITY_STREAM_FAIL_STOP CAP_STREAM_FAIL_STOP
#else
#define STPG_CAPABILITY_STREAM_FAIL_STOP 0u
#endif

#define STPG_CAPABILITY_BASE_V06A (CAP_UPLOAD_SRAM | CAP_UPLOAD_PSRAM | CAP_RUN_16PIN | CAP_RUN_24PIN | CAP_OUTPUT_ENABLE | CAP_SAMPLE_CAPTURE | CAP_EXPECT_COMPARE | CAP_PSRAM_STREAMING | CAP_RATE_VECTOR_MODE | CAP_RATE_REPEAT | CAP_RATE_LOOP_LIMITED | CAP_SEGMENTED_STREAMING | CAP_WIDE_WAIT_32 | CAP_WIDE_RATE_VECTOR_32 | STPG_CAPABILITY_PRACTICAL | STPG_CAPABILITY_FAIL_STOP | STPG_CAPABILITY_STREAM_FAIL_STOP | CAP_STREAM_WAIT_WATCHDOG)

#if STPG_CLOCK_MODE == STPG_CLOCK_MODE_200MHZ
#define STPG_CAPABILITY_CURRENT (STPG_CAPABILITY_BASE_V06A | CAP_OVERCLOCK_200MHZ)
#else
#define STPG_CAPABILITY_CURRENT STPG_CAPABILITY_BASE_V06A
#endif

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t version;
    uint16_t cmd;
    uint32_t seq;
    uint32_t arg0;
    uint32_t arg1;
    uint32_t arg2;
    uint32_t arg3;
    uint32_t crc32;
} StpgCommand;

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t version;
    uint16_t rsp;
    uint32_t seq;
    uint32_t state;
    uint32_t capability;
    uint32_t allowed_cmds;
    uint32_t active_pins;
    uint32_t pattern_size;
    uint32_t event_count;
    uint32_t run_progress;
    uint32_t result_flags;
    uint32_t fail_count;
    uint32_t sample_count;
    uint32_t error_code;
    uint32_t firmware_version;
    uint32_t crc32;
} StpgStatusResponse;

_Static_assert(sizeof(StpgCommand) == STPG_CMD_SIZE, "StpgCommand size must be 32");
_Static_assert(sizeof(StpgStatusResponse) == STPG_RSP_SIZE, "StpgStatusResponse size must be 64");

#endif
