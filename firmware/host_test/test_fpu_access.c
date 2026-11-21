#include <assert.h>

#include "stpg_fpu_access.h"

int main(void) {
    g_stpg_fpu_cpacr_before_run_raw = 0u;
    g_stpg_fpu_cpacr_after_run_raw = 0u;
    g_stpg_fpu_cpacr_repair_count_raw = 0u;

    assert(stpg_fpu_access_ensure_for_run());
    assert(STPG_RUN_CPACR_ACCESS_MASK == 0x00F00303u);
    assert((STPG_RUN_CPACR_ACCESS_MASK & STPG_RUN_CPACR_CP0_ACCESS_MASK)
           == STPG_RUN_CPACR_CP0_ACCESS_MASK);
    assert((STPG_RUN_CPACR_ACCESS_MASK & STPG_RUN_CPACR_CP4_ACCESS_MASK)
           == STPG_RUN_CPACR_CP4_ACCESS_MASK);
    assert((STPG_RUN_CPACR_ACCESS_MASK & STPG_FPU_CPACR_ACCESS_MASK)
           == STPG_FPU_CPACR_ACCESS_MASK);
    assert(g_stpg_fpu_cpacr_before_run_raw == STPG_RUN_CPACR_ACCESS_MASK);
    assert(g_stpg_fpu_cpacr_after_run_raw == STPG_RUN_CPACR_ACCESS_MASK);
    assert(g_stpg_fpu_cpacr_repair_count_raw == 0u);
    return 0;
}
