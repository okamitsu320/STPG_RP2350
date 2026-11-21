#ifndef STPG_USB_L1_AFTER_TUDEXT_PAD_NOPS
#define STPG_USB_L1_AFTER_TUDEXT_PAD_NOPS 2
#endif

#define STPG_STRINGIFY_IMPL(x) #x
#define STPG_STRINGIFY(x) STPG_STRINGIFY_IMPL(x)

__asm__(
    ".section .stpg_usb_pad_after_tud_task_ext,\"ax\",%progbits\n"
    ".global stpg_usb_pad_after_tud_task_ext\n"
    "stpg_usb_pad_after_tud_task_ext:\n"
    ".rept " STPG_STRINGIFY(STPG_USB_L1_AFTER_TUDEXT_PAD_NOPS) "\n"
    "nop\n"
    ".endr\n"
    ".size stpg_usb_pad_after_tud_task_ext, .-stpg_usb_pad_after_tud_task_ext\n"
    ".previous\n");
