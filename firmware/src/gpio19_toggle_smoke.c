#include "pico/stdlib.h"

enum {
    GPIO_SMOKE_PIN = 19,
    GPIO_SMOKE_LED = 25,
    GPIO_SMOKE_LOW_MS = 250,
    GPIO_SMOKE_HIGH_MS = 250,
};

int main(void) {
    gpio_init(GPIO_SMOKE_PIN);
    gpio_set_dir(GPIO_SMOKE_PIN, GPIO_OUT);
    gpio_put(GPIO_SMOKE_PIN, 0);

    gpio_init(GPIO_SMOKE_LED);
    gpio_set_dir(GPIO_SMOKE_LED, GPIO_OUT);
    gpio_put(GPIO_SMOKE_LED, 0);

    while (true) {
        gpio_put(GPIO_SMOKE_PIN, 0);
        gpio_put(GPIO_SMOKE_LED, 0);
        sleep_ms(GPIO_SMOKE_LOW_MS);
        gpio_put(GPIO_SMOKE_PIN, 1);
        gpio_put(GPIO_SMOKE_LED, 1);
        sleep_ms(GPIO_SMOKE_HIGH_MS);
    }
}
