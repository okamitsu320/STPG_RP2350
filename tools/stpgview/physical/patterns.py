"""Write the public synthetic sample patterns (offline; no device contact).

There are no Compare waveforms, so results are deterministic without a DUT.
The counters drive only STPG GPIO2-8 (A0..A6) and GPIO10 (IOOE, held 0) through
``gui_counter.pins.json``; GPIO9 stays free for a marker and nothing above
GPIO10 is ever driven. That keeps them safe with only analyzer inputs on the
DUT pins, and on a verification FPGA whose GPIO2-10 are inputs, with GPIO10 held
0 as its output enable. ``tiny`` drives GPIO2/3 only. Output is byte-identical on
every platform; the command prints the SHA-256 of each file.

    stpg-examples OUTPUT_DIRECTORY
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

TINY = """Header { Title "Public synthetic STPG runner fixture"; }
Signals {
  D0 InOut;
  D1 InOut;
}
SignalGroups {
  ALL = 'D0 + D1';
}
Timing {
  WaveformTable NORMAL {
    Period '400ns';
    Waveforms {
      ALL { 0: 0ns Drive(0); 1: 0ns Drive(1); X: 100ns Compare(X); }
    }
  }
}
Pattern MAIN {
  W NORMAL;
  V { ALL = 01; }
  V { ALL = 1X; }
  V { ALL = 10; }
  V { ALL = 0X; }
}
"""


COUNTER_SIGNALS = [f"A{i}" for i in range(7)] + ["IOOE"]
# Logical bit -> STPG GPIO is bit + 2: A0..A6 = GPIO2..8, IOOE = GPIO10, GPIO9 (bit 7) left free.
COUNTER_BITS = {**{f"A{i}": i for i in range(7)}, "IOOE": 8}


def counter(n: int) -> str:
    """7-bit address A0..A6 = (vector // 4) % 128, IOOE = 0 in every vector; drive only, 400 ns period.

    The address repeats every 512 vectors, so a target below 512 vectors after its segment's RUN keeps a
    state that no earlier vector of the segment had.
    """

    lines = ['Header { Title "Public synthetic drive-only counter (STPG GUI physical test)"; }', "Signals {"]
    lines += [f"  {name} In;" for name in COUNTER_SIGNALS]
    lines += ["}", "SignalGroups {", "  ALL = '" + " + ".join(COUNTER_SIGNALS) + "';", "}", "Timing {",
              "  WaveformTable NORMAL {", "    Period '400ns';", "    Waveforms {"]
    lines += [f"      {name} {{ 0: 0ns Drive(0); 1: 0ns Drive(1); }}" for name in COUNTER_SIGNALS]
    lines += ["    }", "  }", "}", "Pattern MAIN {", "  W NORMAL;"]
    for i in range(n):
        address = (i // 4) % 128
        bits = "".join("1" if address >> b & 1 else "0" for b in range(7))
        lines.append(f"  V {{ ALL = {bits}0; }}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def counter_pins() -> str:
    signals = {name: {"bit": bit, "logical_pin": f"DUT{bit}", "direction": "input", "role": "signal"}
               for name, bit in COUNTER_BITS.items()}
    return json.dumps({"active_pins": 16, "signals": signals}, indent=2) + "\n"


PATTERNS = {
    "gui_tiny.stil": TINY,                  # GPIO2/3, 4 vectors; build with --max-segment-vectors 2
    "gui_counter.stil": counter(1600),      # GPIO2-8 + GPIO10, 1600 vectors, one segment
    "gui_counter_long.stil": counter(3200),  # same pins, 3200 vectors; --max-segment-vectors 1600
    "gui_counter.pins.json": counter_pins(),  # pin map of both counters (GPIO9 free for a marker)
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path, help="new or empty directory")
    args = parser.parse_args(argv)
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        raise SystemExit(f"{args.output} is not an empty directory; use a new one")
    args.output.mkdir(parents=True, exist_ok=True)
    report = {}
    for name, text in PATTERNS.items():
        path = args.output / name
        data = text.encode("ascii")
        path.write_bytes(data)
        report[name] = hashlib.sha256(data).hexdigest()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
