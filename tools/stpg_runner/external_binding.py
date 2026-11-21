"""USB lease identity only; no analyzer protocol, limits, driver or device I/O."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .artifacts import RunnerError
from .device import DeviceConfig, physical_identity_key


@dataclass(frozen=True)
class ExternalDeviceBinding:
    kind: str
    port: str
    vid: int
    pid: int
    serial_number: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExternalDeviceBinding":
        # Legacy analyzer bindings remain usable for target leasing/reset. Other
        # fields are opaque to the core and are NOT firmware/protocol validation.
        if (not isinstance(value, dict) or not isinstance(value.get("kind"), str) or value.get("kind") not in
                {"stpg_external_device", "stpg_capture_analyzer"}
                or type(value.get("schema_version")) is not int or value["schema_version"] != 1):
            raise RunnerError("unsupported external USB binding kind/schema")
        fields = ("kind", "port", "vid", "pid", "serial_number")
        if any(field not in value for field in fields):
            raise RunnerError("external USB binding is missing identity fields")
        port = value["port"]
        if (not isinstance(port, str) or not port.strip() or port != port.strip()
                or len(port) > 4096 or any(ord(c) < 32 for c in port)):
            raise RunnerError("external USB binding requires an exact nonempty port")
        result = cls(**{field: value[field] for field in fields})
        physical_identity_key(result)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "schema_version": 1, "port": self.port,
                "vid": self.vid, "pid": self.pid, "serial_number": self.serial_number}


def target_binding(value: dict[str, Any]) -> DeviceConfig | ExternalDeviceBinding:
    if isinstance(value, dict) and value.get("kind") == "stpg_runner_device":
        return DeviceConfig.from_dict(value)
    return ExternalDeviceBinding.from_dict(value)
