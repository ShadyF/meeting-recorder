"""Strict, durable metadata stored beside a finalized recording."""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .calendar_domain import (
    CalendarOccurrence,
    MeetingSnapshot,
    decode_occurrence_selector,
    encode_occurrence_selector,
    meeting_snapshot,
)
if TYPE_CHECKING:
    from .speakr_domain import Tag

SIDECAR_SCHEMA_VERSION = 2
_SIDECAR_SUFFIX = ".meeting.json"
_V1_TOP_LEVEL_KEYS = frozenset({
    "schema_version", "recording_filename", "original_fallback_filename",
    "capture_started_at", "capture_ended_at", "meeting",
})
_TOP_LEVEL_KEYS = _V1_TOP_LEVEL_KEYS | {"tags"}
_MEETING_KEYS = frozenset({
    "selector", "title", "scheduled_start_utc", "scheduled_end_utc",
    "participant_labels", "description", "location", "details_visible",
})


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


def _basename(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError(f"{name} must be a non-empty basename")
    if "\x00" in value or "/" in value or "\\" in value:
        raise ValueError(f"{name} must not contain a path separator")
    if Path(value).name != value:
        raise ValueError(f"{name} must be a basename")
    return value


@dataclass(frozen=True)
class MeetingSidecar:
    """The complete, immutable sidecar payload for one recording."""

    recording_filename: str
    original_fallback_filename: str
    capture_started_at: datetime
    capture_ended_at: datetime
    meeting: MeetingSnapshot | None
    tags: tuple[Tag, ...] = ()

    def __post_init__(self) -> None:
        _basename(self.recording_filename, "recording filename")
        _basename(self.original_fallback_filename, "fallback filename")
        start = _utc(self.capture_started_at, "capture start")
        end = _utc(self.capture_ended_at, "capture end")
        if end < start:
            raise ValueError("capture end must not precede capture start")
        if self.meeting is not None and not isinstance(self.meeting, MeetingSnapshot):
            raise ValueError("meeting must be a MeetingSnapshot or None")
        from .speakr_domain import Tag, _MAX_TAGS
        if not isinstance(self.tags, tuple) or any(not isinstance(tag, Tag) for tag in self.tags):
            raise ValueError("sidecar tags must be tag values")
        if len(self.tags) > _MAX_TAGS or len({tag.tag_id for tag in self.tags}) != len(self.tags):
            raise ValueError("sidecar tags contain duplicate tag IDs")


def sidecar_path(media: Path | str) -> Path:
    """Return exactly ``<media filename>.meeting.json`` beside ``media``."""
    media_path = Path(media)
    _basename(media_path.name, "media filename")
    return media_path.with_name(media_path.name + _SIDECAR_SUFFIX)


def _stamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_stamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{name} is not a UTC timestamp") from exc
    return _utc(parsed, name)


def _occurrence_payload(occurrence: CalendarOccurrence | MeetingSnapshot) -> dict[str, object]:
    snapshot = meeting_snapshot(occurrence) if isinstance(occurrence, CalendarOccurrence) else occurrence
    return {
        "selector": encode_occurrence_selector(snapshot.occurrence_key),
        "title": snapshot.title,
        "scheduled_start_utc": _stamp(snapshot.scheduled_start_utc),
        "scheduled_end_utc": _stamp(snapshot.scheduled_end_utc),
        "participant_labels": list(snapshot.participant_labels),
        "description": snapshot.description,
        "location": snapshot.location,
        "details_visible": snapshot.details_visible,
    }


def _occurrence_from_payload(value: object) -> MeetingSnapshot:
    if not isinstance(value, dict) or set(value) != _MEETING_KEYS:
        raise ValueError("meeting metadata is malformed")
    selector = decode_occurrence_selector(value["selector"])
    labels = value["participant_labels"]
    if not isinstance(labels, list) or any(not isinstance(item, str) or not item for item in labels):
        raise ValueError("meeting participant labels are malformed")
    if not isinstance(value["details_visible"], bool):
        raise ValueError("meeting visibility is malformed")
    return MeetingSnapshot(
        selector, value["title"],
        _parse_stamp(value["scheduled_start_utc"], "meeting start"),
        _parse_stamp(value["scheduled_end_utc"], "meeting end"), tuple(labels),
        value["description"], value["location"], value["details_visible"],
    )


def encode_sidecar(sidecar: MeetingSidecar) -> dict[str, object]:
    """Encode a validated sidecar into the strict JSON object schema."""
    if not isinstance(sidecar, MeetingSidecar):
        raise ValueError("sidecar is invalid")
    # Store the frozen tags as public ID-name pairs beside recording metadata.
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "recording_filename": sidecar.recording_filename,
        "original_fallback_filename": sidecar.original_fallback_filename,
        "capture_started_at": _stamp(sidecar.capture_started_at),
        "capture_ended_at": _stamp(sidecar.capture_ended_at),
        "meeting": _occurrence_payload(sidecar.meeting) if sidecar.meeting else None,
        "tags": [{"tag_id": tag.tag_id, "name": tag.name} for tag in sidecar.tags],
    }


def decode_sidecar(value: object) -> MeetingSidecar:
    """Decode only the exact supported sidecar schema."""
    if not isinstance(value, dict):
        raise ValueError("sidecar schema is malformed")
    version = value.get("schema_version")
    if type(version) is not int or version not in {1, SIDECAR_SCHEMA_VERSION}:
        raise ValueError("sidecar schema version is unsupported")
    if set(value) != (_V1_TOP_LEVEL_KEYS if version == 1 else _TOP_LEVEL_KEYS):
        raise ValueError("sidecar schema is malformed")
    # Project legacy sidecars to an empty tag list before strict tag decoding.
    tags_value = [] if version == 1 else value["tags"]
    if not isinstance(tags_value, list):
        raise ValueError("sidecar tags are malformed")
    # Rebuild only complete canonical tags in their persisted order.
    from .speakr_domain import Tag
    tags: list[Tag] = []
    for item in tags_value:
        if not isinstance(item, dict) or set(item) != {"tag_id", "name"}:
            raise ValueError("sidecar tags are malformed")
        tags.append(Tag(item["tag_id"], item["name"]))
    # Decode meeting metadata after the complete tag list is known valid.
    meeting_value = value["meeting"]
    meeting = None if meeting_value is None else _occurrence_from_payload(meeting_value)
    return MeetingSidecar(
        value["recording_filename"], value["original_fallback_filename"],
        _parse_stamp(value["capture_started_at"], "capture start"),
        _parse_stamp(value["capture_ended_at"], "capture end"), meeting, tuple(tags),
    )


def load_sidecar_fd(descriptor: int, *, max_bytes: int = 1_048_576) -> MeetingSidecar:
    """Decode one bounded sidecar from an already-open descriptor without reopening its path."""
    if type(descriptor) is not int or descriptor < 0:
        raise ValueError("sidecar descriptor is invalid")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 16 * 1024 * 1024:
        raise ValueError("sidecar size limit is invalid")
    # Validate the held descriptor before reading any metadata bytes.
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("sidecar is not a single regular file")
    chunks: list[bytes] = []
    total = 0
    # Read bounded chunks at fixed offsets so the path is never reopened.
    while True:
        chunk = os.pread(descriptor, min(65_536, max_bytes - total + 1), total)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("sidecar is too large")
        chunks.append(chunk)
    try:
        # Decode only after the complete bounded byte stream has been collected.
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("sidecar JSON is malformed") from exc
    return decode_sidecar(value)


def load_sidecar(path: Path | str) -> MeetingSidecar:
    """Load and strictly validate one regular, non-symlink sidecar file."""
    sidecar = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(sidecar, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("sidecar is not a regular file")
        # Reuse the descriptor reader so path-based loads retain the byte bound.
        return load_sidecar_fd(descriptor)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise ValueError("sidecar must not be a symlink") from exc
        raise
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    """Durably publish adjacent metadata while tolerating only known unsupported fsyncs."""
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        unsupported = {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
        if exc.errno not in unsupported:
            raise


def write_sidecar(path: Path | str, sidecar: MeetingSidecar) -> None:
    """Atomically write mode-0600 metadata beside the media file."""
    destination = Path(path)
    _basename(destination.name, "sidecar filename")
    if not destination.name.endswith(_SIDECAR_SUFFIX):
        destination = sidecar_path(destination)
    if os.path.lexists(destination):
        info = os.lstat(destination)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("refusing to replace non-regular sidecar")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(encode_sidecar(sidecar), ensure_ascii=False,
                         separators=(",", ":"), sort_keys=True).encode("utf-8")
    descriptor = -1
    temporary = ""
    try:
        # Write and sync a private temporary file before publishing its final name.
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = ""
        # Sync the directory so the replacement name survives a crash.
        _fsync_directory(destination.parent)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def remove_sidecar(path: Path | str) -> bool:
    """Remove a regular sidecar safely and durably; return false when absent."""
    target = Path(path)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("refusing to remove non-regular sidecar")
    # Remove the validated regular sidecar and publish the namespace change.
    os.unlink(target)
    _fsync_directory(target.parent)
    return True
