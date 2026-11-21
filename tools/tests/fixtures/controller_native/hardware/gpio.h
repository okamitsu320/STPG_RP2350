#pragma once
constexpr bool GPIO_IN = false;
constexpr bool GPIO_OUT = true;
void gpio_init(unsigned pin);
void gpio_set_dir(unsigned pin, bool direction);
void gpio_disable_pulls(unsigned pin);
void gpio_put(unsigned pin, bool value);
