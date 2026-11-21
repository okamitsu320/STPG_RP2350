#include "stpg_stack_watermark.h"

#if defined(STPG_STACK_WATERMARK_DIAGNOSTIC) && STPG_STACK_WATERMARK_DIAGNOSTIC && defined(PICO_ON_DEVICE)

#include <stddef.h>
#include <stdint.h>

#include "hardware/sync.h"
#include "pico/platform.h"

#define STPG_STACK_WATERMARK_BOTTOM_RESERVE_BYTES 128u
#define STPG_STACK_WATERMARK_GUARD_BYTES 128u
#define STPG_STACK_WATERMARK_WORD_SEED 0xA53C91E7u

extern uint32_t __StackBottom;
extern uint32_t __StackTop;
extern uint32_t __StackOneBottom;
extern uint32_t __StackOneTop;

volatile stpg_stack_watermark_record_t g_stpg_stack_watermark_core0
    __attribute__((used, aligned(4)));
volatile stpg_stack_watermark_record_t g_stpg_stack_watermark_core1
    __attribute__((used, aligned(4)));

static inline __attribute__((always_inline)) uint32_t stpg_stack_watermark_read_sp(void) {
    uint32_t stack_pointer;
#if defined(__riscv)
    __asm volatile("mv %0, sp" : "=r"(stack_pointer));
#else
    __asm volatile("mov %0, sp" : "=r"(stack_pointer));
#endif
    return stack_pointer;
}

static inline __attribute__((always_inline)) uint32_t stpg_stack_watermark_word(uint32_t address) {
    return STPG_STACK_WATERMARK_WORD_SEED ^ address ^ ((address >> 2u) * 0x9E3779B9u);
}

static void __no_inline_not_in_flash_func(stpg_stack_watermark_paint_once)(
    volatile stpg_stack_watermark_record_t *record,
    uint32_t stack_bottom,
    uint32_t stack_top,
    uint32_t guard_bytes
) {
    if (record->state != STPG_STACK_WATERMARK_STATE_EMPTY) {
        return;
    }
    record->state = STPG_STACK_WATERMARK_STATE_INITIALIZING;

    const uint32_t stack_pointer = stpg_stack_watermark_read_sp();
    if (stack_bottom >= stack_top
        || stack_bottom > UINT32_MAX - STPG_STACK_WATERMARK_BOTTOM_RESERVE_BYTES - 3u
        || stack_pointer <= stack_bottom + STPG_STACK_WATERMARK_BOTTOM_RESERVE_BYTES
        || stack_pointer <= stack_bottom + guard_bytes
        || stack_pointer > stack_top) {
        record->stack_bottom = stack_bottom;
        record->stack_top = stack_top;
        record->initial_sp = stack_pointer;
        record->bottom_reserve_bytes = STPG_STACK_WATERMARK_BOTTOM_RESERVE_BYTES;
        record->guard_bytes = guard_bytes;
        record->state = STPG_STACK_WATERMARK_STATE_INVALID;
        return;
    }

    const uint32_t paint_start =
        (stack_bottom + STPG_STACK_WATERMARK_BOTTOM_RESERVE_BYTES + 3u) & ~3u;
    const uint32_t paint_end = (stack_pointer - guard_bytes) & ~3u;
    if (paint_end <= paint_start) {
        record->stack_bottom = stack_bottom;
        record->stack_top = stack_top;
        record->paint_start = paint_start;
        record->paint_end = paint_end;
        record->initial_sp = stack_pointer;
        record->bottom_reserve_bytes = STPG_STACK_WATERMARK_BOTTOM_RESERVE_BYTES;
        record->guard_bytes = guard_bytes;
        record->state = STPG_STACK_WATERMARK_STATE_INVALID;
        return;
    }

    record->magic = 0u;
    record->version = STPG_STACK_WATERMARK_VERSION;
    record->stack_bottom = stack_bottom;
    record->stack_top = stack_top;
    record->paint_start = paint_start;
    record->paint_end = paint_end;
    record->initial_sp = stack_pointer;
    record->painted_words = (paint_end - paint_start) / sizeof(uint32_t);
    record->bottom_reserve_bytes = STPG_STACK_WATERMARK_BOTTOM_RESERVE_BYTES;
    record->guard_bytes = guard_bytes;
    record->initial_sp_used_bytes = stack_top - stack_pointer;

    for (uint32_t address = paint_start; address < paint_end; address += sizeof(uint32_t)) {
        *(volatile uint32_t *)(uintptr_t)address = stpg_stack_watermark_word(address);
    }
    __dmb();
    record->magic = STPG_STACK_WATERMARK_MAGIC;
    record->state = STPG_STACK_WATERMARK_STATE_VALID;
}

void __no_inline_not_in_flash_func(stpg_stack_watermark_core0_init_once)(void) {
    stpg_stack_watermark_paint_once(
        &g_stpg_stack_watermark_core0,
        (uint32_t)(uintptr_t)&__StackBottom,
        (uint32_t)(uintptr_t)&__StackTop,
        STPG_STACK_WATERMARK_GUARD_BYTES
    );
}

void __no_inline_not_in_flash_func(stpg_stack_watermark_core1_init_once)(void) {
    stpg_stack_watermark_paint_once(
        &g_stpg_stack_watermark_core1,
        (uint32_t)(uintptr_t)&__StackOneBottom,
        (uint32_t)(uintptr_t)&__StackOneTop,
        STPG_STACK_WATERMARK_GUARD_BYTES
    );
}

#endif
