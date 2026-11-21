"""New public ATE-syntax fixtures, generated solely from arithmetic and literals.

No vendor-generated source, private STIL, FPGA project or recorded data is read.
Historical public test filenames are retained only to minimize assertion edits.
"""
from pathlib import Path


def write_public_ate(root: Path) -> Path:
    names = (["clk"] + [f"ctrl_{i}" for i in range(3)] + [f"dout_{i}" for i in range(7)]
             + [f"io_{i}" for i in range(8)] + [f"mode_{i}" for i in range(4)] + ["rst_n"])
    for modified in (False, True):
        lines = ['Header { Title "Public synthetic ATE syntax"; }', 'Signals {']
        lines += [f"  {name} {'Out' if name.startswith('dout_') else 'InOut' if name.startswith('io_') else 'In'};"
                  for name in names]
        lines += ["}", "SignalGroups {", "  PINS_FPGASTIL_RATE100 = '" + " + ".join(names) + "';", "}",
                  "Timing {", "  WaveformTable FPGASTIL_RATE100_WFT {", "    Period '100ns';", "    Waveforms {"]
        for name in names:
            sample_time = "50ns" if modified else "0ns"
            if not name.startswith("dout_"):
                lines += [f"      {name} {{ 0 {{ '0ns' D; }} }}", f"      {name} {{ 1 {{ '0ns' U; }} }}"]
            if name.startswith(("dout_", "io_")):
                lines += [f"      {name} {{ L {{ '{sample_time}' L; }} }}", f"      {name} {{ H {{ '{sample_time}' H; }} }}"]
        lines += ["    }", "  }", "}"]
        filename = "FPGASTIL_RATE100_project_mod.stil" if modified else "FPGASTIL_RATE100_project.stil"
        (root / filename).write_text("\n".join(lines) + "\n")
    patterns = root / "Patterns"
    patterns.mkdir()
    lines = ['Header { Title "Public arithmetic counter, not recorded waveform data"; }',
             "Pattern FPGASTIL_RATE100_1 {", "  W FPGASTIL_RATE100_WFT;"]
    for value in range(4096):
        row = str(value & 1) + "000" + "".join("H" if value & (1 << bit) else "L" for bit in range(7))
        row += "00000000" + "0000" + "1"
        lines.append("  V { PINS_FPGASTIL_RATE100 = " + row + "; }")
    (patterns / "FPGASTIL_RATE100.stil").write_text("\n".join(lines + ["}"]) + "\n")
    return root
