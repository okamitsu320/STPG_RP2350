#ifndef STPG_USB_L2V1_PAD_NOPS
#define STPG_USB_L2V1_PAD_NOPS 2
#endif

    .syntax unified
    .thumb

    .section .text.stpg_usb_pad_before_tud_task_ext,"axR",%progbits
    .global stpg_usb_pad_before_tud_task_ext
    .type stpg_usb_pad_before_tud_task_ext, %function
stpg_usb_pad_before_tud_task_ext:
    .rept STPG_USB_L2V1_PAD_NOPS
    nop
    .endr
    bx lr
    .size stpg_usb_pad_before_tud_task_ext, . - stpg_usb_pad_before_tud_task_ext
