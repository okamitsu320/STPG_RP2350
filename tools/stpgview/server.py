"""Local HTTP server for the viewer UI (standard library only).

The server binds to 127.0.0.1 by default. Every API request must carry the
per-process token (``X-STPG-Token`` header or ``token`` query parameter), and
the Host header must name the bound address, so other local programs or web
pages cannot read files through it.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import traceback
from urllib.parse import parse_qs, quote, urlparse

from .session import Session, dumps
from .vcd import write_vcd

STATIC_DIR = Path(__file__).with_name("static")
UPLOAD_LIMIT = 512 * 1024 * 1024
JSON_LIMIT = 1024 * 1024
LOOPBACK = ("127.0.0.1", "localhost", "::1")
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


EXTERNAL_VIEWERS = ("surfer", "gtkwave")


def find_external_viewer(name: str | None) -> str | None:
    """Path of an installed waveform viewer (Surfer preferred), or None."""

    for candidate in ((name,) if name else EXTERNAL_VIEWERS):
        if candidate not in EXTERNAL_VIEWERS:
            continue
        path = shutil.which(candidate)
        if path:
            return path
    return None


def path_roots(system: str | None = None, drives=None) -> list[str]:
    """Where a path can start: on Windows every logical drive that exists now (A to Z), elsewhere "/".

    No network location is searched; a UNC path typed into the path box is opened with Go."""

    if (system or os.name) != "nt":
        return ["/"]
    if drives is None:
        listdrives = getattr(os, "listdrives", None)  # Python 3.12 and later
        try:
            drives = list(listdrives()) if listdrives else None
        except OSError:
            drives = None
        if drives is None:
            drives = [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    return sorted({str(drive) for drive in drives}, key=str.upper)


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], session: Session, token: str, runner=None) -> None:
        super().__init__(address, ViewerHandler)
        self.session = session
        self.token = token
        self.upload_dir = Path(tempfile.mkdtemp(prefix="stpg-view-"))
        self.runner = runner  # RunnerService, or None when the runner is unavailable

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/?token={self.token}"


class ViewerHandler(BaseHTTPRequestHandler):
    server: ViewerServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - signature from the base class
        if os.environ.get("STPG_VIEW_DEBUG"):
            super().log_message(format, *args)

    # ---- request body ---------------------------------------------------
    # HTTP/1.1 keeps the connection open, so bytes of a body that is not read would be taken as the start of the
    # next request on it ("{}POST"). Each request's body is therefore framed once, read at most once, and the
    # connection is closed after any response whose request body was not read. Nothing is drained blindly.
    def _frame_body(self) -> str | None:
        """Take the body's length from the headers once; the reason it cannot be framed safely, if any."""

        self._body_length, self._body_state = 0, "none"
        if self.headers.get("Transfer-Encoding") is not None:
            self._body_state = "unframed"
            return "unsupported request framing (Transfer-Encoding)"
        values = self.headers.get_all("Content-Length") or []
        if not values:
            return None
        text = values[0].strip()
        if len(values) != 1 or not text.isascii() or not text.isdigit() or len(text) > 15:
            self._body_state = "unframed"
            return "invalid Content-Length"
        self._body_length = int(text)
        if self._body_length:
            self._body_state = "unread"
        return None

    def _body(self, limit: int = UPLOAD_LIMIT) -> bytes:
        """The request body, read once; a body over ``limit`` stays unread and the connection closes."""

        state = getattr(self, "_body_state", "none")
        if state == "read":
            raise ValueError("the request body was already read")
        if state == "unframed":
            raise ValueError("the request body cannot be framed")
        if state == "none":
            self._body_state = "read"
            return b""
        if self._body_length > limit:
            raise ValueError("request too large")
        data = self.rfile.read(self._body_length)
        self._body_state = "read" if len(data) == self._body_length else "unframed"
        if self._body_state != "read":
            raise ValueError("the request body ended early")
        return data

    def _json_body(self) -> dict:
        value = json.loads(self._body(JSON_LIMIT) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON object expected")
        return value

    # ---- helpers --------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        if getattr(self, "_body_state", "none") in ("unread", "unframed"):
            self.send_header("Connection", "close")  # also sets close_connection: no leftover byte is parsed
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: object, status: int = 200) -> None:
        self._send(status, dumps(value), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        bound = self.server.server_address[0]
        return host in ("127.0.0.1", "localhost", bound)

    def _authorized(self, query: dict) -> bool:
        token = self.headers.get("X-STPG-Token") or (query.get("token") or [""])[0]
        return secrets.compare_digest(token, self.server.token)

    def _same_origin(self) -> bool:
        """State changes must come from this page: same Origin (when sent) and no cross-site fetch."""

        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{self.headers.get('Host', '')}":
            return False
        site = self.headers.get("Sec-Fetch-Site")
        return site in (None, "same-origin", "none")

    # ---- routing --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        unframed = self._frame_body()
        if unframed:
            self._error(400, unframed)
            return
        if not self._host_ok():
            self._error(403, "bad host")
            return
        try:
            if method == "GET" and (parsed.path == "/" or parsed.path.startswith("/static/")):
                self._static(parsed.path)
                return
            if not parsed.path.startswith("/api/"):
                self._error(404, "not found")
                return
            if not self._authorized(query):
                self._error(401, "missing or wrong token")
                return
            if method == "POST":
                if not self.headers.get("X-STPG-Token") or not self._same_origin():
                    self._error(403, "state changes need the token header and a same-origin request")
                    return
            name = parsed.path[5:].replace("/", "_")
            if name.startswith("runner_"):
                if self.server.runner is None:
                    self._error(503, "the runner is not available in this server")
                    return
            handler = getattr(self, f"_api_{name}", None)
            if handler is None:
                self._error(404, "unknown api")
                return
            handler(method, query)
        except (ValueError, KeyError, FileNotFoundError) as exc:
            status = 409 if type(exc).__name__ == "Busy" else 400
            self._error(status, str(exc))
        except Exception as exc:  # noqa: BLE001 - report to the UI instead of dropping the connection
            if os.environ.get("STPG_VIEW_DEBUG"):
                traceback.print_exc()
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _static(self, path: str) -> None:
        name = "index.html" if path == "/" else path[len("/static/"):]
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            self._error(404, "not found")
            return
        # Fixed types: on Windows the registry can map .js to text/plain, which
        # browsers refuse to execute when nosniff is set.
        content_type = _STATIC_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] \
            or "application/octet-stream"
        self._send(200, target.read_bytes(), content_type)

    # ---- api ------------------------------------------------------------
    def _api_state(self, method: str, query: dict) -> None:
        self._json(self.server.session.state())

    def _api_open(self, method: str, query: dict) -> None:
        if method != "POST":
            self._error(405, "POST required")
            return
        request = self._json_body()  # JSON, with the JSON limit; only /api/upload takes a file's bytes
        paths = request.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        ids = self.server.session.load(paths, pin_map=request.get("pin_map") or None,
                                       map_path=request.get("map") or None,
                                       replace=bool(request.get("replace", True)))
        self._json({"loaded": ids, **self.server.session.state()})

    def _api_upload(self, method: str, query: dict) -> None:
        """Store an uploaded file (drag & drop) under the session upload dir and return its path."""

        if method != "POST":
            self._error(405, "POST required")
            return
        name = Path((query.get("name") or ["upload.bin"])[0]).name
        if not name or name in (".", ".."):
            raise ValueError("bad file name")
        # Files dropped together share one folder so a pattern.bin finds its map.json.
        batch = "".join(ch for ch in (query.get("batch") or [""])[0] if ch.isalnum())[:32]
        folder = self.server.upload_dir / (batch or secrets.token_hex(4))
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        target.write_bytes(self._body())
        self._json({"path": str(target)})

    def _api_browse(self, method: str, query: dict) -> None:
        raw = (query.get("dir") or [""])[0]
        folder = Path(raw).expanduser() if raw else Path.cwd()
        folder = folder.resolve()
        if not folder.is_dir():
            raise ValueError(f"{folder}: not a directory")
        entries = []
        for entry in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
                size = None if is_dir else entry.stat().st_size
            except OSError:
                continue
            entries.append({"name": entry.name, "dir": is_dir, "size": size})
        self._json({"dir": str(folder), "parent": str(folder.parent), "entries": entries, "roots": path_roots(),
                    "home": str(Path.home())})

    def _trace(self, query: dict):  # type: ignore[no-untyped-def]
        return self.server.session.get((query.get("trace") or [None])[0])

    @staticmethod
    def _int(query: dict, key: str, default: int | None = None) -> int | None:
        value = (query.get(key) or [None])[0]
        if value is None or value == "":
            return default
        return int(float(value))

    def _api_rows(self, method: str, query: dict) -> None:
        trace = self._trace(query)
        self._json(self.server.session.rows(trace, self._int(query, "start", 0), self._int(query, "count", 200)))

    def _api_wave(self, method: str, query: dict) -> None:
        trace = self._trace(query)
        self._json(self.server.session.wave(trace, self._int(query, "t0", 0), self._int(query, "t1", 0),
                                            self._int(query, "buckets", 1000)))

    def _api_vector(self, method: str, query: dict) -> None:
        trace = self._trace(query)
        index = self._int(query, "i", 0)
        if not 0 <= index < trace.vector_count:
            raise ValueError("vector index out of range")
        self._json(trace.detail(index))

    def _api_find(self, method: str, query: dict) -> None:
        trace = self._trace(query)
        self._json(self.server.session.find(trace, line=self._int(query, "line"), time_ps=self._int(query, "t"),
                                            vector=self._int(query, "vector")))

    def _api_marks(self, method: str, query: dict) -> None:
        """Fail marks of one trace: POST places a run's fails (checked against this trace), GET reads them."""

        if method == "POST":
            body = self._json_body()
            self._json(self.server.session.set_marks(body.get("trace"), body.get("marks"), body.get("title") or ""))
            return
        self._json(self.server.session.marks(self._trace(query)))

    def _api_external(self, method: str, query: dict) -> None:
        """Export the trace to a VCD and open it in Surfer or GTKWave if installed."""

        if method != "POST":
            self._error(405, "POST required")
            return
        viewer = find_external_viewer((query.get("viewer") or [""])[0] or None)
        if viewer is None:
            raise ValueError("no external waveform viewer found on PATH (install surfer or gtkwave)")
        trace = self._trace(query)
        folder = self.server.upload_dir / "vcd"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{Path(trace.title).stem}-{secrets.token_hex(3)}.vcd"
        with target.open("w", newline="\n") as stream:
            write_vcd(trace, stream)
        subprocess.Popen([viewer, str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=os.name != "nt")
        self._json({"viewer": viewer, "vcd": str(target)})

    def _api_viewers(self, method: str, query: dict) -> None:
        self._json({"external": find_external_viewer(None)})

    def _api_edits(self, method: str, query: dict) -> None:
        trace_id = (query.get("trace") or [None])[0]
        manager = self.server.session.edits
        if method == "GET":
            self._json(manager.info(trace_id))
            return
        request = self._json_body()
        action = request.get("action")
        if action == "add":
            info = manager.add(trace_id, request.get("edit") or {})
        elif action == "remove":
            info = manager.remove(trace_id, int(request.get("index", -1)))
        elif action == "clear":
            info = manager.replace_all(trace_id, [])
        elif action == "save":
            info = manager.save(trace_id, request.get("path") or None)
        elif action == "export":
            info = manager.export(trace_id, request.get("path") or "")
            self._json(info)
            return
        else:
            raise ValueError(f"unknown edit action {action!r}")
        self._json({**info, **self.server.session.state()})

    def _api_preview_map(self, method: str, query: dict) -> None:
        """One vector of an edited preview in the source (``i``), or one source vector in the preview (``source``)."""

        mapping = self.server.session.edits.preview_mapping((query.get("trace") or [""])[0])
        index, source = self._int(query, "i"), self._int(query, "source")
        if mapping is None:
            self._json({"source": index, "preview": source})
        elif source is not None:
            self._json({"preview": mapping.to_preview(source)})
        else:
            self._json({"source": mapping.to_source(index if index is not None else -1)})

    def _api_edits_chars(self, method: str, query: dict) -> None:
        self._json(self.server.session.edits.char_choices((query.get("trace") or [""])[0], self._int(query, "i", 0),
                                                         (query.get("signal") or [""])[0]))

    def _api_export_vcd(self, method: str, query: dict) -> None:
        trace = self._trace(query)
        buffer = io.StringIO()
        write_vcd(trace, buffer, start=self._int(query, "start", 0), end=self._int(query, "end"))
        body = buffer.getvalue().encode()
        name = Path(trace.title).stem + ".vcd"
        self._send(200, body, "text/plain; charset=utf-8",
                   {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    # ---- runner (SDK adapter; see runner/service.py) ------------------------
    def _need_post(self, method: str) -> bool:
        if method != "POST":
            self._error(405, "POST required")
            return False
        return True

    def _job(self, job) -> None:  # type: ignore[no-untyped-def]
        self._json(job.public(0))

    def _api_runner_state(self, method: str, query: dict) -> None:
        self._json(self.server.runner.state())

    def _api_runner_project_open(self, method: str, query: dict) -> None:
        if self._need_post(method):
            self._json(self.server.runner.open_project(self._json_body().get("path") or ""))

    def _api_runner_project_init(self, method: str, query: dict) -> None:
        if self._need_post(method):
            self._json(self.server.runner.init_project(self._json_body()))

    def _api_runner_project_edit(self, method: str, query: dict) -> None:
        if self._need_post(method):
            self._json(self.server.runner.edit_project(self._json_body()))

    def _api_runner_bundle_open(self, method: str, query: dict) -> None:
        if self._need_post(method):
            body = self._json_body()
            self._json(self.server.runner.open_bundle(body.get("path") or "", body.get("manifest_sha256")))

    def _api_runner_build(self, method: str, query: dict) -> None:
        if self._need_post(method):
            body = self._json_body()
            self._job(self.server.runner.start_build(body.get("project") or "", body.get("work_dir") or None))

    def _api_runner_verify(self, method: str, query: dict) -> None:
        if self._need_post(method):
            body = self._json_body()
            self._job(self.server.runner.start_verify(body.get("bundle") or "", body.get("manifest_sha256")))

    def _api_runner_plan(self, method: str, query: dict) -> None:
        if self._need_post(method):
            body = self._json_body()
            self._job(self.server.runner.start_plan(body.get("bundle") or "", body.get("manifest_sha256") or ""))

    def _api_runner_job(self, method: str, query: dict) -> None:
        job = self.server.runner.jobs.get((query.get("id") or [""])[0])
        self._json(job.public(self._int(query, "since", 0) or 0))

    def _api_runner_job_cancel(self, method: str, query: dict) -> None:
        if self._need_post(method):
            self._json(self.server.runner.cancel(self._json_body().get("id") or "").public(0))

    def _api_runner_bindings(self, method: str, query: dict) -> None:
        runner = self.server.runner
        if method == "GET":
            self._json(runner.bindings((query.get("kind") or ["devices"])[0]))
            return
        body = self._json_body()
        kind, name = body.get("kind") or "", body.get("name") or ""
        if body.get("action") == "save":
            runner.store.save(kind, name, body.get("config") or {}, replace=bool(body.get("replace")))
        elif body.get("action") == "delete":
            runner.store.delete(kind, name)
        else:
            raise ValueError("binding action must be save or delete")
        self._json(runner.bindings(kind))

    def _api_runner_ports(self, method: str, query: dict) -> None:
        if self._need_post(method):  # an explicit user action, never part of a page load
            self._json_body()  # the page sends {}: read it, so that it cannot reach the next request
            self._json(self.server.runner.scan_ports())

    def _api_runner_capabilities(self, method: str, query: dict) -> None:
        self._json({"names": self.server.runner.capability_names(self._int(query, "value", 0) or 0)})

    def _api_runner_run_prepare(self, method: str, query: dict) -> None:
        if self._need_post(method):
            self._json(self.server.runner.prepare_run(self._json_body()))

    def _api_runner_run_start(self, method: str, query: dict) -> None:
        if self._need_post(method):
            self._job(self.server.runner.start_run(self._json_body()))

    def _api_runner_control_prepare(self, method: str, query: dict) -> None:
        if self._need_post(method):  # manual GPIO set / HW reset: confirmation first, never automatic
            self._json(self.server.runner.prepare_control(self._json_body()))

    def _api_runner_control_start(self, method: str, query: dict) -> None:
        if self._need_post(method):
            self._job(self.server.runner.start_control(self._json_body()))

    def _api_runner_control_identify(self, method: str, query: dict) -> None:
        if self._need_post(method):  # identity/status exchange only; no pin command
            self._job(self.server.runner.start_controller_identify(self._json_body().get("controller") or ""))

    def _api_runner_gpio_plan_load(self, method: str, query: dict) -> None:
        if self._need_post(method):
            self._json(self.server.runner.control_plan_load(self._json_body()))

    def _api_runner_save_plan(self, method: str, query: dict) -> None:
        if self._need_post(method):
            self._json(self.server.runner.save_plan(self._json_body()))

    def _api_runner_external_inspect(self, method: str, query: dict) -> None:
        if self._need_post(method):  # reads the selected local accessory config and its files; launches nothing
            self._json(self.server.runner.inspect_external(self._json_body()))

    def _api_runner_stil_info(self, method: str, query: dict) -> None:
        if self._need_post(method):  # reads the named STIL only (the scope hint of the New-project dialog)
            self._json(self.server.runner.stil_info(self._json_body().get("path") or ""))

    def _api_runner_results(self, method: str, query: dict) -> None:
        self._json(self.server.runner.results((query.get("root") or [None])[0]))

    def _api_runner_result_describe(self, method: str, query: dict) -> None:
        if self._need_post(method):  # offline: the SDK's STIL description of one result; no device
            self._json(self.server.runner.describe_run(self._json_body()))

    def _api_runner_result_save_text(self, method: str, query: dict) -> None:
        if self._need_post(method):  # a new .txt file only; the text is made again on the server
            self._json(self.server.runner.save_description(self._json_body()))

    def _api_runner_rebind(self, method: str, query: dict) -> None:
        if self._need_post(method):  # explicit, offline: a new bundle beside the old one, no recompile
            body = self._json_body()
            self._job(self.server.runner.start_rebind(body.get("bundle") or "", body.get("manifest_sha256") or ""))

def make_runner(session: Session, *, host: str, device_run: bool = True, demo: bool = False,
                config_dir: str | Path | None = None, results_dir: Path | None = None):
    """The runner exists only on a loopback server: it can drive hardware."""

    if host not in LOOPBACK:
        return None
    from .config_location import resolve_config_dir
    from .runner.service import RunnerService

    location = resolve_config_dir(config_dir)
    return RunnerService(config_dir=location.path, config_location=location, device_run=device_run, demo=demo,
                         results_dir=results_dir or Path.home() / "stpg-work" / "runs", session=session)


def serve(session: Session, *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True,
          device_run: bool = True, demo: bool = False, config_dir: str | Path | None = None,
          results_dir: Path | None = None) -> None:
    from .config_location import start_line

    token = secrets.token_urlsafe(16)
    runner = make_runner(session, host=host, device_run=device_run, demo=demo, config_dir=config_dir,
                         results_dir=results_dir)
    server = ViewerServer((host, port), session, token, runner)
    if runner is None:
        print("stpg-view: runner disabled: it is only served on a loopback address", file=sys.stderr)
    else:
        print(start_line("stpg-view", runner.location), flush=True)
        if not device_run:
            print("stpg-view: device execution disabled (--no-device-run)", flush=True)
    if demo:
        print("stpg-view: demo mode: a simulated STPG and GPIO controller are available (no hardware)", flush=True)
    print(f"STPG pattern viewer: {server.url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        import webbrowser

        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
