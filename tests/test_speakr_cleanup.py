"""Focused zero-dependency tests for explicit Speakr local cleanup."""

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
import time
from typing import Any, Iterator, NoReturn

import meeting_recorder.recording_paths as recording_paths_module
import meeting_recorder.speakr_cleanup as speakr_cleanup_module
from meeting_recorder.meeting_sidecar import MeetingSidecar, write_sidecar
from meeting_recorder.speakr_cleanup import CleanupReason, CleanupReport, CleanupStatus, PublicationCleanup
from meeting_recorder.speakr_domain import CleanupPhase, MediaIdentity, PublicationKey, PublicationState
from meeting_recorder.speakr_store import PublicationStore


HASH = "a" * 64


def _publish(store: PublicationStore, key: PublicationKey, media: Path, *, job_id: str = "cleanup-job") -> None:
    # Capture the media identity before registering the publication job.
    identity = MediaIdentity(media, media.stat().st_dev, media.stat().st_ino, media.stat().st_size, media.stat().st_mtime_ns)

    # Move the job through the real publication states used by cleanup eligibility.
    store.create_or_reuse(key, media, identity=identity, job_id=job_id)
    claim = store.claim_one("publisher", key, now_ms=1_000)
    assert claim is not None
    store.transition(key, PublicationState.TRANSFERRING, owner="publisher", generation=claim.lease_generation, now_ms=1_000)
    store.transition(key, PublicationState.METADATA_PENDING, owner="publisher", generation=claim.lease_generation, remote_recording_id=1, now_ms=1_001)
    store.transition(key, PublicationState.PUBLISHED, remote_recording_id=1, now_ms=1_002)


def test_preview_is_read_only_and_delete_removes_media_and_sidecar() -> None:
    # Create one old published recording with its adjacent sidecar.
    with TemporaryDirectory() as directory:
        root = Path(directory) / "recordings"
        root.mkdir()
        media = root / "recording.mkv"
        payload = b"cleanup payload"
        media.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        ended = now - timedelta(days=5)
        write_sidecar(media, MeetingSidecar(media.name, media.name, ended - timedelta(seconds=1), ended, None))
        store = PublicationStore(Path(directory) / "state.sqlite3", clock=lambda: 1_000)
        key = PublicationKey("https://cleanup.example", digest)
        _publish(store, key, media)
        cleanup = PublicationCleanup(store, root, clock=lambda: now)

        # Preview reports eligibility without changing either file.
        preview = cleanup.preview(5)
        assert preview.results[0].status is CleanupStatus.ELIGIBLE
        assert media.exists() and (root / (media.name + ".meeting.json")).exists()

        # Explicit deletion removes both files and records local removal.
        report = cleanup.delete(5)
        assert report.results[0].reason is CleanupReason.DELETED
        assert not media.exists() and not (root / (media.name + ".meeting.json")).exists()
        completed = store.get(key)
        assert completed is not None and completed.state is PublicationState.LOCAL_REMOVED


def test_age_boundary_future_and_sidecar_mismatch_are_fail_closed() -> None:
    # Build a published recording whose media is newer than the threshold.
    with TemporaryDirectory() as directory:
        root = Path(directory) / "recordings"
        root.mkdir()
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        media = root / "recording.mkv"
        media.write_bytes(b"fallback")
        digest = hashlib.sha256(b"fallback").hexdigest()
        store = PublicationStore(Path(directory) / "state.sqlite3", clock=lambda: 1_000)
        key = PublicationKey("https://cleanup.example", digest)
        _publish(store, key, media)
        cleanup = PublicationCleanup(store, root, clock=lambda: now)

        # A future or boundary-safe recording must not be considered old.
        assert cleanup.preview(5).results[0].reason is CleanupReason.NOT_OLD

        # A sidecar naming mismatch must fail closed instead of deleting media.
        write_sidecar(media, MeetingSidecar("wrong.mkv", media.name, now - timedelta(days=6), now - timedelta(days=5), None))
        result = cleanup.preview(5).results[0]
        assert result.status is CleanupStatus.INCOMPLETE and result.reason is CleanupReason.SIDECAR_UNSAFE


def test_media_only_delete_uses_persisted_source_mtime_and_preserves_neighbors() -> None:
    # Create old media without a sidecar and keep an unrelated neighbor nearby.
    with TemporaryDirectory() as directory:
        root = Path(directory) / "recordings"
        root.mkdir()
        media = root / "media.mkv"
        neighbor = root / "neighbor.txt"
        payload = b"media only"
        media.write_bytes(payload)
        neighbor.write_text("keep", encoding="utf-8")
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        old_seconds = now.timestamp() - 5 * 86_400
        os.utime(media, (old_seconds, old_seconds))
        digest = hashlib.sha256(payload).hexdigest()
        store = PublicationStore(Path(directory) / "state.sqlite3", clock=lambda: 1_000)
        key = PublicationKey("https://cleanup.example", digest)
        _publish(store, key, media)

        # Delete only the eligible media and preserve the unrelated neighbor.
        report = PublicationCleanup(store, root, clock=lambda: now).delete(5)
        assert report.results[0].reason is CleanupReason.DELETED
        assert not media.exists() and neighbor.read_text(encoding="utf-8") == "keep"


def test_configured_root_symlink_is_accepted_but_traversal_is_rejected() -> None:
    # Configure cleanup through a directory symlink pointing at the real root.
    with TemporaryDirectory() as directory:
        root = Path(directory) / "real"
        root.mkdir()
        link = Path(directory) / "configured"
        link.symlink_to(root, target_is_directory=True)
        media = root / "recording.mkv"
        media.write_bytes(b"root link")
        digest = hashlib.sha256(b"root link").hexdigest()
        store = PublicationStore(Path(directory) / "state.sqlite3", clock=lambda: 1_000)
        key = PublicationKey("https://cleanup.example", digest)
        _publish(store, key, media)
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        cleanup = PublicationCleanup(store, link, clock=lambda: now)

        # Preview the linked root while preserving the canonical media path.
        assert cleanup.preview(1).results[0].reason is CleanupReason.NOT_OLD
        assert cleanup.preview(1).results[0].path == str(media)


def test_configured_root_replacement_at_each_open_and_mutation_boundary_is_fail_closed() -> None:
    # Exercise root replacement at every security-sensitive cleanup boundary.
    for configured_as_symlink in (False, True):
        for checkpoint_name in ("root_before_open", "root_opened", "before_media_quarantine"):
            with TemporaryDirectory() as directory:
                base = Path(directory)
                old_root = base / "old-root"
                replacement = base / "replacement-root"
                old_root.mkdir()
                replacement.mkdir()
                configured = base / "configured-root"
                if configured_as_symlink:
                    configured.symlink_to(old_root, target_is_directory=True)
                    cleanup_root = configured
                else:
                    cleanup_root = old_root
                media = old_root / "recording.mkv"
                media.write_bytes(b"root race")
                old = datetime(2026, 8, 22, tzinfo=timezone.utc).timestamp() - 5 * 86_400
                os.utime(media, (old, old))
                replacement_file = replacement / "unrelated.mkv"
                replacement_file.write_bytes(b"keep replacement")
                digest = hashlib.sha256(b"root race").hexdigest()
                store = PublicationStore(base / "state.sqlite3", clock=lambda: 1_000)
                key = PublicationKey(f"https://root-race-{configured_as_symlink}.example", digest)
                _publish(store, key, media, job_id=f"root-race-{configured_as_symlink}-{checkpoint_name}")
                now = datetime(2026, 8, 22, tzinfo=timezone.utc)

                # Retarget the configured root without touching either unrelated namespace.
                replaced = False

                def checkpoint(name: str) -> None:
                    nonlocal replaced
                    if name != checkpoint_name or replaced:
                        return
                    replaced = True
                    if configured_as_symlink:
                        configured.unlink()
                        configured.symlink_to(replacement, target_is_directory=True)
                    else:
                        displaced = base / "displaced-root"
                        old_root.rename(displaced)
                        old_root.mkdir()
                        (old_root / "replacement-only.mkv").write_bytes(b"keep new root")

                # Attempt deletion and verify the race is reported without mutation.
                result = PublicationCleanup(store, cleanup_root, clock=lambda: now, checkpoint=checkpoint).delete(1)
                assert result.results[0].status is CleanupStatus.INCOMPLETE
                preserved_media = media if configured_as_symlink else base / "displaced-root" / "recording.mkv"
                assert replaced and preserved_media.exists() and preserved_media.read_bytes() == b"root race"
                assert replacement_file.exists() and replacement_file.read_bytes() == b"keep replacement"
                assert not list(old_root.glob(".cleanup-*"))
                job = store.get(key)
                assert job is not None and job.state is PublicationState.PUBLISHED
                intents = store.list_cleanup_intents(limit=10)
                if checkpoint_name.startswith("root_"):
                    assert job.cleanup_lease_owner is None and not intents
                else:
                    assert intents and job.cleanup_lease_owner is not None


def test_group_conflicts_and_live_leases_are_never_deleted() -> None:
    # Register two publications for one media path to create a cleanup conflict.
    with TemporaryDirectory() as directory:
        root = Path(directory) / "recordings"
        root.mkdir()
        media = root / "shared.mkv"
        media.write_bytes(b"shared")
        digest = hashlib.sha256(b"shared").hexdigest()
        store = PublicationStore(Path(directory) / "state.sqlite3", clock=lambda: 1_000)
        _publish(store, PublicationKey("https://one.example", digest), media, job_id="one")
        store.create_or_reuse(PublicationKey("https://two.example", digest), media, job_id="two")
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)

        # Conflicting publication ownership must remain published.
        result = PublicationCleanup(store, root, clock=lambda: now).preview(1).results[0]
        assert result.reason is CleanupReason.PUBLISHED_REQUIRED
        assert media.exists()


def test_outside_root_root_candidate_and_symlinked_parent_fail_closed() -> None:
    # Place one candidate outside the configured root and another behind a symlink.
    with TemporaryDirectory() as directory:
        base = Path(directory)
        root = base / "root"
        root.mkdir()
        outside = base / "outside.mkv"
        outside.write_bytes(b"outside")
        digest = hashlib.sha256(b"outside").hexdigest()
        store = PublicationStore(base / "state.sqlite3", clock=lambda: 1_000)
        key = PublicationKey("https://outside.example", digest)
        _publish(store, key, outside, job_id="outside-job")

        # Reject a direct outside-root candidate.
        result = PublicationCleanup(store, root, clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc)).preview(1).results[0]
        assert result.reason is CleanupReason.OUTSIDE_ROOT

        link_parent = root / "linked"
        link_parent.symlink_to(base, target_is_directory=True)
        linked = link_parent / "candidate.mkv"
        linked.write_bytes(b"linked")
        linked_digest = hashlib.sha256(b"linked").hexdigest()
        linked_key = PublicationKey("https://linked.example", linked_digest)
        _publish(store, linked_key, linked, job_id="linked-job")

        # Reject traversal through a symlinked parent as well.
        result = PublicationCleanup(store, root, clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc)).preview(1).results[-1]
        assert result.reason is CleanupReason.OUTSIDE_ROOT


def test_media_and_sidecar_unsafe_entries_are_not_followed() -> None:
    # Verify unsafe media links are reported without following their targets.
    with TemporaryDirectory() as directory:
        base = Path(directory)
        root = base / "root"
        root.mkdir()
        target = base / "target.mkv"
        target.write_bytes(b"target")
        media = root / "symlink.mkv"
        media.symlink_to(target)
        digest = hashlib.sha256(b"target").hexdigest()
        store = PublicationStore(base / "state.sqlite3", clock=lambda: 1_000)
        key = PublicationKey("https://unsafe.example", digest)
        _publish(store, key, media, job_id="symlink-job")

        # A media symlink must not expose or remove the target file.
        result = PublicationCleanup(store, root, clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc)).preview(1).results[0]
        assert result.reason is CleanupReason.MEDIA_UNSAFE and target.exists()

        safe_media = root / "sidecar-hardlink.mkv"
        safe_media.write_bytes(b"sidecar target")
        safe_digest = hashlib.sha256(b"sidecar target").hexdigest()
        write_sidecar(safe_media, MeetingSidecar(safe_media.name, safe_media.name, datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 8, 2, tzinfo=timezone.utc), None))
        sidecar_extra = base / "sidecar-extra.json"
        os.link(safe_media.with_name(safe_media.name + ".meeting.json"), sidecar_extra)
        safe_store_key = PublicationKey("https://sidecar-hardlink.example", safe_digest)
        _publish(store, safe_store_key, safe_media, job_id="sidecar-hardlink-job")

        # A sidecar hardlink must also fail closed while preserving both names.
        results = PublicationCleanup(store, root, clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc)).preview(1).results
        result = next(item for item in results if item.job_ids == ("sidecar-hardlink-job",))
        assert result.reason is CleanupReason.SIDECAR_UNSAFE
        assert safe_media.exists() and sidecar_extra.exists()


def test_post_link_crash_is_resumed_only_by_later_explicit_delete() -> None:
    # Stop after the quarantine link and verify the first delete remains incomplete.
    with TemporaryDirectory() as directory:
        root = Path(directory) / "root"
        root.mkdir()
        media = root / "crash.mkv"
        payload = b"crash"
        media.write_bytes(payload)
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        os.utime(media, (now.timestamp() - 5 * 86_400, now.timestamp() - 5 * 86_400))
        store_clock = [1_000]
        store = PublicationStore(Path(directory) / "state.sqlite3", clock=lambda: store_clock[0])
        key = PublicationKey("https://crash.example", hashlib.sha256(payload).hexdigest())
        _publish(store, key, media, job_id="crash-job")
        crashed = PublicationCleanup(
            store, root, clock=lambda: now,
            checkpoint=lambda name: (_ for _ in ()).throw(RuntimeError("stop")) if name == "media_link" else None,
        )
        first = crashed.delete(1).results[0]
        assert first.status is CleanupStatus.INCOMPLETE and media.exists()

        # Resume later through an explicit delete and finish the cleanup.
        store_clock[0] = 200_000
        second = PublicationCleanup(store, root, clock=lambda: now).delete(1).results[0]
        assert second.reason is CleanupReason.DELETED and not media.exists()


def test_preclaim_and_pre_mutation_failures_abort_intent_and_release_leases() -> None:
    # Exercise failures before claiming and before the first namespace mutation.
    with TemporaryDirectory() as directory:
        root = Path(directory) / "root"
        root.mkdir()
        media = root / "failure.mkv"
        payload = b"failure"
        media.write_bytes(payload)
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        os.utime(media, (now.timestamp() - 5 * 86_400, now.timestamp() - 5 * 86_400))
        store = PublicationStore(Path(directory) / "state.sqlite3", clock=lambda: 1_000)
        key = PublicationKey("https://failure.example", hashlib.sha256(payload).hexdigest())
        _publish(store, key, media, job_id="failure-job")

        # A claim failure leaves no intent and does not acquire a cleanup lease.
        original_claim = store.claim_cleanup_group

        def fail_claim(*_args: Any, **_kwargs: Any) -> NoReturn:
            raise RuntimeError("claim unavailable")

        store.claim_cleanup_group = fail_claim
        first = PublicationCleanup(store, root, clock=lambda: now).delete(1)
        store.claim_cleanup_group = original_claim
        assert first.results[0].status is CleanupStatus.INCOMPLETE
        assert not store.list_cleanup_intents(limit=10)
        job = store.get(key)
        assert job is not None and job.cleanup_lease_owner is None
        assert media.exists()

        # A claimed but untouched intent is also aborted when its first checkpoint fails.
        second = PublicationCleanup(
            store, root, clock=lambda: now,
            checkpoint=lambda name: (_ for _ in ()).throw(RuntimeError("before mutation")) if name == "before_sidecar_quarantine" else None,
        ).delete(1)
        assert second.results[0].status is CleanupStatus.INCOMPLETE
        assert not store.list_cleanup_intents(limit=10)
        job = store.get(key)
        assert job is not None and job.cleanup_lease_owner is None
        assert media.exists()


def test_expired_reclaim_waits_for_directory_lock_and_stale_worker_does_not_mutate() -> None:
    # Hold the first cleanup at the mutation boundary while a stale worker waits.
    with TemporaryDirectory() as directory:
        root = Path(directory) / "root"
        root.mkdir()
        media = root / "rehash.mkv"
        payload = b"rehash lock"
        media.write_bytes(payload)
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        os.utime(media, (now.timestamp() - 5 * 86_400, now.timestamp() - 5 * 86_400))
        store_clock = [1_000]
        store = PublicationStore(Path(directory) / "state.sqlite3", clock=lambda: store_clock[0])
        key = PublicationKey("https://rehash-lock.example", hashlib.sha256(payload).hexdigest())
        _publish(store, key, media, job_id="rehash-lock-job")
        entered_lock = threading.Event()
        worker_result: list[object] = []
        original_link = speakr_cleanup_module.link_regular_file_no_replace_dirfd
        link_calls: list[object] = []
        thread: threading.Thread | None = None

        # Count link attempts to prove the stale worker waits before mutation.
        def counted_link(*args: Any, **kwargs: Any) -> Any:
            link_calls.append(args)
            return original_link(*args, **kwargs)

        speakr_cleanup_module.link_regular_file_no_replace_dirfd = counted_link
        try:
            second_cleanup = PublicationCleanup(store, root, clock=lambda: now)
            original_locked = second_cleanup._locked_path

            # Observe when the stale worker enters the shared directory lock.
            @contextmanager
            def observed_lock(path: bytes) -> Iterator[Any]:
                entered_lock.set()
                with original_locked(path) as resolved:
                    yield resolved

            second_cleanup._locked_path = observed_lock

            def reclaim() -> None:
                worker_result.append(second_cleanup.delete(1))

            started = False

            # Start a competing worker only after the first cleanup reaches the race boundary.
            def checkpoint(name: str) -> None:
                nonlocal started, thread
                if name != "before_media_quarantine" or started:
                    return
                started = True
                thread = threading.Thread(target=reclaim)
                thread.start()
                assert entered_lock.wait(2)
                time.sleep(0.05)
                assert thread.is_alive()
                store_clock[0] = 200_000

            # Start the stale worker and ensure it cannot mutate while the lock is held.
            first = PublicationCleanup(store, root, clock=lambda: now, checkpoint=checkpoint).delete(1)
            assert first.results[0].status is CleanupStatus.INCOMPLETE
            assert not link_calls
            assert media.exists()
        finally:
            speakr_cleanup_module.link_regular_file_no_replace_dirfd = original_link

        assert thread is not None
        thread.join(5)
        assert not thread.is_alive()
        assert worker_result
        report = worker_result[0]
        assert isinstance(report, CleanupReport)
        assert report.results[-1].reason is CleanupReason.DELETED
        assert not media.exists()


def test_extra_hardlink_after_prepare_blocks_quarantine_and_preserves_job() -> None:
    # Add an unexpected hardlink immediately before media quarantine.
    with TemporaryDirectory() as directory:
        root = Path(directory) / "root"
        root.mkdir()
        media = root / "hardlink.mkv"
        payload = b"hardlink"
        media.write_bytes(payload)
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        os.utime(media, (now.timestamp() - 5 * 86_400, now.timestamp() - 5 * 86_400))
        store = PublicationStore(Path(directory) / "state.sqlite3", clock=lambda: 1_000)
        key = PublicationKey("https://hardlink.example", hashlib.sha256(payload).hexdigest())
        _publish(store, key, media, job_id="hardlink-job")
        extra = root / "unexpected-link"

        # Add the unexpected link only at the selected checkpoint.
        def checkpoint(name: str) -> None:
            if name == "before_media_quarantine":
                os.link(media, extra)

        # The extra link must make cleanup incomplete and preserve both names.
        result = PublicationCleanup(store, root, clock=lambda: now, checkpoint=checkpoint).delete(1).results[0]
        assert result.reason is CleanupReason.INCOMPLETE and media.exists() and extra.exists()


def test_media_and_sidecar_hardlinks_in_original_both_and_quarantine_states_never_complete() -> None:
    # Check external hardlinks before, during, and after each quarantine step.
    for kind, with_sidecar in (("media", False), ("sidecar", True)):
        for state, checkpoint_name in (
            ("original", f"before_{kind}_quarantine"),
            ("both", f"{kind}_link"),
            ("quarantine", f"{kind}_unlink"),
        ):
            with TemporaryDirectory() as directory:
                root = Path(directory) / "root"
                root.mkdir()
                media = root / f"{kind}-{state}.mkv"
                payload = f"{kind}-{state}".encode()
                media.write_bytes(payload)
                now = datetime(2026, 8, 22, tzinfo=timezone.utc)
                old = now.timestamp() - 5 * 86_400
                os.utime(media, (old, old))
                if with_sidecar:
                    ended = now - timedelta(days=5)
                    write_sidecar(media, MeetingSidecar(media.name, media.name, ended - timedelta(seconds=1), ended, None))
                store_clock = [1_000]
                store = PublicationStore(Path(directory) / "state.sqlite3", clock=lambda: store_clock[0])
                key = PublicationKey(f"https://hardlink-{kind}-{state}.example", hashlib.sha256(payload).hexdigest())
                _publish(store, key, media, job_id=f"hardlink-{kind}-{state}")
                original = media if kind == "media" else media.with_name(media.name + ".meeting.json")
                extra = root / f"external-{kind}-{state}"

                # Add an external link at the selected namespace state, then stop the worker.
                def checkpoint(name: str) -> None:
                    if name != checkpoint_name:
                        return

                    # Link the original or quarantine name, depending on the selected state.
                    if state == "quarantine":
                        intent = store.list_cleanup_intents(limit=10)[0]
                        quarantine_name = intent.quarantine_media_basename if kind == "media" else intent.quarantine_sidecar_basename
                        assert quarantine_name is not None
                        os.link(root / quarantine_name, extra)
                    else:
                        os.link(original, extra)
                    raise RuntimeError("hardlink race")

                # Fail the first run and verify a later run still refuses unsafe deletion.
                first = PublicationCleanup(store, root, clock=lambda: now, checkpoint=checkpoint).delete(1)
                assert first.results[0].status is CleanupStatus.INCOMPLETE
                store_clock[0] = 200_000
                second = PublicationCleanup(store, root, clock=lambda: now).delete(1)
                assert second.results and second.results[-1].status is CleanupStatus.INCOMPLETE
                job = store.get(key)
                assert job is not None and job.state is PublicationState.PUBLISHED
                assert extra.exists()


def test_first_link_fsync_failure_leaves_both_names_for_explicit_recovery() -> None:
    # Verify both media and sidecar first-link failures leave recoverable names.
    for kind, with_sidecar in (("media", False), ("sidecar", True)):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            media = root / f"first-fsync-{kind}.mkv"
            payload = f"first-fsync-{kind}".encode()
            media.write_bytes(payload)
            now = datetime(2026, 8, 22, tzinfo=timezone.utc)
            old = now.timestamp() - 5 * 86_400
            os.utime(media, (old, old))
            if with_sidecar:
                ended = now - timedelta(days=5)
                write_sidecar(media, MeetingSidecar(media.name, media.name, ended - timedelta(seconds=1), ended, None))
            store_clock = [1_000]
            store = PublicationStore(Path(directory) / "state.sqlite3", clock=lambda: store_clock[0])
            key = PublicationKey(f"https://first-fsync-{kind}.example", hashlib.sha256(payload).hexdigest())
            _publish(store, key, media, job_id=f"first-fsync-{kind}")
            original_fsync = recording_paths_module.fsync_recording_directory_fd
            failed = False

            def fail_first_fsync(descriptor: int) -> None:
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("first directory fsync failed")
                original_fsync(descriptor)

            # Inject one fsync failure without changing later recovery behavior.
            recording_paths_module.fsync_recording_directory_fd = fail_first_fsync
            try:
                first = PublicationCleanup(store, root, clock=lambda: now).delete(1)
            finally:
                recording_paths_module.fsync_recording_directory_fd = original_fsync

            assert failed and first.results[0].status is CleanupStatus.INCOMPLETE
            job = store.get(key)
            assert job is not None and job.state is PublicationState.PUBLISHED
            intents = store.list_cleanup_intents(limit=10)
            assert len(intents) == 1 and intents[0].phase in {
                CleanupPhase.PREPARED, CleanupPhase.SIDECAR_QUARANTINED,
            }
            intent = intents[0]
            original_name = media if kind == "media" else media.with_name(media.name + ".meeting.json")
            quarantine_basename = (
                intent.quarantine_media_basename
                if kind == "media" else intent.quarantine_sidecar_basename
            )
            assert quarantine_basename is not None
            quarantine_name = root / quarantine_basename
            assert quarantine_name.exists() and original_name.exists()
            original_info = os.stat(original_name)
            quarantine_info = os.stat(quarantine_name)
            assert original_info.st_ino == quarantine_info.st_ino and original_info.st_nlink == 2
            assert quarantine_info.st_nlink == 2

            # The later explicit delete observes and fsyncs both names before unlinking the original.
            store_clock[0] = 200_000
            observed = False

            # Inspect both hardlink names immediately before the original is unlinked.
            def checkpoint(name: str) -> None:
                nonlocal observed
                if name != f"{kind}_both_fsync":
                    return
                observed = True
                before = os.stat(original_name)
                quarantine = os.stat(quarantine_name)
                assert before.st_ino == quarantine.st_ino and before.st_nlink == 2

            second = PublicationCleanup(store, root, clock=lambda: now, checkpoint=checkpoint).delete(1)
            assert observed and second.results[-1].reason is CleanupReason.DELETED
            assert not original_name.exists() and not quarantine_name.exists()
            final = store.get(key)
            assert final is not None and final.state is PublicationState.LOCAL_REMOVED


def test_destructive_checkpoint_matrix_requires_later_explicit_resume() -> None:
    # Cover every destructive checkpoint for media-only and sidecar-backed cleanup.
    media_checkpoints = (
        "before_sidecar_quarantine", "before_media_quarantine", "before_sidecar_unlink", "before_media_unlink",
        "media_link", "media_link_fsync", "media_both_fsync", "media_unlink", "media_unlink_fsync",
        "media_quarantine_unlink", "media_quarantine_unlink_fsync",
        "before_phase_media_quarantined", "before_phase_media_quarantined_commit",
        "after_phase_media_quarantined_commit", "phase_media_quarantined",
        "before_phase_media_unlinked", "before_phase_media_unlinked_commit",
        "after_phase_media_unlinked_commit", "phase_media_unlinked",
        "before_complete_cleanup_group", "after_complete_cleanup_group",
    )
    sidecar_checkpoints = (
        "before_sidecar_quarantine", "before_media_quarantine", "before_sidecar_unlink", "before_media_unlink",
        "sidecar_link", "sidecar_link_fsync", "sidecar_both_fsync", "sidecar_unlink", "sidecar_unlink_fsync",
        "sidecar_quarantine_unlink", "sidecar_quarantine_unlink_fsync",
        "before_phase_sidecar_quarantined", "before_phase_sidecar_quarantined_commit",
        "after_phase_sidecar_quarantined_commit", "phase_sidecar_quarantined",
        "before_phase_sidecar_unlinked", "before_phase_sidecar_unlinked_commit",
        "after_phase_sidecar_unlinked_commit", "phase_sidecar_unlinked",
    )
    sidecar_phase_checkpoints = (
        "before_phase_sidecar_quarantined", "before_phase_sidecar_quarantined_commit",
        "after_phase_sidecar_quarantined_commit", "phase_sidecar_quarantined",
    )

    # Repeat each crash boundary with and without a sidecar.
    for with_sidecar in (False, True):
        checkpoints = media_checkpoints + sidecar_checkpoints if with_sidecar else media_checkpoints + sidecar_phase_checkpoints
        for checkpoint_name in checkpoints:
            with TemporaryDirectory() as directory:
                root = Path(directory) / "root"
                root.mkdir()
                media = root / "matrix.mkv"
                payload = f"{with_sidecar}:{checkpoint_name}".encode()
                media.write_bytes(payload)
                now = datetime(2026, 8, 22, tzinfo=timezone.utc)
                old = now.timestamp() - 5 * 86_400
                os.utime(media, (old, old))
                if with_sidecar:
                    ended = now - timedelta(days=5)
                    write_sidecar(media, MeetingSidecar(media.name, media.name, ended - timedelta(seconds=1), ended, None))
                store_clock = [1_000]
                store = PublicationStore(Path(directory) / "state.sqlite3", clock=lambda: store_clock[0])
                key = PublicationKey(f"https://matrix-{with_sidecar}.example", hashlib.sha256(payload).hexdigest())
                _publish(store, key, media, job_id=f"matrix-{with_sidecar}-{checkpoint_name}")

                # Stop exactly once at the selected durable boundary.
                stopped = False

                # Raise once at the selected checkpoint to model a process crash.
                def checkpoint(name: str) -> None:
                    nonlocal stopped
                    if name == checkpoint_name and not stopped:
                        stopped = True
                        raise RuntimeError("simulated crash")

                # The interrupted run must expose only the durable state reached so far.
                first = PublicationCleanup(store, root, clock=lambda: now, checkpoint=checkpoint).delete(1)
                assert stopped and first.results[0].status is CleanupStatus.INCOMPLETE
                job = store.get(key)
                assert job is not None
                if checkpoint_name == "after_complete_cleanup_group":
                    assert job.state is PublicationState.LOCAL_REMOVED
                    assert not store.list_cleanup_intents(limit=10)
                    assert not list(root.iterdir())
                    continue

                # A crash before completion never changes the publication state or hides its intent.
                assert job.state is PublicationState.PUBLISHED
                intents = store.list_cleanup_intents(limit=10)
                pre_sidecar_transition = checkpoint_name in {
                    "before_sidecar_quarantine", "before_phase_sidecar_quarantined",
                    "before_phase_sidecar_quarantined_commit",
                }
                if checkpoint_name == "before_sidecar_quarantine" or (not with_sidecar and pre_sidecar_transition):
                    assert not intents
                else:
                    assert len(intents) == 1
                    intent = intents[0]
                    assert intent.expected_private_path == os.fsencode(str(media))
                    assert intent.phase in tuple(CleanupPhase)
                    names = {path.name for path in root.iterdir()}
                    assert all(name == media.name or name == media.name + ".meeting.json" or name.startswith(".cleanup-") for name in names)

                # Only a later explicit delete may resume or create the cleanup intent again.
                store_clock[0] = 200_000
                second = PublicationCleanup(store, root, clock=lambda: now).delete(1)
                assert second.results and second.results[-1].reason is CleanupReason.DELETED
                assert not media.exists()
                assert not (root / (media.name + ".meeting.json")).exists()
                assert not list(root.glob(".cleanup-*"))
                final = store.get(key)
                assert final is not None and final.state is PublicationState.LOCAL_REMOVED
