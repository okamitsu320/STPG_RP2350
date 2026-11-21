from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from stpg.client import StpgClient
from stpg.constants import STORAGE_SRAM, STPG_DEFAULT_TICK_HZ
from stpg.transport import SerialTransport

from .cli_runtime import _build_state_update_pattern
from .pipeline import CompileOptions, compile_stil, write_compile_outputs
from .report import render_fail_report_text


FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "stil"
CASES = {
    "pass": FIXTURE_DIR / "selfloop_compare_pass.stil",
    "fail": FIXTURE_DIR / "selfloop_compare_fail.stil",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a compiler-driven self-loopback validation case")
    parser.add_argument("--port", required=True)
    parser.add_argument("--case", choices=sorted(CASES), default="pass")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--drive-bit", type=int, required=True)
    parser.add_argument("--observe-bit", type=int, required=True)
    parser.add_argument("--active-pins", type=int, choices=[16, 24], default=16)
    parser.add_argument("--marker-bit", type=int)
    parser.add_argument("--fail-bit", type=int)
    parser.add_argument("--marker-vector", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--chunk-size", type=int, default=4096)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    pin_map = build_selfloop_pin_map(
        active_pins=args.active_pins,
        drive_bit=args.drive_bit,
        observe_bit=args.observe_bit,
        marker_bit=args.marker_bit,
        fail_bit=args.fail_bit,
    )

    marker_args = []
    if args.marker_bit is not None:
        marker_args.append(f"__MARK0:vector={args.marker_vector}:mode=pulse")

    output = args.output_dir / f"selfloop_{args.case}.bin"
    with TemporaryDirectory() as tmpdir:
        pin_map_path = Path(tmpdir) / "selfloop_pin_map.json"
        pin_map_path.write_text(json.dumps(pin_map, indent=2), encoding="utf-8")
        payload, map_data, report_data = compile_stil(
            CompileOptions(
                input_path=CASES[args.case],
                mode="auto",
                tick_hz=STPG_DEFAULT_TICK_HZ,
                pin_map_path=pin_map_path,
                markers=tuple(),
            )
        )
        if marker_args:
            from .markers import parse_marker_specs

            payload, map_data, report_data = compile_stil(
                CompileOptions(
                    input_path=CASES[args.case],
                    mode="auto",
                    tick_hz=STPG_DEFAULT_TICK_HZ,
                    pin_map_path=pin_map_path,
                    markers=parse_marker_specs(marker_args, []),
                )
            )
        write_compile_outputs(output, payload, map_data, report_data)

    with SerialTransport(args.port, timeout=args.timeout) as transport:
        client = StpgClient(transport)
        client.reset()
        client.upload(payload, storage_target=STORAGE_SRAM, chunk_size=args.chunk_size)
        client.run()
        status = client.status()
        sample_info = client.sample_info()
        summary = client.fail_summary()
        print(
            f"SELFLOOP case={args.case} state={status.state} error={status.error_code} "
            f"samples={sample_info.sample_count} fails={summary.fail_count} flags=0x{summary.result_flags:08X}"
        )

        if args.case == "pass" and summary.fail_count != 0:
            raise SystemExit(f"selfloop pass case failed: fail_count={summary.fail_count}")
        if args.case == "fail" and summary.fail_count == 0:
            raise SystemExit("selfloop fail case did not produce a fail")

        if summary.fail_count > 0:
            entries = client.read_fail_log(0, summary.fail_count)
            report_text = render_fail_report_text(entries, map_data, pattern_bytes=payload)
            (args.output_dir / f"selfloop_{args.case}.fail.txt").write_text(report_text, encoding="utf-8")
            print(report_text)
            if args.fail_bit is not None:
                mini_pattern = _build_state_update_pattern(report_data, map_data, "__FAIL0", asserted=True)
                client.upload(mini_pattern, storage_target=STORAGE_SRAM, chunk_size=args.chunk_size)
                client.run()
                client.status()
    return 0


def build_selfloop_pin_map(*, active_pins: int, drive_bit: int, observe_bit: int, marker_bit: int | None, fail_bit: int | None) -> dict:
    _validate_bit(active_pins, drive_bit, "drive_bit")
    _validate_bit(active_pins, observe_bit, "observe_bit")
    signals: dict[str, dict] = {
        "DRV": {"bit": drive_bit, "logical_pin": f"DUT{drive_bit}", "role": "signal", "direction": "inout"},
        "OBS": {"bit": observe_bit, "logical_pin": f"DUT{observe_bit}", "role": "signal", "direction": "inout"},
    }
    used_bits = {drive_bit, observe_bit}
    if marker_bit is not None:
        _validate_bit(active_pins, marker_bit, "marker_bit")
        if marker_bit in used_bits:
            raise ValueError("marker_bit must be distinct")
        if active_pins == 24 and marker_bit == 20:
            raise ValueError("marker_bit cannot use logical bit20 on current 24pin board")
        signals["__MARK0"] = {
            "bit": marker_bit,
            "logical_pin": f"DUT{marker_bit}",
            "role": "marker",
            "direction": "inout",
        }
        used_bits.add(marker_bit)
    if fail_bit is not None:
        _validate_bit(active_pins, fail_bit, "fail_bit")
        if fail_bit in used_bits:
            raise ValueError("fail_bit must be distinct")
        if active_pins == 24 and fail_bit == 20:
            raise ValueError("fail_bit cannot use logical bit20 on current 24pin board")
        signals["__FAIL0"] = {
            "bit": fail_bit,
            "logical_pin": f"DUT{fail_bit}",
            "role": "fail",
            "direction": "inout",
        }
    return {
        "active_pins": active_pins,
        "signals": signals,
    }


def _validate_bit(active_pins: int, bit: int, name: str) -> None:
    if bit < 0 or bit >= active_pins:
        raise ValueError(f"{name} out of range for active_pins={active_pins}")


if __name__ == "__main__":
    raise SystemExit(main())
