from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from stil.json_artifacts import read_json_artifact, resolve_json_artifact
from stil.pattern_package import (
    PatternPackageError,
    PatternPackageOptions,
    build_pattern_package,
    derive_pattern_package_with_wait,
    verify_pattern_package,
)
from stil.pipeline import CompileOptions, compile_stil
from stil.wait_acceptance import evaluate_wait_schedule
from stil.wait_schedule import build_wait_schedule, set_wait


STIL = Path(__file__).parent / "fixtures" / "stil" / "single_rate_nrz.stil"
RAW_STIL = Path(__file__).parent / "fixtures" / "stil" / "c1_macro_m1_200ns.stil"


def test_build_blocks_pattern_that_exceeds_legacy_runtime_event_capacity(tmp_path: Path) -> None:
    source = tmp_path / "oversized.stil"
    source.write_text(
        """Header { Title \"oversized\"; }
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

    with pytest.raises(PatternPackageError, match="source analysis is REJECT"):
        build_pattern_package(
            PatternPackageOptions(
                input_path=source,
                output_dir=tmp_path / "package",
                mode="rate-vector",
                runtime_profile="current",
            )
        )

    analysis = read_json_artifact(tmp_path / "package" / "source.analysis.json")
    assert any(item["code"] == "ERR_RUNTIME_STREAM_EVENT_LIMIT" for item in analysis["anomalies"])


def test_build_rate_vector_package_is_reproducible_and_host_only(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"

    report = build_pattern_package(
        PatternPackageOptions(
            input_path=STIL,
            output_dir=package_dir,
            mode="rate-vector",
        )
    )

    assert report["status"] == "VERIFIED"
    assert report["physical_execution_authorized"] is False
    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    expected_files = {Path(item["path"]).name for item in manifest["artifacts"].values()}
    expected_files.add("manifest.json")
    assert {item.name for item in package_dir.iterdir() if item.is_file()} == expected_files
    assert {"pattern.map.json", "pattern.wait.json"} <= expected_files
    assert isinstance(read_json_artifact(package_dir / "pattern.map.json")["vectors"], dict)
    assert manifest["schema_version"] == 3
    assert read_json_artifact(package_dir / "pattern.wait.validation.json")["status"] == "PASS"
    assert read_json_artifact(package_dir / "pattern.wait.acceptance.json")["status"] == (
        "REQUIRES_SERVICE_PROFILE"
    )
    metadata = read_json_artifact(package_dir / "pattern.metadata.json")
    assert metadata["pattern"]["runtime_capacity_observation"]["metrics"]["event_count"]["value"] == 2
    assert verify_pattern_package(package_dir)["status"] == "PASS"

    compiled, _map, _report = compile_stil(
        CompileOptions(input_path=STIL, mode="rate-vector", tick_hz=200_000_000)
    )
    assert (package_dir / "pattern.bin").read_bytes() == compiled


def test_v1_manifest_with_uncompressed_json_sidecars_remains_verifiable(tmp_path: Path) -> None:
    package_dir = tmp_path / "legacy-v1"
    build_pattern_package(
        PatternPackageOptions(input_path=STIL, output_dir=package_dir, mode="rate-vector")
    )
    manifest_path = package_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 3

    for descriptor in manifest["artifacts"].values():
        stored_path = package_dir / descriptor["path"]
        stored = stored_path.read_bytes()
        if stored.startswith(b"\x1f\x8b"):
            uncompressed = gzip.decompress(stored)
            plain_name = stored_path.name.removesuffix(".gz")
            plain_path = package_dir / plain_name
            plain_path.write_bytes(uncompressed)
            stored_path.unlink()
            descriptor["path"] = plain_name
            descriptor["size_bytes"] = len(uncompressed)
            descriptor["sha256"] = hashlib.sha256(uncompressed).hexdigest()
        descriptor.pop("encoding", None)
        descriptor.pop("uncompressed_size_bytes", None)
        descriptor.pop("uncompressed_sha256", None)
    manifest["schema_version"] = 1
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert verify_pattern_package(package_dir)["status"] == "PASS"


@pytest.mark.parametrize("legacy_schema", [1, 2])
def test_legacy_manifest_schema_versions_remain_verifiable(
    tmp_path: Path, legacy_schema: int
) -> None:
    package_dir = tmp_path / f"legacy-v{legacy_schema}"
    build_pattern_package(
        PatternPackageOptions(input_path=STIL, output_dir=package_dir, mode="rate-vector")
    )
    manifest_path = package_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = legacy_schema
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert verify_pattern_package(package_dir)["status"] == "PASS"


def test_build_can_reuse_hash_bound_schedule_and_evaluate_bounded_model(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    build_pattern_package(
        PatternPackageOptions(input_path=STIL, output_dir=first_dir, mode="rate-vector")
    )

    second_dir = tmp_path / "second"
    report = build_pattern_package(
        PatternPackageOptions(
            input_path=STIL,
            output_dir=second_dir,
            mode="rate-vector",
            wait_schedule_path=first_dir / "pattern.wait.json",
            service_time_us=0.2,
            service_time_source="bounded package test profile",
            max_wait_us=1.0,
            max_total_wait_us=10.0,
        )
    )

    assert report["status"] == "VERIFIED"
    acceptance = read_json_artifact(second_dir / "pattern.wait.acceptance.json")
    assert acceptance["status"] == "ACCEPTED"
    assert acceptance["steady_tfill"]["status"] == "not_emitted"
    assert acceptance["physical_execution_authorized"] is False


def test_non_rate_vector_package_marks_waits_not_applicable(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"

    build_pattern_package(
        PatternPackageOptions(
            input_path=RAW_STIL,
            output_dir=package_dir,
            mode="raw-event",
        )
    )

    validation = read_json_artifact(package_dir / "pattern.wait.validation.json")
    acceptance = read_json_artifact(package_dir / "pattern.wait.acceptance.json")
    assert validation["status"] == "NOT_APPLICABLE"
    assert acceptance["status"] == "NOT_APPLICABLE"
    assert verify_pattern_package(package_dir)["status"] == "PASS"


def test_verify_rejects_tampered_artifact(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    build_pattern_package(
        PatternPackageOptions(input_path=STIL, output_dir=package_dir, mode="rate-vector")
    )
    pattern = package_dir / "pattern.bin"
    pattern.write_bytes(pattern.read_bytes()[:-1] + bytes([pattern.read_bytes()[-1] ^ 0x01]))

    report = verify_pattern_package(package_dir)

    assert report["status"] == "REJECT"
    assert any("artifact pattern.bin" in error for error in report["errors"])


def test_derive_wait_package_rebuilds_hash_bound_artifacts(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    build_pattern_package(PatternPackageOptions(input_path=STIL, output_dir=source_dir, mode="rate-vector"))
    schedule = tmp_path / "edited.wait.json"
    schedule.write_text(
        json.dumps(
            build_wait_schedule(
                source_dir / "pattern.bin",
                map_path=source_dir / "pattern.map.json",
                chunk_event_capacity=1,
            )
        ),
        encoding="utf-8",
    )
    edited = tmp_path / "edited.wait.json"
    set_wait(
        source_dir / "pattern.bin",
        schedule,
        map_path=source_dir / "pattern.map.json",
        before_chunk_index=1,
        wait_us="2",
        reason="package derivation test",
        output_path=edited,
    )
    acceptance = evaluate_wait_schedule(
        source_dir / "pattern.bin",
        edited,
        map_path=source_dir / "pattern.map.json",
        service_time_us=0.2,
        service_time_source="bounded package test profile",
    )
    assert acceptance["status"] == "ACCEPTED"
    acceptance_path = tmp_path / "acceptance.json"
    acceptance_path.write_text(json.dumps(acceptance), encoding="utf-8")

    derived_dir = tmp_path / "derived"
    result = derive_pattern_package_with_wait(
        source_dir,
        schedule_path=edited,
        acceptance_path=acceptance_path,
        output_dir=derived_dir,
    )

    assert result["status"] == "VERIFIED"
    assert verify_pattern_package(derived_dir)["status"] == "PASS"
    assert read_json_artifact(derived_dir / "pattern.wait.json")["status"] == "APPLIED"
    assert resolve_json_artifact(derived_dir / "pattern.wait.patch.json").is_file()
    assert resolve_json_artifact(derived_dir / "pattern.wait.source.json").is_file()
    for name in ("source.analysis.json", "pattern.compile_report.json"):
        before = resolve_json_artifact(source_dir / name)
        after = resolve_json_artifact(derived_dir / name)
        assert after.name == before.name
        assert after.read_bytes() == before.read_bytes()
