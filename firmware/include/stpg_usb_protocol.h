#ifndef STPG_USB_PROTOCOL_H
#define STPG_USB_PROTOCOL_H

#include <stdbool.h>
#include <stdint.h>

void stpg_usb_protocol_init(void);
void stpg_usb_protocol_task(void);

/* A protocol RUN response must leave the USB TX path before the runner
 * performs its potentially blocking PSRAM/PIO/DMA preparation. */
void stpg_usb_run_response_begin(void);
bool stpg_usb_run_response_pending(void);
void stpg_usb_run_response_cancel(void);
bool stpg_usb_service_diagnostics(uint32_t words[6]);
bool stpg_usb_quantum_diagnostics(uint32_t words[6]);

#endif
