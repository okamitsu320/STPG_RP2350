#include "stpg_flash_xip.h"
#include "stpg_psram.h"

#ifdef PICO_ON_DEVICE
#include "hardware/dma.h"
#include "hardware/flash.h"
#include "hardware/structs/qmi.h"
#include "hardware/structs/xip.h"
#include "hardware/sync.h"
#include "pico/flash.h"
#include "pico/multicore.h"

typedef struct {
    uint32_t timing, rfmt, rcmd;
} stpg_flash_xip_state_t;

static stpg_flash_xip_state_t g_xip_boot, g_xip_before, g_xip_after;
static bool g_xip_boot_valid;
static uint32_t g_xip_calls, g_xip_attempts, g_xip_reason;

static stpg_flash_xip_state_t stpg_flash_xip_read(void) {
    return (stpg_flash_xip_state_t){qmi_hw->m[0].timing, qmi_hw->m[0].rfmt,
                                  qmi_hw->m[0].rcmd};
}

static bool stpg_flash_xip_matches(stpg_flash_xip_state_t state) {
    return state.timing == g_xip_boot.timing && state.rfmt == g_xip_boot.rfmt
        && state.rcmd == g_xip_boot.rcmd;
}

void stpg_flash_xip_init(void) {
    if (!g_xip_boot_valid) {
        g_xip_boot = stpg_flash_xip_read();
        g_xip_boot_valid = true;
    }
}

/* Only SDK safe-zone execution enters here: Core1 is in its SRAM lockout
 * handler and Core0 IRQs are masked. No acquisition is active. DMA continues
 * during CPU lockout, so reject any busy channel before changing QSPI state.
 * The SDK restores boot XIP via ROM/SRAM, not raw M0 writes: a debugger may
 * also have exited the flash chip's continuous-read mode. */
static void __no_inline_not_in_flash_func(stpg_flash_xip_restore_cb)(void *param) {
    uint32_t *reason = (uint32_t *)param;
    for (uint channel = 0u; channel < NUM_DMA_CHANNELS; ++channel) {
        if (dma_channel_is_busy(channel)) {
            return;
        }
    }
    flash_start_xip();
    __dsb();
    __isb();
    if (qmi_hw->m[0].timing != g_xip_boot.timing
        || qmi_hw->m[0].rfmt != g_xip_boot.rfmt
        || qmi_hw->m[0].rcmd != g_xip_boot.rcmd) {
        *reason = 5u;
        return;
    }
    /* Flash is now available at the captured boot contract. The existing
     * PSRAM helper may execute from Flash, but keep Core1 locked and Core0
     * IRQs masked until it has restored CS1: the interrupted old producer
     * must not resume into the SDK's temporary M1 configuration. */
    if (stpg_psram_prepare_access() != ERR_NONE) {
        *reason = 6u;
        return;
    }
    *reason = 0u;
}

stpg_err_t stpg_flash_xip_ensure_for_run(bool safe_to_lockout) {
    ++g_xip_calls;
    g_xip_reason = 0u;
    g_xip_before = stpg_flash_xip_read();
    g_xip_after = g_xip_before;
    if (!g_xip_boot_valid) {
        g_xip_reason = 1u;
        return ERR_BAD_STATE;
    }
    if (stpg_flash_xip_matches(g_xip_before)) {
        return ERR_NONE;
    }
    /* Do not assume an unlaunched/halted/uncooperative Core1 is safe, and
     * avoid the SDK's assert-on-unsafe path. No repair is made on rejection. */
    if (!safe_to_lockout || !multicore_lockout_ready()) {
        g_xip_reason = 2u;
        return ERR_BAD_STATE;
    }
    uint32_t callback_reason = 4u;
    ++g_xip_attempts;
    const int rc = flash_safe_execute(stpg_flash_xip_restore_cb, &callback_reason, 100u);
    g_xip_after = stpg_flash_xip_read();
    if (rc != PICO_OK) {
        g_xip_reason = 3u;
        return ERR_BAD_STATE;
    }
    if (callback_reason != 0u) {
        g_xip_reason = callback_reason;
        return callback_reason == 6u ? ERR_PSRAM_UNAVAILABLE : ERR_BAD_STATE;
    }
    if (!stpg_flash_xip_matches(g_xip_after)) {
        g_xip_reason = 5u;
        return ERR_BAD_STATE;
    }
    return ERR_NONE;
}

bool stpg_flash_xip_diagnostics(uint32_t page, uint32_t words[6]) {
    if (words == 0) return false;
    switch (page) {
    case 77u:
        words[0] = g_xip_boot.timing; words[1] = g_xip_boot.rfmt; words[2] = g_xip_boot.rcmd;
        words[3] = g_xip_before.timing; words[4] = g_xip_before.rfmt; words[5] = g_xip_before.rcmd;
        return true;
    case 78u:
        words[0] = g_xip_after.timing; words[1] = g_xip_after.rfmt; words[2] = g_xip_after.rcmd;
        words[3] = g_xip_calls; words[4] = g_xip_attempts; words[5] = g_xip_reason;
        return true;
    case 79u:
        words[0] = qmi_hw->m[0].timing; words[1] = qmi_hw->m[0].rfmt; words[2] = qmi_hw->m[0].rcmd;
        words[3] = xip_ctrl_hw->ctrl; words[4] = qmi_hw->m[1].timing; words[5] = qmi_hw->direct_csr;
        return true;
    default: return false;
    }
}
#else
void stpg_flash_xip_init(void) {}
stpg_err_t stpg_flash_xip_ensure_for_run(bool safe_to_lockout) {
    (void)safe_to_lockout;
    return ERR_NONE;
}
bool stpg_flash_xip_diagnostics(uint32_t page, uint32_t words[6]) {
    (void)page; (void)words;
    return false;
}
#endif
