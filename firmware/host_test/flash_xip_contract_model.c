/* Actual application C with modeled SDK/ROM/hardware: no physical I/O. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "stpg_error.h"
#undef assert
/* Mutation negatives must fail without generating a host core dump. */
#define assert(test) do { if (!(test)) { fprintf(stderr, "Assertion: %s\n", #test); exit(90); } } while (0)
typedef unsigned int uint;
#define NUM_DMA_CHANNELS 16u
#define PICO_OK 0
#define __no_inline_not_in_flash_func(name) name
typedef struct { uint32_t timing, rfmt, rcmd; } model_m;
static struct { model_m m[2]; uint32_t direct_csr; } qmi;
static struct { uint32_t ctrl; } xip;
#define qmi_hw (&qmi)
#define xip_ctrl_hw (&xip)
static bool ready = true, in_safe;
static uint32_t busy_mask, safe_calls, restores, psram_calls, dsb_calls, isb_calls;
static int safe_rc, safe_exit_rc;
static bool mismatch;
static stpg_err_t psram_rc;
static model_m baseline;
static bool multicore_lockout_ready(void) { return ready; }
static bool dma_channel_is_busy(uint n) { assert(in_safe && n < 16); return (busy_mask & (1u << n)) != 0; }
static void flash_start_xip(void) {
    assert(in_safe && busy_mask == 0);
    ++restores;
    qmi.m[0] = baseline;
    if (mismatch) ++qmi.m[0].rcmd;
    qmi.m[1].timing = 0; /* ROM/SDK may disturb the existing CS1 contract. */
}
static void __dsb(void) { assert(in_safe); ++dsb_calls; }
static void __isb(void) { assert(in_safe); ++isb_calls; }
static int flash_safe_execute(void (*cb)(void *), void *p, uint32_t timeout) {
    assert(ready && !in_safe && timeout == 100);
    ++safe_calls;
    if (safe_rc) return safe_rc;
    in_safe = true; cb(p); in_safe = false;
    if (restores && !mismatch && psram_rc == ERR_NONE) assert(qmi.m[1].timing == 77);
    return safe_exit_rc;
}
static stpg_err_t stpg_psram_prepare_access(void) {
    assert(in_safe && restores == 1);
    ++psram_calls;
    if (psram_rc == ERR_NONE) qmi.m[1].timing = 77;
    return psram_rc;
}
#include "flash-xip-subject.inc"
static void page(uint32_t n, uint32_t w[6]) { assert(stpg_flash_xip_diagnostics(n, w)); }
int main(int argc, char **argv) {
    assert(argc == 2);
    const int scenario = atoi(argv[1]);
    baseline = (model_m){0x60000202, 0x1000eb, 0xa0eb};
    qmi.m[0] = baseline; qmi.m[1].timing = 77;
    if (scenario != 1) stpg_flash_xip_init();
    if (scenario != 0) qmi.m[0] = (model_m){12, 0, 3};
    if (scenario == 2) ready = false;
    if (scenario == 4) safe_rc = -2;
    if (scenario == 5) safe_exit_rc = -2;
    if (scenario == 6) mismatch = true;
    if (scenario == 7) psram_rc = ERR_PSRAM_UNAVAILABLE;
    if (scenario >= 10 && scenario < 26) busy_mask = 1u << (scenario - 10);
    if (scenario == 9) stpg_flash_xip_init(); /* never recapture slow post-debug state */
    const stpg_err_t err = stpg_flash_xip_ensure_for_run(scenario != 3);
    uint32_t w[6], again[6]; page(78, w);
    if (scenario == 0) {
        assert(err == ERR_NONE && safe_calls == 0 && restores == 0 && psram_calls == 0 && w[5] == 0);
    } else if (scenario == 1) {
        assert(err == ERR_BAD_STATE && safe_calls == 0 && w[5] == 1);
    } else if (scenario == 2 || scenario == 3) {
        assert(err == ERR_BAD_STATE && safe_calls == 0 && restores == 0 && w[5] == 2);
    } else if (scenario == 4 || scenario == 5) {
        assert(err == ERR_BAD_STATE && safe_calls == 1 && w[5] == 3 && psram_calls == (scenario == 5));
        assert(restores == (scenario == 5));
    } else if (scenario == 6) {
        assert(err == ERR_BAD_STATE && w[5] == 5 && psram_calls == 0);
    } else if (scenario == 7) {
        assert(err == ERR_PSRAM_UNAVAILABLE && w[5] == 6 && psram_calls == 1);
    } else if (scenario >= 10 && scenario < 26) {
        assert(err == ERR_BAD_STATE && w[5] == 4 && restores == 0 && psram_calls == 0);
    } else {
        assert(err == ERR_NONE && w[5] == 0 && safe_calls == 1 && restores == 1 && psram_calls == 1);
        assert(qmi.m[1].timing == 77 && dsb_calls == 1 && isb_calls == 1);
        assert(w[0] == baseline.timing && w[1] == baseline.rfmt && w[2] == baseline.rcmd);
        assert(w[3] == 1 && w[4] == 1);
        page(77, w);
        assert(w[0] == baseline.timing && w[1] == baseline.rfmt && w[2] == baseline.rcmd);
        assert(w[3] == 12 && w[4] == 0 && w[5] == 3);
        assert(stpg_flash_xip_ensure_for_run(false) == ERR_NONE); /* matching: no lockout needed */
        assert(safe_calls == 1 && restores == 1 && psram_calls == 1);
        page(78, w); assert(w[3] == 2 && w[4] == 1);
    }
    for (uint32_t n = 77; n <= 79; ++n) {
        page(n, w); page(n, again);
        assert(memcmp(w, again, sizeof(w)) == 0);
    }
    assert(!stpg_flash_xip_diagnostics(80, w));
    assert(!stpg_flash_xip_diagnostics(77, NULL));
    printf("PASS scenario=%d (SDK/ROM/hardware modeled, not measured)\n", scenario);
    return 0;
}
