#include "pico/stdlib.h"

volatile uint32_t g_swd_probe_magic __attribute__((used, externally_visible, section(".swd_probe_data"))) = 0x53574450u;
volatile uint32_t g_swd_probe_counter __attribute__((used, externally_visible, section(".swd_probe_data"))) = 0u;
volatile uint32_t g_swd_probe_last_state __attribute__((used, externally_visible, section(".swd_probe_data"))) = 0u;

enum {
    SWD_PROBE_LED_PIN = 25,
    SWD_PROBE_DELAY_MS = 200,
};

int main(void) {
    gpio_init(SWD_PROBE_LED_PIN);
    gpio_set_dir(SWD_PROBE_LED_PIN, GPIO_OUT);
    gpio_put(SWD_PROBE_LED_PIN, 0);

    while (true) {
        if (g_swd_probe_magic != 0x53574450u) {
            g_swd_probe_last_state = 0xdeadbeefu;
        }

        g_swd_probe_last_state = 0u;
        gpio_put(SWD_PROBE_LED_PIN, 0);
        g_swd_probe_counter++;
        sleep_ms(SWD_PROBE_DELAY_MS);

        g_swd_probe_last_state = 1u;
        gpio_put(SWD_PROBE_LED_PIN, 1);
        g_swd_probe_counter++;
        sleep_ms(SWD_PROBE_DELAY_MS);
    }
}
