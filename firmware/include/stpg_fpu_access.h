#ifndef STPG_FPU_ACCESS_H
#define STPG_FPU_ACCESS_H

#include <stdbool.h>
#include <stdint.h>

#define STPG_FPU_CPACR_ADDRESS 0xE000ED88u
#define STPG_RUN_CPACR_CP0_ACCESS_MASK 0x00000003u
#define STPG_RUN_CPACR_CP4_ACCESS_MASK 0x00000300u
#define STPG_FPU_CPACR_ACCESS_MASK 0x00F00000u
#define STPG_RUN_CPACR_ACCESS_MASK \
    (STPG_RUN_CPACR_CP0_ACCESS_MASK | STPG_RUN_CPACR_CP4_ACCESS_MASK | \
     STPG_FPU_CPACR_ACCESS_MASK)

extern volatile uint32_t g_stpg_fpu_cpacr_before_run_raw;
extern volatile uint32_t g_stpg_fpu_cpacr_after_run_raw;
extern volatile uint32_t g_stpg_fpu_cpacr_repair_count_raw;

/* This function is compiled with -mgeneral-regs-only.  It is safe to call
 * before the RP2350 GPIO (CP0), double (CP4), and FP (CP10/CP11)
 * coprocessor access used by RUN and upload finalization is established. */
bool stpg_fpu_access_ensure_for_run(void);

#endif
