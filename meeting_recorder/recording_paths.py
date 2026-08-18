"""Filesystem-safe recording names, directory locks, and no-replace moves."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Sequence

from .calendar_domain import CalendarOccurrence
from .utils import LOG


def sanitize_title(title: object) -> str:
    """Normalize a meeting title into a readable, conservative filename token."""
    value = unicodedata.normalize("NFC", title if isinstance(title, str) else "")
    output: list[str] = []
    for character in value:
        if character.isspace():
            if output and output[-1] != "_":
                output.append("_")
        elif character.isalnum() or character in ".-_":
            output.append(character)
        else:
            if output and output[-1] != "_":
                output.append("_")
    value = "".join(output).strip("._-") or "Meeting"
    value = truncate_utf8(value).rstrip("._-")
    return value or "Meeting"


def truncate_utf8(value: str, max_bytes: int = 120) -> str:
    """Truncate by UTF-8 bytes without splitting a Unicode code point."""
    if max_bytes < 0:
        raise ValueError("maximum byte length must not be negative")
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _suffix_text(value: Path | str | Sequence[str]) -> str:
    if isinstance(value, Path):
        return "".join(value.suffixes)
    if isinstance(value, str):
        return value if value.startswith(".") else "".join(Path(value).suffixes)
    return "".join(value)


def _local_time(value: datetime, converter: Callable[[datetime], datetime] | None) -> datetime:
    converted = converter(value) if converter else value.astimezone()
    if converted.tzinfo is None:
        raise ValueError("local time converter must return an aware datetime")
    return converted


def visible_recording_filename(
    occurrence: CalendarOccurrence,
    media_suffixes: Path | str | Sequence[str],
    to_local: Callable[[datetime], datetime] | None = None,
) -> str:
    """Build the visible UTC-scheduled-event filename using local event time."""
    local_start = _local_time(occurrence.start_utc, to_local)
    title = truncate_utf8(sanitize_title(occurrence.summary or "Meeting"))
    return f"{local_start:%Y-%m-%d_%H-%M-%S}_{title}{_suffix_text(media_suffixes)}"


def visible_recording_path(
    directory: Path | str,
    occurrence: CalendarOccurrence,
    media_suffixes: Path | str | Sequence[str],
    to_local: Callable[[datetime], datetime] | None = None,
) -> Path:
    return Path(directory) / visible_recording_filename(occurrence, media_suffixes, to_local)


def _sidecar_for(media: Path) -> Path:
    from .meeting_sidecar import sidecar_path
    return sidecar_path(media)


def _available(path: Path, source: Path | None) -> bool:
    if source is not None and path == source:
        return False
    if os.path.lexists(path):
        return True
    sidecar = _sidecar_for(path)
    if source is not None and sidecar == _sidecar_for(source):
        return False
    return os.path.lexists(sidecar)


def collision_safe_path(preferred: Path | str, source: Path | str | None = None) -> Path:
    """Return a candidate clear of media, sidecar, and broken-symlink collisions."""
    candidate = Path(preferred)
    source_path = Path(source) if source is not None else None
    if not _available(candidate, source_path):
        return candidate
    suffixes = "".join(candidate.suffixes)
    stem = candidate.name[:-len(suffixes)] if suffixes else candidate.name
    number = 2
    while True:
        candidate = candidate.with_name(f"{stem}-{number}{suffixes}")
        if not _available(candidate, source_path):
            return candidate
        number += 1


def _fsync_directory(directory: Path) -> None:
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


def move_regular_file_no_replace(source: Path | str, destination: Path | str) -> Path:
    """Move one regular file atomically without overwriting either destination or source."""
    source_path, destination_path = Path(source), Path(destination)
    if os.path.realpath(source_path.parent) != os.path.realpath(destination_path.parent):
        raise ValueError("source and destination must share a canonical parent")
    source_info = os.lstat(source_path)
    if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISREG(source_info.st_mode):
        raise ValueError("source must be a regular non-symlink file")
    if source_path == destination_path:
        return destination_path
    if os.path.lexists(destination_path):
        raise FileExistsError(destination_path)
    parent_info = os.stat(source_path.parent)
    destination_parent_info = os.stat(destination_path.parent)
    if source_info.st_dev != parent_info.st_dev or parent_info.st_dev != destination_parent_info.st_dev:
        raise OSError(errno.EXDEV, "source and destination are on different devices")

    os.link(source_path, destination_path, follow_symlinks=False)
    linked = False
    source_removed = False
    try:
        destination_info = os.lstat(destination_path)
        linked = (stat.S_ISREG(destination_info.st_mode)
                  and destination_info.st_dev == source_info.st_dev
                  and destination_info.st_ino == source_info.st_ino)
        if not linked:
            raise OSError("linked destination could not be verified")
        try:
            os.unlink(source_path)
            source_removed = True
        except Exception:
            # Roll back only a destination still verified to be our hard link.
            try:
                rollback = os.lstat(destination_path)
                if (stat.S_ISREG(rollback.st_mode)
                        and rollback.st_dev == source_info.st_dev
                        and rollback.st_ino == source_info.st_ino):
                    os.unlink(destination_path)
                    _fsync_directory(source_path.parent)
            except Exception as rollback_error:
                LOG.error("Could not roll back recording move: %s", type(rollback_error).__name__)
            raise
        _fsync_directory(source_path.parent)
        return destination_path
    except Exception:
        if linked and not source_removed and os.path.lexists(destination_path):
            try:
                rollback = os.lstat(destination_path)
                if (stat.S_ISREG(rollback.st_mode)
                        and rollback.st_dev == source_info.st_dev
                        and rollback.st_ino == source_info.st_ino):
                    os.unlink(destination_path)
            except Exception as rollback_error:
                LOG.error("Could not clean up failed recording move: %s",
                          type(rollback_error).__name__)
        raise


@contextmanager
def recording_directory_lock(directory: Path | str) -> Iterator[None]:
    """Serialize recording metadata transactions using a private XDG cache lock."""
    canonical = os.path.realpath(Path(directory))
    cache_home = Path(os.path.expanduser(os.environ.get("XDG_CACHE_HOME") or "~/.cache"))
    lock_root = cache_home / "meeting-recorder" / "recording-directory-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    lock_path = lock_root / f"{key}.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
