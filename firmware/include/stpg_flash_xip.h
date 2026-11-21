#ifndef STPG_FLASH_XIP_H
#define STPG_FLASH_XIP_H

#include <stdbool.h>
#include <stdint.h>
#include "stpg_error.h"

/* Capture once during boot, never during RESET or after debugger contact. */
void stpg_flash_xip_init(void);
/* Core0, before RUN hardware starts. A changed M0 requires a lockout-ready
 * previously launched Core1 and disabled owned output/sample PIO. */
stpg_err_t stpg_flash_xip_ensure_for_run(bool safe_to_lockout);
/* Read-only pages77/78/79; neither resets nor repairs state. */
bool stpg_flash_xip_diagnostics(uint32_t page, uint32_t words[6]);

#endif
