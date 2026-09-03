"""Small, bounded stdlib HTTP transport for the Speakr API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import http.client
import json
import math
import secrets
import ssl
from typing import BinaryIO, Callable, Protocol, Sequence, runtime_checkable
from urllib.parse import parse_qs, quote, urlsplit

from .speakr_domain import SpeakrMetadata, Tag, normalize_speakr_url


class SpeakrError(Exception):
    """Base class for sanitized transport failures."""

    def __init__(self) -> None:
        super().__init__("Speakr request failed")


class SpeakrHTTPError(SpeakrError):
    """A sanitized HTTP rejection with a bounded retry hint."""

    status: int
    retry_after: float | None
    classification: str

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        self.status = _require_http_status(status)
        self.retry_after = _validate_retry_after_value(retry_after)
        self.classification = _http_classification(self.status)
        super().__init__()

    @property
    def category(self) -> str:
        """Return the normalized failure category."""
        return self.classification

    @property
    def http_status(self) -> int:
        return self.status

    @property
    def is_transient(self) -> bool:
        return self.classification in {"rate_limited", "server"}

    @property
    def transient(self) -> bool:
        return self.is_transient

    @property
    def is_permanent(self) -> bool:
        return self.classification == "permanent"

    @property
    def retry_after_seconds(self) -> float | None:
        return self.retry_after


class TransferNotSent(SpeakrError):
    """The upload connection failed before request bytes were sent."""

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Speakr upload was not sent",)


class TransferRejected(SpeakrHTTPError):
    """Speakr completed an upload request with a non-accepted status."""

    status: int

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__(status, retry_after)
        self.args = (f"Speakr upload was rejected (HTTP {self.status})",)


class TransferOutcomeUnknown(SpeakrError):
    """The upload may have reached Speakr, but its result is unknown."""

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Speakr upload outcome is unknown",)


class MetadataRejected(SpeakrHTTPError):
    """Speakr completed a metadata request with a non-success status."""

    status: int

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__(status, retry_after)
        self.args = (f"Speakr metadata was rejected (HTTP {self.status})",)


class MetadataUnavailable(SpeakrError):
    """The metadata request did not produce a complete response."""

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Speakr metadata result is unavailable",)


class ReconciliationRejected(SpeakrHTTPError):
    """Speakr completed a reconciliation request with a non-success status."""

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__(status, retry_after)
        self.args = (
            f"Speakr reconciliation was rejected (HTTP {self.status})",
        )


class ReconciliationUnavailable(SpeakrError):
    """The reconciliation response was unavailable or outside safe bounds."""

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Speakr reconciliation result is unavailable",)


class TagDiscoveryRejected(SpeakrHTTPError):
    """Speakr completed tag discovery with a non-success status."""

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__(status, retry_after)
        self.args = (f"Speakr tag discovery was rejected (HTTP {self.status})",)


class TagDiscoveryUnavailable(SpeakrError):
    """Tag discovery could not obtain a complete response from Speakr."""

    classification = "network"
    is_transient = True
    is_permanent = False

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Speakr tag discovery is unavailable",)


class InvalidTagCatalog(SpeakrError):
    """A successful tag response did not meet the pinned API contract."""

    classification = "contract"
    is_transient = False
    is_permanent = True

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Speakr returned an invalid tag catalog",)


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
        meeting_date: datetime,
        title: str | None = None,
        tag_ids: Sequence[int] = (),
    ) -> int:
        ...

    def list_tags(self, instance_url: str, token: str) -> tuple[Tag, ...]:
        ...

    def patch_metadata(
        self,
        instance_url: str,
        token: str,
        remote_recording_id: int,
        metadata: SpeakrMetadata,
    ) -> None:
        ...

    def reconcile_recordings(
        self,
        instance_url: str,
        token: str,
        marker_token: str,
    ) -> tuple[int, ...]:
        ...


_UPLOAD_PATH = "/api/v1/recordings/upload"
_RECORDINGS_PATH = "/api/v1/recordings"
_TAGS_PATH = "/api/v1/tags"
_MULTIPART_CONTENT_TYPE = "multipart/form-data; boundary="
_BEARER_PREFIX = "Bearer "
_MAX_TITLE_CHARS = 4096
_MAX_MARKER_TOKEN_CHARS = 128
_MAX_RETRY_AFTER_SECONDS = 21600.0
_DEFAULT_RECONCILIATION_PAGE_SIZE = 100
_DEFAULT_RECONCILIATION_PAGES = 16
_DEFAULT_RECONCILIATION_ITEMS = 1000
_DEFAULT_TAG_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class _Origin:
    scheme: str
    host: str
    port: int


class _ResponseReadError(Exception):
    """Internal marker for an incomplete or over-limit HTTP response."""


def _require_http_status(status: object) -> int:
    """Accept only an HTTP status that can be safely stored in an error."""
    # Reject malformed status values before they reach public error objects.
    if type(status) is not int or not 100 <= status <= 599:
        raise ValueError("Speakr returned an invalid HTTP status")
    return status


def _http_classification(status: int) -> str:
    """Map a status to the retry category used by the engine."""
    # Keep authentication failures separate from retryable service failures.
    if status in {401, 403}:
        return "auth"
    if status == 429:
        return "rate_limited"
    if status == 408 or 500 <= status <= 599:
        return "server"
    return "permanent"


def _validate_retry_after_value(value: object) -> float | None:
    """Validate and cap an already normalized retry delay."""
    # Preserve an absent server hint as an absent retry schedule.
    if value is None:
        return None

    # Reject values that could not have come from the bounded parser.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Speakr retry delay is invalid")

    # Convert numeric input once so non-finite values cannot bypass the cap.
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        raise ValueError("Speakr retry delay is invalid") from None
    # Reject negative and non-finite delays before storing them.
    if not math.isfinite(converted) or converted < 0:
        raise ValueError("Speakr retry delay is invalid")

    # Apply the fixed six-hour limit used by all typed HTTP failures.
    return min(_MAX_RETRY_AFTER_SECONDS, converted)


def _parse_retry_after(
    value: object, clock: Callable[[], datetime],
) -> float | None:
    """Normalize Retry-After without retaining its untrusted text."""
    # Ignore headers with a type that cannot contain a valid retry hint.
    if not isinstance(value, str):
        return None
    candidate = value.strip()

    # Bound header parsing before handing text to date parsing.
    if not candidate or len(candidate) > 128:
        return None

    # RFC 9110 delta-seconds are non-negative decimal integers.  Rejecting
    # signs and fractional values avoids surprising retry behavior.
    if candidate.isdecimal():
        try:
            return min(_MAX_RETRY_AFTER_SECONDS, float(int(candidate)))
        except (OverflowError, ValueError):
            return _MAX_RETRY_AFTER_SECONDS

    # Parse HTTP-date values against the caller's operation clock.
    try:
        target = parsedate_to_datetime(candidate)
        if target.tzinfo is None or target.utcoffset() is None:
            target = target.replace(tzinfo=timezone.utc)
        now = clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            return None
        delay = (target - now.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    # Treat an expired date as ready now, while rejecting non-finite results.
    if not math.isfinite(delay):
        return None

    # Keep future server hints within the same fixed cap as delta-seconds.
    return min(_MAX_RETRY_AFTER_SECONDS, max(0.0, delay))


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


def _validate_tag_ids(value: object) -> tuple[int, ...]:
    """Validate ordered upload-time tag IDs without accepting text iterables."""
    # Materialize the caller's ordered selection before opening a connection.
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        raise ValueError("Speakr tag IDs are invalid")
    try:
        tag_ids = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError("Speakr tag IDs are invalid") from None

    # Keep each value suitable for Speakr's integer multipart parser.
    if any(type(tag_id) is not int or tag_id <= 0 for tag_id in tag_ids):
        raise ValueError("Speakr tag IDs are invalid")
    return tag_ids


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


def _validate_marker_token(token: object) -> str:
    """Validate the engine marker token used in the search query and prefix."""
    # Permit only URL-safe marker characters so LIKE wildcards cannot enter q.
    if (
        not isinstance(token, str)
        or not token
        or len(token) > _MAX_MARKER_TOKEN_CHARS
        or any(
            not (char.isascii() and (char.isalnum() or char == "-"))
            for char in token
        )
    ):
        raise ValueError("Speakr reconciliation token is invalid")
    return token


def _validate_filename(filename: object) -> str:
    if not isinstance(filename, str) or not filename or any(
        ord(char) < 0x20 or ord(char) == 0x7F or char in "\r\n" for char in filename
    ):
        raise ValueError("Speakr filename is invalid")
    return filename


def _validate_title(title: object) -> str:
    """Validate a title before placing it in a multipart text field."""
    # Reject empty and control-containing titles before multipart encoding.
    if (
        not isinstance(title, str)
        or not title
        or len(title) > _MAX_TITLE_CHARS
        or any(
            ord(char) < 0x20 or ord(char) == 0x7F
            for char in title
        )
    ):
        raise ValueError("Speakr title is invalid")
    return title


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


def _validate_meeting_date(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Speakr meeting date must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_timestamp(value: datetime) -> str:
    return _validate_meeting_date(value).isoformat().replace("+00:00", "Z")


def _multipart_parts(
    filename: str,
    title: str,
    file_last_modified_ms: int,
    meeting_date: datetime,
    tag_ids: tuple[int, ...],
) -> tuple[
    bytes, bytes, bytes, bytes, bytes, bytes, bytes, bytes, str,
]:
    """Build the fixed multipart sections for one bounded upload."""
    # Create one boundary and preserve the existing filename encoding rules.
    boundary = "----meeting-recorder-" + secrets.token_hex(16)
    disposition = _filename_parameters(filename)

    # Encode the streamed file part before the scalar form fields.
    file_prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; {disposition}\r\n'
        "Content-Type: application/octet-stream\r\n"
        "\r\n"
    ).encode("utf-8")
    file_suffix = b"\r\n"

    # Include the temporary title supplied by the publication engine.
    title_part = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="title"\r\n'
        "\r\n"
        f"{title}\r\n"
    ).encode("utf-8")

    # Preserve the API's existing file timestamp field.
    modified_part = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file_last_modified"\r\n'
        "\r\n"
        f"{file_last_modified_ms}\r\n"
    ).encode("ascii")

    # Send the meeting date explicitly in UTC.
    meeting_date_part = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="meeting_date"\r\n'
        "\r\n"
        f"{_utc_timestamp(meeting_date)}\r\n"
    ).encode("ascii")

    # Explicitly keep the original media instead of audio-only conversion.
    keep_audio_only_part = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="keep_audio_only"\r\n'
        "\r\n"
        "false\r\n"
    ).encode("ascii")

    # Number tags from zero without gaps because Speakr stops at a missing index.
    tag_parts = b"".join(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="tag_ids[{index}]"\r\n'
            "\r\n"
            f"{tag_id}\r\n"
        ).encode("ascii")
        for index, tag_id in enumerate(tag_ids)
    )

    # Close the multipart body after all fields have been emitted.
    closing = f"--{boundary}--\r\n".encode("ascii")
    return (
        file_prefix, file_suffix, title_part, modified_part, meeting_date_part,
        keep_audio_only_part, tag_parts, closing, boundary,
    )


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


def _response_retry_after(
    response: http.client.HTTPResponse, clock: Callable[[], datetime],
) -> float | None:
    """Read only the one bounded response hint needed by typed HTTP errors."""
    # Ignore response implementations that cannot expose a header safely.
    try:
        value = response.getheader("Retry-After")
    except Exception:
        return None
    return _parse_retry_after(value, clock)


class StdlibSpeakrTransport:
    """Bounded HTTP transport with no redirect or third-party dependencies."""

    def __init__(
        self,
        timeout_seconds: float = 60,
        chunk_size: int = 1024 * 1024,
        max_response_bytes: int = 1024 * 1024,
        max_reconciliation_pages: int = _DEFAULT_RECONCILIATION_PAGES,
        max_reconciliation_items: int = _DEFAULT_RECONCILIATION_ITEMS,
        reconciliation_page_size: int = _DEFAULT_RECONCILIATION_PAGE_SIZE,
        tag_timeout_seconds: float = _DEFAULT_TAG_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # Validate all transport and reconciliation bounds before storing them.
        self.timeout_seconds = _validate_positive_number(timeout_seconds, "timeout")
        self.chunk_size = _validate_positive_int(chunk_size, "chunk size")
        self.max_response_bytes = _validate_positive_int(max_response_bytes, "response limit")
        self.max_reconciliation_pages = _validate_positive_int(
            max_reconciliation_pages, "reconciliation page limit",
        )
        self.max_reconciliation_items = _validate_positive_int(
            max_reconciliation_items, "reconciliation item limit",
        )
        self.reconciliation_page_size = _validate_positive_int(
            reconciliation_page_size, "reconciliation page size",
        )
        self.tag_timeout_seconds = _validate_positive_number(
            tag_timeout_seconds, "tag timeout",
        )

        # Accept a clock only when it can supply operation-scoped HTTP dates.
        if clock is not None and not callable(clock):
            raise ValueError("Speakr clock is invalid")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def upload(
        self,
        instance_url: str,
        token: str,
        media: BinaryIO,
        media_size: int,
        filename: str,
        file_last_modified_ms: int,
        meeting_date: datetime,
        title: str | None = None,
        tag_ids: Sequence[int] = (),
    ) -> int:
        # Validate endpoint, credentials, and upload metadata before opening media.
        origin = _origin(instance_url)
        safe_token = _validate_token(token)
        safe_filename = _validate_filename(filename)
        safe_title = safe_filename if title is None else _validate_title(title)
        media_size = _validate_nonnegative_int(media_size, "media size")
        file_last_modified_ms = _validate_nonnegative_int(
            file_last_modified_ms, "file last modified time"
        )
        meeting_date = _validate_meeting_date(meeting_date)
        safe_tag_ids = _validate_tag_ids(tag_ids)

        # Rewind seekable media and build the bounded multipart sections.
        initial_position = self._prepare_media(media)
        (
            file_prefix,
            file_suffix,
            title_part,
            modified_part,
            meeting_date_part,
            keep_audio_only_part,
            tag_parts,
            closing,
            boundary,
        ) = _multipart_parts(
            safe_filename, safe_title, file_last_modified_ms, meeting_date,
            safe_tag_ids,
        )

        # Compute the exact request length before sending any bytes.
        content_length = (
            len(file_prefix) + media_size + len(file_suffix)
            + len(modified_part) + len(title_part)
            + len(meeting_date_part) + len(keep_audio_only_part)
            + len(tag_parts)
            + len(closing)
        )

        connection: http.client.HTTPConnection | None = None
        try:
            # Connect first so failures are known to have sent no upload bytes.
            try:
                connection = _connection_for(origin, self.timeout_seconds)
                connection.connect()
            except Exception:
                raise TransferNotSent from None

            # Set fixed headers without enabling redirects or implicit encoding.
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

            # Stream each multipart section and the media without buffering it.
            try:
                connection.endheaders()
                self._send_bytes(connection, file_prefix)
                self._send_media(connection, media, media_size)
                self._send_bytes(connection, file_suffix)
                self._send_bytes(connection, title_part)
                self._send_bytes(connection, modified_part)
                self._send_bytes(connection, meeting_date_part)
                self._send_bytes(connection, keep_audio_only_part)
                self._send_bytes(connection, tag_parts)
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
                retry_after = _response_retry_after(response, self._clock)
                _close_response(response)
                raise TransferRejected(status, retry_after)

            # Read only the bounded success body needed to obtain the ID.
            try:
                body = _read_bounded(response, self.max_response_bytes)
            except _ResponseReadError:
                raise TransferOutcomeUnknown from None

            return self._recording_id(body)
        finally:
            # Close the connection and restore the caller's media position.
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
        # Validate endpoint, credentials, remote identity, and metadata first.
        origin = _origin(instance_url)
        safe_token = _validate_token(token)
        if type(remote_recording_id) is not int or remote_recording_id <= 0:
            raise ValueError("Speakr recording ID is invalid")
        if not isinstance(metadata, SpeakrMetadata):
            raise ValueError("Speakr metadata is invalid")

        # Emit UTC explicitly so the API never has to infer a local timezone.
        meeting_date = _utc_timestamp(metadata.meeting_date)

        # Build the public metadata payload without retaining response text.
        payload = {
            "title": metadata.title,
            "meeting_date": meeting_date,
            "notes": metadata.notes,
            "participants": metadata.participants,
        }

        # Encode the bounded JSON request body deterministically.
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        path = f"/api/v1/recordings/{remote_recording_id}"

        connection: http.client.HTTPConnection | None = None
        try:
            # Connect before sending so connection failures stay unavailable.
            try:
                connection = _connection_for(origin, self.timeout_seconds)
                connection.connect()
            except Exception:
                raise MetadataUnavailable from None

            # Send only the fixed PATCH request and its bounded JSON body.
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
                retry_after = _response_retry_after(response, self._clock)
                _close_response(response)
                raise MetadataRejected(status, retry_after)

            # Drain a successful response within the same body bound.
            try:
                _read_bounded(response, self.max_response_bytes)
            except _ResponseReadError:
                raise MetadataUnavailable from None
        finally:
            # Release the connection after either success or typed failure.
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def reconcile_recordings(
        self,
        instance_url: str,
        token: str,
        marker_token: str,
    ) -> tuple[int, ...]:
        """Return IDs carrying one exact engine-owned marker prefix."""
        # Validate the origin and both credentials before constructing q.
        origin = _origin(instance_url)
        safe_token = _validate_token(token)
        safe_marker = _validate_marker_token(marker_token)

        # Search for the marker text, then verify the exact prefix locally.
        marker_prefix = f"[mr:{safe_marker}] "
        query = quote(f"[mr:{safe_marker}]", safe="")
        matched: list[int] = []
        seen_pages: set[int] = set()
        page = 1
        items_seen = 0

        # Keep every page request on the configured origin; server-provided
        # next links are reduced to a validated page number before requesting,
        # preventing redirects or credential forwarding to an untrusted host.
        for _ in range(self.max_reconciliation_pages):
            # Refuse repeated pages before another authenticated request.
            if page in seen_pages:
                raise ReconciliationUnavailable
            seen_pages.add(page)

            # Rebuild each request locally on the configured origin.
            path = (
                f"{_RECORDINGS_PATH}?q={query}&page={page}"
                f"&per_page={self.reconciliation_page_size}"
            )
            payload = self._get_reconciliation_page(
                origin, safe_token, path,
            )

            # Parse one bounded page before adding any IDs to the result.
            (
                page_ids,
                next_page,
                page_item_count,
            ) = self._parse_reconciliation_page(
                payload, marker_prefix, page, items_seen,
            )
            items_seen += page_item_count

            # An item bound applies to all records, including unmatched ones.
            if items_seen > self.max_reconciliation_items:
                raise ReconciliationUnavailable
            matched.extend(page_ids)

            # A complete page gives the engine its exact zero/one/multiple set.
            if next_page is None:
                return tuple(dict.fromkeys(matched))

            # Advance one page at a time so the page bound remains meaningful.
            if next_page <= page or next_page != page + 1:
                raise ReconciliationUnavailable
            page = next_page

        # A page explicitly advertised beyond the configured cap is not a
        # partial reconciliation result: callers must classify it as unknown.
        raise ReconciliationUnavailable

    def find_recording_ids(
        self,
        instance_url: str,
        token: str,
        marker_token: str,
    ) -> tuple[int, ...]:
        """Compatibility spelling for the reconciliation transport boundary."""
        return self.reconcile_recordings(instance_url, token, marker_token)

    def list_tags(self, instance_url: str, token: str) -> tuple[Tag, ...]:
        """Return the caller's accessible tags in Speakr's response order."""
        # Validate credentials before making the fixed tag discovery request.
        origin = _origin(instance_url)
        safe_token = _validate_token(token)
        connection: http.client.HTTPConnection | None = None
        try:
            # Use the short discovery timeout so a stale catalog can be useful.
            try:
                connection = _connection_for(origin, self.tag_timeout_seconds)
                connection.connect()
                connection.putrequest("GET", _TAGS_PATH, skip_accept_encoding=True)
                connection.putheader("Authorization", _BEARER_PREFIX + safe_token)
                connection.putheader("Accept", "application/json")
                connection.putheader("Connection", "close")
                connection.endheaders()
                response = connection.getresponse()
                status = _response_status(response)
            except _ResponseReadError:
                raise TagDiscoveryUnavailable from None
            except Exception:
                raise TagDiscoveryUnavailable from None

            # Preserve HTTP status without reading an untrusted rejection body.
            if status != 200:
                retry_after = _response_retry_after(response, self._clock)
                _close_response(response)
                raise TagDiscoveryRejected(status, retry_after)

            # Parse only a complete bounded JSON document before exposing tags.
            try:
                body = _read_bounded(response, self.max_response_bytes)
                payload = json.loads(body.decode("utf-8"))
            except (_ResponseReadError, UnicodeDecodeError, json.JSONDecodeError):
                raise InvalidTagCatalog from None
            except Exception:
                raise InvalidTagCatalog from None
            finally:
                _close_response(response)
            return self._parse_tag_catalog(payload)
        finally:
            # Release a short-lived discovery connection on every outcome.
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    @staticmethod
    def _parse_tag_catalog(payload: object) -> tuple[Tag, ...]:
        """Validate the exact tag container while retaining its original order."""
        # Reject alternate containers and partial entries as contract failures.
        if not isinstance(payload, dict) or set(payload) != {"tags"}:
            raise InvalidTagCatalog
        raw_tags = payload["tags"]
        if not isinstance(raw_tags, list):
            raise InvalidTagCatalog

        # Build a typed immutable catalog only after every entry is valid.
        tags: list[Tag] = []
        for raw_tag in raw_tags:
            if not isinstance(raw_tag, dict):
                raise InvalidTagCatalog
            tag_id, name = raw_tag.get("id"), raw_tag.get("name")
            if type(tag_id) is not int or tag_id <= 0 or not isinstance(name, str) or not name.strip():
                raise InvalidTagCatalog
            tags.append(Tag(tag_id, name))
        return tuple(tags)

    def _get_reconciliation_page(
        self, origin: _Origin, token: str, path: str,
    ) -> object:
        """Fetch and decode one bounded reconciliation page."""
        connection: http.client.HTTPConnection | None = None
        try:
            # Connect only to the already validated configured origin.
            try:
                connection = _connection_for(origin, self.timeout_seconds)
                connection.connect()
            except Exception:
                raise ReconciliationUnavailable from None

            # Send a GET with Bearer auth and no redirect-capable handler.
            try:
                connection.putrequest("GET", path, skip_accept_encoding=True)
                connection.putheader("Authorization", _BEARER_PREFIX + token)
                connection.putheader("Accept", "application/json")
                connection.putheader("Connection", "close")
                connection.endheaders()
                response = connection.getresponse()
                status = _response_status(response)
            except _ResponseReadError:
                raise ReconciliationUnavailable from None
            except Exception:
                raise ReconciliationUnavailable from None

            # Preserve only the status and normalized retry hint; never read a
            # rejection body that could contain credentials or private text.
            if status != 200:
                retry_after = _response_retry_after(response, self._clock)
                _close_response(response)
                raise ReconciliationRejected(status, retry_after)

            # Decode only a successful body after enforcing its byte bound.
            try:
                body = _read_bounded(response, self.max_response_bytes)
            except _ResponseReadError:
                raise ReconciliationUnavailable from None
            finally:
                _close_response(response)

            # Convert JSON syntax errors into the sanitized unavailable type.
            try:
                return json.loads(body.decode("utf-8"))
            except Exception:
                raise ReconciliationUnavailable from None
        finally:
            # Release the page connection on every result path.
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    @staticmethod
    def _parse_reconciliation_page(
        payload: object,
        marker_prefix: str,
        requested_page: int,
        items_before_page: int,
    ) -> tuple[list[int], int | None, int]:
        """Validate a page, select exact IDs, and find its next page."""
        # Accept known list containers and reject ambiguous response shapes.
        if isinstance(payload, list):
            raw_items = payload
            metadata: dict[str, object] = {}
        elif isinstance(payload, dict):
            containers = [
                key for key in ("recordings", "items", "results", "data")
                if key in payload
            ]
            if (
                len(containers) != 1
                or not isinstance(payload[containers[0]], list)
            ):
                raise ReconciliationUnavailable
            raw_items = payload[containers[0]]
            raw_pagination = payload.get("pagination", {})
            if not isinstance(raw_pagination, dict):
                raise ReconciliationUnavailable
            metadata = {**raw_pagination, **payload}
        else:
            raise ReconciliationUnavailable

        # Validate every returned record before exposing any matching ID.
        ids: list[int] = []
        for item in raw_items:
            if not isinstance(item, dict):
                raise ReconciliationUnavailable
            recording_id = item.get("id")
            title = item.get("title")
            if (
                type(recording_id) is not int
                or recording_id <= 0
                or not isinstance(title, str)
            ):
                raise ReconciliationUnavailable

            # Match only the marker at the beginning of the title.
            if title.startswith(marker_prefix):
                ids.append(recording_id)

        # A bare list has no safe pagination signal and ends the search.
        if not metadata:
            return ids, None, len(raw_items)

        # Confirm the reported current page matches the request to prevent loops.
        current_page = metadata.get("page", metadata.get("current_page"))
        if current_page is not None:
            if type(current_page) is not int or current_page != requested_page:
                raise ReconciliationUnavailable

        # Prefer explicit numeric pagination over response-provided URLs.
        next_page = metadata.get("next_page", metadata.get("nextPage"))
        if next_page is not None:
            if type(next_page) is not int or next_page <= requested_page:
                raise ReconciliationUnavailable
            return ids, next_page, len(raw_items)

        # Accept only a relative next URL and extract its page locally.
        if "next" in metadata:
            next_link = metadata["next"]
            if next_link is None:
                return ids, None, len(raw_items)
            if type(next_link) is int:
                if next_link <= requested_page:
                    raise ReconciliationUnavailable
                return ids, next_link, len(raw_items)
            if not isinstance(next_link, str):
                raise ReconciliationUnavailable
            try:
                parsed_next = urlsplit(next_link)
                next_query = parse_qs(parsed_next.query, strict_parsing=True)
                next_page_value = next_query.get("page", [None])[0]
                next_query_value = next_query.get("q", [None])[0]
                if (
                    parsed_next.scheme
                    or parsed_next.netloc
                    or parsed_next.fragment
                    or parsed_next.path != _RECORDINGS_PATH
                    or next_query_value != marker_prefix[:-1]
                    or type(next_page_value) is not str
                ):
                    raise ReconciliationUnavailable
                next_page = int(next_page_value)
            except (TypeError, ValueError):
                raise ReconciliationUnavailable from None
            if next_page <= requested_page:
                raise ReconciliationUnavailable
            return ids, next_page, len(raw_items)

        # Validate boolean and count hints before advancing; reject contradictions.
        has_next = metadata.get(
            "has_next", metadata.get("hasNext", metadata.get("has_more")),
        )
        if has_next is not None and type(has_next) is not bool:
            raise ReconciliationUnavailable

        # Use declared page counts when available.
        total_pages = metadata.get("total_pages", metadata.get("pages"))
        if total_pages is not None:
            if type(total_pages) is not int or total_pages < requested_page:
                raise ReconciliationUnavailable
            derived_next = (
                requested_page + 1
                if requested_page < total_pages else None
            )
            if has_next is False and derived_next is not None:
                raise ReconciliationUnavailable
            if has_next is True and derived_next is None:
                raise ReconciliationUnavailable
            return ids, derived_next, len(raw_items)

        # Fall back to a bounded total-item count when page counts are absent.
        total = metadata.get("total")
        if total is not None:
            if (
                type(total) is not int
                or total < 0
                or items_before_page + len(raw_items) > total
            ):
                raise ReconciliationUnavailable
            derived_next = (
                requested_page + 1
                if items_before_page + len(raw_items) < total else None
            )
            if has_next is False and derived_next is not None:
                raise ReconciliationUnavailable
            if has_next is True and derived_next is None:
                raise ReconciliationUnavailable
            return ids, derived_next, len(raw_items)

        # Follow an explicit boolean continuation only when it is true.
        if has_next is True:
            return ids, requested_page + 1, len(raw_items)
        return ids, None, len(raw_items)

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
    "InvalidTagCatalog",
    "MetadataRejected",
    "MetadataUnavailable",
    "ReconciliationRejected",
    "ReconciliationUnavailable",
    "SpeakrError",
    "SpeakrHTTPError",
    "SpeakrTransport",
    "StdlibSpeakrTransport",
    "TransferNotSent",
    "TransferOutcomeUnknown",
    "TransferRejected",
    "TagDiscoveryRejected",
    "TagDiscoveryUnavailable",
]
