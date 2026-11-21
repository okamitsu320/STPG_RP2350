# CLI and SDK

Most users should start with [the GUI](../getting-started.md).

## Installing local wheels (optional)

The getting-started guide installs the source folder directly. Do not type a
description in angle brackets into PowerShell, and do not install the STPG
distribution by name from an unverified index.

To build the core wheel yourself, from the core source folder on Windows:

```powershell
.\stpg-env\Scripts\python.exe -m pip install build
.\stpg-env\Scripts\python.exe -m build --wheel --outdir packages .
.\stpg-env\Scripts\python.exe -m pip install --force-reinstall ./packages/stpg_rp2350-0.1.0.dev7-py3-none-any.whl
```

These are filenames for this core candidate, not placeholders. On Linux/macOS
use `./stpg-env/bin/python` instead of the Windows interpreter path. For an
offline core install, prepare `wheelhouse/` with the runtime dependencies
(at least pyserial 3.5), then use:

```powershell
.\stpg-env\Scripts\python.exe -m pip install --force-reinstall --no-index --find-links ./wheelhouse ./packages/stpg_rp2350-0.1.0.dev7-py3-none-any.whl
```

Building source offline also needs the build backend's local dependencies;
runtime wheels alone do not provide those. No public Release is assumed.

To add the logic-analyzer tool, build its wheel from its source folder (repository `STPG_RP2350_LA`, beside this
one) and install both matching wheels in one command, from the core source folder:

```powershell
.\stpg-env\Scripts\python.exe -m build --wheel --outdir packages ../STPG_RP2350_LA
.\stpg-env\Scripts\python.exe -m pip install --force-reinstall ./packages/stpg_rp2350-0.1.0.dev7-py3-none-any.whl ./packages/stpg_rp2350_la-0.1.0.dev7-py3-none-any.whl
```

Offline, add `--no-index --find-links ./wheelhouse` to the second command. `stpg-rp2350-la` 0.1.0.dev7 requires
exactly `stpg-rp2350` 0.1.0.dev7. An older LA wheel (0.1.0.dev3) requires the old core: do not install it here, since
pip would replace this core with the old one. Do not use `--no-deps` to bypass the pin, and do not fetch either
distribution by name from a public index. The core works without LA.

Close both programs before updating, then restart them. These preparation
candidates can keep the same version while GUI bytes change: without
`--force-reinstall`, pip can skip an already installed version. A new virtual
environment is another option. The SDK fingerprint alone does not identify a
GUI revision. This SDK update changes the bundle fingerprint: old bundles need
an explicit verified [rebind or rebuild](results.md), not a hand-edited manifest.
Re-export accessory configs to a new folder after an environment update.

## Commands and API

Offline entry points include stpg-examples, stpg-stil-preprocess,
stpg-stil-analyze, stpg-stil-package and stpg-stil-segment-package.
Each has command-specific help.

stpg-runner manages projects, builds and runs. A device run requires explicit
confirmation and the matching bundle/device contracts. Removed in-process
analyzer options remain rejected, not silently ignored. Analyzer acquisition
uses the separately installed external-accessory program and explicit protocol.

The Python packages are stpg (wire/pattern types), stil (compiler),
stpg_runner (project/execution SDK) and stpgview (local GUI).
Only these runtime packages and GUI assets are installed by the core wheel;
tests and release-checking tools remain source-only.

See [run evidence and bundle migration](results.md) for the gzip reader,
offline STIL descriptions, FAIL_LOG collection contract and no-recompile API.

SDK review: [custom chunk wait length](custom-waits.md) documents the explicit
`chunk-custom` / `chunk_wait_us` API; its GUI input is a separate pending step.
