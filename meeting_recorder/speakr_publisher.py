"""Durable, restart-safe publication of finalized recordings to Speakr.

The publisher is deliberately a small orchestration layer.  SQLite transitions
and leases are kept in :mod:`speakr_store`; this module owns only transient
file descriptors, metadata projection, transport classification, and the
worker loop between those short transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import secrets
import socket
import stat
from threading import Event, Thread, current_thread
import time
from typing import BinaryIO, Callable, List, Sequence, cast
from uuid import uuid4

from .meeting_sidecar import MeetingSidecar, load_sidecar, sidecar_path
from .recording_paths import recording_directory_lock
from .speakr_domain import (
    MediaIdentity, PublicationJob, PublicationKey, PublicationOperation,
    PublicationResult, PublicationState, ResumeIntent, SpeakrMetadata,
    map_speakr_metadata, normalize_speakr_url,
)
from .speakr_http import (
    MetadataRejected, MetadataUnavailable, ReconciliationRejected,
    ReconciliationUnavailable, SpeakrTransport, TransferNotSent,
    TransferOutcomeUnknown, TransferRejected,
)
from .speakr_store import PublicationStore, PublicationTransitionError


_DEFAULT_CHUNK_SIZE = 1024 * 1024
_DEFAULT_LEASE_MS = 60_000
_BACKOFF_BASE_SECONDS = 60.0
_BACKOFF_CAP_SECONDS = 21_600.0
_STAGING_PREFIX = ".staging-"
_STALE_STAGING_SECONDS = 24 * 60 * 60
_MAX_FILENAME_BYTES = 255
_MAX_TITLE_CHARS = 4096


@dataclass(frozen=True)
class _StagedMedia:
    path: Path
    descriptor: int
    digest: str
    identity: MediaIdentity
    file_last_modified_ms: int
    descriptor_info: os.stat_result
    staging_info: os.stat_result


@dataclass(frozen=True)
class _DiscoveredMedia:
    path: Path
    sidecar: MeetingSidecar | None
    info: os.stat_result


class _MetadataProblem(ValueError):
    """A bounded metadata discovery failure with no private cause text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ConfigurationProblem(Exception):
    """A safe engine seam failure that occurs before transfer intent."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _LeaseHeartbeat:
    """Renew one claimed lease in a bounded, stoppable worker thread."""

    def __init__(
        self,
        store: PublicationStore,
        job_id: str,
        owner: str,
        generation: int,
        lease_ms: int,
        clock: Callable[[], int],
    ) -> None:
        self.store = store
        self.job_id = job_id
        self.owner = owner
        self.generation = generation
        self.lease_ms = lease_ms
        self.clock = clock
        self.interval_seconds = min(5.0, max(0.001, lease_ms / 3000.0))
        self.stop_event = Event()
        self.thread: Thread | None = None
        self.failure: Exception | None = None

    def start(self) -> None:
        # A non-daemon thread is joined by stop so a lease worker cannot leak.
        self.thread = Thread(target=self._run, name="speakr-lease-heartbeat", daemon=False)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread is not current_thread():
            thread.join()

    def check(self) -> None:
        failure = self.failure
        if failure is None:
            return
        if isinstance(failure, _ConfigurationProblem):
            raise failure
        if isinstance(failure, PublicationTransitionError):
            raise failure
        raise PublicationTransitionError("lease heartbeat failed") from None

    def _run(self) -> None:
        # Wake well before expiry and stop immediately when the owner finishes.
        while not self.stop_event.wait(self.interval_seconds):
            try:
                self.store.renew(
                    self.job_id, self.owner, self.generation,
                    lease_ms=self.lease_ms, now_ms=self.clock(),
                )
            except Exception as exc:
                self.failure = exc
                return


class _TrackingMedia:
    """Delegate a staged file while recording whether request bytes were read."""

    def __init__(self, media: BinaryIO) -> None:
        self.media = media
        self.bytes_read = False

    def read(self, *args: int) -> bytes:
        data = self.media.read(*args)
        if data:
            self.bytes_read = True
        return data

    def seek(self, *args: int) -> int:
        return self.media.seek(*args)

    def tell(self) -> int:
        return self.media.tell()

    def fileno(self) -> int:
        return self.media.fileno()


def _regular_mode(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _identity_tuple(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _source_flags() -> int:
    return (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _safe_filename(path: Path) -> str:
    """Bound the multipart filename without retaining or exposing its path."""
    value = "".join(
        "_" if ord(char) < 0x20 or ord(char) == 0x7F else char
        for char in path.name
    )
    value = _truncate_utf8(value, _MAX_FILENAME_BYTES)
    return value or "recording"


def _safe_title(title: str, marker: str) -> str:
    prefix = f"[mr:{marker}] "
    available = max(1, _MAX_TITLE_CHARS - len(prefix))
    return prefix + _truncate_utf8(title, available)


class SpeakrPublisher:
    """Shared publication engine used by explicit commands and workers."""

    def __init__(
        self,
        store: PublicationStore,
        transport: SpeakrTransport,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        *,
        worker_id: str | None = None,
        lease_ms: int = _DEFAULT_LEASE_MS,
        clock: Callable[[], int] | None = None,
        random_source: Callable[[], float] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(store, PublicationStore):
            raise ValueError("publication store is invalid")
        # Runtime protocol checks reject useful small fakes that implement only
        # the operations exercised by a test.  Validate the required boundary
        # here and fail safely when an optional reconciliation call is needed.
        if not callable(getattr(transport, "upload", None)) or not callable(
            getattr(transport, "patch_metadata", None)
        ):
            raise ValueError("Speakr transport is invalid")
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk size must be a positive integer")
        if type(lease_ms) is not int or not 1 <= lease_ms <= 86_400_000:
            raise ValueError("lease duration is invalid")
        if clock is not None and not callable(clock):
            raise ValueError("publisher clock is invalid")
        if random_source is not None and not callable(random_source):
            raise ValueError("publisher random source is invalid")
        if token_factory is not None and not callable(token_factory):
            raise ValueError("publisher token factory is invalid")

        self.store = store
        self.transport = transport
        self.chunk_size = chunk_size
        self.worker_id = worker_id or self._default_worker_id()
        self.lease_ms = lease_ms
        self._clock = clock or _now_ms
        self._random = random_source or secrets.SystemRandom().random
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))
        self._heartbeat_context: ContextVar[_LeaseHeartbeat | None] = ContextVar(
            "speakr_lease_heartbeat", default=None,
        )

    @staticmethod
    def _default_worker_id() -> str:
        try:
            host = socket.gethostname()
        except OSError:
            host = "host"
        safe_host = "".join(
            char if char.isascii() and (char.isalnum() or char in ".:-_") else "-"
            for char in host
        ).strip("-") or "host"
        return f"mr-{safe_host[:48]}-{os.getpid()}-{uuid4().hex[:16]}"

    def _now(self) -> int:
        try:
            value = self._clock()
        except Exception:
            raise _ConfigurationProblem("protocol_error") from None
        if type(value) is not int or value < 0:
            raise _ConfigurationProblem("protocol_error")
        return value

    @staticmethod
    def _origin(value: str) -> str:
        return normalize_speakr_url(value)

    def _new_marker(self) -> str:
        try:
            marker = self._token_factory()
        except Exception:
            raise _ConfigurationProblem("protocol_error") from None
        if (
            not isinstance(marker, str) or not marker or len(marker) > 128
            or any(not (char.isascii() and (char.isalnum() or char == "-")) for char in marker)
            or "%" in marker or "_" in marker
        ):
            raise _ConfigurationProblem("protocol_error")
        return marker

    @staticmethod
    def _validate_token(token: str) -> None:
        if (
            not isinstance(token, str) or not token or len(token) > 4096
            or any(char.isspace() or not char.isprintable() for char in token)
        ):
            raise _ConfigurationProblem("protocol_error")

    # ------------------------------------------------------------------
    # Public engine operations

    def enqueue(self, path: Path | str, instance_url: str) -> PublicationJob:
        """Securely snapshot ``path`` and create or reuse its origin/SHA job."""
        source = self._absolute_path(path)
        staged: _StagedMedia | None = None
        source_descriptor = -1
        try:
            staged, source_descriptor = self._stage(source)
            key = PublicationKey(instance_url, staged.digest)
            path_bytes = os.fsencode(source)
            job = self.store.create_or_reuse(
                key, path_bytes, staged.file_last_modified_ms, identity=staged.identity,
            )
            return job
        finally:
            if source_descriptor >= 0:
                self._close_descriptor(source_descriptor)
            if staged is not None:
                self._close_and_remove_staging(staged)

    def get(self, reference: PublicationKey | PublicationJob | str) -> PublicationJob | None:
        return self.store.get(reference)

    def list(
        self,
        states: Sequence[PublicationState | str] | None = None,
        instance_url: str | None = None,
    ) -> list[PublicationJob]:
        jobs = self.store.list(states)
        if instance_url is None:
            return jobs
        origin = self._origin(instance_url)
        return [job for job in jobs if job.key.instance_url == origin]

    def due_job_ids(
        self,
        instance_url: str,
        *,
        now_ms: int | None = None,
        limit: int = 100,
    ) -> tuple[str, ...]:
        """Return due IDs through the store using this publisher's clock."""
        origin = self._origin(instance_url)
        current = self._now() if now_ms is None else now_ms
        return self.store.due_job_ids(origin, now_ms=current, limit=limit)

    def next_wake_at_ms(self, instance_url: str, *, now_ms: int | None = None) -> int | None:
        """Return the next due deadline through the store using this publisher's clock."""
        origin = self._origin(instance_url)
        current = self._now() if now_ms is None else now_ms
        return self.store.next_wake_at_ms(origin, now_ms=current)

    def retry(
        self,
        reference: PublicationKey | PublicationJob | str,
        *,
        now_ms: int | None = None,
    ) -> PublicationJob:
        """Explicitly resume a job, verifying media before changing its phase."""
        job = self.store.get(reference)
        if job is None:
            raise ValueError("publication job does not exist")
        target = self._retry_target(job)

        # Store.retry intentionally permits an uncertain row to become POST;
        # hash the durable path first so that operator authorization cannot
        # accidentally authorize different bytes.
        if target in {PublicationState.QUEUED, PublicationState.METADATA_PENDING}:
            self._verify_job_digest(job)
        return self.store.retry(
            job.job_id, now_ms=self._now() if now_ms is None else now_ms,
        )

    def relink(
        self,
        reference: PublicationKey | PublicationJob | str,
        new_path: Path | str,
        *,
        now_ms: int | None = None,
    ) -> PublicationJob:
        """Securely verify a replacement path before changing the local row."""
        job = self.store.get(reference)
        if job is None:
            raise ValueError("publication job does not exist")
        replacement = self._absolute_path(new_path)
        staged: _StagedMedia | None = None
        descriptor = -1
        try:
            staged, descriptor = self._stage(replacement)
            if staged.digest != job.key.recording_sha256:
                raise ValueError("replacement recording does not match publication SHA-256")
        finally:
            if descriptor >= 0:
                self._close_descriptor(descriptor)
            if staged is not None:
                self._close_and_remove_staging(staged)
        return self.store.relink(
            job.job_id, os.fsencode(replacement), now_ms=self._now() if now_ms is None else now_ms,
        )

    def forget(self, reference: PublicationKey | PublicationJob | str) -> None:
        """Forget only the local row; no remote request is made."""
        self.store.forget(reference)

    def run_one(
        self,
        instance_url: str,
        token: str,
        reference: PublicationKey | PublicationJob | str | None = None,
        *,
        job_id: str | None = None,
    ) -> PublicationResult | None:
        """Claim and execute one due job for exactly ``instance_url``."""
        if reference is not None and job_id is not None:
            raise ValueError("publication reference was supplied twice")
        reference = job_id if job_id is not None else reference
        origin = self._origin(instance_url)
        if reference is not None:
            existing = self.store.get(reference)
            if existing is None:
                return None
            if existing.key.instance_url != origin:
                raise ValueError("Speakr origin does not match the publication job")
        claimed: PublicationJob | None = None
        if reference is None:
            # Query the origin-filtered due index before fencing any job.
            for candidate_id in self.due_job_ids(origin, now_ms=self._now(), limit=1):
                claimed = self.store.claim_one(
                    self.worker_id, candidate_id,
                    lease_ms=self.lease_ms, now_ms=self._now(),
                )
                if claimed is not None:
                    break
        else:
            claimed = self.store.claim_one(
                self.worker_id, reference, lease_ms=self.lease_ms, now_ms=self._now(),
            )
        if claimed is None:
            if reference is None:
                return None
            current = self.store.get(reference)
            return None if current is None else self._result(current, current.state is PublicationState.PUBLISHED)
        return self._run_claimed(claimed, origin, token)

    def block_configuration(
        self, reference: PublicationKey | PublicationJob | str,
        *,
        instance_url: str | None = None,
    ) -> PublicationJob | None:
        """Claim one due referenced job and block it without contacting Speakr."""
        existing = self.store.get(reference)
        if existing is None:
            return None
        claimed = self.store.claim_for_action(
            existing.job_id, self.worker_id, self.lease_ms,
            instance_url=instance_url,
        )
        if claimed is None:
            return self.store.get(existing.job_id)
        return self._block(claimed, "protocol_error", None).job

    def run_due(
        self,
        instance_url: str,
        token: str,
        limit: int = 100,
    ) -> List[PublicationResult]:
        """Claim and execute a bounded due batch without crossing origins."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("run limit is invalid")
        origin = self._origin(instance_url)
        results: List[PublicationResult] = []

        # Snapshot bounded origin-filtered IDs before fencing any job.
        snapshot_now = self._now()
        snapshot_ids = self.due_job_ids(origin, now_ms=snapshot_now, limit=limit)
        for candidate_id in snapshot_ids:
            if len(results) >= limit:
                break
            claimed = self.store.claim_one(
                self.worker_id, candidate_id,
                lease_ms=self.lease_ms, now_ms=snapshot_now,
            )
            if claimed is None:
                continue
            results.append(self._run_claimed(claimed, origin, token))
        return results

    def run_all_due(self, instance_url: str, token: str) -> List[PublicationResult]:
        """Execute each job due at command start at most once in stable order."""
        origin = self._origin(instance_url)
        snapshot_now = self._now()
        # Take one bounded database snapshot so zero-delay retries cannot re-enter this command.
        snapshot_ids = self.due_job_ids(origin, now_ms=snapshot_now, limit=1_000)
        results: List[PublicationResult] = []

        # Each stable ID belongs to the initial due snapshot, so retry timing
        # changes cannot make one job occupy another job's command slot.
        for job_id in snapshot_ids:
            result = self.run_one(origin, token, job_id)
            if result is not None:
                results.append(result)
        return results

    def publish(self, path: Path | str, instance_url: str, token: str) -> PublicationResult:
        """Compatibility facade: enqueue, then execute through the lease engine."""
        job = self.enqueue(path, instance_url)
        if job.state is PublicationState.PUBLISHED:
            return self._result(job, True)
        result = self.run_one(instance_url, token, job.job_id)
        if result is not None:
            return result
        current = self.store.get(job.job_id)
        return self._result(job if current is None else current)

    # ------------------------------------------------------------------
    # Claimed-job execution

    def _run_claimed(
        self, claimed: PublicationJob, origin: str, token: str,
    ) -> PublicationResult:
        # Bind the heartbeat to this lease before any phase can contact Speakr.
        heartbeat = _LeaseHeartbeat(
            self.store, claimed.job_id, self.worker_id, claimed.lease_generation,
            self.lease_ms, self._now,
        )
        context_token = self._heartbeat_context.set(heartbeat)
        try:
            try:
                # Keep the lease alive while the selected phase performs I/O.
                heartbeat.start()
                job = claimed

                # Reject unusable credentials before staging or sending bytes.
                self._validate_token(token)

                # Continue the durable job from its current persisted phase.
                if job.state is PublicationState.QUEUED:
                    return self._run_post(job, origin, token)
                if job.state is PublicationState.UNCERTAIN and job.reconciliation_eligible:
                    return self._run_reconciliation(job, origin, token)
                if job.state is PublicationState.METADATA_PENDING:
                    return self._run_patch(job, origin, token)
                return self._result(job, job.state is PublicationState.PUBLISHED)
            except PublicationTransitionError:
                # A lease loss may occur after an external side effect.  The stale
                # worker must not transition the row and must report current state.
                current = self.store.get(claimed.job_id)
                return self._result(claimed if current is None else current)
            except _ConfigurationProblem as exc:
                try:
                    return self._block(claimed, exc.code, None)
                except PublicationTransitionError:
                    current = self.store.get(claimed.job_id)
                    return self._result(claimed if current is None else current)
        finally:
            # Stop and detach the heartbeat before returning the phase result.
            heartbeat.stop()
            self._heartbeat_context.reset(context_token)

    def _run_post(self, job: PublicationJob, origin: str, token: str) -> PublicationResult:
        staged: _StagedMedia | None = None
        source_descriptor = -1
        try:
            staged, source_descriptor = self._stage(self._job_path(job))
            if staged.digest != job.key.recording_sha256:
                return self._mark_missing(job, "local_missing")
            job = self._renew(job)

            # Metadata is transient and is read for this attempt only.  The
            # same-command inode lookup supports a rename without scanning for
            # an externally moved file after a restart.
            discovered, metadata = self._current_metadata(
                staged, source_descriptor, job, owner=True,
            )
            job = self._renew(job)
            self._require_unchanged(source_descriptor, staged.descriptor_info)
            self._require_staged_unchanged(staged)
            marker = self._new_marker()

            # This is the last durable step before request bytes may leave the
            # process: transfer intent and its exact reconciliation marker are
            # committed in one short transaction.
            job = self.store.transition(
                job.job_id, PublicationState.TRANSFERRING,
                owner=self.worker_id, generation=job.lease_generation,
                expected_state=PublicationState.QUEUED,
                reconciliation_token=marker, now_ms=self._now(),
            )
            self._require_staged_unchanged(staged)
            job = self._renew(job)
            remote_id: int | None = None
            upload_error: Exception | None = None
            try:
                remote_id = self._upload(
                    origin, token, _safe_filename(discovered.path), staged,
                    metadata.meeting_date, _safe_title(metadata.title, marker),
                )
            except Exception as exc:
                upload_error = exc

            # A staging mutation after the request is potentially a sent-result
            # ambiguity; never durably accept an ID from that snapshot.
            try:
                self._require_staged_unchanged(staged)
            except ValueError:
                return self._move_to_uncertain(job, "transfer_unknown", None, origin, token)
            # Preserve a same-command rename for the post-transfer PATCH while
            # the source descriptor still proves which inode was uploaded.
            self._refresh_current_path(staged, source_descriptor, job)
            if upload_error is not None:
                return self._classify_post_error(job, upload_error, origin, token)
            if type(remote_id) is not int or remote_id <= 0:
                return self._move_to_uncertain(job, "transfer_unknown", None, origin, token)

            # Persist the remote ID before any PATCH.  The new state is claimed
            # again because store transitions release the previous lease.
            self._prepare_transition()
            pending = self.store.transition(
                job.job_id, PublicationState.METADATA_PENDING,
                owner=self.worker_id, generation=job.lease_generation,
                expected_state=PublicationState.TRANSFERRING,
                remote_recording_id=remote_id, now_ms=self._now(),
            )
            return self._claim_and_continue(pending, origin, token)
        except (_MetadataProblem, ValueError) as exc:
            if isinstance(exc, PublicationTransitionError):
                raise
            if isinstance(exc, _MetadataProblem):
                return self._metadata_failure(job, exc.code)
            return self._mark_missing(job, "local_missing")
        finally:
            self._close_descriptor(source_descriptor)
            if staged is not None:
                self._close_and_remove_staging(staged)

    def _run_reconciliation(
        self, job: PublicationJob, origin: str, token: str,
    ) -> PublicationResult:
        marker = job.reconciliation_token
        if marker is None:
            return self._terminal_uncertain(job, "reconciliation_failed", None)
        job = self._renew(job)
        try:
            ids = self._reconcile(origin, token, marker)
        except ReconciliationRejected as exc:
            if exc.status in {408, 429} or 500 <= exc.status <= 599:
                return self._schedule(job, "reconciliation_failed", exc.status, exc.retry_after)
            return self._block(job, "reconciliation_failed", exc.status)
        except _ConfigurationProblem:
            raise
        except Exception as exc:
            retry_after = getattr(exc, "retry_after", None)
            return self._schedule(job, "reconciliation_failed", None, retry_after)

        if len(ids) != 1:
            # A zero or multiple marker match cannot identify one remote row;
            # terminal uncertainty is safer than authorizing another POST.
            return self._terminal_uncertain(job, "reconciliation_failed", None)
        self._prepare_transition()
        pending = self.store.transition(
            job.job_id, PublicationState.METADATA_PENDING,
            owner=self.worker_id, generation=job.lease_generation,
            expected_state=PublicationState.UNCERTAIN,
            remote_recording_id=ids[0], now_ms=self._now(),
        )
        return self._claim_and_continue(pending, origin, token)

    def _run_patch(self, job: PublicationJob, origin: str, token: str) -> PublicationResult:
        if job.remote_recording_id is None:
            return self._block(job, "metadata_failed", None)
        staged: _StagedMedia | None = None
        source_descriptor = -1
        try:
            staged, source_descriptor = self._stage(self._job_path(job))
            if staged.digest != job.key.recording_sha256:
                return self._mark_missing(job, "local_missing")
            job = self._renew(job)

            # Re-read pathname and sidecar immediately before the authoritative
            # PATCH, so a rename or metadata edit cannot use stale local data.
            discovered, metadata = self._current_metadata(
                staged, source_descriptor, job, owner=True,
            )
            self._require_unchanged(source_descriptor, staged.descriptor_info)
            self._require_staged_unchanged(staged)
            job = self._renew(job)
            remote_id = job.remote_recording_id
            if remote_id is None:
                return self._block(job, "metadata_failed", None)
            try:
                self.transport.patch_metadata(
                    origin, token, remote_id, metadata,
                )
            except MetadataRejected as exc:
                if exc.status in {408, 429} or 500 <= exc.status <= 599:
                    return self._schedule(job, "metadata_failed", exc.status, exc.retry_after)
                return self._block(job, "metadata_failed", exc.status)
            except MetadataUnavailable:
                return self._schedule(job, "metadata_failed", None)
            except (TypeError, ValueError) as exc:
                if isinstance(exc, PublicationTransitionError):
                    raise
                return self._block(job, "protocol_error", None)
            except Exception:
                return self._schedule(job, "metadata_failed", None)
            self._prepare_transition()
            return self.store.mark_published(
                job.job_id, owner=self.worker_id, generation=job.lease_generation,
            )
        except _MetadataProblem as exc:
            return self._metadata_failure(job, exc.code)
        except ValueError as exc:
            if isinstance(exc, PublicationTransitionError):
                raise
            return self._mark_missing(job, "local_missing")
        finally:
            self._close_descriptor(source_descriptor)
            if staged is not None:
                self._close_and_remove_staging(staged)

    def _claim_and_continue(
        self, previous: PublicationJob, origin: str, token: str,
    ) -> PublicationResult:
        self._prepare_transition()
        claimed = self.store.claim_one(
            self.worker_id, previous.job_id,
            lease_ms=self.lease_ms, now_ms=self._now(),
        )
        if claimed is None:
            current = self.store.get(previous.job_id)
            return self._result(previous if current is None else current)
        return self._run_claimed(claimed, origin, token)

    def _renew(self, job: PublicationJob) -> PublicationJob:
        heartbeat = self._heartbeat_context.get()
        if heartbeat is not None:
            heartbeat.check()
        renewed = self.store.renew(
            job.job_id, self.worker_id, job.lease_generation,
            lease_ms=self.lease_ms, now_ms=self._now(),
        )
        if heartbeat is not None:
            heartbeat.check()
        return renewed

    def _prepare_transition(self) -> None:
        heartbeat = self._heartbeat_context.get()
        if heartbeat is None:
            return
        heartbeat.check()
        heartbeat.stop()

    def _current_metadata(
        self,
        staged: _StagedMedia,
        source_descriptor: int,
        job: PublicationJob,
        *,
        owner: bool,
    ) -> tuple[_DiscoveredMedia, SpeakrMetadata]:
        del owner  # retained in the signature to make the lease checkpoint explicit at call sites
        try:
            discovered = self._discover_current(
                staged.identity.path.parent, source_descriptor,
                staged.descriptor_info, staged.identity,
            )
            metadata = map_speakr_metadata(
                discovered.path, discovered.info.st_mtime_ns, discovered.sidecar,
            )
        except _MetadataProblem:
            raise
        except Exception:
            raise _MetadataProblem("metadata_malformed") from None

        current_bytes = os.fsencode(discovered.path)
        if job.private_path != current_bytes:
            self.store.relink(
                job.job_id, current_bytes, owner=self.worker_id,
                generation=job.lease_generation, now_ms=self._now(),
            )
        return discovered, metadata

    def _refresh_current_path(
        self, staged: _StagedMedia, source_descriptor: int, job: PublicationJob,
    ) -> None:
        try:
            discovered = self._discover_current(
                staged.identity.path.parent, source_descriptor,
                staged.descriptor_info, staged.identity,
            )
        except _MetadataProblem:
            return
        current_bytes = os.fsencode(discovered.path)
        if job.private_path != current_bytes:
            self.store.relink(
                job.job_id, current_bytes, owner=self.worker_id,
                generation=job.lease_generation, now_ms=self._now(),
            )

    def _upload(
        self,
        origin: str,
        token: str,
        filename: str,
        staged: _StagedMedia,
        meeting_date: datetime,
        title: str,
    ) -> int:
        os.lseek(staged.descriptor, 0, os.SEEK_SET)
        duplicate = os.dup(staged.descriptor)
        try:
            with os.fdopen(duplicate, "rb") as raw_media:
                duplicate = -1
                media = _TrackingMedia(raw_media)
                try:
                    return self.transport.upload(
                        origin, token, cast(BinaryIO, media), staged.identity.size, filename,
                        staged.file_last_modified_ms, meeting_date, title=title,
                    )
                except (TypeError, ValueError):
                    if not media.bytes_read:
                        raise _ConfigurationProblem("protocol_error") from None
                    raise
        finally:
            if duplicate >= 0:
                self._close_descriptor(duplicate)

    def _reconcile(self, origin: str, token: str, marker: str) -> tuple[int, ...]:
        method = getattr(self.transport, "reconcile_recordings", None)
        if not callable(method):
            raise ReconciliationUnavailable()
        try:
            result = method(origin, token, marker)
        except (TypeError, ValueError):
            raise _ConfigurationProblem("protocol_error") from None
        if not isinstance(result, (tuple, list)) or any(
            type(item) is not int or item <= 0 for item in result
        ):
            raise ReconciliationUnavailable()
        return tuple(result)

    # ------------------------------------------------------------------
    # Transition/classification helpers

    @staticmethod
    def _retry_target(job: PublicationJob) -> PublicationState:
        if job.state is PublicationState.UNCERTAIN:
            return PublicationState.QUEUED
        if job.state in {PublicationState.BLOCKED, PublicationState.MISSING}:
            return {
                ResumeIntent.POST.value: PublicationState.QUEUED,
                ResumeIntent.RECONCILE.value: PublicationState.UNCERTAIN,
                ResumeIntent.PATCH.value: PublicationState.METADATA_PENDING,
            }.get(job.resume_intent, PublicationState.LOCAL_REMOVED)
        if job.state in {PublicationState.QUEUED, PublicationState.METADATA_PENDING}:
            return job.state
        raise ValueError("publication job is not retryable")

    def _verify_job_digest(self, job: PublicationJob) -> None:
        if job.private_path is None:
            raise ValueError("publication job has no local media path")
        staged: _StagedMedia | None = None
        descriptor = -1
        try:
            staged, descriptor = self._stage(self._job_path(job))
            if staged.digest != job.key.recording_sha256:
                raise ValueError("current recording does not match publication SHA-256")
        finally:
            self._close_descriptor(descriptor)
            if staged is not None:
                self._close_and_remove_staging(staged)

    def _classify_post_error(
        self, job: PublicationJob, error: Exception, origin: str, token: str,
    ) -> PublicationResult:
        if isinstance(error, PublicationTransitionError):
            raise error
        if isinstance(error, _ConfigurationProblem):
            return self._block(job, error.code, None)
        if isinstance(error, TransferNotSent):
            return self._safe_requeue_post(
                job, "transfer_not_sent", None, getattr(error, "retry_after", None),
            )
        if isinstance(error, TransferRejected):
            if error.status == 429:
                return self._safe_requeue_post(
                    job, "transfer_rejected", error.status, error.retry_after,
                )
            if 500 <= error.status <= 599 or error.status == 408:
                return self._move_to_uncertain(job, "transfer_unknown", error.status, origin, token)
            return self._block(job, "transfer_rejected", error.status)
        if isinstance(error, (TransferOutcomeUnknown, ValueError, TypeError)):
            return self._move_to_uncertain(job, "transfer_unknown", None, origin, token)
        return self._move_to_uncertain(job, "transfer_unknown", None, origin, token)

    def _move_to_uncertain(
        self,
        job: PublicationJob,
        error_code: str,
        status: int | None,
        origin: str,
        token: str,
    ) -> PublicationResult:
        self._prepare_transition()
        uncertain = self.store.transition(
            job.job_id, PublicationState.UNCERTAIN,
            owner=self.worker_id, generation=job.lease_generation,
            expected_state=PublicationState.TRANSFERRING,
            operation=PublicationOperation.RECONCILE,
            error_code=error_code, http_status=status, now_ms=self._now(),
        )
        # Reconcile immediately when the lease can be reclaimed.  A second
        # claim is required because terminal/non-terminal transitions release
        # their previous lease in PublicationStore.
        return self._claim_and_continue(uncertain, origin, token)

    def _safe_requeue_post(
        self,
        job: PublicationJob,
        error_code: str,
        status: int | None,
        retry_after: float | None,
    ) -> PublicationResult:
        self._prepare_transition()
        uncertain = self.store.transition(
            job.job_id, PublicationState.UNCERTAIN,
            owner=self.worker_id, generation=job.lease_generation,
            expected_state=PublicationState.TRANSFERRING,
            operation=PublicationOperation.RECONCILE,
            error_code=error_code, now_ms=self._now(),
        )
        # TransferNotSent and a completed 429 are the only automatic POST
        # requeue cases; the intermediate claim keeps this exception fenced.
        reclaimed = self.store.claim_one(
            self.worker_id, uncertain.job_id,
            lease_ms=self.lease_ms, now_ms=self._now(),
        )
        if reclaimed is None:
            current = self.store.get(job.job_id)
            return self._result(uncertain if current is None else current)
        queued = self.store.retry(
            reclaimed.job_id, owner=self.worker_id,
            generation=reclaimed.lease_generation, now_ms=self._now(),
        )
        claimed = self.store.claim_one(
            self.worker_id, queued.job_id,
            lease_ms=self.lease_ms, now_ms=self._now(),
        )
        if claimed is None:
            current = self.store.get(job.job_id)
            return self._result(queued if current is None else current)
        return self._schedule(claimed, error_code, status, retry_after)

    def _schedule(
        self,
        job: PublicationJob,
        error_code: str,
        status: int | None,
        retry_after: float | None = None,
    ) -> PublicationResult:
        self._prepare_transition()
        delay = retry_after if isinstance(retry_after, (int, float)) else None
        if delay is None:
            # Attempts are intentionally unbounded; only the delay is capped.
            exponent = max(0, min(16, job.attempt_count - 1))
            bound = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** exponent))
            try:
                sample = self._random()
            except Exception:
                raise _ConfigurationProblem("protocol_error") from None
            if isinstance(sample, bool) or not isinstance(sample, (int, float)):
                raise _ConfigurationProblem("protocol_error")
            delay = bound * max(0.0, min(1.0, float(sample)))
        delay = max(0.0, min(_BACKOFF_CAP_SECONDS, float(delay)))
        next_attempt = self._now() + int(delay * 1000)
        scheduled = self.store.schedule(
            job.job_id, self.worker_id, job.lease_generation,
            next_attempt_at_ms=next_attempt, error_code=error_code,
            http_status=status, now_ms=self._now(),
        )
        return self._result(scheduled)

    def _metadata_failure(self, job: PublicationJob, code: str) -> PublicationResult:
        if code in {"metadata_missing", "metadata_changed", "metadata_unavailable"}:
            return self._mark_missing(job, "local_missing")
        return self._schedule(job, code, None)

    def _block(
        self, job: PublicationJob, error_code: str, status: int | None,
    ) -> PublicationResult:
        self._prepare_transition()
        blocked = self.store.transition(
            job.job_id, PublicationState.BLOCKED,
            owner=self.worker_id, generation=job.lease_generation,
            error_code=error_code, http_status=status, now_ms=self._now(),
        )
        return self._result(blocked)

    def _mark_missing(self, job: PublicationJob, error_code: str) -> PublicationResult:
        self._prepare_transition()
        missing = self.store.transition(
            job.job_id, PublicationState.MISSING,
            owner=self.worker_id, generation=job.lease_generation,
            error_code=error_code, now_ms=self._now(),
        )
        return self._result(missing)

    def _terminal_uncertain(
        self, job: PublicationJob, error_code: str, status: int | None,
    ) -> PublicationResult:
        self._prepare_transition()
        uncertain = self.store.transition(
            job.job_id, PublicationState.UNCERTAIN,
            owner=self.worker_id, generation=job.lease_generation,
            expected_state=PublicationState.UNCERTAIN,
            operation=PublicationOperation.NONE,
            error_code=error_code, http_status=status, now_ms=self._now(),
        )
        return self._result(uncertain)

    @staticmethod
    def _result(job: PublicationJob, already_published: bool = False) -> PublicationResult:
        return PublicationResult(job, already_published)

    # ------------------------------------------------------------------
    # Secure local media handling

    @staticmethod
    def _job_path(job: PublicationJob) -> Path:
        if job.private_path is None:
            raise ValueError("publication job has no local media path")
        return SpeakrPublisher._absolute_path(os.fsdecode(job.private_path))

    @staticmethod
    def _absolute_path(path: Path | str) -> Path:
        """Make a durable pathname absolute without resolving its final entry."""
        try:
            # abspath normalizes cwd and dot segments but does not follow the
            # final symlink; O_NOFOLLOW below remains the first final-entry check.
            return Path(os.path.abspath(os.fspath(path)))
        except (OSError, TypeError, ValueError):
            raise ValueError("recording media path is invalid") from None

    def _stage(self, source: Path) -> tuple[_StagedMedia, int]:
        source_descriptor = -1
        stage_descriptor = -1
        stage_path: Path | None = None
        try:
            self._clean_stale_staging()
            # The directory lock closes the initial pathname/open race; the
            # descriptor then anchors the snapshot through same-command renames.
            with recording_directory_lock(source.parent):
                try:
                    source_descriptor = os.open(source, _source_flags())
                    source_info = os.fstat(source_descriptor)
                except OSError:
                    raise ValueError("recording media is invalid") from None
                if not _regular_mode(source_info):
                    raise ValueError("recording media is invalid")
                stage_path, stage_descriptor = self._new_staging_file()
                # Copy and fsync a private snapshot before hashing so the exact
                # descriptor sent later is the descriptor named by the digest.
                remaining = source_info.st_size
                written = 0
                while remaining:
                    chunk = os.read(source_descriptor, min(self.chunk_size, remaining))
                    if not chunk:
                        raise ValueError("recording media changed during staging")
                    offset = 0
                    while offset < len(chunk):
                        count = os.write(stage_descriptor, chunk[offset:])
                        if count <= 0:
                            raise ValueError("recording staging failed")
                        offset += count
                    written += len(chunk)
                    remaining -= len(chunk)
                if written != source_info.st_size:
                    raise ValueError("recording media changed during staging")
                os.fsync(stage_descriptor)
                if _identity_tuple(os.fstat(source_descriptor)) != _identity_tuple(source_info):
                    raise ValueError("recording media changed during staging")
                staging_info = os.fstat(stage_descriptor)
                if (
                    not _regular_mode(staging_info)
                    or stat.S_IMODE(staging_info.st_mode) != 0o600
                    or staging_info.st_size != source_info.st_size
                ):
                    raise ValueError("recording staging failed")
                # Hash only a complete, identity-checked staging inode.
                digest, staging_info = self._hash_staged(stage_descriptor, staging_info)

            identity = MediaIdentity(
                source, source_info.st_dev, source_info.st_ino,
                source_info.st_size, source_info.st_mtime_ns,
            )
            staged = _StagedMedia(
                stage_path, stage_descriptor, digest, identity,
                source_info.st_mtime_ns // 1_000_000,
                source_info, staging_info,
            )
            stage_descriptor = -1
            return staged, source_descriptor
        except Exception:
            self._close_descriptor(stage_descriptor)
            if stage_path is not None:
                self._remove_owned_staging(stage_path)
            self._close_descriptor(source_descriptor)
            raise

    def _hash_staged(
        self, descriptor: int, expected: os.stat_result,
    ) -> tuple[str, os.stat_result]:
        before = os.fstat(descriptor)
        if _identity_tuple(before) != _identity_tuple(expected):
            raise ValueError("recording staging changed")
        digest = hashlib.sha256()
        remaining = before.st_size
        os.lseek(descriptor, 0, os.SEEK_SET)
        while remaining:
            chunk = os.read(descriptor, min(self.chunk_size, remaining))
            if not chunk or len(chunk) > remaining:
                raise ValueError("recording staging changed")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if _identity_tuple(after) != _identity_tuple(before):
            raise ValueError("recording staging changed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return digest.hexdigest(), after

    @staticmethod
    def _require_staged_unchanged(staged: _StagedMedia) -> None:
        try:
            actual = os.fstat(staged.descriptor)
        except OSError:
            raise ValueError("recording staging is unavailable") from None
        if not _regular_mode(actual) or _identity_tuple(actual) != _identity_tuple(staged.staging_info):
            raise ValueError("recording staging changed")

    @staticmethod
    def _require_unchanged(descriptor: int, expected: os.stat_result) -> None:
        try:
            actual = os.fstat(descriptor)
        except OSError:
            raise ValueError("recording media is unavailable") from None
        if not _regular_mode(actual) or _identity_tuple(actual) != _identity_tuple(expected):
            raise ValueError("recording media changed")

    def _new_staging_file(self) -> tuple[Path, int]:
        for _ in range(8):
            path = self.store.state_directory / f"{_STAGING_PREFIX}{os.getpid()}-{secrets.token_hex(12)}"
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            except OSError:
                raise ValueError("recording staging failed") from None
            try:
                info = os.fstat(descriptor)
                if not _regular_mode(info) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError("recording staging failed")
            except Exception:
                self._close_descriptor(descriptor)
                self._remove_owned_staging(path)
                raise
            return path, descriptor
        raise ValueError("recording staging failed")

    def _discover_current(
        self,
        parent: Path,
        source_descriptor: int,
        expected_info: os.stat_result,
        identity: MediaIdentity,
    ) -> _DiscoveredMedia:
        with recording_directory_lock(parent):
            try:
                source_info = os.fstat(source_descriptor)
            except OSError:
                raise _MetadataProblem("metadata_missing") from None
            if not _regular_mode(source_info) or _identity_tuple(source_info) != _identity_tuple(expected_info):
                raise _MetadataProblem("metadata_changed")
            try:
                entries = list(os.scandir(parent))
            except OSError:
                raise _MetadataProblem("metadata_unavailable") from None
            # Select an inode match, preferring the one sidecar whose filename
            # intent names it; unrelated hard links must not leak metadata.
            candidates: list[tuple[Path, os.stat_result, MeetingSidecar | None]] = []
            strict: list[tuple[Path, os.stat_result, MeetingSidecar]] = []
            for entry in entries:
                candidate = Path(entry.path)
                try:
                    info = os.lstat(candidate)
                except FileNotFoundError:
                    raise _MetadataProblem("metadata_changed") from None
                except OSError:
                    continue
                if not _regular_mode(info) or (info.st_dev, info.st_ino) != (source_info.st_dev, source_info.st_ino):
                    continue
                sidecar: MeetingSidecar | None = None
                adjacent = sidecar_path(candidate)
                if os.path.lexists(adjacent):
                    try:
                        sidecar = load_sidecar(adjacent)
                    except (OSError, ValueError):
                        raise _MetadataProblem("metadata_malformed") from None
                candidates.append((candidate, info, sidecar))
                if sidecar is not None and sidecar.recording_filename == candidate.name:
                    strict.append((candidate, info, sidecar))
            if not candidates:
                raise _MetadataProblem("metadata_missing")
            if len(candidates) > 1:
                if len(strict) != 1:
                    raise _MetadataProblem("metadata_ambiguous")
                selected_path, selected_info, selected_sidecar = strict[0]
            else:
                selected_path, selected_info, selected_sidecar = candidates[0]
            if selected_sidecar is not None and selected_sidecar.recording_filename != selected_path.name:
                selected_sidecar = None
            try:
                final_info = os.lstat(selected_path)
                source_info = os.fstat(source_descriptor)
            except OSError:
                raise _MetadataProblem("metadata_changed") from None
            if (
                not _regular_mode(final_info)
                or _identity_tuple(final_info) != _identity_tuple(selected_info)
                or _identity_tuple(source_info) != _identity_tuple(expected_info)
            ):
                raise _MetadataProblem("metadata_changed")
            return _DiscoveredMedia(selected_path, selected_sidecar, final_info)

    def _clean_stale_staging(self) -> None:
        now = time.time()
        try:
            entries = list(self.store.state_directory.iterdir())
        except OSError:
            return
        for candidate in entries:
            if not candidate.name.startswith(_STAGING_PREFIX):
                continue
            try:
                info = os.lstat(candidate)
                if _regular_mode(info) and now - info.st_mtime > _STALE_STAGING_SECONDS:
                    os.unlink(candidate)
            except OSError:
                continue

    def _close_and_remove_staging(self, staged: _StagedMedia) -> None:
        self._close_descriptor(staged.descriptor)
        try:
            info = os.lstat(staged.path)
            if _regular_mode(info) and info.st_dev == staged.staging_info.st_dev and info.st_ino == staged.staging_info.st_ino:
                os.unlink(staged.path)
        except OSError:
            pass

    @staticmethod
    def _remove_owned_staging(path: Path) -> None:
        try:
            info = os.lstat(path)
            if _regular_mode(info):
                os.unlink(path)
        except OSError:
            pass

    @staticmethod
    def _close_descriptor(descriptor: int) -> None:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


__all__ = ["SpeakrPublisher"]
