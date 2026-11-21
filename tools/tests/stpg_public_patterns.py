"""Public synthetic patterns and small helpers shared by the core GUI/runner tests (no analyzer code).

``counter_stil`` is the public counter that the logic-analyzer tool's tests use too; it lives here so that the core
tests do not import that separate distribution.
"""

from __future__ import annotations

from types import SimpleNamespace

from stpg_runner import build_project, init_project


def counter_stil(n: int) -> str:
    """Public synthetic pattern: 8-bit address counter, clock pulse, two compared outputs."""

    sig = [f"A{i}" for i in range(8)] + ["CLK", "EN", "DQ0", "DQ1"]
    lines = ['Header { Title "Synthetic counter pattern (public)"; }', "Signals {"]
    lines += [f"  {s} In;" for s in sig[:10]] + [f"  {s} Out;" for s in sig[10:]]
    lines += ["}", "SignalGroups {", "  ALL = '" + " + ".join(sig) + "';", "}", "Timing {",
              "  WaveformTable NORMAL {", "    Period '400ns';", "    Waveforms {"]
    lines += [f"      {s} {{ 0: 0ns Drive(0); 1: 0ns Drive(1); }}" for s in sig[:8] + ["EN"]]
    lines.append("      CLK { 0: 0ns Drive(0); 1: 0ns Drive(1); P: 0ns Drive(0), 100ns Drive(1), 300ns Drive(0); }")
    lines += [f"      {s} {{ H: 250ns Compare(1); L: 250ns Compare(0); X: 250ns Compare(X); }}" for s in sig[10:]]
    lines += ["    }", "  }", "}", "Pattern MAIN {", "  W NORMAL;"]
    for i in range(n):
        a = (i // 4) % 256 if i < n // 2 else (0x55 if i % 2 else 0xAA)
        bits = "".join("1" if a >> b & 1 else "0" for b in range(8))
        lines.append(f"  V {{ ALL = {bits}P1{'HL'[i % 2]}{'HLX'[i % 3]}; }}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def build_public(tmp_path_factory, name: str, text: str, **init):
    """Build a public STIL with the SDK (no wait, completion hold) in a fresh temporary folder."""

    root = tmp_path_factory.mktemp(name)
    source = root / "pattern.stil"
    source.write_text(text, encoding="utf-8")
    project = init_project(source, root / "project", wait_preset="none", completion="hold", **init)
    return build_project(project)


def ports_for(config):
    """A one-entry port list that matches a binding's port and USB identity (metadata only)."""

    return lambda: [SimpleNamespace(device=config.port, vid=config.vid, pid=config.pid,
                                    serial_number=config.serial_number)]
