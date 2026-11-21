# STPG manual

STPG turns an RP2350 board into a pattern generator and comparator for testing digital devices. You give it a test
pattern in a subset of STIL; it plays the pattern on GPIO pins and reports what happened.

Everyday work happens in one local program, **`stpg-view`**, which opens in your browser and has two pages: the
**Viewer** shows and edits patterns, and the **Runner** builds and runs them.

## Read in this order

1. [Getting started](getting-started.md): install, build a sample and run it, without any hardware.
2. [Hardware](hardware.md): what you need for a real run.
3. [Registering an STPG device](stpg-device.md): the one-time binding of a real board, and what its values mean.
4. [Viewer](viewer.md): look at, compare and edit patterns.
5. [Runner](runner.md): projects, builds, runs and results.
6. [Settings folder](settings-folder.md): where local bindings are kept, and how to use an earlier setup.

Optional parts, only if you use them:

- [GPIO controller](gpio-controller.md): an RP2040 board that drives reset lines and general pins.
- [Logic analyzer](logic-analyzer.md): a separate tool that captures the real waveform during a run.

When something goes wrong: [Troubleshooting](troubleshooting.md). To check the GUI itself: [Testing the GUI](testing-gui.md).

## What this software does not claim

- A completed run is not a pass of the device under test. Completion and comparison are reported separately, and
  neither is a verdict on the device.
- Only a subset of STIL is supported. A file outside it is refused with the reason; nothing is guessed.
- Timing on the pins is what the firmware was built and measured for. See [Hardware](hardware.md).
