#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-usbd", type=Path, required=True)
    parser.add_argument("--input-pad", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ld", default="arm-none-eabi-ld")
    parser.add_argument(
        "--anchor-section",
        default=".text.tud_task_ext",
        help="section in input-usbd that pad is inserted relative to",
    )
    parser.add_argument(
        "--pad-section",
        required=True,
        help="section name contributed by input-pad",
    )
    parser.add_argument(
        "--position",
        choices=("before", "after"),
        default="after",
        help="insert pad before or after anchor-section",
    )
    return parser.parse_args()


def section_rows(obj_path: Path) -> list[tuple[str, str]]:
    output = subprocess.check_output(
        ["arm-none-eabi-objdump", "-h", str(obj_path)],
        text=True,
    )
    rows: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        if not parts[0].isdigit():
            continue
        name = parts[1]
        if name in SKIP_EXACT or name.startswith(SKIP_PREFIXES):
            continue
        section_type = "noload" if name.startswith(".bss") else "load"
        rows.append((name, section_type))
    return rows


def build_script(
    rows: list[tuple[str, str]],
    *,
    anchor_section: str,
    pad_section: str,
    position: str,
) -> str:
    out: list[str] = ["SECTIONS", "{"]
    inserted = False
    for name, section_type in rows:
        if name == anchor_section and position == "before":
            out.append(f"  {pad_section} : {{ *({pad_section}) }}")
            if section_type == "noload":
                out.append(f"  {name} (NOLOAD) : {{ *({name}) }}")
            else:
                out.append(f"  {name} : {{ *({name}) }}")
            inserted = True
            continue
        if name == anchor_section and position == "after":
            if section_type == "noload":
                out.append(f"  {name} (NOLOAD) : {{ *({name}) }}")
            else:
                out.append(f"  {name} : {{ *({name}) }}")
            out.append(f"  {pad_section} : {{ *({pad_section}) }}")
            inserted = True
            continue
        if section_type == "noload":
            out.append(f"  {name} (NOLOAD) : {{ *({name}) }}")
        else:
            out.append(f"  {name} : {{ *({name}) }}")
    if not inserted:
        raise SystemExit(f"anchor section {anchor_section} not found in input-usbd")
    out.append("  /DISCARD/ : { *(.note.GNU-stack) }")
    out.append("}")
    return "\n".join(out) + "\n"


def main() -> int:
    args = parse_args()
    rows = section_rows(args.input_usbd)
    script_text = build_script(
        rows,
        anchor_section=args.anchor_section,
        pad_section=args.pad_section,
        position=args.position,
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
                str(args.input_usbd),
                str(args.input_pad),
            ]
        )
    finally:
        script_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
