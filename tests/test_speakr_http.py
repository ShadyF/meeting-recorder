"""Deterministic protocol tests for the stdlib Speakr transport."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
from threading import Event, Thread
from typing import Iterator, cast

import meeting_recorder.speakr_http as speakr_http
from meeting_recorder.speakr_domain import SpeakrMetadata
from meeting_recorder.speakr_http import (
    InvalidSpeakrResponse,
    MetadataRejected,
    MetadataUnavailable,
    StdlibSpeakrTransport,
    TransferNotSent,
    TransferOutcomeUnknown,
    TransferRejected,
)


TOKEN = "test-token"
NOW = datetime(2026, 8, 20, 12, 34, 56, 789000, tzinfo=timezone.utc)


class _ServerState:
    def __init__(
        self,
        *,
        response_status: int = 202,
        response_body: bytes = b'{"id": 7}',
        response_headers: dict[str, str] | None = None,
        drop_after_body: bool = False,
        truncated_response: bool = False,
    ) -> None:
        self.response_status = response_status
        self.response_body = response_body
        self.response_headers = response_headers or {}
        self.drop_after_body = drop_after_body
        self.truncated_response = truncated_response
        self.request_received = Event()
        self.requests: list[dict[str, object]] = []

    def handle(self, request: BaseHTTPRequestHandler) -> None:
        length = int(request.headers.get("Content-Length", "0"))
        body = request.rfile.read(length)
        self.requests.append({
            "method": request.command,
            "path": request.path,
            "headers": {key.lower(): value for key, value in request.headers.items()},
            "body": body,
        })
        self.request_received.set()

        if self.drop_after_body:
            try:
                request.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.connection.close()
            return

        request.send_response(self.response_status)
        for key, value in self.response_headers.items():
            request.send_header(key, value)
        advertised_length = len(self.response_body) + (3 if self.truncated_response else 0)
        request.send_header("Content-Length", str(advertised_length))
        request.end_headers()
        request.wfile.write(self.response_body)
        request.wfile.flush()
        if self.truncated_response:
            request.connection.close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        self.server.state.handle(self)  # type: ignore[attr-defined]

    def do_PATCH(self) -> None:
        self.server.state.handle(self)  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _server(**kwargs: object) -> Iterator[tuple[ThreadingHTTPServer, _ServerState]]:
    state = _ServerState(**kwargs)  # type: ignore[arg-type]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = state  # type: ignore[attr-defined]
    started = Event()

    def serve() -> None:
        started.set()
        server.serve_forever()

    thread = Thread(target=serve, daemon=True)
    thread.start()
    if not started.wait(2):
        raise AssertionError("fake server did not start")
    try:
        yield server, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def _url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_port}"


class _BoundedMedia(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.read_sizes: list[int] = []

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            raise AssertionError("transport attempted an unbounded media read")
        self.read_sizes.append(size)
        return super().read(size)


class _ScriptedResponse:
    def __init__(
        self,
        status: int,
        *,
        declared_length: object,
        chunks: tuple[bytes, ...] = (),
        read_failure: Exception | None = None,
    ) -> None:
        self.status = status
        self.length = declared_length
        self._chunks = list(chunks)
        self._read_failure = read_failure
        self.read_calls = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if self._read_failure is not None:
            raise self._read_failure
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True


class _ScriptedConnection:
    def __init__(self, response: _ScriptedResponse) -> None:
        self.response = response
        self.closed = False

    def connect(self) -> None:
        return

    def putrequest(self, *args: object, **kwargs: object) -> None:
        return

    def putheader(self, *args: object, **kwargs: object) -> None:
        return

    def endheaders(self) -> None:
        return

    def send(self, payload: bytes) -> None:
        return

    def getresponse(self) -> _ScriptedResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


@contextmanager
def _scripted_connection(response: _ScriptedResponse) -> Iterator[_ScriptedConnection]:
    original = speakr_http._connection_for
    connection = _ScriptedConnection(response)
    speakr_http._connection_for = lambda origin, timeout: connection  # type: ignore[assignment]
    try:
        yield connection
    finally:
        speakr_http._connection_for = original


def _multipart_parts(content_type: str, body: bytes) -> dict[str, tuple[str, bytes]]:
    boundary = content_type.split("boundary=", 1)[1].encode("ascii")
    result: dict[str, tuple[str, bytes]] = {}
    for segment in body.split(b"--" + boundary)[1:]:
        if segment in (b"--\r\n", b"--"):
            continue
        header_bytes, value = segment.split(b"\r\n\r\n", 1)
        value = value.removesuffix(b"\r\n")
        headers = header_bytes.decode("utf-8").split("\r\n")
        disposition = next(line for line in headers if line.lower().startswith("content-disposition:"))
        name = disposition.split('name="', 1)[1].split('"', 1)[0]
        result[name] = (disposition, value)
    return result


def test_upload_sends_exact_streamed_multipart_request() -> None:
    media = _BoundedMedia(b"0123456789abcdef")
    media.seek(4)
    with _server() as (server, state):
        transport = StdlibSpeakrTransport(timeout_seconds=2, chunk_size=3)
        assert transport.upload(
            _url(server), TOKEN, media, 16, 'café "clip".mkv', 123456789, NOW,
        ) == 7
        assert media.tell() == 4
        assert media.read_sizes == [3, 3, 3, 3, 3, 1]
        assert state.request_received.is_set()
        request = cast(dict[str, object], state.requests[0])
        assert request["method"] == "POST"
        assert request["path"] == "/api/v1/recordings/upload"
        headers = cast(dict[str, str], request["headers"])
        body = cast(bytes, request["body"])
        assert headers["authorization"] == "Bearer " + TOKEN
        assert headers["content-length"] == str(len(body))
        assert headers["content-type"].startswith("multipart/form-data; boundary=")
        parts = _multipart_parts(headers["content-type"], body)
        assert parts["file"][1] == b"0123456789abcdef"
        assert parts["file_last_modified"][1] == b"123456789"
        assert parts["meeting_date"][1] == b"2026-08-20T12:34:56.789000Z"
        assert "filename*=UTF-8''caf%C3%A9%20%22clip%22.mkv" in parts["file"][0]
        assert "\r" not in parts["file"][0] and "\n" not in parts["file"][0]
        assert max(media.read_sizes) <= 3


def test_upload_accepts_only_positive_integer_id() -> None:
    responses = (
        b"true",
        b'"7"',
        b"{}",
        b"[]",
        b"not-json",
        b'{"id": 0}',
        b'{"id": 7.0}',
    )
    for response_body in responses:
        with _server(response_body=response_body) as (server, _):
            try:
                StdlibSpeakrTransport(timeout_seconds=2).upload(
                    _url(server), TOKEN, io.BytesIO(b"x"), 1, "x.mkv", 0, NOW
                )
            except TransferOutcomeUnknown as exc:
                assert isinstance(exc, InvalidSpeakrResponse)
            else:
                raise AssertionError("invalid upload response was accepted")


def test_upload_rejects_oversized_and_truncated_success_responses() -> None:
    with _server(response_body=b'{"id": 123456789}') as (server, _):
        try:
            StdlibSpeakrTransport(timeout_seconds=2, max_response_bytes=8).upload(
                _url(server), TOKEN, io.BytesIO(b"x"), 1, "x.mkv", 0, NOW
            )
        except TransferOutcomeUnknown:
            pass
        else:
            raise AssertionError("oversized response was accepted")

    with _server(response_body=b'{"id": 7}', truncated_response=True) as (server, _):
        try:
            StdlibSpeakrTransport(timeout_seconds=2).upload(
                _url(server), TOKEN, io.BytesIO(b"x"), 1, "x.mkv", 0, NOW
            )
        except TransferOutcomeUnknown:
            pass
        else:
            raise AssertionError("truncated response was accepted")


def test_complete_non_202_uploads_are_rejected_without_body_exposure() -> None:
    private_body = b"token=test-token notes=private participants=Alice"
    for status in (401, 422, 500):
        with _server(response_status=status, response_body=private_body) as (server, _):
            try:
                StdlibSpeakrTransport(timeout_seconds=2).upload(
                    _url(server), TOKEN, io.BytesIO(b"x"), 1, "private-name.mkv", 0, NOW
                )
            except TransferRejected as exc:
                assert exc.status == status
                assert "test-token" not in str(exc)
                assert "private" not in repr(exc)
            else:
                raise AssertionError("rejected upload was accepted")


def test_known_rejections_survive_oversized_truncated_malformed_and_stalled_bodies() -> None:
    cases = (
        ("oversized", 413, 9, (b"ignored",), None),
        ("truncated", 422, 4, (b"x",), None),
        ("malformed framing", 500, "not-a-length", (), None),
        ("stalled", 503, 1, (), socket.timeout("body stalled")),
    )
    metadata = SpeakrMetadata("Title", NOW, "Notes", "Alice")

    for name, status, declared_length, chunks, read_failure in cases:
        response = _ScriptedResponse(
            status,
            declared_length=declared_length,
            chunks=chunks,
            read_failure=read_failure,
        )
        with _scripted_connection(response) as connection:
            try:
                StdlibSpeakrTransport(timeout_seconds=0.1, max_response_bytes=8).upload(
                    "http://scripted.invalid", TOKEN, io.BytesIO(b"x"), 1, "x.mkv", 0, NOW
                )
            except TransferRejected as exc:
                assert type(exc) is TransferRejected, name
                assert exc.status == status
            else:
                raise AssertionError(f"{name} upload rejection was not classified")
        assert response.read_calls == 0, name
        assert response.closed and connection.closed, name

        response = _ScriptedResponse(
            status,
            declared_length=declared_length,
            chunks=chunks,
            read_failure=read_failure,
        )
        with _scripted_connection(response) as connection:
            try:
                StdlibSpeakrTransport(timeout_seconds=0.1, max_response_bytes=8).patch_metadata(
                    "http://scripted.invalid", TOKEN, 1, metadata
                )
            except MetadataRejected as exc:
                assert type(exc) is MetadataRejected, name
                assert exc.status == status
            else:
                raise AssertionError(f"{name} metadata rejection was not classified")
        assert response.read_calls == 0, name
        assert response.closed and connection.closed, name


def test_connect_failure_is_not_sent_and_drop_after_body_is_unknown() -> None:
    original = http.client.HTTPConnection
    calls: list[str] = []

    class RefusingConnection:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return

        def connect(self) -> None:
            calls.append("connect")
            raise OSError("private network detail")

        def close(self) -> None:
            calls.append("close")

    http.client.HTTPConnection = RefusingConnection  # type: ignore[assignment]
    try:
        try:
            StdlibSpeakrTransport().upload(
                "http://example.invalid", TOKEN, io.BytesIO(b"x"), 1, "x.mkv", 0, NOW
            )
        except TransferNotSent as exc:
            assert calls == ["connect", "close"]
            assert "private network detail" not in str(exc)
        else:
            raise AssertionError("connect failure was not classified")
    finally:
        http.client.HTTPConnection = original

    with _server(drop_after_body=True) as (server, state):
        try:
            StdlibSpeakrTransport(timeout_seconds=2).upload(
                _url(server), TOKEN, io.BytesIO(b"x"), 1, "x.mkv", 0, NOW
            )
        except TransferOutcomeUnknown:
            assert state.request_received.is_set()
        else:
            raise AssertionError("post-body disconnect was classified as a rejection")


def test_redirect_is_rejected_without_following_or_forwarding_token() -> None:
    with _server(
        response_status=302,
        response_body=b"private redirect",
        response_headers={"Location": "http://example.invalid/next"},
    ) as (server, state):
        try:
            StdlibSpeakrTransport(timeout_seconds=2).upload(
                _url(server), TOKEN, io.BytesIO(b"x"), 1, "x.mkv", 0, NOW
            )
        except TransferRejected as exc:
            assert exc.status == 302
            assert len(state.requests) == 1
        else:
            raise AssertionError("redirect was followed")


def test_patch_sends_exact_public_json_and_requires_complete_success() -> None:
    metadata = SpeakrMetadata("Title", NOW, "Notes", "Alice, Bob")
    with _server(response_status=204, response_body=b"") as (server, state):
        assert StdlibSpeakrTransport(timeout_seconds=2).patch_metadata(
            _url(server), TOKEN, 42, metadata
        ) is None
        request = cast(dict[str, object], state.requests[0])
        assert request["method"] == "PATCH"
        assert request["path"] == "/api/v1/recordings/42"
        headers = cast(dict[str, str], request["headers"])
        body = cast(bytes, request["body"])
        assert headers["authorization"] == "Bearer " + TOKEN
        assert headers["content-type"] == "application/json"
        assert headers["content-length"] == str(len(body))
        payload = json.loads(body)
        assert payload == {
            "title": "Title",
            "meeting_date": "2026-08-20T12:34:56.789000Z",
            "notes": "Notes",
            "participants": "Alice, Bob",
        }
        assert "file_last_modified" not in payload

    for status in (401, 422, 500, 302):
        with _server(response_status=status, response_body=b"private metadata") as (server, _):
            try:
                StdlibSpeakrTransport(timeout_seconds=2).patch_metadata(
                    _url(server), TOKEN, 42, metadata
                )
            except MetadataRejected as exc:
                assert exc.status == status
                assert "private metadata" not in repr(exc)
            else:
                raise AssertionError("non-2xx metadata response was accepted")


def test_patch_disconnect_and_truncation_are_unavailable() -> None:
    metadata = SpeakrMetadata("Title", NOW, "Notes", "Alice")
    with _server(drop_after_body=True) as (server, _):
        try:
            StdlibSpeakrTransport(timeout_seconds=2).patch_metadata(
                _url(server), TOKEN, 1, metadata
            )
        except MetadataUnavailable:
            pass
        else:
            raise AssertionError("metadata disconnect was accepted")

    with _server(response_status=200, response_body=b"x", truncated_response=True) as (server, _):
        try:
            StdlibSpeakrTransport(timeout_seconds=2).patch_metadata(
                _url(server), TOKEN, 1, metadata
            )
        except MetadataUnavailable:
            pass
        else:
            raise AssertionError("truncated metadata response was accepted")


def test_validation_and_error_representations_are_sanitized() -> None:
    for kwargs in (
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"chunk_size": 0},
        {"chunk_size": True},
        {"max_response_bytes": 0},
    ):
        try:
            StdlibSpeakrTransport(**kwargs)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:
            raise AssertionError("invalid transport configuration was accepted")

    with _server(response_status=202, response_body=b"not-json token=test-token notes=Notes") as (server, _):
        try:
            StdlibSpeakrTransport(timeout_seconds=2).upload(
                _url(server), TOKEN, io.BytesIO(b"x"), 1, "private-name.mkv", 0, NOW
            )
        except TransferOutcomeUnknown as exc:
            rendered = str(exc) + repr(exc)
            assert "test-token" not in rendered
            assert "private-name" not in rendered
            assert "Notes" not in rendered
        else:
            raise AssertionError("malformed response was accepted")

    for bad_filename, bad_token in (("name\r\nX-Leak: yes", TOKEN), ("name.mkv", "bad\r\ntoken")):
        try:
            StdlibSpeakrTransport().upload(
                "http://127.0.0.1:1", bad_token, io.BytesIO(b"x"), 1, bad_filename, 0, NOW
            )
        except ValueError as exc:
            assert "X-Leak" not in str(exc)
            assert "bad" not in repr(exc).casefold()
        else:
            raise AssertionError("header injection value was accepted")

    try:
        StdlibSpeakrTransport().upload(
            "http://127.0.0.1:1", TOKEN, io.BytesIO(b"x"), 1, "x.mkv", 0,
            datetime(2026, 8, 20),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("naive meeting date was accepted")

    try:
        StdlibSpeakrTransport().patch_metadata(
            "http://127.0.0.1:1", TOKEN, 0, SpeakrMetadata("Title", NOW, "", "")
        )
    except ValueError:
        pass
    else:
        raise AssertionError("invalid recording ID was accepted")
