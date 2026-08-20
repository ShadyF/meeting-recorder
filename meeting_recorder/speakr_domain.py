"""Immutable, public data exchanged with the Speakr publisher."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import ipaddress
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit
import unicodedata

from .calendar_domain import MeetingSnapshot
from .meeting_sidecar import MeetingSidecar


class PublicationState(str, Enum):
    """The durable states of one Speakr publication job."""

    READY = "ready"
    TRANSFERRING = "transferring"
    TRANSFER_REJECTED = "transfer_rejected"
    TRANSFER_UNKNOWN = "transfer_unknown"
    METADATA_PENDING = "metadata_pending"
    PUBLISHED = "published"


def _is_control_or_whitespace(value: str) -> bool:
    return any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)


def normalize_speakr_url(value: object) -> str:
    """Return the canonical origin form of a Speakr instance URL.

    Credentials and request-target components are deliberately rejected rather
    than normalized.  This keeps an instance identity from becoming a place to
    smuggle a bearer token or another private request value.
    """
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

    # urlsplit accepts an empty port as ``None``; reject it explicitly instead
    # of accidentally turning a malformed authority into a valid origin.
    authority = parsed.netloc
    if authority.startswith("["):
        closing_bracket = authority.find("]")
        if closing_bracket < 0:
            raise ValueError("Speakr URL has a malformed IPv6 authority")
        host_text = authority[1:closing_bracket]
        port_text = authority[closing_bracket + 1:]
        if not host_text or (port_text and not re.fullmatch(r":\d+", port_text)):
            raise ValueError("Speakr URL has a malformed authority")
        try:
            # Zone identifiers are valid in local IPv6 authorities, but are not
            # part of the address accepted by IPv6Address itself.
            ipaddress.IPv6Address(host_text.split("%", 1)[0])
        except ValueError as exc:
            raise ValueError("Speakr URL has a malformed IPv6 host") from exc
        rendered_host = f"[{hostname.casefold()}]"
    else:
        if ":" in authority:
            host_text, port_text = authority.rsplit(":", 1)
            if not port_text or not port_text.isdecimal():
                raise ValueError("Speakr URL has a malformed port")
        else:
            host_text = authority
        if not host_text or ":" in host_text or "[" in host_text or "]" in host_text:
            raise ValueError("Speakr URL has a malformed host")
        rendered_host = hostname.casefold()

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

    # Public single-line values may not carry control characters.  Replacing
    # them with a space avoids joining two words while keeping the projection
    # deterministic and free of private formatting bytes.
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
    """The server-independent idempotency identity for one recording."""

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
    """The local file identity captured when a publication job is created."""

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
    """The sanitized, user-visible metadata sent to Speakr."""

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


@dataclass(frozen=True, init=False)
class PublicationJob:
    """The complete durable state for one publication attempt.

    The constructor accepts a few historical spellings for persisted fields so
    callers can read an architecture snapshot without adding duplicate schema
    fields.  Only the fields below participate in equality, repr, and storage.
    """

    key: PublicationKey
    media: MediaIdentity
    metadata: SpeakrMetadata
    state: PublicationState = PublicationState.READY
    attempt_count: int = 0
    remote_recording_id: int | None = None
    transfer_started_at: datetime | None = None
    transfer_completed_at: datetime | None = None
    published_at: datetime | None = None
    error_code: str | None = None
    http_status: int | None = None

    def __init__(
        self,
        key: PublicationKey,
        media: MediaIdentity,
        metadata: SpeakrMetadata,
        state: PublicationState = PublicationState.READY,
        attempt_count: int = 0,
        remote_recording_id: int | None = None,
        transfer_started_at: datetime | None = None,
        transfer_completed_at: datetime | None = None,
        published_at: datetime | None = None,
        error_code: str | None = None,
        http_status: int | None = None,
        **aliases: Any,
    ) -> None:
        # Accept naming variants from the design notes without exposing them as
        # independent persisted fields or allowing two values for one field.
        values = {
            "attempt_count": attempt_count,
            "transfer_completed_at": transfer_completed_at,
            "error_code": error_code,
            "http_status": http_status,
        }
        for alias, canonical in {
            "attempt": "attempt_count",
            "attempts": "attempt_count",
            "transfer_finished_at": "transfer_completed_at",
            "transferred_at": "transfer_completed_at",
            "last_error_code": "error_code",
            "last_http_status": "http_status",
        }.items():
            if alias in aliases:
                if values[canonical] != (0 if canonical == "attempt_count" else None):
                    raise TypeError(f"both {canonical} and {alias} were provided")
                values[canonical] = aliases.pop(alias)
        if aliases:
            unexpected = next(iter(aliases))
            raise TypeError(f"unexpected PublicationJob field: {unexpected}")

        object.__setattr__(self, "key", key)
        object.__setattr__(self, "media", media)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "attempt_count", values["attempt_count"])
        object.__setattr__(self, "remote_recording_id", remote_recording_id)
        object.__setattr__(self, "transfer_started_at", transfer_started_at)
        object.__setattr__(self, "transfer_completed_at", values["transfer_completed_at"])
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "error_code", values["error_code"])
        object.__setattr__(self, "http_status", values["http_status"])
        self._validate()

    @property
    def attempt(self) -> int:
        return self.attempt_count

    @property
    def attempts(self) -> int:
        return self.attempt_count

    @property
    def transfer_finished_at(self) -> datetime | None:
        return self.transfer_completed_at

    @property
    def transferred_at(self) -> datetime | None:
        return self.transfer_completed_at

    @property
    def last_error_code(self) -> str | None:
        return self.error_code

    @property
    def last_http_status(self) -> int | None:
        return self.http_status

    def _validate(self) -> None:
        if not isinstance(self.key, PublicationKey):
            raise ValueError("publication key is invalid")
        if not isinstance(self.media, MediaIdentity):
            raise ValueError("media identity is invalid")
        if not isinstance(self.metadata, SpeakrMetadata):
            raise ValueError("Speakr metadata is invalid")
        if not isinstance(self.state, PublicationState):
            raise ValueError("publication state is invalid")
        _nonnegative_int(self.attempt_count, "publication attempt count")

        for timestamp, name in ((self.transfer_started_at, "transfer start"),
                                (self.transfer_completed_at, "transfer completion"),
                                (self.published_at, "publication time")):
            if timestamp is not None:
                _utc(timestamp, name)

        if self.remote_recording_id is not None and type(self.remote_recording_id) is not int:
            raise ValueError("remote recording ID must be an integer")
        final_transfer_state = self.state in {
            PublicationState.METADATA_PENDING, PublicationState.PUBLISHED,
        }
        if final_transfer_state:
            if self.remote_recording_id is None or self.remote_recording_id <= 0:
                raise ValueError("metadata states require a positive remote recording ID")
        elif self.remote_recording_id is not None:
            raise ValueError("remote recording ID is not valid in this state")

        if self.state is PublicationState.READY:
            if any(value is not None for value in (
                    self.transfer_started_at, self.transfer_completed_at, self.published_at)):
                raise ValueError("ready jobs must not have transfer timestamps")
        elif self.state is PublicationState.TRANSFERRING:
            if self.transfer_started_at is None or self.transfer_completed_at is not None or self.published_at is not None:
                raise ValueError("transferring jobs have an inconsistent timestamp set")
        else:
            if self.transfer_started_at is None or self.transfer_completed_at is None:
                raise ValueError("completed transfer states require transfer timestamps")
            if self.transfer_completed_at < self.transfer_started_at:
                raise ValueError("transfer timestamps are out of order")
            if self.state is PublicationState.PUBLISHED:
                if self.published_at is None or self.published_at < self.transfer_completed_at:
                    raise ValueError("published jobs require an ordered publication timestamp")
            elif self.published_at is not None:
                raise ValueError("only published jobs have a publication timestamp")

        error_state = self.state in {
            PublicationState.TRANSFER_REJECTED, PublicationState.TRANSFER_UNKNOWN,
        }
        if self.error_code is not None:
            if not isinstance(self.error_code, str) or _SAFE_ERROR_CODE.fullmatch(self.error_code) is None:
                raise ValueError("error code is not an allowed safe code")
            if not error_state:
                raise ValueError("error code is only valid for transfer failures")
        if self.http_status is not None:
            if type(self.http_status) is not int or not 100 <= self.http_status <= 599:
                raise ValueError("HTTP status is invalid")
            if not error_state:
                raise ValueError("HTTP status is only valid for transfer failures")


@dataclass(frozen=True)
class PublicationResult:
    """A publication operation result and its duplicate-reconciliation flag."""

    job: PublicationJob
    already_published: bool

    def __post_init__(self) -> None:
        if not isinstance(self.job, PublicationJob):
            raise ValueError("publication result job is invalid")
        if type(self.already_published) is not bool:
            raise ValueError("already_published must be a bool")


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
    """Project only visible Calendar data, with safe media fallbacks."""
    if not isinstance(media, (Path, str)):
        raise ValueError("media path is invalid")
    _nonnegative_int(media_mtime_ns, "media mtime")
    media_path = Path(media)
    fallback_title = _normalize_line(media_path.stem, "media stem", required=True)
    if sidecar is not None and not isinstance(sidecar, MeetingSidecar):
        raise ValueError("sidecar is invalid")

    meeting: MeetingSnapshot | None = sidecar.meeting if sidecar is not None else None
    if meeting is None or not meeting.details_visible:
        return SpeakrMetadata(fallback_title, _mtime_utc(media_mtime_ns), "", "")

    # Description and location are the only visible free-form fields.  Keep
    # their line structure, then separate the optional location label clearly.
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
        meeting.scheduled_start_utc,
        notes,
        participants,
    )


__all__ = [
    "MediaIdentity",
    "PublicationJob",
    "PublicationKey",
    "PublicationResult",
    "PublicationState",
    "SpeakrMetadata",
    "map_speakr_metadata",
    "normalize_speakr_url",
]
