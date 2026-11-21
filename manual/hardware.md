# Hardware

Only the STPG board is required. Everything else is optional.

| Part | Needed for |
| --- | --- |
| STPG board with the STPG firmware | Any real run |
| GPIO controller board | Driving reset lines and general pins from the Runner ([GPIO controller](gpio-controller.md)) |
| Logic analyzer board | Capturing the real waveform ([Logic analyzer](logic-analyzer.md)) |
| Debug probe | Firmware development only (see below); not needed to install or use the firmware, and not used by `stpg-view` |

The Runner never needs the optional parts. A part you have not selected for a run is not contacted.

## What the STPG board must have

These requirements come from the firmware itself, so they hold whatever a seller's page shows. A listing can be
shown in another language, change or disappear; it does not replace this section.

| Requirement | Why |
| --- | --- |
| An RP2350 in the RP2350A package (30 GPIOs), as on a Raspberry Pi Pico 2 | The firmware is built for the `pico2` board definition |
| 8 MB QSPI PSRAM on the RP2350's second chip select (QMI CS1), wired to GPIO19 | Patterns, results and staging live there; the firmware's memory layout uses all 8 MB. GPIO19 is therefore not a pattern pin |
| 4 MB of flash, as on a Pico 2 | The firmware is built for 4 MB: the first 1 MB holds the firmware, the rest three stored pattern slots. Other sizes have not been checked |
| The RP2350's USB port, as a USB device | The host talks to the firmware over USB serial (CDC) |
| Pattern pins brought out: GPIO2–GPIO17 for 16 pins; for 24 pins also GPIO18, GPIO20–GPIO23 and GPIO26–GPIO28 | Logical DUT0–DUT15, then DUT16–DUT23, in this order |

GPIO0 and GPIO1 are not pattern pins. The pins switch at the board's I/O supply voltage; this software cannot see,
set or measure it.

## Boards used during development

The table records what the project owner reported using. It is not a recommendation, and these boards, their sellers
and their makers have not been independently checked. A board with the same name or a similar listing can differ.

| Role | Reported board | Not confirmed |
| --- | --- | --- |
| STPG | [AliExpress item 1005010151985058](https://www.aliexpress.com/item/1005010151985058.html), the "PSRAM-For-Linux" option, 8 MB PSRAM | Maker, board revision, flash size and part, PSRAM part, chip stepping |
| GPIO controller (optional) | [Waveshare RP2040-Zero](https://www.waveshare.com/wiki/RP2040-Zero), an example from development | Board revision, the exact sketch version on it |
| Debug probe (optional) | [Waveshare RP2040-Zero](https://www.waveshare.com/wiki/RP2040-Zero) running probe firmware, an example from development | Board revision, probe firmware version and origin |
| Logic analyzer (optional) | Official Raspberry Pi Pico 2 | The exact firmware image on it |
| Verification FPGA (optional, separate project) | [AliExpress item 1005009019405281](https://www.aliexpress.com/item/1005009019405281.html), Cyclone 10 LP 10CL006YE144C8G | Board maker and revision, and whether the listing matches the part fitted |

The RP2040-Zero manufacturer attribution is the owner's identification, with the manufacturer's documentation linked
above; no manufacturer photographs or logos are redistributed. The seller links use the global domain rather than
a Japanese regional subdomain. The retailer may still show them in your local language, and that a listing shows in
English has not been confirmed; current listing contents and availability have not been independently verified.
The requirements above are what matters, and they do not depend on any listing.

Two boards with the same name are separate boards. The GPIO controller and the debug probe above are not one board
doing both jobs, and the analyzer's plain Pico 2 is not the STPG's PSRAM board.

## Debug probe

Not needed to install or use the firmware: a firmware file is copied to the board over USB while the RP2350 is in its
USB boot mode, as described with the firmware file. A probe is needed only to develop or debug the firmware over SWD,
and then it must be one that supports the RP2350, with debugger software (for example OpenOCD) that supports it too.
Not every probe or OpenOCD version does. The owner used an RP2040-Zero running probe firmware for this; it is an
example from development, not a requirement and not a recommendation.

## GPIO controller board

Optional. The owner used a Waveshare RP2040-Zero with the controller sketch. Another RP2040 board can work only if it
exposes the GPIO numbers the sketch uses; that has not been checked.

## Before connecting a device

- Check that the voltage levels of your board and your device match. This software cannot see or measure them.
- A run drives the pins your pattern uses. With `hold` completion the last levels stay driven after the run.
- Cancelling sends no STOP or release. Outputs may stay driven and the device may continue.
- The configured clock profile is an experimental setting of this project, not a rated operating point of the chip
  or of any board. Nothing here guarantees operation at that speed.

Firmware built outside the specified release build environment is not guaranteed to match the release binary or to
behave identically. Even in that environment, reproducibility is only what an actual comparison shows.
