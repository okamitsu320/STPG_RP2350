"""Versioned host-only progress and cooperative cancellation.

Unknown totals are null, never a fabricated percentage. A callback exception
aborts the host workflow. This module issues no device commands; caller-supplied
Python callbacks are trusted application code, never loaded from a project file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import threading
import time
from typing import Any, Callable, TypeVar

from .artifacts import RunnerError

T = TypeVar("T")


class BuildCancelled(RunnerError):
    pass


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise BuildCancelled("host workflow cancelled; no automatic STOP/release/recovery; "
                                 "consult each device's lifecycle for contact facts")


@dataclass(frozen=True)
class ProgressEvent:
    stage: str
    event: str
    elapsed_s: float
    completed: int | None = None
    total: int | None = None
    detail: str = ""
    kind: str = "stpg_runner_progress"
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Reporter:
    def __init__(self, callback: Callable[[ProgressEvent], None] | None = None,
                 cancel: CancellationToken | None = None, *, heartbeat_interval: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.callback = callback
        self.cancel = cancel or CancellationToken()
        self.clock = clock
        self.started = clock()
        self.heartbeat_interval = heartbeat_interval
        self._lock = threading.RLock()

    def emit(self, stage: str, event: str, *, completed: int | None = None,
             total: int | None = None, detail: str = "") -> None:
        with self._lock:
            self.cancel.check()
            if self.callback is not None:
                self.callback(ProgressEvent(stage, event, self.clock() - self.started,
                                            completed, total, detail))
            self.cancel.check()

    def perform(self, stage: str, operation: Callable[[], T], *, total: int | None = None) -> T:
        """Report elapsed-time heartbeats, not compiler percentage or device state.

        Callbacks are serialized but heartbeats use a helper thread. Cancellation
        cannot interrupt the existing compiler; it rejects at the next boundary.
        """
        self.emit(stage, "start", total=total)
        stop = threading.Event()
        errors: list[BaseException] = []

        def heartbeat() -> None:
            while not stop.wait(self.heartbeat_interval):
                try:
                    self.emit(stage, "heartbeat", total=total)
                except BaseException as exc:
                    errors.append(exc)
                    stop.set()

        worker = None
        if self.callback is not None:
            worker = threading.Thread(target=heartbeat, name="stpg-host-progress", daemon=True)
            worker.start()
        try:
            result = operation()
        finally:
            stop.set()
            if worker is not None:
                worker.join()
        if errors:
            raise errors[0]
        self.emit(stage, "done", completed=total, total=total)
        return result
