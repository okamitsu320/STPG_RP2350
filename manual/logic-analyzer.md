# Logic analyzer (optional)

Capturing the real waveform around a chosen vector is done by a **separate tool** with its own
[repository and download](https://github.com/okamitsu320/STPG_RP2350_LA), program `stpg-la`,
browser page and licence (GPL v3). `stpg-view` contains none of its code, and every other function works without it.

For a physical capture, the analyzer runs the unbundled V6 firmware from
[LogicAnalyzer by gusmanb](https://github.com/gusmanb/logicanalyzer/releases/tag/v6.0.0.0). The STPG companion is a
separate project; report problems with the companion to the companion repository, not to the firmware project.

So there are two pages, and each has its job:

| Task | Where |
| --- | --- |
| Add, edit and Identify an analyzer; plan a capture; see the capture | `stpg-la` page |
| Build the pattern; run it with the capture | `stpg-view` Runner |

The Runner's **Analyzers…** list is read only for this reason. It shows the USB identity of the analyzers in the
[settings folder](settings-folder.md), which is what **Reset LA…** needs and what keeps an analyzer apart from the STPG
and the GPIO controller.

## From nothing to a capture

The same steps are in the Runner's External accessory card, under **How to set up a logic analyzer**.

For the source-install example below, put the two matching source downloads beside each other with these folder
names. Open the terminal in `STPG_RP2350` and use the environment made by [Getting started](getting-started.md).

```text
your-work-folder/
  STPG_RP2350/       (pyproject.toml, stpg-env/)
  STPG_RP2350_LA/    (pyproject.toml, stpg_capture/)
```

Windows PowerShell:

```powershell
.\stpg-env\Scripts\python.exe -m pip install . ../STPG_RP2350_LA
.\stpg-env\Scripts\python.exe -m stpg_capture.ui.server --demo --no-device-io --config-dir ./settings-check --results-dir ./la-runs-check
```

Linux / macOS:

```sh
./stpg-env/bin/python -m pip install . ../STPG_RP2350_LA
./stpg-env/bin/python -m stpg_capture.ui.server --demo --no-device-io --config-dir ./settings-check --results-dir ./la-runs-check
```

These commands install both local sources together, not a package named `stpg-rp2350` from an index. Python build
dependencies may still need an index. The two programs now share `settings-check`; run the core command from the
same folder in another terminal. The LA page is a demo with device I/O off: Scan ports reads nothing, Identify is
disabled, and a mock capture is synthetic. No device or firmware is required for this check.

For local wheel files instead, use [developer installation](development/interfaces.md); give pip both matching
files together. Restart the core GUI after installing the tool so that it refreshes the installed-tool metadata.

For a real capture after you have reviewed wiring, safety and firmware compatibility:

1. Install the matching two programs into the same Python environment, then restart `stpg-view`.

   The tool requires exactly the STPG version it was built for. Give pip both files in one command, so that it takes
   the STPG package from your file and does not look for it by name on a package index.

   The External accessory card then says the tool is installed. If it says *not found*, the tool is not in this Python
   environment; STPG-only and GPIO runs still work.
2. Start the LA page without the demo/no-device flags, using the same environment's Python and the same settings
   folder. `stpg-la` is the equivalent console command if the environment is activated. The LA page has its own
   port; `stpg-view` never starts it for you.
3. Check that both pages show the same **settings folder**. If they do not, start them with the same `--config-dir` or
   `STPG_CONFIG_DIR`.
4. On the `stpg-la` page: add the analyzer with **Scan ports**, press **Identify**, wire it as its **Wiring** table shows.
5. Open the same bundle there, by the **folder** shown in the Runner's bundle card (it holds `runner.bundle.json`), and
   plan a capture around a vector.
6. **Use in an STPG run** writes a new folder with `external.json`.
7. In the Runner's **External accessory** card, choose that `external.json`, press **Check**, tick **use with the next
   run**, and press **Run…**.
8. Open the run's folder on the `stpg-la` page to see the capture, its alignment and the waveform.

The tool's own manual (`manual/README.md` in `STPG_RP2350_LA`) covers wiring, triggers, what a capture can and cannot
see, and its limits.

## The External accessory card

The card says what is true now:

| State | Meaning |
| --- | --- |
| not selected | The next run uses the STPG, and a GPIO controller if you chose one |
| not checked | A config is typed in, but a run ignores it until it is checked and ticked |
| check failed | This config cannot be used; the reason is shown. Nothing was started |
| checked, not in use | Tick "use with the next run" to include it |
| used with the next run | The next run starts the program. If it fails, the run stops; it does not fall back to an STPG-only run |

**Check** only reads files: the config and the hashes of what it names, the USB identity of the accessory's device
(which must differ from the STPG and the GPIO controller), and that the segment is in the bundle. It starts nothing and
lists no ports. A check belongs to one bundle and one STPG, so choosing another clears it. The demo STPG accepts only a
`simulated` config and a real STPG only a `device` one.

## What happens in a run

1. The confirmation shows the program, its hashes, the device identity, the segment, the mode and three limits: **open**
   (validation before it answers READY), **arm/complete** (each later reply) and **stdin EOF grace** (time to exit
   after its input is closed). The selection is frozen: an edited config is refused at Start, and the files it names
   are hashed again before the program starts.
2. The program starts before the STPG port is opened and must answer READY, after checking its plan and opening its own
   device. Otherwise the STPG run is not started ("STPG not started").
3. At the target segment: GPIO before-run actions, the accessory's ARMED, then RUN. After a valid DONE/HOLD of that
   segment it collects and answers COMPLETE. Later segments still run, after a check that it stayed silent.
4. On a failure or cancel, nothing is sent to it except closing its input: no cleanup, cancel or reset. It may be
   terminated after a bounded wait, and that does not prove that its device or pins were released.

A failure while it collects stops the run after that segment's DONE, like a failed GPIO action: the STPG outcome shows
REJECT and the completed segment is kept.

## Results

The run folder holds `stpg/run.json.gz` (and `stpg/control.json`), `accessory/session.json` (the Runner's record of the
process) and the tool's own files under `accessory/`. The Runner shows the accessory as its own result: COMPLETED with
the data adopted or not (the tool's own judgement), INCOMPLETE (the run ended before it was asked to collect), REJECT or
CANCELLED, and any messages it printed. It does not interpret samples.

After a cancel the result may name **unadmitted bytes**: output of the accessory that was not accepted as a reply in the
cancelled exchange. This can include bytes buffered before the cancel, so it does not tell when they arrived.

Exit codes of a run: 130 cancelled; 2 error, refusal or incomplete; 4 accessory data not adopted; 3 compare mismatch;
0 otherwise. None is a pass of the device.

An accessory config names a program that runs with your rights. Select only programs you trust: its hash does not
attest the code it imports, and a project or bundle can never select one.

Accessory configs and run folders contain local folder names and USB serial numbers. Review them before sharing.

A bundle whose pin map gives a signal a special role, such as the marker of a marker pattern, is named in the bundle
card and the confirmation. The STPG drives that pin like any other, so check that it is spare and safe.
