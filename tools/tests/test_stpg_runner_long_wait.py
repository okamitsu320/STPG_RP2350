"""Public synthetic long periods; no real ports or device I/O."""
from dataclasses import replace
from pathlib import Path

import pytest

from stil.device_precheck import inspect_pattern_requirements
from stil.json_artifacts import read_json_artifact
from stil.runtime_wait_model import iter_runtime_events
from stil.segmented_package import SegmentedPackageError, build_segmented_packages
from stpg import constants as c
from stpg.pattern import LoopBlock, LoopRefRecord, pack_wide_rate_vector, unpack_rate_vector_full, validate_pattern
from stpg_runner import build_project, init_project, prepare_run, run_bundle, verify_bundle
from stpg_runner.artifacts import read_json
from test_stpg_runner_execution import Clock, DEVICE, FirmwarePeer, ports
from test_stpg_runner_wait_capacity import source_text

pytestmark = pytest.mark.stpg_host_only


def write_source(path: Path, *, period="10s", vectors=12, slow_at=7) -> Path:
    prefix = source_text(2).split("Pattern MAIN")[0]
    wft = prefix.split("Timing {\n", 1)[1].rsplit("}\n", 1)[0]
    slow = wft.replace("NORMAL", "SLOW").replace("400ns", period)
    prefix = prefix.replace(wft, wft + slow)
    lines = ["Pattern MAIN {", " W NORMAL;"]
    for i in range(vectors):
        if i == slow_at:
            lines.append(" W SLOW;")
        elif i == slow_at + 1:
            lines.append(" W NORMAL;")
        lines.append(f" V {{ ALL = {i % 2}; }}")
    path.write_text(prefix + "\n".join(lines + ["}", ""]), encoding="utf-8")
    return path


def test_segmented_wait_planner_accepts_mixed_narrow_and_wide_inputs(tmp_path):
    """A long WFT affects only one segment; it is not an inserted wait."""
    source = write_source(tmp_path / "synthetic.stil")
    project = init_project(source, tmp_path / "project", wait_preset="chunk-4ms",
                           completion="hold", service_us=1,
                           service_source="synthetic budget", service_scope="non-loop",
                           max_total_wait_us=10, max_segment_vectors=6)
    result = build_project(project)
    root = result.bundle_dir
    assert result.package_kind == "segmented"
    assert verify_bundle(root, manifest_sha256=result.manifest_sha256)["status"] == "PASS"
    plan = prepare_run(root, manifest_sha256=result.manifest_sha256)
    assert [unpack_rate_vector_full(s.payload)[0].version for s in plan.segments] == [
        c.STPG_PATTERN_VERSION, c.STPG_PATTERN_VERSION_WIDE_WAIT]
    assert sum(s.expected["events"] for s in plan.segments) == 12
    assert plan.segments[1].required_capability & c.CAP_WIDE_RATE_VECTOR_32
    assert not plan.segments[0].required_capability & c.CAP_WIDE_RATE_VECTOR_32
    assert max(e.desired_delta_ticks for e in iter_runtime_events(plan.segments[1].payload)) == 2_000_000_000
    manifest = read_json(root / "runner.bundle.json")
    nominal = (root / manifest["package_manifest"]).parent
    acceptance = read_json_artifact(nominal / "segment.wait.plan.acceptance.json")
    assert acceptance["status"] == "ACCEPTED"
    assert acceptance["model"]["pattern"]["versions"] == [1, 2]
    schedule = read_json_artifact(nominal / "segment.wait.plan.json")
    assert all(b["wait_ticks"] == 0 for b in schedule["boundaries"])  # only segment-entry boundaries


def test_mixed_versions_still_check_actual_inserted_wait_limit(tmp_path):
    source = write_source(tmp_path / "synthetic.stil")
    options = dict(input_path=source, ranges=((0, 5), (6, 11)), mode="rate-vector",
                   tick_hz=200_000_000, chunk_event_capacity=2, service_time_us=1,
                   service_time_source="synthetic budget", max_wait_us=4000,
                   segment_wait_plan="every-boundary")
    with pytest.raises(SegmentedPackageError, match="exceeds max_total_wait_us"):
        build_segmented_packages(output_dir=tmp_path / "limited", report_path=tmp_path / "limited.json",
                                 max_total_wait_us=11999, **options)
    result = build_segmented_packages(output_dir=tmp_path / "fits", report_path=tmp_path / "fits.json",
                                      max_total_wait_us=12000, **options)
    assert result["status"] == "COMPLETE"
    nominal = tmp_path / "fits/nominal"
    plan = read_json_artifact(nominal / "segment.wait.plan.json")
    assert [b["wait_ticks"] for b in plan["boundaries"]] == [0, 800000, 800000, 0, 800000, 0]
    for segment in result["variants"][0]["segments"]:
        preflight = inspect_pattern_requirements((nominal / segment["path"]).read_bytes())
        assert preflight.estimated_event_count == 6


@pytest.mark.parametrize("period,requires", [("249999995ns", False), ("250ms", True), ("3s", True), ("10s", True)])
def test_encoded_gap_threshold_matches_runtime_timing(tmp_path, period, requires):
    from stil.pipeline import CompileOptions, compile_stil

    source = write_source(tmp_path / "synthetic.stil", period=period)
    payload, _, _ = compile_stil(CompileOptions(input_path=source, mode="rate-vector", tick_hz=200_000_000))
    measured = max(e.desired_delta_ticks for e in iter_runtime_events(payload)) >= 50_000_000
    assert measured is requires
    assert inspect_pattern_requirements(payload).requires_stream_wait_watchdog is requires


def test_repeat_and_loop_wrap_gaps_without_expansion(tmp_path):
    from stil.pipeline import CompileOptions, compile_stil

    # Long period on the last record: its trailing period alone is not an
    # emitted gap. A VectorRecord repeat or a Loop traversal adds that gap.
    source = write_source(tmp_path / "synthetic.stil", period="3s", vectors=2, slow_at=1)
    payload, _, _ = compile_stil(CompileOptions(input_path=source, mode="rate-vector", tick_hz=200_000_000))
    header, rates, slots, entries, _ = unpack_rate_vector_full(payload)
    assert not inspect_pattern_requirements(payload).requires_stream_wait_watchdog
    repeated = pack_wide_rate_vector(rates, slots, [entries[0], replace(entries[1], repeat=4)],
                                     tick_hz=header.tick_hz, active_pins=header.active_pins)
    validate_pattern(repeated)
    assert inspect_pattern_requirements(repeated).requires_stream_wait_watchdog
    loop = pack_wide_rate_vector(rates, slots, [LoopRefRecord(loop_id=0)],
                                 tick_hz=header.tick_hz, active_pins=header.active_pins,
                                 loop_blocks=[LoopBlock(loop_count=4, body_records=tuple(entries))])
    validate_pattern(loop)
    assert inspect_pattern_requirements(loop).requires_stream_wait_watchdog


@pytest.mark.parametrize("streamed,supports,admit", [(True, False, False), (True, True, True), (False, False, True)])
def test_long_gap_preflight_uses_live_firmware_capability_before_reset(tmp_path, streamed, supports, admit):
    source = write_source(tmp_path / "synthetic.stil", period="3s")
    project = init_project(source, tmp_path / "project", wait_preset="none", completion="hold",
                           max_segment_vectors=6 if streamed else None, compile_mode="rate-vector")
    result = build_project(project)
    assert result.package_kind == ("segmented" if streamed else "single")
    cap = DEVICE.capability | (c.CAP_STREAM_WAIT_WATCHDOG if supports else 0)
    device = replace(DEVICE, capability=cap)
    peer = FirmwarePeer(mutate=lambda _, reply: replace(reply, capability=cap))
    clock = Clock()
    report = run_bundle(result.bundle_dir, manifest_sha256=result.manifest_sha256, device=device,
                        output_dir=tmp_path / "run", confirm_run=True, stream_factory=lambda _: peer,
                        port_provider=ports, clock=clock, sleep=clock.sleep)
    assert peer.close_count == 1
    if admit:
        assert report["outcome"] == "COMPLETED", report["errors"]
        assert sum(cmd.cmd == c.CMD_RUN for cmd in peer.commands) == (2 if streamed else 1)
    else:
        assert report["outcome"] == "REJECT"
        assert "timer-aware stream watchdog" in report["errors"][-1]
        assert [cmd.cmd for cmd in peer.commands] == [c.CMD_INFO, c.CMD_GET_CLOCK_INFO, c.CMD_GET_STATUS]
        assert not peer.payloads
