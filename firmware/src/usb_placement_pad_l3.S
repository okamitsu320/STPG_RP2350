#ifndef STPG_USB_L3_CLUSTER_PAD_NOPS
#define STPG_USB_L3_CLUSTER_PAD_NOPS 0
#endif

.section .stpg_usb_l3_cluster_pad, "ax", %progbits
.thumb
.balign 4
.global stpg_usb_l3_cluster_pad
stpg_usb_l3_cluster_pad:
    .rept STPG_USB_L3_CLUSTER_PAD_NOPS
    nop
    .endr
