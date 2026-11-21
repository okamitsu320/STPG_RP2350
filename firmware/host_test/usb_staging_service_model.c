/* Actual USB task/waits and runner staging entry, under deterministic traffic.
 * Not a USB, SWD, IRQ, DMA or QMI hardware simulator. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define STPG_SAMPLE_PSRAM_SRAM_STAGING 1
#define STPG_USB_CDC_READY_WAIT_US 100000u
typedef uint32_t stpg_err_t;
enum { ERR_NONE = 0, ERR_DMA_ERROR = 9 };
static struct { bool active, streaming; uint32_t pending_stream_fail_error; } g_runner;
static uint32_t clock_us, produced, saved, service_calls, failures;
static uint32_t rx_left, accepted, tx_stalls, ready_stalls, sent, task_limit;
static uint8_t wire[1024];
static uint32_t g_usb_protocol_task_call_count_raw, g_usb_available_poll_count_raw;
static uint32_t g_usb_rx_read_call_count_raw, g_usb_rx_byte_count_raw;
static uint32_t g_usb_write_spin_count_raw, g_usb_write_last_success_bytes_raw;
static volatile bool g_run_response_pending;
static struct { uint32_t calls, total_us, max_us, tx_yields, ready_yields, rx_reads; } g_usb_service_diag;
static struct { uint32_t calls, total_us, max_us, ready_at_entry, ready_at_exit, last_us; } g_usb_quantum_diag;
static uint32_t time_us_32(void) { return clock_us++; }
static bool stpg_runner_is_active(void) { return g_runner.active; }
static uint32_t stpg_runner_captured_sample_count(void) { return produced; }
static stpg_err_t stpg_samples_service(uint32_t (*frontier)(void)) {
    assert(g_runner.active && g_runner.streaming && !g_runner.pending_stream_fail_error);
    ++service_calls;
    uint32_t pending = frontier() - saved;
    if (pending > 2032) return ERR_DMA_ERROR;
    saved += pending > 64 ? 64 : pending;
    return ERR_NONE;
}
static void stpg_runner_fail_streaming(stpg_err_t error, bool irq) {
    assert(error == ERR_DMA_ERROR && !irq);
    ++failures;
    g_runner.active = false;
}
static bool tud_task_event_ready(void) { return true; }
static void tud_task_ext(uint32_t timeout_ms, bool in_isr) {
    assert(timeout_ms == 0 && !in_isr);
    assert(++task_limit < 10000);
    if (g_runner.active) produced += 32;
    clock_us += 10;
}
static bool tud_cdc_n_ready(uint8_t itf) {
    assert(itf == 0);
    if (ready_stalls) { --ready_stalls; return false; }
    return true;
}
static uint32_t tud_cdc_available(void) { return rx_left; }
static uint32_t tud_cdc_read(uint8_t *buffer, uint32_t count) {
    if (count > rx_left) count = rx_left;
    for (uint32_t i = 0; i < count; ++i) buffer[i] = (uint8_t)((4096 - rx_left + i) % 251);
    rx_left -= count;
    if (g_runner.active) produced += 32;
    return count;
}
static void stpg_usb_accept_byte(uint8_t byte) {
    assert(byte == (uint8_t)(accepted % 251));
    ++accepted;
}
static uint32_t tud_cdc_write(const uint8_t *buffer, uint32_t length) {
    if (tx_stalls) { --tx_stalls; return 0; }
    uint32_t count = length > 7 ? 7 : length;
    assert(sent + count <= sizeof(wire));
    memcpy(wire + sent, buffer, count);
    sent += count;
    return count;
}
static void tud_cdc_write_flush(void) {}
static void stpg_usb_note_tud_task_call(void) {}
#include "usb-staging-subject.inc"
static void prepare(void) {
    memset(&g_runner, 0, sizeof(g_runner));
    memset(&g_usb_service_diag, 0, sizeof(g_usb_service_diag));
    g_runner.active = g_runner.streaming = true;
    clock_us = produced = saved = service_calls = failures = 0;
    rx_left = accepted = tx_stalls = ready_stalls = sent = task_limit = 0;
}
int main(void) {
    prepare();
    rx_left = 4096;
    while (rx_left) {
        uint32_t before = rx_left;
        stpg_usb_protocol_task();
        assert(before - rx_left <= 64); /* Old unbounded drain must fail. */
        assert(stpg_runner_service_samples());
    }
    assert(accepted == 4096 && !failures && g_usb_service_diag.rx_reads == 64);
    uint8_t response[64];
    for (uint32_t i = 0; i < sizeof(response); ++i) response[i] = (uint8_t)i;
    prepare(); tx_stalls = 160;
    stpg_usb_write_bytes(response, sizeof(response));
    assert(!failures && sent == sizeof(response) && !memcmp(wire, response, sizeof(response)));
    assert(g_usb_service_diag.tx_yields == 160 && service_calls >= 160 && produced - saved < 64);
    prepare(); ready_stalls = 160;
    stpg_usb_write_bytes(response, sizeof(response));
    assert(!failures && sent == sizeof(response) && service_calls >= 159 && produced - saved < 64);
    assert(g_usb_service_diag.ready_yields == 160);
    prepare(); produced = 2663;
    assert(!stpg_runner_service_samples() && failures == 1 && !g_runner.active);
    assert(stpg_runner_service_samples() && failures == 1 && service_calls == 1);
    prepare(); g_runner.pending_stream_fail_error = ERR_DMA_ERROR;
    assert(stpg_runner_service_samples() && !service_calls && !failures);
    prepare(); g_runner.streaming = false;
    assert(stpg_runner_service_samples() && !service_calls);
    memset(&g_usb_service_diag, 0xff, sizeof(g_usb_service_diag));
    memset(&g_usb_quantum_diag, 0xff, sizeof(g_usb_quantum_diag));
    stpg_usb_run_response_begin();
    assert(g_run_response_pending);
    uint32_t words[6];
    assert(stpg_usb_service_diagnostics(words) && !stpg_usb_service_diagnostics(0));
    for (uint32_t i = 0; i < 6; ++i) assert(words[i] == 0);
    assert(stpg_usb_quantum_diagnostics(words) && !stpg_usb_quantum_diagnostics(0));
    for (uint32_t i = 0; i < 6; ++i) assert(words[i] == 0);
    prepare();
    stpg_usb_run_response_begin();
    clock_us = UINT32_MAX - 5u;
    stpg_usb_task_quantum();
    assert(stpg_usb_quantum_diagnostics(words));
    assert(words[0] == 1 && words[1] == 11 && words[2] == 11);
    assert(words[3] == 1 && words[4] == 1 && words[5] == 11);
    assert(produced == saved && saved == 32);
    g_runner.active = false;
    stpg_usb_task_quantum();
    assert(stpg_usb_quantum_diagnostics(words) && words[0] == 1);
    puts("USB staging cooperation PASS");
    return 0;
}
