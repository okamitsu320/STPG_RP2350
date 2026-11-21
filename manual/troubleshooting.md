# Troubleshooting

| Symptom | What to do |
| --- | --- |
| pip succeeds but `stpg-examples` / `stpg-view` is not recognised on Windows | Use the explicit venv Python and module commands in [Getting started](getting-started.md); no PATH change is needed |
| PowerShell rejects a command containing `<the ... wheel>` | That was a description, not a filename. Use the source-install command from [Getting started](getting-started.md) or the real filenames in [developer installation](development/interfaces.md) |
| Sample generation says the folder is not empty | Reuse the samples already there, or choose a new empty folder; existing files are never overwritten |
| No browser tab opens | Paste the address from the terminal, including `token=` |
| "missing or wrong token" | Use the full address printed by the terminal |
| A button seems to do nothing | Look for the activity pill in the top bar, then the red text in the status bar |
| The tool stops at start with `--config-dir is blank`, "wrapped in quotes" or "cannot expand" | See [Settings folder](settings-folder.md); no other folder is used instead |
| **Settings folder problem** on the page | The folder cannot be used; the reason is shown. Fix it or restart with `--config-dir` |
| My devices are gone | Compare the settings folder on the page with the earlier one. Nothing is moved or deleted for you |
| `stpg-view` and `stpg-la` show different folders | Start both with the same `--config-dir` or `STPG_CONFIG_DIR` |
| A binding is listed with an error | That file is unreadable or has another format. It is left as it is; fix it or add a new binding |
| Signal names are DUT0, DUT1 … | The binary has no `pattern.map.json` next to it: open the map or use `--pin-map` |
| A wait is shown as `wait?` | `pattern.wait.patch.json` is missing; the wait is inferred |
| "cannot be read by the STIL subset front end" | The compiler does not read this form; see the hint |
| "looks like an ATE-generator project/pattern file" | Open the project and pattern files together |
| Edit refused with Loop/Macro/Procedure | That vector is repeated; edit the source structure instead |
| Add signal: "no free pin bit" | All 24 bits are used; remove a signal first |
| Build: "service profile scope … does not match artifact scope …" | Press **Use Scope = …**, Save, Build |
| Build: "existing cache is incomplete/corrupt; not overwritten" | A failed build with these inputs and settings exists: change a setting or the input |
| Build fails with the same error after an update | Restart `stpg-view`: a running program keeps the old code |
| Run…: "STPG port check" | The registered port or USB identity differs from the current list. Compare with Scan ports. The port was not opened |
| Run: "installed compiler/SDK differs from the bundle" | Build again, or **Update for this SDK without recompiling…** in the Project card; then Run… |
| **Reading the retained fail log stopped** | A segment's compare failures were reported; reading their entries got no or a rejected reply. The comparison stands, the entries read are kept, and nothing more was sent (no release). Review the evidence before any further device work |
| STIL details: "The bundle this run used … is not open" | Open that bundle (or its project), then press STIL details again |
| Run: "already leased" | Another of your programs is using the device |
| Run: "could not open port … PermissionError … 5" (Windows) | Another program holds the port; close it. No command was written |
| Run: **Refused at the STPG start check** (Errors: "response code/state/error mismatch") | The STPG was not in a state a run starts from (IDLE or DONE), for example READY. Nothing was uploaded, and GPIO actions were not started. Find out what left it in that state before you reset it; the program does not reset or retry. An earlier run that stopped after its upload and before RUN leaves it READY |
| Run: **Upload accepted; no subsequent STPG command recorded** | A finished record ends in an accepted upload, for example at a failed GPIO controller step. If the STPG is still READY/HOLD_AWAITING_NEXT, another run is refused. The saved reply is not a live device/pin check. Check the cause and wiring before deciding on an explicit reset |
| GPIO controller: identification refused ("unexpected controller identity/startup line") | The result lists the lines the controller sent and how each was read. Check that the port is the controller with the sketch and protocol you registered (Controllers… → Identify). A record from an earlier version has no lines |
| Build: **Wait plan rejected** | The SDK refused the chunk-wait plan, for example over the total wait limit. Nothing was changed for you: change the wait length or limits (Edit settings…) and Build again |
| Run: "timer-aware stream watchdog" | This streamed Rate Vector pattern has an emitted gap of at least 250 ms. The registered firmware does not report the required capability. No RESET, upload or RUN was sent. See [long periods](runner.md#response-deadlines); changing Max total wait will not add firmware support |
| Run: `DEVICE_ERROR`, error `12 (ERR_TIMEOUT)` | The STPG reported its own timeout in a STATUS answer that arrived; this is not a host response timeout. The code alone does not say where in the firmware it was set. Firmware without `CAP_STREAM_WAIT_WATCHDOG` can report it during a streamed Rate Vector pause of 250 ms or more; with this SDK such a run is refused before RESET on that firmware. See [long periods](runner.md#response-deadlines). The program does not reset or retry |
| Bundle REJECT: "explicit wait plan rejected" | An older SDK kept a partial wait plan that is not executable. Update for this SDK cannot repair it: Build the project again |
| Run: **The STPG's identity differs from this binding** | Firmware version or capability differ from the binding; both are shown. Check the board and its firmware before you change the binding: see [Registering an STPG device](stpg-device.md) |
| First Save of a binding failed with "Unsupported method ('{}POST')" | Fixed in this version (the request after Scan ports could be misread). Restart `stpg-view` from this version and save again |
| A run ends with a serial error | Read "Last STPG exchanges" in the result: the last row is where it stopped. Outputs may still be driven; check that the wiring is safe. Nothing is retried |
| Run: **Where the run stopped** shows accepted upload and RUN, but no complete first GET_STATUS reply | The result file identifies this segment's last confirmed steps. Whether the segment ran to its end is not known, and outputs may still be driven. The record does not say whether the firmware, the USB link or the host is the cause. The runner stops at the failure without a retry, a recovery reset or a longer wait. Keep the result folder and review it before any further device work |
| External accessory says "not found in this Python environment" | Use the matching local core+LA install in [Logic analyzer](logic-analyzer.md), restart the core, or ignore it if you do not need captures |
| Accessory Check: "the accessory config is for mode 'device'" (or 'simulated') | The demo STPG needs a simulated config and a real STPG a device one: write a new config in that mode |
| Start: "the external accessory selection changed after this run was confirmed" | The config changed after the confirmation: Check again and press Run… |
| "STPG not started", accessory REJECT | The accessory did not answer READY. Read its messages in the result and its records in the accessory folder |
| Accessory INCOMPLETE | The run ended before it was asked to collect; the STPG and GPIO sections show where |
| Capture plan, trigger or alignment problems | See the logic-analyzer tool's manual |
