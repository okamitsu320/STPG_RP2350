# Viewer

The Viewer shows STIL sources and compiled `pattern.bin` files with signal names and tester characters, compares
them, and edits a copy of a STIL. It reads STIL with the same front end as the compiler, so what it shows is what the
compiler understands.

```sh
stpg-view pattern.stil                    # a STIL
stpg-view work/package/                   # a package folder (pattern.bin + pattern.map.json)
stpg-view pattern.bin --pin-map pins.json # signal names for a binary without a map
stpg-view project.stil pattern.stil       # ATE-generator project and pattern files
stpg-view pattern.edited.edits.json       # a STIL with a saved edit list applied
stpg-view pattern.stil --vcd out.vcd      # write a VCD only, no browser
```

Or start `stpg-view` and press **Open…**, or drop files on the waveform. In **Open…**, type a folder and press **Go**,
or use ↑ (parent), ⌂ (home) and the drive buttons (every drive letter that exists on Windows); a network path can be
typed.

## What can be opened

| Input | What happens |
| --- | --- |
| Toolchain STIL (`0: 0ns Drive(0);`, `H: 50ns Compare(1);` …) | Shown as it is |
| A gzip STIL (`*.stil.gz`, for example a bundle's copy of its STIL) | Read from an uncompressed copy in the work folder; the file is not changed |
| ATE-generator output (`'0ns' D;` style) | Converted first, into a work folder; the original is not changed. Open the project and pattern files together. |
| Other STIL (combined codes, `Spec` variables, one-line blocks, quoted names, `C`, `Shift`, `MatchLoop`) | Refused, with the reason and a hint |
| `pattern.bin`, a package folder, `pattern.map.json` | Decoded by the firmware's rules; names from the map, a pin map, or `DUT<n>` |
| `*.edits.json` | The STIL it belongs to, with the edits applied |

Patterns whose Loops expand to millions of vectors open, because Loops are not expanded when reading. Up to 20,000,000
expanded vectors are shown; beyond that the file is refused with its count, and the Runner can still build it.

## The screen

The top bar has **Viewer / Runner**, the pattern tabs, **Go to**, **Open…**, **Build in Runner…**, **Colours…**,
**Export VCD**, and `?` for this legend. The waveform has one lane per signal; the vector table has one row per vector;
the inspector on the right shows the selected vector, edits, events and pattern details.

| Mark | Meaning |
| --- | --- |
| Blue line (light fill while 1) | The tester drives 0 or 1 (a device input) |
| Green ▲ | Compare H: the device output is expected high |
| Orange ▼ | Compare L: expected low |
| Grey ◆ | X: sampled, not compared |
| Grey dashed line | Z: the tester does not drive |
| Yellow hatching | An inserted wait |
| Bar left of a signal name | Direction: blue In, green Out, purple InOut |

With about 4,000 or more vectors on screen the waveform is summarised per pixel column; zoom in for every edge.

While something long runs, a pill in the top bar says what and for how long, and the button you pressed shows a
spinner. No pill and no spinner means the program is idle.

## Fails of a run

**Show fails in the viewer** under a run's STIL details opens the bundle's copy of the STIL and marks each fail-log
entry: a red band over the vector, a red box on each failed signal, and a red edge on its table row. The inspector's
**Fails** section steps through them in time order (◀ ▶) and shows the segment, line, vector and the expected and
observed levels.

A fail is marked only where this view's vector at that index is on the STIL line the SDK named for it. Otherwise it
is listed under **not placed** with the reason; it is never moved to a nearby vector. The marks come from the result:
the waveform shows the pattern, not measured levels.

## Moving around

| Action | Effect |
| --- | --- |
| Mouse wheel | Scroll the lanes |
| Ctrl + wheel, `+`, `-` | Zoom at the pointer |
| Shift + wheel, drag | Pan in time |
| Click | Select a vector |
| Shift + click | Select a range (for edits) |
| Click a signal name | Highlight it (the default signal of the edit forms) |
| Right-click a signal name or a rate cell | Choose its colour |
| `←` `→` | Previous or next vector |
| `F` / `Z` | Fit the whole pattern / zoom to the selected vector |
| `/` | Go to: a vector (`120`), a STIL line (`L57`) or a time (`3.2us`) |

In the table, drag the edge of a column header to resize it, and double-click it to fit. **Colours…** tints rates and
signals; colours are kept in this browser by name.

## Comparing a STIL and its binary

Open both, select the same vector and switch tabs. The STIL tab shows the characters as written. The binary tab derives
them from what the firmware does: a rising drive is R, a falling one F, "no action" is Z, and a signal still driven
from an earlier vector is `-`. X is not always recoverable from a binary. Waits come from `pattern.wait.patch.json`
next to the binary; without it they are inferred and marked `wait?`.

## Editing a copy

Only a STIL can be edited, and **the source is never changed**: edits build a copy in a `STIL ✎ <name>.edited.stil` tab.
Each edit is listed and can be removed on its own. Vectors produced by a Loop, Macro or Procedure cannot be edited one
by one, because every repetition would change.

Select a vector (Shift + click for a range), then in the inspector's **Edit** section:

- **Change character**: another character for one signal. Only characters that the vector's WaveformTable defines are
  offered. On an InOut pin, changing between drive and compare changes who drives the line; the form warns you.
- **Insert wait**: a hold-only vector after the selected one. Remove it with × on its item in the edit list. Waits
  already in the source STIL and the chunk waits that Build places are not removed here
  ([Waits](runner.md#waits)).
- **Change period**: run the range with a copy of its WaveformTable with another period.
- **Move edge**: new time for one signal's event, for those vectors or for every vector using the table.

Without a selection: **Rename signal**, **Add signal** and **Remove signal**. After a signal edit the copy gets a pin
map that keeps every other signal on its bit, so no other signal moves to a different pin.

**Save edited STIL** writes `<name>.edited.stil`, `<name>.edited.edits.json` and, after a signal edit,
`<name>.edited.pinmap.json` next to the source. **Export STIL…** asks where to write. Existing files are not overwritten.

**Build in Runner…** opens the New project dialog filled in from what is open.

## VCD

**Export VCD** saves the shown pattern. With Surfer or GTKWave installed, **Open in …** opens it there.

| VCD signal | Content |
| --- | --- |
| `drive.<signal>` | Tester output 0 or 1, `z` while not driving |
| `expect.<signal>` | Expected level from the strobe to the end of the vector: 1 (H), 0 (L), x (X), otherwise z |
| `info.vector`, `info.stil_line`, `info.rate` | Vector number, STIL line (0 if unknown), rate index |
