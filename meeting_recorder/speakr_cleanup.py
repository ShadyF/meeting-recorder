"""Explicit, crash-resumable local cleanup for published Speakr recordings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import errno
import hashlib
import os
from pathlib import Path
import stat
from contextlib import contextmanager
from typing import Callable, Iterable, Iterator, Literal
from uuid import uuid4

from .meeting_sidecar import MeetingSidecar, load_sidecar_fd
from .recording_paths import (
    fsync_recording_directory_fd,
    link_regular_file_no_replace_dirfd,
    recording_directory_lock,
    unlink_verified_file_dirfd,
)
from .speakr_domain import CleanupClaim, CleanupIntent, CleanupPhase, PublicationJob, PublicationState
from .speakr_store import PublicationStore, PublicationStoreError


class CleanupStatus(str, Enum):
    """Stable result categories returned by the explicit cleanup command."""

    ELIGIBLE = "eligible"
    DELETED = "deleted"
    INCOMPLETE = "incomplete"
    SKIPPED = "skipped"


class CleanupReason(str, Enum):
    """Safe, bounded explanations for cleanup decisions."""

    ELIGIBLE = "eligible"
    DELETED = "deleted"
    NOT_OLD = "not_old"
    PUBLISHED_REQUIRED = "published_required"
    GROUP_CONFLICT = "group_conflict"
    OUTSIDE_ROOT = "outside_root"
    MEDIA_UNSAFE = "media_unsafe"
    SIDECAR_UNSAFE = "sidecar_unsafe"
    HASH_MISMATCH = "hash_mismatch"
    CHANGED = "changed"
    LEASED = "leased"
    INCOMPLETE = "incomplete"
    STORE_UNAVAILABLE = "store_unavailable"


@dataclass(frozen=True)
class CleanupResult:
    """One bounded, metadata-free cleanup decision."""

    path: str
    job_ids: tuple[str, ...]
    recording_sha256: str | None
    age_days: int | None
    age_source: str | None
    status: CleanupStatus
    reason: CleanupReason


@dataclass(frozen=True)
class CleanupReport:
    """The immutable result of one preview or delete command."""

    results: tuple[CleanupResult, ...]


@dataclass(frozen=True)
class _ResolvedPath:
    root_fd: int
    parent_fd: int
    root_identity: tuple[int, int]
    parent_identity: tuple[int, int]
    parent_path: Path
    media_name: bytes


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    nlink: int = 1


@dataclass(frozen=True)
class _Inspection:
    media: _FileIdentity
    sidecar: _FileIdentity | None
    age_days: int
    age_source: str
    old: bool
    digest: str


class _CleanupFailure(Exception):
    """Internal control flow carrying only a safe result reason."""

    def __init__(self, reason: CleanupReason, *, mutated: bool = False) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.mutated = mutated


_SIDECAR_SUFFIX = b".meeting.json"
_HASH_CHUNK = 1 << 20
_MAX_PAGE = 100
_LEASE_MS = 120_000
_FLAGS_DIRECTORY = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_FLAGS_CHILD_DIRECTORY = _FLAGS_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FLAGS_FILE = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    # Keep every age and journal timestamp on one explicit UTC timeline.
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("cleanup clock must return timezone-aware UTC")
    return value


def _identity(info: os.stat_result) -> _FileIdentity:
    return _FileIdentity(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)


def _same_identity(info: os.stat_result, expected: _FileIdentity) -> bool:
    return (
        info.st_dev == expected.device and info.st_ino == expected.inode
        and info.st_size == expected.size and info.st_mtime_ns == expected.mtime_ns
    )


def _same_identity_and_nlink(info: os.stat_result, expected: _FileIdentity, nlink: int) -> bool:
    return _same_identity(info, expected) and info.st_nlink == nlink


def _valid_pair(
    original: os.stat_result | None,
    quarantine: os.stat_result | None,
    expected: _FileIdentity,
) -> bool:
    # A hard link has count two while both names exist and count one after one name is gone.
    if original is not None and quarantine is not None:
        return _same_identity_and_nlink(original, expected, 2) and _same_identity_and_nlink(quarantine, expected, 2)
    if original is not None:
        return _same_identity_and_nlink(original, expected, 1)
    if quarantine is not None:
        return _same_identity_and_nlink(quarantine, expected, 1)
    return False


def _safe_name(name: bytes) -> str:
    # Convert only round-trippable filesystem bytes for stable result output.
    value = os.fsdecode(name)
    if os.fsencode(value) != name:
        raise _CleanupFailure(CleanupReason.OUTSIDE_ROOT)
    return value


class PublicationCleanup:
    """Preview and explicitly remove old published recordings under one private root."""

    def __init__(
        self,
        store: PublicationStore,
        root: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        # Require the existing store contract before configuring the cleanup engine.
        if not isinstance(store, PublicationStore):
            raise TypeError("cleanup store is invalid")

        # Normalize the configured root once so later descriptor checks use one spelling.
        self._store = store
        self._root = Path(root).expanduser()
        if not self._root.is_absolute():
            self._root = Path.cwd() / self._root

        # Keep production clocks and optional crash checkpoints injectable for deterministic control.
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._checkpoint = checkpoint

    def preview(self, older_than_days: int) -> CleanupReport:
        """Inspect all bounded candidate groups without changing SQLite or the namespace."""
        # Freeze the policy inputs so every candidate uses one age boundary.
        days = self._days(older_than_days)
        now = _utc_now(self._clock)
        # Treat candidate-store failures as one bounded, non-destructive result.
        try:
            groups = self._candidate_groups()
        except Exception:
            return CleanupReport((self._result("", (), None, None, None, CleanupStatus.INCOMPLETE, CleanupReason.STORE_UNAVAILABLE),))
        # Inspect each exact-path group without creating an intent or lease.
        results = tuple(self._preview_group(path, group, days, now) for path, group in groups)
        return CleanupReport(results)

    def delete(self, older_than_days: int) -> CleanupReport:
        """Resume durable intents first, then explicitly delete newly eligible groups."""
        # Freeze the policy inputs so recovery and new work share one age boundary.
        days = self._days(older_than_days)
        now = _utc_now(self._clock)
        results: list[CleanupResult] = []

        # Resume only durable intents; preview never enters this path.
        try:
            intents = self._store.list_cleanup_intents(limit=_MAX_PAGE)
        except Exception:
            return CleanupReport((self._result("", (), None, None, None, CleanupStatus.INCOMPLETE, CleanupReason.STORE_UNAVAILABLE),))
        # Finish each persisted intent before considering new candidates.
        for intent in intents:
            results.append(self._resume_intent(intent, now))

        # Enumerate candidates again so newly eligible work is independent of recovery results.
        try:
            groups = self._candidate_groups()
        except Exception:
            results.append(self._result("", (), None, None, None, CleanupStatus.INCOMPLETE, CleanupReason.STORE_UNAVAILABLE))
            return CleanupReport(tuple(results))
        resumed_paths = {intent.expected_private_path for intent in intents}
        # Avoid starting a second intent for a path already handled in this command.
        for path, group in groups:
            if path in resumed_paths:
                continue
            results.append(self._delete_group(path, group, days, now))
        return CleanupReport(tuple(results))

    @staticmethod
    def _days(value: int) -> int:
        # Reject booleans and fractional policy values before touching the store.
        if type(value) is not int or value < 1:
            raise ValueError("older_than_days must be an integer of at least one")
        return value

    @staticmethod
    def _result(
        path: str,
        job_ids: Iterable[str],
        digest: str | None,
        age_days: int | None,
        source: str | None,
        status: CleanupStatus,
        reason: CleanupReason,
    ) -> CleanupResult:
        return CleanupResult(path, tuple(job_ids), digest, age_days, source, status, reason)

    def _candidate_groups(self) -> list[tuple[bytes, tuple[PublicationJob, ...]]]:
        # Page by the store's stable cursor and process each private path once.
        groups: list[tuple[bytes, tuple[PublicationJob, ...]]] = []
        seen: set[bytes] = set()
        cursor: tuple[int, str] | None = None
        while True:
            # Fetch one bounded page using the previous page's final stable key.
            if cursor is None:
                page = self._store.list_cleanup_candidates(limit=_MAX_PAGE)
            else:
                page = self._store.list_cleanup_candidates(
                    after_created_at_ms=cursor[0], after_job_id=cursor[1], limit=_MAX_PAGE,
                )
            if not page:
                break
            # Collapse rows sharing one exact path into one cleanup decision.
            for candidate in page:
                if candidate.private_path is None or candidate.private_path in seen:
                    continue
                seen.add(candidate.private_path)
                try:
                    group = tuple(self._store.list_cleanup_group(candidate.private_path, limit=_MAX_PAGE))
                except PublicationStoreError:
                    group = ()
                groups.append((candidate.private_path, group))
            last = page[-1]
            cursor = (last.created_at_ms, last.job_id)
            # Stop after the final short page; otherwise continue from the stable cursor.
            if len(page) < _MAX_PAGE:
                break
        return groups

    def _preview_group(
        self, path: bytes, group: tuple[PublicationJob, ...], days: int, now: datetime,
    ) -> CleanupResult:
        # Validate durable group invariants before opening a candidate path.
        base = self._group_result(path, group)
        if base is not None:
            return base
        try:
            with self._locked_path(path) as resolved:
                inspection = self._inspect(resolved, path, group, days, now)
        except _CleanupFailure as failure:
            return self._result(os.fsdecode(path), (job.job_id for job in group), self._group_hash(group), None, None, CleanupStatus.INCOMPLETE, failure.reason)
        except Exception:
            return self._result(os.fsdecode(path), (job.job_id for job in group), self._group_hash(group), None, None, CleanupStatus.INCOMPLETE, CleanupReason.INCOMPLETE)
        if not inspection.old:
            return self._result(os.fsdecode(path), (job.job_id for job in group), inspection.digest, inspection.age_days, inspection.age_source, CleanupStatus.SKIPPED, CleanupReason.NOT_OLD)
        return self._result(os.fsdecode(path), (job.job_id for job in group), inspection.digest, inspection.age_days, inspection.age_source, CleanupStatus.ELIGIBLE, CleanupReason.ELIGIBLE)

    def _delete_group(
        self, path: bytes, group: tuple[PublicationJob, ...], days: int, now: datetime,
    ) -> CleanupResult:
        # Run the complete preview validation again while holding the directory lock.
        base = self._group_result(path, group)
        if base is not None:
            return base
        intent_id: str | None = None
        claim: CleanupClaim | None = None
        try:
            with self._locked_path(path) as resolved:
                try:
                    # Revalidate the file while the directory lock excludes competing cleanup work.
                    inspection = self._inspect(resolved, path, group, days, now)
                    if not inspection.old:
                        return self._result(os.fsdecode(path), (job.job_id for job in group), inspection.digest, inspection.age_days, inspection.age_source, CleanupStatus.SKIPPED, CleanupReason.NOT_OLD)
                    intent = self._new_intent(path, group, inspection, self._store.current_time_ms())
                    intent_id = intent.intent_id
                    # Record the exact group before claiming leases or changing names.
                    self._store.prepare_cleanup_intent(intent, tuple(job.job_id for job in group))
                    claim = self._store.claim_cleanup_group(intent.intent_id, self._new_owner(), _LEASE_MS)
                    self._assert_claimed_group(claim, group, intent)
                    # Resume every phase under the held lock and immutable claim fence.
                    final = self._resume_locked(intent.intent_id, claim, resolved, now)
                    return self._result(os.fsdecode(path), tuple(job.job_id for job in group), inspection.digest, inspection.age_days, inspection.age_source, CleanupStatus.DELETED, CleanupReason.DELETED) if final else self._result(os.fsdecode(path), tuple(job.job_id for job in group), inspection.digest, inspection.age_days, inspection.age_source, CleanupStatus.INCOMPLETE, CleanupReason.INCOMPLETE)
                except Exception as failure:
                    # Abort only when no namespace mutation has started.
                    if intent_id is not None and not getattr(failure, "mutated", False):
                        self._abort_prepared_locked(intent_id, claim)
                    raise
        except _CleanupFailure as failure:
            return self._result(os.fsdecode(path), (job.job_id for job in group), self._group_hash(group), None, None, CleanupStatus.INCOMPLETE, failure.reason)
        except Exception:
            return self._result(os.fsdecode(path), (job.job_id for job in group), self._group_hash(group), None, None, CleanupStatus.INCOMPLETE, CleanupReason.INCOMPLETE)

    def _resume_intent(self, intent: CleanupIntent, now: datetime) -> CleanupResult:
        # A live lease is reported without attempting to steal it.
        path = intent.expected_private_path
        try:
            with self._locked_path(path) as resolved:
                claim: CleanupClaim | None = None
                try:
                    # Recheck exact membership after acquiring the recording directory lock.
                    group = tuple(self._store.list_cleanup_group(path, limit=_MAX_PAGE))
                    if not group or tuple(job.job_id for job in group) != tuple(intent.claimed_job_ids):
                        raise _CleanupFailure(CleanupReason.INCOMPLETE)
                    try:
                        # Reclaim only after the lock prevents an active cleanup worker from mutating.
                        claim = self._store.claim_cleanup_group(intent.intent_id, self._new_owner(), _LEASE_MS)
                    except Exception:
                        if any(job.cleanup_lease_owner is not None for job in group):
                            raise _CleanupFailure(CleanupReason.LEASED)
                        raise _CleanupFailure(CleanupReason.INCOMPLETE)
                    deleted = self._resume_locked(intent.intent_id, claim, resolved, now)
                except Exception as failure:
                    # Keep any intent whose namespace state may already have changed.
                    if not getattr(failure, "mutated", False):
                        self._abort_prepared_locked(intent.intent_id, claim)
                    raise
            return self._result(os.fsdecode(path), intent.claimed_job_ids, intent.expected_recording_sha256, None, None, CleanupStatus.DELETED if deleted else CleanupStatus.INCOMPLETE, CleanupReason.DELETED if deleted else CleanupReason.INCOMPLETE)
        except _CleanupFailure as failure:
            return self._result(os.fsdecode(path), intent.claimed_job_ids, intent.expected_recording_sha256, None, None, CleanupStatus.INCOMPLETE, failure.reason)
        except Exception:
            return self._result(os.fsdecode(path), intent.claimed_job_ids, intent.expected_recording_sha256, None, None, CleanupStatus.INCOMPLETE, CleanupReason.INCOMPLETE)

    def _abort_prepared_locked(self, intent_id: str, claim: CleanupClaim | None) -> None:
        # Release only an untouched intent while its recording lock still excludes reclaimers.
        try:
            current = self._store.load_cleanup_intent(intent_id)
            if current.phase is CleanupPhase.PREPARED:
                self._store.abort_cleanup_intent(intent_id, claim)
        except Exception:
            pass

    @staticmethod
    def _new_owner() -> str:
        return f"cleanup-{uuid4().hex}"

    @staticmethod
    def _group_hash(group: tuple[PublicationJob, ...]) -> str | None:
        return group[0].key.recording_sha256 if group and all(job.key.recording_sha256 == group[0].key.recording_sha256 for job in group) else None

    def _group_result(self, path: bytes, group: tuple[PublicationJob, ...]) -> CleanupResult | None:
        # Classify durable conflicts without revealing row contents beyond IDs and hash.
        display = os.fsdecode(path)
        ids = tuple(job.job_id for job in group)
        digest = self._group_hash(group)
        if not group or any(job.private_path != path for job in group):
            return self._result(display, ids, digest, None, None, CleanupStatus.INCOMPLETE, CleanupReason.GROUP_CONFLICT)
        if any(job.cleanup_lease_owner is not None or job.lease_owner is not None for job in group):
            return self._result(display, ids, digest, None, None, CleanupStatus.INCOMPLETE, CleanupReason.LEASED)
        if any(job.state is not PublicationState.PUBLISHED for job in group):
            return self._result(display, ids, digest, None, None, CleanupStatus.INCOMPLETE, CleanupReason.PUBLISHED_REQUIRED)
        if digest is None:
            return self._result(display, ids, None, None, None, CleanupStatus.INCOMPLETE, CleanupReason.GROUP_CONFLICT)
        return None

    @staticmethod
    def _new_intent(path: bytes, group: tuple[PublicationJob, ...], inspection: _Inspection, journal_now_ms: int) -> CleanupIntent:
        # Use separate bounded hidden names so no namespace operation can overwrite a file.
        token = uuid4().hex
        sidecar_name = None if inspection.sidecar is None else f".cleanup-{token}-sidecar"
        return CleanupIntent(
            intent_id=f"cleanup-{token}", expected_private_path=path,
            expected_recording_sha256=inspection.digest,
            media_device=inspection.media.device, media_inode=inspection.media.inode,
            media_size=inspection.media.size, media_mtime_ns=inspection.media.mtime_ns,
            sidecar_device=None if inspection.sidecar is None else inspection.sidecar.device,
            sidecar_inode=None if inspection.sidecar is None else inspection.sidecar.inode,
            sidecar_size=None if inspection.sidecar is None else inspection.sidecar.size,
            sidecar_mtime_ns=None if inspection.sidecar is None else inspection.sidecar.mtime_ns,
            quarantine_media_basename=f".cleanup-{token}-media",
            quarantine_sidecar_basename=sidecar_name,
            created_at_ms=journal_now_ms, updated_at_ms=journal_now_ms,
            claimed_job_ids=tuple(job.job_id for job in group),
            claimed_lease_generations=(0,) * len(group),
            media_nlink=inspection.media.nlink,
            sidecar_nlink=None if inspection.sidecar is None else inspection.sidecar.nlink,
        )

    @staticmethod
    def _assert_claimed_group(claim: CleanupClaim, expected: tuple[PublicationJob, ...], intent: CleanupIntent) -> None:
        # Require the store claim to return the exact complete group before touching files.
        if claim.intent_id != intent.intent_id or claim.job_ids != tuple(job.job_id for job in expected):
            raise _CleanupFailure(CleanupReason.INCOMPLETE)

    @contextmanager
    def _locked_path(self, path: bytes) -> Iterator[_ResolvedPath]:
        # Acquire the existing directory lock, then compare the anchored parent again.
        resolved = self._resolve(path)
        try:
            with recording_directory_lock(resolved.parent_path):
                self._rewalk(resolved, path)
                yield resolved
        finally:
            self._close(resolved)

    def _resolve(self, path: bytes) -> _ResolvedPath:
        # Decode without normalizing away traversal or undecodable byte distinctions.
        try:
            text = os.fsdecode(path)
            if os.fsencode(text) != path or not text.startswith("/") or "\x00" in text:
                raise _CleanupFailure(CleanupReason.OUTSIDE_ROOT)
        except (TypeError, UnicodeError):
            raise _CleanupFailure(CleanupReason.OUTSIDE_ROOT)
        # Reject traversal components before opening any configured root.
        parts = text.split("/")
        if any(part in {"", ".", ".."} for part in parts[1:-1]) or not parts[-1]:
            raise _CleanupFailure(CleanupReason.OUTSIDE_ROOT)
        root_abs = os.path.abspath(os.fspath(self._root))
        root_fd = -1
        current_fd = -1
        try:
            # Open and validate the configured root before deriving a relative path.
            self._checkpoint_call("root_before_open")
            root_fd = os.open(root_abs, _FLAGS_DIRECTORY)
            root_info = os.fstat(root_fd)
            if not stat.S_ISDIR(root_info.st_mode):
                raise _CleanupFailure(CleanupReason.OUTSIDE_ROOT)
            configured_info = os.stat(root_abs, follow_symlinks=True)
            if (configured_info.st_dev, configured_info.st_ino) != (root_info.st_dev, root_info.st_ino):
                raise _CleanupFailure(CleanupReason.CHANGED)
            actual_target = os.readlink(f"/proc/self/fd/{root_fd}")
            if actual_target.endswith(" (deleted)"):
                raise _CleanupFailure(CleanupReason.CHANGED)
            canonical_root = os.path.realpath(actual_target)
            actual_info = os.stat(canonical_root, follow_symlinks=True)
            if (actual_info.st_dev, actual_info.st_ino) != (root_info.st_dev, root_info.st_ino):
                raise _CleanupFailure(CleanupReason.CHANGED)
            self._checkpoint_call("root_opened")
            base = None
            # Accept the configured spelling or canonical target, but never an unrelated root.
            for candidate_root in (root_abs, canonical_root):
                if os.path.commonpath((text, candidate_root)) == candidate_root:
                    relative = os.path.relpath(text, candidate_root)
                    if relative not in {".", ""} and not relative.startswith(".." + os.sep):
                        base = (candidate_root, relative)
                        break
            if base is None:
                raise _CleanupFailure(CleanupReason.OUTSIDE_ROOT)
            # Walk parent components without following symlinked directories.
            relative_parts = base[1].split(os.sep)
            media_name = os.fsencode(relative_parts[-1])
            current_fd = root_fd
            for component in relative_parts[:-1]:
                next_fd = os.open(component, _FLAGS_CHILD_DIRECTORY, dir_fd=current_fd)
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd
            parent_info = os.fstat(current_fd)
            parent_path = Path(text).parent
            return _ResolvedPath(
                root_fd, current_fd, (root_info.st_dev, root_info.st_ino),
                (parent_info.st_dev, parent_info.st_ino), parent_path, media_name,
            )
        except _CleanupFailure:
            # Close every descriptor when a deliberate safety rejection occurs.
            if current_fd >= 0 and current_fd != root_fd:
                os.close(current_fd)
            if root_fd >= 0:
                os.close(root_fd)
            raise
        except (OSError, ValueError):
            # Convert unexpected path-open failures into a fail-closed result.
            if current_fd >= 0 and current_fd != root_fd:
                os.close(current_fd)
            if root_fd >= 0:
                os.close(root_fd)
            raise _CleanupFailure(CleanupReason.OUTSIDE_ROOT)

    @staticmethod
    def _close(resolved: _ResolvedPath) -> None:
        # Close each anchored descriptor exactly once after the lock scope ends.
        if resolved.parent_fd != resolved.root_fd:
            os.close(resolved.parent_fd)
        os.close(resolved.root_fd)

    def _rewalk(self, resolved: _ResolvedPath, path: bytes) -> None:
        # Parent replacement or root replacement invalidates the whole operation.
        fresh = self._resolve(path)
        try:
            if fresh.root_identity != resolved.root_identity or fresh.parent_identity != resolved.parent_identity:
                raise _CleanupFailure(CleanupReason.CHANGED)
        finally:
            self._close(fresh)

    def _entry(self, descriptor: int, name: bytes) -> os.stat_result | None:
        # Use lstat-at semantics so final symlinks and non-regular entries fail closed.
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise _CleanupFailure(CleanupReason.MEDIA_UNSAFE)
        return info

    def _sidecar(self, resolved: _ResolvedPath) -> tuple[MeetingSidecar | None, _FileIdentity | None]:
        # Open only the exact adjacent sidecar name and decode through its held descriptor.
        name = resolved.media_name + _SIDECAR_SUFFIX
        descriptor = -1
        try:
            try:
                # Hold the sidecar descriptor so decoding does not reopen a changing pathname.
                descriptor = os.open(name, _FLAGS_FILE, dir_fd=resolved.parent_fd)
            except FileNotFoundError:
                return None, None
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.EMLINK}:
                    raise _CleanupFailure(CleanupReason.SIDECAR_UNSAFE)
                raise _CleanupFailure(CleanupReason.SIDECAR_UNSAFE)
            before = os.fstat(descriptor)
            # Require one private regular sidecar before decoding its bounded contents.
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise _CleanupFailure(CleanupReason.SIDECAR_UNSAFE)
            try:
                sidecar = load_sidecar_fd(descriptor)
            except Exception:
                raise _CleanupFailure(CleanupReason.SIDECAR_UNSAFE)
            after = os.fstat(descriptor)
            # Confirm that the decoded descriptor was not replaced or rewritten.
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise _CleanupFailure(CleanupReason.CHANGED)
            if sidecar.recording_filename != _safe_name(resolved.media_name):
                raise _CleanupFailure(CleanupReason.SIDECAR_UNSAFE)
            return sidecar, _identity(before)
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def _media(self, resolved: _ResolvedPath, digest: str) -> _FileIdentity:
        # Hash one held descriptor and prove the entry did not change during the read.
        entry = self._entry(resolved.parent_fd, resolved.media_name)
        if entry is None or entry.st_nlink != 1:
            raise _CleanupFailure(CleanupReason.MEDIA_UNSAFE)
        descriptor = -1
        try:
            try:
                # Open the exact media entry without following a final symlink.
                descriptor = os.open(resolved.media_name, _FLAGS_FILE, dir_fd=resolved.parent_fd)
            except OSError:
                raise _CleanupFailure(CleanupReason.MEDIA_UNSAFE)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not _same_identity(before, _identity(entry)):
                raise _CleanupFailure(CleanupReason.CHANGED)
            hasher = hashlib.sha256()
            # Hash chunks from the held descriptor rather than the pathname.
            while True:
                chunk = os.read(descriptor, _HASH_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
            after = os.fstat(descriptor)
            current = os.stat(resolved.media_name, dir_fd=resolved.parent_fd, follow_symlinks=False)
            # Compare both descriptor and directory-entry identities after hashing.
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink):
                raise _CleanupFailure(CleanupReason.CHANGED)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
                raise _CleanupFailure(CleanupReason.CHANGED)
            if hasher.hexdigest() != digest:
                raise _CleanupFailure(CleanupReason.HASH_MISMATCH)
            return _identity(before)
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def _inspect(
        self, resolved: _ResolvedPath, path: bytes, group: tuple[PublicationJob, ...], days: int, now: datetime,
    ) -> _Inspection:
        # Validate grouped durable identity before filesystem hashing and age calculation.
        if any(job.private_path != path or job.state is not PublicationState.PUBLISHED for job in group):
            raise _CleanupFailure(CleanupReason.GROUP_CONFLICT)
        digest = self._group_hash(group)
        if digest is None:
            raise _CleanupFailure(CleanupReason.GROUP_CONFLICT)
        sidecar, sidecar_identity = self._sidecar(resolved)
        media_identity = self._media(resolved, digest)
        if sidecar is None:
            if len({job.source_mtime_ns for job in group}) != 1:
                raise _CleanupFailure(CleanupReason.GROUP_CONFLICT)
            source_time = datetime.fromtimestamp(group[0].source_mtime_ns / 1_000_000_000, timezone.utc)
            source = "source_mtime_ns"
        else:
            source_time = sidecar.capture_ended_at
            source = "sidecar"
        age = now - source_time
        return _Inspection(
            media_identity, sidecar_identity, int(age.total_seconds() // 86_400), source,
            source_time <= now - timedelta(days=days), digest,
        )

    def _resume_locked(self, intent_id: str, claim: CleanupClaim, resolved: _ResolvedPath, now: datetime) -> bool:
        # Convert unexpected failures into explicit pre-mutation or post-mutation recovery decisions.
        mutation_state = [False]
        try:
            return self._resume_locked_impl(intent_id, claim, resolved, now, mutation_state)
        except _CleanupFailure:
            raise
        except Exception as failure:
            raise _CleanupFailure(CleanupReason.INCOMPLETE, mutated=mutation_state[0]) from failure

    def _resume_locked_impl(
        self, intent_id: str, claim: CleanupClaim, resolved: _ResolvedPath, now: datetime,
        mutation_state: list[bool],
    ) -> bool:
        # Advance one durable phase at a time; every namespace operation is followed by a directory fsync.
        intent = self._store.load_cleanup_intent(intent_id)
        group = tuple(self._store.list_cleanup_group(intent.expected_private_path, limit=_MAX_PAGE))
        if tuple(job.job_id for job in group) != tuple(intent.claimed_job_ids):
            raise _CleanupFailure(CleanupReason.INCOMPLETE)
        if claim.intent_id != intent.intent_id:
            raise _CleanupFailure(CleanupReason.INCOMPLETE)
        mutation_state[0] = mutation_state[0] or intent.phase is not CleanupPhase.PREPARED
        mutation_started = mutation_state[0]
        if intent.phase is CleanupPhase.PREPARED:
            # Revalidate untouched originals or inspect exact names left by a prior crash.
            claim = self._renew_claim(intent.intent_id, claim)
            media_quarantine = self._entry(resolved.parent_fd, os.fsencode(intent.quarantine_media_basename))
            sidecar_quarantine = (
                None if intent.quarantine_sidecar_basename is None
                else self._entry(resolved.parent_fd, os.fsencode(intent.quarantine_sidecar_basename))
            )
            if media_quarantine is None and sidecar_quarantine is None:
                self._revalidate_originals(resolved, intent)
            else:
                mutation_state[0] = True
                mutation_started = True
                self._verify_prepared_recovery(resolved, intent)
            self._rewalk(resolved, intent.expected_private_path)
            self._checkpoint_call("before_sidecar_quarantine")
            self._rewalk(resolved, intent.expected_private_path)
            claim = self._renew_claim(intent.intent_id, claim)
            mutation_started = self._quarantine(resolved, intent, "sidecar", renew=lambda: self._renew_claim(intent.intent_id, claim)) or mutation_started
            mutation_state[0] = mutation_state[0] or mutation_started
            self._checkpoint_call("before_phase_sidecar_quarantined")
            claim = self._renew_claim(intent.intent_id, claim)
            intent = self._advance(intent, claim, CleanupPhase.SIDECAR_QUARANTINED)
        if intent.phase is CleanupPhase.SIDECAR_QUARANTINED:
            # Quarantine media only after the sidecar phase is fenced and durable.
            claim = self._renew_claim(intent.intent_id, claim)
            self._verify_phase_state(resolved, intent, CleanupPhase.SIDECAR_QUARANTINED)
            self._rewalk(resolved, intent.expected_private_path)
            self._checkpoint_call("before_media_quarantine")
            self._rewalk(resolved, intent.expected_private_path)
            claim = self._renew_claim(intent.intent_id, claim)
            mutation_started = self._quarantine(resolved, intent, "media", renew=lambda: self._renew_claim(intent.intent_id, claim)) or mutation_started
            mutation_state[0] = mutation_state[0] or mutation_started
            self._checkpoint_call("before_phase_media_quarantined")
            claim = self._renew_claim(intent.intent_id, claim)
            intent = self._advance(intent, claim, CleanupPhase.MEDIA_QUARANTINED)
        if intent.phase is CleanupPhase.MEDIA_QUARANTINED:
            # Remove the quarantined sidecar only after both-name media state is verified.
            claim = self._renew_claim(intent.intent_id, claim)
            self._verify_phase_state(resolved, intent, CleanupPhase.MEDIA_QUARANTINED)
            self._rewalk(resolved, intent.expected_private_path)
            self._checkpoint_call("before_sidecar_unlink")
            self._rewalk(resolved, intent.expected_private_path)
            claim = self._renew_claim(intent.intent_id, claim)
            mutation_started = self._unlink_quarantine(resolved, intent, "sidecar", renew=lambda: self._renew_claim(intent.intent_id, claim)) or mutation_started
            mutation_state[0] = mutation_state[0] or mutation_started
            self._checkpoint_call("before_phase_sidecar_unlinked")
            claim = self._renew_claim(intent.intent_id, claim)
            intent = self._advance(intent, claim, CleanupPhase.SIDECAR_UNLINKED)
        if intent.phase is CleanupPhase.SIDECAR_UNLINKED:
            # Remove the quarantined media only after the sidecar unlink phase is fenced.
            claim = self._renew_claim(intent.intent_id, claim)
            self._verify_phase_state(resolved, intent, CleanupPhase.SIDECAR_UNLINKED)
            self._rewalk(resolved, intent.expected_private_path)
            self._checkpoint_call("before_media_unlink")
            self._rewalk(resolved, intent.expected_private_path)
            claim = self._renew_claim(intent.intent_id, claim)
            mutation_started = self._unlink_quarantine(resolved, intent, "media", renew=lambda: self._renew_claim(intent.intent_id, claim)) or mutation_started
            mutation_state[0] = mutation_state[0] or mutation_started
            self._checkpoint_call("before_phase_media_unlinked")
            claim = self._renew_claim(intent.intent_id, claim)
            intent = self._advance(intent, claim, CleanupPhase.MEDIA_UNLINKED)
        if intent.phase is CleanupPhase.MEDIA_UNLINKED:
            # Complete the database transition only after every expected name is absent.
            claim = self._renew_claim(intent.intent_id, claim)
            self._rewalk(resolved, intent.expected_private_path)
            if (
                self._entry(resolved.parent_fd, resolved.media_name) is not None
                or self._entry(resolved.parent_fd, resolved.media_name + _SIDECAR_SUFFIX) is not None
                or self._entry(resolved.parent_fd, os.fsencode(intent.quarantine_media_basename)) is not None
                or (intent.quarantine_sidecar_basename is not None
                    and self._entry(resolved.parent_fd, os.fsencode(intent.quarantine_sidecar_basename)) is not None)
            ):
                raise _CleanupFailure(CleanupReason.INCOMPLETE)
            self._checkpoint_call("before_complete_cleanup_group")
            self._store.complete_cleanup_group(intent.intent_id, claim)
            self._checkpoint_call("after_complete_cleanup_group")
            return True
        return False

    def _renew_claim(self, intent_id: str, claim: CleanupClaim) -> CleanupClaim:
        # Let the store's injected clock decide lease freshness for every physical checkpoint.
        return self._store.renew_cleanup_group(intent_id, claim, _LEASE_MS)

    def _verify_prepared_recovery(self, resolved: _ResolvedPath, intent: CleanupIntent) -> None:
        # Accept only exact pre-phase crash evidence; no new pathname is trusted after a partial move.
        media_expected = _FileIdentity(intent.media_device, intent.media_inode, intent.media_size, intent.media_mtime_ns)
        media_original = self._entry(resolved.parent_fd, resolved.media_name)
        media_quarantine = self._entry(resolved.parent_fd, os.fsencode(intent.quarantine_media_basename))
        if media_original is None and media_quarantine is None:
            raise _CleanupFailure(CleanupReason.INCOMPLETE)
        if not _valid_pair(media_original, media_quarantine, media_expected):
            raise _CleanupFailure(CleanupReason.INCOMPLETE)
        if intent.sidecar_device is None:
            if self._entry(resolved.parent_fd, resolved.media_name + _SIDECAR_SUFFIX) is not None:
                raise _CleanupFailure(CleanupReason.INCOMPLETE)
            return
        sidecar_expected = _FileIdentity(intent.sidecar_device, intent.sidecar_inode or 0, intent.sidecar_size or 0, intent.sidecar_mtime_ns or 0)
        sidecar_original = self._entry(resolved.parent_fd, resolved.media_name + _SIDECAR_SUFFIX)
        sidecar_quarantine = self._entry(resolved.parent_fd, os.fsencode(intent.quarantine_sidecar_basename or ""))
        if sidecar_original is None and sidecar_quarantine is None:
            raise _CleanupFailure(CleanupReason.INCOMPLETE)
        if not _valid_pair(sidecar_original, sidecar_quarantine, sidecar_expected):
            raise _CleanupFailure(CleanupReason.INCOMPLETE)

    def _verify_phase_state(self, resolved: _ResolvedPath, intent: CleanupIntent, phase: CleanupPhase) -> None:
        # Refuse to continue when any original or quarantine entry is ambiguous at a checkpoint.
        media_expected = _FileIdentity(intent.media_device, intent.media_inode, intent.media_size, intent.media_mtime_ns)
        media_original = self._entry(resolved.parent_fd, resolved.media_name)
        media_quarantine = self._entry(resolved.parent_fd, os.fsencode(intent.quarantine_media_basename))
        if phase is CleanupPhase.SIDECAR_QUARANTINED:
            if media_original is None and media_quarantine is None:
                raise _CleanupFailure(CleanupReason.INCOMPLETE)
        elif phase is CleanupPhase.MEDIA_QUARANTINED:
            if media_original is not None or media_quarantine is None:
                raise _CleanupFailure(CleanupReason.INCOMPLETE)
        elif phase is CleanupPhase.SIDECAR_UNLINKED:
            if media_original is not None and media_quarantine is None:
                raise _CleanupFailure(CleanupReason.INCOMPLETE)
        if media_original is not None and not _same_identity_and_nlink(media_original, media_expected, 1 if media_quarantine is None else 2):
            raise _CleanupFailure(CleanupReason.INCOMPLETE)
        if media_quarantine is not None and not _same_identity_and_nlink(media_quarantine, media_expected, 1 if media_original is None else 2):
            raise _CleanupFailure(CleanupReason.INCOMPLETE)
        sidecar_original_name = resolved.media_name + _SIDECAR_SUFFIX
        if intent.sidecar_device is None:
            if self._entry(resolved.parent_fd, sidecar_original_name) is not None or (
                intent.quarantine_sidecar_basename is not None
                and self._entry(resolved.parent_fd, os.fsencode(intent.quarantine_sidecar_basename)) is not None
            ):
                raise _CleanupFailure(CleanupReason.INCOMPLETE)
            return
        sidecar_expected = _FileIdentity(intent.sidecar_device, intent.sidecar_inode or 0, intent.sidecar_size or 0, intent.sidecar_mtime_ns or 0)
        sidecar_original = self._entry(resolved.parent_fd, sidecar_original_name)
        sidecar_quarantine = self._entry(resolved.parent_fd, os.fsencode(intent.quarantine_sidecar_basename or ""))
        if phase is CleanupPhase.SIDECAR_QUARANTINED:
            if sidecar_quarantine is None or sidecar_original is not None:
                raise _CleanupFailure(CleanupReason.INCOMPLETE)
        elif phase is CleanupPhase.MEDIA_QUARANTINED:
            if sidecar_original is not None:
                raise _CleanupFailure(CleanupReason.INCOMPLETE)
        elif phase is CleanupPhase.SIDECAR_UNLINKED:
            if sidecar_original is not None or sidecar_quarantine is not None:
                raise _CleanupFailure(CleanupReason.INCOMPLETE)
        if sidecar_quarantine is not None and not _same_identity_and_nlink(sidecar_quarantine, sidecar_expected, 1):
            raise _CleanupFailure(CleanupReason.INCOMPLETE)

    def _advance(self, intent: CleanupIntent, claim: CleanupClaim, phase: CleanupPhase) -> CleanupIntent:
        # Store advancement is the durable checkpoint after the corresponding fsync.
        # Stop before the transaction so a test or failure cannot hide the old phase.
        self._checkpoint_call(f"before_phase_{phase.value}_commit")
        updated = self._store.advance_cleanup_intent(intent.intent_id, claim, intent.phase, phase)
        # Expose both outcomes of the durable phase transaction to crash tests.
        self._checkpoint_call(f"after_phase_{phase.value}_commit")
        self._checkpoint_call(f"phase_{phase.value}")
        return updated

    def _revalidate_originals(self, resolved: _ResolvedPath, intent: CleanupIntent) -> None:
        # Recheck the current recording bytes and exact sidecar identity immediately before mutation.
        self._rewalk(resolved, intent.expected_private_path)
        media = self._media(resolved, intent.expected_recording_sha256)
        expected_media = _FileIdentity(intent.media_device, intent.media_inode, intent.media_size, intent.media_mtime_ns)
        if media != expected_media:
            raise _CleanupFailure(CleanupReason.CHANGED)
        sidecar, identity = self._sidecar(resolved)
        if intent.sidecar_device is None:
            if sidecar is not None:
                raise _CleanupFailure(CleanupReason.CHANGED)
        elif identity != _FileIdentity(intent.sidecar_device, intent.sidecar_inode or 0, intent.sidecar_size or 0, intent.sidecar_mtime_ns or 0):
            raise _CleanupFailure(CleanupReason.CHANGED)

    def _quarantine(
        self, resolved: _ResolvedPath, intent: CleanupIntent, kind: Literal["media", "sidecar"],
        *, renew: Callable[[], CleanupClaim] | None = None,
    ) -> bool:
        # Complete a quarantine move idempotently from either side of its fsync checkpoint.
        # Select the expected identity and checkpoint prefix without changing phase semantics.
        if kind == "sidecar":
            if intent.sidecar_device is None:
                return False
            original = resolved.media_name + _SIDECAR_SUFFIX
            quarantine = os.fsencode(intent.quarantine_sidecar_basename or "")
            expected = _FileIdentity(intent.sidecar_device, intent.sidecar_inode or 0, intent.sidecar_size or 0, intent.sidecar_mtime_ns or 0)
        else:
            original = resolved.media_name
            quarantine = os.fsencode(intent.quarantine_media_basename)
            expected = _FileIdentity(intent.media_device, intent.media_inode, intent.media_size, intent.media_mtime_ns)
        source = self._entry(resolved.parent_fd, original)
        target = self._entry(resolved.parent_fd, quarantine)
        changed = target is not None and source is None
        # Reject a quarantine name that does not match the journaled identity or link count.
        if target is not None:
            expected_count = 2 if source is not None else 1
            if not _same_identity_and_nlink(target, expected, expected_count):
                raise _CleanupFailure(CleanupReason.INCOMPLETE, mutated=True)
        if source is None and target is None:
            raise _CleanupFailure(CleanupReason.INCOMPLETE)
        if source is None and target is not None:
            # Finish a prior original-unlink crash by syncing the surviving quarantine name.
            self._mutation_fsync(resolved.parent_fd)
            self._mutation_checkpoint(f"{kind}_unlink_fsync")
            target_after = self._entry(resolved.parent_fd, quarantine)
            if target_after is None or not _same_identity_and_nlink(target_after, expected, 1):
                raise _CleanupFailure(CleanupReason.INCOMPLETE, mutated=True)
            return True
        if target is None:
            # Create a unique hard link only after the current claim is renewed.
            if renew is not None:
                renew()
            try:
                link_regular_file_no_replace_dirfd(resolved.parent_fd, original, resolved.parent_fd, quarantine)
            except Exception:
                raise _CleanupFailure(CleanupReason.INCOMPLETE, mutated=True)
            changed = True
            self._mutation_checkpoint(f"{kind}_link")
            self._mutation_fsync(resolved.parent_fd)
            self._mutation_checkpoint(f"{kind}_link_fsync")
            source = self._entry(resolved.parent_fd, original)
            target = self._entry(resolved.parent_fd, quarantine)
        if source is not None:
            # Verify the two-name state before removing the original entry.
            if not _same_identity_and_nlink(source, expected, 2 if target is not None else 1):
                raise _CleanupFailure(CleanupReason.INCOMPLETE, mutated=changed)
            if target is not None:
                # Sync the observed pair before making the original unlink durable.
                self._mutation_fsync(resolved.parent_fd)
                self._mutation_checkpoint(f"{kind}_both_fsync")
            try:
                if renew is not None:
                    renew()
                unlink_verified_file_dirfd(resolved.parent_fd, original, expected.device, expected.inode, expected.size, expected.mtime_ns, 2)
            except Exception:
                raise _CleanupFailure(CleanupReason.INCOMPLETE, mutated=True)
            changed = True
            self._mutation_checkpoint(f"{kind}_unlink")
            self._mutation_fsync(resolved.parent_fd)
            self._mutation_checkpoint(f"{kind}_unlink_fsync")
            target_after = self._entry(resolved.parent_fd, quarantine)
            if target_after is None or not _same_identity_and_nlink(target_after, expected, 1):
                raise _CleanupFailure(CleanupReason.INCOMPLETE, mutated=True)
        return changed

    def _unlink_quarantine(
        self, resolved: _ResolvedPath, intent: CleanupIntent, kind: Literal["media", "sidecar"],
        *, renew: Callable[[], CleanupClaim] | None = None,
    ) -> bool:
        # Remove only the exact quarantined identity; an unexpected original is ambiguous and stops safely.
        # Select the original name only for the ambiguity check and the journaled quarantine name for unlink.
        if kind == "sidecar":
            if intent.sidecar_device is None:
                return False
            original = resolved.media_name + _SIDECAR_SUFFIX
            quarantine = os.fsencode(intent.quarantine_sidecar_basename or "")
            expected = _FileIdentity(intent.sidecar_device, intent.sidecar_inode or 0, intent.sidecar_size or 0, intent.sidecar_mtime_ns or 0)
        else:
            original = resolved.media_name
            quarantine = os.fsencode(intent.quarantine_media_basename)
            expected = _FileIdentity(intent.media_device, intent.media_inode, intent.media_size, intent.media_mtime_ns)
        if self._entry(resolved.parent_fd, original) is not None:
            raise _CleanupFailure(CleanupReason.INCOMPLETE)
        target = self._entry(resolved.parent_fd, quarantine)
        if target is None:
            # Treat a missing quarantine name as an already completed unlink and resync the directory.
            self._mutation_fsync(resolved.parent_fd)
            self._mutation_checkpoint(f"{kind}_quarantine_unlink_fsync")
            return True
        if not _same_identity_and_nlink(target, expected, 1):
            raise _CleanupFailure(CleanupReason.INCOMPLETE)
        try:
            # Renew immediately before unlinking the exact quarantine identity.
            if renew is not None:
                renew()
            unlink_verified_file_dirfd(resolved.parent_fd, quarantine, expected.device, expected.inode, expected.size, expected.mtime_ns, 1)
        except Exception:
            raise _CleanupFailure(CleanupReason.INCOMPLETE, mutated=True)
        self._mutation_checkpoint(f"{kind}_quarantine_unlink")
        self._mutation_fsync(resolved.parent_fd)
        self._mutation_checkpoint(f"{kind}_quarantine_unlink_fsync")
        return True

    def _mutation_checkpoint(self, name: str) -> None:
        # Preserve the intent whenever a checkpoint fails after a namespace mutation.
        try:
            self._checkpoint_call(name)
        except Exception as failure:
            raise _CleanupFailure(CleanupReason.INCOMPLETE, mutated=True) from failure

    @staticmethod
    def _mutation_fsync(descriptor: int) -> None:
        # Preserve the intent whenever directory durability becomes ambiguous after mutation.
        try:
            fsync_recording_directory_fd(descriptor)
        except Exception as failure:
            raise _CleanupFailure(CleanupReason.INCOMPLETE, mutated=True) from failure

    def _checkpoint_call(self, name: str) -> None:
        # Tests may stop at named crash boundaries without changing production behavior.
        if self._checkpoint is not None:
            self._checkpoint(name)


__all__ = [
    "CleanupReason", "CleanupReport", "CleanupResult", "CleanupStatus", "PublicationCleanup",
]
