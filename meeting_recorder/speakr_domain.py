"""Transient metadata and the public Speakr publication state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit
import unicodedata

from .calendar_domain import MeetingSnapshot
from .meeting_sidecar import MeetingSidecar


class PublicationState(str, Enum):
    """The durable states of one explicit Speakr publication."""

    READY = "ready"
    TRANSFERRING = "transferring"
    TRANSFER_REJECTED = "transfer_rejected"
    TRANSFER_UNKNOWN = "transfer_unknown"
    METADATA_PENDING = "metadata_pending"
    PUBLISHED = "published"


def _is_control_or_whitespace(value: str) -> bool:
    return any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)


def normalize_speakr_url(value: object) -> str:
    """Return a canonical HTTP(S) origin and reject request-target material."""
    if not isinstance(value, str) or not value or _is_control_or_whitespace(value):
        raise ValueError("Speakr URL contains invalid characters")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.casefold()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Speakr URL is malformed") from exc

    if scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError("Speakr URL must be an HTTP(S) origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Speakr URL must not contain userinfo")
    if parsed.query or parsed.fragment or "?" in value or "#" in value:
        raise ValueError("Speakr URL must not contain a query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("Speakr URL must contain only an origin path")

    # urlsplit accepts an empty port as ``None``; validate the authority too so
    # malformed input cannot be made valid by canonicalization.
    authority = parsed.netloc
    if authority.startswith("["):
        closing_bracket = authority.find("]")
        if closing_bracket < 0:
            raise ValueError("Speakr URL has a malformed IPv6 authority")
        host_text = authority[1:closing_bracket]
        port_text = authority[closing_bracket + 1:]
        if not host_text or (port_text and not re.fullmatch(r":\d+", port_text)):
            raise ValueError("Speakr URL has a malformed authority")
        if "%" in host_text:
            raise ValueError("Speakr URL zone identifiers are not supported")
        try:
            rendered_host = f"[{ipaddress.IPv6Address(host_text).compressed.casefold()}]"
        except ValueError as exc:
            raise ValueError("Speakr URL has a malformed IPv6 host") from exc
    else:
        if ":" in authority:
            host_text, port_text = authority.rsplit(":", 1)
            if not port_text or not port_text.isdecimal():
                raise ValueError("Speakr URL has a malformed port")
        else:
            host_text = authority
        if not host_text or ":" in host_text or "[" in host_text or "]" in host_text:
            raise ValueError("Speakr URL has a malformed host")
        try:
            rendered_host = hostname.encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise ValueError("Speakr URL has a malformed DNS host") from exc

    if port is not None and not 0 <= port <= 65535:
        raise ValueError("Speakr URL port is out of range")
    default_port = 80 if scheme == "http" else 443
    rendered_port = "" if port is None or port == default_port else f":{port}"
    return f"{scheme}://{rendered_host}{rendered_port}"


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _normalize_line(value: object, name: str, *, required: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    cleaned = "".join(" " if unicodedata.category(char).startswith("C") else char for char in value)
    cleaned = " ".join(cleaned.split())
    if required and not cleaned:
        raise ValueError(f"{name} must be non-empty")
    return cleaned


def _normalize_notes(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("notes must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in normalized.split("\n"):
        cleaned = "".join(" " if unicodedata.category(char).startswith("C") else char for char in line)
        lines.append(" ".join(cleaned.split()))
    return "\n".join(lines).strip("\n")


@dataclass(frozen=True)
class PublicationKey:
    """The idempotency identity: normalized instance origin and media digest."""

    instance_url: str
    recording_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "instance_url", normalize_speakr_url(self.instance_url))
        if not isinstance(self.recording_sha256, str):
            raise ValueError("recording SHA-256 must be a string")
        digest = self.recording_sha256.casefold()
        if len(digest) != 64 or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("recording SHA-256 must be 64 hexadecimal characters")
        object.__setattr__(self, "recording_sha256", digest)


@dataclass(frozen=True)
class MediaIdentity:
    """Transient identity captured from a securely opened media descriptor."""

    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int

    def __post_init__(self) -> None:
        if isinstance(self.path, str):
            object.__setattr__(self, "path", Path(self.path))
        if not isinstance(self.path, Path):
            raise ValueError("media path is invalid")
        for value, name in ((self.device, "media device"), (self.inode, "media inode"),
                            (self.size, "media size"), (self.mtime_ns, "media mtime")):
            _nonnegative_int(value, name)


@dataclass(frozen=True)
class SpeakrMetadata:
    """Transient, sanitized user-visible metadata; never durable publication state."""

    title: str
    meeting_date: datetime
    notes: str
    participants: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _normalize_line(self.title, "title", required=True))
        object.__setattr__(self, "meeting_date", _utc(self.meeting_date, "meeting date"))
        object.__setattr__(self, "notes", _normalize_notes(self.notes))
        object.__setattr__(self, "participants",
                           _normalize_line(self.participants, "participants", required=False))


_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_ERROR_CODES = frozenset({
    "interrupted_transfer", "metadata_ambiguous", "metadata_changed", "metadata_failed",
    "metadata_malformed", "metadata_missing", "metadata_unavailable", "protocol_error",
    "transfer_not_sent", "transfer_rejected", "transfer_unknown",
})


@dataclass(frozen=True)
class PublicationJob:
    """Only public, restart-safe publication state persisted by SQLite."""

    key: PublicationKey
    state: PublicationState = PublicationState.READY
    remote_recording_id: int | None = None
    media_device: int = 0
    media_inode: int = 0
    media_size: int = 0
    source_mtime_ns: int = 0
    file_last_modified_ms: int = 0
    attempt_count: int = 0
    last_error_code: str | None = None
    last_http_status: int | None = None
    transfer_started_at_ms: int | None = None
    accepted_at_ms: int | None = None
    published_at_ms: int | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.key, PublicationKey):
            raise ValueError("publication key is invalid")
        if not isinstance(self.state, PublicationState):
            raise ValueError("publication state is invalid")
        for value, name in (
            (self.media_device, "media device"), (self.media_inode, "media inode"),
            (self.media_size, "media size"), (self.source_mtime_ns, "source mtime"),
            (self.file_last_modified_ms, "file last modified time"),
            (self.attempt_count, "publication attempt count"),
        ):
            _nonnegative_int(value, name)
        for value, name in (
            (self.transfer_started_at_ms, "transfer start"),
            (self.accepted_at_ms, "accepted time"), (self.published_at_ms, "published time"),
            (self.created_at_ms, "created time"), (self.updated_at_ms, "updated time"),
        ):
            if value is not None:
                _nonnegative_int(value, name)
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("publication timestamps are out of order")
        if self.transfer_started_at_ms is not None and self.transfer_started_at_ms < self.created_at_ms:
            raise ValueError("transfer start precedes publication creation")
        if self.accepted_at_ms is not None and self.accepted_at_ms < self.created_at_ms:
            raise ValueError("acceptance precedes publication creation")
        if self.published_at_ms is not None and self.published_at_ms < self.created_at_ms:
            raise ValueError("publication precedes publication creation")
        if self.transfer_started_at_ms is not None and self.transfer_started_at_ms > self.updated_at_ms:
            raise ValueError("transfer start is newer than publication update")
        if self.accepted_at_ms is not None and self.accepted_at_ms > self.updated_at_ms:
            raise ValueError("acceptance is newer than publication update")
        if self.published_at_ms is not None and self.published_at_ms > self.updated_at_ms:
            raise ValueError("publication is newer than publication update")
        if self.remote_recording_id is not None and (
                type(self.remote_recording_id) is not int or self.remote_recording_id <= 0):
            raise ValueError("remote recording ID must be a positive integer")
        if self.last_error_code is not None and (
                not isinstance(self.last_error_code, str)
                or _SAFE_ERROR_CODE.fullmatch(self.last_error_code) is None
                or self.last_error_code not in _SAFE_ERROR_CODES):
            raise ValueError("error code is not an allowed safe code")
        if self.last_http_status is not None and (
                type(self.last_http_status) is not int or not 100 <= self.last_http_status <= 599):
            raise ValueError("HTTP status is invalid")

        # Enforce a closed restart-safe progression: only accepted transfers
        # may carry remote identity, and every accepted state needs an attempt.
        if self.state is PublicationState.READY:
            if self.attempt_count != 0 or self.remote_recording_id is not None:
                raise ValueError("ready jobs have no transfer identity")
            if any(value is not None for value in (
                    self.transfer_started_at_ms, self.accepted_at_ms, self.published_at_ms,
                    self.last_error_code, self.last_http_status)):
                raise ValueError("ready jobs have no transfer timestamps or errors")
        elif self.state is PublicationState.TRANSFERRING:
            if self.attempt_count <= 0 or self.transfer_started_at_ms is None:
                raise ValueError("transferring jobs require an attempt and start time")
            if any(value is not None for value in (
                    self.remote_recording_id, self.accepted_at_ms, self.published_at_ms,
                    self.last_error_code, self.last_http_status)):
                raise ValueError("transferring jobs have an inconsistent public state")
        elif self.state in {PublicationState.TRANSFER_REJECTED, PublicationState.TRANSFER_UNKNOWN}:
            if self.attempt_count <= 0 or self.transfer_started_at_ms is None:
                raise ValueError("failed transfers require an attempt and start time")
            if self.accepted_at_ms is not None or self.published_at_ms is not None:
                raise ValueError("failed transfers cannot have accepted metadata")
            if self.remote_recording_id is not None or self.last_error_code is None:
                raise ValueError("failed transfers require no remote ID and a safe error")
        else:
            if self.attempt_count <= 0 or self.remote_recording_id is None or self.transfer_started_at_ms is None:
                raise ValueError("accepted states require a remote ID and transfer start")
            if self.accepted_at_ms is None or self.accepted_at_ms < self.transfer_started_at_ms:
                raise ValueError("accepted states require an ordered acceptance time")
            if self.state is PublicationState.PUBLISHED and (
                    self.last_error_code is not None or self.last_http_status is not None):
                raise ValueError("accepted states cannot retain transfer errors")
            if self.state is PublicationState.PUBLISHED:
                if self.published_at_ms is None or self.published_at_ms < self.accepted_at_ms:
                    raise ValueError("published jobs require an ordered publication time")
            elif self.published_at_ms is not None:
                raise ValueError("pending metadata cannot have a publication time")


@dataclass(frozen=True)
class PublicationResult:
    """A safe operation result; error fields are transient and never stored."""

    job: PublicationJob
    already_published: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.job, PublicationJob):
            raise ValueError("publication result job is invalid")
        if type(self.already_published) is not bool:
            raise ValueError("already_published must be a bool")

    @property
    def error_code(self) -> str | None:
        """Expose the job's current safe error without duplicating durable state."""
        return self.job.last_error_code

    @property
    def http_status(self) -> int | None:
        """Expose the job's current HTTP status without duplicating durable state."""
        return self.job.last_http_status


def _mtime_utc(mtime_ns: int) -> datetime:
    try:
        return datetime.fromtimestamp(mtime_ns / 1_000_000_000, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("media mtime cannot be represented as UTC") from exc


def map_speakr_metadata(
    media: Path | str,
    media_mtime_ns: int,
    sidecar: MeetingSidecar | None,
) -> SpeakrMetadata:
    """Project only visible Calendar data from the current media snapshot."""
    if not isinstance(media, (Path, str)):
        raise ValueError("media path is invalid")
    _nonnegative_int(media_mtime_ns, "media mtime")
    media_path = Path(media)
    fallback_title = _normalize_line(media_path.stem, "media stem", required=True)
    if sidecar is not None and not isinstance(sidecar, MeetingSidecar):
        raise ValueError("sidecar is invalid")

    if sidecar is None:
        return SpeakrMetadata(fallback_title, _mtime_utc(media_mtime_ns), "", "")

    meeting = sidecar.meeting
    if meeting is None:
        return SpeakrMetadata(fallback_title, sidecar.capture_started_at, "", "")
    if not meeting.details_visible:
        return SpeakrMetadata(fallback_title, meeting.scheduled_start_utc, "", "")

    description = _normalize_notes(meeting.description or "")
    location = _normalize_line(meeting.location or "", "location", required=False)
    notes = description
    if location:
        notes = f"{notes}\n\nLocation: {location}" if notes else f"Location: {location}"
    participants = ", ".join(
        label for label in (
            _normalize_line(item, "participant", required=False)
            for item in meeting.participant_labels
        ) if label
    )
    return SpeakrMetadata(
        _normalize_line(meeting.title, "meeting title", required=True),
        meeting.scheduled_start_utc, notes, participants,
    )


__all__ = [
    "MediaIdentity", "PublicationJob", "PublicationKey", "PublicationResult",
    "PublicationState", "SpeakrMetadata", "map_speakr_metadata", "normalize_speakr_url",
]
