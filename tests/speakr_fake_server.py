"""Local HTTP protocol fixture for Speakr acceptance tests."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Event, Lock, Thread
from typing import Iterator


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class FakeSpeakrState:
    """Record complete requests and return scripted upload/PATCH responses."""

    def __init__(self, *, patch_statuses: tuple[int, ...] = ()) -> None:
        self.patch_statuses = list(patch_statuses)
        self.requests: list[Request] = []
        self.request_received = Event()
        self._lock = Lock()

    def response_for(self, request: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        length = int(request.headers.get("Content-Length", "0"))
        body = request.rfile.read(length)
        headers = {key.lower(): value for key, value in request.headers.items()}
        with self._lock:
            self.requests.append(Request(request.command, request.path, headers, body))
            self.request_received.set()
            if request.command == "POST":
                return 202, b'{"id": 42}'
            if request.command == "PATCH" and self.patch_statuses:
                status = self.patch_statuses.pop(0)
                return status, b"" if 200 <= status <= 299 else b"rejected private body"
            return 204, b""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self) -> None:
        status, body = self.server.state.response_for(self)  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
            self.wfile.flush()

    def do_POST(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def log_message(self, format: str, *_args: object) -> None:
        return


@contextmanager
def fake_speakr_server(*, patch_statuses: tuple[int, ...] = ()) -> Iterator[tuple[str, FakeSpeakrState]]:
    """Start a loopback server and wait for its serving thread without polling sleeps."""
    state = FakeSpeakrState(patch_statuses=patch_statuses)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.state = state  # type: ignore[attr-defined]
    started = Event()

    def serve() -> None:
        started.set()
        server.serve_forever()

    thread = Thread(target=serve, daemon=True)
    thread.start()
    if not started.wait(2):
        raise AssertionError("fake Speakr server did not start")
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def multipart_parts(request: Request) -> dict[str, tuple[str, bytes]]:
    """Decode the small multipart fixture without adding a runtime dependency."""
    boundary = request.headers["content-type"].split("boundary=", 1)[1].encode("ascii")
    result: dict[str, tuple[str, bytes]] = {}
    for segment in request.body.split(b"--" + boundary)[1:]:
        if segment in (b"--\r\n", b"--"):
            continue
        header_bytes, value = segment.split(b"\r\n\r\n", 1)
        value = value.removesuffix(b"\r\n")
        headers = header_bytes.decode("utf-8").split("\r\n")
        disposition = next(
            line for line in headers if line.casefold().startswith("content-disposition:")
        )
        name = disposition.split('name="', 1)[1].split('"', 1)[0]
        result[name] = (disposition, value)
    return result


def json_body(request: Request) -> dict[str, object]:
    """Decode a recorded JSON request for public metadata assertions."""
    return json.loads(request.body.decode("utf-8"))
