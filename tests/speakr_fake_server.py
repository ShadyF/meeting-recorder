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
    """Record complete requests and return scripted Speakr responses."""

    def __init__(
        self,
        *,
        patch_statuses: tuple[int, ...] = (),
        recording_pages: tuple[object, ...] = (),
        recording_statuses: tuple[int, ...] = (),
        recording_headers: tuple[dict[str, str], ...] = (),
        tag_pages: tuple[object, ...] = (),
        tag_statuses: tuple[int, ...] = (),
        tag_headers: tuple[dict[str, str], ...] = (),
    ) -> None:
        self.patch_statuses = list(patch_statuses)
        self.recording_pages = list(recording_pages)
        self.recording_statuses = list(recording_statuses)
        self.recording_headers = list(recording_headers)
        self.tag_pages = list(tag_pages)
        self.tag_statuses = list(tag_statuses)
        self.tag_headers = list(tag_headers)
        self.requests: list[Request] = []
        self.request_received = Event()
        self._lock = Lock()

    def response_for(
        self, request: BaseHTTPRequestHandler,
    ) -> tuple[int, bytes, dict[str, str]]:
        """Record one request and return its scripted response pieces."""
        # Read the complete request before selecting a response.
        length = int(request.headers.get("Content-Length", "0"))
        body = request.rfile.read(length)
        headers = {key.lower(): value for key, value in request.headers.items()}

        # Serialize request recording so page scripts stay in request order.
        with self._lock:
            self.requests.append(Request(request.command, request.path, headers, body))
            self.request_received.set()

            # Serve the next scripted reconciliation page for each GET.
            if request.command == "GET" and request.path == "/api/v1/tags":
                # Keep tag scripts independent from recording reconciliation GETs.
                index = sum(item.path == "/api/v1/tags" for item in self.requests) - 1
                status = self.tag_statuses[index] if index < len(self.tag_statuses) else 200
                page = self.tag_pages[index] if index < len(self.tag_pages) else {"tags": []}
                response_body = page if isinstance(page, bytes) else json.dumps(
                    page, separators=(",", ":"),
                ).encode("utf-8")
                response_headers = self.tag_headers[index] if index < len(self.tag_headers) else {}
                return status, response_body, response_headers

            if request.command == "GET":
                # Select the response by the number of GETs already recorded.
                index = sum(item.method == "GET" for item in self.requests) - 1
                status = (
                    self.recording_statuses[index]
                    if index < len(self.recording_statuses) else 200
                )

                # Use an empty page when a test did not script this request.
                if index < len(self.recording_pages):
                    page = self.recording_pages[index]
                else:
                    page = {"recordings": []}

                # Serialize pages while preserving raw malformed bytes.
                if isinstance(page, bytes):
                    response_body = page
                else:
                    response_body = json.dumps(
                        page, separators=(",", ":"),
                    ).encode("utf-8")

                # Add any headers scripted for this page or status test.
                response_headers = (
                    self.recording_headers[index]
                    if index < len(self.recording_headers) else {}
                )
                return status, response_body, response_headers

            # Keep the existing upload and PATCH fixture responses unchanged.
            if request.command == "POST":
                return 202, b'{"id": 42}', {}
            if request.command == "PATCH" and self.patch_statuses:
                status = self.patch_statuses.pop(0)
                response_body = (
                    b"" if 200 <= status <= 299 else b"rejected private body"
                )
                return status, response_body, {}
            return 204, b"", {}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self) -> None:
        # Retrieve the complete scripted response for this request.
        state = self.server.state  # type: ignore[attr-defined]
        status, body, response_headers = state.response_for(self)

        # Mirror scripted headers and add the framing headers required by HTTP.
        self.send_response(status)
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()

        # Write the body only when the scripted response has content.
        if body:
            self.wfile.write(body)
            self.wfile.flush()

    def do_POST(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_GET(self) -> None:
        self._handle()

    def log_message(self, format: str, *_args: object) -> None:
        return


@contextmanager
def fake_speakr_server(
    *,
    patch_statuses: tuple[int, ...] = (),
    recording_pages: tuple[object, ...] = (),
    recording_statuses: tuple[int, ...] = (),
    recording_headers: tuple[dict[str, str], ...] = (),
    tag_pages: tuple[object, ...] = (),
    tag_statuses: tuple[int, ...] = (),
    tag_headers: tuple[dict[str, str], ...] = (),
) -> Iterator[tuple[str, FakeSpeakrState]]:
    """Start a loopback server and wait for its serving thread without polling sleeps."""
    # Keep all response scripts in one state object shared by the handler.
    state = FakeSpeakrState(
        patch_statuses=patch_statuses,
        recording_pages=recording_pages,
        recording_statuses=recording_statuses,
        recording_headers=recording_headers,
        tag_pages=tag_pages,
        tag_statuses=tag_statuses,
        tag_headers=tag_headers,
    )

    # Start the local server before yielding its normalized origin.
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.state = state  # type: ignore[attr-defined]
    started = Event()

    def serve() -> None:
        # Signal readiness before entering the blocking server loop.
        started.set()
        server.serve_forever()

    thread = Thread(target=serve, daemon=True)
    # Start serving before checking readiness.
    thread.start()

    # Do not let tests race the serving thread during their first request.
    if not started.wait(2):
        raise AssertionError("fake Speakr server did not start")

    # Hand the test the live origin and always clean it up afterward.
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        # Stop and join the fixture thread so no test server leaks between tests.
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
