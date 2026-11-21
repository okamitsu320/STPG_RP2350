"""Selected source-level USB contracts, not physical USB scheduling tests."""
import re
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[2] / "firmware/src/usb_protocol.c"


def test_protocol_response_waits_for_ready_not_dtr():
    source = SOURCE.read_text(encoding="utf-8")
    helper = source.split("static bool stpg_usb_wait_for_cdc_ready", 1)[1].split(
        "static void stpg_usb_write_bytes", 1)[0]
    body = source.split("static void stpg_usb_write_bytes(const uint8_t *buf, uint32_t len) {", 1)[1].split(
        "static void stpg_usb_send_read_payload", 1)[0]
    assert "tud_cdc_n_ready(0)" in helper
    assert "STPG_USB_CDC_READY_WAIT_US" in helper
    assert "tud_cdc_connected" not in re.sub(r"//[^\n]*", "", helper + body)
    assert "!stpg_usb_wait_for_cdc_ready()" in body


def test_usb_diagnostics_keep_receive_dispatch_and_response_boundaries():
    source = SOURCE.read_text(encoding="utf-8")
    for statement in ("g_usb_available_poll_count_raw += 1u", "g_usb_rx_read_call_count_raw += 1u",
                      "g_usb_rx_byte_count_raw += count", "g_usb_frame_complete_count_raw += 1u",
                      "g_usb_dispatch_call_count_raw += 1u", "g_usb_write_response_count_raw += 1u"):
        assert statement in source


def test_payload_error_retains_its_error_code():
    assert "g_usb_last_payload_error_code_raw = (uint32_t)err" in SOURCE.read_text(encoding="utf-8")
