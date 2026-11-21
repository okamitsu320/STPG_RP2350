"""Local SDK/install diagnostics. No project, port or device is inspected."""
from pathlib import Path
import platform
import sys

from . import build

_IMPORT_TOOL_SHA256 = build.tool_fingerprint()


def sdk_info() -> dict:
    installed = build.tool_fingerprint()
    return {
        "kind": "stpg_runner_sdk_info", "schema_version": 1, "status": "SDK_INFO",
        "tool_sha256": installed, "import_tool_sha256": _IMPORT_TOOL_SHA256,
        "code_changed_since_import": installed != _IMPORT_TOOL_SHA256,
        "python": platform.python_version(), "python_executable": sys.executable,
        "sdk_directory": str(Path(build.__file__).resolve().parent),
        "identity_scope": "installed files now and at SDK import; not an attestation of every loaded module",
        "device_io_performed": False, "physical_execution_authorized": False,
    }
