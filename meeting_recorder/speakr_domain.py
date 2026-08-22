"""Public, restart-safe domain objects for Speakr publication jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import ipaddress
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import unicodedata

from .calendar_domain import MeetingSnapshot
from .meeting_sidecar import MeetingSidecar


class PublicationState(str, Enum):
    """The complete durable state vocabulary for one publication job."""

    QUEUED = "queued"
    TRANSFERRING = "transferring"
    METADATA_PENDING = "metadata_pending"
    PUBLISHED = "published"
    UNCERTAIN = "uncertain"
    BLOCKED = "blocked"
    MISSING = "missing"
    LOCAL_REMOVED = "local_removed"


class PublicationOperation(str, Enum):
    """The external operation represented by a durable job."""

    POST = "post"
    PATCH = "patch"
    RECONCILE = "reconcile"
    NONE = "none"


class ResumeIntent(str, Enum):
    """How a worker must resume a job after a restart."""

    POST = "post"
    PATCH = "patch"
    RECONCILE = "reconcile"
    NONE = "none"


class CleanupPhase(str, Enum):
    """The durable phases of one explicit local cleanup intent."""

    PREPARED = "prepared"
    SIDECAR_QUARANTINED = "sidecar_quarantined"
    MEDIA_QUARANTINED = "media_quarantined"
    SIDECAR_UNLINKED = "sidecar_unlinked"
    MEDIA_UNLINKED = "media_unlinked"


def _is_control_or_whitespace(value: str) -> bool:
    return any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)


def normalize_speakr_url(value: object) -> str:
    """Return a canonical HTTP(S) origin and reject request-target material."""

    # Reject non-text input and characters that could alter a request target.
    if not isinstance(value, str) or not value or _is_control_or_whitespace(value):
        raise ValueError("Speakr URL contains invalid characters")

    # Parse the authority once so scheme, host, and port use one source of truth.
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.casefold()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Speakr URL is malformed") from exc

    # Require an HTTP(S) origin without credentials or request-target data.
    if scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError("Speakr URL must be an HTTP(S) origin")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Speakr URL must not contain userinfo")

    if parsed.query or parsed.fragment or "?" in value or "#" in value:
        raise ValueError("Speakr URL must not contain a query or fragment")

    if parsed.path not in {"", "/"}:
        raise ValueError("Speakr URL must contain only an origin path")

    # Validate the authority independently because urlsplit accepts an empty port.
    authority = parsed.netloc
    if authority.startswith("["):
        # Canonicalize IPv6 hosts and reject unsupported zone identifiers.
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
        # Canonicalize DNS hosts while validating an explicit numeric port.
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

    # Drop default ports while retaining non-default ports in the identity.
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
        for numeric_value, name in (
            (self.device, "media device"), (self.inode, "media inode"),
            (self.size, "media size"), (self.mtime_ns, "media mtime"),
        ):
            _nonnegative_int(numeric_value, name)


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
        object.__setattr__(self, "participants", _normalize_line(self.participants, "participants", required=False))


_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_ERROR_CODES = frozenset({
    "interrupted_transfer", "lease_expired", "local_missing",
    "metadata_ambiguous", "metadata_changed", "metadata_failed", "metadata_malformed",
    "metadata_missing", "metadata_unavailable", "protocol_error", "reconciliation_failed",
    "transfer_not_sent", "transfer_rejected", "transfer_unknown",
})
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_INTENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OPERATIONS = frozenset(item.value for item in PublicationOperation)
_RESUME_INTENTS = frozenset(item.value for item in ResumeIntent)


def _path_bytes(value: object, *, required: bool = True) -> bytes | None:
    if value is None and not required:
        return None
    if isinstance(value, bytes):
        path = value
    elif isinstance(value, (str, os.PathLike)):
        path = os.fsencode(os.fspath(value))
    else:
        raise ValueError("private media path is invalid")
    if not path or b"\x00" in path or len(path) > 4096:
        raise ValueError("private media path is invalid")
    return path


def _cleanup_basename(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValueError(f"{name} is invalid")
    if value in {".", ".."} or any(
        char in "/\\" or char.isspace() or unicodedata.category(char).startswith("C")
        for char in value
    ):
        raise ValueError(f"{name} is invalid")
    return value


@dataclass(frozen=True)
class CleanupIntent:
    """Protected, restartable data for one explicit namespace cleanup."""

    intent_id: str
    expected_private_path: bytes
    expected_recording_sha256: str
    media_device: int
    media_inode: int
    media_size: int
    media_mtime_ns: int
    sidecar_device: int | None = None
    sidecar_inode: int | None = None
    sidecar_size: int | None = None
    sidecar_mtime_ns: int | None = None
    quarantine_media_basename: str = "media"
    quarantine_sidecar_basename: str | None = None
    phase: CleanupPhase = CleanupPhase.PREPARED
    created_at_ms: int = 0
    updated_at_ms: int = 0
    claimed_job_ids: tuple[str, ...] = ()
    claimed_lease_generations: tuple[int, ...] = ()
    media_nlink: int = 1
    sidecar_nlink: int | None = None

    def __post_init__(self) -> None:
        # Validate and canonicalize the intent identity before checking dependent fields.
        if not isinstance(self.intent_id, str) or _SAFE_INTENT_ID.fullmatch(self.intent_id) is None:
            raise ValueError("cleanup intent ID is invalid")
        path = _path_bytes(self.expected_private_path)
        if path is None or not path.startswith(b"/"):
            raise ValueError("cleanup expected path must be absolute")
        object.__setattr__(self, "expected_private_path", path)
        if (
            not isinstance(self.expected_recording_sha256, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", self.expected_recording_sha256) is None
        ):
            raise ValueError("cleanup expected hash is invalid")
        object.__setattr__(self, "expected_recording_sha256", self.expected_recording_sha256.casefold())
        # Require complete nonnegative media and optional sidecar identities.
        for value, name in (
            (self.media_device, "media device"), (self.media_inode, "media inode"),
            (self.media_size, "media size"), (self.media_mtime_ns, "media mtime"),
        ):
            _nonnegative_int(value, name)
        sidecar_values = (
            self.sidecar_device, self.sidecar_inode, self.sidecar_size, self.sidecar_mtime_ns,
        )
        if any(value is None for value in sidecar_values) and not all(value is None for value in sidecar_values):
            raise ValueError("sidecar identity must be complete")
        for value, name in zip(sidecar_values, ("sidecar device", "sidecar inode", "sidecar size", "sidecar mtime")):
            if value is not None:
                _nonnegative_int(value, name)
        # Keep quarantine names private, bounded, and distinct.
        _cleanup_basename(self.quarantine_media_basename, "media quarantine basename")
        if self.quarantine_sidecar_basename is not None:
            _cleanup_basename(self.quarantine_sidecar_basename, "sidecar quarantine basename")
            if self.quarantine_sidecar_basename == self.quarantine_media_basename:
                raise ValueError("cleanup quarantine basenames must be unique")
        if not isinstance(self.phase, CleanupPhase):
            raise ValueError("cleanup phase is invalid")
        # Preserve monotonic journal timestamps and exact claimed membership.
        _nonnegative_int(self.created_at_ms, "cleanup created time")
        _nonnegative_int(self.updated_at_ms, "cleanup updated time")
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("cleanup timestamps are out of order")
        if len(self.claimed_job_ids) != len(set(self.claimed_job_ids)):
            raise ValueError("cleanup job IDs are duplicated")
        if len(self.claimed_job_ids) != len(self.claimed_lease_generations):
            raise ValueError("cleanup membership is incomplete")
        for job_id in self.claimed_job_ids:
            if _SAFE_JOB_ID.fullmatch(job_id) is None:
                raise ValueError("cleanup job ID is invalid")
        for generation in self.claimed_lease_generations:
            if type(generation) is not int or generation < 0:
                raise ValueError("cleanup lease generation is invalid")
        # Journal only single-link source identities before quarantine begins.
        if type(self.media_nlink) is not int or self.media_nlink != 1:
            raise ValueError("cleanup media link count must be one")
        if self.sidecar_device is None:
            if self.sidecar_nlink is not None:
                raise ValueError("cleanup sidecar link count requires a sidecar")
        elif type(self.sidecar_nlink) is not int or self.sidecar_nlink != 1:
            raise ValueError("cleanup sidecar link count must be one")


@dataclass(frozen=True)
class CleanupClaim:
    """An invocation-owned cleanup lease fence that cannot be refreshed from storage."""

    intent_id: str
    owner: str
    job_ids: tuple[str, ...]
    lease_generations: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.intent_id, str) or _SAFE_INTENT_ID.fullmatch(self.intent_id) is None:
            raise ValueError("cleanup claim intent ID is invalid")
        if not isinstance(self.owner, str) or _SAFE_OWNER.fullmatch(self.owner) is None:
            raise ValueError("cleanup claim owner is invalid")
        if tuple(self.job_ids) != tuple(sorted(self.job_ids)) or len(self.job_ids) != len(set(self.job_ids)):
            raise ValueError("cleanup claim job IDs must be sorted and unique")
        if len(self.job_ids) != len(self.lease_generations) or not self.job_ids:
            raise ValueError("cleanup claim membership is incomplete")
        for job_id in self.job_ids:
            if _SAFE_JOB_ID.fullmatch(job_id) is None:
                raise ValueError("cleanup claim job ID is invalid")
        for generation in self.lease_generations:
            if type(generation) is not int or generation < 1:
                raise ValueError("cleanup claim generation is invalid")


@dataclass(frozen=True)
class PublicationJob:
    """Durable publication progress with one protected private filesystem locator.

    Progress fields contain only public publication state. The private path is
    operational state inside the protected store and is not public data. The
    job contains no credentials or copied Meeting title, notes, or participants.
    """

    job_id: str
    key: PublicationKey
    state: PublicationState = PublicationState.QUEUED
    operation: str = PublicationOperation.POST.value
    resume_intent: str = ResumeIntent.POST.value
    private_path: bytes | None = None
    reconciliation_token: str | None = None
    remote_recording_id: int | None = None
    media_device: int = 0
    media_inode: int = 0
    media_size: int = 0
    source_mtime_ns: int = 0
    file_last_modified_ms: int = 0
    attempt_count: int = 0
    next_attempt_at_ms: int = 0
    lease_owner: str | None = None
    lease_generation: int = 0
    lease_expires_at_ms: int | None = None
    cleanup_lease_owner: str | None = None
    cleanup_lease_generation: int = 0
    cleanup_lease_expires_at_ms: int | None = None
    last_error_code: str | None = None
    last_http_status: int | None = None
    transfer_started_at_ms: int | None = None
    accepted_at_ms: int | None = None
    published_at_ms: int | None = None
    uncertain_at_ms: int | None = None
    blocked_at_ms: int | None = None
    missing_at_ms: int | None = None
    local_removed_at_ms: int | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or _SAFE_JOB_ID.fullmatch(self.job_id) is None:
            raise ValueError("publication job ID is invalid")
        if not isinstance(self.key, PublicationKey):
            raise ValueError("publication key is invalid")
        if not isinstance(self.state, PublicationState):
            raise ValueError("publication state is invalid")
        for numeric_value, name in (
            (self.media_device, "media device"), (self.media_inode, "media inode"),
            (self.media_size, "media size"), (self.source_mtime_ns, "source mtime"),
            (self.file_last_modified_ms, "file last modified time"),
            (self.attempt_count, "publication attempt count"),
            (self.next_attempt_at_ms, "next attempt time"),
            (self.lease_generation, "lease generation"),
            (self.cleanup_lease_generation, "cleanup lease generation"),
        ):
            _nonnegative_int(numeric_value, name)
        if self.operation not in _OPERATIONS:
            raise ValueError("publication operation is invalid")
        if self.resume_intent not in _RESUME_INTENTS:
            raise ValueError("publication resume intent is invalid")
        object.__setattr__(self, "private_path", _path_bytes(self.private_path, required=False))
        if self.reconciliation_token is not None and (
            not isinstance(self.reconciliation_token, str)
            or _SAFE_TOKEN.fullmatch(self.reconciliation_token) is None
        ):
            raise ValueError("reconciliation token is invalid")
        if self.lease_owner is not None and (
            not isinstance(self.lease_owner, str) or _SAFE_OWNER.fullmatch(self.lease_owner) is None
        ):
            raise ValueError("lease owner is invalid")
        if self.lease_expires_at_ms is not None:
            _nonnegative_int(self.lease_expires_at_ms, "lease expiry")
        if self.cleanup_lease_owner is not None and (
            not isinstance(self.cleanup_lease_owner, str)
            or _SAFE_OWNER.fullmatch(self.cleanup_lease_owner) is None
        ):
            raise ValueError("cleanup lease owner is invalid")
        if self.cleanup_lease_expires_at_ms is not None:
            _nonnegative_int(self.cleanup_lease_expires_at_ms, "cleanup lease expiry")
        if (self.cleanup_lease_owner is None) != (self.cleanup_lease_expires_at_ms is None):
            raise ValueError("cleanup lease owner and expiry must be paired")
        if self.cleanup_lease_owner is not None and self.cleanup_lease_generation < 1:
            raise ValueError("cleanup lease generation is invalid")
        for timestamp_value, name in (
            (self.transfer_started_at_ms, "transfer start"),
            (self.accepted_at_ms, "accepted time"), (self.published_at_ms, "published time"),
            (self.uncertain_at_ms, "uncertain time"), (self.blocked_at_ms, "blocked time"),
            (self.missing_at_ms, "missing time"), (self.local_removed_at_ms, "local removed time"),
            (self.created_at_ms, "created time"), (self.updated_at_ms, "updated time"),
        ):
            if timestamp_value is not None:
                _nonnegative_int(timestamp_value, name)
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("publication timestamps are out of order")
        if self.remote_recording_id is not None and (
            type(self.remote_recording_id) is not int or self.remote_recording_id <= 0
        ):
            raise ValueError("remote recording ID must be a positive integer")
        if self.last_error_code is not None and (
            not isinstance(self.last_error_code, str)
            or _SAFE_ERROR_CODE.fullmatch(self.last_error_code) is None
            or self.last_error_code not in _SAFE_ERROR_CODES
        ):
            raise ValueError("error code is not an allowed safe code")
        if self.last_http_status is not None and (
            type(self.last_http_status) is not int or not 100 <= self.last_http_status <= 599
        ):
            raise ValueError("HTTP status is invalid")

        # Keep the resume instruction derivable from state so a restarted worker
        # cannot accidentally turn a known remote record into a new POST.
        expected = {
            PublicationState.QUEUED: (PublicationOperation.POST.value, ResumeIntent.POST.value),
            PublicationState.TRANSFERRING: (PublicationOperation.NONE.value, ResumeIntent.POST.value),
            PublicationState.METADATA_PENDING: (PublicationOperation.PATCH.value, ResumeIntent.PATCH.value),
            PublicationState.PUBLISHED: (PublicationOperation.NONE.value, ResumeIntent.NONE.value),
            PublicationState.UNCERTAIN: (self.operation, ResumeIntent.RECONCILE.value),
            PublicationState.BLOCKED: (PublicationOperation.NONE.value, self.resume_intent),
            PublicationState.MISSING: (PublicationOperation.NONE.value, self.resume_intent),
            PublicationState.LOCAL_REMOVED: (PublicationOperation.NONE.value, ResumeIntent.NONE.value),
        }[self.state]
        if (self.operation, self.resume_intent) != expected or (
            self.state is PublicationState.UNCERTAIN
            and self.operation not in {PublicationOperation.NONE.value, PublicationOperation.RECONCILE.value}
        ):
            raise ValueError("publication operation does not match state")
        if self.state in {PublicationState.QUEUED, PublicationState.TRANSFERRING} and self.remote_recording_id is not None:
            raise ValueError("POST states cannot contain a remote recording ID")
        if self.state in {PublicationState.METADATA_PENDING, PublicationState.PUBLISHED} and self.remote_recording_id is None:
            raise ValueError("known-record states require a remote recording ID")
        if self.state in {PublicationState.BLOCKED, PublicationState.MISSING}:
            if self.resume_intent in {ResumeIntent.POST.value, ResumeIntent.RECONCILE.value} and self.remote_recording_id is not None:
                raise ValueError("POST and reconciliation resumes cannot contain a remote recording ID")
            if self.resume_intent == ResumeIntent.PATCH.value and self.remote_recording_id is None:
                raise ValueError("PATCH resumes require a remote recording ID")
        if self.state is PublicationState.TRANSFERRING and (
            self.reconciliation_token is None or self.transfer_started_at_ms is None
        ):
            raise ValueError("transferring jobs require a start time and reconciliation token")
        if self.state in {PublicationState.METADATA_PENDING, PublicationState.PUBLISHED} and (
            self.transfer_started_at_ms is None or self.accepted_at_ms is None
            or self.accepted_at_ms < self.transfer_started_at_ms
        ):
            raise ValueError("known-record states require ordered transfer acceptance")
        if self.state is PublicationState.PUBLISHED and (
            self.published_at_ms is None or self.accepted_at_ms is None
            or self.published_at_ms < self.accepted_at_ms
        ):
            raise ValueError("published jobs require an ordered publication time")
        if self.state is PublicationState.UNCERTAIN and self.reconciliation_token is None:
            raise ValueError("uncertain jobs require a reconciliation token")
        if self.state is PublicationState.LOCAL_REMOVED:
            if (
                self.private_path is not None or self.reconciliation_token is not None
                or self.lease_owner is not None or self.lease_expires_at_ms is not None
                or self.cleanup_lease_owner is not None or self.cleanup_lease_expires_at_ms is not None
                or self.next_attempt_at_ms != 0 or self.last_error_code is not None
                or self.last_http_status is not None or self.remote_recording_id is None
                or self.remote_recording_id <= 0 or self.transfer_started_at_ms is None
                or self.accepted_at_ms is None or self.published_at_ms is None
                or self.local_removed_at_ms is None
                or not (self.transfer_started_at_ms <= self.accepted_at_ms <= self.published_at_ms <= self.local_removed_at_ms)
            ):
                raise ValueError("local removed jobs must retain only completed publication audit data")

    @property
    def reconciliation_eligible(self) -> bool:
        """Whether an uncertain job may be claimed for a safe reconciliation GET."""
        return (
            self.state is PublicationState.UNCERTAIN
            and self.operation == PublicationOperation.RECONCILE.value
            and self.resume_intent == ResumeIntent.RECONCILE.value
            and self.remote_recording_id is None
            and self.reconciliation_token is not None
        )

    @property
    def http_method(self) -> str | None:
        """Return the only HTTP method structurally permitted by this job."""
        if self.state is PublicationState.QUEUED and self.operation == PublicationOperation.POST.value and self.remote_recording_id is None:
            return "POST"
        if self.state is PublicationState.METADATA_PENDING and self.operation == PublicationOperation.PATCH.value and self.remote_recording_id is not None:
            return "PATCH"
        return None


@dataclass(frozen=True)
class PublicationResult:
    """An operation result, not a public serialization payload.

    Error fields are transient and never stored separately; the embedded job
    may contain a protected private filesystem locator.
    """

    job: PublicationJob
    already_published: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.job, PublicationJob):
            raise ValueError("publication result job is invalid")
        if type(self.already_published) is not bool:
            raise ValueError("already_published must be a bool")

    @property
    def error_code(self) -> str | None:
        return self.job.last_error_code

    @property
    def http_status(self) -> int | None:
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
    "CleanupClaim", "CleanupIntent", "CleanupPhase", "MediaIdentity", "PublicationJob", "PublicationKey", "PublicationOperation",
    "PublicationResult", "PublicationState", "ResumeIntent", "SpeakrMetadata",
    "map_speakr_metadata", "normalize_speakr_url",
]
