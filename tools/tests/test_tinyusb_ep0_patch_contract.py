"""Static EP0 invariants of the selected cumulative patch; no private snapshot.

This does not apply the patch, verify a configured SDK or execute the upstream
driver. Those are separate dependency/build acceptance checks.
"""
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def inactive_hunk():
    lock = json.loads((ROOT / "firmware/dependencies/tinyusb-build-lock.json").read_text(encoding="utf-8"))
    raw = (ROOT / lock["tinyusb_patch"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == lock["tinyusb_patch_sha256"]
    driver = raw.decode().split("diff --git a/src/device/usbd.c", 1)[0]
    hunk = driver.split("bool __tusb_irq_path_func(hw_endpoint_xfer_continue)", 1)[1]
    # New-side context/additions only. Removed text cannot satisfy an assertion.
    return "\n".join(line[1:] for line in hunk.splitlines() if line.startswith((" ", "+")))


def test_stale_ep0_branch_consumes_without_a_completion_notification():
    body = inactive_hunk()
    ep0 = body.split("if (tu_edpt_number(ep->ep_addr) == 0u)", 1)[1].split("}", 1)[0]
    assert ep0.index("hw_endpoint_lock_update(ep, -1);") < ep0.index("return false;")
    assert "dcd_event_xfer_complete" not in ep0


def test_inactive_non_control_endpoint_still_panics():
    body = inactive_hunk()
    assert body.count('panic("Can\'t continue xfer on inactive ep %02X"') == 1
    assert body.index("return false;") < body.index('panic("Can\'t continue xfer on inactive ep %02X"')
