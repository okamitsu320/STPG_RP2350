# License scope and dependencies

Original STPG code is offered under the root MIT LICENSE. That grant does
not relicense third-party software, board designs, trademarks or firmware.

The optional logic-analyzer tool is a separate distribution. No analyzer
protocol implementation, GPL license text or analyzer entry point is included
in the core wheel. Separate processes and repositories do not, by themselves,
settle every licensing question; review the companion's own LICENSE and NOTICE.

## Dependencies

- pyserial 3.5: BSD-3-Clause; notice in LICENSES.
- Raspberry Pi Pico SDK: BSD-3-Clause, with separately licensed components.
  The SDK checkout is not bundled here. Its selected revision and components
  must be recorded for each firmware Release.
- TinyUSB: MIT; local patches and a build lock are in firmware/dependencies.
  Retain the upstream copyright and permission notices when distributing it.
- CMSIS components used by the SDK: Apache-2.0; reference notice in LICENSES.
- Python, setuptools, wheel, pytest, CMake, Ninja and compilers are external
  development tools, not redistributed runtime code in this wheel.
- Selenium and a browser are optional test tools, not bundled software.

The reference texts in LICENSES do not enumerate everything linked into a
firmware binary. Use the build record and the accompanying notices for that
specific release, including SDK components and linked toolchain runtime code.
Board and chip names identify the setup; no manufacturer endorsement or
right to reuse logos/images is asserted.
