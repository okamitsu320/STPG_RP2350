"""Requests on one kept-alive HTTP/1.1 connection: each body is read once or the connection is closed (host only).

The first STPG binding could not be saved: Scan ports sent ``{}``, the server answered without reading it, and the
same connection then carried ``{}POST /api/runner/bindings`` (501). These checks use one connection per case and
a demo runner with device runs off; no port is listed or opened.

Several cases put a complete binding-save request inside a body that the server must not read. If such a body were
left on the connection, that save would be executed as the next request.
"""

from __future__ import annotations

import http.client
import io
import json
from pathlib import Path
import re
import socket

import pytest

from stpgview.runner.simulated import SIMULATED_DEVICE
from stpgview.server import JSON_LIMIT, ViewerHandler, ViewerServer, make_runner
from stpgview.session import Session

pytestmark = pytest.mark.stpg_host_only
TOKEN = "tok"
SAVE = json.dumps({"kind": "devices", "action": "save", "name": "bench", "config": SIMULATED_DEVICE.to_dict()})


@pytest.fixture
def served(tmp_path, monkeypatch):
    import threading

    session = Session(tmp_path / "work")
    runner = make_runner(session, host="127.0.0.1", demo=True, device_run=False, config_dir=tmp_path / "cfg",
                         results_dir=tmp_path / "runs")
    saves = []
    original = runner.store.save
    monkeypatch.setattr(runner.store, "save", lambda *a, **k: saves.append(a[1]) or original(*a, **k))
    server = ViewerServer(("127.0.0.1", 0), session, TOKEN, runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.saves = saves
    server.host = "%s:%d" % server.server_address[:2]
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def raw_request(host: str, method: str, path: str, body: bytes = b"", headers: tuple = (), length: str | None = None):
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", *headers]
    if length is not None or body:
        lines.append(f"Content-Length: {len(body) if length is None else length}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def exchange(server, data: bytes, timeout: float = 3.0) -> tuple[bytes, bool]:
    """Everything the server sends back on one connection, and whether it closed that connection."""

    with socket.create_connection(server.server_address[:2], timeout=timeout) as sock:
        sock.sendall(data)
        received = b""
        try:
            while chunk := sock.recv(65536):
                received += chunk
            return received, True
        except socket.timeout:
            return received, False


def statuses(data: bytes) -> list[int]:
    # A status line can follow the previous body directly (no line break), so search the whole stream.
    return [int(code) for code in re.findall(rb"HTTP/1\.[01] (\d{3}) ", data)]


def inner_save(host: str) -> bytes:
    """A complete save request: it must never run when it arrives as the unread body of another request."""

    return raw_request(host, "POST", "/api/runner/bindings", SAVE.encode(),
                       (f"X-STPG-Token: {TOKEN}", "Content-Type: application/json"))


def test_scan_then_save_on_one_connection_is_200_200_and_saves_once(served):
    host, port = served.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=10)
    headers = {"X-STPG-Token": TOKEN, "Content-Type": "application/json"}
    try:
        connection.request("POST", "/api/runner/ports", b"{}", headers)
        scan = connection.getresponse()
        assert scan.status == 200 and scan.getheader("Connection") is None  # read, so the connection stays
        assert json.loads(scan.read())["real_scan_performed"] is False
        connection.request("POST", "/api/runner/bindings", SAVE, headers)
        save = connection.getresponse()
        body = save.read()
        assert save.status == 200, body
        assert [item["name"] for item in json.loads(body)["items"] if not item.get("simulated")] == ["bench"]
    finally:
        connection.close()
    assert served.saves == ["bench"]
    assert [p.name for p in (Path(served.runner.store.root) / "devices").iterdir()] == ["bench.json"]


CASES = {
    "bad host": lambda h: ("POST", "/api/runner/bindings", (f"X-STPG-Token: {TOKEN}",), "evil.example:80", 403),
    "wrong token": lambda h: ("POST", "/api/runner/bindings", ("X-STPG-Token: wrong",), h, 401),
    "token only in the query": lambda h: ("POST", f"/api/runner/bindings?token={TOKEN}", (), h, 403),
    "cross-site": lambda h: ("POST", "/api/runner/bindings", (f"X-STPG-Token: {TOKEN}", "Origin: http://evil.example"),
                             h, 403),
    "unknown api": lambda h: ("POST", "/api/no_such_route", (f"X-STPG-Token: {TOKEN}",), h, 404),
    "not an api": lambda h: ("POST", "/nowhere", (), h, 404),
    "a route that reads no body": lambda h: ("POST", "/api/state", (f"X-STPG-Token: {TOKEN}",), h, 200),
    "a GET with a body": lambda h: ("GET", "/api/runner/state", (f"X-STPG-Token: {TOKEN}",), h, 200),
    "the page with a body": lambda h: ("GET", "/", (), h, 200),
    "an upload refused before its body": lambda h: ("POST", "/api/upload?name=..", (f"X-STPG-Token: {TOKEN}",), h, 400),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_an_unread_body_closes_the_connection_and_never_becomes_the_next_request(served, name):
    method, path, headers, host, status = CASES[name](served.host)
    first = raw_request(host, method, path, inner_save(served.host), headers)
    follow = raw_request(served.host, "GET", "/api/runner/state", headers=(f"X-STPG-Token: {TOKEN}",))
    data, closed = exchange(served, first + follow)
    assert statuses(data) == [status], data[:300]  # one answer: neither the body nor the follow-up was run
    assert closed and b"Connection: close" in data
    assert served.saves == [] and not (Path(served.runner.store.root) / "devices").exists()


@pytest.mark.parametrize("framing", [
    ("Transfer-Encoding: chunked",),
    ("Content-Length: 2", "Content-Length: 2"),
    ("Content-Length: +2",),
    ("Content-Length: -1",),
    ("Content-Length: 2x",),
    ("Content-Length: 0x2",),
])
def test_a_body_that_cannot_be_framed_is_refused_and_the_connection_closes(served, framing):
    body = b"{}" + inner_save(served.host)
    head = ["POST /api/runner/ports HTTP/1.1", f"Host: {served.host}", f"X-STPG-Token: {TOKEN}",
            "Content-Type: application/json", *framing]
    data, closed = exchange(served, ("\r\n".join(head) + "\r\n\r\n").encode("utf-8") + body)
    assert statuses(data) == [400] and closed and b"Connection: close" in data
    assert served.saves == []


def test_a_json_body_over_the_json_limit_is_not_read_and_the_connection_closes(served):
    declared = str(JSON_LIMIT + 1)  # only the start is sent: the server must answer without waiting for the rest
    first = raw_request(served.host, "POST", "/api/runner/project/open", inner_save(served.host),
                        (f"X-STPG-Token: {TOKEN}", "Content-Type: application/json"), length=declared)
    data, closed = exchange(served, first)
    assert statuses(data) == [400] and b"request too large" in data and closed
    assert served.saves == []


def test_the_open_route_takes_json_under_the_json_limit_and_only_upload_takes_file_bytes(served):
    host, port = served.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=10)
    headers = {"X-STPG-Token": TOKEN, "Content-Type": "application/json"}
    try:
        connection.request("POST", "/api/open", b"[]", headers)  # JSON, but not an object
        response = connection.getresponse()
        assert response.status == 400 and b"JSON object expected" in response.read()
        payload = b"x" * (JSON_LIMIT + 10)
        connection.request("POST", "/api/upload?name=big.bin&batch=b1", payload, {"X-STPG-Token": TOKEN})
        response = connection.getresponse()
        stored = Path(json.loads(response.read())["path"])
        assert response.status == 200 and stored.read_bytes() == payload
        connection.request("GET", "/api/runner/state", headers={"X-STPG-Token": TOKEN})  # still the same connection
        assert connection.getresponse().status == 200
    finally:
        connection.close()


def _handler(raw_headers: bytes, body: bytes) -> ViewerHandler:
    handler = ViewerHandler.__new__(ViewerHandler)  # no socket: only the body bookkeeping is exercised
    handler.headers = http.client.parse_headers(io.BytesIO(raw_headers + b"\r\n"))
    handler.rfile = io.BytesIO(body)
    return handler


def test_a_body_is_read_at_most_once():
    handler = _handler(b"Content-Length: 2\r\n", b"{}")
    assert handler._frame_body() is None and handler._json_body() == {}
    with pytest.raises(ValueError, match="already read"):
        handler._body()
    empty = _handler(b"", b"")
    assert empty._frame_body() is None and empty._body() == b""
    with pytest.raises(ValueError, match="already read"):
        empty._json_body()


def test_a_length_must_be_ascii_digits():
    from email.message import Message

    handler = _handler(b"", b"{}")
    handler.headers = Message()
    handler.headers["Content-Length"] = "\uff12"  # a digit to str.isdigit, not to HTTP
    assert handler._frame_body() == "invalid Content-Length" and handler._body_state == "unframed"


def test_a_body_that_ends_early_is_never_taken_as_complete():
    handler = _handler(b"Content-Length: 10\r\n", b"{}")
    assert handler._frame_body() is None
    with pytest.raises(ValueError, match="ended early"):
        handler._body()
    assert handler._body_state == "unframed"  # the answer then closes the connection
