/* Extracted actual TinyUSB task body; queue/callback timing is a host model,
 * not a USB/PIO/DMA/QMI or interrupt-latency measurement. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define OPT_OS_NONE 0
#define OPT_OS_PICO 1
#ifndef CFG_TUSB_OS
#define CFG_TUSB_OS OPT_OS_PICO
#endif
#define CFG_TUSB_DEBUG 0
#define CFG_TUD_LOG_LEVEL 2
#define TU_LOG_USBD(...) ((void)0)
#define TU_LOG_BUF(...) ((void)0)
#define TU_ASSERT(c, ...) do { if (!(c)) { assert(c); return __VA_ARGS__; } } while (0)
#define TU_BREAKPOINT() (++breakpoints)
#define TUSB_DIR_OUT 0
#define TUSB_DIR_IN 1
#define TUSB_DIR_IN_MASK 0x80
#define SOF_CONSUMER_USER 0
enum { DCD_EVENT_BUS_RESET, DCD_EVENT_UNPLUGGED, DCD_EVENT_SETUP_RECEIVED,
       DCD_EVENT_XFER_COMPLETE, DCD_EVENT_SUSPEND, DCD_EVENT_RESUME,
       USBD_EVENT_FUNC_CALL, DCD_EVENT_SOF, DCD_EVENT_COUNT };
typedef int xfer_result_t;
typedef struct {
    uint8_t event_id, rhport;
    struct { uint8_t speed; } bus_reset;
    struct { uint32_t id; } setup_received;
    struct { uint8_t ep_addr, result; uint32_t len; } xfer_complete;
    struct { void (*func)(void *); void *param; } func_call;
    struct { uint32_t frame_count; } sof;
} dcd_event_t;
typedef struct { void (*xfer_cb)(uint8_t, uint8_t, xfer_result_t, uint32_t); } usbd_class_driver_t;
static struct {
    uint8_t speed, connected, remote_wakeup_en, sof_consumer;
    struct { uint8_t busy, claimed; } ep_status[16][2];
    uint8_t ep2drv[16][2];
} _usbd_dev;
static uint32_t _usbd_queued_setup;
static dcd_event_t queue[512];
static uint32_t head, tail, popped, callback_count, regen, produced, saved, faults;
static uint32_t callback_cost, clock_us, resets, setups, stalls, ep0, bulk, suspends, resumes, sofs, unmounts, breakpoints;
static bool initialized;
#define _usbd_q ((void *)0)
static bool tud_inited(void) { return initialized; }
static void enqueue(dcd_event_t event) {
    assert(tail < 512);
    queue[tail++] = event;
    if (event.event_id == DCD_EVENT_SETUP_RECEIVED) ++_usbd_queued_setup;
}
static bool osal_queue_receive(void *q, dcd_event_t *event, uint32_t timeout_ms) {
    (void)q; (void)timeout_ms;
    if (head == tail) return false;
    *event = queue[head++]; ++popped;
    return true;
}
static bool osal_queue_empty(void *q) { (void)q; return head == tail; }
static uint8_t tu_edpt_number(uint8_t addr) { return addr & 15; }
static uint8_t tu_edpt_dir(uint8_t addr) { return addr >> 7; }
static bool tu_bit_test(uint8_t bits, unsigned bit) { return (bits & (1u << bit)) != 0; }
static void usbd_reset(uint8_t rhport) { assert(rhport == 0); ++resets; }
static void tud_umount_cb(void) { ++unmounts; }
static bool process_control_request(uint8_t rhport, const void *request) {
    assert(rhport == 0 && request); ++setups; return true;
}
static void dcd_edpt_stall(uint8_t rhport, uint8_t addr) { (void)rhport; (void)addr; ++stalls; }
static void usbd_control_xfer_cb(uint8_t rhport, uint8_t addr, xfer_result_t result, uint32_t len) {
    assert(rhport == 0 && addr == 0x80 && result == 0 && len == 64); ++ep0;
}
static void bulk_callback(uint8_t rhport, uint8_t addr, xfer_result_t result, uint32_t len) {
    assert(rhport == 0 && addr == 0x82 && result == 0 && len == 64); ++bulk;
}
static const usbd_class_driver_t *get_driver(uint8_t id) {
    static const usbd_class_driver_t driver = {bulk_callback}; assert(id == 0); return &driver;
}
static void tud_suspend_cb(uint8_t remote) { assert(remote == 1); ++suspends; }
static void tud_resume_cb(void) { ++resumes; }
static void tud_sof_cb(uint32_t frame) { assert(frame == 123); ++sofs; }
static void continuous(void *param) {
    assert((uintptr_t)param == callback_count); /* FIFO order across quanta */
    ++callback_count; produced += callback_cost; clock_us += callback_cost;
    if (regen) {
        --regen;
        dcd_event_t event = {.event_id=USBD_EVENT_FUNC_CALL};
        event.func_call.func=continuous; event.func_call.param=(void *)(uintptr_t)callback_count;
        enqueue(event);
    }
}
#include "tinyusb-event-subject.inc"
static void service(void) {
    uint32_t pending = produced - saved;
    if (pending > 2032) { ++faults; return; }
    saved += pending > 64 ? 64 : pending;
}
static void reset_model(void) {
    head=tail=popped=callback_count=regen=produced=saved=faults=clock_us=0;
    resets=setups=stalls=ep0=bulk=suspends=resumes=sofs=unmounts=breakpoints=0;
    _usbd_queued_setup=0; initialized=true; callback_cost=32;
    memset(&_usbd_dev, 0, sizeof(_usbd_dev));
}
int main(void) {
    reset_model(); regen=199;
    dcd_event_t event={.event_id=USBD_EVENT_FUNC_CALL}; event.func_call.func=continuous;
    enqueue(event);
    uint32_t turns=0;
    while (!osal_queue_empty(_usbd_q)) {
        uint32_t before=popped;
        tud_task_ext(0, false);
#if defined(CFG_TUD_TASK_EVENTS_PER_RUN) && CFG_TUD_TASK_EVENTS_PER_RUN > 0
        assert(popped-before <= CFG_TUD_TASK_EVENTS_PER_RUN);
#else
        assert(popped-before == 200);
#endif
        service(); ++turns;
    }
    assert(callback_count==200 && popped==200);
#if defined(CFG_TUD_TASK_EVENTS_PER_RUN) && CFG_TUD_TASK_EVENTS_PER_RUN == 1
    assert(turns==200 && faults==0 && produced==saved);
#else
    assert(faults>0); /* Old drain or too-large service work deficit. */
#endif
    reset_model(); callback_cost=2663; enqueue(event);
    tud_task_ext(0,false); service(); assert(faults==1); /* One slow callback is NOT bounded. */

    reset_model();
    event=(dcd_event_t){.event_id=DCD_EVENT_SETUP_RECEIVED}; enqueue(event); enqueue(event);
    event=(dcd_event_t){.event_id=DCD_EVENT_XFER_COMPLETE};
    event.xfer_complete.ep_addr=0x80; event.xfer_complete.len=64; enqueue(event);
    event.xfer_complete.ep_addr=0x82; enqueue(event);
    _usbd_dev.remote_wakeup_en=1; _usbd_dev.sof_consumer=1;
    _usbd_dev.ep_status[0][1].busy=_usbd_dev.ep_status[2][1].busy=1;
    event=(dcd_event_t){.event_id=DCD_EVENT_SUSPEND}; enqueue(event);
    event.event_id=DCD_EVENT_RESUME; enqueue(event);
    event.event_id=DCD_EVENT_SOF; event.sof.frame_count=123; enqueue(event);
    event.event_id=DCD_EVENT_BUS_RESET; event.bus_reset.speed=2; enqueue(event);
    event.event_id=DCD_EVENT_UNPLUGGED; enqueue(event);
    event.event_id=255; enqueue(event);
    while (!osal_queue_empty(_usbd_q)) tud_task_ext(0,false);
    assert(popped==10 && setups==1 && !_usbd_queued_setup && !stalls);
    assert(ep0==1 && bulk==1 && suspends==1 && resumes==1 && sofs==1);
    assert(resets==2 && unmounts==1 && breakpoints==1 && _usbd_dev.speed==2);
    assert(!_usbd_dev.ep_status[0][1].busy && !_usbd_dev.ep_status[2][1].busy);
    uint32_t before=popped; tud_task_ext(0,false); assert(popped==before);
    enqueue(event); initialized=false; tud_task_ext(0,false); assert(popped==before && head<tail);
    puts("actual TinyUSB quantum model PASS; old starvation and slow-callback limits demonstrated");
}
