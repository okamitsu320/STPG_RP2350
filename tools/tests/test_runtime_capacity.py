from types import SimpleNamespace

from stpg.constants import (
    CAP_PRACTICAL_STREAMING,
    CAP_STREAM_FAIL_STOP,
    STPG_PSRAM_PATTERN_BASE,
    STPG_PSRAM_PATTERN_BYTES,
    STPG_PSRAM_RESULT_BASE,
    STPG_PSRAM_RESULT_BYTES,
    STPG_PSRAM_STAGING_BASE,
    STPG_PSRAM_STAGING_BYTES,
)
from stil.runtime_capacity import (
    DEFAULT_RUNTIME_PROFILE,
    STREAMING_FAIL_STOP_SRAM_PROFILE,
    assess_runtime_capacity,
    get_runtime_capacity_profile,
    runtime_capacity_observation,
)


def _requirements(
    *,
    events: int,
    sample: bool = False,
    compare: bool = False,
    sample_count: int | None = None,
    compare_count: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        mode="rate-vector",
        estimated_event_count=events,
        requires_sample=sample,
        requires_compare=compare,
        estimated_sample_count=sample_count,
        estimated_compare_count=compare_count,
    )


def test_psram_capacity_profile_accepts_large_compare_free_stream_within_byte_region() -> None:
    errors = assess_runtime_capacity(
        _requirements(events=473_136),
        pattern_size_bytes=2_219_060,
        profile=get_runtime_capacity_profile("psram-capacity-compare-free"),
    )

    assert errors == []


def test_psram_capacity_profile_rejects_sample_and_compare_streams() -> None:
    errors = assess_runtime_capacity(
        _requirements(events=473_136, sample=True, compare=True),
        pattern_size_bytes=2_219_060,
        profile=get_runtime_capacity_profile("psram-capacity-compare-free"),
    )

    assert {item["code"] for item in errors} == {
        "ERR_RUNTIME_STREAM_SAMPLE_LIMIT",
        "ERR_RUNTIME_STREAM_COMPARE_LIMIT",
        "ERR_RUNTIME_STREAM_COMPARE_FREE_ONLY",
    }


def test_psram_pattern_region_limit_is_independent_of_event_limit() -> None:
    errors = assess_runtime_capacity(
        _requirements(events=32),
        pattern_size_bytes=get_runtime_capacity_profile("current").psram_pattern_bytes + 1,
        profile=get_runtime_capacity_profile("current"),
    )

    assert [item["code"] for item in errors] == ["ERR_PSRAM_PATTERN_BYTES_LIMIT"]


def test_psram_compare_sample_profile_accepts_exact_large_sample_and_compare_counts() -> None:
    errors = assess_runtime_capacity(
        _requirements(
            events=473_136,
            sample=True,
            compare=True,
            sample_count=120_846,
            compare_count=110_538,
        ),
        pattern_size_bytes=2_219_060,
        profile=get_runtime_capacity_profile("psram-compare-sample-streaming"),
    )

    assert errors == []


def test_default_profile_is_compare_sample_capable_for_large_rate_vector() -> None:
    assert DEFAULT_RUNTIME_PROFILE == "psram-compare-sample-streaming-practical-5m2m"
    errors = assess_runtime_capacity(
        _requirements(
            events=473_136,
            sample=True,
            compare=True,
            sample_count=120_846,
            compare_count=110_538,
        ),
        pattern_size_bytes=2_219_060,
        profile=get_runtime_capacity_profile(),
    )

    assert errors == []


def test_practical_profile_declares_a_distinct_device_capability() -> None:
    profile = get_runtime_capacity_profile()
    assert profile.required_capability == CAP_PRACTICAL_STREAMING
    assert get_runtime_capacity_profile("psram-compare-sample-streaming").required_capability == 0


def test_streaming_fail_stop_profile_binds_sram_contract_and_event_limit() -> None:
    profile = get_runtime_capacity_profile(STREAMING_FAIL_STOP_SRAM_PROFILE)
    assert profile.required_capability == CAP_STREAM_FAIL_STOP
    assert profile.full_expansion_event_limit == 6551
    assert profile.streaming_event_limit == 0xFFFFFFFF
    assert profile.streaming_sample_limit == 6551
    errors = assess_runtime_capacity(
        _requirements(events=131072, sample=True, compare=True, sample_count=4096, compare_count=4096),
        pattern_size_bytes=492,
        profile=profile,
    )
    assert errors == []

    sample_overflow = assess_runtime_capacity(
        _requirements(events=131072, sample=True, compare=True, sample_count=6552, compare_count=6552),
        pattern_size_bytes=492,
        profile=profile,
    )
    assert {item["code"] for item in sample_overflow} == {
        "ERR_RUNTIME_STREAM_SAMPLE_LIMIT",
    }

    event_overflow = assess_runtime_capacity(
        _requirements(events=0x1_0000_0000),
        pattern_size_bytes=492,
        profile=profile,
    )
    assert [item["code"] for item in event_overflow] == ["ERR_RUNTIME_STREAM_EVENT_LIMIT"]


def test_default_psram_partition_contract_is_current_5m2m() -> None:
    profile = get_runtime_capacity_profile()
    assert profile.psram_pattern_base == STPG_PSRAM_PATTERN_BASE
    assert profile.psram_pattern_bytes == STPG_PSRAM_PATTERN_BYTES
    assert profile.psram_result_base == STPG_PSRAM_RESULT_BASE
    assert profile.psram_result_bytes == STPG_PSRAM_RESULT_BYTES
    assert profile.psram_staging_base == STPG_PSRAM_STAGING_BASE
    assert profile.psram_staging_bytes == STPG_PSRAM_STAGING_BYTES


def test_5m2m_profile_declares_address_boundary_and_double_sample_capacity() -> None:
    profile = get_runtime_capacity_profile("psram-compare-sample-streaming-practical-5m2m")
    assert profile.psram_pattern_base == 0x010000
    assert profile.psram_pattern_bytes == 0x4F0000
    assert profile.psram_result_base == 0x500000
    assert profile.psram_result_bytes == 0x200000
    assert profile.psram_staging_base == 0x700000
    assert profile.psram_staging_bytes == 0x100000
    assert profile.streaming_sample_limit == 524_288


def test_5m2m_profile_accepts_large_pattern_and_sample_count() -> None:
    errors = assess_runtime_capacity(
        _requirements(
            events=1_083_380,
            sample=True,
            compare=True,
            sample_count=280_068,
            compare_count=243_070,
        ),
        pattern_size_bytes=4_890_376,
        profile=get_runtime_capacity_profile("psram-compare-sample-streaming-practical-5m2m"),
    )

    assert errors == []


def test_5m2m_profile_rejects_pattern_or_sample_overflow() -> None:
    profile = get_runtime_capacity_profile("psram-compare-sample-streaming-practical-5m2m")
    errors = assess_runtime_capacity(
        _requirements(
            events=1_083_380,
            sample=True,
            compare=True,
            sample_count=524_289,
            compare_count=243_070,
        ),
        pattern_size_bytes=0x4F0001,
        profile=profile,
    )

    assert {item["code"] for item in errors} == {
        "ERR_PSRAM_PATTERN_BYTES_LIMIT",
        "ERR_RUNTIME_STREAM_SAMPLE_LIMIT",
    }


def test_psram_compare_sample_profile_rejects_sample_result_overflow() -> None:
    errors = assess_runtime_capacity(
        _requirements(
            events=473_136,
            sample=True,
            compare=True,
            sample_count=262_145,
            compare_count=110_538,
        ),
        pattern_size_bytes=2_219_060,
        profile=get_runtime_capacity_profile("psram-compare-sample-streaming"),
    )

    assert [item["code"] for item in errors] == ["ERR_RUNTIME_STREAM_SAMPLE_LIMIT"]


def test_capacity_observation_explains_sample_boundary_and_unbounded_compare() -> None:
    observation = runtime_capacity_observation(
        _requirements(
            events=473_136,
            sample=True,
            compare=True,
            sample_count=262_144,
            compare_count=233_313,
        ),
        pattern_size_bytes=4_690_376,
        profile=get_runtime_capacity_profile("psram-compare-sample-streaming"),
    )

    assert observation["status"] == "REVIEW"
    assert observation["boundary_metrics"] == ["sample_count"]
    assert observation["metrics"]["sample_count"] == {
        "value": 262_144,
        "limit": 262_144,
        "remaining": 0,
        "utilization_percent": 100.0,
        "status": "at_limit",
        "scope": "streaming_sample_result_region",
    }
    assert observation["metrics"]["compare_count"]["status"] == "not_bounded"
    assert observation["result_reporting"]["mismatch_log"] == {
        "possible_mismatch_entries": 233_313,
        "capacity": 341,
        "remaining_by_count": -232_972,
        "status": "may_overflow_if_mismatches",
        "scope": "stored_fail_log",
    }
    assert "sample words" in observation["definitions"]["sample_count"]
    assert "expect mask" in observation["definitions"]["compare_count"]


def test_capacity_observation_reports_sample_overflow_before_physical_use() -> None:
    observation = runtime_capacity_observation(
        _requirements(
            events=473_136,
            sample=True,
            compare=True,
            sample_count=262_145,
            compare_count=233_313,
        ),
        pattern_size_bytes=4_690_376,
        profile=get_runtime_capacity_profile("psram-compare-sample-streaming"),
    )

    assert observation["status"] == "REJECT"
    assert observation["blocking_codes"] == ["ERR_RUNTIME_STREAM_SAMPLE_LIMIT"]
    assert observation["metrics"]["sample_count"]["status"] == "exceeds_limit"
    assert observation["metrics"]["sample_count"]["remaining"] == -1
    assert "result-storage" in observation["next_action"]


def test_psram_compare_sample_profile_rejects_unsupported_oversized_macro_stream() -> None:
    requirements = _requirements(events=473_136, sample=True, compare=True)
    requirements.mode = "macro-stream"

    errors = assess_runtime_capacity(
        requirements,
        pattern_size_bytes=2_219_060,
        profile=get_runtime_capacity_profile("psram-compare-sample-streaming"),
    )

    assert [item["code"] for item in errors] == [
        "ERR_RUNTIME_DIRECT_BYTES_LIMIT",
        "ERR_RUNTIME_STREAM_MODE_UNSUPPORTED",
    ]


def test_practical_profile_rejects_macro_payload_beyond_sram_materialization_limit() -> None:
    requirements = _requirements(events=32)
    requirements.mode = "macro-stream"

    errors = assess_runtime_capacity(
        requirements,
        pattern_size_bytes=128 * 1024 + 1,
        profile=get_runtime_capacity_profile(),
    )

    assert [item["code"] for item in errors] == ["ERR_RUNTIME_DIRECT_BYTES_LIMIT"]


def test_f57_profile_keeps_bounded_streaming_event_limit() -> None:
    errors = assess_runtime_capacity(
        _requirements(events=40_961),
        pattern_size_bytes=128_000,
        profile=get_runtime_capacity_profile("f57-ring6"),
    )

    assert [item["code"] for item in errors] == ["ERR_RUNTIME_STREAM_EVENT_LIMIT"]
