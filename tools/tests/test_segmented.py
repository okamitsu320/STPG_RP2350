from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from stil.segmented import (
    HoldState,
    SegmentSpec,
    _collect_segment_results,
    _validate_segment_result_counts,
    build_segmented_artifacts,
    globalize_fail_entries,
    load_manifest,
    load_segment_map_payloads,
    merge_segment_fail_json,
    merge_segment_maps,
    run_segmented_rate_vector_compare,
    merge_segment_results,
    run_segmented_raw,
)
from stpg.constants import (
    ALLOW_RESULT_READ,
    CAP_STREAM_FAIL_STOP,
    ERR_COMPARE_FAIL_STOP,
    STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW,
    STPG_RESULT_FLAG_SAMPLE_OVERFLOW,
    STATE_DONE,
    STATE_ERROR,
    STATE_HOLD_AWAITING_NEXT,
    STATE_READY,
    STORAGE_PSRAM,
    STPG_DRIVE_VECTOR,
    STPG_RATE_SLOT_DIR_UPDATE,
    STPG_RATE_SLOT_SAMPLE,
)
from stpg.pattern import RateDesc, RateSlot, VectorRecord, pack_rate_vector


def _single_compare_rate_vector() -> bytes:
    return pack_rate_vector(
        [RateDesc(period_ticks=24, slot_count=1, flags=0, slot_offset=0)],
        [
            RateSlot(
                time_ticks=0,
                flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE | STPG_RATE_SLOT_SAMPLE,
                drive_mask=1,
                dir_mask=1,
                sample_mask=1,
            )
        ],
        [VectorRecord(rate_id=0, flags=0, repeat=1, drive_value=0, drive_enable=1,
                      expect_value=0, expect_mask=1)],
        tick_hz=150_000_000,
    )


def test_load_manifest_requires_matching_hold_state(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "final_hold_state": {"drive_value": "0x12", "drive_enable": "0x34"},
                    },
                    {
                        "path": "b.bin",
                        "entry_hold_state": {"drive_value": "0x12", "drive_enable": "0x34"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    assert manifest.segments[0].final_hold_state == HoldState(0x12, 0x34)
    assert manifest.segments[1].entry_hold_state == HoldState(0x12, 0x34)


def test_load_manifest_rejects_non_contiguous_global_event_ranges(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_event_start": 0,
                        "global_event_end": 3,
                        "final_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                    {
                        "path": "b.bin",
                        "global_event_start": 5,
                        "global_event_end": 7,
                        "entry_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="global event range starts"):
        load_manifest(manifest_path)


def test_segmented_rejects_unchecked_compare_before_first_upload(tmp_path: Path) -> None:
    compare_pattern = _single_compare_rate_vector()
    (tmp_path / "a.bin").write_bytes(compare_pattern)
    (tmp_path / "b.bin").write_bytes(compare_pattern)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_event_start": 0,
                        "global_event_end": 0,
                        "final_hold_state": {"drive_value": "0x0", "drive_enable": "0x0"},
                    },
                    {
                        "path": "b.bin",
                        "global_event_start": 1,
                        "global_event_end": 1,
                        "entry_hold_state": {"drive_value": "0x0", "drive_enable": "0x0"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)

    class NoUploadClient:
        calls: list[str] = []

        def upload(self, *_args: object, **_kwargs: object) -> None:
            self.calls.append("upload")

    client = NoUploadClient()
    with pytest.raises(ValueError, match="not checked by SRAM-sample streaming fail-stop"):
        run_segmented_rate_vector_compare(
            client,
            manifest,
            device_capability=CAP_STREAM_FAIL_STOP,
            timeout_s=0.1,
        )
    assert client.calls == []

    with pytest.raises(ValueError, match="does not advertise CAP_STREAM_FAIL_STOP"):
        run_segmented_rate_vector_compare(
            client,
            manifest,
            device_capability=0,
            timeout_s=0.1,
            fail_stop=True,
        )
    assert client.calls == []


def test_globalize_fail_entries_rejects_event_outside_manifest_range(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_event_start": 0,
                        "global_event_end": 3,
                        "final_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                    {
                        "path": "b.bin",
                        "global_event_start": 4,
                        "global_event_end": 7,
                        "entry_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    with pytest.raises(ValueError, match="outside segment"):
        globalize_fail_entries([{"event_index": 4}], manifest.segments[0])


def test_run_segmented_raw_sequences_hold_and_resume(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"A")
    (tmp_path / "b.bin").write_bytes(b"B")
    (tmp_path / "c.bin").write_bytes(b"C")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "final_hold_state": {"drive_value": "0x2", "drive_enable": "0x2"},
                    },
                    {
                        "path": "b.bin",
                        "entry_hold_state": {"drive_value": "0x2", "drive_enable": "0x2"},
                        "final_hold_state": {"drive_value": "0x6", "drive_enable": "0x6"},
                    },
                    {
                        "path": "c.bin",
                        "entry_hold_state": {"drive_value": "0x6", "drive_enable": "0x6"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)

    class FakeStatus:
        def __init__(self, state: int, error_code: int = 0) -> None:
            self.state = state
            self.error_code = error_code
            self.fail_count = 0
            self.sample_count = 0

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []
            self.statuses = [
                FakeStatus(STATE_HOLD_AWAITING_NEXT),
                FakeStatus(STATE_HOLD_AWAITING_NEXT),
                FakeStatus(STATE_DONE),
            ]

        def upload(self, payload: bytes, *, storage_target: int, chunk_size: int) -> None:
            self.calls.append(("upload", payload, storage_target, chunk_size))

        def run_segment_hold(self) -> None:
            self.calls.append(("hold",))

        def run_resume_from_hold(self, drive_value: int, drive_enable: int, *, hold_after: bool = False) -> None:
            self.calls.append(("resume", drive_value, drive_enable, hold_after))

        def status(self):
            return self.statuses.pop(0)

    client = FakeClient()
    hold_statuses, final_status = run_segmented_raw(client, manifest, chunk_size=123, timeout_s=0.1)
    assert len(hold_statuses) == 2
    assert hold_statuses[0].state == STATE_HOLD_AWAITING_NEXT
    assert hold_statuses[1].state == STATE_HOLD_AWAITING_NEXT
    assert final_status.state == STATE_DONE
    assert client.calls == [
        ("upload", b"A", 1, 123),
        ("hold",),
        ("upload", b"B", 1, 123),
        ("resume", 0x2, 0x2, True),
        ("upload", b"C", 1, 123),
        ("resume", 0x6, 0x6, False),
    ]


def test_run_segmented_rate_vector_collects_local_results_before_next_upload(tmp_path: Path) -> None:
    for name in ("a.bin", "b.bin", "c.bin"):
        (tmp_path / name).write_bytes(name.encode("ascii"))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_vector_start": 0,
                        "global_vector_end": 1,
                        "global_event_start": 0,
                        "global_event_end": 3,
                        "final_hold_state": {"drive_value": "0x2", "drive_enable": "0x2"},
                    },
                    {
                        "path": "b.bin",
                        "global_vector_start": 2,
                        "global_vector_end": 3,
                        "global_event_start": 4,
                        "global_event_end": 7,
                        "entry_hold_state": {"drive_value": "0x2", "drive_enable": "0x2"},
                        "final_hold_state": {"drive_value": "0x6", "drive_enable": "0x6"},
                    },
                    {
                        "path": "c.bin",
                        "global_vector_start": 4,
                        "global_vector_end": 5,
                        "global_event_start": 8,
                        "global_event_end": 11,
                        "entry_hold_state": {"drive_value": "0x6", "drive_enable": "0x6"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)

    def status(state: int, *, fail_count: int = 0, sample_count: int = 0):
        return SimpleNamespace(
            state=state,
            error_code=0,
            pattern_size=1,
            event_count=4,
            run_progress=4,
            result_flags=0,
            fail_count=fail_count,
            sample_count=sample_count,
        )

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []
            self.statuses = [
                status(STATE_HOLD_AWAITING_NEXT),
                status(STATE_HOLD_AWAITING_NEXT),
                status(STATE_DONE),
            ]
            self.result_index = 0
            self.current_segment = 0
            self.pending_fail = False

        def upload(self, payload: bytes, *, storage_target: int, chunk_size: int) -> None:
            self.calls.append(("upload", payload, storage_target, chunk_size))

        def run_stream_segment_hold(self):
            self.calls.append(("run_hold",))

        def run_stream_resume_from_hold(self, drive_value: int, drive_enable: int, *, hold_after: bool = False):
            self.calls.append(("run_resume", drive_value, drive_enable, hold_after))

        def status(self):
            return self.statuses.pop(0)

        def sample_info(self):
            self.current_segment = self.result_index
            self.calls.append(("sample_info", self.result_index))
            return status(STATE_DONE, sample_count=1)

        def fail_summary(self):
            self.calls.append(("fail_summary", self.current_segment))
            self.pending_fail = self.current_segment == 0
            if not self.pending_fail:
                self.result_index += 1
            return status(STATE_DONE, fail_count=1 if self.pending_fail else 0)

        def read_samples(self, offset: int, length: int):
            self.calls.append(("read_samples", self.current_segment, offset, length))
            return bytes((self.current_segment + 1, 0, 0, 0))

        def read_fail_log(self, offset: int, count: int):
            self.calls.append(("read_fail_log", self.current_segment, offset, count))
            self.result_index += 1
            self.pending_fail = False
            return [SimpleNamespace(event_index=2, actual=0x10, expected=0x20, mask=0x30)]

    client = FakeClient()
    results, final_result = run_segmented_rate_vector_compare(
        client, manifest, device_capability=0, timeout_s=0.1
    )

    assert final_result is results[-1]
    assert [result["status"]["state"] for result in results] == [
        STATE_HOLD_AWAITING_NEXT,
        STATE_HOLD_AWAITING_NEXT,
        STATE_DONE,
    ]
    assert [result["sample_count"] for result in results] == [1, 1, 1]
    assert results[0]["entries"][0]["event_index"] == 2
    assert results[1]["entries"] == []
    assert results[2]["entries"] == []
    assert results[0]["sample_element_size_bytes"] == 4
    assert results[0]["stored_fail_entry_count"] == 1
    assert results[0]["fail_log_truncated"] is False
    assert client.calls[:6] == [
        ("upload", b"a.bin", STORAGE_PSRAM, 4096),
        ("run_hold",),
        ("sample_info", 0),
        ("read_samples", 0, 0, 4),
        ("fail_summary", 0),
        ("read_fail_log", 0, 0, 1),
    ]
    assert client.calls[6:12] == [
        ("upload", b"b.bin", STORAGE_PSRAM, 4096),
        ("run_resume", 0x2, 0x2, True),
        ("sample_info", 1),
        ("read_samples", 1, 0, 4),
        ("fail_summary", 1),
        ("upload", b"c.bin", STORAGE_PSRAM, 4096),
    ]
    assert client.calls[12:] == [
        ("run_resume", 0x6, 0x6, False),
        ("sample_info", 2),
        ("read_samples", 2, 0, 4),
        ("fail_summary", 2),
    ]


def test_run_segmented_rate_vector_does_not_read_results_after_error(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"a")
    (tmp_path / "b.bin").write_bytes(b"b")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_event_start": 0,
                        "global_event_end": 1,
                        "final_hold_state": {"drive_value": "0x0", "drive_enable": "0x0"},
                    },
                    {
                        "path": "b.bin",
                        "global_event_start": 2,
                        "global_event_end": 3,
                        "entry_hold_state": {"drive_value": "0x0", "drive_enable": "0x0"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)

    error_status = SimpleNamespace(
        state=STATE_ERROR,
        error_code=50,
        result_flags=0,
        fail_count=0,
        sample_count=0,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def upload(self, *_args, **_kwargs):
            self.calls.append("upload")

        def run_stream_segment_hold(self):
            self.calls.append("run_hold")

        def status(self):
            self.calls.append("status")
            return error_status

        def sample_info(self):
            raise AssertionError("sample_info must not follow a terminal error")

        def fail_summary(self):
            raise AssertionError("fail_summary must not follow a terminal error")

        def read_samples(self, *_args):
            raise AssertionError("read_samples must not follow a terminal error")

        def read_fail_log(self, *_args):
            raise AssertionError("read_fail_log must not follow a terminal error")

    client = FakeClient()
    results, final_result = run_segmented_rate_vector_compare(
        client, manifest, device_capability=0, timeout_s=0.1
    )

    assert final_result is results[0]
    assert results[0]["result_reads_performed"] is False
    assert results[0]["result_read_skipped_reason"] == "terminal_error_state"
    assert results[0]["status"]["error_code"] == 50
    assert client.calls == ["upload", "run_hold", "status"]


def test_run_segmented_rate_vector_collects_compare_fail_stop_and_stops_chain(tmp_path: Path) -> None:
    compare_pattern = _single_compare_rate_vector()
    (tmp_path / "a.bin").write_bytes(compare_pattern)
    (tmp_path / "b.bin").write_bytes(compare_pattern)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_event_start": 0,
                        "global_event_end": 1,
                        "expected_sample_count": 3000,
                        "final_hold_state": {"drive_value": "0x0", "drive_enable": "0x0"},
                    },
                    {
                        "path": "b.bin",
                        "global_event_start": 2,
                        "global_event_end": 3,
                        "entry_hold_state": {"drive_value": "0x0", "drive_enable": "0x0"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    terminal_error = SimpleNamespace(
        state=STATE_ERROR,
        error_code=ERR_COMPARE_FAIL_STOP,
        result_flags=0,
        fail_count=1,
        sample_count=241,
        allowed_cmds=ALLOW_RESULT_READ,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def upload(self, payload: bytes, **kwargs: object):
            self.calls.append(("upload", payload, kwargs))
            return SimpleNamespace(error_code=0, pattern_size=len(payload), state=STATE_READY)

        def run_stream_segment_hold(self, **kwargs: object):
            self.calls.append(("run_hold", kwargs))
            return SimpleNamespace(error_code=0)

        def status(self, **kwargs: object):
            self.calls.append(("status", kwargs))
            return terminal_error

        def sample_info(self):
            self.calls.append(("sample_info",))
            return SimpleNamespace(sample_count=241, active_pins=16, result_flags=0)

        def read_samples(self, offset: int, length: int):
            self.calls.append(("read_samples", offset, length))
            return b"\x00" * (241 * 2)

        def fail_summary(self):
            self.calls.append(("fail_summary",))
            return SimpleNamespace(fail_count=1, result_flags=0)

        def read_fail_log(self, offset: int, count: int):
            self.calls.append(("read_fail_log", offset, count))
            return [SimpleNamespace(event_index=1, actual=0, expected=1, mask=0xFFFF)]

    client = FakeClient()
    results, final_result = run_segmented_rate_vector_compare(
        client,
        manifest,
        device_capability=CAP_STREAM_FAIL_STOP,
        timeout_s=0.1,
        fail_stop=True,
        fail_stop_limit=2,
    )

    assert final_result is results[0]
    assert results[0]["result_read_reason"] == "compare_fail_stop"
    assert results[0]["result_reads_performed"] is True
    assert results[0]["fail_count"] == 1
    assert results[0]["sample_count"] == 241
    assert results[0]["entries"][0]["event_index"] == 1
    assert [call[0] for call in client.calls] == [
        "upload",
        "run_hold",
        "status",
        "sample_info",
        "read_samples",
        "fail_summary",
        "read_fail_log",
    ]
    assert client.calls[1][1] == {"fail_stop_limit": 2}


def test_validate_segment_result_counts_keeps_normal_sample_check_and_overflow_guard(
    tmp_path: Path,
) -> None:
    segment = SegmentSpec(
        path=tmp_path / "segment.bin",
        entry_hold_state=HoldState(0, 0),
        final_hold_state=HoldState(0, 0),
        segment_id=0,
        global_vector_start=None,
        global_vector_end=None,
        global_event_start=None,
        global_event_end=None,
        map_path=None,
        expected_event_count=None,
        expected_sample_count=3000,
        expected_compare_count=None,
    )
    sample_info = SimpleNamespace(sample_count=241, result_flags=0)

    with pytest.raises(ValueError, match="sample count 241 != expected 3000"):
        _validate_segment_result_counts(segment, sample_info, SimpleNamespace(result_flags=0))

    with pytest.raises(ValueError, match="sample result overflow"):
        _validate_segment_result_counts(
            segment,
            sample_info,
            SimpleNamespace(result_flags=STPG_RESULT_FLAG_SAMPLE_OVERFLOW),
            allow_partial_samples=True,
        )


def test_run_segmented_rate_vector_forwards_bounded_response_timeouts(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"a")
    (tmp_path / "b.bin").write_bytes(b"b")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_event_start": 0,
                        "global_event_end": 1,
                        "final_hold_state": {"drive_value": "0x0", "drive_enable": "0x0"},
                    },
                    {
                        "path": "b.bin",
                        "global_event_start": 2,
                        "global_event_end": 3,
                        "entry_hold_state": {"drive_value": "0x0", "drive_enable": "0x0"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    statuses = [
        SimpleNamespace(state=STATE_HOLD_AWAITING_NEXT, error_code=0),
        SimpleNamespace(state=STATE_DONE, error_code=0),
    ]

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def upload(self, payload: bytes, **kwargs: object):
            self.calls.append(("upload", payload, kwargs.get("end_response_timeout")))
            return SimpleNamespace(rsp=0x8000, error_code=0, pattern_size=len(payload))

        def run_stream_segment_hold(self, **kwargs: object):
            self.calls.append(("run_hold", kwargs.get("response_timeout")))

        def run_stream_resume_from_hold(self, *_args: object, **kwargs: object):
            self.calls.append(("run_resume", kwargs.get("response_timeout")))

        def status(self, **kwargs: object):
            self.calls.append(("status", kwargs.get("response_timeout")))
            return statuses.pop(0)

        def sample_info(self):
            return SimpleNamespace(sample_count=0, result_flags=0)

        def fail_summary(self):
            return SimpleNamespace(fail_count=0, result_flags=0)

    client = FakeClient()
    run_segmented_rate_vector_compare(
        client,
        manifest,
        device_capability=0,
        timeout_s=10.0,
        upload_end_timeout_s=20.0,
        run_response_timeout_s=21.0,
        status_response_timeout_s=22.0,
    )

    assert client.calls == [
        ("upload", b"a", 20.0),
        ("run_hold", 21.0),
        ("status", 22.0),
        ("upload", b"b", 20.0),
        ("run_resume", 21.0),
        ("status", 22.0),
    ]


def test_merge_segment_results_preserves_overflow_and_global_counts(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_event_start": 0,
                        "global_event_end": 3,
                        "final_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                    {
                        "path": "b.bin",
                        "global_event_start": 4,
                        "global_event_end": 7,
                        "entry_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    merged = merge_segment_results(
        manifest,
        [
            {
                "segment_id": 0,
                "sample_count": 4,
                "fail_count": 400,
                "stored_fail_entry_count": 1,
                "fail_log_truncated": True,
                "fail_log_read_complete": True,
                "result_flags": STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW,
                "entries": [{"event_index": 1, "actual": 2, "expected": 3, "mask": 4}],
            },
            {
                "segment_id": 1,
                "sample_count": 4,
                "fail_count": 0,
                "stored_fail_entry_count": 0,
                "fail_log_truncated": False,
                "fail_log_read_complete": True,
                "result_flags": 0,
                "entries": [],
            },
        ],
    )
    assert merged["global_event_frontier"] == 8
    assert merged["segment_count"] == 2
    assert merged["completed_segment_count"] == 2
    assert merged["global_sample_count"] == 8
    assert merged["global_fail_count"] == 400
    assert merged["global_stored_fail_entry_count"] == 1
    assert merged["global_result_flags"] == STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW
    assert merged["fail_log_complete"] is False
    assert merged["entries"][0]["event_index"] == 1


def test_merge_segment_results_marks_prefix_as_incomplete(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_event_start": 0,
                        "global_event_end": 3,
                        "final_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                    {
                        "path": "b.bin",
                        "global_event_start": 4,
                        "global_event_end": 7,
                        "entry_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    merged = merge_segment_results(
        manifest,
        [
            {
                "segment_id": 0,
                "sample_count": 4,
                "fail_count": 1,
                "stored_fail_entry_count": 1,
                "fail_log_truncated": False,
                "fail_log_read_complete": True,
                "result_flags": 0,
                "entries": [{"event_index": 2, "actual": 0, "expected": 1, "mask": 2}],
            }
        ],
        allow_partial=True,
    )
    assert merged["complete"] is False
    assert merged["segment_count"] == 2
    assert merged["completed_segment_count"] == 1
    assert merged["global_event_frontier"] is None
    assert merged["global_sample_count"] == 4
    assert merged["fail_log_complete"] is False
    assert merged["entries"][0]["event_index"] == 2


def test_merge_segment_results_uses_error_progress_for_frontier(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_event_start": 0,
                        "global_event_end": 3,
                        "final_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                    {
                        "path": "b.bin",
                        "global_event_start": 4,
                        "global_event_end": 7,
                        "entry_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    merged = merge_segment_results(
        manifest,
        [
            {
                "segment_id": 0,
                "sample_count": 4,
                "fail_count": 0,
                "stored_fail_entry_count": 0,
                "fail_log_truncated": False,
                "fail_log_read_complete": True,
                "result_flags": 0,
                "entries": [],
                "status": {"state": STATE_DONE, "error_code": 0, "run_progress": 4},
            },
            {
                "segment_id": 1,
                "sample_count": 0,
                "fail_count": 0,
                "stored_fail_entry_count": 0,
                "fail_log_truncated": False,
                "fail_log_read_complete": False,
                "result_flags": 0,
                "entries": [],
                "result_reads_performed": False,
                "status": {"state": STATE_ERROR, "error_code": 50, "run_progress": 2},
            },
        ],
    )
    assert merged["complete"] is False
    assert merged["global_event_frontier"] == 6
    assert merged["fail_log_complete"] is False


def test_collect_segment_results_bounds_overflowed_fail_log(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"a")
    (tmp_path / "b.bin").write_bytes(b"b")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_event_start": 0,
                        "global_event_end": 3,
                        "estimated_sample_count": 4,
                        "final_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                    {
                        "path": "b.bin",
                        "global_event_start": 4,
                        "global_event_end": 7,
                        "entry_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    segment = load_manifest(manifest_path).segments[0]
    sample_info = SimpleNamespace(
        state=STATE_HOLD_AWAITING_NEXT,
        active_pins=24,
        sample_count=4,
        result_flags=0,
    )
    fail_summary = SimpleNamespace(
        state=STATE_HOLD_AWAITING_NEXT,
        fail_count=400,
        result_flags=STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW,
    )

    class FakeClient:
        def sample_info(self):
            return sample_info

        def read_samples(self, offset: int, length: int):
            assert (offset, length) == (0, 16)
            return b"\x00" * length

        def fail_summary(self):
            return fail_summary

        def read_fail_log(self, offset: int, count: int):
            assert (offset, count) == (0, 341)
            return [SimpleNamespace(event_index=index, actual=0, expected=1, mask=1) for index in range(count)]

    result = _collect_segment_results(
        FakeClient(),
        segment,
        SimpleNamespace(state=STATE_HOLD_AWAITING_NEXT, allowed_cmds=ALLOW_RESULT_READ),
    )
    assert result["sample_count"] == 4
    assert result["sample_element_size_bytes"] == 4
    assert result["fail_count"] == 400
    assert result["stored_fail_entry_count"] == 341
    assert result["fail_log_truncated"] is True
    assert result["result_flags"] == STPG_RESULT_FLAG_FAIL_LOG_OVERFLOW


def test_globalize_fail_entries_uses_manifest_offset(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_vector_start": 10,
                        "global_vector_end": 11,
                        "final_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                    {
                        "path": "b.bin",
                        "global_vector_start": 12,
                        "global_vector_end": 12,
                        "entry_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    entries = [{"vector_index": 0, "fail_bits": 2}]
    out = globalize_fail_entries(entries, manifest.segments[1])
    assert out == [{"vector_index": 12, "local_vector_index": 0, "fail_bits": 2, "segment_id": 1}]


def test_merge_segment_maps_rewrites_global_vector_indices(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_vector_start": 100,
                        "global_vector_end": 101,
                        "final_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                    {
                        "path": "b.bin",
                        "global_vector_start": 102,
                        "global_vector_end": 102,
                        "entry_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    map0 = {
        "signal_map": {"A": {"bit": 1}},
        "groups": {},
        "vectors": {
            "0": {"source_map": [{"global_vector_index": 0, "stil_line": 10}]},
            "1": {"source_map": [{"global_vector_index": 1, "stil_line": 11}]},
        },
    }
    map1 = {
        "signal_map": {"A": {"bit": 1}},
        "groups": {},
        "vectors": {
            "0": {"source_map": [{"global_vector_index": 0, "stil_line": 12}]},
        },
    }
    merged = merge_segment_maps(manifest, [map0, map1])
    assert merged["segmented"] is True
    assert sorted(merged["vectors"].keys()) == ["100", "101", "102"]
    assert merged["vectors"]["100"]["source_map"][0]["local_vector_index"] == 0
    assert merged["vectors"]["100"]["source_map"][0]["segment_id"] == 0
    assert merged["vectors"]["102"]["source_map"][0]["segment_id"] == 1


def test_merge_segment_fail_json_globalizes_all_segments(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "global_vector_start": 10,
                        "global_vector_end": 11,
                        "final_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                    {
                        "path": "b.bin",
                        "global_vector_start": 12,
                        "global_vector_end": 13,
                        "entry_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    merged = merge_segment_fail_json(
        manifest,
        [
            {"entries": [{"vector_index": 1, "fail_bits": 1}]},
            {"entries": [{"vector_index": 0, "fail_bits": 2}]},
        ],
    )
    assert merged["segment_count"] == 2
    assert merged["entries"] == [
        {"vector_index": 11, "local_vector_index": 1, "fail_bits": 1, "segment_id": 0},
        {"vector_index": 12, "local_vector_index": 0, "fail_bits": 2, "segment_id": 1},
    ]


def test_load_segment_map_payloads_and_build_artifacts(tmp_path: Path) -> None:
    map0 = tmp_path / "seg0.map.json"
    map1 = tmp_path / "seg1.map.json"
    map0.write_text(
        json.dumps({"signal_map": {"A": {"bit": 1}}, "groups": {}, "vectors": {"0": {"source_map": [{"global_vector_index": 0, "stil_line": 10}]}}}),
        encoding="utf-8",
    )
    map1.write_text(
        json.dumps({"signal_map": {"A": {"bit": 1}}, "groups": {}, "vectors": {"0": {"source_map": [{"global_vector_index": 0, "stil_line": 11}]}}}),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "a.bin",
                        "map_path": map0.name,
                        "global_vector_start": 20,
                        "global_vector_end": 20,
                        "final_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                    {
                        "path": "b.bin",
                        "map_path": map1.name,
                        "global_vector_start": 21,
                        "global_vector_end": 21,
                        "entry_hold_state": {"drive_value": "0x1", "drive_enable": "0x1"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    payloads = load_segment_map_payloads(manifest)
    artifacts = build_segmented_artifacts(manifest, map_payloads=payloads)
    assert artifacts.merged_map is not None
    assert sorted(artifacts.merged_map["vectors"].keys()) == ["20", "21"]
    assert artifacts.merged_fail_json["entries"] == []
