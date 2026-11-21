#ifndef STPG_STACK_WATERMARK_H
#define STPG_STACK_WATERMARK_H

#include <stdint.h>

#define STPG_STACK_WATERMARK_MAGIC 0x53544B57u
#define STPG_STACK_WATERMARK_VERSION 2u
#define STPG_STACK_WATERMARK_STATE_EMPTY 0u
#define STPG_STACK_WATERMARK_STATE_INITIALIZING 1u
#define STPG_STACK_WATERMARK_STATE_VALID 2u
#define STPG_STACK_WATERMARK_STATE_INVALID 3u

typedef struct {
    volatile uint32_t magic;
    volatile uint32_t version;
    volatile uint32_t state;
    volatile uint32_t stack_bottom;
    volatile uint32_t stack_top;
    volatile uint32_t paint_start;
    volatile uint32_t paint_end;
    volatile uint32_t initial_sp;
    volatile uint32_t painted_words;
    volatile uint32_t bottom_reserve_bytes;
    volatile uint32_t guard_bytes;
    volatile uint32_t initial_sp_used_bytes;
} stpg_stack_watermark_record_t;

#if defined(STPG_STACK_WATERMARK_DIAGNOSTIC) && STPG_STACK_WATERMARK_DIAGNOSTIC
extern volatile stpg_stack_watermark_record_t g_stpg_stack_watermark_core0;
extern volatile stpg_stack_watermark_record_t g_stpg_stack_watermark_core1;

void stpg_stack_watermark_core0_init_once(void);
void stpg_stack_watermark_core1_init_once(void);
#else
static inline void stpg_stack_watermark_core0_init_once(void) {}
static inline void stpg_stack_watermark_core1_init_once(void) {}
#endif

#endif
