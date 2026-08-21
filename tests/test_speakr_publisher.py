"""Focused tests for the shared, lease-fenced Speakr publication engine."""

from __future__ import annotations

from pathlib import Path
import hashlib
import os
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time

from meeting_recorder.speakr_domain import PublicationKey, PublicationState
from meeting_recorder.speakr_http import (
    MetadataRejected, MetadataUnavailable, ReconciliationRejected,
    ReconciliationUnavailable, TransferNotSent, TransferOutcomeUnknown,
    TransferRejected,
)
from meeting_recorder.speakr_publisher import SpeakrPublisher
from meeting_recorder.speakr_store import PublicationStore


ORIGIN = "https://example.com"
TOKEN = "bearer-secret"


class FakeTransport:
    def __init__(self) -> None:
        self.uploads: list[dict[str, object]] = []
        self.patches: list[tuple[int, object]] = []
        self.upload_error: Exception | None = None
        self.first_upload_error: Exception | None = None
        self.preflight_error: Exception | None = None
        self.patch_error: Exception | None = None
        self.reconcile_error: Exception | None = None
        self.reconcile_ids: tuple[int, ...] = ()
        self.reconcile_calls: list[str] = []
        self.upload_started = Event()
        self.release_upload = Event()
        self.block_upload = False

    def upload(
        self, instance_url, token, media, media_size, filename, file_last_modified_ms, meeting_date,
        title=None,
    ) -> int:
        # Signal the caller before optionally pausing the fake request.
        self.upload_started.set()
        assert title is not None
        if self.block_upload:
            assert self.release_upload.wait(5)

        # Raise preflight and transfer failures before recording a request body.
        if isinstance(self.upload_error, TransferNotSent):
            raise self.upload_error
        if self.preflight_error is not None:
            raise self.preflight_error

        # Record the exact request projection and apply any configured outcome.
        first_upload = not self.uploads
        body = media.read()
        self.uploads.append({
            "origin": instance_url, "token": token, "body": body, "size": media_size,
            "filename": filename, "title": title,
        })
        error = (
            self.first_upload_error
            if first_upload and self.first_upload_error is not None
            else self.upload_error
        )
        if error is not None:
            raise error
        return 7

    def patch_metadata(self, instance_url, token, remote_recording_id, metadata) -> None:
        # Record each metadata request before applying its configured failure.
        self.patches.append((remote_recording_id, metadata))
        if self.patch_error is not None:
            raise self.patch_error

    def reconcile_recordings(self, instance_url, token, marker_token) -> tuple[int, ...]:
        # Record reconciliation markers and return the configured remote matches.
        self.reconcile_calls.append(marker_token)
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return self.reconcile_ids


def _setup(data: bytes = b"recording"):
    # Build isolated durable state and deterministic clock and token sources.
    directory = TemporaryDirectory()
    root = Path(directory.name)
    media = root / "recording.mkv"
    media.write_bytes(data)
    now = [1_000]
    store = PublicationStore(root / "state" / "publications.sqlite3", clock=lambda: now[0])
    transport = FakeTransport()
    publisher = SpeakrPublisher(
        store, transport, chunk_size=3, worker_id="worker", lease_ms=10,
        clock=lambda: now[0], random_source=lambda: 0.0,
        token_factory=lambda: "marker-token",
    )
    return directory, root, media, now, store, transport, publisher


def test_enqueue_reuses_normalized_origin_and_sha_without_credentials() -> None:
    # Create one publisher and clean its temporary state after the assertions.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Equivalent origins reuse one durable job without contacting transport.
        first = publisher.enqueue(media, "HTTPS://EXAMPLE.COM:443/")
        second = publisher.enqueue(media, ORIGIN)
        assert first.job_id == second.job_id
        assert first.key == PublicationKey(ORIGIN, hashlib.sha256(b"recording").hexdigest())
        assert transport.uploads == []
        raw = store.database_path.read_bytes()
        assert TOKEN.encode() not in raw
        assert b"bearer-secret" not in raw and b"private meeting" not in raw
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()


def test_enqueue_does_not_relink_an_externally_renamed_existing_job() -> None:
    # Preserve the original path until an explicit relink is requested.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        job = publisher.enqueue(media, ORIGIN)
        renamed = root / "renamed.mkv"
        media.rename(renamed)

        # Publishing the renamed path must not silently mutate the old job.
        unchanged = publisher.publish(renamed, ORIGIN, TOKEN)

        # Confirm the old row is missing while transport remains unused.
        assert unchanged.job.state is PublicationState.MISSING
        assert unchanged.job.private_path == os.fsencode(media)
        assert transport.uploads == []

        relinked = publisher.relink(job.job_id, renamed)
        assert relinked.state is PublicationState.QUEUED
        completed = publisher.run_one(ORIGIN, TOKEN, job.job_id)
        assert completed is not None and completed.job.state is PublicationState.PUBLISHED
        assert len(transport.uploads) == 1
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()


def test_enqueue_and_relink_store_absolute_bytes_without_following_final_symlink() -> None:
    # Exercise path normalization from a temporary working directory.
    directory, root, media, now, store, transport, publisher = _setup()
    original_cwd = Path.cwd()
    try:
        # Enqueue and relink must persist absolute non-symlink paths.
        relative = root / "relative.mkv"
        relative.write_bytes(b"relative")
        os.chdir(root)
        job = publisher.enqueue(Path("relative.mkv"), ORIGIN)
        assert job.private_path is not None
        assert job.private_path == os.fsencode(relative)
        assert os.path.isabs(job.private_path)

        replacement = root / "replacement.mkv"
        replacement.write_bytes(b"relative")
        relinked = publisher.relink(job.job_id, Path("replacement.mkv"))
        assert relinked.private_path is not None
        assert relinked.private_path == os.fsencode(replacement)
        assert os.path.isabs(relinked.private_path)

        symlink = root / "final-link.mkv"
        symlink.symlink_to(replacement)
        # A final symlink is rejected before a second job can be created.
        try:
            publisher.enqueue(Path("final-link.mkv"), ORIGIN)
        except ValueError:
            pass
        else:
            raise AssertionError("final symlink was accepted")
        assert len(publisher.list()) == 1
    finally:
        # Restore process state before removing temporary files.
        os.chdir(original_cwd)
        directory.cleanup()


def test_publish_uses_staged_bytes_marker_title_and_patch() -> None:
    # Publish one recording and verify the staged request and follow-up PATCH.
    directory, root, media, now, store, transport, publisher = _setup(b"012345")
    try:
        result = publisher.publish(media, ORIGIN, TOKEN)
        assert result.job.state is PublicationState.PUBLISHED
        assert transport.uploads[0]["body"] == b"012345"
        assert transport.uploads[0]["title"] == "[mr:marker-token] recording"
        assert transport.patches[0][0] == result.job.remote_recording_id == 7
        assert list(store.state_directory.glob(".staging-*")) == []
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()


def test_transfer_not_sent_and_429_requeue_without_reconciliation() -> None:
    # Run both retryable upload outcomes in fresh isolated state.
    for failure, retry_after in ((TransferNotSent(), None), (TransferRejected(429, 5), 5)):
        directory, root, media, now, store, transport, publisher = _setup()
        try:
            transport.upload_error = failure
            result = publisher.publish(media, ORIGIN, TOKEN)

            # Both outcomes requeue POST without starting reconciliation.
            assert result.job.state is PublicationState.QUEUED
            assert result.job.resume_intent == "post"
            assert result.job.operation == "post"
            assert transport.patches == []
            if retry_after is not None:
                # A server retry hint advances only the retry schedule.
                assert result.job.next_attempt_at_ms == 6_000
        finally:
            # Remove each iteration's isolated database and recording.
            directory.cleanup()


def test_invalid_token_blocks_queued_reconcile_and_patch_phases() -> None:
    # Use one state machine to check token fencing at every network phase.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Queue a POST and verify empty credentials block it before staging.
        queued = publisher.enqueue(media, ORIGIN)
        blocked_queued = publisher.run_one(ORIGIN, "", queued.job_id)
        assert blocked_queued is not None
        assert blocked_queued.job.state is PublicationState.BLOCKED
        assert blocked_queued.job.resume_intent == "post"

        # Restore authorization before testing the active reconciliation phase.
        publisher.retry(queued.job_id)
        transport.upload_error = TransferOutcomeUnknown()
        transport.reconcile_error = MetadataUnavailable()
        active = publisher.publish(media, ORIGIN, TOKEN)
        assert active.job.state is PublicationState.UNCERTAIN
        assert active.job.reconciliation_eligible
        blocked_reconcile = publisher.run_one(ORIGIN, "", active.job.job_id)
        assert blocked_reconcile is not None
        assert blocked_reconcile.job.state is PublicationState.BLOCKED
        assert blocked_reconcile.job.resume_intent == "reconcile"

        # A known remote ID still requires a valid token for the PATCH phase.
        transport.upload_error = None
        transport.reconcile_error = None
        transport.patch_error = MetadataUnavailable()
        patch_media = root / "patch.mkv"
        patch_media.write_bytes(b"patch-recording")
        pending = publisher.publish(patch_media, ORIGIN, TOKEN)
        assert pending.job.state is PublicationState.METADATA_PENDING
        blocked_patch = publisher.run_one(ORIGIN, "", pending.job.job_id)
        assert blocked_patch is not None
        assert blocked_patch.job.state is PublicationState.BLOCKED
        assert blocked_patch.job.resume_intent == "patch"
        assert TOKEN.encode() not in store.database_path.read_bytes()
    finally:
        # Remove the isolated database and recordings.
        directory.cleanup()


def test_block_configuration_claims_future_post_reconcile_and_patch_jobs() -> None:
    # Prepare future work in each resumable phase and block it without network I/O.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Claim a delayed POST, then block the future work through the engine seam.
        queued = publisher.enqueue(media, ORIGIN)
        claimed_queued = store.claim_one("worker", queued.job_id, lease_ms=10, now_ms=1_000)
        store.schedule(
            queued.job_id, "worker", claimed_queued.lease_generation,  # type: ignore[union-attr]
            next_attempt_at_ms=50_000, now_ms=1_000,
        )
        blocked_queued = publisher.block_configuration(queued.job_id, instance_url=ORIGIN)
        assert blocked_queued is not None
        assert blocked_queued.state is PublicationState.BLOCKED
        assert blocked_queued.resume_intent == "post"

        # Create delayed reconciliation work and block its resume phase.
        active_media = root / "future-reconcile.mkv"
        active_media.write_bytes(b"future-reconcile")
        transport.upload_error = TransferOutcomeUnknown()
        transport.reconcile_error = MetadataUnavailable()
        active = publisher.publish(active_media, ORIGIN, TOKEN)
        claimed_reconcile = store.claim_one("worker", active.job.job_id, lease_ms=10, now_ms=1_000)
        store.schedule(
            active.job.job_id, "worker", claimed_reconcile.lease_generation,  # type: ignore[union-attr]
            next_attempt_at_ms=50_000, now_ms=1_000,
        )
        blocked_reconcile = publisher.block_configuration(
            active.job.job_id, instance_url=ORIGIN,
        )
        assert blocked_reconcile is not None
        assert blocked_reconcile.state is PublicationState.BLOCKED
        assert blocked_reconcile.resume_intent == "reconcile"

        # Create delayed PATCH work and block it without contacting transport.
        transport.upload_error = None
        transport.reconcile_error = None
        transport.patch_error = MetadataUnavailable()
        patch_media = root / "future-patch.mkv"
        patch_media.write_bytes(b"future-patch")
        pending = publisher.publish(patch_media, ORIGIN, TOKEN)
        claimed_patch = store.claim_one("worker", pending.job.job_id, lease_ms=10, now_ms=1_000)
        store.schedule(
            pending.job.job_id, "worker", claimed_patch.lease_generation,  # type: ignore[union-attr]
            next_attempt_at_ms=50_000, now_ms=1_000,
        )
        blocked_patch = publisher.block_configuration(pending.job.job_id, instance_url=ORIGIN)
        assert blocked_patch is not None
        assert blocked_patch.state is PublicationState.BLOCKED
        assert blocked_patch.resume_intent == "patch"
    finally:
        # Remove the isolated database and recordings.
        directory.cleanup()


def test_block_configuration_respects_live_leases_and_origin_fences() -> None:
    # Verify configuration blocking leaves live leases and other origins untouched.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # A live lease prevents configuration blocking from taking the row.
        leased = publisher.enqueue(media, ORIGIN)
        claimed = store.claim_one("worker", leased.job_id, lease_ms=10_000, now_ms=1_000)
        unchanged = publisher.block_configuration(leased.job_id, instance_url=ORIGIN)
        assert unchanged is not None and unchanged.state is PublicationState.QUEUED
        assert unchanged.lease_owner == "worker"

        # A different origin is fenced before its row can be claimed.
        other_media = root / "other-origin.mkv"
        other_media.write_bytes(b"other-origin")
        other = publisher.enqueue(other_media, "https://other.example")
        mismatch = publisher.block_configuration(other.job_id, instance_url=ORIGIN)
        assert mismatch is not None and mismatch.state is PublicationState.QUEUED
        assert mismatch.lease_owner is None
    finally:
        # Remove the isolated database and recordings.
        directory.cleanup()


def test_transport_preflight_value_error_blocks_post_before_request_bytes() -> None:
    # A transport configuration error must block before any request body is read.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Inject a local transport failure and run the normal POST path.
        transport.preflight_error = ValueError("invalid transport setup")
        result = publisher.publish(media, ORIGIN, TOKEN)
        assert result.job.state is PublicationState.BLOCKED
        assert result.job.resume_intent == "post"
        assert transport.uploads == []
        assert transport.reconcile_calls == []
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()


def test_ambiguous_upload_and_5xx_reconcile_exactly_once() -> None:
    # Exercise each ambiguous outcome with a fresh durable publication.
    for failure, matches, expected in (
        (TransferOutcomeUnknown(), (7,), PublicationState.PUBLISHED),
        (TransferOutcomeUnknown(), (), PublicationState.UNCERTAIN),
        (TransferRejected(503), (7,), PublicationState.PUBLISHED),
        (TransferOutcomeUnknown(), (7, 8), PublicationState.UNCERTAIN),
    ):
        directory, root, media, now, store, transport, publisher = _setup()
        try:
            # Reconcile the marker instead of sending a second upload.
            transport.upload_error = failure
            transport.reconcile_ids = matches
            result = publisher.publish(media, ORIGIN, TOKEN)
            assert result.job.state is expected
            assert len(transport.uploads) == 1

            # Multiple or missing matches remain terminally uncertain.
            assert result.job.operation == ("none" if len(matches) != 1 else "none")
            if len(matches) != 1:
                assert result.job.reconciliation_eligible is False
                rerun = publisher.run_one(ORIGIN, TOKEN, result.job.job_id)
                assert rerun is not None and len(transport.uploads) == 1
        finally:
            # Remove each iteration's isolated database and recording.
            directory.cleanup()


def test_transient_reconciliation_stays_active_and_later_gets_without_post() -> None:
    # Verify transient reconciliation waits, retries, and never posts again.
    failures = (
        (ReconciliationRejected(429, 5), 6_000),
        (ReconciliationRejected(503), 1_000),
        (ReconciliationUnavailable(), 1_000),
    )
    for failure, expected_due in failures:
        directory, root, media, now, store, transport, publisher = _setup()
        try:
            # First move the upload into active reconciliation with a retry delay.
            transport.upload_error = TransferOutcomeUnknown()
            transport.reconcile_error = failure
            first = publisher.publish(media, ORIGIN, TOKEN)
            assert first.job.state is PublicationState.UNCERTAIN
            assert first.job.reconciliation_eligible
            assert first.job.next_attempt_at_ms == expected_due
            assert len(transport.uploads) == 1

            # A not-yet-due reconciliation does not send another request.
            now[0] = 999
            not_due = publisher.run_one(ORIGIN, TOKEN, first.job.job_id)
            assert not_due is not None and not_due.job.state is PublicationState.UNCERTAIN
            assert len(transport.uploads) == 1

            # Once due, one matching marker completes through PATCH.
            now[0] = 100_000
            transport.reconcile_error = None
            transport.reconcile_ids = (7,)
            completed = publisher.run_one(ORIGIN, TOKEN, first.job.job_id)
            assert completed is not None and completed.job.state is PublicationState.PUBLISHED
            assert len(transport.uploads) == 1
            assert len(transport.reconcile_calls) == 2
        finally:
            # Remove each iteration's isolated database and recording.
            directory.cleanup()


def test_explicit_uncertain_retry_hashes_before_authorizing_post() -> None:
    # Require matching bytes before an uncertain job can be explicitly retried.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Produce an uncertain job, then reject a retry with changed bytes.
        transport.upload_error = TransferOutcomeUnknown()
        result = publisher.publish(media, ORIGIN, TOKEN)
        assert result.job.state is PublicationState.UNCERTAIN
        media.write_bytes(b"different")
        try:
            publisher.retry(result.job.job_id)
        except ValueError:
            pass
        else:
            raise AssertionError("mismatching retry media was accepted")

        # Restore the original bytes and allow the explicit retry to publish.
        assert store.get(result.job.job_id).state is PublicationState.UNCERTAIN  # type: ignore[union-attr]
        media.write_bytes(b"recording")
        queued = publisher.retry(result.job.job_id)
        assert queued.state is PublicationState.QUEUED
        transport.upload_error = None
        assert publisher.run_one(ORIGIN, TOKEN, queued.job_id).job.state is PublicationState.PUBLISHED  # type: ignore[union-attr]
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()


def test_known_id_is_persisted_before_patch_and_never_posts_again() -> None:
    # Verify a known remote ID resumes through PATCH without another POST.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Leave the remote ID durable while the first PATCH is unavailable.
        transport.patch_error = MetadataUnavailable()
        first = publisher.publish(media, ORIGIN, TOKEN)
        assert first.job.state is PublicationState.METADATA_PENDING
        assert first.job.remote_recording_id == 7

        # Resume the pending PATCH and confirm no second upload occurs.
        transport.patch_error = None
        second = publisher.run_one(ORIGIN, TOKEN, first.job.job_id)
        assert second is not None and second.job.state is PublicationState.PUBLISHED
        assert len(transport.uploads) == 1
        assert len(transport.patches) == 2
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()


def test_invalid_marker_factory_blocks_configuration_before_transfer_intent() -> None:
    # An invalid marker factory must block before transfer intent is persisted.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Use an invalid marker factory before the upload phase begins.
        job = publisher.enqueue(media, ORIGIN)
        invalid = SpeakrPublisher(
            store, transport, chunk_size=3, worker_id="worker", lease_ms=10,
            clock=lambda: now[0], random_source=lambda: 0.0,
            token_factory=lambda: "invalid_marker%",
        )
        result = invalid.run_one(ORIGIN, TOKEN, job.job_id)
        assert result is not None and result.job.state is PublicationState.BLOCKED
        assert result.job.last_error_code == "protocol_error"
        assert result.job.resume_intent == "post"
        assert transport.uploads == []
        assert transport.reconcile_calls == []
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()


def test_same_command_rename_is_rediscovered_for_patch_metadata() -> None:
    # Hold the upload open so a same-command rename can be observed before PATCH.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Run the upload in another thread while the source path changes.
        transport.block_upload = True
        job = publisher.enqueue(media, ORIGIN)
        results: list[object] = []
        thread = Thread(
            target=lambda: results.append(publisher.run_one(ORIGIN, TOKEN, job.job_id)),
        )
        thread.start()
        assert transport.upload_started.wait(5)
        renamed = root / "renamed.mkv"
        media.rename(renamed)
        transport.release_upload.set()
        thread.join(5)
        assert not thread.is_alive()
        assert results[0].job.state is PublicationState.PUBLISHED  # type: ignore[union-attr]
        assert transport.patches[0][1].title == "renamed"  # type: ignore[union-attr]
        assert store.get(job.job_id).private_path == os.fsencode(renamed)  # type: ignore[union-attr]
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()


def test_auth_and_permanent_errors_block_with_the_resume_phase() -> None:
    # Preserve the phase-specific resume intent for permanent failures.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # A permanent POST error records a resumable POST phase.
        transport.upload_error = TransferRejected(401)
        blocked = publisher.publish(media, ORIGIN, TOKEN)
        assert blocked.job.state is PublicationState.BLOCKED
        assert blocked.job.resume_intent == "post"
        transport.upload_error = None
        assert publisher.retry(blocked.job.job_id).state is PublicationState.QUEUED

        # A permanent PATCH error preserves the PATCH resume phase and remote ID.
        transport.patch_error = MetadataRejected(404)
        pending = publisher.publish(media, ORIGIN, TOKEN)
        assert pending.job.state is PublicationState.BLOCKED
        assert pending.job.resume_intent == "patch"
        assert len(transport.uploads) == 2
        transport.patch_error = None
        resumed = publisher.retry(pending.job.job_id)
        assert resumed.state is PublicationState.METADATA_PENDING
        result = publisher.run_one(ORIGIN, TOKEN, resumed.job_id)
        assert result is not None and result.job.state is PublicationState.PUBLISHED
        assert len(transport.uploads) == 2
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()


def test_missing_and_relink_require_an_exact_sha() -> None:
    # Require the original digest for both missing-media recovery and relink.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Mark the original media missing, then reject a digest mismatch.
        job = publisher.enqueue(media, ORIGIN)
        media.unlink()
        missing = publisher.run_one(ORIGIN, TOKEN, job.job_id)
        assert missing is not None and missing.job.state is PublicationState.MISSING
        wrong = root / "wrong.mkv"
        wrong.write_bytes(b"wrong")
        try:
            publisher.relink(job.job_id, wrong)
        except ValueError:
            pass
        else:
            raise AssertionError("mismatching relink was accepted")

        # A matching replacement is accepted and can be published.
        assert store.get(job.job_id).state is PublicationState.MISSING  # type: ignore[union-attr]
        replacement = root / "replacement.mkv"
        replacement.write_bytes(b"recording")
        relinked = publisher.relink(job.job_id, replacement)
        assert relinked.state is PublicationState.QUEUED
        assert publisher.run_one(ORIGIN, TOKEN, relinked.job_id).job.state is PublicationState.PUBLISHED  # type: ignore[union-attr]
    finally:
        # Remove the isolated database and recordings.
        directory.cleanup()


def test_backoff_and_retry_after_are_bounded_and_injected() -> None:
    # Use the injected clock and retry hint to verify bounded scheduling.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Schedule an uncertain result with the injected backoff clock.
        transport.upload_error = TransferRejected(503)
        result = publisher.publish(media, ORIGIN, TOKEN)
        assert result.job.state is PublicationState.UNCERTAIN
        assert result.job.next_attempt_at_ms == 1_000
        # A terminal reconciliation can be explicitly resumed, then 429 is
        # handled as a bounded queued retry with the server's hint.
        transport.upload_error = None
        transport.reconcile_ids = (7,)
        assert publisher.retry(result.job.job_id).state is PublicationState.QUEUED
        transport.upload_error = TransferRejected(429, 99_999)
        retried = publisher.run_one(ORIGIN, TOKEN, result.job.job_id)
        assert retried is not None and retried.job.state is PublicationState.QUEUED
        assert retried.job.next_attempt_at_ms == 21_601_000
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()


def test_origin_filter_never_sends_credentials_to_another_job() -> None:
    # Keep a different origin completely outside this publisher's claim path.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Check due and referenced execution before attempting the other origin.
        other = publisher.enqueue(media, "https://other.example")
        assert publisher.run_due(ORIGIN, TOKEN) == []
        assert publisher.run_one(ORIGIN, TOKEN) is None
        assert store.get(other.job_id).lease_owner is None  # type: ignore[union-attr]

        # A direct cross-origin reference must be rejected before transport use.
        try:
            publisher.run_one(ORIGIN, TOKEN, other.job_id)
        except ValueError:
            pass
        else:
            raise AssertionError("cross-origin token use was accepted")
        assert transport.uploads == []
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()


def test_stale_worker_cannot_commit_after_lease_recovery() -> None:
    # Force lease loss during an upload and verify the stale worker cannot commit.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Hold the first worker in transfer while its renewal begins failing.
        transport.block_upload = True
        original_renew = store.renew

        def fail_stale_renew(reference, owner, generation, **kwargs):
            # Make only the original worker's renewal fail after recovery time.
            if owner == "worker" and now[0] >= 1_011:
                raise RuntimeError("renewal intentionally unavailable")
            return original_renew(reference, owner, generation, **kwargs)

        store.renew = fail_stale_renew
        job = publisher.enqueue(media, ORIGIN)
        first_result: list[object] = []
        thread = Thread(
            target=lambda: first_result.append(publisher.run_one(ORIGIN, TOKEN, job.job_id)),
        )
        thread.start()
        assert transport.upload_started.wait(5)
        now[0] = 1_011

        # Recover the lease with another worker and finish the stale request.
        other = SpeakrPublisher(
            store, transport, chunk_size=3, worker_id="new-worker", lease_ms=100,
            clock=lambda: now[0], random_source=lambda: 0.0,
            token_factory=lambda: "new-marker",
        )
        recovered = other.run_one(ORIGIN, TOKEN, job.job_id)
        assert recovered is not None and recovered.job.state is PublicationState.UNCERTAIN
        transport.release_upload.set()
        thread.join(5)
        assert not thread.is_alive()
        assert store.get(job.job_id).state is PublicationState.UNCERTAIN  # type: ignore[union-attr]
        assert len(transport.uploads) == 1
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()


def test_heartbeat_renews_blocked_post_before_second_worker_can_reclaim() -> None:
    # Keep a blocked request leased while a peer attempts to reclaim it.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Start a blocked upload and move time far enough for heartbeat renewal.
        publisher.lease_ms = 60
        transport.block_upload = True
        job = publisher.enqueue(media, ORIGIN)
        first_result: list[object] = []
        thread = Thread(
            target=lambda: first_result.append(publisher.run_one(ORIGIN, TOKEN, job.job_id)),
        )
        thread.start()
        assert transport.upload_started.wait(5)
        # Move the injected clock before the original lease expires and wait for renewal.
        now[0] = 1_050
        deadline = time.time() + 2
        while time.time() < deadline:
            current_job = store.get(job.job_id)
            if current_job is not None and current_job.lease_expires_at_ms == 1_110:
                break
            time.sleep(0.01)
        assert store.get(job.job_id).lease_expires_at_ms == 1_110  # type: ignore[union-attr]
        now[0] = 1_100
        # The peer must observe the renewed fence instead of reclaiming the request.
        other = SpeakrPublisher(
            store, transport, chunk_size=3, worker_id="renewed-peer", lease_ms=60,
            clock=lambda: now[0], random_source=lambda: 0.0,
            token_factory=lambda: "peer-marker",
        )
        current = other.run_one(ORIGIN, TOKEN, job.job_id)
        assert current is not None and current.job.state is PublicationState.TRANSFERRING
        assert len(transport.uploads) == 0

        # Release the original request and verify it completes under its fence.
        transport.release_upload.set()
        thread.join(5)
        assert not thread.is_alive()
        assert first_result[0].job.state is PublicationState.PUBLISHED  # type: ignore[union-attr]
    finally:
        # Release the fake request before removing the temporary state.
        transport.release_upload.set()
        directory.cleanup()


def test_run_all_due_uses_one_initial_snapshot_without_zero_delay_starvation() -> None:
    # Create more jobs than a retrying first job could otherwise starve.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Build the initial due set and make only its first upload retryable.
        paths = [media]
        for index in range(1, 102):
            path = root / f"recording-{index}.mkv"
            path.write_bytes(f"recording-{index}".encode())
            paths.append(path)
        for path in paths:
            publisher.enqueue(path, ORIGIN)
        transport.first_upload_error = TransferRejected(429, 0)

        # The initial snapshot must return every job despite the zero-delay retry.
        results = publisher.run_all_due(ORIGIN, TOKEN)

        assert len(results) == 102
        assert results[0].job.state is PublicationState.QUEUED
        assert all(result.job.state is PublicationState.PUBLISHED for result in results[1:])
        assert len(transport.uploads) == 102
        assert len(transport.patches) == 101
    finally:
        # Remove the isolated database and recordings.
        directory.cleanup()


def test_get_list_forget_and_published_rerun_do_not_use_network() -> None:
    # Verify local reads, idempotent reruns, and forgetting after publication.
    directory, root, media, now, store, transport, publisher = _setup()
    try:
        # Publish once, then use local reads and a rerun without another upload.
        result = publisher.publish(media, ORIGIN, TOKEN)
        upload_count = len(transport.uploads)
        assert publisher.get(result.job.job_id) == result.job
        assert result.job in publisher.list(instance_url=ORIGIN)
        rerun = publisher.publish(media, ORIGIN, TOKEN)
        assert rerun.already_published
        assert len(transport.uploads) == upload_count
        publisher.forget(result.job.job_id)
        assert publisher.get(result.job.job_id) is None
    finally:
        # Remove the isolated database and recording.
        directory.cleanup()
