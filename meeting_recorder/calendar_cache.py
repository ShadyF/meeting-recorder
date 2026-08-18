"""Private atomic cache snapshots for selected Calendar occurrences."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Sequence

from .calendar_domain import CalendarOccurrence, CalendarParticipant, OccurrenceKey


CACHE_VERSION = 2
MAX_OFFLINE_AGE = timedelta(days=7)
_LOOKBACK, _LOOKAHEAD = timedelta(hours=24), timedelta(days=7)


def _utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("cache timestamp must be UTC")


def _root() -> Path:
    return Path(os.path.expanduser(os.environ.get("XDG_CACHE_HOME") or "~/.cache")) / "meeting-recorder" / "google-calendar"


def snapshot_window(now: datetime) -> tuple[datetime, datetime]:
    _utc(now)
    return now - _LOOKBACK, now + _LOOKAHEAD


@dataclass(frozen=True, eq=False)
class CalendarSnapshot:
    version: int
    calendar_id: str
    fetched_at_utc: datetime
    window_start_utc: datetime
    window_end_utc: datetime
    occurrences: tuple[CalendarOccurrence, ...]

    def __eq__(self, other: object) -> bool:
        """Compare snapshot content across a v1-to-v2 compatibility upgrade."""
        if not isinstance(other, CalendarSnapshot):
            return NotImplemented
        return (self.calendar_id, self.fetched_at_utc, self.window_start_utc,
                self.window_end_utc, self.occurrences) == (
                    other.calendar_id, other.fetched_at_utc, other.window_start_utc,
                    other.window_end_utc, other.occurrences)

    def __post_init__(self) -> None:
        if self.version not in {1, CACHE_VERSION} or not isinstance(self.calendar_id, str) or not self.calendar_id:
            raise ValueError("snapshot identity is invalid")
        for value in (self.fetched_at_utc, self.window_start_utc, self.window_end_utc):
            _utc(value)
        if self.window_end_utc <= self.window_start_utc or not isinstance(self.occurrences, tuple):
            raise ValueError("snapshot window is invalid")
        if any(item.key.calendar_id != self.calendar_id for item in self.occurrences):
            raise ValueError("snapshot calendar does not match occurrences")


def is_snapshot_fresh(snapshot: CalendarSnapshot, now: datetime) -> bool:
    _utc(now)
    age = now - snapshot.fetched_at_utc
    return timedelta() <= age <= MAX_OFFLINE_AGE


def _stamp(value: datetime) -> str:
    _utc(value)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("invalid cached timestamp")
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    _utc(result)
    return result


class CalendarCache:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else _root()

    def path_for(self, calendar_id: str) -> Path:
        if not isinstance(calendar_id, str) or not calendar_id:
            raise ValueError("calendar ID is invalid")
        return self.root / (hashlib.sha256(calendar_id.encode()).hexdigest() + ".json")

    def load(self, calendar_id: str) -> CalendarSnapshot | None:
        try:
            data = json.loads(self.path_for(calendar_id).read_text(encoding="utf-8"))
            snapshot = _decode(data)
            return snapshot if snapshot.calendar_id == calendar_id else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def load_fresh(self, calendar_id: str, now: datetime) -> CalendarSnapshot | None:
        snapshot = self.load(calendar_id)
        return snapshot if snapshot and is_snapshot_fresh(snapshot, now) else None

    def load_selected_occurrences(self, calendar_ids: Sequence[str], now: datetime) -> tuple[CalendarOccurrence, ...]:
        return tuple(item for calendar_id in calendar_ids for snapshot in [self.load_fresh(calendar_id, now)]
                     if snapshot for item in snapshot.occurrences)

    def store(self, snapshot: CalendarSnapshot) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = self.path_for(snapshot.calendar_id)
        payload = json.dumps(_encode(snapshot), separators=(",", ":"), sort_keys=True).encode()
        descriptor, temporary = tempfile.mkstemp(prefix=".calendar-write-", suffix=".tmp", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            _fsync_directory(self.root)
        finally:
            if descriptor != -1:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def clear(self) -> None:
        for path in self.root.glob("*.json") if self.root.is_dir() else ():
            path.unlink()


def _encode(snapshot: CalendarSnapshot) -> dict[str, object]:
    return {"version": snapshot.version, "calendar_id": snapshot.calendar_id,
            "fetched_at_utc": _stamp(snapshot.fetched_at_utc), "window_start_utc": _stamp(snapshot.window_start_utc),
            "window_end_utc": _stamp(snapshot.window_end_utc), "occurrences": [
                {"key": {"calendar_id": item.key.calendar_id, "event_id": item.key.event_id,
                         "original_start_utc": _stamp(item.key.original_start_utc) if item.key.original_start_utc else None},
                 "start_utc": _stamp(item.start_utc), "end_utc": _stamp(item.end_utc), "summary": item.summary,
                 "description": item.description, "location": item.location,
                 "details_visible": item.details_visible,
                 "participants": [{"email": p.email, "display_name": p.display_name} for p in item.participants],
                 "participants_complete": item.participants_complete} for item in snapshot.occurrences]}


def _decode(data: object) -> CalendarSnapshot:
    if not isinstance(data, dict) or data.get("version") not in {1, CACHE_VERSION} or not isinstance(data.get("occurrences"), list):
        raise ValueError("cached snapshot is malformed")
    occurrences = []
    for item in data["occurrences"]:
        if not isinstance(item, dict) or not isinstance(item.get("key"), dict) or not isinstance(item.get("participants"), list):
            raise ValueError("cached occurrence is malformed")
        key = item["key"]
        participants = tuple(CalendarParticipant(p.get("email"), p.get("display_name")) for p in item["participants"]
                             if isinstance(p, dict))
        if len(participants) != len(item["participants"]):
            raise ValueError("cached participants are malformed")
        summary = item.get("summary")
        if summary is not None and (not isinstance(summary, str) or not summary.strip()):
            summary = None
        version = data.get("version")
        description = item.get("description") if version == CACHE_VERSION else None
        location = item.get("location") if version == CACHE_VERSION else None
        details_visible = bool(summary and summary.strip()) if version == 1 else item.get("details_visible", False)
        occurrences.append(CalendarOccurrence(OccurrenceKey(key.get("calendar_id"), key.get("event_id"),
            _parse(key["original_start_utc"]) if key.get("original_start_utc") else None),
            _parse(item.get("start_utc")), _parse(item.get("end_utc")), summary, participants,
            item.get("participants_complete"), description, location, details_visible))
    return CalendarSnapshot(CACHE_VERSION, data.get("calendar_id"), _parse(data.get("fetched_at_utc")),
                            _parse(data.get("window_start_utc")), _parse(data.get("window_end_utc")), tuple(occurrences))


def _fsync_directory(directory: Path) -> None:
    """Make a completed same-directory rename durable when the filesystem supports it."""
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            return
        raise


@contextmanager
def calendar_operation_lock(*, blocking: bool, root: Path | str | None = None) -> Iterator[bool]:
    base = Path(root) if root is not None else _root()
    lock_path = base.parent / "calendar.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        os.close(descriptor)
        yield False
        return
    except OSError:
        os.close(descriptor)
        raise
    try:
        yield True
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
