# Firmware Release policy and build provenance

The source tree does not contain a Release firmware binary. Look for available
images in [GitHub Releases](https://github.com/okamitsu320/STPG_RP2350/releases)
and check the board, source revision, build record and SHA-256 for the selected
image. Do not substitute diagnostic images.

Each future firmware Release must bind:

1. The public source commit and clean/dirty source state.
2. Compiler, linker, CMake, Ninja, Python and host/platform versions.
3. Pico SDK and TinyUSB revisions, submodule identities and exact patch hashes.
4. Every build setting, preset and full configure/build command.
5. ELF/UF2/BIN sizes and SHA-256, link map and memory-budget checks.
6. Host tests and the separately authorized physical compatibility results.

Collect toolchain information **before** configuring the build and archive it
with the configuration, dependency lock and final artifact hashes. Release
automation must reject missing or stale provenance rather than silently reuse
an old record. Use the documented recipe for the selected release. The
standalone CMake preset does not enforce the TinyUSB build lock on its own.

Builds outside the recorded Release environment are **not guaranteed to
produce the same binary or the same behaviour as the Release**. Rebuilding
with different tools or settings creates a new candidate requiring verification,
not permission to replace the released artifact. Do not infer general board
compatibility or timing guarantees from a successful build.

The C host tests do not require the SDK checkout. Target builds do: supply
the separately acquired Pico SDK via PICO_SDK_PATH and review firmware/CMakeLists.txt,
CMakePresets.json, and dependencies/tinyusb-build-lock.json. No target rebuild,
hardware contact or image adoption is part of the source-curation checks.

## Long programmed streaming waits

`CAP_STREAM_WAIT_WATCHDOG` (bit 23) identifies the timer-aware streaming
watchdog, not a change to the Rate Vector format or the USB response deadline.
The previous watchdog treated every 250 ms without DMA progress as a stall,
including while the output PIO was counting down a valid wide wait.

The no-progress monitor now checks the active output DMA and the output SM's
countdown PC. When it observes an in-flight transfer and an enabled SM in
that countdown, a long-rate run can use one bounded allowance for this
progress epoch. The allowance is the rate-table upper bound (saturated at
the 32-bit wait-word limit) plus 250 ms, measured from the same last progress
timestamp. It is retained through countdown exit but not renewed by repeated
observations. Without this evidence, a stall still uses the 250 ms limit.
Startup fill, sample handoff, timeout terminalization, GPIO ownership and PIO
instruction words are unchanged.

The SDK gates long streaming gaps on the advertised capability; it does not
infer the capability from the firmware version or change a device binding.
A newly compiled image requires separate physical validation and a binding
with its confirmed INFO values. A successful cross-build alone is not a
timing measurement or approval to install the image.
