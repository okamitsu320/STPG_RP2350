import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "firmware/src/command_dispatch.c"
RUNNER = ROOT / "firmware/src/stpg_runner.c"
USB = ROOT / "firmware/src/usb_protocol.c"


def _function(source: str, signature: str, next_signature: str) -> str:
    return source.split(signature, 1)[1].split(next_signature, 1)[0]


def test_all_non_measurement_run_modes_use_the_deferred_start_path() -> None:
    source = DISPATCH.read_text(encoding="utf-8")
    run_control = _function(
        source,
        "static void dispatch_run_control",
        "static void dispatch_result_read",
    )
    assert "const bool deferred_run = (cmd->arg0 & measurement_flags) == 0u" in run_control
    assert "stpg_runner_request_start(" in run_control
    assert "cmd->arg1 == 0u" not in run_control
    assert "cmd->arg2 == 0u" not in run_control
    assert "cmd->arg3 == 0u" not in run_control


def test_deferred_start_validates_fail_stop_and_streaming_before_ack() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    validator = _function(
        source,
        "static stpg_err_t stpg_runner_validate_pattern_start",
        "stpg_err_t stpg_runner_start(",
    )
    request = _function(
        source,
        "stpg_err_t stpg_runner_request_start",
        "bool stpg_runner_start_pending",
    )
    for token in (
        "stpg_fail_stop_resolve_limit",
        "STPG_RUN_FLAG_STREAMING_POC",
        "STPG_RUN_FLAG_FAIL_STOP",
        "STPG_PATTERN_FORMAT_RATE_VECTOR",
        "STPG_PATTERN_FORMAT_RAW_EVENT",
        "ERR_HOLD_STATE_MISMATCH",
    ):
        assert token in validator
    assert "stpg_runner_ensure_initialized()" in request
    assert "stpg_runner_validate_pattern_start(" in request
    assert request.index("stpg_runner_validate_pattern_start(") < request.index(
        "g_runner.pending_start = true"
    )


def test_run_starts_only_after_the_64_byte_response_transfer_completes() -> None:
    usb = USB.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")
    handle_frame = _function(
        usb,
        "static void stpg_usb_handle_frame",
        "static void stpg_usb_accept_byte",
    )
    task = _function(
        runner,
        "void stpg_runner_task(void)",
        "static stpg_err_t stpg_runner_start_streaming",
    )
    assert "tud_cdc_tx_complete_cb" in usb
    assert "g_run_response_pending = false" in usb
    assert "stpg_runner_start_pending()" in handle_frame
    assert handle_frame.index("stpg_usb_run_response_begin()") < handle_frame.index(
        "stpg_usb_write_response(&rsp)"
    )
    assert "if (stpg_usb_run_response_pending())" in task
    assert task.index("if (stpg_usb_run_response_pending())") < task.index(
        "stpg_runner_start("
    )


def test_usb_response_readiness_is_not_dtr_and_init_has_bounded_reenumeration() -> None:
    source = USB.read_text(encoding="utf-8")
    write_path = _function(
        source,
        "static bool stpg_usb_wait_for_cdc_ready",
        "static inline void stpg_usb_note_tud_task_call",
    )
    init = _function(
        source,
        "void stpg_usb_protocol_init(void)",
        "static void stpg_usb_reset_rx",
    )
    assert "tud_cdc_n_ready(0)" in write_path
    assert "tud_cdc_connected" not in re.sub(r"//[^\n]*", "", write_path)
    assert "tusb_init();" in init
    assert init.count("tud_disconnect();") == 1
    assert init.count("sleep_ms(100);") == 1
    assert init.count("tud_connect();") == 1
    assert init.index("tusb_init();") < init.index("tud_disconnect();")
    assert init.index("tud_disconnect();") < init.index("sleep_ms(100);")
    assert init.index("sleep_ms(100);") < init.index("tud_connect();")
    assert "tusb_rhport_init" not in init
