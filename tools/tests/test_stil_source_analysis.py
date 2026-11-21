from __future__ import annotations

import json
from pathlib import Path

from stil.cli_analyze import main as analyze_main
from stil.cli_compile import main as compile_main
from stil.runtime_capacity import RuntimeCapacityProfile
from stil.source_analysis import analyze_stil_source


FIXTURES = Path(__file__).parent / "fixtures" / "stil"


def test_source_analysis_normal_rate_vector_is_pass_and_has_workflow_fields(tmp_path: Path) -> None:
    output = tmp_path / "single.analysis.json"

    assert analyze_main([str(FIXTURES / "single_rate_nrz.stil"), "-o", str(output)]) == 0

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["kind"] == "stpg_stil_source_analysis"
    assert report["status"] == "PASS"
    assert report["decision"]["physical_execution_authorized"] is False
    assert report["decision"]["generation_ready"] is True
    assert report["vectors"]["vector_count"] == 2
    assert report["vectors"]["event_group_count"] == 2
    assert report["capability"]["backend_candidates"]["rate-vector"]["status"] == "available"
    capacity = report["runtime_capacity_assessments"]["rate-vector"]
    assert capacity["metrics"]["sample_count"]["value"] == 0
    assert capacity["definitions"]["compare_count"]
    assert report["wait_planning_inputs"]["automatic_wait_insertion"] is False
    assert report["wait_planning_inputs"]["streaming_timing_gate"]["status"] == "REJECT"
    assert report["wait_planning_inputs"]["selected_execution_path"] == "direct"
    assert report["wait_planning_inputs"]["selected_execution_path_status"] == "PASS"
    assert report["wait_planning_inputs"]["boundary_wait_decision"]["status"] == (
        "NOT_REQUIRED_FOR_DIRECT"
    )
    assert report["input"]["source"]["sha256"]


def test_cli_analyze_rejects_rate_vector_streaming_gap_before_package_generation(tmp_path: Path) -> None:
    source = tmp_path / "dense_gap.stil"
    source.write_text(
        """Header { Title \"dense-gap\"; }
Signals {
  D0 InOut;
}
SignalGroups {
  ALL = 'D0';
}
Timing {
WaveformTable WFT1 {
    Period '200ns';
    Waveforms {
      D0 { P: 0ns Drive(0), 50ns Drive(1); }
    }
  }
}
Pattern MAIN {
  W WFT1;
  Loop 7000 {
    V { ALL = P; }
  }
}
""",
        encoding="utf-8",
    )
    output = tmp_path / "dense-gap.analysis.json"

    assert analyze_main([
        str(source), "-o", str(output), "--mode", "rate-vector",
        "--tick-hz", "150000000", "--runtime-profile", "psram-capacity-compare-free",
    ]) == 2

    report = json.loads(output.read_text(encoding="utf-8"))
    codes = {item["code"] for item in report["anomalies"]}
    assert report["status"] == "REJECT"
    assert "ERR_STREAMING_POC_TIMING" in codes
    candidate = report["capability"]["backend_candidates"]["rate-vector"]
    assert candidate["streaming_poc_compatible"] is False
    assert len(candidate["streaming_poc_timing_issues"]) >= 1
    assert all("deficit_ticks" in issue for issue in candidate["streaming_poc_timing_issues"])
    assert report["wait_planning_inputs"]["streaming_timing_gate"]["status"] == "REJECT"
    assert report["wait_planning_inputs"]["boundary_wait_decision"]["status"] == "NOT_A_REMEDIATION"


def test_cli_analyze_reports_runtime_event_limit_before_generation(tmp_path: Path) -> None:
    source = tmp_path / "oversized_stream.stil"
    source.write_text(
        """Header { Title \"oversized-stream\"; }
Signals {
  D0 InOut;
}
SignalGroups {
  ALL = 'D0';
}
Timing {
WaveformTable WFT1 {
    Period '1000ns';
    Waveforms {
      D0 { P: 0ns Drive(0), 500ns Drive(1); }
    }
  }
}
Pattern MAIN {
  W WFT1;
  Loop 7000 {
    V { ALL = P; }
  }
}
""",
        encoding="utf-8",
    )

    output = tmp_path / "oversized-stream.analysis.json"
    assert analyze_main([
        str(source), "-o", str(output), "--mode", "rate-vector",
        "--runtime-profile", "current",
    ]) == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    codes = {item["code"] for item in report["anomalies"]}
    assert report["status"] == "REJECT"
    assert "ERR_RUNTIME_STREAM_EVENT_LIMIT" in codes
    assert report["runtime_capacity"]["name"] == "current"
    candidate = report["capability"]["backend_candidates"]["rate-vector"]
    assert candidate["estimated_event_count"] == 14000
    assert candidate["pattern_size_bytes"] > 0

    f57_output = tmp_path / "oversized-stream.f57.analysis.json"
    assert (
        analyze_main(
            [
                str(source),
                "-o",
                str(f57_output),
                "--mode",
                "rate-vector",
                "--runtime-profile",
                "f57-ring6",
            ]
        )
        == 0
    )


def test_cli_analyze_segment_on_capacity_routes_to_host_only_segmenter(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "capacity-only.stil"
    source.write_text("placeholder\n", encoding="utf-8")
    analysis_output = tmp_path / "capacity-only.analysis.json"
    segmented_output = tmp_path / "capacity-only.segmented.json"
    segmented_dir = tmp_path / "capacity-only.segments"

    analysis = {
        "status": "REJECT",
        "vectors": {"vector_count": 5},
        "segmentation": {
            "available": True,
            "status": "RECOMMENDED",
            "recommended_segment_vector_count": 100_000,
            "physical_execution_authorized": False,
        },
    }
    monkeypatch.setattr("stil.cli_analyze.analyze_stil_source", lambda *args, **kwargs: analysis)
    captured: dict[str, object] = {}

    def fake_segmented_builder(**kwargs):
        captured.update(kwargs)
        return {"status": "COMPLETE", "variants": []}

    monkeypatch.setattr("stil.segmented_package.build_segmented_packages", fake_segmented_builder)

    assert analyze_main(
        [
            str(source),
            "--output",
            str(analysis_output),
            "--mode",
            "rate-vector",
            "--segment-on-capacity",
            "--segment-vector-count",
            "2",
            "--segment-output-dir",
            str(segmented_dir),
            "--segment-json-out",
            str(segmented_output),
        ]
    ) == 0

    assert captured["input_path"] == source
    assert captured["ranges"] == ((0, 1), (2, 3), (4, 4))
    updated = json.loads(analysis_output.read_text(encoding="utf-8"))
    assert updated["segmentation"]["status"] == "COMPLETE"
    assert updated["segmentation"]["physical_execution_authorized"] is False
    assert updated["segmentation"]["report_path"] == str(segmented_output)


def test_source_analysis_reports_sample_limit_and_compare_definition_before_package(tmp_path: Path) -> None:
    profile = RuntimeCapacityProfile(
        name="test-sample-capacity",
        full_expansion_event_limit=1,
        streaming_event_limit=1_000,
        streaming_sample_limit=1,
        streaming_compare_limit=1_000,
        supported_stream_modes=("rate-vector",),
    )

    report = analyze_stil_source(
        FIXTURES / "compare_hlt.stil",
        mode="rate-vector",
        runtime_profile=profile,
    )

    codes = {item["code"] for item in report["anomalies"]}
    assert report["status"] == "REJECT"
    assert "ERR_RUNTIME_STREAM_SAMPLE_LIMIT" in codes
    observation = report["runtime_capacity_assessments"]["rate-vector"]
    assert observation["metrics"]["sample_count"]["value"] == 2
    assert observation["metrics"]["sample_count"]["limit"] == 1
    assert observation["metrics"]["sample_count"]["status"] == "exceeds_limit"
    assert observation["metrics"]["compare_count"]["value"] == 2
    assert observation["definitions"]["compare_count"]


def test_source_analysis_rejects_structural_and_timing_anomalies(tmp_path: Path) -> None:
    source = tmp_path / "bad.stil"
    source.write_text(
        """Header { Title \"bad\"; }
Signals {
  D0 InOut;
  D0 InOut;
}
SignalGroups {
  ALL = 'D0';
}
Timing {
  WaveformTable WFT1 {
    Period '20ns';
    Waveforms {
      ALL { 1: 20ns Drive(1); }
    }
  }
}
Pattern MAIN {
  W WFT1;
  V { ALL = 1; }
}
""",
        encoding="utf-8",
    )

    report = analyze_stil_source(source)
    codes = {item["code"] for item in report["anomalies"]}
    assert report["status"] == "REJECT"
    assert "ERR_DUPLICATE_SIGNAL" in codes
    assert "ERR_EVENT_TIME_OUT_OF_RANGE" in codes
    assert report["decision"]["generation_ready"] is False
    assert report["capability"]["backend_candidates"]["rate-vector"]["error_code"] == "ERR_BACKEND_VALIDATION"


def test_source_analysis_keeps_resolution_error_and_structural_context(tmp_path: Path) -> None:
    source = tmp_path / "unmapped.stil"
    source.write_text(
        """Signals {
  D0 InOut;
}
SignalGroups {
  ALL = 'D0 + MISSING';
}
WaveformTable WFT1 {
  Period '40ns';
  Waveforms {
    ALL { 1: 0ns Drive(1); }
  }
}
Pattern MAIN {
  W WFT1;
  V { ALL = 11; }
}
""",
        encoding="utf-8",
    )

    report = analyze_stil_source(source)
    codes = {item["code"] for item in report["anomalies"]}
    assert report["status"] == "REJECT"
    assert "ERR_GROUP_UNKNOWN_SIGNAL" in codes
    assert "ERR_UNMAPPED_ACTIVE_SIGNAL" in codes
    assert report["source_structure"]["signal_groups"][0]["signals"] == ["D0", "MISSING"]


def test_source_analysis_parse_failure_is_written_by_cli(tmp_path: Path) -> None:
    source = tmp_path / "unsupported.stil"
    source.write_text("Procedure P0 { }\n", encoding="utf-8")
    output = tmp_path / "unsupported.analysis.json"

    assert analyze_main([str(source), "-o", str(output)]) == 2

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "REJECT"
    assert report["decision"]["blocking_codes"] == ["ERR_UNSUPPORTED_BLOCK"]


def test_compile_report_embeds_source_analysis(tmp_path: Path) -> None:
    output = tmp_path / "compiled.bin"

    assert compile_main([str(FIXTURES / "compare_hlt.stil"), "-o", str(output), "--mode", "rate-vector"]) == 0

    report = json.loads(output.with_suffix(".compile_report.json").read_text(encoding="utf-8"))
    source_analysis = report["source_analysis"]
    assert source_analysis["kind"] == "stpg_stil_source_analysis"
    assert source_analysis["selection"]["mode_emitted"] == "rate-vector"
    assert source_analysis["vectors"]["compare_vector_count"] == 2
