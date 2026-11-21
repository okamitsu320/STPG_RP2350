from pathlib import Path
import json


ROOT = Path(__file__).resolve().parents[1]


def test_standard_build_selects_the_practical_profile() -> None:
    # The build is selected by the CMake preset; no separate build script takes part.
    preset = json.loads((ROOT.parent / "firmware/CMakePresets.json").read_text(encoding="utf-8"))
    profile = preset["configurePresets"][0]["cacheVariables"]
    cmake = (ROOT.parent / "firmware/CMakeLists.txt").read_text(encoding="utf-8")
    assert profile["STPG_STREAM_PRACTICAL_PROFILE"] == "ON"
    assert profile["STPG_STREAM_HOT_TELEMETRY"] == "OFF"
    assert "if(STPG_STREAM_PRACTICAL_PROFILE)" in cmake
    assert 'set(STPG_STREAM_PRACTICAL_PROFILE ON CACHE BOOL' in cmake
    assert 'set(STPG_STREAM_HOT_TELEMETRY OFF CACHE BOOL' in cmake
    for option in (
        "STPG_STREAM_PARTIAL_REPEAT_FAST_PATH",
        "STPG_STREAM_COMPARE_FREE_BLOCK_FILL",
        "STPG_STREAM_PRODUCER_SRAM",
        "STPG_STREAM_REPEAT_BLOCK_FILL",
        "STPG_STREAM_REPEAT_BOUNDARY_FILL",
        "STPG_STREAM_REPEAT_BOUNDARY_CONTINUE",
        "STPG_STREAM_REPEAT_BOUNDARY_REARM",
        "STPG_STREAM_COMPARE_STREAMING",
        "STPG_STREAM_PSRAM_COMPARE_SAMPLE",
        "STPG_STREAM_FIFO_FILL_REQUESTS",
    ):
        assert f"set({option} ON CACHE BOOL" in cmake


def test_repeat_fast_path_is_not_disabled_by_hot_telemetry() -> None:
    source = (ROOT.parent / "firmware/src/pattern_buffer.c").read_text(encoding="utf-8")
    runner = (ROOT.parent / "firmware/src/stpg_runner.c").read_text(encoding="utf-8")
    assert "#if STPG_STREAM_REPEAT_BLOCK_FILL && !STPG_STREAM_HOT_TELEMETRY" not in source
    assert "#if STPG_STREAM_REPEAT_BOUNDARY_FILL && !STPG_STREAM_HOT_TELEMETRY" not in source
    assert "#if STPG_STREAM_REPEAT_BOUNDARY_REARM && STPG_STREAM_REPEAT_BLOCK_FILL && !STPG_STREAM_HOT_TELEMETRY" not in source
    assert "#if STPG_STREAM_PRODUCER_SRAM && defined(PICO_ON_DEVICE)" in source
    assert "#if STPG_STREAM_PRODUCER_SRAM && defined(PICO_ON_DEVICE)" in runner


def test_standard_profile_keeps_generic_fallback_explicit() -> None:
    source = (ROOT.parent / "firmware/src/pattern_buffer.c").read_text(encoding="utf-8")
    assert "stpg_buffer_stream_repeat_block_shape_eligible" in source
    assert "runtime->group_count != 2u" in source
    assert "vector->repeat <= state->repeat_index" in source
    assert "for (uint32_t repeat_index = repeat_start;" in source


def test_partial_repeat_option_is_value_guarded() -> None:
    source = (ROOT.parent / "firmware/src/pattern_buffer.c").read_text(encoding="utf-8")
    assert "#ifdef STPG_STREAM_PARTIAL_REPEAT_FAST_PATH" not in source
    assert source.count("#if STPG_STREAM_PARTIAL_REPEAT_FAST_PATH") == 2


def test_host_practical_target_exercises_the_full_profile() -> None:
    cmake = (ROOT.parent / "firmware/host_test/CMakeLists.txt").read_text(encoding="utf-8")
    assert "add_executable(test_rate_vector_practical" in cmake
    assert "add_test(NAME test_rate_vector_practical COMMAND test_rate_vector_practical)" in cmake
    assert "add_executable(test_rate_vector_practical_hot_off" in cmake
    assert "add_test(NAME test_rate_vector_practical_hot_off COMMAND test_rate_vector_practical_hot_off)" in cmake
    for definition in (
        "STPG_STREAM_PRACTICAL_PROFILE=1",
        "STPG_STREAM_PARTIAL_REPEAT_FAST_PATH=1",
        "STPG_STREAM_HOT_TELEMETRY=1",
        "STPG_STREAM_COMPARE_FREE_BLOCK_FILL=1",
        "STPG_STREAM_PRODUCER_SRAM=1",
        "STPG_STREAM_REPEAT_BLOCK_FILL=1",
        "STPG_STREAM_REPEAT_BOUNDARY_FILL=1",
        "STPG_STREAM_COMPARE_STREAMING=1",
        "STPG_STREAM_PSRAM_COMPARE_SAMPLE=1",
        "STPG_STREAM_FIFO_FILL_REQUESTS=1",
        "STPG_STREAM_PRECOMPUTED_UOP_CAPACITY=32",
    ):
        assert definition in cmake
    assert "STPG_STREAM_HOT_TELEMETRY=0" in cmake
