"""Background jobs for SDK calls, with an application lock for device ownership.

SDK progress callbacks may arrive on helper threads; they only append to the
job under a lock, the browser polls. A job never restarts itself: a failed or
cancelled job stays failed and a new attempt is a new job with a new output
reservation.

Event positions are absolute: ``base`` counts the events dropped from the front
once there are more than MAX_EVENTS, so a poll's ``since`` stays valid. A running
stage's heartbeat replaces the previous one in place, so a poll asks for its last
event again (``since = next - 1``); ``first`` says where the returned events start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import secrets
import threading
import time
import traceback
from typing import Any, Callable

from stpg_runner import CancellationToken
from stpg_runner.artifacts import RunnerError

MAX_EVENTS = 4000


class Busy(RunnerError):
    """Another job owns the device (or this project's build) right now."""


@dataclass
class Job:
    id: str
    kind: str
    title: str
    device: bool
    key: str | None
    started: float = field(default_factory=time.monotonic)
    ended: float | None = None
    status: str = "running"  # running | done | failed | cancelled
    events: list[dict] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    cancel_requested: bool = False
    token: CancellationToken = field(default_factory=CancellationToken)
    lock: threading.Lock = field(default_factory=threading.Lock)
    base: int = 0  # absolute position of events[0]

    def progress(self, event: Any) -> None:
        record = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        # Arrival on the job's own clock: SDK events count from the SDK call's start, GUI events have none.
        record["job_s"] = round(time.monotonic() - self.started, 3)
        with self.lock:
            if (record.get("event") == "heartbeat" and self.events
                    and self.events[-1].get("event") == "heartbeat" and self.events[-1].get("stage") == record.get("stage")):
                self.events[-1] = record  # keep the latest heartbeat of a running stage only
                return
            self.events.append(record)
            if len(self.events) > MAX_EVENTS:
                excess = len(self.events) - MAX_EVENTS
                del self.events[:excess]
                self.base += excess

    def public(self, since: int = 0) -> dict:
        with self.lock:
            start = max(0, since - self.base)
            events = self.events[start:]
            return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                    "device": self.device, "elapsed_s": round((self.ended or time.monotonic()) - self.started, 3),
                    "events": events, "first": self.base + start, "next": self.base + len(self.events),
                    "result": self.result, "error": self.error, "cancel_requested": self.cancel_requested}


class RunnerJobs:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def active_device_job(self) -> Job | None:
        with self.lock:
            return next((j for j in self.jobs.values() if j.device and j.status == "running"), None)

    def start(self, kind: str, title: str, work: Callable[[Job], Any], *, device: bool = False,
              key: str | None = None) -> Job:
        with self.lock:
            for job in self.jobs.values():
                if job.status != "running":
                    continue
                if device and job.device:
                    raise Busy(f"device job {job.id} ({job.title}) is still running")
                if key is not None and job.key == key:
                    raise Busy(f"job {job.id} ({job.title}) is still running for the same input")
            job = Job(secrets.token_hex(6), kind, title, device, key)
            self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job, work), name=f"stpg-view-{kind}", daemon=True).start()
        return job

    @staticmethod
    def _run(job: Job, work: Callable[[Job], Any]) -> None:
        try:
            result = work(job)
            with job.lock:
                job.result = result
                job.status = "cancelled" if job.cancel_requested and _cancelled(result) else "done"
        except BaseException as exc:  # noqa: BLE001 - shown in the UI, never retried
            with job.lock:
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = "cancelled" if job.cancel_requested else "failed"
            if os.environ.get("STPG_VIEW_DEBUG") and not isinstance(exc, (RunnerError, ValueError, OSError)):
                traceback.print_exc()
        finally:
            job.ended = time.monotonic()

    def running(self, key: str) -> bool:
        with self.lock:
            return any(job.key == key and job.status == "running" for job in self.jobs.values())

    def get(self, job_id: str) -> Job:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(f"unknown job {job_id!r}")
            return self.jobs[job_id]

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        with job.lock:
            if job.status == "running":
                job.cancel_requested = True
                job.token.cancel()
        return job

    def summary(self) -> list[dict]:
        with self.lock:
            jobs = list(self.jobs.values())
        return [{"id": j.id, "kind": j.kind, "title": j.title, "status": j.status, "device": j.device}
                for j in jobs[-50:]]


def _cancelled(result: Any) -> bool:
    """A job ended by its own cancellation: the STPG run or the manual GPIO operation reports CANCELLED."""

    if isinstance(result, dict):
        parts = [result[key] for key in ("run", "control") if isinstance(result.get(key), dict)] or [result]
        return any(part.get("outcome") == "CANCELLED" for part in parts)
    return False
