#ifndef STPG_USB_L1V2_PAD_NOPS
#define STPG_USB_L1V2_PAD_NOPS 2
#endif

#define STPG_STRINGIFY_IMPL(x) #x
#define STPG_STRINGIFY(x) STPG_STRINGIFY_IMPL(x)

    .section .text.stpg_usb_pad_after_tud_task_ext,"axR",%progbits
    .global stpg_usb_pad_after_tud_task_ext
    .type stpg_usb_pad_after_tud_task_ext, %function
stpg_usb_pad_after_tud_task_ext:
    .rept STPG_USB_L1V2_PAD_NOPS
    nop
    .endr
    .size stpg_usb_pad_after_tud_task_ext, .-stpg_usb_pad_after_tud_task_ext
