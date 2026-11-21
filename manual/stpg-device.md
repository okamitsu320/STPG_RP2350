# Registering an STPG device

A **binding** tells the Runner which USB port is your STPG and what that STPG must report about itself. You need one
binding per board, once, before a real run. The demo device `simulated` needs none.

Registering reads USB information only. It opens no port and sends nothing to the board.

## Before you start

1. Wire and power the board as described in [Hardware](hardware.md), and check the voltage levels.
2. Install the STPG firmware on the board, and keep the **firmware version** and **capability** that come with that
   firmware (see "Firmware version and capability" below).
3. Start `stpg-view` without `--no-device-run`. With that option the port list shows no real ports.

## Add the binding

1. In the Runner, press **Devices…**, then **Add…**.
2. Type a **Name** you will recognise, for example `bench-stpg`.
3. Press **Scan ports**. The list shows each serial port with its USB VID:PID, serial number and description. This is
   metadata only: no port is opened, and nothing is selected for you.
4. Find your STPG in the list, for example by unplugging it and scanning again. Press **Use** on its row. This fills
   **Port**, **USB VID**, **USB PID** and **Serial number**.
5. Enter **Firmware version** and **Capability**: the values listed for the firmware you installed (see below).
6. Press **Save**. The binding is written to your [settings folder](settings-folder.md).

## Firmware version and capability

These are the values the STPG reports about itself in its INFO reply, the first thing every run asks for. The
binding holds what you **expect**; each run compares it with what the board **reports**, and a difference refuses
the run before anything is uploaded. Nothing is guessed, skipped or copied over for you.

| Field | What it is | What it is not |
| --- | --- | --- |
| Firmware version | A 32-bit identity of the STPG firmware build: major and minor version in the low two bytes, a build-layout code in the third byte and the rate-table size in the top byte | Not the USB device version, the Python package version or the bootloader version |
| Capability | A set of bits, one for each feature the firmware was built with (for example PSRAM streaming, 24-pin runs, sample capture). The form names the bits of the value you type | Not a list of features you choose: it must match the firmware exactly |

Where the values come from:

- **From the release of the firmware you installed.** Each firmware release is meant to list both values with the
  firmware file.
- **Not yet settled for this candidate.** No firmware Release with recorded values has been published yet, so this
  page cannot give values to type. Until then, use the values given with the firmware file you were provided, by
  the person who built it.
- **If a run reports a different identity,** its result shows both: what the binding expects and what the board
  reported. Check first that the board and its firmware are the ones you meant. Change the binding (**Devices… →
  Edit**) only if the board's firmware is the one you want to use; the program never changes a binding by itself.

## Check it

Choose the new device in **3 · Run** and press **Run…** with a small pattern. The confirmation shows the port, the
USB identity and the expected firmware and capability. A refusal before upload names its reason in the result:

| Result says | Meaning |
| --- | --- |
| Port check: missing or duplicated, or USB metadata does not match | The port in the binding is not there, or another device is on it. No port was opened |
| The STPG's identity differs from this binding | Firmware version or capability differ (both values are shown). Nothing was uploaded |
| Refused at the STPG start check | The board is not in a state where a run can start (for example READY). Nothing was uploaded |
