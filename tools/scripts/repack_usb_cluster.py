#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path


SKIP_PREFIXES = (
    ".debug",
    ".comment",
    ".ARM.attributes",
)
SKIP_EXACT = {
    "",
    ".text",
    ".data",
    ".bss",
}
PAD_MARKER = "__STPG_USB_PAD__"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", type=Path, nargs="+", required=True)
    parser.add_argument("--input-pad", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--anchor-object", type=Path, required=True)
    parser.add_argument("--anchor-section", required=True)
    parser.add_argument("--pad-section", required=True)
    parser.add_argument("--position", choices=("before", "after"), default="after")
    parser.add_argument("--ld", default="arm-none-eabi-ld")
    return parser.parse_args()


def section_rows(obj_path: Path) -> list[tuple[str, str]]:
    output = subprocess.check_output(
        ["arm-none-eabi-objdump", "-h", str(obj_path)],
        text=True,
    )
    rows: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        name = parts[1]
        if name in SKIP_EXACT or name.startswith(SKIP_PREFIXES):
            continue
        section_type = "noload" if name.startswith(".bss") else "load"
        rows.append((name, section_type))
    return rows


def quoted(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def emit_section(output_name: str, input_file: Path, input_section: str, section_type: str) -> str:
    noload = " (NOLOAD)" if section_type == "noload" else ""
    return f"  {output_name}{noload} : {{ {quoted(input_file)}({input_section}) }}"


def build_script(
    inputs: list[Path],
    *,
    anchor_object: Path,
    anchor_section: str,
    pad_section: str,
    position: str,
) -> str:
    out = ["SECTIONS", "{"]
    ordinal = 0
    inserted = False
    anchor_object = anchor_object.resolve()

    for obj in inputs:
        obj = obj.resolve()
        rows = section_rows(obj)
        for section_name, section_type in rows:
            if obj == anchor_object and section_name == anchor_section and position == "before":
                out.append(
                    emit_section(
                        f".text{pad_section}.__stpg_{ordinal:04d}",
                        Path(PAD_MARKER),
                        pad_section,
                        "load",
                    )
                )
                ordinal += 1
                inserted = True
            out.append(
                emit_section(
                    f"{section_name}.__stpg_{ordinal:04d}",
                    obj,
                    section_name,
                    section_type,
                )
            )
            ordinal += 1
            if obj == anchor_object and section_name == anchor_section and position == "after":
                out.append(
                    emit_section(
                        f".text{pad_section}.__stpg_{ordinal:04d}",
                        Path(PAD_MARKER),
                        pad_section,
                        "load",
                    )
                )
                ordinal += 1
                inserted = True

    if not inserted:
        raise SystemExit(f"anchor {anchor_object}:{anchor_section} not found")

    out.append("  /DISCARD/ : { *(.note.GNU-stack) }")
    out.append("}")
    return "\n".join(out) + "\n"


def main() -> int:
    args = parse_args()
    script_text = build_script(
        args.inputs,
        anchor_object=args.anchor_object,
        anchor_section=args.anchor_section,
        pad_section=args.pad_section,
        position=args.position,
    )
    script_text = script_text.replace(
        quoted(Path(PAD_MARKER)),
        quoted(args.input_pad.resolve()),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".ld", delete=False) as handle:
        handle.write(script_text)
        script_path = Path(handle.name)
    try:
        subprocess.check_call(
            [
                args.ld,
                "-r",
                "-T",
                str(script_path),
                "-o",
                str(args.output),
                *(str(path) for path in args.inputs),
                str(args.input_pad),
            ]
        )
    finally:
        script_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
