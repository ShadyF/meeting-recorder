"""Private, atomic cache for the active Speakr tag catalog."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .speakr_domain import Tag, normalize_speakr_url


CACHE_VERSION = 1
MAX_CATALOG_BYTES = 1024 * 1024
MAX_TAGS = 10000
MAX_NAME_CHARS = 4096


def _root() -> Path:
    """Return the private XDG cache directory for Speakr catalogs."""
    return Path(
        os.path.expanduser(os.environ.get("XDG_CACHE_HOME") or "~/.cache"),
    ) / "meeting-recorder"


def _utc(value: object) -> datetime:
    """Require a UTC timestamp that is safe to serialize as cache metadata."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta():
        raise ValueError("tag catalog timestamp is invalid")
    return value


def _stamp(value: datetime) -> str:
    """Encode a validated UTC timestamp without local-time ambiguity."""
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_stamp(value: object) -> datetime:
    """Decode the fixed UTC timestamp format used in the cache schema."""
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 64:
        raise ValueError("tag catalog timestamp is invalid")
    try:
        return _utc(datetime.fromisoformat(value[:-1] + "+00:00"))
    except ValueError:
        raise ValueError("tag catalog timestamp is invalid") from None


@dataclass(frozen=True)
class TagCatalogSnapshot:
    """One fetched catalog for a normalized Speakr origin."""

    origin: str
    fetched_at_utc: datetime
    tags: tuple[Tag, ...]

    def __post_init__(self) -> None:
        # Keep the cache identity independent of credentials and URL spelling.
        if self.origin != normalize_speakr_url(self.origin):
            raise ValueError("tag catalog origin is invalid")
        _utc(self.fetched_at_utc)

        # Validate the bounded typed catalog before it reaches disk.
        if not isinstance(self.tags, tuple) or len(self.tags) > MAX_TAGS:
            raise ValueError("tag catalog is invalid")
        if any(
            not isinstance(tag, Tag)
            or type(tag.tag_id) is not int
            or tag.tag_id <= 0
            or not isinstance(tag.name, str)
            or not tag.name
            or len(tag.name) > MAX_NAME_CHARS
            for tag in self.tags
        ):
            raise ValueError("tag catalog is invalid")


class SpeakrTagCache:
    """Persist only the latest catalog for the currently active Speakr URL."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else _root()

    @property
    def path(self) -> Path:
        """Return the single catalog path; it never includes URL or token text."""
        return self.root / "speakr-tags.json"

    @property
    def lock_path(self) -> Path:
        """Return the separate advisory lock path for catalog operations."""
        return self.root / "speakr-tags.lock"

    def load(self, instance_url: str) -> TagCatalogSnapshot | None:
        """Load the snapshot only when it belongs to the requested active URL."""
        # Normalize before locking so invalid configuration never touches disk.
        origin = normalize_speakr_url(instance_url)
        try:
            with self.operation_lock(blocking=True) as acquired:
                if not acquired:
                    return None
                return self._load_locked(origin)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def store(self, snapshot: TagCatalogSnapshot) -> None:
        """Atomically replace the prior origin with one validated catalog."""
        if not isinstance(snapshot, TagCatalogSnapshot):
            raise ValueError("tag catalog is invalid")

        # Create and harden the private directory before acquiring its lock.
        self._ensure_root()
        with self.operation_lock(blocking=True) as acquired:
            if not acquired:
                raise OSError("tag catalog lock is unavailable")
            self._store_locked(snapshot)

    def clear(self, instance_url: str | None = None) -> None:
        """Delete the active catalog, optionally only when its origin matches."""
        # Normalize an optional deletion target before changing the cache.
        origin = normalize_speakr_url(instance_url) if instance_url is not None else None
        try:
            self._ensure_root()
            with self.operation_lock(blocking=True) as acquired:
                if not acquired:
                    return
                if origin is None or self._load_locked(origin) is not None:
                    self.path.unlink(missing_ok=True)
                    _fsync_directory(self.root)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    delete = clear

    def activate(self, instance_url: str | None) -> str | None:
        """Select one active origin and delete any catalog from another origin."""
        # Normalize the configured origin without ever accepting token identity.
        origin = normalize_speakr_url(instance_url) if instance_url is not None else None
        try:
            self._ensure_root()
            with self.operation_lock(blocking=True) as acquired:
                if not acquired:
                    return origin
                if origin is None:
                    self.path.unlink(missing_ok=True)
                    _fsync_directory(self.root)
                    return None

                # Remove malformed or mismatched prior state before a new fetch.
                snapshot = self._load_any_locked()
                if snapshot is None or snapshot.origin != origin:
                    self.path.unlink(missing_ok=True)
                    _fsync_directory(self.root)
                return origin
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return origin

    @contextmanager
    def operation_lock(self, *, blocking: bool) -> Iterator[bool]:
        """Serialize readers, replacements, and deletion of this catalog."""
        # Share the module-level convention with callers that need one lock scope.
        with tag_catalog_operation_lock(blocking=blocking, root=self.root) as acquired:
            yield acquired

    def _ensure_root(self) -> None:
        """Create the XDG cache directory with owner-only permissions."""
        # Correct inherited umasks or existing modes before storing private names.
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _load_locked(self, origin: str) -> TagCatalogSnapshot | None:
        """Decode a bounded snapshot while the catalog cannot be replaced."""
        snapshot = self._load_any_locked()
        return snapshot if snapshot is not None and snapshot.origin == origin else None

    def _load_any_locked(self) -> TagCatalogSnapshot | None:
        """Decode a bounded snapshot without applying an origin selection."""
        # Refuse oversized files before decoding untrusted JSON text.
        try:
            if self.path.stat().st_size > MAX_CATALOG_BYTES:
                return None
            payload = json.loads(self.path.read_bytes().decode("utf-8"))
            return _decode(payload)
        except (OSError, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _store_locked(self, snapshot: TagCatalogSnapshot) -> None:
        """Durably publish one complete cache document with a same-dir rename."""
        # Serialize before creating a temporary file so invalid data changes nothing.
        payload = json.dumps(_encode(snapshot), separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(payload) > MAX_CATALOG_BYTES:
            raise ValueError("tag catalog is too large")
        descriptor, temporary = tempfile.mkstemp(prefix=".speakr-tags-", suffix=".tmp", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            _fsync_directory(self.root)
        finally:
            if descriptor != -1:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _encode(snapshot: TagCatalogSnapshot) -> dict[str, object]:
    """Produce the fixed secret-free on-disk document."""
    return {
        "version": CACHE_VERSION,
        "origin": snapshot.origin,
        "fetched_at_utc": _stamp(snapshot.fetched_at_utc),
        "tags": [{"id": tag.tag_id, "name": tag.name} for tag in snapshot.tags],
    }


def _decode(payload: object) -> TagCatalogSnapshot:
    """Strictly decode one bounded cache document without partial recovery."""
    # Require precisely the documented keys so unknown data never becomes state.
    if not isinstance(payload, dict) or set(payload) != {"version", "origin", "fetched_at_utc", "tags"}:
        raise ValueError("tag catalog is malformed")
    if payload["version"] != CACHE_VERSION or not isinstance(payload["tags"], list):
        raise ValueError("tag catalog is malformed")
    if len(payload["tags"]) > MAX_TAGS:
        raise ValueError("tag catalog is malformed")

    # Validate all entries before returning a usable snapshot.
    tags = []
    for item in payload["tags"]:
        if not isinstance(item, dict) or set(item) != {"id", "name"}:
            raise ValueError("tag catalog is malformed")
        tags.append(Tag(item["id"], item["name"]))
    return TagCatalogSnapshot(payload["origin"], _parse_stamp(payload["fetched_at_utc"]), tuple(tags))


def _fsync_directory(directory: Path) -> None:
    """Make a completed same-directory replacement durable when supported."""
    # Some filesystems cannot sync directories; keep their documented exception isolated.
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
def tag_catalog_operation_lock(
    *, blocking: bool, root: Path | str | None = None,
) -> Iterator[bool]:
    """Acquire the owner-only advisory lock used by tag catalog operations."""
    # Create the directory and lock file before attempting exclusive access.
    base = Path(root) if root is not None else _root()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    descriptor = os.open(base / "speakr-tags.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        os.close(descriptor)
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


__all__ = [
    "CACHE_VERSION",
    "SpeakrTagCache",
    "TagCatalogSnapshot",
    "tag_catalog_operation_lock",
]
