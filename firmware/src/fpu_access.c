#include "stpg_fpu_access.h"

volatile uint32_t g_stpg_fpu_cpacr_before_run_raw;
volatile uint32_t g_stpg_fpu_cpacr_after_run_raw;
volatile uint32_t g_stpg_fpu_cpacr_repair_count_raw;

bool stpg_fpu_access_ensure_for_run(void) {
#ifdef PICO_ON_DEVICE
    volatile uint32_t *const cpacr =
        (volatile uint32_t *)(uintptr_t)STPG_FPU_CPACR_ADDRESS;
    const uint32_t before = *cpacr;
    g_stpg_fpu_cpacr_before_run_raw = before;
    if ((before & STPG_RUN_CPACR_ACCESS_MASK) != STPG_RUN_CPACR_ACCESS_MASK) {
        *cpacr = before | STPG_RUN_CPACR_ACCESS_MASK;
        __asm volatile("dsb sy" ::: "memory");
        __asm volatile("isb sy" ::: "memory");
        g_stpg_fpu_cpacr_repair_count_raw += 1u;
    }
    const uint32_t after = *cpacr;
#else
    const uint32_t before = STPG_RUN_CPACR_ACCESS_MASK;
    g_stpg_fpu_cpacr_before_run_raw = before;
    const uint32_t after = before;
#endif
    g_stpg_fpu_cpacr_after_run_raw = after;
    return (after & STPG_RUN_CPACR_ACCESS_MASK) == STPG_RUN_CPACR_ACCESS_MASK;
}
