"""Small, bounded stdlib HTTP transport for the Speakr API."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import math
import secrets
import ssl
from datetime import timezone
from typing import BinaryIO, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit

from .speakr_domain import SpeakrMetadata, normalize_speakr_url


class SpeakrError(Exception):
    """Base class for sanitized transport failures."""

    def __init__(self) -> None:
        super().__init__("Speakr request failed")


class TransferNotSent(SpeakrError):
    """The upload connection failed before request bytes were sent."""

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Speakr upload was not sent",)


class TransferRejected(SpeakrError):
    """Speakr completed an upload request with a non-accepted status."""

    status: int

    def __init__(self, status: int) -> None:
        self.status = _require_http_status(status)
        super().__init__()
        self.args = (f"Speakr upload was rejected (HTTP {self.status})",)


class TransferOutcomeUnknown(SpeakrError):
    """The upload may have reached Speakr, but its result is unknown."""

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Speakr upload outcome is unknown",)


class MetadataRejected(SpeakrError):
    """Speakr completed a metadata request with a non-success status."""

    status: int

    def __init__(self, status: int) -> None:
        self.status = _require_http_status(status)
        super().__init__()
        self.args = (f"Speakr metadata was rejected (HTTP {self.status})",)


class MetadataUnavailable(SpeakrError):
    """The metadata request did not produce a complete response."""

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Speakr metadata result is unavailable",)


class InvalidSpeakrResponse(TransferOutcomeUnknown):
    """A successful upload response was not the required JSON shape."""

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Speakr returned an invalid upload response",)


@runtime_checkable
class SpeakrTransport(Protocol):
    """Transport boundary used by the Speakr publisher."""

    def upload(
        self,
        instance_url: str,
        token: str,
        media: BinaryIO,
        media_size: int,
        filename: str,
        file_last_modified_ms: int,
    ) -> int:
        ...

    def patch_metadata(
        self,
        instance_url: str,
        token: str,
        remote_recording_id: int,
        metadata: SpeakrMetadata,
    ) -> None:
        ...


_UPLOAD_PATH = "/api/v1/recordings/upload"
_MULTIPART_CONTENT_TYPE = "multipart/form-data; boundary="
_BEARER_PREFIX = "Bearer "


@dataclass(frozen=True)
class _Origin:
    scheme: str
    host: str
    port: int


class _ResponseReadError(Exception):
    """Internal marker for an incomplete or over-limit HTTP response."""


def _require_http_status(status: object) -> int:
    if type(status) is not int or not 100 <= status <= 599:
        raise ValueError("Speakr returned an invalid HTTP status")
    return status


def _validate_positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be positive")
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be positive") from None
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError(f"{name} must be positive")
    return converted


def _validate_positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _validate_nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _origin(value: object) -> _Origin:
    try:
        normalized = normalize_speakr_url(value)
        parsed = urlsplit(normalized)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("Speakr instance URL is invalid") from None

    if host is None:
        raise ValueError("Speakr instance URL is invalid")
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return _Origin(parsed.scheme, host, port)


def _validate_token(token: object) -> str:
    if not isinstance(token, str) or not token or any(
        ord(char) < 0x21 or ord(char) > 0x7E for char in token
    ):
        raise ValueError("Speakr token is invalid")
    return token


def _validate_filename(filename: object) -> str:
    if not isinstance(filename, str) or not filename or any(
        ord(char) < 0x20 or ord(char) == 0x7F or char in "\r\n" for char in filename
    ):
        raise ValueError("Speakr filename is invalid")
    return filename


def _filename_parameters(filename: str) -> str:
    # Keep the legacy filename parameter ASCII while retaining Unicode through
    # RFC 5987's filename* parameter.
    fallback = "".join(
        char if 0x20 <= ord(char) <= 0x7E and char not in {'"', "\\", "/"} else "_"
        for char in filename
    ).strip()
    if not fallback:
        fallback = "recording"
    try:
        encoded = quote(filename, safe="!#$&+-.^_`|~")
    except UnicodeError:
        encoded = "recording"
    return f'filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def _multipart_parts(
    filename: str, file_last_modified_ms: int
) -> tuple[bytes, bytes, bytes, bytes, str]:
    boundary = "----meeting-recorder-" + secrets.token_hex(16)
    disposition = _filename_parameters(filename)
    file_prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; {disposition}\r\n'
        "Content-Type: application/octet-stream\r\n"
        "\r\n"
    ).encode("utf-8")
    file_suffix = b"\r\n"
    modified_part = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file_last_modified"\r\n'
        "\r\n"
        f"{file_last_modified_ms}\r\n"
    ).encode("ascii")
    closing = f"--{boundary}--\r\n".encode("ascii")
    return file_prefix, file_suffix, modified_part, closing, boundary


def _read_bounded(response: http.client.HTTPResponse, limit: int) -> bytes:
    body = bytearray()
    try:
        declared_length = getattr(response, "length", None)
        if declared_length is not None and (
            type(declared_length) is not int or declared_length < 0
        ):
            raise _ResponseReadError
        if declared_length is not None and declared_length > limit:
            raise _ResponseReadError

        while len(body) < limit:
            chunk = response.read(limit - len(body))
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise _ResponseReadError
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > limit:
                raise _ResponseReadError

        # A one-byte probe distinguishes an exactly-at-limit body from an
        # oversized body without ever retaining more than the configured cap.
        if len(body) == limit:
            extra = response.read(1)
            if not isinstance(extra, (bytes, bytearray, memoryview)):
                raise _ResponseReadError
            if extra:
                raise _ResponseReadError
        if declared_length is not None and len(body) != declared_length:
            raise _ResponseReadError
    except _ResponseReadError:
        raise
    except Exception:
        raise _ResponseReadError from None
    return bytes(body)


def _connection_for(origin: _Origin, timeout_seconds: float) -> http.client.HTTPConnection:
    if origin.scheme == "https":
        return http.client.HTTPSConnection(
            origin.host,
            origin.port,
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(origin.host, origin.port, timeout=timeout_seconds)


def _response_status(response: http.client.HTTPResponse) -> int:
    try:
        return _require_http_status(response.status)
    except (TypeError, ValueError):
        raise _ResponseReadError from None


def _close_response(response: http.client.HTTPResponse) -> None:
    try:
        response.close()
    except Exception:
        pass


class StdlibSpeakrTransport:
    """Bounded HTTP transport with no redirect or third-party dependencies."""

    def __init__(
        self,
        timeout_seconds: float = 60,
        chunk_size: int = 1024 * 1024,
        max_response_bytes: int = 1024 * 1024,
    ) -> None:
        self.timeout_seconds = _validate_positive_number(timeout_seconds, "timeout")
        self.chunk_size = _validate_positive_int(chunk_size, "chunk size")
        self.max_response_bytes = _validate_positive_int(max_response_bytes, "response limit")

    def upload(
        self,
        instance_url: str,
        token: str,
        media: BinaryIO,
        media_size: int,
        filename: str,
        file_last_modified_ms: int,
    ) -> int:
        origin = _origin(instance_url)
        safe_token = _validate_token(token)
        safe_filename = _validate_filename(filename)
        media_size = _validate_nonnegative_int(media_size, "media size")
        file_last_modified_ms = _validate_nonnegative_int(
            file_last_modified_ms, "file last modified time"
        )
        initial_position = self._prepare_media(media)
        file_prefix, file_suffix, modified_part, closing, boundary = _multipart_parts(
            safe_filename, file_last_modified_ms
        )
        content_length = (
            len(file_prefix) + media_size + len(file_suffix) + len(modified_part) + len(closing)
        )

        connection: http.client.HTTPConnection | None = None
        try:
            try:
                connection = _connection_for(origin, self.timeout_seconds)
                connection.connect()
            except Exception:
                raise TransferNotSent from None

            try:
                connection.putrequest("POST", _UPLOAD_PATH, skip_accept_encoding=True)
                connection.putheader("Authorization", _BEARER_PREFIX + safe_token)
                connection.putheader(
                    "Content-Type", _MULTIPART_CONTENT_TYPE + boundary
                )
                connection.putheader("Content-Length", str(content_length))
                connection.putheader("Connection", "close")
            except Exception:
                raise TransferNotSent from None

            try:
                connection.endheaders()
                self._send_bytes(connection, file_prefix)
                self._send_media(connection, media, media_size)
                self._send_bytes(connection, file_suffix)
                self._send_bytes(connection, modified_part)
                self._send_bytes(connection, closing)
                response = connection.getresponse()
                status = _response_status(response)
            except _ResponseReadError:
                raise TransferOutcomeUnknown from None
            except SpeakrError:
                raise
            except Exception:
                raise TransferOutcomeUnknown from None

            # Classify a completed rejection before touching its untrusted body;
            # rejection status must survive bad framing, truncation, and stalls.
            if status != 202:
                _close_response(response)
                raise TransferRejected(status)

            try:
                body = _read_bounded(response, self.max_response_bytes)
            except _ResponseReadError:
                raise TransferOutcomeUnknown from None

            return self._recording_id(body)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self._restore_media(media, initial_position)

    def patch_metadata(
        self,
        instance_url: str,
        token: str,
        remote_recording_id: int,
        metadata: SpeakrMetadata,
    ) -> None:
        origin = _origin(instance_url)
        safe_token = _validate_token(token)
        if type(remote_recording_id) is not int or remote_recording_id <= 0:
            raise ValueError("Speakr recording ID is invalid")
        if not isinstance(metadata, SpeakrMetadata):
            raise ValueError("Speakr metadata is invalid")

        # Emit UTC explicitly so the API never has to infer a local timezone.
        meeting_date = metadata.meeting_date.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        payload = {
            "title": metadata.title,
            "meeting_date": meeting_date,
            "notes": metadata.notes,
            "participants": metadata.participants,
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        path = f"/api/v1/recordings/{remote_recording_id}"

        connection: http.client.HTTPConnection | None = None
        try:
            try:
                connection = _connection_for(origin, self.timeout_seconds)
                connection.connect()
            except Exception:
                raise MetadataUnavailable from None

            try:
                connection.putrequest("PATCH", path, skip_accept_encoding=True)
                connection.putheader("Authorization", _BEARER_PREFIX + safe_token)
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(body)))
                connection.putheader("Connection", "close")
                connection.endheaders()
                self._send_bytes(connection, body)
                response = connection.getresponse()
                status = _response_status(response)
            except _ResponseReadError:
                raise MetadataUnavailable from None
            except Exception:
                raise MetadataUnavailable from None

            # Preserve a known HTTP rejection without reading a body that may
            # be oversized, truncated, malformed, or indefinitely stalled.
            if not 200 <= status <= 299:
                _close_response(response)
                raise MetadataRejected(status)

            try:
                _read_bounded(response, self.max_response_bytes)
            except _ResponseReadError:
                raise MetadataUnavailable from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _prepare_media(self, media: BinaryIO) -> int:
        try:
            position = media.tell()
            media.seek(0)
        except Exception:
            raise ValueError("Speakr media must be seekable") from None
        if type(position) is not int or position < 0:
            raise ValueError("Speakr media position is invalid")
        return position

    def _restore_media(self, media: BinaryIO, position: int) -> None:
        try:
            media.seek(position)
        except Exception:
            pass

    def _send_bytes(self, connection: http.client.HTTPConnection, payload: bytes) -> None:
        for offset in range(0, len(payload), self.chunk_size):
            connection.send(payload[offset:offset + self.chunk_size])

    def _send_media(
        self, connection: http.client.HTTPConnection, media: BinaryIO, media_size: int
    ) -> None:
        remaining = media_size
        while remaining:
            requested = min(self.chunk_size, remaining)
            chunk = media.read(requested)
            if not isinstance(chunk, (bytes, bytearray, memoryview)) or not chunk:
                raise TransferOutcomeUnknown
            if len(chunk) > requested:
                raise TransferOutcomeUnknown
            connection.send(chunk)
            remaining -= len(chunk)

    @staticmethod
    def _recording_id(body: bytes) -> int:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            raise InvalidSpeakrResponse from None
        if not isinstance(payload, dict) or type(payload.get("id")) is not int:
            raise InvalidSpeakrResponse
        recording_id = payload["id"]
        if recording_id <= 0:
            raise InvalidSpeakrResponse
        return recording_id


__all__ = [
    "InvalidSpeakrResponse",
    "MetadataRejected",
    "MetadataUnavailable",
    "SpeakrError",
    "SpeakrTransport",
    "StdlibSpeakrTransport",
    "TransferNotSent",
    "TransferOutcomeUnknown",
    "TransferRejected",
]
