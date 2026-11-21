"""Public malformed-STIL negatives: retain rejection without formatter crashes."""
from pathlib import Path

import pytest

from stil.pattern_package import PatternPackageError, PatternPackageOptions, build_pattern_package
from stil.source_analysis import source_rejection


@pytest.mark.parametrize("capability", [None, {}, {"backend_candidates": None},
                                      {"backend_candidates": {"rate-vector": None}}, []])
def test_nullable_capability_preserves_the_original_error(capability):
    message = source_rejection({"capability": capability,
                                "anomalies": [{"severity": "error", "code": "ERR_SYNTHETIC",
                                               "message": "public invalid input"}]})
    assert message == ("source analysis is REJECT; package generation is blocked: "
                       "ERR_SYNTHETIC: public invalid input")


@pytest.mark.parametrize("report", [{}, {"anomalies": None, "capability": None},
                                  {"anomalies": [None], "capability": {}},
                                  {"capability": {"backend_candidates": {
                                      "rate-vector": {"runtime_capacity_assessment": None}}}}])
def test_incomplete_diagnostic_has_a_rejected_fallback(report):
    assert source_rejection(report).endswith("no usable source analysis; inspect the retained source.analysis.json")


def test_normal_backend_and_capacity_reasons_are_not_dropped():
    message = source_rejection({"anomalies": [], "capability": {"backend_candidates": {
        "rate-vector": {"error": "synthetic backend error", "runtime_capacity_assessment": {
            "blocking_codes": ["ERR_SYNTHETIC_CAPACITY"]}}}}})
    assert "rate-vector: synthetic backend error; rate-vector: ERR_SYNTHETIC_CAPACITY" in message


def test_invalid_public_source_is_a_package_rejection_not_attribute_error(tmp_path: Path):
    source = tmp_path / "invalid.stil"
    source.write_text("Signals {\n  PUBLIC_SIGNAL In;\n}\nPattern MAIN {\n  V { UNKNOWN = 0; }\n}\n", encoding="utf-8")
    with pytest.raises(PatternPackageError, match="source analysis is REJECT"):
        build_pattern_package(PatternPackageOptions(input_path=source, output_dir=tmp_path / "package"))
    assert (tmp_path / "package" / "source.analysis.json").is_file()
    assert not (tmp_path / "package" / "pattern.bin").exists()
