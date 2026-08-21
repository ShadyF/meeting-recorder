"""Focused persistence, lease, recovery, and fencing tests for Speakr jobs."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from meeting_recorder.speakr_domain import MediaIdentity, PublicationKey, PublicationState
from meeting_recorder.speakr_store import (
    PublicationMigrationError, PublicationStore, PublicationStoreSecurityError,
    PublicationTransitionError,
)


HASH = "a" * 64


def _key(url="HTTPS://EXAMPLE.COM:443/", digest=HASH) -> PublicationKey:
    return PublicationKey(url, digest)


def _store(directory: str, *, clock=None) -> PublicationStore:
    return PublicationStore(
        Path(directory) / "state" / "publications.sqlite3", clock=clock or (lambda: 1_000),
    )


def _raises(callable_object, *args, **kwargs) -> None:
    try:
        callable_object(*args, **kwargs)
    except (TypeError, ValueError, PublicationTransitionError, PublicationMigrationError, PublicationStoreSecurityError):
        return
    raise AssertionError("expected validation failure")


def test_fresh_database_is_private_v2_without_command_lock_or_metadata() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        assert store.state_directory.stat().st_mode & 0o777 == 0o700
        assert store.database_path.stat().st_mode & 0o777 == 0o600
        assert not hasattr(store, "publication_lock")
        assert not hasattr(store, "get_by_id")
        assert not hasattr(store, "create_ready")
        assert not hasattr(store, "relink_path")
        assert not hasattr(store, "begin_transfer")
        assert not (store.state_directory / "publications.lock").exists()
        with sqlite3.connect(store.database_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (2,)
            columns = [row[1] for row in connection.execute("PRAGMA table_info(publications)")]
            assert "job_id" in columns and "private_path" in columns and "reconciliation_token" in columns
            assert "max_attempts" not in columns
            assert not set(columns).intersection({"title", "meeting_date", "notes", "participants", "token", "headers"})
            assert connection.execute("SELECT wr, strict FROM pragma_table_list WHERE name = 'publications'").fetchone() == (1, 1)


def test_indexless_v2_open_adds_exact_indexes_without_changing_rows() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "publications.sqlite3"
        store = PublicationStore(path, clock=lambda: 1_000)
        first = store.create_or_reuse(_key(), b"/private/one")
        second = store.create_or_reuse(_key("https://other.example"), b"/private/two")
        before = store.list()
        index_names = (
            "idx_publications_origin_due",
            "idx_publications_lease_expiry",
            "idx_publications_private_media_identity",
        )
        with sqlite3.connect(path) as connection:
            # Simulate the indexless v2 database produced by issue #23.
            for name in index_names:
                connection.execute(f"DROP INDEX {name}")
            assert connection.execute("PRAGMA user_version").fetchone() == (2,)

        reopened = PublicationStore(path, clock=lambda: 1_000)
        assert reopened.list() == before
        with sqlite3.connect(path) as connection:
            actual = {
                row[1] for row in connection.execute("PRAGMA index_list(publications)")
                if not row[1].startswith("sqlite_autoindex_")
            }
            assert actual == set(index_names)
            assert reopened.get(first.job_id) == first
            assert reopened.get(second.job_id) == second


def test_v1_and_incompatible_existing_databases_are_rejected_without_deletion() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "publications.sqlite3"
        path.touch(mode=0o600)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE old(value TEXT)")
            connection.execute("PRAGMA user_version = 1")
        before = path.read_bytes()
        _raises(PublicationStore, path)
        assert path.exists() and path.read_bytes() == before

        path.unlink()
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version = 99")
        os.chmod(path, 0o600)
        _raises(PublicationStore, path)
        assert path.exists()

        path.unlink()
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE publications (legacy TEXT)")
            connection.execute("PRAGMA user_version = 2")
        os.chmod(path, 0o600)
        before = path.read_bytes()
        _raises(PublicationStore, path)
        assert path.exists() and path.read_bytes() == before


def test_create_reuse_keeps_job_id_and_normalized_url_sha_identity() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        first = store.create_or_reuse(_key(), b"/private/\xff-one", 4_000)
        second = store.create_or_reuse(_key("https://example.com"), b"/private/two", 9_000)
        other = store.create_or_reuse(_key("https://other.example"), b"/private/three")
        assert second.job_id == first.job_id
        assert second.private_path == b"/private/\xff-one"
        assert other.key.instance_url == "https://other.example"
        assert store.get(first.job_id) == first
        assert {job.job_id for job in store.list()} == {first.job_id, other.job_id}


def test_claims_compete_transactionally_and_generation_is_monotonic() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "publications.sqlite3"
        stores = [PublicationStore(path, clock=lambda: 1_000) for _ in range(8)]
        key = _key()
        stores[0].create_or_reuse(key, b"/private/recording")

        def claim(index: int) -> str:
            job = stores[index].claim_one(f"worker-{index}", now_ms=1_000)
            return "none" if job is None else job.lease_owner or "missing"

        with ThreadPoolExecutor(max_workers=len(stores)) as executor:
            results = list(executor.map(claim, range(len(stores))))
        assert sum(value != "none" for value in results) == 1
        claimed = stores[0].get(key)
        assert claimed is not None and claimed.lease_generation == 1


def test_due_ids_and_next_wake_use_state_deadlines_origin_and_stable_order() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        queued_key = _key("https://due.example")
        delayed_key = _key("https://due.example", "b" * 64)
        other_key = _key("https://other.example", "c" * 64)
        queued = store.create_or_reuse(queued_key, b"/private/queued", job_id="queued-due")
        store.create_or_reuse(delayed_key, b"/private/delayed", job_id="delayed-due")
        store.create_or_reuse(other_key, b"/private/other", job_id="other-due")
        with sqlite3.connect(store.database_path) as connection:
            # Exercise the exact boundary and keep another origin in the table.
            connection.execute(
                "UPDATE publications SET next_attempt_at_ms = ? WHERE job_id = ?",
                (1_001, "delayed-due"),
            )
            connection.execute(
                "UPDATE publications SET next_attempt_at_ms = ? WHERE job_id = ?",
                (1_000, "other-due"),
            )

        assert store.due_job_ids("HTTPS://DUE.EXAMPLE:443/", now_ms=999) == ()
        assert store.due_job_ids("https://due.example", now_ms=1_000) == (queued.job_id,)
        assert store.next_wake_at_ms("https://due.example", now_ms=999) == 1_000
        assert store.next_wake_at_ms("https://due.example", now_ms=1_500) == 1_500

        # A transferring lease is due at expiry, not at its retry timestamp.
        transfer_key = _key("https://due.example", "d" * 64)
        transfer = store.create_or_reuse(transfer_key, b"/private/transferring", job_id="transfer-due")
        claimed = store.claim_one("transfer", transfer_key, lease_ms=10, now_ms=1_000)
        assert claimed is not None
        store.transition(
            transfer_key, PublicationState.TRANSFERRING,
            owner="transfer", generation=claimed.lease_generation, now_ms=1_001,
        )
        assert transfer.job_id not in store.due_job_ids("https://due.example", now_ms=1_009)
        assert transfer.job_id in store.due_job_ids("https://due.example", now_ms=1_010)


def test_due_limit_and_terminal_or_wrong_origin_rows_are_excluded() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        jobs = [
            store.create_or_reuse(
                _key("https://limit.example", format(index, "064x")),
                b"/private/recording", job_id=f"limit-{index}",
            )
            for index in range(3)
        ]
        terminal_key = _key("https://limit.example", "f" * 64)
        terminal = store.create_or_reuse(terminal_key, b"/private/terminal")
        store.transition(terminal_key, PublicationState.UNCERTAIN, operation="none")
        wrong_key = _key("https://wrong.example", "e" * 64)
        store.create_or_reuse(wrong_key, b"/private/wrong")

        assert store.due_job_ids("https://limit.example", now_ms=1_000, limit=2) == tuple(
            sorted(job.job_id for job in jobs)[:2]
        )
        assert terminal.job_id not in store.due_job_ids("https://limit.example", now_ms=1_000)
        _raises(store.due_job_ids, "https://limit.example", limit=0)
        _raises(store.due_job_ids, "https://limit.example", limit=1_001)


def test_claim_for_action_ignores_backoff_and_claims_all_eligible_phases() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)

        # Seed one queued job whose normal due deadline is deliberately far away.
        queued_key = _key("https://action-queued.example")
        store.create_or_reuse(queued_key, b"/private/queued")
        with sqlite3.connect(store.database_path) as connection:
            connection.execute(
                "UPDATE publications SET next_attempt_at_ms = 999999 "
                "WHERE instance_url = ? AND recording_sha256 = ?",
                (queued_key.instance_url, queued_key.recording_sha256),
            )
        queued = store.claim_for_action(queued_key, "action-queued", 100)
        assert queued is not None and queued.state is PublicationState.QUEUED
        assert queued.attempt_count == 1 and queued.lease_generation == 1

        # Known-record metadata retries remain claimable even when backoff is active.
        metadata_key = _key("https://action-metadata.example")
        store.create_or_reuse(metadata_key, b"/private/metadata")
        store.transition(metadata_key, PublicationState.TRANSFERRING)
        store.accept_transfer(metadata_key, 7)
        with sqlite3.connect(store.database_path) as connection:
            connection.execute(
                "UPDATE publications SET next_attempt_at_ms = 999999 "
                "WHERE instance_url = ? AND recording_sha256 = ?",
                (metadata_key.instance_url, metadata_key.recording_sha256),
            )
        metadata = store.claim_for_action(metadata_key, "action-metadata", 100)
        assert metadata is not None and metadata.state is PublicationState.METADATA_PENDING

        # Active reconciliation also ignores backoff, while terminal uncertainty does not.
        uncertain_key = _key("https://action-uncertain.example")
        store.create_or_reuse(uncertain_key, b"/private/uncertain")
        store.transition(uncertain_key, PublicationState.UNCERTAIN)
        with sqlite3.connect(store.database_path) as connection:
            connection.execute(
                "UPDATE publications SET next_attempt_at_ms = 999999 "
                "WHERE instance_url = ? AND recording_sha256 = ?",
                (uncertain_key.instance_url, uncertain_key.recording_sha256),
            )
        uncertain = store.claim_for_action(uncertain_key, "action-uncertain", 100)
        assert uncertain is not None and uncertain.reconciliation_eligible

        terminal_key = _key("https://action-terminal.example")
        store.create_or_reuse(terminal_key, b"/private/terminal")
        store.transition(terminal_key, PublicationState.UNCERTAIN, operation="none")
        assert store.claim_for_action(terminal_key, "action-terminal", 100) is None


def test_claim_for_action_recovers_expired_transfer_and_filters_origin_and_leases() -> None:
    with TemporaryDirectory() as directory:
        now = [1_000]
        store = _store(directory, clock=lambda: now[0])
        expired_key = _key("https://action-expired.example")
        store.create_or_reuse(expired_key, b"/private/expired")
        first = store.claim_one("old-worker", lease_ms=10, now_ms=1_000)
        assert first is not None
        store.transition(
            expired_key, PublicationState.TRANSFERRING,
            owner="old-worker", generation=first.lease_generation, now_ms=1_001,
        )
        now[0] = 1_011
        recovered = store.claim_for_action(expired_key, "reconciler", 100)
        assert recovered is not None and recovered.state is PublicationState.UNCERTAIN
        assert recovered.lease_generation == 2 and recovered.attempt_count == 2

        # An explicit origin mismatch must not claim or mutate the referenced row.
        origin_key = _key("https://action-origin.example")
        store.create_or_reuse(origin_key, b"/private/origin")
        assert store.claim_for_action(
            origin_key, "wrong-origin", 100, instance_url="https://other.example",
        ) is None
        origin_job = store.get(origin_key)
        assert origin_job is not None and origin_job.lease_owner is None and origin_job.attempt_count == 0

        # A genuine unexpired lease wins over an operator action claim.
        leased_key = _key("https://action-leased.example")
        store.create_or_reuse(leased_key, b"/private/leased")
        leased = store.claim_one("active-worker", reference=leased_key, lease_ms=100, now_ms=1_011)
        assert leased is not None
        assert store.claim_for_action(leased_key, "action-worker", 100) is None

        # Releasing and reclaiming fences the previous owner and generation.
        store.release(leased_key, "active-worker", leased.lease_generation, now_ms=1_012)
        fresh = store.claim_for_action(leased_key, "fresh-worker", 100)
        assert fresh is not None and fresh.lease_generation == leased.lease_generation + 1
        _raises(
            store.transition, leased_key, PublicationState.BLOCKED,
            owner="active-worker", generation=leased.lease_generation,
        )


def test_expiry_recovers_each_kind_and_claims_new_generation() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        key = _key()
        store.create_or_reuse(key, b"/private/a")
        first = store.claim_one("first", lease_ms=10, now_ms=1_000)
        assert first is not None
        transferring = store.transition(key, PublicationState.TRANSFERRING, owner="first", generation=1, now_ms=1_001)
        assert transferring.state is PublicationState.TRANSFERRING
        uncertain = store.claim_one("second", lease_ms=10, now_ms=1_011)
        assert uncertain is not None and uncertain.state is PublicationState.UNCERTAIN
        assert uncertain.operation == "reconcile" and uncertain.reconciliation_token
        assert uncertain.lease_generation == 2

        pending = store.transition(key, PublicationState.METADATA_PENDING, owner="second", generation=2, remote_recording_id=7, now_ms=1_012)
        assert pending.lease_owner is None
        reclaimed = store.claim_one("third", lease_ms=10, now_ms=1_012)
        assert reclaimed is not None and reclaimed.state is PublicationState.METADATA_PENDING
        store.release(key, "third", reclaimed.lease_generation, now_ms=1_013)
        queued = store.retry(key, now_ms=1_014)
        assert queued.state is PublicationState.METADATA_PENDING


def test_queued_expiry_is_safe_reclaim_and_renewal_fences_owner() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        key = _key()
        store.create_or_reuse(key, b"/private/a")
        claim = store.claim_one("worker", lease_ms=10, now_ms=1_000)
        assert claim is not None
        renewed = store.renew(key, "worker", claim.lease_generation, lease_ms=100, now_ms=1_005)
        assert renewed.lease_expires_at_ms == 1_105
        _raises(store.renew, key, "stale", claim.lease_generation, now_ms=1_006)
        store.release(key, "worker", claim.lease_generation, now_ms=1_007)
        reclaimed = store.claim_one("next", lease_ms=10, now_ms=1_008)
        assert reclaimed is not None and reclaimed.lease_generation == 2


def test_stale_owner_cannot_transition_after_expiry_or_recovery() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        key = _key()
        store.create_or_reuse(key, b"/private/a")
        claim = store.claim_one("old", lease_ms=10, now_ms=1_000)
        assert claim is not None
        store.transition(key, PublicationState.TRANSFERRING, owner="old", generation=1, now_ms=1_001)
        fresh = store.claim_one("new", lease_ms=100, now_ms=1_011)
        assert fresh is not None and fresh.lease_generation == 2
        _raises(store.transition, key, PublicationState.UNCERTAIN, owner="old", generation=1, now_ms=1_012)


def test_all_safe_transitions_and_known_id_never_reenter_post() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        key = _key()
        store.create_or_reuse(key, b"/private/a")
        store.transition(key, PublicationState.TRANSFERRING, now_ms=1_001)
        pending = store.accept_transfer(key, 9)
        assert pending.state is PublicationState.METADATA_PENDING and pending.http_method == "PATCH"
        failed = store.mark_metadata_pending(key, "metadata_failed", 503)
        assert failed.last_error_code == "metadata_failed" and failed.last_http_status == 503
        published = store.mark_published(key)
        assert published.job.state is PublicationState.PUBLISHED
        assert store.mark_published(key).already_published
        _raises(store.retry, key)

        # Cleanup owns the local_removed transition until issue #25 lands.
        _raises(store.transition, key, PublicationState.LOCAL_REMOVED)


def test_future_local_removed_rows_remain_readable_without_cleanup_transition() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        key = _key()
        store.create_or_reuse(key, b"/private/a")
        with sqlite3.connect(store.database_path) as connection:
            # Model the structurally valid row that the cleanup issue will write later.
            connection.execute(
                "UPDATE publications SET state = 'local_removed', operation = 'none', "
                "resume_intent = 'none', private_path = NULL, local_removed_at_ms = 1001 "
                "WHERE instance_url = ? AND recording_sha256 = ?",
                (key.instance_url, key.recording_sha256),
            )
        future = store.get(key)
        assert future is not None and future.state is PublicationState.LOCAL_REMOVED
        _raises(store.transition, key, PublicationState.LOCAL_REMOVED)


def test_resume_intent_controls_relink_retry_and_forget_is_explicit() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        post_key = _key()
        job = store.create_or_reuse(post_key, b"/private/Secret Acquisition Meeting.mkv")
        blocked = store.transition(post_key, PublicationState.BLOCKED)
        assert blocked.operation == "none" and blocked.resume_intent == "post"
        relinked = store.relink(post_key, b"/private/\xff-new.mkv")
        assert relinked.state is PublicationState.BLOCKED
        relinked = store.retry(post_key)
        assert relinked.state is PublicationState.QUEUED and relinked.operation == "post"

        reconcile_key = _key("https://reconcile.example")
        store.create_or_reuse(reconcile_key, b"/private/reconcile")
        uncertain = store.transition(reconcile_key, PublicationState.UNCERTAIN)
        blocked_reconcile = store.transition(reconcile_key, PublicationState.BLOCKED)
        assert blocked_reconcile.operation == "none" and blocked_reconcile.resume_intent == "reconcile"
        restored = store.retry(reconcile_key)
        assert restored.state is PublicationState.UNCERTAIN and restored.operation == "reconcile"

        patch_key = _key("https://patch.example")
        store.create_or_reuse(patch_key, b"/private/patch")
        store.transition(patch_key, PublicationState.TRANSFERRING)
        store.accept_transfer(patch_key, 8)
        blocked_patch = store.transition(patch_key, PublicationState.BLOCKED)
        assert blocked_patch.operation == "none" and blocked_patch.resume_intent == "patch"
        assert store.retry(patch_key).state is PublicationState.METADATA_PENDING

        terminal = store.transition(reconcile_key, PublicationState.UNCERTAIN, operation="none")
        assert terminal.operation == "none" and not terminal.reconciliation_eligible
        assert store.claim_one("reconciler", reconcile_key, now_ms=1_000) is None
        store.forget(reconcile_key)
        assert store.get(reconcile_key) is None
        store.forget(post_key)
        store.forget(patch_key)

        raw = store.database_path.read_bytes()
        assert b"Bearer secret" not in raw and b"private metadata" not in raw
        assert store.get(job.job_id) is None


def test_attempt_count_is_informational_and_retries_are_unbounded() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        key = _key()
        store.create_or_reuse(key, b"/private/a")
        # Repeated release and claim cycles must never become blocked by persistence.
        for index in range(8):
            claim = store.claim_one(f"worker-{index}", now_ms=1_000 + index)
            assert claim is not None and claim.attempt_count == index + 1
            released = store.release(key, claim.lease_owner or "", claim.lease_generation, now_ms=1_000 + index)
            assert released.state is PublicationState.QUEUED
        final = store.get(key)
        assert final is not None and final.attempt_count == 8

        transfer_key = _key("https://transfer.example")
        store.create_or_reuse(transfer_key, b"/private/transfer")
        # The in-flight marker records the same claim, not a second attempt.
        first = store.claim_one("transfer-worker", transfer_key, now_ms=2_000)
        assert first is not None and first.attempt_count == 1
        transferring = store.transition(
            transfer_key, PublicationState.TRANSFERRING,
            owner="transfer-worker", generation=first.lease_generation, now_ms=2_001,
        )
        assert transferring.attempt_count == 1


def test_update_path_matches_absolute_bytes_and_identity_without_touching_job_fields() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        old_path = b"/private/old-recording.mkv"
        new_path = b"/private/new-recording.mkv"
        identity = MediaIdentity(Path("/private/old-recording.mkv"), 7, 8, 9, 10)

        queued_key = _key("https://rename-queued.example")
        blocked_key = _key("https://rename-blocked.example")
        missing_key = _key("https://rename-missing.example")
        # Seed rows with one shared locator and a caller-proven current identity.
        for key in (queued_key, blocked_key, missing_key):
            store.create_or_reuse(key, old_path, identity=identity)
        store.transition(blocked_key, PublicationState.BLOCKED, error_code="protocol_error", http_status=409)
        store.transition(missing_key, PublicationState.MISSING, error_code="local_missing")
        before = {key: store.get(key) for key in (queued_key, blocked_key, missing_key)}

        # The bounded callback changes only two matching rows in this pass.
        assert store.update_path(old_path, new_path, identity, limit=2) == 2
        updated = {key: store.get(key) for key in (queued_key, blocked_key, missing_key)}
        changed = [job for job in updated.values() if job is not None and job.private_path == new_path]
        assert len(changed) == 2
        for key, job in updated.items():
            original = before[key]
            assert job is not None and original is not None
            if job.private_path == new_path:
                assert job == replace(original, private_path=new_path)

        # Wrong locator or identity must leave every row untouched.
        assert store.update_path(b"/private/not-old.mkv", new_path, identity) == 0
        wrong_identity = MediaIdentity(identity.path, 7, 99, 9, 10)
        assert store.update_path(old_path, new_path, wrong_identity) == 0
        assert store.update_path(old_path, new_path, identity) == 1

        leased_key = _key("https://rename-leased.example")
        store.create_or_reuse(leased_key, old_path, identity=identity)
        claim = store.claim_one("rename-worker", reference=leased_key, now_ms=1_000)
        assert claim is not None
        # Leased rows are deferred so worker fencing cannot be bypassed.
        assert store.update_path(old_path, new_path, identity) == 0
        leased = store.get(leased_key)
        assert leased is not None and leased.private_path == old_path


def test_uncertain_retry_is_the_only_uncertain_to_post_authorization() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        key = _key()
        store.create_or_reuse(key, b"/private/a")
        uncertain = store.transition(key, PublicationState.UNCERTAIN)
        assert uncertain.reconciliation_eligible
        terminal = store.transition(key, PublicationState.UNCERTAIN, operation="none")
        assert terminal.operation == "none"
        requeued = store.retry(key)
        assert requeued.state is PublicationState.QUEUED
        assert requeued.operation == "post" and requeued.remote_recording_id is None
        assert requeued.reconciliation_token is None
        transferring = store.transition(key, PublicationState.TRANSFERRING)
        assert transferring.reconciliation_token


def test_uncertain_forget_rejects_active_lease_but_allows_unleased_row() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        key = _key()
        store.create_or_reuse(key, b"/private/a")
        store.transition(key, PublicationState.UNCERTAIN)
        claim = store.claim_one("reconciler", now_ms=1_000)
        assert claim is not None
        _raises(store.forget, key)
        store.release(key, "reconciler", claim.lease_generation, now_ms=1_001)
        store.forget(key)
        assert store.get(key) is None


def test_security_rejects_database_and_directory_symlinks() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        state = root / "state"
        state.mkdir(mode=0o700)
        target = root / "real.sqlite3"
        target.touch(mode=0o600)
        os.symlink(target, state / "publications.sqlite3")
        _raises(PublicationStore, state / "publications.sqlite3")
