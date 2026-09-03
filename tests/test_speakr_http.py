"""Deterministic protocol tests for the stdlib Speakr transport."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
from threading import Event, Thread
from typing import Iterator, cast
from urllib.parse import parse_qs, urlsplit

import meeting_recorder.speakr_http as speakr_http
from meeting_recorder.speakr_domain import SpeakrMetadata
from meeting_recorder.speakr_domain import Tag
from tests.speakr_fake_server import fake_speakr_server, multipart_parts
from meeting_recorder.speakr_http import (
    InvalidSpeakrResponse,
    MetadataRejected,
    MetadataUnavailable,
    ReconciliationRejected,
    ReconciliationUnavailable,
    InvalidTagCatalog,
    SpeakrHTTPError,
    StdlibSpeakrTransport,
    TransferNotSent,
    TransferOutcomeUnknown,
    TransferRejected,
    TagDiscoveryRejected,
    TagDiscoveryUnavailable,
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
        # Send the temporary title through the same bounded multipart path.
        assert transport.upload(
            _url(server), TOKEN, media, 16, 'café "clip".mkv', 123456789, NOW,
            title="Temporary title",
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
        # Confirm the new scalar fields and the existing streamed file fields.
        assert parts["file"][1] == b"0123456789abcdef"
        assert parts["title"][1] == "Temporary title".encode()
        assert parts["file_last_modified"][1] == b"123456789"
        assert parts["meeting_date"][1] == b"2026-08-20T12:34:56.789000Z"
        assert parts["keep_audio_only"][1] == b"false"
        assert "filename*=UTF-8''caf%C3%A9%20%22clip%22.mkv" in parts["file"][0]
        assert "\r" not in parts["file"][0] and "\n" not in parts["file"][0]
        assert max(media.read_sizes) <= 3


def test_upload_emits_only_contiguous_ordered_tag_fields() -> None:
    # Exercise empty, one, and multiple selections through the real multipart stream.
    for tag_ids, expected in (((), {}), ((9,), {"tag_ids[0]": b"9"}), ((9, 3), {"tag_ids[0]": b"9", "tag_ids[1]": b"3"})):
        with _server() as (server, state):
            StdlibSpeakrTransport(timeout_seconds=2).upload(
                _url(server), TOKEN, io.BytesIO(b"x"), 1, "x.mkv", 0, NOW,
                tag_ids=tag_ids,
            )
            request = cast(dict[str, object], state.requests[0])
            headers = cast(dict[str, str], request["headers"])
            parts = _multipart_parts(headers["content-type"], cast(bytes, request["body"]))
            actual = {name: value for name, (_, value) in parts.items() if name.startswith("tag_ids[")}
            assert actual == expected


def test_tag_discovery_uses_exact_path_auth_order_and_short_timeout() -> None:
    # Preserve the API's personal-then-group ordering instead of sorting locally.
    with fake_speakr_server(tag_pages=({"tags": [{"id": 9, "name": "Zulu"}, {"id": 2, "name": "Alpha"}]},)) as (url, state):
        transport = StdlibSpeakrTransport(timeout_seconds=60)
        assert transport.list_tags(url, TOKEN) == (
            Tag(9, "Zulu"), Tag(2, "Alpha"),
        )
        request = state.requests[0]
        assert request.method == "GET" and request.path == "/api/v1/tags"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers["accept"] == "application/json"
        assert transport.tag_timeout_seconds == 5


def test_tag_discovery_has_sanitized_transient_and_contract_failures() -> None:
    # Keep HTTP rejections typed so callers can choose cache fallback correctly.
    for status, expected in ((401, "auth"), (429, "rate_limited"), (503, "server")):
        with fake_speakr_server(tag_statuses=(status,), tag_pages=(b"private token=test-token",)) as (url, _):
            try:
                StdlibSpeakrTransport(timeout_seconds=2).list_tags(url, TOKEN)
            except TagDiscoveryRejected as exc:
                assert exc.classification == expected
                assert "test-token" not in repr(exc)
            else:
                raise AssertionError("tag rejection was accepted")

    for payload, response_limit in (
        (b"not-json", 8),
        (b'{"tags":[{"id":0,"name":"private"}]}', 1024),
        (b'{"tags":[{"id":1,"name":"  "}]}', 1024),
        (b"x" * 20, 8),
    ):
        with fake_speakr_server(tag_pages=(payload,)) as (url, _):
            try:
                StdlibSpeakrTransport(timeout_seconds=2, max_response_bytes=response_limit).list_tags(url, TOKEN)
            except InvalidTagCatalog as exc:
                assert "private" not in repr(exc)
            else:
                raise AssertionError("invalid tag catalog was accepted")

    # Network failure stays transient and does not expose the socket detail.
    response = _ScriptedResponse(200, declared_length=0)
    with _scripted_connection(response) as connection:
        connection.connect = lambda: (_ for _ in ()).throw(socket.timeout("private timeout"))  # type: ignore[method-assign]
        try:
            StdlibSpeakrTransport().list_tags("http://scripted.invalid", TOKEN)
        except TagDiscoveryUnavailable as exc:
            assert exc.is_transient and "private timeout" not in repr(exc)
        else:
            raise AssertionError("tag timeout was accepted")


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


def test_reconciliation_returns_exact_zero_one_or_multiple_matches() -> None:
    # Script two pages so matching also exercises the pagination contract.
    pages = (
        {
            "recordings": [
                {"id": 1, "title": "[mr:abc] first"},
                {"id": 2, "title": "before [mr:abc] first"},
            ],
            "page": 1,
            "pages": 2,
        },
        {
            "recordings": [
                {"id": 3, "title": "[mr:abc] second"},
                {"id": 4, "title": "[mr:abcd] not exact"},
            ],
            "page": 2,
            "pages": 2,
        },
    )
    with fake_speakr_server(recording_pages=pages) as (url, state):
        # Reconcile locally after the server's substring search returns pages.
        result = StdlibSpeakrTransport(timeout_seconds=2).reconcile_recordings(
            url, TOKEN, "abc",
        )
        assert result == (1, 3)
        assert len(state.requests) == 2

        # Confirm both requests retained the bounded query and Bearer header.
        for request in state.requests:
            assert request.method == "GET"
            query = parse_qs(urlsplit(request.path).query)
            assert query["q"] == ["[mr:abc]"]
            assert query["per_page"] == ["100"]
            assert request.headers["authorization"] == "Bearer " + TOKEN

    # Check that the engine receives exact zero and one-match tuples as well.
    for title, expected in (
        ("not a match", ()),
        ("[mr:abc] one", (7,)),
    ):
        with fake_speakr_server(
            recording_pages=({"recordings": [{"id": 7, "title": title}]},),
        ) as (url, _):
            assert StdlibSpeakrTransport(timeout_seconds=2).find_recording_ids(
                url, TOKEN, "abc",
            ) == expected


def test_reconciliation_rejects_unsafe_marker_and_bounds_pages_items_and_body(
) -> None:
    # Reject marker input that could change the server-side search semantics.
    for marker in ("abc_def", "abc%def", "abc/def", ""):
        try:
            StdlibSpeakrTransport().reconcile_recordings(
                "http://127.0.0.1:1", TOKEN, marker,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe reconciliation marker was accepted")

    # Prepare a response that advertises a second page.
    pages = (
        {
            "recordings": [{"id": 1, "title": "[mr:abc] one"}],
            "page": 1,
            "pages": 2,
        },
        {
            "recordings": [{"id": 2, "title": "[mr:abc] two"}],
            "page": 2,
            "pages": 2,
        },
    )
    with fake_speakr_server(recording_pages=pages) as (url, _):
        # A configured page cap must fail instead of returning a partial set.
        try:
            transport = StdlibSpeakrTransport(
                timeout_seconds=2, max_reconciliation_pages=1,
            )
            transport.reconcile_recordings(
                url, TOKEN, "abc",
            )
        except ReconciliationUnavailable:
            pass
        else:
            raise AssertionError("page bound was ignored")

    with fake_speakr_server(
        recording_pages=(
            {"recordings": [
                {"id": 1, "title": "[mr:abc] one"},
                {"id": 2, "title": "other"},
            ]},
        ),
    ) as (url, _):
        # The item cap counts unmatched records too.
        try:
            transport = StdlibSpeakrTransport(
                timeout_seconds=2, max_reconciliation_items=1,
            )
            transport.reconcile_recordings(
                url, TOKEN, "abc",
            )
        except ReconciliationUnavailable:
            pass
        else:
            raise AssertionError("item bound was ignored")

    with fake_speakr_server(
        recording_pages=(b'{"recordings": [',),
    ) as (url, _):
        # Invalid JSON must not become a partial result.
        try:
            StdlibSpeakrTransport(timeout_seconds=2).reconcile_recordings(
                url, TOKEN, "abc",
            )
        except ReconciliationUnavailable:
            pass
        else:
            raise AssertionError("malformed reconciliation body was accepted")

    with fake_speakr_server(recording_pages=(b"x" * 20,)) as (url, _):
        # The response byte cap must reject an oversized body before decoding.
        try:
            StdlibSpeakrTransport(
                timeout_seconds=2, max_response_bytes=8,
            ).reconcile_recordings(
                url, TOKEN, "abc",
            )
        except ReconciliationUnavailable:
            pass
        else:
            raise AssertionError("oversized reconciliation body was accepted")


def test_reconciliation_http_failures_are_typed_sanitized_and_retryable(
) -> None:
    # Use a fixed clock so both Retry-After formats have deterministic results.
    retry_clock = NOW.replace(microsecond=0)
    future = format_datetime(retry_clock + timedelta(seconds=37), usegmt=True)
    cases = (
        (401, {}, "auth", None),
        (422, {}, "permanent", None),
        (503, {"Retry-After": "999999"}, "server", 21600.0),
        (429, {"Retry-After": "7"}, "rate_limited", 7.0),
        (429, {"Retry-After": future}, "rate_limited", 37.0),
    )
    for status, headers, classification, retry_after in cases:
        # Return a private body to prove typed failures retain no raw text.
        with fake_speakr_server(
            recording_statuses=(status,), recording_headers=(headers,),
            recording_pages=(b"private token=secret",),
        ) as (url, _):
            # Exercise only the status path; rejection bodies must not be read.
            try:
                StdlibSpeakrTransport(
                    timeout_seconds=2, clock=lambda: retry_clock,
                ).reconcile_recordings(
                    url, TOKEN, "abc",
                )
            except ReconciliationRejected as exc:
                assert isinstance(exc, SpeakrHTTPError)
                assert exc.classification == classification
                assert exc.retry_after == retry_after
                assert "secret" not in str(exc) and TOKEN not in repr(exc)
            else:
                raise AssertionError(
                    "HTTP reconciliation failure was accepted",
                )


def test_fake_server_integration_covers_temporary_title_and_reconciliation(
) -> None:
    # Exercise upload and reconciliation against the same local HTTP fixture.
    with fake_speakr_server(
        recording_pages=(
            {"recordings": [{"id": 42, "title": "[mr:abc] upload"}]},
        ),
    ) as (url, state):
        transport = StdlibSpeakrTransport(timeout_seconds=2)
        # Upload the marker, then use it for exact reconciliation.
        assert transport.upload(
            url, TOKEN, io.BytesIO(b"x"), 1, "x.mkv", 0, NOW,
            title="[mr:abc] upload",
        ) == 42
        assert transport.reconcile_recordings(url, TOKEN, "abc") == (42,)
        # Inspect the recorded upload without changing the transport assertion.
        upload_request = next(
            request for request in state.requests if request.method == "POST"
        )
        parts = multipart_parts(upload_request)
        assert parts["title"][1] == b"[mr:abc] upload"
        assert parts["keep_audio_only"][1] == b"false"
