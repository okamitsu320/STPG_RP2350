# Host-only verification

Run in a virtual environment. No serial device or FPGA is required.

```sh
python -m pip install '.[dev]'
python -m pytest tools/tests
cmake -S firmware/host_test -B /tmp/stpg-host-build
cmake --build /tmp/stpg-host-build
ctest --test-dir /tmp/stpg-host-build --output-on-failure
```

Use a fresh temporary build directory if the example path already exists.
The Python suite uses public synthetic inputs. Its default hardware guard
blocks serial opens, USB enumeration and common debugger executables.
Simulation passes are not physical timing, sample or DUT validation.

The guard intercepts the current test process's Python serial/enumeration calls
and `subprocess.Popen`, including command-line strings and executable overrides.
It is not an OS sandbox: `os.system`, direct system calls and programs launched
inside a child are not intercepted. Never test a broken guard against a real
device or debugger. Guard regression tests install independent fake lower layers
first, so a deliberately broken guard still cannot contact hardware.

## Firmware and wire contracts

`tools/tests/test_firmware_wire_contract.py` compiles a small native C probe from
the actual public headers. It compares all command, response, error and state
values, the framing constants, and packed command/response bytes and CRCs with
Python. It needs a native C compiler and a little-endian host; an unmet
requirement is NOT RUN, not cross-language validation. Neither Pico SDK nor an
ARM cross-compiler is used by that test.

The C host suite checks total/header/data CRC rejection independently for raw
16/32-bit-wait and rate-vector inputs. Its compare tests check an exactly full
fail log, the first two excess failures, retained entries, read bounds, CRC and
reset. These host tests do not validate real flash, PSRAM, DMA or USB timing.

The Python suite also retains wide-format round trips, RUN write/flush/read
ordering, deferred USB response admission, EOF fail-stop drain ordering, QMI
source contracts and practical-profile defaults. Source-text assertions and
the selected TinyUSB EP0 patch checks are static regressions, not execution of
the device paths or proof that a target build used the patched dependency.
Three public viewer fixtures that have unrepresentable raw sub-slots assert
their exact compiler error instead of skipping arbitrary compilation failures.

For distribution checks:

```sh
python -m build --outdir /tmp/stpg-distributions
python -m release_tools.audit_distribution /tmp/stpg-distributions
```

The checked-in package/source inventories bind the reviewed bytes. Do not
regenerate them merely to make a mismatch pass: investigate the diff first.
A source archive includes the fixtures and helper code required by the suite.
Unpack the sdist into a fresh directory, install its development dependencies,
and run the same pytest command there, away from the original checkout.

Set STPG_CORE_WHEEL and STPG_WHEELHOUSE to local artifacts to run
tools/tests/test_gui_installed_core.py. Installed core-plus-analyzer tests
belong to the separate LA tree. Wheel tests are skipped when artifacts are
not supplied; that is not an installation pass.
Browser tests require Selenium and a compatible local browser/driver;
see [GUI testing](../testing-gui.md). Native Windows/macOS and physical tests
are separate evidence, not inferred from Linux or simulation.
