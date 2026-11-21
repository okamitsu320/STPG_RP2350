from __future__ import annotations

import argparse

from .parser import parse_stil_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect a STIL subset input")
    parser.add_argument("input")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    document = parse_stil_file(args.input)
    print(f"signals={len(document.signals)} groups={len(document.signal_groups)} wfts={len(document.waveform_tables)} patterns={len(document.patterns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

