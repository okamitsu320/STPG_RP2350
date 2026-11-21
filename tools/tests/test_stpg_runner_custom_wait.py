"""Custom chunk lengths use the existing placement/encoding guards, host only."""
from pathlib import Path

import pytest

from stpg_runner import Project, ProjectError, build_project, init_project, verify_bundle
from stpg_runner.artifacts import identity, read_json, write_json
from stpg_runner.cli import main
from stpg_runner.project import WAIT_MODES, WAIT_PRESETS, chunk_wait_microseconds, project_template
from stil.json_artifacts import read_json_artifact
from test_stpg_runner_wait_capacity import region, source_text

pytestmark = pytest.mark.stpg_host_only
FIXTURE = Path(__file__).parent / "fixtures/runner/pattern.stil"


def options(**overrides):
    return dict(wait_preset="chunk-custom", chunk_wait_us=6500, completion="hold",
                service_us=50, service_source="public declared budget, not measured",
                service_scope="non-loop", max_total_wait_us=1_000_000, **overrides)


def test_presets_stay_numeric_and_custom_is_explicit():
    assert WAIT_PRESETS == {"none": 0, "chunk-4ms": 4000, "chunk-12ms": 12000}
    assert WAIT_MODES == (*WAIT_PRESETS, "chunk-custom")
    for name, duration in WAIT_PRESETS.items():
        fields = {} if name == "none" else dict(service_us=50, service_source="declared", service_scope="non-loop",
                                               max_total_wait_us=1_000_000)
        raw = project_template(stil="input.stil", wait_preset=name, completion="hold", **fields).to_dict()
        assert "chunk_wait_us" not in raw["wait"]  # old project binding bytes are not expanded
        assert Project.from_dict(raw).to_dict() == raw
        assert chunk_wait_microseconds(raw["wait"]) == duration


@pytest.mark.parametrize("value", [None, 0, -1, True, 6500.0, "6500", float("nan"), float("inf"),
                                  21_474_837, 1 << 32, 10**100])
def test_bad_duration_rejects_before_directory_creation(tmp_path, value):
    args = options()
    args["chunk_wait_us"] = value
    with pytest.raises(ProjectError):
        init_project(FIXTURE, tmp_path / "invalid", **args)
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("preset", list(WAIT_PRESETS))
def test_duration_cannot_be_silently_ignored_by_a_named_preset(preset):
    raw = project_template(stil="input.stil", **options()).to_dict()
    raw["wait"]["preset"] = preset
    with pytest.raises(ProjectError, match="only allowed"):
        Project.from_dict(raw)


def test_custom_duration_checks_target_tick_grid_and_does_not_round():
    raw = project_template(stil="input.stil", **options()).to_dict()
    raw["target"]["tick_hz"] = 1
    with pytest.raises(ProjectError, match="whole uint32 target ticks"):
        Project.from_dict(raw)
    raw["wait"]["chunk_wait_us"] = 1_000_000
    assert Project.from_dict(raw).to_dict()["wait"]["chunk_wait_us"] == 1_000_000


@pytest.mark.parametrize("duration", [1, 6500, 21_474_836])
def test_valid_duration_is_distinct_from_service_budget(duration):
    args = options(); args["chunk_wait_us"] = duration
    wait = project_template(stil="input.stil", **args).to_dict()["wait"]
    assert chunk_wait_microseconds(wait) == duration and wait["service_us"] == 50


@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("shape", ["flat", "loop"])
def test_custom_wait_reaches_single_and_wait_aware_segmented_encoding(tmp_path, monkeypatch, segmented, shape):
    source = tmp_path / "public.stil"
    source.write_text(source_text(6000, shape=shape))
    args = options()
    args["service_scope"] = "loop-generic" if shape == "loop" else "non-loop"
    # Force the segmented path independently of byte compression for Loops.
    args["max_segment_vectors"] = 2500 if segmented else None
    project = init_project(source, tmp_path / "project", **args)
    result = build_project(project)
    manifest = read_json(result.bundle_dir / "runner.bundle.json")
    assert result.package_kind == ("segmented" if segmented else "single")
    schedules = (list((result.bundle_dir / "segmented/nominal").glob("segment-*/projected.wait.json*"))
                 if segmented else [result.bundle_dir / "wait.proposed.json"])
    assert schedules
    nonzero = []
    for path in schedules:
        data = read_json_artifact(path)
        assert data["boundaries"][0]["wait_ticks"] == 0
        nonzero += [b["wait_ticks"] for b in data["boundaries"] if b["wait_ticks"]]
    assert nonzero and set(nonzero) == {6500 * 200}
    assert verify_bundle(result.bundle_dir, manifest_sha256=result.manifest_sha256)["status"] == "PASS"
    assert manifest["binding"]["project"]["wait"]["chunk_wait_us"] == 6500


def test_custom_wait_is_counted_in_capacity_first_search(tmp_path, monkeypatch):
    profile = region(monkeypatch, 50000)
    source = tmp_path / "public.stil"; source.write_text(source_text(6000))
    project = init_project(source, tmp_path / "project", **options())
    raw = read_json(project); raw["target"]["runtime_profile"] = profile; write_json(project, raw)
    result = build_project(project)
    report = read_json_artifact(result.bundle_dir / "segmented.report.json")
    assert result.package_kind == "segmented"
    assert report["capacity_plan"]["capacity_basis"] == "every-boundary-encoded-waits"
    for decision in report["capacity_plan"]["decisions"]:
        assert decision["metrics"]["wait_encoding"]["pattern_bytes_after_waits"] <= 50000
    assert verify_bundle(result.bundle_dir)["status"] == "PASS"


@pytest.mark.parametrize("preset,duration", [("chunk-4ms", 4000), ("chunk-12ms", 12000)])
def test_custom_equivalent_of_a_preset_has_identical_pattern_bytes(tmp_path, preset, duration):
    source = tmp_path / "public.stil"; source.write_text(source_text(6000))
    custom = options(); custom["chunk_wait_us"] = duration
    named = {k:v for k,v in custom.items() if k != "chunk_wait_us"}; named["wait_preset"] = preset
    outputs = []
    for name,args in (("named", named), ("custom", custom)):
        result = build_project(init_project(source, tmp_path / name, **args))
        manifest = read_json(result.bundle_dir / "runner.bundle.json")
        outputs.append([(result.bundle_dir / item["path"]).read_bytes() for item in manifest["patterns"]])
    assert outputs[0] == outputs[1]


def test_forbidden_rate_and_total_limit_still_apply(tmp_path):
    source = tmp_path / "public.stil"; source.write_text(source_text(6000))
    args = options(); args["forbidden_stil_rates"] = ("NORMAL",)
    result = build_project(init_project(source, tmp_path / "forbidden", **args))
    schedule = read_json_artifact(result.bundle_dir / "wait.proposed.json")
    assert not any(b["wait_ticks"] for b in schedule["boundaries"])
    args = options(); args["max_total_wait_us"] = 100
    with pytest.raises(ValueError, match="wait plan rejected"):
        build_project(init_project(source, tmp_path / "too-small-total", **args))


@pytest.mark.parametrize("preset", ["chunk-4ms", "chunk-12ms"])
def test_named_preset_cannot_adopt_a_rejected_partial_zero_wait_proposal(tmp_path, preset):
    source = tmp_path / "public.stil"; source.write_text(source_text(6000))
    args = options(); args.pop("chunk_wait_us"); args["wait_preset"] = preset; args["max_total_wait_us"] = 100
    project = init_project(source, tmp_path / "project", **args)
    with pytest.raises(ValueError, match="explicit wait plan rejected"):
        build_project(project)
    proposal = next((project.parent / "build").glob("*/wait.proposal.json"))
    assert read_json_artifact(proposal)["wait_plan"]["status"] == "rejected"
    assert not list((project.parent / "build").glob("*/runner.bundle.json"))


def test_current_verifier_and_rebind_reject_a_saved_rejected_proposal(tmp_path):
    from stpg_runner import rebind_bundle
    args = options(); args.pop("chunk_wait_us"); args["wait_preset"] = "chunk-4ms"
    result = build_project(init_project(FIXTURE, tmp_path / "project", **args))
    path = result.bundle_dir / "wait.proposal.json"
    proposal = read_json(path); proposal["wait_plan"]["status"] = "rejected"
    proposal["wait_plan"]["reason"] = "public synthetic rejected partial proposal"
    write_json(path, proposal)
    # Deliberately repair only a synthetic test inventory to reach semantic
    # validation, not the earlier byte-integrity rejection.
    manifest_path = result.bundle_dir / "runner.bundle.json"
    manifest = read_json(manifest_path); manifest["files"][path.name] = identity(path)
    write_json(manifest_path, manifest)
    checked = verify_bundle(result.bundle_dir)
    assert checked["status"] == "REJECT" and "explicit wait plan rejected" in str(checked["errors"])
    with pytest.raises(ValueError):
        rebind_bundle(result.bundle_dir, manifest_sha256=identity(manifest_path)["sha256"])


def test_cli_custom_input_is_stored_without_sending_anything(tmp_path, capsys):
    assert main(["init", str(FIXTURE), "--directory", str(tmp_path / "project"),
        "--wait-preset", "chunk-custom", "--chunk-wait-us", "6500", "--completion", "hold",
        "--service-us", "50", "--service-source", "public assumption", "--service-scope", "non-loop",
        "--max-total-wait-us", "1000000"]) == 0
    assert read_json(tmp_path / "project/project.stpg-project.json")["wait"]["chunk_wait_us"] == 6500
    assert '"device_io_performed": false' in capsys.readouterr().out
