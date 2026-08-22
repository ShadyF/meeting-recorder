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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from .calendar_domain import CalendarOccurrence


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
    if is_live_reserved(path):
        return True
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
    suffix = candidate.suffix
    stem = candidate.name[:-len(suffix)] if suffix else candidate.name
    number = 2
    while True:
        candidate = candidate.with_name(f"{stem}-{number}{suffix}")
        if not _available(candidate, source_path):
            return candidate
        number += 1


def _reservation_root() -> Path:
    cache_home = Path(os.path.expanduser(os.environ.get("XDG_CACHE_HOME") or "~/.cache"))
    root = cache_home / "meeting-recorder" / "recording-path-reservations"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(root)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("reservation root must be a private directory")
    os.chmod(root, 0o700)
    return root


@contextmanager
def _reservation_registry_lock(root: Path | None = None) -> Iterator[None]:
    """Serialize marker lifecycle operations without holding it during reservations."""
    root = root or _reservation_root()
    registry = root / "registry.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(registry, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("reservation registry must not be a symlink") from exc
        raise
    try:
        os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    locked = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        try:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _reservation_path(path: Path | str) -> Path:
    canonical = os.path.realpath(Path(path))
    root = _reservation_root()
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return root / f"{key}.lock"


@dataclass
class RecordingPathReservation:
    """An advisory cross-process reservation held by an open flock descriptor."""

    path: Path
    _descriptor: int
    _marker: Path

    def release(self) -> None:
        """Release this reservation exactly once; crash closes the flock too."""
        descriptor, self._descriptor = self._descriptor, -1
        if descriptor == -1:
            return
        try:
            with _reservation_registry_lock(self._marker.parent):
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
                    descriptor = -1
                try:
                    os.unlink(self._marker)
                except FileNotFoundError:
                    pass
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def __enter__(self) -> "RecordingPathReservation":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.release()


def reserve_recording_path(path: Path | str) -> RecordingPathReservation:
    """Reserve a canonical destination until the returned handle is released."""
    destination = Path(path)
    marker = _reservation_path(destination)
    with _reservation_registry_lock(marker.parent):
        flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(marker, flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError("reservation marker must not be a symlink") from exc
            raise
        try:
            os.fchmod(descriptor, 0o600)
        except Exception:
            os.close(descriptor)
            raise
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise FileExistsError(destination) from exc
        except Exception:
            os.close(descriptor)
            raise
        if os.path.lexists(destination):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
            os.unlink(marker)
            raise FileExistsError(destination)
        return RecordingPathReservation(destination, descriptor, marker)


def release_recording_path(reservation: RecordingPathReservation) -> None:
    """Canonical idempotent release operation for a recording reservation."""
    if not isinstance(reservation, RecordingPathReservation):
        raise TypeError("recording reservation is invalid")
    reservation.release()


def is_live_reserved(path: Path | str) -> bool:
    """Return whether another process currently holds the destination lock."""
    marker = _reservation_path(path)
    with _reservation_registry_lock(marker.parent):
        if not os.path.lexists(marker):
            return False
        info = os.lstat(marker)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("reservation marker must be a regular file")
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(marker, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError("reservation marker must not be a symlink") from exc
            raise
        try:
            os.fchmod(descriptor, 0o600)
        except Exception:
            os.close(descriptor)
            raise
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        os.unlink(marker)
        return False


def _fsync_directory(directory: Path) -> None:
    # Open the directory itself so its namespace changes can be made durable.
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        # Treat filesystems that cannot sync directories as an explicit supported limitation.
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            return
        raise


def fsync_recording_directory(directory: Path | str) -> None:
    """Durably publish a recording-directory namespace change."""
    _fsync_directory(Path(directory))


def fsync_recording_directory_fd(descriptor: int) -> None:
    """Durably publish a namespace change through an already-open directory FD."""
    if type(descriptor) is not int or descriptor < 0:
        raise ValueError("directory descriptor is invalid")
    # Sync the already-held directory descriptor without reopening a pathname.
    try:
        os.fsync(descriptor)
    except OSError as exc:
        # Preserve the same supported-filesystem behavior as path-based syncing.
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            raise


def link_regular_file_no_replace_dirfd(
    source_directory_fd: int,
    source_name: str | bytes,
    destination_directory_fd: int,
    destination_name: str | bytes,
    *,
    expected_link_count: int = 2,
) -> None:
    """Create a verified same-filesystem hard link without replacing a destination."""
    # Validate the source before creating any namespace entry.
    if type(expected_link_count) is not int or expected_link_count < 2:
        raise ValueError("expected link count is invalid")
    source_info = os.stat(source_name, dir_fd=source_directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
        raise ValueError("source must be a single regular file")
    # Create the unique destination without allowing replacement semantics.
    os.link(
        source_name, destination_name,
        src_dir_fd=source_directory_fd, dst_dir_fd=destination_directory_fd,
        follow_symlinks=False,
    )
    # Make the unique destination durable before validating or reporting an ambiguous result.
    fsync_recording_directory_fd(destination_directory_fd)
    try:
        # Verify that the durable destination still names the source identity and link count.
        destination_info = os.stat(destination_name, dir_fd=destination_directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(destination_info.st_mode)
            or destination_info.st_dev != source_info.st_dev
            or destination_info.st_ino != source_info.st_ino
            or destination_info.st_nlink != expected_link_count
        ):
            raise OSError(errno.EIO, "linked destination identity changed")
    except Exception:
        # Leave the unique entry for the durable cleanup intent to inspect and recover.
        raise


def unlink_verified_file_dirfd(
    directory_fd: int,
    name: str | bytes,
    expected_device: int,
    expected_inode: int,
    expected_size: int,
    expected_mtime_ns: int,
    expected_nlink: int,
) -> None:
    """Unlink only a regular entry whose identity still matches the expected file."""
    # Check the final directory entry immediately before removing its exact identity.
    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_dev != expected_device
        or info.st_ino != expected_inode
        or info.st_size != expected_size
        or info.st_mtime_ns != expected_mtime_ns
        or info.st_nlink != expected_nlink
    ):
        raise OSError(errno.EIO, "file identity changed before unlink")
    # Remove only after the identity and expected link count pass validation.
    os.unlink(name, dir_fd=directory_fd)


def move_regular_file_no_replace(source: Path | str, destination: Path | str) -> Path:
    """Move one regular file atomically without overwriting either destination or source."""
    # Validate canonical parents, source identity, and device placement before linking.
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

    try:
        # Create the destination hard link without replacing an existing name.
        os.link(source_path, destination_path, follow_symlinks=False)
    except OSError as exc:
        raise MovePrecommitError(source_path, destination_path) from exc
    try:
        # Confirm the destination still names the source before removing the source name.
        destination_info = os.lstat(destination_path)
        if not (stat.S_ISREG(destination_info.st_mode)
                and destination_info.st_dev == source_info.st_dev
                and destination_info.st_ino == source_info.st_ino):
            raise MovePrecommitError(source_path, destination_path)
        try:
            # Remove the source only after the destination identity is verified.
            os.unlink(source_path)
        except Exception as exc:
            raise MovePrecommitError(source_path, destination_path) from exc
        try:
            # Publish the committed two-name-to-one-name transition durably.
            _fsync_directory(source_path.parent)
        except Exception as exc:
            raise MoveCommittedError(destination_path) from exc
        return destination_path
    except Exception:
        # Keep the linked destination when source removal or durability is ambiguous.
        raise


class MovePrecommitError(OSError):
    """A move failed before the source namespace entry was committed away."""

    def __init__(self, source: Path, destination: Path) -> None:
        super().__init__(errno.EIO, "recording move did not commit")
        self.source = source
        self.destination = destination


class MoveCommittedError(OSError):
    """The destination exists and is authoritative despite a post-move failure."""

    def __init__(self, destination: Path) -> None:
        super().__init__(errno.EIO, "recording move committed but directory sync failed")
        self.destination = destination


@contextmanager
def recording_directory_lock(directory: Path | str) -> Iterator[None]:
    """Serialize recording metadata transactions using a private XDG cache lock."""
    canonical = os.path.realpath(Path(directory))
    cache_home = Path(os.path.expanduser(os.environ.get("XDG_CACHE_HOME") or "~/.cache"))
    lock_root = cache_home / "meeting-recorder" / "recording-directory-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Derive one private lock path from the canonical recording directory.
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    lock_path = lock_root / f"{key}.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        # Hold the lock across the complete metadata and namespace transaction.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
