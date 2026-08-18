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

from .calendar_domain import (
    CalendarOccurrence,
    MeetingSnapshot,
    decode_occurrence_selector,
    encode_occurrence_selector,
    meeting_snapshot,
)

SIDECAR_SCHEMA_VERSION = 1
_SIDECAR_SUFFIX = ".meeting.json"
_TOP_LEVEL_KEYS = frozenset({
    "schema_version", "recording_filename", "original_fallback_filename",
    "capture_started_at", "capture_ended_at", "meeting",
})
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

    def __post_init__(self) -> None:
        _basename(self.recording_filename, "recording filename")
        _basename(self.original_fallback_filename, "fallback filename")
        start = _utc(self.capture_started_at, "capture start")
        end = _utc(self.capture_ended_at, "capture end")
        if end < start:
            raise ValueError("capture end must not precede capture start")
        if self.meeting is not None and not isinstance(self.meeting, MeetingSnapshot):
            raise ValueError("meeting must be a MeetingSnapshot or None")


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
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "recording_filename": sidecar.recording_filename,
        "original_fallback_filename": sidecar.original_fallback_filename,
        "capture_started_at": _stamp(sidecar.capture_started_at),
        "capture_ended_at": _stamp(sidecar.capture_ended_at),
        "meeting": _occurrence_payload(sidecar.meeting) if sidecar.meeting else None,
    }


def decode_sidecar(value: object) -> MeetingSidecar:
    """Decode only the exact supported sidecar schema."""
    if not isinstance(value, dict) or set(value) != _TOP_LEVEL_KEYS:
        raise ValueError("sidecar schema is malformed")
    if (not isinstance(value["schema_version"], int)
            or isinstance(value["schema_version"], bool)
            or value["schema_version"] != SIDECAR_SCHEMA_VERSION):
        raise ValueError("sidecar schema version is unsupported")
    meeting_value = value["meeting"]
    meeting = None if meeting_value is None else _occurrence_from_payload(meeting_value)
    return MeetingSidecar(
        value["recording_filename"], value["original_fallback_filename"],
        _parse_stamp(value["capture_started_at"], "capture start"),
        _parse_stamp(value["capture_ended_at"], "capture end"), meeting,
    )


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
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            try:
                value = json.load(handle)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("sidecar JSON is malformed") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise ValueError("sidecar must not be a symlink") from exc
        raise
    finally:
        if descriptor != -1:
            os.close(descriptor)
    return decode_sidecar(value)


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
    os.unlink(target)
    _fsync_directory(target.parent)
    return True
