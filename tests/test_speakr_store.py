"""Focused persistence tests for the canonical Speakr state machine."""

from concurrent.futures import ThreadPoolExecutor
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


def _identity(name="recording.mkv") -> MediaIdentity:
    return MediaIdentity(Path(name), 1, 2, 3, 4_000_000_000)


def _key(url="HTTPS://EXAMPLE.COM:443/", digest=HASH) -> PublicationKey:
    return PublicationKey(url, digest)


def _store(directory: str, *, clock=None) -> PublicationStore:
    return PublicationStore(
        Path(directory) / "state" / "publications.sqlite3",
        clock=clock or (lambda: 1_000),
    )


def test_default_path_and_constructor_have_one_canonical_path_surface() -> None:
    with TemporaryDirectory() as directory:
        old_xdg = os.environ.get("XDG_STATE_HOME")
        try:
            os.environ["XDG_STATE_HOME"] = directory
            store = PublicationStore(clock=lambda: 1_000)
            assert store.database_path == Path(directory) / "meeting-recorder" / "publications.sqlite3"
            assert store.state_directory.stat().st_mode & 0o777 == 0o700
            assert not hasattr(store, "db_path")
            assert not hasattr(store, "state_dir")
            assert not hasattr(store, "staging_directory")
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg


def test_schema_is_private_strict_and_public_only() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        with sqlite3.connect(store.database_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (1,)
            assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
            columns = [row[1] for row in connection.execute("PRAGMA table_info(publications)")]
            assert columns == [
                "instance_url", "recording_sha256", "media_device", "media_inode",
                "media_size", "source_mtime_ns", "file_last_modified_ms", "state", "attempt_count",
                "remote_recording_id", "last_error_code", "last_http_status", "transfer_started_at_ms",
                "accepted_at_ms", "published_at_ms", "created_at_ms", "updated_at_ms",
            ]
            assert not set(columns).intersection({
                "title", "meeting_date", "notes", "participants", "token", "body", "headers",
            })
            assert connection.execute(
                "SELECT wr, strict FROM pragma_table_list WHERE name = 'publications'"
            ).fetchone() == (1, 1)
        assert store.database_path.stat().st_mode & 0o777 == 0o600
        assert store.lock_path.stat().st_mode & 0o777 == 0o600


def test_create_is_idempotent_by_normalized_key_and_does_not_overwrite() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        first = store.create_ready(_key(), _identity(), 4_000)
        second = store.create_ready(_key("https://example.com"), _identity("different.mkv"), 9_000)
        other = store.create_ready(_key("https://other.example"), _identity(), 4_000)
        assert second == first
        assert other.key.instance_url == "https://other.example"


def test_all_valid_transitions_retain_attempt_remote_id_and_clear_patch_errors() -> None:
    with TemporaryDirectory() as directory:
        ticks = iter([1_000, 2_000, 3_000, 4_000, 5_000])
        store = _store(directory, clock=lambda: next(ticks))
        key = _key()
        store.create_ready(key, _identity(), 4_000)
        transferring = store.begin_transfer(key, _identity("before.mkv"), 4_001)
        assert transferring.state is PublicationState.TRANSFERRING
        assert transferring.attempt_count == 1
        pending = store.accept_transfer(key, 9)
        assert pending.state is PublicationState.METADATA_PENDING
        assert pending.remote_recording_id == 9
        failed = store.mark_metadata_pending(key, "metadata_failed", 503)
        assert failed.last_error_code == "metadata_failed"
        assert failed.last_http_status == 503
        result = store.mark_published(key)
        assert result.job.state is PublicationState.PUBLISHED
        assert result.job.remote_recording_id == 9
        assert result.job.last_error_code is None
        again = store.mark_published(key)
        assert again.already_published
        assert again.job.published_at_ms == result.job.published_at_ms


def test_rejected_can_retry_unknown_cannot_restart_and_invalid_predicates_fail() -> None:
    with TemporaryDirectory() as directory:
        ticks = iter([1_000, 2_000, 3_000, 4_000, 5_000])
        store = _store(directory, clock=lambda: next(ticks))
        key = _key()
        store.create_ready(key, _identity(), 4_000)
        store.begin_transfer(key, _identity(), 4_000)
        rejected = store.mark_transfer_rejected(key, "transfer_rejected", 503)
        assert rejected.state is PublicationState.TRANSFER_REJECTED
        retried = store.begin_transfer(key, _identity(), 4_000)
        assert retried.attempt_count == 2
        unknown = store.mark_transfer_unknown(key)
        assert unknown.state is PublicationState.TRANSFER_UNKNOWN
        try:
            store.begin_transfer(key, _identity(), 4_000)
            assert False, "unknown transfers must not restart"
        except PublicationTransitionError:
            pass
        try:
            store.mark_published(key)
            assert False, "unknown transfers cannot publish"
        except PublicationTransitionError:
            pass


def test_interrupted_transfer_is_terminal_unknown() -> None:
    with TemporaryDirectory() as directory:
        ticks = iter([1_000, 2_000, 3_000])
        store = _store(directory, clock=lambda: next(ticks))
        key = _key()
        store.create_ready(key, _identity(), 4_000)
        store.begin_transfer(key, _identity(), 4_000)
        recovered = store.mark_interrupted_transfer_unknown(key)
        assert recovered.state is PublicationState.TRANSFER_UNKNOWN
        assert recovered.last_error_code == "interrupted_transfer"


def test_symlinks_nonregular_and_unsafe_modes_are_rejected() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        state = root / "state"
        state.mkdir(mode=0o700)
        target = root / "real.sqlite3"
        target.touch(mode=0o600)
        os.symlink(target, state / "publications.sqlite3")
        try:
            PublicationStore(state / "publications.sqlite3")
            assert False, "database symlink must be rejected"
        except PublicationStoreSecurityError:
            pass

        (state / "publications.sqlite3").unlink()
        lock = state / "publications.lock"
        lock.unlink()
        lock.mkdir(mode=0o700)
        try:
            PublicationStore(state / "publications.sqlite3")
            assert False, "lock directory must be rejected"
        except PublicationStoreSecurityError:
            pass


def test_newer_and_legacy_migrations_fail_without_partial_schema_change() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "publications.sqlite3"
        path.touch(mode=0o600)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE old(value TEXT)")
            connection.execute("PRAGMA user_version = 0")
        try:
            PublicationStore(path)
            assert False, "legacy version zero schema must be rejected"
        except PublicationMigrationError:
            pass

        path.unlink()
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version = 99")
        os.chmod(path, 0o600)
        try:
            PublicationStore(path)
            assert False, "newer schema must be rejected"
        except PublicationMigrationError:
            pass


def test_error_validation_and_database_bytes_do_not_store_private_sentinels() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory, clock=lambda: 2_000)
        key = _key()
        private_identity = _identity("/private/Secret Acquisition Meeting.mkv")
        store.create_ready(key, private_identity, 4_000)
        store.begin_transfer(key, private_identity, 4_000)
        try:
            store.mark_transfer_rejected(key, "BearerSecret")
            assert False, "unknown error vocabulary must be rejected"
        except ValueError:
            pass
        store.mark_transfer_unknown(key, "transfer_unknown")
        raw = store.database_path.read_bytes()
        assert b"BearerSecret" not in raw
        assert b"private metadata" not in raw
        assert b"Secret Acquisition Meeting" not in raw
        assert b"/private/" not in raw


def test_sqlite_checks_reject_accepted_states_with_zero_attempts() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        key = _key()
        store.create_ready(key, _identity(), 4_000)
        store.begin_transfer(key, _identity(), 4_000)
        store.accept_transfer(key, 9)

        try:
            with sqlite3.connect(store.database_path) as connection:
                connection.execute(
                    "UPDATE publications SET attempt_count = 0 "
                    "WHERE instance_url = ? AND recording_sha256 = ?",
                    (key.instance_url, key.recording_sha256),
                )
            assert False, "metadata_pending must require a transfer attempt"
        except sqlite3.IntegrityError:
            pass

        store.mark_published(key)
        try:
            with sqlite3.connect(store.database_path) as connection:
                connection.execute(
                    "UPDATE publications SET attempt_count = 0 "
                    "WHERE instance_url = ? AND recording_sha256 = ?",
                    (key.instance_url, key.recording_sha256),
                )
            assert False, "published must require a transfer attempt"
        except sqlite3.IntegrityError:
            pass


def test_connect_rejects_database_replacement_and_closes_every_descriptor() -> None:
    import meeting_recorder.speakr_store as module

    with TemporaryDirectory() as directory:
        store = _store(directory)
        replacement = store.database_path.with_name("replacement.sqlite3")
        backup = store.database_path.with_name("original.sqlite3")
        original_connect = module.sqlite3.connect
        before = set(os.listdir("/proc/self/fd"))

        def replace_then_connect(path, *args, **kwargs):
            os.rename(path, backup)
            replacement.touch(mode=0o600)
            return original_connect(path, *args, **kwargs)

        module.sqlite3.connect = replace_then_connect
        try:
            try:
                store._connect()
                assert False, "database replacement must be rejected"
            except PublicationStoreSecurityError:
                pass
        finally:
            module.sqlite3.connect = original_connect
        assert set(os.listdir("/proc/self/fd")) == before


def test_connect_closes_connection_and_descriptor_when_pragma_fails() -> None:
    import meeting_recorder.speakr_store as module

    with TemporaryDirectory() as directory:
        store = _store(directory)
        original_connect = module.sqlite3.connect
        before = set(os.listdir("/proc/self/fd"))
        closed: list[bool] = []

        class FailingConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *args):
                if sql == "PRAGMA busy_timeout = 5000":
                    raise sqlite3.OperationalError("injected PRAGMA failure")
                return self.connection.execute(sql, *args)

            def close(self):
                closed.append(True)
                return self.connection.close()

        module.sqlite3.connect = lambda path, *args, **kwargs: FailingConnection(
            original_connect(path, *args, **kwargs)
        )
        try:
            try:
                store._connect()
                assert False, "PRAGMA failure must be raised"
            except sqlite3.OperationalError:
                pass
        finally:
            module.sqlite3.connect = original_connect
        assert closed == [True]
        assert set(os.listdir("/proc/self/fd")) == before


def test_lock_fd_cleanup_is_idempotent() -> None:
    with TemporaryDirectory() as directory:
        store = _store(directory)
        before = set(os.listdir("/proc/self/fd"))
        with store.publication_lock():
            with store.publication_lock():
                pass
        after = set(os.listdir("/proc/self/fd"))
        assert after == before


def test_concurrent_threads_serialize_one_begin_for_one_key() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "publications.sqlite3"
        stores = [PublicationStore(path, clock=lambda: 1_000) for _ in range(8)]
        key = _key()
        stores[0].create_ready(key, _identity(), 4_000)

        def attempt(index: int) -> str:
            try:
                return str(stores[index].begin_transfer(key, _identity(), 4_000).attempt_count)
            except PublicationTransitionError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=len(stores)) as executor:
            results = list(executor.map(attempt, range(len(stores))))
        assert results.count("1") == 1
        assert results.count("rejected") == len(stores) - 1


def test_write_rollback_and_first_creation_directory_fsync() -> None:
    import meeting_recorder.speakr_store as module

    with TemporaryDirectory() as directory:
        fsynced: list[Path] = []
        original_fsync = module._fsync_directory
        module._fsync_directory = lambda path: fsynced.append(path)
        try:
            store = _store(directory, clock=lambda: 2_000)
        finally:
            module._fsync_directory = original_fsync
        assert fsynced == [store.state_directory]

        key = _key()
        store.create_ready(key, _identity(), 4_000)
        store.begin_transfer(key, _identity(), 4_000)
        original_job = store._job
        setattr(store, "_job", lambda *_args: (_ for _ in ()).throw(RuntimeError("rollback sentinel")))
        try:
            try:
                store.mark_transfer_unknown(key)
                assert False, "injected update failure must be raised"
            except RuntimeError:
                pass
        finally:
            setattr(store, "_job", original_job)
        current = store.get(key)
        assert current is not None and current.state is PublicationState.TRANSFERRING
        assert not any(path.name.endswith(("-journal", "-wal", "-shm")) for path in store.state_directory.iterdir())
