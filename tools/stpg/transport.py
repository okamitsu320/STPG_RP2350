"""Byte-stream transports for STPG protocol clients."""

from __future__ import annotations

import math
import time
from typing import Callable, Protocol


class Transport(Protocol):
    def write(self, data: bytes) -> None:
        ...

    def read_exactly(self, size: int) -> bytes:
        ...

    def read_exactly_with_timeout(self, size: int, timeout: float) -> bytes:
        ...

    def set_dtr(self, enabled: bool) -> bool:
        ...

    def close(self) -> None:
        ...


class SerialTransport:
    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 115200,
        timeout: float = 2.0,
        suppress_dtr_on_open: bool = False,
    ):
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("pyserial is required for SerialTransport") from exc
        options = {
            "baudrate": baudrate,
            "timeout": timeout,
            "write_timeout": timeout,
        }
        if not isinstance(suppress_dtr_on_open, bool):
            raise TypeError("suppress_dtr_on_open must be bool")
        if not suppress_dtr_on_open:
            self._serial = serial.Serial(port=port, **options)
        else:
            # On POSIX, pyserial skips its DTR ioctl during open when
            # dsrdtr=True. This is used only by the Linux human-run pilot;
            # the kernel/USB CDC driver's own open behavior is platform-owned.
            self._serial = serial.Serial(
                port=None,
                dsrdtr=True,
                rtscts=False,
                **options,
            )
            # pyserial 3.5 does not accept RTS as a Serial constructor
            # argument. Set its cached line state while closed; open() applies
            # it together with the requested port configuration.
            self._serial.rts = True
            self._serial.port = port
            self._serial.open()

    def write(self, data: bytes) -> None:
        written = self._serial.write(data)
        if written != len(data):
            raise OSError(f"short serial write: expected {len(data)} byte(s), wrote {written}")
        self._serial.flush()

    def write_run_packet_with_boundary(self, data: bytes, on_host_write_complete: Callable[[bytes], None]) -> None:
        """Dedicated RUN boundary: full 32-byte host write and flush, callback, then caller may read."""
        if len(data) != 32:
            raise ValueError("dedicated RUN packet must be exactly 32 bytes")
        written = self._serial.write(data)
        if written != len(data):
            raise OSError(f"short RUN write: expected {len(data)} byte(s), wrote {written}")
        self._serial.flush()
        on_host_write_complete(data)

    def write_upload_end_packet_with_boundary(
        self, data: bytes, on_host_write_complete: Callable[[bytes], None]
    ) -> None:
        """Dedicated UPLOAD_END boundary: full 32-byte write and flush before the callback.

        This method deliberately does not read a response or send any second
        packet.  The caller owns the one response wait after its durable
        callback has completed.
        """
        if len(data) != 32:
            raise ValueError("dedicated UPLOAD_END packet must be exactly 32 bytes")
        written = self._serial.write(data)
        if written != len(data):
            raise OSError(f"short UPLOAD_END write: expected {len(data)} byte(s), wrote {written}")
        self._serial.flush()
        on_host_write_complete(data)

    def read_exactly(self, size: int) -> bytes:
        deadline = time.monotonic() + float(self._serial.timeout or 0.0)
        chunks = bytearray()
        while len(chunks) < size:
            remaining = size - len(chunks)
            data = self._serial.read(remaining)
            if data:
                chunks.extend(data)
                continue
            if time.monotonic() >= deadline:
                break
        if len(chunks) != size:
            raise TimeoutError(f"expected {size} byte(s), got {len(chunks)}")
        return bytes(chunks)

    def read_exactly_with_timeout(self, size: int, timeout: float) -> bytes:
        """Read one response with a temporary bounded serial read timeout.

        This deliberately changes only the response-read deadline.  It does
        not alter the transport's write timeout or issue any additional I/O.
        The original timeout is restored even when the read fails.
        """
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ValueError("response timeout must be a finite positive number")
        original_timeout = self._serial.timeout
        self._serial.timeout = float(timeout)
        try:
            return self.read_exactly(size)
        finally:
            self._serial.timeout = original_timeout

    def set_dtr(self, enabled: bool) -> bool:
        """Issue one synchronous CDC SET_CONTROL_LINE_STATE DTR request."""
        if not isinstance(enabled, bool):
            raise TypeError("DTR state must be bool")
        self._serial.dtr = enabled
        return bool(self._serial.dtr)

    def close(self) -> None:
        self._serial.close()

    def __enter__(self) -> "SerialTransport":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.close()
