from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from .cli_compile import main as compile_main
from .workspace import add_work_dir_options, resolve_work_dir


@dataclass(frozen=True)
class RegressionCase:
    name: str
    fixture: str
    mode: str
    pattern_burst: str | None = None
    pattern: str | None = None
    allow_approx: bool = False
    prefer_raw: bool = False
    expect_mode_emitted: str = ""


def build_cases() -> dict[str, RegressionCase]:
    return {
        "single-rate-nrz": RegressionCase("single-rate-nrz", "single_rate_nrz.stil", "rate-vector", expect_mode_emitted="rate-vector"),
        "multi-wft-rate-switch": RegressionCase("multi-wft-rate-switch", "multi_wft_rate_switch.stil", "rate-vector", expect_mode_emitted="rate-vector"),
        "compare-hlt": RegressionCase("compare-hlt", "compare_hlt.stil", "rate-vector", expect_mode_emitted="rate-vector"),
        "raw-fallback": RegressionCase("raw-fallback", "rate_vector_reject_raw_fallback.stil", "auto", expect_mode_emitted="rate-vector"),
        "patternburst-select": RegressionCase("patternburst-select", "patternburst_multi_pattern_select.stil", "rate-vector", pattern_burst="PB1", pattern="PAT1", expect_mode_emitted="rate-vector"),
        "pn-norm": RegressionCase("pn-norm", "pn_norm.stil", "rate-vector", expect_mode_emitted="rate-vector"),
        "approx-period": RegressionCase("approx-period", "approx_period.stil", "rate-vector", allow_approx=True, expect_mode_emitted="rate-vector"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run public STIL sample regression cases")
    parser.add_argument("--fixture-dir", type=Path, default=Path("tools/tests/fixtures/stil"))
    add_work_dir_options(parser, scope="regression")
    parser.add_argument("--cases", nargs="*", default=sorted(build_cases().keys()))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = resolve_work_dir(args)
    output_dir.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    for case_name in args.cases:
        case = cases[case_name]
        output = output_dir / f"{case.name}.bin"
        argv_case = [str(args.fixture_dir / case.fixture), "-o", str(output), "--mode", case.mode]
        if case.pattern_burst is not None:
            argv_case += ["--pattern-burst", case.pattern_burst]
        if case.pattern is not None:
            argv_case += ["--pattern", case.pattern]
        if case.allow_approx:
            argv_case += ["--allow-approx"]
        if case.prefer_raw:
            argv_case += ["--prefer-raw"]
        rc = compile_main(argv_case)
        if rc != 0:
            raise RuntimeError(f"compile failed for case {case.name}")
        report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
        if report["mode_emitted"] != case.expect_mode_emitted:
            raise RuntimeError(
                f"unexpected emitted mode for {case.name}: expected {case.expect_mode_emitted}, got {report['mode_emitted']}"
            )
        print(f"{case.name}: PASS mode={report['mode_emitted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
