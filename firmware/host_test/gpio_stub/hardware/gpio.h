#include <stdbool.h>
#include <stdint.h>
#define GPIO_IN false
#define GPIO_OUT true
#define GPIO_FUNC_XIP_CS1 9u
void gpio_init(uint32_t gpio);
void gpio_set_dir(uint32_t gpio, bool direction);
void gpio_put(uint32_t gpio, bool value);
void gpio_disable_pulls(uint32_t gpio);
void gpio_pull_down(uint32_t gpio);
void gpio_set_input_enabled(uint32_t gpio, bool enabled);
void gpio_set_function(uint32_t gpio, uint32_t function);
