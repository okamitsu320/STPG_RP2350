# Optional GPIO controller

The firmware sketch is
[firmware/rp2040_zero_gpio_toggle.ino](firmware/rp2040_zero_gpio_toggle.ino).
The development setup used an RP2040-Zero; this is separate from the STPG
RP2350 firmware and optional for ordinary STPG operation.

See the [controller manual](../manual/gpio-controller.md) for roles, binding,
safe states and wiring. Compile/upload requires the appropriate Arduino board
support and explicit hardware authorization. This candidate's tests do not
flash the controller or prove electrical safety.
