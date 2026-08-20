"""Durable, restart-safe publication of finalized recordings to Speakr."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
import stat
import time

from .meeting_sidecar import MeetingSidecar, load_sidecar, sidecar_path
from .recording_paths import recording_directory_lock
from .speakr_domain import (
    MediaIdentity, PublicationJob, PublicationKey, PublicationResult, PublicationState,
    map_speakr_metadata,
)
from .speakr_http import (
    MetadataRejected, MetadataUnavailable, SpeakrTransport, TransferNotSent,
    TransferOutcomeUnknown, TransferRejected,
)
from .speakr_store import PublicationStore


_DEFAULT_CHUNK_SIZE = 1024 * 1024
_STAGING_PREFIX = ".staging-"
_STALE_STAGING_SECONDS = 24 * 60 * 60


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

    def __init__(self, code: str, path: Path) -> None:
        super().__init__(code)
        self.code = code
        self.path = path


def _regular_mode(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _identity_tuple(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _source_flags() -> int:
    return (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


class SpeakrPublisher:
    """Publish one recording while keeping all durable state public and bounded."""

    def __init__(
        self, store: PublicationStore, transport: SpeakrTransport,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
    ) -> None:
        if not isinstance(store, PublicationStore):
            raise ValueError("publication store is invalid")
        if not isinstance(transport, SpeakrTransport):
            raise ValueError("Speakr transport is invalid")
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk size must be a positive integer")
        self.store = store
        self.transport = transport
        self.chunk_size = chunk_size

    def publish(self, path: Path | str, instance_url: str, token: str) -> PublicationResult:
        """Stage, upload, and patch one recording under the publication lock."""
        source = Path(path)
        with self.store.publication_lock():
            self._clean_stale_staging()
            staged: _StagedMedia | None = None
            source_descriptor = -1
            try:
                staged, source_descriptor = self._stage(source)
                key = PublicationKey(instance_url, staged.digest)
                job = self.store.get(key)

                # A durable in-flight upload is never replayed after a restart.
                if job is not None and job.state is PublicationState.TRANSFERRING:
                    recovered = self.store.mark_interrupted_transfer_unknown(key)
                    return self._result(recovered)
                if job is not None and job.state is PublicationState.TRANSFER_UNKNOWN:
                    return self._result(job)
                if job is not None and job.state is PublicationState.PUBLISHED:
                    return PublicationResult(job, True)

                if job is None:
                    job = self.store.create_ready(
                        key, staged.identity, staged.file_last_modified_ms,
                    )

                if job.state is PublicationState.METADATA_PENDING:
                    return self._finish_metadata(
                        key, job, source, source_descriptor, staged, token,
                    )

                if job.state not in {PublicationState.READY, PublicationState.TRANSFER_REJECTED}:
                    return self._result(job)

                # Check the open source descriptor immediately before recording
                # the transfer intent, so a preflight mutation cannot be sent.
                self._require_unchanged(source_descriptor, staged.descriptor_info)
                self.store.begin_transfer(
                    key, staged.identity, staged.file_last_modified_ms,
                )
                try:
                    remote_id = self._upload(instance_url, token, source, staged)
                except TransferNotSent:
                    rejected = self.store.mark_transfer_rejected(key, "transfer_not_sent")
                    return self._result(rejected)
                except TransferRejected as exc:
                    rejected = self.store.mark_transfer_rejected(
                        key, "transfer_rejected", exc.status,
                    )
                    return self._result(rejected)
                except TransferOutcomeUnknown:
                    unknown = self.store.mark_transfer_unknown(key, "transfer_unknown")
                    return self._result(unknown)
                except (ValueError, TypeError):
                    # Validation failures can occur before request bytes are sent;
                    # the durable outcome remains a safe, retryable rejection.
                    rejected = self.store.mark_transfer_rejected(key, "protocol_error")
                    return self._result(rejected)
                except Exception:
                    # An injected or future transport failure may have sent bytes.
                    unknown = self.store.mark_transfer_unknown(key, "transfer_unknown")
                    return self._result(unknown)

                if type(remote_id) is not int or remote_id <= 0:
                    unknown = self.store.mark_transfer_unknown(key, "transfer_unknown")
                    return self._result(unknown)

                # Commit the remote ID before any filesystem or PATCH work.
                accepted = self.store.accept_transfer(key, remote_id)
                return self._finish_metadata(
                    key, accepted, source, source_descriptor, staged, token,
                )
            finally:
                if source_descriptor >= 0:
                    try:
                        os.close(source_descriptor)
                    except OSError:
                        pass
                if staged is not None:
                    self._close_and_remove_staging(staged)

    def _upload(self, instance_url: str, token: str, source: Path, staged: _StagedMedia) -> int:
        # The staged descriptor is the immutable upload source; the original
        # source descriptor stays open in publish() until this command ends.
        os.lseek(staged.descriptor, 0, os.SEEK_SET)
        duplicate = os.dup(staged.descriptor)
        try:
            with os.fdopen(duplicate, "rb") as media:
                duplicate = -1
                return self.transport.upload(
                    instance_url, token, media, staged.identity.size,
                    source.name, staged.file_last_modified_ms,
                )
        finally:
            if duplicate >= 0:
                os.close(duplicate)

    def _finish_metadata(
        self, key: PublicationKey, job: PublicationJob, source: Path,
        source_descriptor: int, staged: _StagedMedia, token: str,
    ) -> PublicationResult:
        try:
            discovered = self._discover_current(
                source.parent, source_descriptor, staged.descriptor_info, staged.identity,
            )
        except _MetadataProblem as exc:
            pending = self.store.mark_metadata_pending(
                key, exc.code, None, exc.path,
            )
            return self._result(pending)
        except Exception:
            pending = self.store.mark_metadata_pending(
                key, "metadata_unavailable", None, job.path_hint,
            )
            return self._result(pending)

        current_path = discovered.path
        try:
            self._require_unchanged(source_descriptor, staged.descriptor_info)
        except ValueError:
            pending = self.store.mark_metadata_pending(
                key, "metadata_changed", None, current_path,
            )
            return self._result(pending)

        try:
            metadata = map_speakr_metadata(
                current_path, discovered.info.st_mtime_ns, discovered.sidecar,
            )
        except _MetadataProblem as exc:
            pending = self.store.mark_metadata_pending(
                key, exc.code, None, exc.path,
            )
            return self._result(pending)
        except Exception:
            pending = self.store.mark_metadata_pending(
                key, "metadata_malformed", None, current_path,
            )
            return self._result(pending)

        try:
            self.transport.patch_metadata(
                key.instance_url, token, job.remote_recording_id or 0, metadata,
            )
        except MetadataRejected as exc:
            pending = self.store.mark_metadata_pending(
                key, "metadata_failed", exc.status, current_path,
            )
            return self._result(pending)
        except MetadataUnavailable:
            pending = self.store.mark_metadata_pending(
                key, "metadata_failed", None, current_path,
            )
            return self._result(pending)
        except Exception:
            pending = self.store.mark_metadata_pending(
                key, "metadata_failed", None, current_path,
            )
            return self._result(pending)

        return self.store.mark_published(key, current_path)

    def _stage(self, source: Path) -> tuple[_StagedMedia, int]:
        source_descriptor = -1
        stage_descriptor = -1
        stage_path: Path | None = None
        try:
            # The directory lock closes the TOCTOU window for the initial source
            # open, while later rename handling deliberately uses the descriptor.
            with recording_directory_lock(source.parent):
                try:
                    source_descriptor = os.open(source, _source_flags())
                    source_info = os.fstat(source_descriptor)
                except OSError:
                    raise ValueError("recording media is invalid") from None
                if not _regular_mode(source_info):
                    raise ValueError("recording media is invalid")
                stage_path, stage_descriptor = self._new_staging_file()
                digest = hashlib.sha256()
                remaining = source_info.st_size
                written = 0
                while remaining:
                    chunk = os.read(source_descriptor, min(self.chunk_size, remaining))
                    if not chunk:
                        raise ValueError("recording media changed during staging")
                    digest.update(chunk)
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
                final_source_info = os.fstat(source_descriptor)
                if _identity_tuple(final_source_info) != _identity_tuple(source_info):
                    raise ValueError("recording media changed during staging")
                if os.fstat(stage_descriptor).st_size != source_info.st_size:
                    raise ValueError("recording staging failed")
                staging_info = os.fstat(stage_descriptor)
                os.fsync(stage_descriptor)
                os.lseek(stage_descriptor, 0, os.SEEK_SET)

            identity = MediaIdentity(
                source, source_info.st_dev, source_info.st_ino,
                source_info.st_size, source_info.st_mtime_ns,
            )
            staged = _StagedMedia(
                stage_path, stage_descriptor, digest.hexdigest(), identity,
                source_info.st_mtime_ns // 1_000_000, source_info, staging_info,
            )
            stage_descriptor = -1
            return staged, source_descriptor
        except Exception:
            if stage_descriptor >= 0:
                try:
                    os.close(stage_descriptor)
                except OSError:
                    pass
            if stage_path is not None:
                self._remove_owned_staging(stage_path)
            if source_descriptor >= 0:
                try:
                    os.close(source_descriptor)
                except OSError:
                    pass
            raise

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
                os.close(descriptor)
                self._remove_owned_staging(path)
                raise
            return path, descriptor
        raise ValueError("recording staging failed")

    def _discover_current(
        self, parent: Path, source_descriptor: int, expected_info: os.stat_result,
        identity: MediaIdentity,
    ) -> _DiscoveredMedia:
        with recording_directory_lock(parent):
            try:
                source_info = os.fstat(source_descriptor)
            except OSError:
                raise _MetadataProblem("metadata_missing", identity.path) from None
            if not _regular_mode(source_info) or _identity_tuple(source_info) != _identity_tuple(expected_info):
                raise _MetadataProblem("metadata_changed", identity.path)

            candidates: list[tuple[Path, os.stat_result, MeetingSidecar | None]] = []
            strict: list[tuple[Path, os.stat_result, MeetingSidecar]] = []
            try:
                entries = list(os.scandir(parent))
            except OSError:
                raise _MetadataProblem("metadata_unavailable", identity.path) from None
            for entry in entries:
                candidate = Path(entry.path)
                try:
                    info = os.lstat(candidate)
                except FileNotFoundError:
                    raise _MetadataProblem("metadata_changed", identity.path) from None
                except OSError:
                    continue
                if not _regular_mode(info) or _identity_tuple(info)[:2] != _identity_tuple(source_info)[:2]:
                    continue
                sidecar: MeetingSidecar | None = None
                adjacent = sidecar_path(candidate)
                if os.path.lexists(adjacent):
                    try:
                        sidecar = load_sidecar(adjacent)
                    except (OSError, ValueError):
                        raise _MetadataProblem("metadata_malformed", candidate) from None
                candidates.append((candidate, info, sidecar))
                if sidecar is not None and sidecar.recording_filename == candidate.name:
                    strict.append((candidate, info, sidecar))

            if not candidates:
                raise _MetadataProblem("metadata_missing", identity.path)
            if len(candidates) > 1:
                if len(strict) != 1:
                    raise _MetadataProblem("metadata_ambiguous", identity.path)
                selected_path, selected_info, selected_sidecar = strict[0]
            else:
                selected_path, selected_info, selected_sidecar = candidates[0]

            # A sidecar only belongs to this publication when its strict
            # filename intent names the selected media entry; an unrelated
            # adjacent sidecar must not leak another meeting into PATCH.
            if (
                selected_sidecar is not None
                and selected_sidecar.recording_filename != selected_path.name
            ):
                selected_sidecar = None

            try:
                final_info = os.lstat(selected_path)
                source_info = os.fstat(source_descriptor)
            except OSError:
                raise _MetadataProblem("metadata_changed", selected_path) from None
            if (
                not _regular_mode(final_info)
                or _identity_tuple(final_info) != _identity_tuple(selected_info)
                or _identity_tuple(source_info) != _identity_tuple(expected_info)
            ):
                raise _MetadataProblem("metadata_changed", selected_path)
            return _DiscoveredMedia(selected_path, selected_sidecar, final_info)

    @staticmethod
    def _require_unchanged(descriptor: int, expected: os.stat_result) -> None:
        try:
            actual = os.fstat(descriptor)
        except OSError:
            raise ValueError("recording media is unavailable") from None
        if not _regular_mode(actual) or _identity_tuple(actual) != _identity_tuple(expected):
            raise ValueError("recording media changed")

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
                if (
                    _regular_mode(info)
                    and now - info.st_mtime > _STALE_STAGING_SECONDS
                ):
                    os.unlink(candidate)
            except OSError:
                continue

    def _close_and_remove_staging(self, staged: _StagedMedia) -> None:
        try:
            os.close(staged.descriptor)
        except OSError:
            pass
        try:
            info = os.lstat(staged.path)
            if (
                _regular_mode(info)
                and info.st_dev == staged.staging_info.st_dev
                and info.st_ino == staged.staging_info.st_ino
            ):
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
    def _result(job: PublicationJob) -> PublicationResult:
        return PublicationResult(
            job, False, error_code=job.last_error_code, http_status=job.last_http_status,
        )


__all__ = ["SpeakrPublisher"]
