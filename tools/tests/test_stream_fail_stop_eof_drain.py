from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER_SOURCE = ROOT / "firmware" / "src" / "stpg_runner.c"


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function: {signature}")


def test_streaming_eof_drains_final_fail_stop_metadata_after_hardware_quiescence() -> None:
    source = RUNNER_SOURCE.read_text(encoding="utf-8")
    finish = _function_body(
        source,
        "static void stpg_runner_finish_streaming_success(void)",
    )

    quiesce = finish.index("stpg_runner_finish_hw_hold_outputs();")
    capture = finish.index(
        "const uint32_t captured_sample_count = stpg_runner_captured_sample_count();"
    )
    final_drain = finish.index(
        "stpg_runner_check_stream_fail_stop_samples(&fail_event_index);"
    )
    integrity_check = finish.index(
        "metadata_sample_count != expected_sample_count"
    )

    assert quiesce < capture < final_drain < integrity_check
    assert finish.count(
        "stpg_runner_check_stream_fail_stop_samples(&fail_event_index);"
    ) == 1


def test_streaming_eof_preserves_compare_fail_and_metadata_error_boundaries() -> None:
    source = RUNNER_SOURCE.read_text(encoding="utf-8")
    finish = _function_body(
        source,
        "static void stpg_runner_finish_streaming_success(void)",
    )

    compare_branch = """if (fail_stop_err == ERR_COMPARE_FAIL_STOP) {
            stpg_runner_finish_streaming_fail_stop(
                ERR_COMPARE_FAIL_STOP,
                fail_event_index + 1u);"""
    other_error_branch = """if (fail_stop_err != ERR_NONE) {
            stpg_runner_finish_streaming_fail_stop(
                fail_stop_err,
                g_runner.fail_stop_next_event_index);"""

    assert compare_branch in finish
    assert other_error_branch in finish
    assert "ERR_FAIL_STOP_METADATA_OVERRUN" in finish
    assert finish.index(compare_branch) < finish.index("ERR_FAIL_STOP_METADATA_OVERRUN")
