"""Deterministic fake-transport tests for durable Speakr publication."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import hashlib
import os
from tempfile import TemporaryDirectory
from threading import Event, Thread

from meeting_recorder.meeting_sidecar import MeetingSidecar, sidecar_path, write_sidecar
from meeting_recorder.speakr_domain import PublicationKey, PublicationState
from meeting_recorder.speakr_http import MetadataRejected, TransferNotSent, TransferOutcomeUnknown, TransferRejected
from meeting_recorder.speakr_publisher import SpeakrPublisher
from meeting_recorder.speakr_store import PublicationStore


TOKEN = "token-not-durable"


class FakeTransport:
    def __init__(self, *, upload_result=7) -> None:
        self.upload_result = upload_result
        self.uploads: list[bytes] = []
        self.upload_args: list[tuple[str, str, int, str, int]] = []
        self.patches: list[tuple[int, object]] = []
        self.upload_started = Event()
        self.release_upload = Event()
        self.block_upload = False
        self.upload_failure: Exception | None = None
        self.patch_failure: Exception | None = None

    def upload(self, instance_url, token, media, media_size, filename, file_last_modified_ms):
        self.upload_started.set()
        if self.block_upload:
            assert self.release_upload.wait(5)
        if self.upload_failure is not None:
            raise self.upload_failure
        chunks = []
        while True:
            chunk = media.read(97)
            if not chunk:
                break
            chunks.append(chunk)
        body = b"".join(chunks)
        self.uploads.append(body)
        self.upload_args.append((instance_url, token, media_size, filename, file_last_modified_ms))
        return self.upload_result

    def patch_metadata(self, instance_url, token, remote_recording_id, metadata):
        self.patches.append((remote_recording_id, metadata))
        if self.patch_failure is not None:
            raise self.patch_failure


def _setup(data=b"recording bytes"):
    directory = TemporaryDirectory()
    root = Path(directory.name)
    media = root / "recording.mkv"
    media.write_bytes(data)
    store = PublicationStore(root / "state" / "publications.sqlite3", clock=lambda: 1_000)
    transport = FakeTransport()
    return directory, root, media, store, transport


def _publish(media, store, transport):
    return SpeakrPublisher(store, transport, chunk_size=3).publish(media, "https://example.com", TOKEN)


def test_publish_stages_exact_bytes_and_hashes_the_staged_descriptor() -> None:
    directory, root, media, store, transport = _setup(b"0123456789abcdef")
    try:
        result = _publish(media, store, transport)
        digest = hashlib.sha256(media.read_bytes()).hexdigest()
        job = store.get(PublicationKey("https://example.com", digest))
        assert result.job.state is PublicationState.PUBLISHED
        assert job is not None and job.state is PublicationState.PUBLISHED
        assert transport.uploads == [b"0123456789abcdef"]
        assert transport.upload_args[0][2:] == (16, "recording.mkv", media.stat().st_mtime_ns // 1_000_000)
        assert list(store.state_directory.glob(".staging-*")) == []
    finally:
        directory.cleanup()


def test_hash_uses_same_length_staged_bytes_that_are_uploaded() -> None:
    directory, root, media, store, transport = _setup(b"0123456789abcdef")
    replacement = b"fedcba9876543210"
    try:
        publisher = SpeakrPublisher(store, transport, chunk_size=3)
        original_hash = publisher._hash_staged

        def replace_before_hash(descriptor, expected):
            os.lseek(descriptor, 0, os.SEEK_SET)
            assert os.write(descriptor, replacement) == len(replacement)
            os.fsync(descriptor)
            os.utime(descriptor, ns=(expected.st_atime_ns, expected.st_mtime_ns))
            return original_hash(descriptor, expected)

        publisher._hash_staged = replace_before_hash
        result = publisher.publish(media, "https://example.com", TOKEN)
        digest = hashlib.sha256(replacement).hexdigest()
        assert result.job.state is PublicationState.PUBLISHED
        assert result.job.key == PublicationKey("https://example.com", digest)
        assert transport.uploads == [replacement]
    finally:
        directory.cleanup()


def test_staged_modification_after_transfer_intent_is_unknown_without_remote_id() -> None:
    directory, root, media, store, transport = _setup(b"0123456789abcdef")
    replacement = b"fedcba9876543210"
    try:
        publisher = SpeakrPublisher(store, transport, chunk_size=3)
        original_upload = publisher._upload

        def replace_before_upload(instance_url, token, source, staged):
            os.lseek(staged.descriptor, 0, os.SEEK_SET)
            assert os.write(staged.descriptor, replacement) == len(replacement)
            os.fsync(staged.descriptor)
            return original_upload(instance_url, token, source, staged)

        publisher._upload = replace_before_upload
        result = publisher.publish(media, "https://example.com", TOKEN)
        digest = hashlib.sha256(media.read_bytes()).hexdigest()
        assert result.job.state is PublicationState.TRANSFER_UNKNOWN
        assert result.job.remote_recording_id is None
        assert result.error_code == "transfer_unknown"
        assert transport.uploads == [replacement]
        assert store.get(PublicationKey("https://example.com", digest)).state is PublicationState.TRANSFER_UNKNOWN
    finally:
        directory.cleanup()


def test_equivalent_ipv6_and_unicode_origins_share_one_post_each() -> None:
    directory, root, media, store, transport = _setup()
    try:
        publisher = SpeakrPublisher(store, transport, chunk_size=3)
        first_ipv6 = publisher.publish(
            media, "https://[2001:0db8:0000:0000:0000:0000:0000:0001]", TOKEN,
        )
        second_ipv6 = publisher.publish(media, "https://[2001:db8::1]/", TOKEN)
        first_unicode = publisher.publish(media, "https://BÜCHER.example/", TOKEN)
        second_unicode = publisher.publish(media, "https://xn--bcher-kva.example", TOKEN)
        assert first_ipv6.job.state is PublicationState.PUBLISHED
        assert second_ipv6.already_published
        assert first_unicode.job.state is PublicationState.PUBLISHED
        assert second_unicode.already_published
        assert len(transport.uploads) == 2
    finally:
        directory.cleanup()


def test_symlink_fifo_and_directory_are_rejected_before_upload() -> None:
    directory, root, media, store, transport = _setup()
    try:
        symlink = root / "link.mkv"
        symlink.symlink_to(media)
        for invalid in (symlink, root, root / "missing.mkv"):
            try:
                _publish(invalid, store, transport)
                assert False, "invalid media was accepted"
            except ValueError:
                pass
        fifo = root / "pipe.mkv"
        os.mkfifo(fifo)
        try:
            _publish(fifo, store, transport)
            assert False, "FIFO was accepted"
        except ValueError:
            pass
        assert transport.uploads == []
        assert list(store.state_directory.glob(".staging-*")) == []
    finally:
        directory.cleanup()


def test_stale_cleanup_removes_only_old_regular_staging_files() -> None:
    directory, root, media, store, transport = _setup()
    try:
        stale = store.state_directory / ".staging-old"
        stale.write_bytes(b"old")
        os.utime(stale, (1, 1))
        symlink = store.state_directory / ".staging-link"
        symlink.symlink_to(media)
        _publish(media, store, transport)
        assert not stale.exists()
        assert symlink.is_symlink()
    finally:
        directory.cleanup()


def test_persisted_transferring_and_unknown_never_post_again() -> None:
    directory, root, media, store, transport = _setup()
    try:
        digest = hashlib.sha256(media.read_bytes()).hexdigest()
        key = PublicationKey("https://example.com", digest)
        info = media.stat()
        from meeting_recorder.speakr_domain import MediaIdentity
        identity = MediaIdentity(media, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        store.create_ready(key, identity, info.st_mtime_ns // 1_000_000)
        store.begin_transfer(key, identity, info.st_mtime_ns // 1_000_000)
        first = _publish(media, store, transport)
        assert first.job.state is PublicationState.TRANSFER_UNKNOWN
        assert transport.uploads == []
        second = _publish(media, store, transport)
        assert second.job.state is PublicationState.TRANSFER_UNKNOWN
        assert transport.uploads == []
    finally:
        directory.cleanup()


def test_complete_rejection_can_be_explicitly_retried() -> None:
    directory, root, media, store, transport = _setup()
    try:
        transport.upload_failure = TransferRejected(503)
        rejected = _publish(media, store, transport)
        assert rejected.job.state is PublicationState.TRANSFER_REJECTED
        transport.upload_failure = None
        published = _publish(media, store, transport)
        assert published.job.state is PublicationState.PUBLISHED
        assert len(transport.uploads) == 1
    finally:
        directory.cleanup()


def test_ambiguous_upload_outcome_is_terminal_and_private_values_are_absent() -> None:
    directory, root, media, store, transport = _setup()
    try:
        transport.upload_failure = TransferOutcomeUnknown()
        result = _publish(media, store, transport)
        assert result.job.state is PublicationState.TRANSFER_UNKNOWN
        raw = store.database_path.read_bytes()
        assert TOKEN.encode() not in raw
        assert b"private" not in raw
        assert "private" not in repr(result).casefold()
        assert "token-not-durable" not in repr(result)
    finally:
        directory.cleanup()


def test_patch_failure_persists_remote_id_and_rerun_does_not_upload() -> None:
    directory, root, media, store, transport = _setup()
    try:
        transport.patch_failure = MetadataRejected(422)
        failed = _publish(media, store, transport)
        assert failed.job.state is PublicationState.METADATA_PENDING
        assert failed.job.remote_recording_id == 7
        assert failed.job.last_error_code == "metadata_failed"
        uploads = len(transport.uploads)
        transport.patch_failure = None
        retried = _publish(media, store, transport)
        assert retried.job.state is PublicationState.PUBLISHED
        assert len(transport.uploads) == uploads
        assert len(transport.patches) == 2
    finally:
        directory.cleanup()


def test_rename_during_blocked_upload_uses_current_sidecar_path() -> None:
    directory, root, media, store, transport = _setup()
    try:
        old_sidecar = MeetingSidecar(media.name, "fallback.mkv", __import__("datetime").datetime.now(__import__("datetime").timezone.utc), __import__("datetime").datetime.now(__import__("datetime").timezone.utc), None)
        write_sidecar(sidecar_path(media), old_sidecar)
        transport.block_upload = True
        result_holder = []

        def run() -> None:
            result_holder.append(_publish(media, store, transport))

        thread = Thread(target=run)
        thread.start()
        assert transport.upload_started.wait(5)
        renamed = root / "renamed.mkv"
        os.rename(media, renamed)
        os.rename(sidecar_path(media), sidecar_path(renamed))
        write_sidecar(sidecar_path(renamed), replace(old_sidecar, recording_filename=renamed.name))
        transport.release_upload.set()
        thread.join(5)
        assert not thread.is_alive()
        assert result_holder[0].job.state is PublicationState.PUBLISHED
        assert transport.patches[0][1].title == "renamed"
    finally:
        directory.cleanup()


def test_publication_lock_allows_only_one_post() -> None:
    directory, root, media, store, transport = _setup()
    try:
        transport.block_upload = True
        results = []
        first = Thread(target=lambda: results.append(_publish(media, store, transport)))
        second = Thread(target=lambda: results.append(_publish(media, store, transport)))
        first.start()
        assert transport.upload_started.wait(5)
        second.start()
        transport.release_upload.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert len(transport.uploads) == 1
        assert len(results) == 2
    finally:
        directory.cleanup()


def test_cleanup_happens_when_upload_is_not_sent() -> None:
    directory, root, media, store, transport = _setup()
    try:
        transport.upload_failure = TransferNotSent()
        result = _publish(media, store, transport)
        assert result.job.state is PublicationState.TRANSFER_REJECTED
        assert list(store.state_directory.glob(".staging-*")) == []
    finally:
        directory.cleanup()
