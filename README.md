# STPG_RP2350

STPG is a GPIO pattern generator and comparator built around an RP2350.
Its local browser GUI converts a supported subset of STIL, shows waveforms,
builds capacity-checked packages, and manages uploads, runs and results.
Loop preservation, automatic segmentation and explicit timing/wait policies
support larger patterns. Run completion, comparison results and optional
accessory results are separate: none alone certifies a device under test.

## First run: a simulated tutorial

This walks you through the GUI once, from a sample pattern to a run result, against a **simulated** STPG. No board,
firmware or Pico SDK is needed, and nothing touches hardware: the GUI starts with real-device runs switched off. It is a
tutorial, not a hardware test.

### 1. Install and start

Python 3.10 or newer is required. Open a terminal **in this extracted source folder** (the one containing
`pyproject.toml`). These commands use the environment's Python directly: no PATH edit, activation script or
administrator access is needed.

Windows PowerShell:

```powershell
python -m venv stpg-env
.\stpg-env\Scripts\python.exe -m pip install .
.\stpg-env\Scripts\python.exe -m stpgview.physical.patterns samples
.\stpg-env\Scripts\python.exe -m stpgview --demo-device --no-device-run --config-dir ./settings-check --results-dir ./runs-check
```

Linux (also the command form for macOS; macOS is untested and not actively supported):

```sh
python3 -m venv stpg-env
./stpg-env/bin/python -m pip install .
./stpg-env/bin/python -m stpgview.physical.patterns samples
./stpg-env/bin/python -m stpgview --demo-device --no-device-run --config-dir ./settings-check --results-dir ./runs-check
```

The third line writes small public patterns into a new folder `samples` (use a new or empty folder). The last line
starts the GUI and opens it in your browser; if no tab opens, paste the address it prints, including `token=`.
Stop it later with Ctrl+C in that terminal.

### 2. Open the sample

1. On the **Viewer** page press **Open…**, choose `samples/gui_tiny.stil` and open it.
2. You see the waveform at the top and the vector table below: four vectors.

### 3. Build it

1. Press **Build in Runner…**. The **New runner project** dialog opens, filled in from the open file. Use a new
   project folder if the suggested one already contains a project or other files.
2. Choose **Wait preset** `none` and **Completion** `hold`; leave the rest. Press **Create**.
3. Press **Build**. Each stage gets a check mark, and the bundle card shows the built package.

### 4. Run it on the simulated STPG

1. In **3 · Run** choose the device **simulated (demo, no hardware)**.
2. Press **Run…**. The confirmation lists exactly what will run. Tick every statement and press **Start run**.
3. The result shows **Outcome** `COMPLETED` and **Comparison** `NOT_APPLICABLE` (this sample has no compares), a
   **SIMULATED — no hardware** badge, and the result folder inside the project. With the suggested project name,
   look under `samples/gui_tiny-project/results/`. The `runs-check` folder is for runs outside a project.

That is the whole loop. The [getting-started guide](manual/getting-started.md) explains each screen in more detail.

### Next: a real STPG

A real run needs the board with its firmware, a binding for it and real-device runs switched on. Read
[hardware](manual/hardware.md) first (wiring, voltage, held output levels), then
[registering an STPG device](manual/stpg-device.md). Hardware access is always opt-in; a plain reset or closing USB
is not proof that outputs have been released.

## Hardware and optional tools

The main board used during development was a third-party RP2350 board sold
with 8 MB PSRAM. It is not interchangeable with every RP2350 board.
See [hardware](manual/hardware.md) for the owner-reported setup and limitations.

The STPG GUI works without an analyzer, GPIO controller or FPGA.
An optional [Waveshare RP2040-Zero](https://www.waveshare.com/wiki/RP2040-Zero)
controller sketch lives in
[controller/firmware](controller/README.md).
Logic-analyzer support is a **separately installed and licensed program**,
not an in-process core plug-in. Set it up with `stpg-la`, export an accessory
configuration, then select it in the Runner; see
[logic-analyzer setup](manual/logic-analyzer.md).

- [STPG_RP2350_LA](https://github.com/okamitsu320/STPG_RP2350_LA): the separate GPL-3.0-only analyzer program.
- [STPG_RP2350_VERIFY_FPGA](https://github.com/okamitsu320/STPG_RP2350_VERIFY_FPGA): optional verification HDL and Cyclone 10 LP project.

PCB reference designs are not included.

## Developers and verification

- [Host-only testing](manual/development/testing.md): source, wheel, source archive and C tests.
- [Firmware releases and build provenance](manual/development/firmware.md).
- [CLI and SDK](manual/development/interfaces.md).
- [Licensing and third-party notices](THIRD_PARTY_NOTICES.md).

Check [GitHub Releases](https://github.com/okamitsu320/STPG_RP2350/releases)
for available firmware, SHA-256 checksums and the matching build record.
Firmware binaries are not checked into this source tree. Builds made in
another environment are **not guaranteed to produce the same binary or the
same behaviour as the Release**. Read the hardware limits and the release
notes before programming a board.
