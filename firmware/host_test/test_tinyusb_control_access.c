/* Execute the real pinned RP2 driver with host MMIO substitutes, not a model
 * of its transfer algorithm. The source path is supplied by the Python test. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>

#define RP2040_COMMON_H_
#define CFG_TUSB_MCU 1
#define OPT_MCU_RP2040 1
#define TUD_OPT_RP2040_USB_DEVICE_UFRAME_FIX 0
#define __tusb_irq_path_func(x) x
#define TU_ATTR_ALWAYS_INLINE __attribute__((always_inline))
#define __unused __attribute__((unused))
#define TU_LOG(...) ((void)0)
#define TU_LOG2_INT(...) ((void)0)
#define pico_trace(...) ((void)0)
#define USB_MAIN_CTRL_HOST_NDEVICE_BITS 2u
#define RESETS_RESET_USBCTRL_BITS 1u
#define USB_USB_MUXING_TO_PHY_BITS 1u
#define USB_USB_MUXING_SOFTCON_BITS 8u
#define USB_BUF_CTRL_LEN_MASK 0x3ffu
#define USB_BUF_CTRL_AVAIL 0x400u
#define USB_BUF_CTRL_SEL 0x1000u
#define USB_BUF_CTRL_DATA1_PID 0x2000u
#define USB_BUF_CTRL_DATA0_PID 0u
#define USB_BUF_CTRL_LAST 0x4000u
#define USB_BUF_CTRL_FULL 0x8000u
#define EP_CTRL_DOUBLE_BUFFERED_BITS 0x40000000u
#define EP_CTRL_INTERRUPT_PER_DOUBLE_BUFFER 0x10000000u
#define EP_CTRL_INTERRUPT_PER_BUFFER 0x20000000u
#define USB_INTS_DEV_SOF_BITS 1u
#define tu_min16(a, b) ((uint16_t)((a) < (b) ? (a) : (b)))
#define tu_edpt_dir(a) (((a) >> 7u) & 1u)
#define tu_edpt_number(a) ((a) & 0x0fu)

typedef volatile uint32_t io_rw_32;
typedef struct { io_rw_32 main_ctrl, muxing, inte; } usb_hw_t;
static usb_hw_t stub_usb;
static usb_hw_t *const usb_hw = &stub_usb;
static usb_hw_t *const usb_hw_set = &stub_usb;
static struct { uint8_t bytes[4096]; } stub_dpram;
#define usb_dpram (&stub_dpram)
static void reset_block(uint32_t bits) { (void)bits; }
static void unreset_block_wait(uint32_t bits) { (void)bits; }
static void busy_wait_at_least_cycles(uint32_t cycles) { assert(cycles == 12); }
static jmp_buf panic_return;
static bool expect_panic;
static _Noreturn void panic(const char *format, ...) {
    (void)format;
    if (expect_panic) longjmp(panic_return, 1);
    abort();
}
typedef struct hw_endpoint {
    bool configured, rx;
    uint8_t ep_addr, next_pid;
    io_rw_32 *endpoint_control, *buffer_control;
    uint8_t *hw_data_buf, *user_buf;
    uint16_t remaining_len, xferred_len, wMaxPacketSize;
    bool active;
    uint8_t transfer_type, pending;
} hw_endpoint_t;
static inline void hw_endpoint_lock_update(hw_endpoint_t *ep, int delta) { (void)ep; (void)delta; }
void _hw_endpoint_buffer_control_update32(hw_endpoint_t *, uint32_t, uint32_t);
static inline uint32_t _hw_endpoint_buffer_control_get_value32(hw_endpoint_t *ep) { return *ep->buffer_control; }
static inline void _hw_endpoint_buffer_control_set_value32(hw_endpoint_t *ep, uint32_t value) {
    _hw_endpoint_buffer_control_update32(ep, 0, value);
}
#include STPG_TEST_USB_SOURCE

/* The ARM -O3 test inspects only this wrapper: all payload accesses must be
 * byte loads/stores even when both pointers are unaligned. */
__attribute__((noinline)) void stpg_test_dpram_copy(void *dst, const void *src, size_t length) {
    unaligned_memcpy(dst, src, length);
}

#ifndef STPG_USB_ACCESS_ARM_AUDIT
static void transfer(bool ep0, bool host, bool rx, unsigned length, unsigned offset) {
    uint8_t user[260], hardware[136];
    memset(user, 0xa5, sizeof(user));
    memset(hardware, 0x5a, sizeof(hardware));
    uint8_t *buffer = user + 4 + offset;
    if (!rx) for (unsigned i = 0; i < length; ++i) buffer[i] = (uint8_t)(i * 13u + 7u);
    io_rw_32 control = 0x80000180u, buf_ctrl = 0;
    hw_endpoint_t ep = {.configured = true, .rx = rx, .ep_addr = (uint8_t)((rx != host ? 0u : 0x80u) | (ep0 ? 0u : 2u)),
                        .endpoint_control = ep0 && !host ? NULL : &control, .buffer_control = &buf_ctrl,
                        .hw_data_buf = hardware + offset, .wMaxPacketSize = 64};
    stub_usb.main_ctrl = host ? 2u : 0u;
    hw_endpoint_xfer_start(&ep, length == 0 ? NULL : buffer, (uint16_t)length);
    unsigned completed = 0, transactions = 0;
    bool done = false;
    do {
        unsigned a = buf_ctrl & USB_BUF_CTRL_LEN_MASK;
        unsigned b = (buf_ctrl >> 16u) & USB_BUF_CTRL_LEN_MASK;
        if (ep0 && !host) assert((buf_ctrl >> 16u) == 0); /* no phantom EP0 buffer1 */
        assert(a <= 64 && b <= 64 && completed + a + b <= length);
        if (rx) {
            for (unsigned i = 0; i < a; ++i) ep.hw_data_buf[i] = (uint8_t)((completed + i) * 13u + 7u);
            for (unsigned i = 0; i < b; ++i) ep.hw_data_buf[64u + i] = (uint8_t)((completed + a + i) * 13u + 7u);
        } else {
            assert(memcmp(ep.hw_data_buf, buffer + completed, a) == 0);
            assert(memcmp(ep.hw_data_buf + 64, buffer + completed + a, b) == 0);
        }
        printf("%u:%u:%08x:%08x;", a, b, (unsigned)control, (unsigned)buf_ctrl);
        buf_ctrl &= ~(USB_BUF_CTRL_AVAIL | USB_BUF_CTRL_FULL | ((USB_BUF_CTRL_AVAIL | USB_BUF_CTRL_FULL) << 16u));
        if (rx) buf_ctrl |= USB_BUF_CTRL_FULL | (b ? USB_BUF_CTRL_FULL << 16u : 0);
        completed += a + b;
        done = hw_endpoint_xfer_continue(&ep);
        assert(ep.xferred_len == completed && ++transactions <= 10);
    } while (!done);
    assert(completed == length && ep.remaining_len == 0);
    for (unsigned i = 0; i < length; ++i) assert(buffer[i] == (uint8_t)(i * 13u + 7u));
    for (unsigned i = 0; i < 4 + offset; ++i) assert(user[i] == 0xa5);
    for (unsigned i = 4 + offset + length; i < sizeof(user); ++i) assert(user[i] == 0xa5);
    puts("PASS");
}

static void short_out(bool ep0, unsigned offset) {
    uint8_t user[130], hardware[130];
    memset(user, 0, sizeof(user));
    memset(hardware, 0x39, sizeof(hardware));
    io_rw_32 control = 0x80000180u, buf_ctrl = 0;
    hw_endpoint_t ep = {.configured = true, .rx = true, .ep_addr = ep0 ? 0u : 2u,
                        .endpoint_control = ep0 ? NULL : &control, .buffer_control = &buf_ctrl,
                        .hw_data_buf = hardware + offset, .wMaxPacketSize = 64};
    stub_usb.main_ctrl = 0;
    hw_endpoint_xfer_start(&ep, user + offset, 128);
    buf_ctrl = 17u | USB_BUF_CTRL_FULL;
    assert(hw_endpoint_xfer_continue(&ep) && ep.xferred_len == 17 && ep.remaining_len == 0);
    assert(memcmp(user + offset, hardware + offset, 17) == 0 && user[offset + 17] == 0);
    puts("PASS");
}

int main(int argc, char **argv) {
    assert(argc >= 2);
    if (!strcmp(argv[1], "copy")) {
        for (unsigned a = 0; a < 4; ++a) for (unsigned b = 0; b < 4; ++b) {
            uint8_t src[140], dst[140];
            for (unsigned i = 0; i < sizeof(src); ++i) src[i] = (uint8_t)i;
            memset(dst, 0xa5, sizeof(dst));
            stpg_test_dpram_copy(dst + a, src + b, 129);
            assert(memcmp(dst + a, src + b, 129) == 0);
            for (unsigned i = a + 129; i < sizeof(dst); ++i) assert(dst[i] == 0xa5);
        }
        puts("PASS");
    } else if (!strcmp(argv[1], "inactive")) {
        hw_endpoint_t ep = {0};
        assert(!hw_endpoint_xfer_continue(&ep)); /* original stale EP0 behavior retained */
        ep.ep_addr = 2;
        expect_panic = true;
        if (setjmp(panic_return) == 0) { (void)hw_endpoint_xfer_continue(&ep); abort(); }
        puts("PASS");
    } else if (!strcmp(argv[1], "short")) {
        assert(argc == 4);
        short_out(atoi(argv[2]) != 0, (unsigned)atoi(argv[3]));
    } else {
        assert(argc == 7);
        transfer(atoi(argv[2]) != 0, atoi(argv[3]) != 0, atoi(argv[4]) != 0,
                 (unsigned)atoi(argv[5]), (unsigned)atoi(argv[6]));
    }
    return 0;
}
#endif
