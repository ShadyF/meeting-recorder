"""Private SQLite persistence for the public Speakr publication state."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from typing import Callable, Iterator, Sequence

from .speakr_domain import MediaIdentity, PublicationJob, PublicationKey, PublicationResult, PublicationState


class PublicationStoreError(RuntimeError):
    """Base class for persistence and state-machine failures."""


class PublicationMigrationError(PublicationStoreError, ValueError):
    """The database is absent, corrupt, or from an unsupported schema version."""


class PublicationTransitionError(PublicationStoreError, ValueError):
    """An operation did not match the job's expected durable state."""


class PublicationStoreSecurityError(PublicationStoreError, ValueError):
    """A state-store path or file does not meet the private-file contract."""


_SCHEMA_VERSION = 1
_DATABASE_NAME = "publications.sqlite3"
_LOCK_NAME = "publications.lock"
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Only bounded classifications cross the durable boundary.  In particular,
# transport messages, response bodies, credentials, and filesystem details do
# not belong in the database.
_ALLOWED_ERROR_CODES = frozenset({
    "auth_error", "connection_error", "conflict", "file_changed", "http_error",
    "interrupted_transfer", "invalid_media", "invalid_response", "metadata_ambiguous",
    "metadata_changed", "metadata_failed", "metadata_malformed", "metadata_missing",
    "metadata_unavailable", "network_error", "not_found", "permission_denied",
    "protocol_error", "server_error", "staging_failed", "timeout", "transfer_not_sent",
    "transfer_rejected", "transfer_unknown", "unknown", "upload_failed",
})

_COLUMN_NAMES = (
    "instance_url", "recording_sha256", "path_hint", "media_device", "media_inode",
    "media_size", "source_mtime_ns", "file_last_modified_ms", "state", "attempt_count",
    "remote_recording_id", "last_error_code", "last_http_status", "transfer_started_at_ms",
    "accepted_at_ms", "published_at_ms", "created_at_ms", "updated_at_ms",
)
_SELECT_COLUMNS = ", ".join(_COLUMN_NAMES)


def _schema_sql() -> str:
    states = ", ".join(f"'{state.value}'" for state in PublicationState)
    errors = ", ".join(f"'{code}'" for code in sorted(_ALLOWED_ERROR_CODES))
    return f"""
        CREATE TABLE publications (
            instance_url TEXT NOT NULL,
            recording_sha256 TEXT NOT NULL,
            path_hint TEXT NOT NULL CHECK(length(path_hint) > 0),
            media_device INTEGER NOT NULL CHECK(media_device >= 0),
            media_inode INTEGER NOT NULL CHECK(media_inode >= 0),
            media_size INTEGER NOT NULL CHECK(media_size >= 0),
            source_mtime_ns INTEGER NOT NULL CHECK(source_mtime_ns >= 0),
            file_last_modified_ms INTEGER NOT NULL CHECK(file_last_modified_ms >= 0),
            state TEXT NOT NULL CHECK(state IN ({states})),
            attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
            remote_recording_id INTEGER CHECK(remote_recording_id IS NULL OR remote_recording_id > 0),
            last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN ({errors})),
            last_http_status INTEGER CHECK(last_http_status IS NULL OR (last_http_status >= 100 AND last_http_status <= 599)),
            transfer_started_at_ms INTEGER CHECK(transfer_started_at_ms IS NULL OR transfer_started_at_ms >= 0),
            accepted_at_ms INTEGER CHECK(accepted_at_ms IS NULL OR accepted_at_ms >= 0),
            published_at_ms INTEGER CHECK(published_at_ms IS NULL OR published_at_ms >= 0),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
            PRIMARY KEY (instance_url, recording_sha256),
            CHECK(length(recording_sha256) = 64 AND recording_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK((transfer_started_at_ms IS NULL OR transfer_started_at_ms >= created_at_ms)
                AND (accepted_at_ms IS NULL OR accepted_at_ms >= created_at_ms)
                AND (published_at_ms IS NULL OR published_at_ms >= created_at_ms)),
            CHECK(
                (state = 'ready' AND attempt_count = 0 AND remote_recording_id IS NULL
                    AND last_error_code IS NULL AND last_http_status IS NULL
                    AND transfer_started_at_ms IS NULL AND accepted_at_ms IS NULL AND published_at_ms IS NULL)
                OR
                (state = 'transferring' AND attempt_count > 0 AND remote_recording_id IS NULL
                    AND last_error_code IS NULL AND last_http_status IS NULL
                    AND transfer_started_at_ms IS NOT NULL AND accepted_at_ms IS NULL AND published_at_ms IS NULL)
                OR
                (state IN ('transfer_rejected', 'transfer_unknown') AND attempt_count > 0
                    AND remote_recording_id IS NULL AND last_error_code IS NOT NULL
                    AND transfer_started_at_ms IS NOT NULL AND accepted_at_ms IS NULL AND published_at_ms IS NULL)
                OR
                (state = 'metadata_pending' AND remote_recording_id > 0
                    AND transfer_started_at_ms IS NOT NULL AND accepted_at_ms IS NOT NULL AND published_at_ms IS NULL
                    AND accepted_at_ms >= transfer_started_at_ms)
                OR
                (state = 'published' AND remote_recording_id > 0
                    AND last_error_code IS NULL AND last_http_status IS NULL
                    AND transfer_started_at_ms IS NOT NULL AND accepted_at_ms IS NOT NULL AND published_at_ms IS NOT NULL
                    AND accepted_at_ms >= transfer_started_at_ms AND published_at_ms >= accepted_at_ms)
            )
        ) STRICT, WITHOUT ROWID
    """


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _validate_http_status(value: object) -> int:
    if type(value) is not int or not 100 <= value <= 599:
        raise ValueError("HTTP status is invalid")
    return value


def _validate_error_code(value: object) -> str:
    if not isinstance(value, str) or _SAFE_ERROR_CODE.fullmatch(value) is None:
        raise ValueError("error code is not an allowed safe code")
    if value not in _ALLOWED_ERROR_CODES:
        raise ValueError("error code is not in the stable allowlist")
    return value


def _validate_nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _validate_path(value: object) -> str:
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        raise ValueError("publication path is invalid")
    return str(value)


def _validate_identity(value: object) -> MediaIdentity:
    if not isinstance(value, MediaIdentity):
        raise ValueError("media identity is invalid")
    return value


def _validate_fd(fd: int, message: str) -> os.stat_result:
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    if not flags & fcntl.FD_CLOEXEC:
        raise PublicationStoreSecurityError("state-store descriptor is not close-on-exec")
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise PublicationStoreSecurityError(message)
    if stat.S_IMODE(info.st_mode) != _PRIVATE_FILE_MODE:
        raise PublicationStoreSecurityError("state-store file permissions are unsafe")
    return info


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise PublicationStoreSecurityError("state-store directory is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PublicationStoreSecurityError("state-store directory is not a directory")
    if stat.S_IMODE(info.st_mode) != _PRIVATE_DIR_MODE:
        try:
            os.chmod(path, _PRIVATE_DIR_MODE)
        except OSError as exc:
            raise PublicationStoreSecurityError("state-store directory permissions are unsafe") from exc


def _open_private_file(path: Path) -> tuple[int, bool]:
    existed = os.path.lexists(path)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, _PRIVATE_FILE_MODE)
    except OSError as exc:
        raise PublicationStoreSecurityError("state-store file cannot be opened safely") from exc
    try:
        _validate_fd(fd, "state-store component is not a regular file")
    except Exception:
        os.close(fd)
        raise
    return fd, not existed


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PublicationStoreSecurityError("state-store directory cannot be synced") from exc
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class PublicationStore:
    """Short SQLite transactions under one private state directory."""

    def __init__(self, path: str | os.PathLike[str] | None = None, *, clock: Callable[[], int] | None = None) -> None:
        resolved = default_database_path() if path is None else self._resolve_path(path)
        self.database_path = resolved
        self.state_directory = resolved.parent
        self.lock_path = self.state_directory / _LOCK_NAME
        self._clock = clock or _now_ms
        self._lock_state = threading.local()

        _private_directory(self.state_directory)
        lock_fd, _ = _open_private_file(self.lock_path)
        os.close(lock_fd)
        database_fd, database_created = _open_private_file(self.database_path)
        os.close(database_fd)
        self._database_created_and_unsynced = database_created
        self.migrate()

    @staticmethod
    def _resolve_path(value: str | os.PathLike[str]) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.name in {"", ".", ".."}:
            raise PublicationStoreSecurityError("publication database path is invalid")
        return path

    def _time_ms(self) -> int:
        value = self._clock()
        return _validate_nonnegative_int(value, "publication clock")

    @contextmanager
    def publication_lock(self) -> Iterator[None]:
        """Serialize one complete publication command across processes."""
        existing_fd = getattr(self._lock_state, "fd", None)
        if existing_fd is not None:
            self._lock_state.depth += 1
            try:
                yield
            finally:
                self._lock_state.depth -= 1
            return

        descriptor = -1
        acquired = False
        try:
            descriptor, _ = _open_private_file(self.lock_path)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            acquired = True
            self._lock_state.fd = descriptor
            self._lock_state.depth = 1
            yield
        finally:
            if acquired:
                self._lock_state.fd = None
                self._lock_state.depth = 0
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _connect(self) -> sqlite3.Connection:
        # Validate the final component immediately before SQLite opens it.
        descriptor, _ = _open_private_file(self.database_path)
        try:
            connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level=None)
        except Exception:
            os.close(descriptor)
            raise
        os.close(descriptor)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def migrate(self) -> None:
        """Create schema version one and reject every private legacy schema."""
        connection = self._connect()
        created_schema = False
        try:
            connection.execute("BEGIN EXCLUSIVE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise PublicationMigrationError("publication database version is newer than supported")
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if version == 0:
                if tables:
                    raise PublicationMigrationError("publication database has an unsupported legacy schema")
                connection.execute(_schema_sql())
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                created_schema = True
            else:
                self._validate_schema(connection, tables)
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()
        if created_schema and self._database_created_and_unsynced:
            _fsync_directory(self.state_directory)
            self._database_created_and_unsynced = False

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection, tables: Sequence[tuple[str]]) -> None:
        if tuple(name for (name,) in tables) != ("publications",):
            raise PublicationMigrationError("publication database schema is not version one")
        columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(publications)"))
        if columns != _COLUMN_NAMES:
            raise PublicationMigrationError("publication database columns are not version one")
        table_list = connection.execute(
            "SELECT wr, strict FROM pragma_table_list WHERE name = 'publications'"
        ).fetchone()
        if table_list != (1, 1):
            raise PublicationMigrationError("publication table is not strict and without rowid")
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'publications'"
        ).fetchone()
        normalize = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
        if not sql or normalize(sql[0]) != normalize(_schema_sql()):
            raise PublicationMigrationError("publication table definition is not version one")

    @staticmethod
    def _row_to_job(row: sqlite3.Row | tuple[object, ...]) -> PublicationJob:
        values = dict(zip(_COLUMN_NAMES, row))
        try:
            return PublicationJob(
                key=PublicationKey(values["instance_url"], values["recording_sha256"]),
                state=PublicationState(values["state"]),
                remote_recording_id=values["remote_recording_id"],
                path_hint=values["path_hint"],
                media_device=values["media_device"], media_inode=values["media_inode"],
                media_size=values["media_size"], source_mtime_ns=values["source_mtime_ns"],
                file_last_modified_ms=values["file_last_modified_ms"],
                attempt_count=values["attempt_count"], last_error_code=values["last_error_code"],
                last_http_status=values["last_http_status"],
                transfer_started_at_ms=values["transfer_started_at_ms"],
                accepted_at_ms=values["accepted_at_ms"], published_at_ms=values["published_at_ms"],
                created_at_ms=values["created_at_ms"], updated_at_ms=values["updated_at_ms"],
            )
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            raise PublicationStoreError("publication row failed public validation") from exc

    @staticmethod
    def _fetch(connection: sqlite3.Connection, key: PublicationKey) -> tuple[object, ...] | None:
        return connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM publications WHERE instance_url = ? AND recording_sha256 = ?",
            (key.instance_url, key.recording_sha256),
        ).fetchone()

    def _write(self, callback: Callable[[sqlite3.Connection], object]) -> object:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = callback(connection)
            connection.commit()
            return result
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def _job(self, connection: sqlite3.Connection, key: PublicationKey) -> PublicationJob:
        row = self._fetch(connection, key)
        if row is None:
            raise PublicationStoreError("publication update did not produce a job")
        return self._row_to_job(row)

    def _transition_error(self, connection: sqlite3.Connection, key: PublicationKey) -> PublicationTransitionError:
        if self._fetch(connection, key) is None:
            return PublicationTransitionError("publication job does not exist")
        return PublicationTransitionError("publication job is not in the expected state")

    def get(self, key: PublicationKey) -> PublicationJob | None:
        if not isinstance(key, PublicationKey):
            raise TypeError("get requires a PublicationKey")
        connection = self._connect()
        try:
            row = self._fetch(connection, key)
            return None if row is None else self._row_to_job(row)
        finally:
            connection.close()

    def create_ready(self, key: PublicationKey, identity: MediaIdentity, file_last_modified_ms: int) -> PublicationJob:
        if not isinstance(key, PublicationKey):
            raise TypeError("create_ready requires a PublicationKey")
        identity = _validate_identity(identity)
        file_last_modified_ms = _validate_nonnegative_int(file_last_modified_ms, "file last modified time")
        now = self._time_ms()
        values = (
            key.instance_url, key.recording_sha256, _validate_path(identity.path),
            identity.device, identity.inode, identity.size, identity.mtime_ns,
            file_last_modified_ms, PublicationState.READY.value, 0, None, None, None,
            None, None, None, now, now,
        )

        def insert(connection: sqlite3.Connection) -> PublicationJob:
            connection.execute(
                f"INSERT OR IGNORE INTO publications ({_SELECT_COLUMNS}) VALUES ({', '.join('?' for _ in _COLUMN_NAMES)})",
                values,
            )
            return self._job(connection, key)

        return self._write(insert)  # type: ignore[return-value]

    def begin_transfer(self, key: PublicationKey, identity: MediaIdentity, mtime_ms: int) -> PublicationJob:
        if not isinstance(key, PublicationKey):
            raise TypeError("begin_transfer requires a PublicationKey")
        identity = _validate_identity(identity)
        mtime_ms = _validate_nonnegative_int(mtime_ms, "file last modified time")

        def begin(connection: sqlite3.Connection) -> PublicationJob:
            row = self._fetch(connection, key)
            if row is None or row[_COLUMN_NAMES.index("state")] not in {
                    PublicationState.READY.value, PublicationState.TRANSFER_REJECTED.value}:
                raise self._transition_error(connection, key)
            started = self._time_ms()
            cursor = connection.execute(
                """
                UPDATE publications
                   SET state = 'transferring', path_hint = ?, media_device = ?, media_inode = ?,
                       media_size = ?, source_mtime_ns = ?, file_last_modified_ms = ?,
                       attempt_count = attempt_count + 1, transfer_started_at_ms = ?,
                       accepted_at_ms = NULL, published_at_ms = NULL, remote_recording_id = NULL,
                       last_error_code = NULL, last_http_status = NULL, updated_at_ms = ?
                 WHERE instance_url = ? AND recording_sha256 = ?
                   AND state IN ('ready', 'transfer_rejected')
                """,
                (_validate_path(identity.path), identity.device, identity.inode, identity.size,
                 identity.mtime_ns, mtime_ms, started, started,
                 key.instance_url, key.recording_sha256),
            )
            if cursor.rowcount != 1:
                raise self._transition_error(connection, key)
            return self._job(connection, key)

        return self._write(begin)  # type: ignore[return-value]

    def mark_interrupted_transfer_unknown(self, key: PublicationKey) -> PublicationJob:
        return self.mark_transfer_unknown(key, "interrupted_transfer")

    def mark_transfer_rejected(self, key: PublicationKey, error_code: str, status: int | None = None) -> PublicationJob:
        return self._mark_transfer_failure(key, PublicationState.TRANSFER_REJECTED, error_code, status)

    def mark_transfer_unknown(self, key: PublicationKey, error_code: str = "transfer_unknown", status: int | None = None) -> PublicationJob:
        return self._mark_transfer_failure(key, PublicationState.TRANSFER_UNKNOWN, error_code, status)

    def _mark_transfer_failure(
        self, key: PublicationKey, state: PublicationState, error_code: str, status: int | None,
    ) -> PublicationJob:
        if not isinstance(key, PublicationKey):
            raise TypeError("transfer transition requires a PublicationKey")
        if state not in {PublicationState.TRANSFER_REJECTED, PublicationState.TRANSFER_UNKNOWN}:
            raise ValueError("invalid transfer failure state")
        error_code = _validate_error_code(error_code)
        if status is not None:
            status = _validate_http_status(status)

        def fail(connection: sqlite3.Connection) -> PublicationJob:
            now = self._time_ms()
            cursor = connection.execute(
                """
                UPDATE publications SET state = ?, last_error_code = ?, last_http_status = ?,
                    accepted_at_ms = NULL, published_at_ms = NULL, remote_recording_id = NULL,
                    updated_at_ms = ?
                 WHERE instance_url = ? AND recording_sha256 = ? AND state = 'transferring'
                """,
                (state.value, error_code, status, now, key.instance_url, key.recording_sha256),
            )
            if cursor.rowcount != 1:
                raise self._transition_error(connection, key)
            return self._job(connection, key)

        return self._write(fail)  # type: ignore[return-value]

    def accept_transfer(self, key: PublicationKey, remote_recording_id: int, status: int = 202) -> PublicationJob:
        if not isinstance(key, PublicationKey):
            raise TypeError("accept_transfer requires a PublicationKey")
        if type(remote_recording_id) is not int or remote_recording_id <= 0:
            raise ValueError("remote recording ID must be a positive integer")
        if status != 202:
            raise ValueError("only HTTP 202 is an accepted transfer")

        def accept(connection: sqlite3.Connection) -> PublicationJob:
            accepted = self._time_ms()
            cursor = connection.execute(
                """
                UPDATE publications SET state = 'metadata_pending', remote_recording_id = ?,
                    accepted_at_ms = ?, last_error_code = NULL, last_http_status = NULL,
                    published_at_ms = NULL, updated_at_ms = ?
                 WHERE instance_url = ? AND recording_sha256 = ? AND state = 'transferring'
                """,
                (remote_recording_id, accepted, accepted, key.instance_url, key.recording_sha256),
            )
            if cursor.rowcount != 1:
                raise self._transition_error(connection, key)
            return self._job(connection, key)

        return self._write(accept)  # type: ignore[return-value]

    def mark_metadata_pending(
        self, key: PublicationKey, error_code: str, status: int | None, current_path: Path | str,
    ) -> PublicationJob:
        if not isinstance(key, PublicationKey):
            raise TypeError("mark_metadata_pending requires a PublicationKey")
        error_code = _validate_error_code(error_code)
        if status is not None:
            status = _validate_http_status(status)
        current_path = _validate_path(current_path)

        def pending(connection: sqlite3.Connection) -> PublicationJob:
            now = self._time_ms()
            cursor = connection.execute(
                """
                UPDATE publications SET state = 'metadata_pending', path_hint = ?,
                    last_error_code = ?, last_http_status = ?, published_at_ms = NULL,
                    updated_at_ms = ?
                 WHERE instance_url = ? AND recording_sha256 = ?
                   AND state = 'metadata_pending'
                """,
                (current_path, error_code, status, now, key.instance_url, key.recording_sha256),
            )
            if cursor.rowcount != 1:
                raise self._transition_error(connection, key)
            return self._job(connection, key)

        return self._write(pending)  # type: ignore[return-value]

    def mark_published(self, key: PublicationKey, current_path: Path | str) -> PublicationResult:
        if not isinstance(key, PublicationKey):
            raise TypeError("mark_published requires a PublicationKey")
        current_path = _validate_path(current_path)

        def publish(connection: sqlite3.Connection) -> PublicationResult:
            row = self._fetch(connection, key)
            if row is None:
                raise self._transition_error(connection, key)
            state = row[_COLUMN_NAMES.index("state")]
            if state == PublicationState.PUBLISHED.value:
                return PublicationResult(self._job(connection, key), True)
            if state != PublicationState.METADATA_PENDING.value:
                raise self._transition_error(connection, key)
            published = self._time_ms()
            cursor = connection.execute(
                """
                UPDATE publications SET state = 'published', path_hint = ?,
                    last_error_code = NULL, last_http_status = NULL,
                    published_at_ms = ?, updated_at_ms = ?
                 WHERE instance_url = ? AND recording_sha256 = ? AND state = 'metadata_pending'
                """,
                (current_path, published, published, key.instance_url, key.recording_sha256),
            )
            if cursor.rowcount != 1:
                raise self._transition_error(connection, key)
            return PublicationResult(self._job(connection, key), False)

        return self._write(publish)  # type: ignore[return-value]


def default_database_path() -> Path:
    """Return the private default state database location for the CLI."""
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        base = Path(state_home).expanduser()
        if not base.is_absolute():
            raise PublicationStoreSecurityError("XDG_STATE_HOME must be absolute")
    else:
        base = Path.home() / ".local" / "state"
    return base / "meeting-recorder" / _DATABASE_NAME


__all__ = [
    "PublicationMigrationError", "PublicationStore", "PublicationStoreError",
    "PublicationStoreSecurityError", "PublicationTransitionError", "default_database_path",
]
