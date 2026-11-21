"""Local device bindings and lazy serial access. Importing performs no I/O."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any, Protocol

from .artifacts import RunnerError, read_json

SERIAL_READ_TIMEOUT_S = 0.05


@dataclass(frozen=True)
class DeviceConfig:
    port: str
    vid: int
    pid: int
    serial_number: str
    firmware_version: int
    capability: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeviceConfig":
        fields = {"kind", "schema_version", "port", "vid", "pid", "serial_number",
                  "firmware_version", "capability"}
        if not isinstance(value, dict) or set(value) != fields:
            raise RunnerError("local device config has missing/unknown fields")
        if (value["kind"] != "stpg_runner_device" or type(value["schema_version"]) is not int
                or value["schema_version"] != 1):
            raise RunnerError("unsupported local device kind/schema")
        for field in ("port", "serial_number"):
            text = value[field]
            if (not isinstance(text, str) or not text.strip() or text != text.strip()
                    or any(ord(char) < 32 for char in text)):
                raise RunnerError(f"device.{field} must be a nonempty, exact local binding")
        for field, maximum in (("vid", 65535), ("pid", 65535),
                               ("firmware_version", 0xFFFFFFFF), ("capability", 0xFFFFFFFF)):
            if type(value[field]) is not int or not 1 <= value[field] <= maximum:
                raise RunnerError(f"device.{field} must be an integer in 1..{maximum}")
        return cls(**{field: value[field] for field in fields - {"kind", "schema_version"}})

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "stpg_runner_device", "schema_version": 1, **asdict(self)}

    @property
    def lease_key(self) -> str:
        # Identity, not port or config filename: aliases/config copies share a lease.
        return physical_identity_key(self)


class DeviceIdentity(Protocol):
    vid: int
    pid: int
    serial_number: str


def physical_identity_key(identity: DeviceIdentity) -> str:
    """Common per-user lock identity, independent of STPG/analyzer role or port.

    Derive this inside DeviceLease as well: a caller-supplied lease_key must not
    create a second lock namespace for the same physical USB identity.
    """
    if (type(identity.vid) is not int or not 1 <= identity.vid <= 65535
            or type(identity.pid) is not int or not 1 <= identity.pid <= 65535
            or not isinstance(identity.serial_number, str) or not identity.serial_number.strip()
            or identity.serial_number != identity.serial_number.strip()
            or any(ord(char) < 32 for char in identity.serial_number)):
        raise RunnerError("invalid physical identity for device lease")
    return hashlib.sha256(f"{identity.vid}:{identity.pid}:{identity.serial_number}".encode("utf-8")).hexdigest()


def load_device_config(path: Path) -> DeviceConfig:
    return DeviceConfig.from_dict(read_json(Path(path)))


class ByteStream(Protocol):
    timeout: float

    def write(self, data: bytes) -> int: ...
    def read(self, size: int) -> bytes: ...
    def close(self) -> None: ...


def enumerate_ports() -> list[Any]:
    """One metadata enumeration; no serial port is opened by this function."""
    from serial.tools import list_ports
    return list(list_ports.comports(include_links=True))


class PortBindingError(RunnerError):
    """Pure metadata rejection, with a local snapshot; no endpoint is selected as fallback."""

    def __init__(self, code: str, reason: str, details: dict[str, Any]) -> None:
        self.details = {**details, "status": "REJECT", "code": code, "reason": reason}
        super().__init__(
            "selected port missing/duplicated or USB identity missing/ambiguous "
            f"[{code}]: {reason}; expected={json.dumps(details['expected'], ensure_ascii=True)}; "
            f"ports={json.dumps(details['ports'], ensure_ascii=True)}"
        )


def _port_endpoint(name: str, *, windows: bool | None = None) -> str:
    windows = os.name == "nt" if windows is None else windows
    return name.casefold() if windows else os.path.realpath(name)


def port_metadata(ports: list[Any]) -> list[dict[str, Any]]:
    """Exact enumeration facts only; no open/query or inferred firmware identity."""
    return [{field: getattr(port, field, None) for field in ("device", "vid", "pid", "serial_number")}
            for port in ports]


def validate_port(config: DeviceConfig, ports: list[Any]) -> dict[str, Any]:
    ports = list(ports)
    details = {"expected": {"port": config.port, "vid": config.vid, "pid": config.pid,
                             "serial_number": config.serial_number}, "ports": port_metadata(ports)}

    def reject(code: str, reason: str) -> None:
        raise PortBindingError(code, reason, details)

    def usb(port: Any) -> tuple[Any, Any, Any]:
        return tuple(getattr(port, field, None) for field in ("vid", "pid", "serial_number"))

    if any(not isinstance(getattr(port, "device", None), str) or not port.device for port in ports):
        reject("PORT_METADATA_INVALID", "enumeration contains a missing/invalid device name")

    # pyserial 3.5 only discovers links directly under /dev, not nested by-id
    # links. Resolve the explicitly selected alias to the enumerated tty; never
    # choose another endpoint by VID/PID alone. Identical duplicate records reject.
    selected = [port for port in ports if _port_endpoint(port.device) == _port_endpoint(config.port)]
    identities = [port for port in ports if usb(port) == (config.vid, config.pid, config.serial_number)]
    if not selected:
        owners = sorted({port.device for port in identities})
        reject("PORT_NOT_LISTED", f"configured port {config.port!r} is not listed; "
               f"identity is listed on {owners!r}; no alternate port will be used")
    if len({port.device for port in selected}) != len(selected):
        reject("PORT_RECORD_DUPLICATED", f"configured port {config.port!r} is listed more than once")
    if len({_port_endpoint(port.device) for port in identities}) > 1:
        reject("USB_IDENTITY_AMBIGUOUS", "USB identity is present on multiple distinct endpoints")
    port = selected[0]
    if any(usb(item) != (config.vid, config.pid, config.serial_number)
           for item in selected):
        case_only = any(usb(item)[:2] == (config.vid, config.pid)
                        and isinstance(usb(item)[2], str)
                        and usb(item)[2] != config.serial_number
                        and usb(item)[2].casefold() == config.serial_number.casefold() for item in selected)
        reject("USB_SERIAL_CASE_MISMATCH" if case_only else "PORT_USB_MISMATCH",
               "selected port USB vid/pid/serial_number does not match the exact local binding"
               + (" (serial_number differs only in letter case; not normalized)" if case_only else ""))
    if not identities:
        reject("USB_IDENTITY_NOT_LISTED", "no enumerated port has the exact configured USB identity")
    return {"port": config.port, "metadata_port": port.device, "vid": port.vid, "pid": port.pid,
            "serial_number": port.serial_number}


class DeviceLease:
    """Cooperating per-user SDK processes only; not an external-application lock.

    Keep the lock file: unlinking a locked inode would allow a second owner.
    Kernel locks are released on process exit, including a crash.
    """

    def __init__(self, config: DeviceIdentity) -> None:
        self.config = config
        self.key = physical_identity_key(config)
        self.fd: int | None = None

    def __enter__(self) -> "DeviceLease":
        user = str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
        user_key = hashlib.sha256(user.encode("utf-8")).hexdigest()[:16]
        root = Path(tempfile.gettempdir()) / f"stpg-runner-leases-{user_key}"
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink() or (hasattr(os, "getuid") and root.stat().st_uid != os.getuid()):
            raise RunnerError("unsafe SDK lease directory")
        if os.name != "nt" and stat.S_IMODE(root.stat().st_mode) & 0o077:
            raise RunnerError("SDK lease directory must be private to the current user")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(root / f"{self.key}.lock", flags, 0o600)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise RunnerError("device identity is already leased or cannot be locked")
        self.fd = fd
        return self

    def __exit__(self, *args: Any) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)


def open_serial(config: DeviceConfig) -> ByteStream:
    """One open; no buffer reset, line-control call, recovery or endpoint fallback.

    POSIX dsrdtr suppresses its DTR ioctl; on Windows it would instead enable
    DSR flow control, so disable it there. Initial DTR/RTS are cached asserted
    software values, not line readbacks. OS/USB open effects are unmeasured.
    """
    import serial
    stream = serial.Serial(port=None, baudrate=115200, timeout=SERIAL_READ_TIMEOUT_S,
                           write_timeout=2.0, dsrdtr=sys.platform != "win32", rtscts=False, xonxoff=False)
    stream.rts = True  # cached while closed, never set after open
    stream.port = config.port
    stream.open()
    return stream
