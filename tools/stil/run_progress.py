"""Host-only stage timings; observing a run never adds device commands."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
from typing import Any, Iterator


def write_atomic_json(path: Path, value: Any) -> None:
    """Publish a complete small result/checkpoint, leaving old data on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class RunProgress:
    def __init__(self, output_dir: Path | None = None, *, label: str = "segmented") -> None:
        self.started = time.perf_counter()
        self.output_dir = output_dir
        self.label = label
        self.events: list[dict[str, Any]] = []
        self._starts: dict[tuple[str, Any], list[float]] = {}
        self.environment = {
            "python": sys.version, "python_executable": sys.executable,
            "platform": platform.platform(), "module_path": __file__,
            "started_utc": datetime.now(timezone.utc).isoformat(),
        }
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            self.log_path = output_dir / f"{label}.progress.jsonl"
            # A fresh log belongs to this invocation, not a previous run.
            self.log_path.write_text("", encoding="utf-8")
        else:
            self.log_path = None

    @contextmanager
    def stage(self, name: str, **details: Any) -> Iterator[None]:
        started = time.perf_counter()
        self.event(name, "start", **details)
        try:
            yield
        except BaseException as exc:
            self.event(name, "error", duration_s=time.perf_counter() - started,
                       error=f"{type(exc).__name__}: {exc}", **details)
            raise
        else:
            self.event(name, "done", duration_s=time.perf_counter() - started, **details)

    def event(self, stage: str, event: str, **details: Any) -> None:
        elapsed = time.perf_counter() - self.started
        key = (stage, details.get("segment_id"))
        if event == "start":
            self._starts.setdefault(key, []).append(elapsed)
        elif event in {"done", "error"} and self._starts.get(key):
            start = self._starts[key].pop()
            details.setdefault("duration_s", elapsed - start)
        record = {"stage": stage, "event": event, "elapsed_s": elapsed, **details}
        self.events.append(record)
        segment = f" segment={details['segment_id']}" if "segment_id" in details else ""
        duration = f" duration_s={details['duration_s']:.3f}" if "duration_s" in details else ""
        print(f"STPG stage={stage} event={event}{segment}{duration}", file=sys.stderr, flush=True)
        if self.log_path is not None:
            with self.log_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=True) + "\n")

    def save(self) -> None:
        if self.output_dir is not None:
            write_atomic_json(self.output_dir / f"{self.label}.performance.json", {
                "environment": self.environment, "elapsed_s": time.perf_counter() - self.started,
                "events": self.events,
                "timing_scope": "host wall time; not waveform or refill timing sign-off",
            })
