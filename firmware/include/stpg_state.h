#ifndef STPG_STATE_H
#define STPG_STATE_H

#include <stdint.h>

typedef enum {
    STATE_BOOT = 0,
    STATE_IDLE = 1,
    STATE_RECEIVING = 2,
    STATE_READY = 3,
    STATE_RUNNING = 4,
    STATE_RUN_DRAINING = 5,
    STATE_DONE = 6,
    STATE_ERROR = 7,
    STATE_HOLD_AWAITING_NEXT = 8,
} stpg_state_t;

#define ALLOW_UPLOAD (1u << 0)
#define ALLOW_RUN    (1u << 1)
#define ALLOW_STOP   (1u << 2)
#define ALLOW_STATUS (1u << 3)
#define ALLOW_RESET  (1u << 4)
#define ALLOW_INFO   (1u << 5)
#define ALLOW_RESULT_READ (1u << 6)

static inline uint32_t stpg_allowed_cmds_for_state(stpg_state_t state) {
    switch (state) {
    case STATE_BOOT:
        return 0x08u;
    case STATE_IDLE:
        return 0x7Bu;
    case STATE_RECEIVING:
        return 0x19u;
    case STATE_READY:
        return 0x7Bu;
    case STATE_RUNNING:
    case STATE_RUN_DRAINING:
        return 0x0Cu;
    case STATE_DONE:
        return 0x7Bu;
    case STATE_ERROR:
        return 0x7Cu;
    case STATE_HOLD_AWAITING_NEXT:
        /* A held segment keeps its results readable until the next upload. */
        return 0x7Bu;
    default:
        return 0u;
    }
}

#endif
