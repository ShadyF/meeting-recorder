"""Private SQLite persistence and lease-fenced Speakr job primitives."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Callable, Iterator, List, Sequence
from uuid import uuid4

from .speakr_domain import (
    CleanupClaim, CleanupIntent, CleanupPhase, MediaIdentity, PublicationJob, PublicationKey, PublicationOperation,
    PublicationResult, PublicationState, ResumeIntent, Tag, _MAX_TAGS, _SAFE_ERROR_CODES,
    normalize_speakr_url,
)


class PublicationStoreError(RuntimeError):
    """Base class for persistence and state-machine failures."""


class PublicationMigrationError(PublicationStoreError, ValueError):
    """The database is absent, corrupt, or from an unsupported schema version."""


class PublicationTransitionError(PublicationStoreError, ValueError):
    """An operation did not match the job's expected state or lease fence."""


class PublicationStoreSecurityError(PublicationStoreError, ValueError):
    """A state-store path or file does not meet the private-file contract."""


_SCHEMA_VERSION = 4
_DATABASE_NAME = "publications.sqlite3"
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ALLOWED_ERROR_CODES = _SAFE_ERROR_CODES
_DEFAULT_LEASE_MS = 60_000
_MAX_DUE_IDS = 1_000
_MAX_CLEANUP_GROUP = 100
_MAX_CLEANUP_INTENTS = 100
_MAX_TAG_JSON_BYTES = 65_536
_CLEANUP_PHASE_ORDER = (
    CleanupPhase.PREPARED,
    CleanupPhase.SIDECAR_QUARANTINED,
    CleanupPhase.MEDIA_QUARANTINED,
    CleanupPhase.SIDECAR_UNLINKED,
    CleanupPhase.MEDIA_UNLINKED,
)

_INDEX_DEFINITIONS = (
    (
        "idx_publications_origin_due",
        ("instance_url", "operation", "next_attempt_at_ms", "lease_expires_at_ms", "job_id"),
    ),
    (
        "idx_publications_lease_expiry",
        ("lease_expires_at_ms", "lease_owner", "state", "job_id"),
    ),
    (
        "idx_publications_private_media_identity",
        ("private_path", "media_device", "media_inode", "media_size", "source_mtime_ns", "lease_owner", "job_id"),
    ),
    (
        "idx_publications_cleanup_path",
        ("private_path", "state", "cleanup_lease_owner", "job_id"),
    ),
    (
        "idx_publications_cleanup_candidates",
        ("state", "private_path", "created_at_ms", "job_id"),
    ),
)

_CLEANUP_INDEX_DEFINITIONS = (
    ("idx_cleanup_intents_phase", ("phase", "updated_at_ms", "intent_id")),
    ("idx_cleanup_members_job", ("job_id", "intent_id")),
)

_COLUMN_NAMES = (
    "job_id", "instance_url", "recording_sha256", "private_path", "media_device",
    "media_inode", "media_size", "source_mtime_ns", "file_last_modified_ms", "state",
    "operation", "resume_intent", "reconciliation_token", "remote_recording_id",
    "attempt_count", "next_attempt_at_ms", "lease_owner",
    "lease_generation", "lease_expires_at_ms", "last_error_code", "last_http_status",
    "transfer_started_at_ms", "accepted_at_ms", "published_at_ms", "uncertain_at_ms",
    "blocked_at_ms", "missing_at_ms", "local_removed_at_ms", "created_at_ms", "updated_at_ms",
    "cleanup_lease_owner", "cleanup_lease_generation", "cleanup_lease_expires_at_ms",
    "frozen_tags", "effective_tags", "missing_tags", "upload_tags_unknown", "sidecar_warning",
)
_V3_COLUMN_NAMES = _COLUMN_NAMES[:-5]
_SELECT_COLUMNS = ", ".join(_COLUMN_NAMES)


def _schema_sql(table_name: str = "publications", *, include_tags: bool = True) -> str:
    states = ", ".join(f"'{state.value}'" for state in PublicationState)
    operations = ", ".join(f"'{operation.value}'" for operation in PublicationOperation)
    intents = ", ".join(f"'{intent.value}'" for intent in ResumeIntent)
    errors = ", ".join(f"'{code}'" for code in sorted(_ALLOWED_ERROR_CODES))
    tag_columns = "" if not include_tags else f"""
            frozen_tags TEXT NOT NULL CHECK(length(CAST(frozen_tags AS BLOB)) <= {_MAX_TAG_JSON_BYTES} AND json_valid(frozen_tags)),
            effective_tags TEXT NOT NULL CHECK(length(CAST(effective_tags AS BLOB)) <= {_MAX_TAG_JSON_BYTES} AND json_valid(effective_tags)),
            missing_tags TEXT NOT NULL CHECK(length(CAST(missing_tags AS BLOB)) <= {_MAX_TAG_JSON_BYTES} AND json_valid(missing_tags)),
            upload_tags_unknown INTEGER NOT NULL CHECK(upload_tags_unknown IN (0, 1)),
            sidecar_warning INTEGER NOT NULL CHECK(sidecar_warning IN (0, 1)),"""
    return f"""
        CREATE TABLE {table_name} (
            job_id TEXT PRIMARY KEY,
            instance_url TEXT NOT NULL,
            recording_sha256 TEXT NOT NULL,
            private_path BLOB,
            media_device INTEGER NOT NULL CHECK(media_device >= 0),
            media_inode INTEGER NOT NULL CHECK(media_inode >= 0),
            media_size INTEGER NOT NULL CHECK(media_size >= 0),
            source_mtime_ns INTEGER NOT NULL CHECK(source_mtime_ns >= 0),
            file_last_modified_ms INTEGER NOT NULL CHECK(file_last_modified_ms >= 0),
            state TEXT NOT NULL CHECK(state IN ({states})),
            operation TEXT NOT NULL CHECK(operation IN ({operations})),
            resume_intent TEXT NOT NULL CHECK(resume_intent IN ({intents})),
            reconciliation_token TEXT,
            remote_recording_id INTEGER CHECK(remote_recording_id IS NULL OR remote_recording_id > 0),
            attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
            next_attempt_at_ms INTEGER NOT NULL CHECK(next_attempt_at_ms >= 0),
            lease_owner TEXT,
            lease_generation INTEGER NOT NULL CHECK(lease_generation >= 0),
            lease_expires_at_ms INTEGER CHECK(lease_expires_at_ms IS NULL OR lease_expires_at_ms >= 0),
            last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN ({errors})),
            last_http_status INTEGER CHECK(last_http_status IS NULL OR (last_http_status BETWEEN 100 AND 599)),
            transfer_started_at_ms INTEGER CHECK(transfer_started_at_ms IS NULL OR transfer_started_at_ms >= 0),
            accepted_at_ms INTEGER CHECK(accepted_at_ms IS NULL OR accepted_at_ms >= 0),
            published_at_ms INTEGER CHECK(published_at_ms IS NULL OR published_at_ms >= 0),
            uncertain_at_ms INTEGER CHECK(uncertain_at_ms IS NULL OR uncertain_at_ms >= 0),
            blocked_at_ms INTEGER CHECK(blocked_at_ms IS NULL OR blocked_at_ms >= 0),
            missing_at_ms INTEGER CHECK(missing_at_ms IS NULL OR missing_at_ms >= 0),
            local_removed_at_ms INTEGER CHECK(local_removed_at_ms IS NULL OR local_removed_at_ms >= 0),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
            cleanup_lease_owner TEXT,
            cleanup_lease_generation INTEGER NOT NULL CHECK(cleanup_lease_generation >= 0),
            cleanup_lease_expires_at_ms INTEGER CHECK(cleanup_lease_expires_at_ms IS NULL OR cleanup_lease_expires_at_ms >= 0),
            {tag_columns}
            UNIQUE (instance_url, recording_sha256),
            CHECK(length(recording_sha256) = 64 AND recording_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK((lease_owner IS NULL AND lease_expires_at_ms IS NULL)
                OR (lease_owner IS NOT NULL AND lease_expires_at_ms IS NOT NULL)),
            CHECK((cleanup_lease_owner IS NULL AND cleanup_lease_expires_at_ms IS NULL)
                OR (cleanup_lease_owner IS NOT NULL AND cleanup_lease_expires_at_ms IS NOT NULL
                    AND state = 'published')),
            CHECK(private_path IS NOT NULL OR state IN ('missing', 'local_removed')),
            CHECK(
                (state = 'queued' AND remote_recording_id IS NULL AND operation = 'post'
                    AND resume_intent = 'post')
                OR
                (state = 'transferring' AND remote_recording_id IS NULL
                    AND operation = 'none' AND resume_intent = 'post'
                    AND reconciliation_token IS NOT NULL)
                OR
                (state = 'metadata_pending' AND remote_recording_id > 0
                    AND operation = 'patch' AND resume_intent = 'patch')
                OR
                (state = 'published' AND remote_recording_id > 0
                    AND operation = 'none' AND resume_intent = 'none'
                    AND last_error_code IS NULL AND last_http_status IS NULL)
                OR
                (state = 'uncertain' AND remote_recording_id IS NULL
                    AND operation IN ('none', 'reconcile') AND resume_intent = 'reconcile'
                    AND reconciliation_token IS NOT NULL)
                OR
                (state IN ('blocked', 'missing') AND operation = 'none'
                    AND ((resume_intent IN ('post', 'reconcile') AND remote_recording_id IS NULL)
                        OR (resume_intent = 'patch' AND remote_recording_id > 0)
                        OR resume_intent = 'none'))
                OR
                (state = 'local_removed' AND operation = 'none' AND resume_intent = 'none'
                    AND private_path IS NULL AND reconciliation_token IS NULL
                    AND lease_owner IS NULL AND lease_expires_at_ms IS NULL
                    AND cleanup_lease_owner IS NULL AND cleanup_lease_expires_at_ms IS NULL
                    AND cleanup_lease_generation = 0 AND next_attempt_at_ms = 0
                    AND last_error_code IS NULL AND last_http_status IS NULL
                    AND remote_recording_id IS NOT NULL AND remote_recording_id > 0
                    AND transfer_started_at_ms IS NOT NULL AND accepted_at_ms IS NOT NULL
                    AND published_at_ms IS NOT NULL AND local_removed_at_ms IS NOT NULL
                    AND transfer_started_at_ms <= accepted_at_ms
                    AND accepted_at_ms <= published_at_ms
                    AND published_at_ms <= local_removed_at_ms)
            )
        ) STRICT, WITHOUT ROWID;

        CREATE TABLE cleanup_intents (
            intent_id TEXT PRIMARY KEY CHECK(length(intent_id) BETWEEN 1 AND 128),
            expected_private_path BLOB NOT NULL CHECK(length(expected_private_path) BETWEEN 1 AND 4096
                AND substr(expected_private_path, 1, 1) = X'2F'),
            expected_recording_sha256 TEXT NOT NULL CHECK(length(expected_recording_sha256) = 64
                AND expected_recording_sha256 NOT GLOB '*[^0-9a-f]*'),
            media_device INTEGER NOT NULL CHECK(media_device >= 0),
            media_inode INTEGER NOT NULL CHECK(media_inode >= 0),
            media_size INTEGER NOT NULL CHECK(media_size >= 0),
            media_mtime_ns INTEGER NOT NULL CHECK(media_mtime_ns >= 0),
            sidecar_device INTEGER CHECK(sidecar_device IS NULL OR sidecar_device >= 0),
            sidecar_inode INTEGER CHECK(sidecar_inode IS NULL OR sidecar_inode >= 0),
            sidecar_size INTEGER CHECK(sidecar_size IS NULL OR sidecar_size >= 0),
            sidecar_mtime_ns INTEGER CHECK(sidecar_mtime_ns IS NULL OR sidecar_mtime_ns >= 0),
            quarantine_media_basename TEXT NOT NULL CHECK(length(quarantine_media_basename) BETWEEN 1 AND 255),
            quarantine_sidecar_basename TEXT CHECK(quarantine_sidecar_basename IS NULL
                OR length(quarantine_sidecar_basename) BETWEEN 1 AND 255),
            phase TEXT NOT NULL CHECK(phase IN ('prepared', 'sidecar_quarantined', 'media_quarantined', 'sidecar_unlinked', 'media_unlinked')),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
            media_nlink INTEGER NOT NULL CHECK(media_nlink = 1),
            sidecar_nlink INTEGER CHECK(sidecar_nlink IS NULL OR sidecar_nlink = 1),
            CHECK((sidecar_device IS NULL AND sidecar_inode IS NULL AND sidecar_size IS NULL AND sidecar_mtime_ns IS NULL)
                OR (sidecar_device IS NOT NULL AND sidecar_inode IS NOT NULL AND sidecar_size IS NOT NULL AND sidecar_mtime_ns IS NOT NULL
                    AND sidecar_nlink = 1)),
            CHECK(quarantine_sidecar_basename IS NULL OR quarantine_sidecar_basename != quarantine_media_basename)
        ) STRICT, WITHOUT ROWID;

        CREATE TABLE cleanup_intent_members (
            intent_id TEXT NOT NULL REFERENCES cleanup_intents(intent_id) ON DELETE CASCADE,
            job_id TEXT NOT NULL REFERENCES publications(job_id) ON DELETE RESTRICT,
            lease_generation INTEGER NOT NULL CHECK(lease_generation >= 0),
            PRIMARY KEY(intent_id, job_id),
            UNIQUE(job_id)
        ) STRICT, WITHOUT ROWID
    """


def _normalized_sql(value: str) -> str:
    """Normalize only harmless SQL whitespace for exact schema-definition comparison."""
    return re.sub(r"\s+", " ", value).strip().rstrip(";").casefold()


def _table_statement(schema: str, table_name: str) -> str:
    """Extract one generated CREATE TABLE statement for schema validation."""
    for statement in schema.split(";"):
        if re.search(rf"\bCREATE TABLE {re.escape(table_name)}\b", statement, re.IGNORECASE):
            return statement
    raise PublicationMigrationError(f"generated schema is missing table {table_name}")


def _expected_v3_publication_sql() -> str:
    """Return the exact retired v3 definition used only for reset detection."""
    return _table_statement(_schema_sql(include_tags=False), "publications")


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


def _encode_tags(tags: tuple[Tag, ...] | None) -> str:
    """Serialize validated public tag values into a canonical durable snapshot."""
    value = None if tags is None else [{"tag_id": tag.tag_id, "name": tag.name} for tag in tags]
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > _MAX_TAG_JSON_BYTES:
        raise ValueError("tag snapshot is too large")
    return encoded


def _decode_tags(value: object, name: str) -> tuple[Tag, ...] | None:
    """Decode one bounded schema-owned tag snapshot without accepting loose JSON shapes."""
    if not isinstance(value, str) or len(value.encode("utf-8")) > 1_048_576:
        raise ValueError(f"{name} is invalid")
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} is invalid") from exc
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > _MAX_TAGS:
        raise ValueError(f"{name} is invalid")
    tags = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"tag_id", "name"}:
            raise ValueError(f"{name} is invalid")
        tags.append(Tag(item["tag_id"], item["name"]))
    return tuple(tags)


def _validate_identity(value: object | None) -> MediaIdentity | None:
    if value is not None and not isinstance(value, MediaIdentity):
        raise ValueError("media identity is invalid")
    return value


def _path_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        path = value
    elif isinstance(value, (str, os.PathLike)):
        path = os.fsencode(os.fspath(value))
    else:
        raise ValueError("private media path is invalid")
    if not path or b"\x00" in path or len(path) > 4096:
        raise ValueError("private media path is invalid")
    return path


def _absolute_path_bytes(value: object, name: str) -> bytes:
    path = _path_bytes(value)
    if not path.startswith(b"/"):
        raise ValueError(f"{name} must be an absolute filesystem path")
    return path


def _validate_fd(fd: int, message: str) -> os.stat_result:
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    if not flags & fcntl.FD_CLOEXEC:
        raise PublicationStoreSecurityError("state-store descriptor is not close-on-exec")
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise PublicationStoreSecurityError(message)
    if stat.S_IMODE(info.st_mode) != _PRIVATE_FILE_MODE:
        raise PublicationStoreSecurityError("state-store file permissions are unsafe")
    if info.st_uid != os.getuid():
        raise PublicationStoreSecurityError("state-store file ownership is unsafe")
    return info


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise PublicationStoreSecurityError("state-store directory is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PublicationStoreSecurityError("state-store directory is not a directory")
    if info.st_uid != os.getuid():
        raise PublicationStoreSecurityError("state-store directory ownership is unsafe")
    if stat.S_IMODE(info.st_mode) != _PRIVATE_DIR_MODE:
        try:
            os.chmod(path, _PRIVATE_DIR_MODE)
        except OSError as exc:
            raise PublicationStoreSecurityError("state-store directory permissions are unsafe") from exc
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != _PRIVATE_DIR_MODE
        ):
            raise PublicationStoreSecurityError("state-store directory permissions are unsafe")


def _private_file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


def _verify_path_matches_fd(path: Path, descriptor_info: os.stat_result) -> None:
    try:
        path_info = os.lstat(path)
    except OSError as exc:
        raise PublicationStoreSecurityError("state-store path changed during open") from exc
    if _private_file_identity(path_info) != _private_file_identity(descriptor_info):
        raise PublicationStoreSecurityError("state-store path changed during open")


def _verify_private_file(path: Path, descriptor: int, expected_info: os.stat_result) -> None:
    current_info = _validate_fd(descriptor, "state-store component is not a regular file")
    if _private_file_identity(current_info) != _private_file_identity(expected_info):
        raise PublicationStoreSecurityError("state-store file changed during open")
    _verify_path_matches_fd(path, current_info)


def _verify_private_parent(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise PublicationStoreSecurityError("state-store directory is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != _PRIVATE_DIR_MODE or info.st_uid != os.getuid()
    ):
        raise PublicationStoreSecurityError("state-store directory is not private")


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
    """Short SQLite transactions; external publication work never runs here."""

    def __init__(self, path: str | os.PathLike[str] | None = None, *, clock: Callable[[], int] | None = None) -> None:
        resolved = default_database_path() if path is None else self._resolve_path(path)
        self.database_path = resolved
        self.state_directory = resolved.parent
        self._clock = clock or _now_ms
        _private_directory(self.state_directory)
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
        return _validate_nonnegative_int(self._clock(), "publication clock")

    def current_time_ms(self) -> int:
        """Return the store's injected durable clock for coordinated journal timestamps."""
        return self._time_ms()

    def _connect(self) -> sqlite3.Connection:
        _verify_private_parent(self.state_directory)
        descriptor = -1
        connection: sqlite3.Connection | None = None
        try:
            # Keep the validated inode open while SQLite resolves the pathname.
            descriptor, _ = _open_private_file(self.database_path)
            descriptor_info = _validate_fd(descriptor, "state-store component is not a regular file")
            _verify_path_matches_fd(self.database_path, descriptor_info)
            connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level=None)
            _verify_private_file(self.database_path, descriptor, descriptor_info)
            _verify_private_parent(self.state_directory)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            _verify_private_file(self.database_path, descriptor, descriptor_info)
            _verify_private_parent(self.state_directory)
            return connection
        except Exception:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            raise
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def migrate(self) -> None:
        """Create v4, discarding only an exact retired v3 database."""
        connection = self._connect()
        created_schema = False
        try:
            connection.execute("BEGIN EXCLUSIVE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if version == 0 and not tables:
                for statement in _schema_sql().split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                self._ensure_indexes(connection)
                created_schema = True
            elif version == 3:
                self._validate_schema(connection, tables, expected_version=3)
                self._validate_exact_indexes(connection)
                self._reset_legacy_v3(connection)
                created_schema = True
            elif version != _SCHEMA_VERSION:
                raise PublicationMigrationError(
                    f"publication database schema version {version} is unsupported; expected v4"
                )
            else:
                self._validate_schema(connection, tables, expected_version=4)
                self._ensure_indexes(connection)
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
    def _validate_schema(
        connection: sqlite3.Connection,
        tables: Sequence[tuple[str]],
        *,
        expected_version: int,
    ) -> None:
        expected_tables = (("cleanup_intent_members",), ("cleanup_intents",), ("publications",))
        if tuple(sorted(tables)) != tuple(sorted(expected_tables)):
            raise PublicationMigrationError("publication database tables are incompatible")
        columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(publications)"))
        expected_columns = _V3_COLUMN_NAMES if expected_version == 3 else _COLUMN_NAMES
        if columns != expected_columns:
            raise PublicationMigrationError("publication database columns are incompatible")
        table_list = connection.execute(
            "SELECT wr, strict FROM pragma_table_list WHERE name = 'publications'"
        ).fetchone()
        if table_list != (1, 1):
            raise PublicationMigrationError("publication table is not strict and without rowid")
        expected_publication_sql = _expected_v3_publication_sql() if expected_version == 3 else _table_statement(_schema_sql(), "publications")
        actual_publication_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'publications'"
        ).fetchone()
        if not actual_publication_sql or _normalized_sql(actual_publication_sql[0]) != _normalized_sql(expected_publication_sql):
            raise PublicationMigrationError("publication table definition is incompatible")
        if expected_version in {3, 4}:
            for table_name in ("cleanup_intents", "cleanup_intent_members"):
                table_list = connection.execute(
                    "SELECT wr, strict FROM pragma_table_list WHERE name = ?", (table_name,)
                ).fetchone()
                if table_list != (1, 1):
                    raise PublicationMigrationError("cleanup tables are not strict and without rowid")
                actual_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
                ).fetchone()
                expected_sql = _table_statement(_schema_sql(), table_name)
                if not actual_sql or _normalized_sql(actual_sql[0]) != _normalized_sql(expected_sql):
                    raise PublicationMigrationError(f"{table_name} definition is incompatible")

    @staticmethod
    def _reset_legacy_v3(connection: sqlite3.Connection) -> None:
        """Rebuild the verified legacy inode within its exclusive transaction."""
        # Overwrite retired cells before transactional DDL removes their schema.
        connection.execute("PRAGMA secure_delete = ON")
        # Remove children first so foreign keys never leave an intermediate orphan.
        for name, _, _ in connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        ).fetchall():
            connection.execute(f"DROP INDEX {name}")
        for table_name in ("cleanup_intent_members", "cleanup_intents", "publications"):
            connection.execute(f"DROP TABLE {table_name}")
        # Publish a fresh v4 schema without changing the database pathname or companions.
        for statement in _schema_sql().split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        PublicationStore._ensure_indexes(connection)

    @staticmethod
    def _validate_exact_indexes(connection: sqlite3.Connection) -> None:
        """Confirm a retired database is exact before its irreversible reset."""
        expected = dict(_INDEX_DEFINITIONS + _CLEANUP_INDEX_DEFINITIONS)
        rows = connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        ).fetchall()
        if {row[0] for row in rows} != set(expected):
            raise PublicationMigrationError("publication indexes are incompatible")
        for name, table, sql in rows:
            expected_table = "publications" if name in dict(_INDEX_DEFINITIONS) else (
                "cleanup_intents" if name == "idx_cleanup_intents_phase" else "cleanup_intent_members"
            )
            expected_sql = f"CREATE INDEX {name} ON {expected_table} ({', '.join(expected[name])})"
            columns = tuple(row[2] for row in connection.execute(f"PRAGMA index_info({name})"))
            if table != expected_table or columns != expected[name] or _normalized_sql(sql) != _normalized_sql(expected_sql):
                raise PublicationMigrationError("publication indexes are incompatible")

    @staticmethod
    def _ensure_indexes(connection: sqlite3.Connection) -> None:
        """Create and validate the bounded-query indexes without changing rows."""
        for name, columns in _INDEX_DEFINITIONS:
            expected_sql = f"CREATE INDEX {name} ON publications ({', '.join(columns)})"
            index = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (name,),
            ).fetchone()
            if index is None:
                connection.execute(expected_sql)
                continue

            # Existing named indexes must retain the exact definition expected by the store.
            normalize = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
            details = connection.execute("PRAGMA index_list(publications)").fetchall()
            detail = next((row for row in details if row[1] == name), None)
            actual_columns = tuple(
                row[2] for row in connection.execute(f"PRAGMA index_info({name})")
            )
            if (
                detail is None or detail[2] != 0 or actual_columns != columns
                or normalize(index[0]) != normalize(expected_sql)
            ):
                raise PublicationMigrationError(f"publication index {name} is invalid")
        for name, columns in _CLEANUP_INDEX_DEFINITIONS:
            table = "cleanup_intents" if name == "idx_cleanup_intents_phase" else "cleanup_intent_members"
            expected_sql = f"CREATE INDEX {name} ON {table} ({', '.join(columns)})"
            index = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
            ).fetchone()
            if index is None:
                connection.execute(expected_sql)
                continue
            actual_columns = tuple(
                row[2] for row in connection.execute(f"PRAGMA index_info({name})")
            )
            if actual_columns != columns or _normalized_sql(index[0]) != _normalized_sql(expected_sql):
                raise PublicationMigrationError(f"publication index {name} is invalid")

    @staticmethod
    def _row_to_job(row: sqlite3.Row | tuple[object, ...]) -> PublicationJob:
        values = dict(zip(_COLUMN_NAMES, row))
        try:
            # Reject non-canonical SQLite flag values before converting them to bool.
            if values["upload_tags_unknown"] not in (0, 1) or values["sidecar_warning"] not in (0, 1):
                raise ValueError("tag status flags are invalid")
            return PublicationJob(
                job_id=values["job_id"],
                key=PublicationKey(values["instance_url"], values["recording_sha256"]),
                private_path=values["private_path"], state=PublicationState(values["state"]),
                operation=values["operation"], resume_intent=values["resume_intent"],
                reconciliation_token=values["reconciliation_token"],
                remote_recording_id=values["remote_recording_id"],
                media_device=values["media_device"], media_inode=values["media_inode"],
                media_size=values["media_size"], source_mtime_ns=values["source_mtime_ns"],
                file_last_modified_ms=values["file_last_modified_ms"],
                attempt_count=values["attempt_count"],
                next_attempt_at_ms=values["next_attempt_at_ms"], lease_owner=values["lease_owner"],
                lease_generation=values["lease_generation"],
                lease_expires_at_ms=values["lease_expires_at_ms"],
                last_error_code=values["last_error_code"], last_http_status=values["last_http_status"],
                transfer_started_at_ms=values["transfer_started_at_ms"],
                accepted_at_ms=values["accepted_at_ms"], published_at_ms=values["published_at_ms"],
                uncertain_at_ms=values["uncertain_at_ms"], blocked_at_ms=values["blocked_at_ms"],
                missing_at_ms=values["missing_at_ms"], local_removed_at_ms=values["local_removed_at_ms"],
                created_at_ms=values["created_at_ms"], updated_at_ms=values["updated_at_ms"],
                cleanup_lease_owner=values["cleanup_lease_owner"],
                cleanup_lease_generation=values["cleanup_lease_generation"],
                cleanup_lease_expires_at_ms=values["cleanup_lease_expires_at_ms"],
                frozen_tags=_decode_tags(values["frozen_tags"], "frozen tags") or (),
                effective_tags=_decode_tags(values["effective_tags"], "effective tags"),
                missing_tags=_decode_tags(values["missing_tags"], "missing tags"),
                upload_tags_unknown=bool(values["upload_tags_unknown"]),
                sidecar_warning=bool(values["sidecar_warning"]),
            )
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            raise PublicationStoreError("publication row failed public validation") from exc

    @staticmethod
    def _fetch(connection: sqlite3.Connection, key: PublicationKey) -> tuple[object, ...] | None:
        return connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM publications WHERE instance_url = ? AND recording_sha256 = ?",
            (key.instance_url, key.recording_sha256),
        ).fetchone()

    @staticmethod
    def _fetch_id(connection: sqlite3.Connection, job_id: str) -> tuple[object, ...] | None:
        return connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM publications WHERE job_id = ?", (job_id,)
        ).fetchone()

    def _fetch_ref(self, connection: sqlite3.Connection, reference: PublicationKey | PublicationJob | str) -> tuple[object, ...] | None:
        if isinstance(reference, PublicationKey):
            return self._fetch(connection, reference)
        if isinstance(reference, PublicationJob):
            return self._fetch_id(connection, reference.job_id)
        if isinstance(reference, str):
            return self._fetch_id(connection, reference)
        raise TypeError("publication reference must be a key, job, or job ID")

    def _write(self, callback: Callable[[sqlite3.Connection], object], *, mode: str = "IMMEDIATE") -> object:
        connection = self._connect()
        try:
            connection.execute(f"BEGIN {mode}")
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

    def _job(self, connection: sqlite3.Connection, reference: PublicationKey | str) -> PublicationJob:
        row = self._fetch_ref(connection, reference)
        if row is None:
            raise PublicationStoreError("publication job does not exist")
        return self._row_to_job(row)

    @staticmethod
    def _transition_error(message: str = "publication job is not in the expected state") -> PublicationTransitionError:
        return PublicationTransitionError(message)

    def get(self, reference: PublicationKey | PublicationJob | str) -> PublicationJob | None:
        """Read one durable job by key, stable ID, or job object."""
        connection = self._connect()
        try:
            row = self._fetch_ref(connection, reference)
            return None if row is None else self._row_to_job(row)
        finally:
            connection.close()

    def list(self, states: Sequence[PublicationState | str] | None = None) -> list[PublicationJob]:
        """List durable jobs, optionally filtered by their stored state."""
        connection = self._connect()
        try:
            if states is None:
                rows = connection.execute(f"SELECT {_SELECT_COLUMNS} FROM publications ORDER BY created_at_ms, job_id").fetchall()
            else:
                normalized = tuple(state.value if isinstance(state, PublicationState) else state for state in states)
                if not normalized:
                    return []
                placeholders = ", ".join("?" for _ in normalized)
                rows = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM publications WHERE state IN ({placeholders}) ORDER BY created_at_ms, job_id",
                    normalized,
                ).fetchall()
            return [self._row_to_job(row) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _cleanup_intent_row(connection: sqlite3.Connection, intent_id: str) -> CleanupIntent:
        row = connection.execute(
            "SELECT intent_id, expected_private_path, expected_recording_sha256, media_device, media_inode, "
            "media_size, media_mtime_ns, sidecar_device, sidecar_inode, sidecar_size, sidecar_mtime_ns, "
            "quarantine_media_basename, quarantine_sidecar_basename, phase, created_at_ms, updated_at_ms "
            ", media_nlink, sidecar_nlink "
            "FROM cleanup_intents WHERE intent_id = ?", (intent_id,),
        ).fetchone()
        if row is None:
            raise PublicationStoreError("cleanup intent does not exist")
        members = connection.execute(
            "SELECT job_id, lease_generation FROM cleanup_intent_members "
            "WHERE intent_id = ? ORDER BY job_id", (intent_id,),
        ).fetchall()
        return CleanupIntent(
            *row[:13],
            phase=CleanupPhase(row[13]), created_at_ms=row[14], updated_at_ms=row[15],
            claimed_job_ids=tuple(member[0] for member in members),
            claimed_lease_generations=tuple(member[1] for member in members),
            media_nlink=row[16], sidecar_nlink=row[17],
        )

    @staticmethod
    def _exact_cleanup_member_ids(connection: sqlite3.Connection, path: bytes) -> tuple[str, ...]:
        # Read one extra row so an oversized same-path group fails closed.
        rows = connection.execute(
            "SELECT job_id FROM publications WHERE private_path = ? ORDER BY job_id LIMIT ?",
            (path, _MAX_CLEANUP_GROUP + 1),
        ).fetchall()
        if len(rows) > _MAX_CLEANUP_GROUP:
            raise PublicationTransitionError("cleanup path group exceeds the bounded limit")
        return tuple(row[0] for row in rows)

    def list_cleanup_candidates(
        self,
        *,
        after_created_at_ms: int | None = None,
        after_job_id: str | None = None,
        limit: int = _MAX_CLEANUP_GROUP,
    ) -> list[PublicationJob]:
        """Enumerate published rows with private paths using a stable keyset page."""
        # Validate the page size and require both cursor fields together.
        if type(limit) is not int or not 1 <= limit <= _MAX_CLEANUP_GROUP:
            raise ValueError("cleanup candidate limit is invalid")
        if (after_created_at_ms is None) != (after_job_id is None):
            raise ValueError("cleanup candidate cursor is incomplete")

        # Open one connection and build the stable cursor parameters.
        connection = self._connect()
        try:
            params: tuple[object, ...]
            cursor_sql = ""
            if after_created_at_ms is not None and after_job_id is not None:
                after_created_at_ms = _validate_nonnegative_int(after_created_at_ms, "cleanup candidate cursor")
                if not isinstance(after_job_id, str):
                    raise ValueError("cleanup candidate cursor is invalid")
                cursor_sql = "AND (created_at_ms > ? OR (created_at_ms = ? AND job_id > ?)) "
                params = (after_created_at_ms, after_created_at_ms, after_job_id, limit)
            else:
                params = (limit,)

            # Select only published rows with private paths in deterministic key order.
            rows = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM publications WHERE state = 'published' "
                "AND private_path IS NOT NULL " + cursor_sql +
                "ORDER BY created_at_ms, job_id LIMIT ?", params,
            ).fetchall()

            # Decode the bounded rows before closing the connection.
            return [self._row_to_job(row) for row in rows]
        finally:
            connection.close()

    def list_cleanup_group(
        self,
        expected_private_path: bytes | str | os.PathLike[str],
        *,
        limit: int = _MAX_CLEANUP_GROUP,
    ) -> list[PublicationJob]:
        """Return every row currently using one exact private path."""
        # Validate the path and result bound before opening the database.
        path = _path_bytes(expected_private_path)
        if path is None or not path.startswith(b"/"):
            raise ValueError("cleanup path must be absolute")
        if type(limit) is not int or not 1 <= limit <= _MAX_CLEANUP_GROUP:
            raise ValueError("cleanup group limit is invalid")

        # Read the exact path group through one connection so its bound is consistent.
        connection = self._connect()
        try:
            member_ids = self._exact_cleanup_member_ids(connection, path)
            if len(member_ids) > limit:
                raise self._transition_error("cleanup path group exceeds the requested limit")

            # Decode the exact group while the connection still holds its snapshot.
            return [self._job(connection, job_id) for job_id in member_ids]
        finally:
            connection.close()

    def prepare_cleanup_intent(
        self,
        intent: CleanupIntent,
        references: Sequence[PublicationKey | PublicationJob | str] | None = None,
    ) -> CleanupIntent:
        """Durably register one explicit cleanup intent and its exact current group."""
        # Validate the intent phase and reference bound before entering the transaction.
        if not isinstance(intent, CleanupIntent):
            raise TypeError("cleanup intent is invalid")
        if intent.phase is not CleanupPhase.PREPARED:
            raise ValueError("new cleanup intents must be prepared")
        refs = tuple(references) if references is not None else tuple(intent.claimed_job_ids)
        if len(refs) > _MAX_CLEANUP_GROUP:
            raise ValueError("cleanup group is too large")

        def prepare(connection: sqlite3.Connection) -> CleanupIntent:
            # Insert the protected intent before resolving its exact member set.
            connection.execute(
                "INSERT INTO cleanup_intents (intent_id, expected_private_path, expected_recording_sha256, "
                "media_device, media_inode, media_size, media_mtime_ns, sidecar_device, sidecar_inode, "
                "sidecar_size, sidecar_mtime_ns, quarantine_media_basename, quarantine_sidecar_basename, "
                "phase, created_at_ms, updated_at_ms, media_nlink, sidecar_nlink) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (intent.intent_id, intent.expected_private_path, intent.expected_recording_sha256,
                 intent.media_device, intent.media_inode, intent.media_size, intent.media_mtime_ns,
                 intent.sidecar_device, intent.sidecar_inode, intent.sidecar_size, intent.sidecar_mtime_ns,
                 intent.quarantine_media_basename, intent.quarantine_sidecar_basename,
                 intent.phase.value, intent.created_at_ms, intent.updated_at_ms,
                 intent.media_nlink, intent.sidecar_nlink),
            )

            # Derive members from the path when callers supplied no references.
            selected_refs = refs
            if references is None and not selected_refs:
                # Derive an empty intent's members from its exact private path.
                selected_refs = self._exact_cleanup_member_ids(connection, intent.expected_private_path)
            if not selected_refs:
                raise self._transition_error("cleanup intent has no matching private-path members")
            proposed_ids_list: list[str] = []

            # Resolve references before checking exact group coverage.
            for reference in selected_refs:
                row = self._fetch_ref(connection, reference)
                if row is None:
                    raise self._transition_error("cleanup member does not exist")
                proposed_ids_list.append(self._row_to_job(row).job_id)
            proposed_ids = tuple(sorted(proposed_ids_list))
            exact_ids = self._exact_cleanup_member_ids(connection, intent.expected_private_path)
            if proposed_ids != exact_ids:
                raise self._transition_error("cleanup members do not cover the exact private-path group")
            selected: list[str] = []

            # Recheck publication state and install zero-generation members atomically.
            for reference in selected_refs:
                row = self._fetch_ref(connection, reference)
                if row is None:
                    raise self._transition_error("cleanup member does not exist")
                job = self._row_to_job(row)
                if (job.state is not PublicationState.PUBLISHED
                        or job.private_path != intent.expected_private_path
                        or job.key.recording_sha256 != intent.expected_recording_sha256
                        or job.lease_owner is not None or job.cleanup_lease_owner is not None):
                    raise self._transition_error("cleanup member no longer matches its intent")
                connection.execute(
                    "INSERT INTO cleanup_intent_members (intent_id, job_id, lease_generation) VALUES (?, ?, 0)",
                    (intent.intent_id, job.job_id),
                )
                selected.append(job.job_id)
            return self._cleanup_intent_row(connection, intent.intent_id)

        return self._write(prepare)  # type: ignore[return-value]

    def load_cleanup_intent(self, intent_id: str) -> CleanupIntent:
        """Load one intent with only successfully claimed member generations."""
        connection = self._connect()
        try:
            return self._cleanup_intent_row(connection, intent_id)
        finally:
            connection.close()

    def list_cleanup_intents(
        self, phase: CleanupPhase | None = None, *, limit: int = _MAX_CLEANUP_INTENTS,
    ) -> list[CleanupIntent]:
        """List bounded intents so an explicit cleanup command can resume after restart."""
        if phase is not None and not isinstance(phase, CleanupPhase):
            raise TypeError("cleanup phase is invalid")
        if type(limit) is not int or not 1 <= limit <= _MAX_CLEANUP_INTENTS:
            raise ValueError("cleanup intent limit is invalid")
        # Read only bounded intent IDs before decoding each protected row.
        connection = self._connect()
        try:
            if phase is None:
                rows = connection.execute(
                    "SELECT intent_id FROM cleanup_intents ORDER BY updated_at_ms, intent_id LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT intent_id FROM cleanup_intents WHERE phase = ? "
                    "ORDER BY updated_at_ms, intent_id LIMIT ?", (phase.value, limit)
                ).fetchall()
            return [self._cleanup_intent_row(connection, row[0]) for row in rows]
        finally:
            connection.close()

    def claim_cleanup_group(
        self, intent_id: str, owner: str, lease_ttl_ms: int, *, now_ms: int | None = None,
    ) -> CleanupClaim:
        """Claim the complete exact-path group without advancing its journal phase."""
        validated_owner, _ = self._owner_generation(owner, 1)
        if validated_owner is None or type(lease_ttl_ms) is not int or not 1 <= lease_ttl_ms <= 86_400_000:
            raise ValueError("cleanup lease is invalid")
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "cleanup clock")

        def claim(connection: sqlite3.Connection) -> CleanupClaim:
            # Recover stale claims by refusing a mixed or changed group rather than guessing.
            intent = self._cleanup_intent_row(connection, intent_id)
            if intent.phase not in _CLEANUP_PHASE_ORDER:
                raise self._transition_error("cleanup intent is not claimable")
            member_rows = connection.execute(
                "SELECT job_id FROM cleanup_intent_members WHERE intent_id = ? ORDER BY job_id", (intent_id,)
            ).fetchall()
            if not member_rows:
                raise self._transition_error("cleanup intent has no members")
            exact_ids = self._exact_cleanup_member_ids(connection, intent.expected_private_path)
            member_ids = tuple(row[0] for row in member_rows)
            if member_ids != exact_ids:
                raise self._transition_error("cleanup group membership changed")
            jobs: list[PublicationJob] = []
            # Validate every member before changing any lease generation.
            for (job_id,) in member_rows:
                row = self._fetch_id(connection, job_id)
                if row is None:
                    raise self._transition_error("cleanup member disappeared")
                job = self._row_to_job(row)
                if (job.state is not PublicationState.PUBLISHED or job.private_path != intent.expected_private_path
                        or job.key.recording_sha256 != intent.expected_recording_sha256
                        or job.lease_owner is not None
                        or (job.cleanup_lease_owner is not None
                            and (job.cleanup_lease_expires_at_ms or 0) > now)):
                    raise self._transition_error("cleanup group is no longer claimable")
                jobs.append(job)
            # Fence the complete group with one owner and one new generation per job.
            for job in jobs:
                generation = job.cleanup_lease_generation + 1
                connection.execute(
                    "UPDATE publications SET cleanup_lease_owner = ?, cleanup_lease_generation = ?, "
                    "cleanup_lease_expires_at_ms = ?, updated_at_ms = ? WHERE job_id = ?",
                    (validated_owner, generation, now + lease_ttl_ms, max(now, job.created_at_ms), job.job_id),
                )
                connection.execute(
                    "UPDATE cleanup_intent_members SET lease_generation = ? WHERE intent_id = ? AND job_id = ?",
                    (generation, intent_id, job.job_id),
                )
            generations = tuple(job.cleanup_lease_generation + 1 for job in jobs)
            return CleanupClaim(intent_id, validated_owner, member_ids, generations)

        return self._write(claim)  # type: ignore[return-value]

    def _cleanup_claim_jobs(
        self,
        connection: sqlite3.Connection,
        intent: CleanupIntent,
        claim: CleanupClaim,
        now: int,
    ) -> list[PublicationJob]:
        # Compare the caller-held immutable fence with both durable membership and every job row.
        if claim.intent_id != intent.intent_id or tuple(intent.claimed_job_ids) != claim.job_ids:
            raise self._transition_error("cleanup claim membership is stale")
        if tuple(intent.claimed_lease_generations) != claim.lease_generations:
            raise self._transition_error("cleanup claim generation is stale")
        if claim.job_ids != self._exact_cleanup_member_ids(connection, intent.expected_private_path):
            raise self._transition_error("cleanup group membership changed")
        jobs = [self._job(connection, job_id) for job_id in claim.job_ids]
        # Confirm every durable fence before allowing a caller-owned mutation.
        for job, generation in zip(jobs, claim.lease_generations):
            if (
                job.state is not PublicationState.PUBLISHED
                or job.private_path != intent.expected_private_path
                or job.key.recording_sha256 != intent.expected_recording_sha256
                or job.lease_owner is not None
                or job.cleanup_lease_owner != claim.owner
                or job.cleanup_lease_generation != generation
                or job.cleanup_lease_expires_at_ms is None
                or job.cleanup_lease_expires_at_ms <= now
            ):
                raise self._transition_error("cleanup claim is stale")
        return jobs

    def advance_cleanup_intent(
        self,
        intent_id: str,
        claim: CleanupClaim,
        expected_phase: CleanupPhase,
        next_phase: CleanupPhase,
        *,
        now_ms: int | None = None,
    ) -> CleanupIntent:
        """Record exactly one fenced namespace-mutation checkpoint."""
        if not isinstance(claim, CleanupClaim) or not isinstance(expected_phase, CleanupPhase) or not isinstance(next_phase, CleanupPhase):
            raise ValueError("cleanup claim or phase is invalid")
        try:
            is_next = _CLEANUP_PHASE_ORDER[_CLEANUP_PHASE_ORDER.index(expected_phase) + 1] is next_phase
        except IndexError:
            is_next = False
        if not is_next:
            raise ValueError("cleanup phase must advance by one step")
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "cleanup clock")

        def advance(connection: sqlite3.Connection) -> CleanupIntent:
            # Require the complete exact-path group and every current cleanup fence before recording progress.
            intent = self._cleanup_intent_row(connection, intent_id)
            if intent.phase is not expected_phase:
                raise self._transition_error("cleanup intent phase or claim is stale")
            self._cleanup_claim_jobs(connection, intent, claim, now)
            connection.execute(
                "UPDATE cleanup_intents SET phase = ?, updated_at_ms = ? WHERE intent_id = ? AND phase = ?",
                (next_phase.value, now, intent_id, expected_phase.value),
            )
            return self._cleanup_intent_row(connection, intent_id)

        return self._write(advance)  # type: ignore[return-value]

    def renew_cleanup_group(
        self, intent_id: str, claim: CleanupClaim, lease_ttl_ms: int, *, now_ms: int | None = None,
    ) -> CleanupClaim:
        """Extend every cleanup lease only when the whole group fence is current."""
        if not isinstance(claim, CleanupClaim) or type(lease_ttl_ms) is not int or not 1 <= lease_ttl_ms <= 86_400_000:
            raise ValueError("cleanup lease is invalid")
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "cleanup clock")

        def renew(connection: sqlite3.Connection) -> CleanupClaim:
            # Validate the current claim before extending any member lease.
            intent = self._cleanup_intent_row(connection, intent_id)
            if intent.phase not in _CLEANUP_PHASE_ORDER:
                raise self._transition_error("cleanup intent is not actively claimed")
            jobs = self._cleanup_claim_jobs(connection, intent, claim, now)
            # Extend every member together so a group cannot become partially live.
            for job in jobs:
                connection.execute(
                    "UPDATE publications SET cleanup_lease_expires_at_ms = ?, updated_at_ms = ? "
                    "WHERE job_id = ? AND cleanup_lease_owner = ? AND cleanup_lease_generation = ?",
                    (now + lease_ttl_ms, max(now, job.created_at_ms), job.job_id, claim.owner, job.cleanup_lease_generation),
                )
            connection.execute("UPDATE cleanup_intents SET updated_at_ms = ? WHERE intent_id = ?", (now, intent_id))
            return claim

        return self._write(renew)  # type: ignore[return-value]

    def release_cleanup_group(
        self, intent_id: str, claim: CleanupClaim, *, now_ms: int | None = None,
    ) -> CleanupIntent:
        """Release a current cleanup claim without changing publication state."""
        if not isinstance(claim, CleanupClaim):
            raise ValueError("cleanup claim is invalid")
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "cleanup clock")

        def release(connection: sqlite3.Connection) -> CleanupIntent:
            # Require the prepared phase before releasing an untouched group.
            intent = self._cleanup_intent_row(connection, intent_id)
            if intent.phase is not CleanupPhase.PREPARED:
                raise self._transition_error("cleanup intent is not actively claimed")
            jobs = self._cleanup_claim_jobs(connection, intent, claim, now)
            # Clear every member lease and reset its intent generation in one transaction.
            for job in jobs:
                connection.execute(
                    "UPDATE publications SET cleanup_lease_owner = NULL, cleanup_lease_expires_at_ms = NULL, "
                    "updated_at_ms = ? WHERE job_id = ? AND cleanup_lease_owner = ? AND cleanup_lease_generation = ?",
                    (max(now, job.created_at_ms), job.job_id, claim.owner, job.cleanup_lease_generation),
                )
            connection.execute(
                "UPDATE cleanup_intent_members SET lease_generation = 0 WHERE intent_id = ?", (intent_id,)
            )
            connection.execute(
                "UPDATE cleanup_intents SET updated_at_ms = ? WHERE intent_id = ?", (now, intent_id)
            )
            return self._cleanup_intent_row(connection, intent_id)

        return self._write(release)  # type: ignore[return-value]

    def complete_cleanup_group(
        self, intent_id: str, claim: CleanupClaim, *, removed_at_ms: int | None = None,
    ) -> list[PublicationJob]:
        """Atomically complete only a fully fenced MEDIA_UNLINKED group."""
        if not isinstance(claim, CleanupClaim):
            raise ValueError("cleanup claim is invalid")
        removed_at = self._time_ms() if removed_at_ms is None else _validate_nonnegative_int(removed_at_ms, "cleanup clock")

        def complete(connection: sqlite3.Connection) -> list[PublicationJob]:
            # Require the final durable phase and current claim before changing publication state.
            intent = self._cleanup_intent_row(connection, intent_id)
            if intent.phase is not CleanupPhase.MEDIA_UNLINKED:
                raise self._transition_error("cleanup intent is not actively claimed")
            jobs = self._cleanup_claim_jobs(connection, intent, claim, removed_at)
            if any(job.cleanup_lease_expires_at_ms is None or job.cleanup_lease_expires_at_ms <= removed_at for job in jobs):
                raise self._transition_error("cleanup lease is stale, expired, or state changed")
            for job in jobs:
                if (job.private_path != intent.expected_private_path
                        or job.key.recording_sha256 != intent.expected_recording_sha256
                        or job.lease_owner is not None):
                    raise self._transition_error("cleanup member changed")
            completed: list[PublicationJob] = []
            # Mark every exact group member local-removed before deleting the intent record.
            for job in jobs:
                new = replace(
                    job, state=PublicationState.LOCAL_REMOVED,
                    operation=PublicationOperation.NONE.value, resume_intent=ResumeIntent.NONE.value,
                    private_path=None, reconciliation_token=None, lease_owner=None,
                    lease_expires_at_ms=None, cleanup_lease_owner=None,
                    cleanup_lease_generation=0, cleanup_lease_expires_at_ms=None,
                    next_attempt_at_ms=0, last_error_code=None, last_http_status=None,
                    local_removed_at_ms=max(removed_at, job.published_at_ms or removed_at),
                    updated_at_ms=max(removed_at, job.created_at_ms),
                )
                completed.append(self._update_job(connection, job, new))
            connection.execute("DELETE FROM cleanup_intents WHERE intent_id = ?", (intent_id,))
            return completed

        return self._write(complete)  # type: ignore[return-value]

    def abort_cleanup_intent(
        self, intent_id: str, claim: CleanupClaim | None = None, *, now_ms: int | None = None,
    ) -> CleanupIntent:
        """Delete a prepared intent and release its group atomically before mutation."""
        if claim is not None and not isinstance(claim, CleanupClaim):
            raise ValueError("cleanup claim is invalid")
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "cleanup clock")

        def abort(connection: sqlite3.Connection) -> CleanupIntent:
            # A post-quarantine journal is never rewound or implicitly released.
            intent = self._cleanup_intent_row(connection, intent_id)
            if intent.phase is not CleanupPhase.PREPARED:
                raise self._transition_error("mutated cleanup intents cannot be aborted")
            exact_ids = self._exact_cleanup_member_ids(connection, intent.expected_private_path)
            if tuple(intent.claimed_job_ids) != exact_ids:
                raise self._transition_error("cleanup group membership changed")
            jobs = [self._job(connection, job_id) for job_id in intent.claimed_job_ids]
            if claim is None:
                if any(generation != 0 or job.cleanup_lease_owner is not None for job, generation in zip(jobs, intent.claimed_lease_generations)):
                    raise self._transition_error("cleanup claim is required to release leases")
            else:
                self._cleanup_claim_jobs(connection, intent, claim, now)
            # Release only the caller's current leases before deleting the prepared intent.
            for job in jobs:
                if claim is not None and job.cleanup_lease_owner == claim.owner:
                    connection.execute(
                        "UPDATE publications SET cleanup_lease_owner = NULL, cleanup_lease_expires_at_ms = NULL, "
                        "updated_at_ms = ? WHERE job_id = ? AND cleanup_lease_owner = ?",
                        (max(now, job.created_at_ms), job.job_id, claim.owner),
                    )
            connection.execute("DELETE FROM cleanup_intents WHERE intent_id = ?", (intent_id,))
            return intent

        return self._write(abort)  # type: ignore[return-value]

    @staticmethod
    def _due_predicate() -> str:
        """Return the SQL safety predicate shared by due IDs and wake times."""
        return (
            "(cleanup_lease_owner IS NULL AND ((state = 'queued' AND operation = 'post' AND resume_intent = 'post' "
            "AND remote_recording_id IS NULL) "
            "OR (state = 'metadata_pending' AND operation = 'patch' AND resume_intent = 'patch' "
            "AND remote_recording_id IS NOT NULL) "
            "OR (state = 'uncertain' AND operation = 'reconcile' AND resume_intent = 'reconcile' "
            "AND remote_recording_id IS NULL) "
            "OR (state = 'transferring' AND operation = 'none' AND resume_intent = 'post' "
            "AND remote_recording_id IS NULL)))"
        )

    @staticmethod
    def _effective_due_sql() -> str:
        """Return the deadline expression that protects active leases."""
        return (
            "CASE WHEN lease_owner IS NOT NULL THEN lease_expires_at_ms "
            "ELSE COALESCE(next_attempt_at_ms, 0) END"
        )

    def due_job_ids(
        self,
        instance_url: str,
        *,
        now_ms: int | None = None,
        limit: int = 100,
    ) -> tuple[str, ...]:
        """Return a bounded, origin-filtered snapshot of due safe work IDs."""
        if type(limit) is not int or not 1 <= limit <= _MAX_DUE_IDS:
            raise ValueError("due limit is invalid")
        origin = normalize_speakr_url(instance_url)
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "publication clock")
        effective_due = self._effective_due_sql()

        # Select only rows whose operation and deadline permit a safe claim.
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT job_id FROM publications WHERE instance_url = ? "
                f"AND {self._due_predicate()} "
                "AND (((state != 'transferring') AND lease_owner IS NULL "
                "AND (next_attempt_at_ms IS NULL OR next_attempt_at_ms <= ?)) "
                "OR (lease_owner IS NOT NULL AND lease_expires_at_ms IS NOT NULL "
                "AND lease_expires_at_ms <= ?)) "
                f"ORDER BY {effective_due}, job_id LIMIT ?",
                (origin, now, now, limit),
            ).fetchall()
            return tuple(row[0] for row in rows)
        finally:
            connection.close()

    def next_wake_at_ms(self, instance_url: str, *, now_ms: int | None = None) -> int | None:
        """Return the earliest eligible deadline, clamped to the supplied clock."""
        origin = normalize_speakr_url(instance_url)
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "publication clock")
        effective_due = self._effective_due_sql()

        # Clamp overdue work to now so callers never sleep past immediately runnable work.
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT MIN(CASE WHEN deadline <= ? THEN ? ELSE deadline END) FROM ("
                f"SELECT {effective_due} AS deadline FROM publications WHERE instance_url = ? "
                f"AND {self._due_predicate()} "
                "AND (((state != 'transferring') AND lease_owner IS NULL) "
                "OR (lease_owner IS NOT NULL AND lease_expires_at_ms IS NOT NULL))"
                ")",
                (now, now, origin),
            ).fetchone()
            return None if row is None or row[0] is None else int(row[0])
        finally:
            connection.close()

    def create_or_reuse(
        self,
        key: PublicationKey,
        private_path: bytes | str | os.PathLike[str] | MediaIdentity,
        file_last_modified_ms: int = 0,
        *,
        identity: MediaIdentity | None = None,
        job_id: str | None = None,
        tags: tuple[Tag, ...] = (),
    ) -> PublicationJob:
        """Atomically create a queued job or reuse its normalized URL/SHA identity."""
        if not isinstance(key, PublicationKey):
            raise TypeError("create_or_reuse requires a PublicationKey")
        if isinstance(private_path, MediaIdentity):
            identity = _validate_identity(private_path)
            private_path = private_path.path
        identity = _validate_identity(identity)
        path = _path_bytes(private_path)
        if not isinstance(tags, tuple) or len(tags) > _MAX_TAGS or any(not isinstance(tag, Tag) for tag in tags):
            raise ValueError("tags must be tag values")
        if len({tag.tag_id for tag in tags}) != len(tags):
            raise ValueError("tags contain duplicate tag IDs")
        file_last_modified_ms = _validate_nonnegative_int(file_last_modified_ms, "file last modified time")
        if job_id is None:
            job_id = uuid4().hex
        if not isinstance(job_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job_id) is None:
            raise ValueError("publication job ID is invalid")
        now = self._time_ms()
        media = identity or MediaIdentity(Path(os.fsdecode(path)), 0, 0, 0, 0)
        values = (
            job_id, key.instance_url, key.recording_sha256, path,
            media.device, media.inode, media.size, media.mtime_ns, file_last_modified_ms,
            PublicationState.QUEUED.value, PublicationOperation.POST.value, ResumeIntent.POST.value,
            None, None, 0, now, None, 0, None, None, None,
            None, None, None, None, None, None, None, now, now,
            None, 0, None,
            _encode_tags(tags), _encode_tags(None), _encode_tags(None), 0, 0,
        )

        def insert(connection: sqlite3.Connection) -> PublicationJob:
            connection.execute(
                f"INSERT OR IGNORE INTO publications ({_SELECT_COLUMNS}) VALUES ({', '.join('?' for _ in _COLUMN_NAMES)})",
                values,
            )
            return self._job(connection, key)

        return self._write(insert)  # type: ignore[return-value]

    def create(self, key: PublicationKey, private_path: bytes | str | os.PathLike[str], **kwargs: object) -> PublicationJob:
        """Create or reuse a job; this is the concise integration-facing spelling."""
        return self.create_or_reuse(key, private_path, **kwargs)  # type: ignore[arg-type]

    def update_tag_status(
        self, reference: PublicationKey | PublicationJob | str, owner: str, generation: int, *, effective_tags: tuple[Tag, ...] | None = None,
        missing_tags: tuple[Tag, ...] | None = None, upload_tags_unknown: bool | None = None,
        sidecar_warning: bool | None = None,
        now_ms: int | None = None,
    ) -> PublicationJob:
        """Persist publisher tag outcomes behind the normal lease and generation fence."""
        for name, tags in (("effective tags", effective_tags), ("missing tags", missing_tags)):
            if tags is not None and (not isinstance(tags, tuple) or len(tags) > _MAX_TAGS or any(not isinstance(tag, Tag) for tag in tags)):
                raise ValueError(f"{name} must be tag values")
        if upload_tags_unknown is not None and type(upload_tags_unknown) is not bool:
            raise ValueError("upload tag outcome must be bool")
        if sidecar_warning is not None and type(sidecar_warning) is not bool:
            raise ValueError("sidecar warning must be bool")
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "publication clock")

        def update(connection: sqlite3.Connection) -> PublicationJob:
            old = self._job_from_reference(connection, reference)
            self._fence(old, owner, generation, now)
            # Keep ordinary tag outcomes limited to the pre-upload queued phase.
            if old.state is not PublicationState.QUEUED:
                raise self._transition_error("tag status can only be recorded before upload")
            # Reject partial or contradictory outcome shapes before writing them.
            if (effective_tags is None) != (missing_tags is None) and not upload_tags_unknown:
                raise ValueError("known tag outcomes require both tag sets")
            if upload_tags_unknown and (effective_tags is not None or missing_tags is not None):
                raise ValueError("unknown tag outcomes cannot contain exact tag sets")
            # Preserve unchanged status fields while applying the caller's complete outcome.
            new = replace(
                old, effective_tags=(None if upload_tags_unknown else old.effective_tags if effective_tags is None else effective_tags),
                missing_tags=(None if upload_tags_unknown else old.missing_tags if missing_tags is None else missing_tags),
                upload_tags_unknown=old.upload_tags_unknown if upload_tags_unknown is None else upload_tags_unknown,
                sidecar_warning=old.sidecar_warning if sidecar_warning is None else sidecar_warning,
                updated_at_ms=max(now, old.created_at_ms),
            )
            return self._update_job(connection, old, new)

        return self._write(update)  # type: ignore[return-value]

    def resolve_reconciled_tags(
        self, reference: PublicationKey | PublicationJob | str, owner: str, generation: int, *,
        now_ms: int | None = None,
    ) -> PublicationJob:
        """Record that a uniquely reconciled POST accepted its submitted frozen tags."""
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "publication clock")

        def update(connection: sqlite3.Connection) -> PublicationJob:
            # Keep this recovery-only mutation behind the active reconciliation lease.
            old = self._job_from_reference(connection, reference)
            self._fence(old, owner, generation, now)
            # Limit recovery to an actively fenced, reconciliation-eligible uncertain job.
            if old.state is not PublicationState.UNCERTAIN or not old.reconciliation_eligible:
                raise self._transition_error("reconciled tag resolution requires active uncertainty")
            # Preserve known filtering, but resolve an unknown request as its full submitted snapshot.
            if old.upload_tags_unknown:
                new = replace(
                    old, effective_tags=old.frozen_tags, missing_tags=(), upload_tags_unknown=False,
                    updated_at_ms=max(now, old.created_at_ms),
                )
            else:
                new = replace(old, updated_at_ms=max(now, old.created_at_ms))
            return self._update_job(connection, old, new)

        return self._write(update)  # type: ignore[return-value]

    @staticmethod
    def _owner_generation(owner: str | None, generation: int | None) -> tuple[str | None, int | None]:
        if owner is None and generation is None:
            return None, None
        if not isinstance(owner, str) or _SAFE_OWNER.fullmatch(owner) is None:
            raise ValueError("lease owner is invalid")
        if type(generation) is not int or generation < 1:
            raise ValueError("lease generation is invalid")
        return owner, generation

    def _fence(self, job: PublicationJob, owner: str | None, generation: int | None, now: int) -> None:
        if job.cleanup_lease_owner is not None:
            raise self._transition_error("cleanup lease is active")
        owner, generation = self._owner_generation(owner, generation)
        if owner is None:
            if job.lease_owner is not None:
                raise self._transition_error("lease owner and generation are required")
            return
        if job.lease_owner != owner or job.lease_generation != generation:
            raise self._transition_error("publication lease is stale")
        if job.lease_expires_at_ms is None or job.lease_expires_at_ms <= now:
            raise self._transition_error("publication lease has expired")

    def _update_job(self, connection: sqlite3.Connection, old: PublicationJob, new: PublicationJob) -> PublicationJob:
        values_by_column = {
            "job_id": new.job_id,
            "instance_url": new.key.instance_url,
            "recording_sha256": new.key.recording_sha256,
        }
        values_by_column.update({
            name: getattr(new, name) for name in _COLUMN_NAMES[3:]
        })
        # Serialize immutable tag tuples at the SQLite boundary.
        for name in ("frozen_tags", "effective_tags", "missing_tags"):
            values_by_column[name] = _encode_tags(getattr(new, name))
        values = tuple(values_by_column[name] for name in _COLUMN_NAMES)
        cursor = connection.execute(
            f"UPDATE publications SET {', '.join(f'{name} = ?' for name in _COLUMN_NAMES)} WHERE job_id = ?",
            values + (old.job_id,),
        )
        if cursor.rowcount != 1:
            raise self._transition_error("publication job disappeared during update")
        return self._job(connection, old.job_id)

    def transition(
        self,
        reference: PublicationKey | PublicationJob | str,
        state: PublicationState,
        *,
        owner: str | None = None,
        generation: int | None = None,
        expected_state: PublicationState | None = None,
        remote_recording_id: int | None = None,
        error_code: str | None = None,
        http_status: int | None = None,
        reconciliation_token: str | None = None,
        private_path: bytes | str | os.PathLike[str] | None = None,
        operation: str | PublicationOperation | None = None,
        now_ms: int | None = None,
    ) -> PublicationJob:
        """Apply one fenced state transition without performing external work."""
        if not isinstance(state, PublicationState):
            raise TypeError("transition state is invalid")
        if expected_state is not None and not isinstance(expected_state, PublicationState):
            raise TypeError("expected state is invalid")
        if remote_recording_id is not None and (type(remote_recording_id) is not int or remote_recording_id <= 0):
            raise ValueError("remote recording ID must be a positive integer")
        if error_code is not None:
            _validate_error_code(error_code)
        if http_status is not None:
            _validate_http_status(http_status)
        requested_operation = None if operation is None else (
            operation.value if isinstance(operation, PublicationOperation) else operation
        )
        if requested_operation is not None and requested_operation not in {
            item.value for item in PublicationOperation
        }:
            raise ValueError("publication operation is invalid")
        path = None if private_path is None else _path_bytes(private_path)
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "publication clock")

        def apply(connection: sqlite3.Connection) -> PublicationJob:
            row = self._fetch_ref(connection, reference)
            if row is None:
                raise self._transition_error("publication job does not exist")
            old = self._row_to_job(row)
            # Destructive local cleanup belongs to issue #25, not publication storage.
            if state is PublicationState.LOCAL_REMOVED:
                raise self._transition_error("local_removed transitions belong to cleanup")
            event_now = max(now, old.updated_at_ms)
            self._fence(old, owner, generation, now)
            if expected_state is not None and old.state is not expected_state:
                raise self._transition_error("publication job is not in the expected state")
            allowed = {
                PublicationState.QUEUED: {PublicationState.TRANSFERRING, PublicationState.METADATA_PENDING, PublicationState.UNCERTAIN, PublicationState.BLOCKED, PublicationState.MISSING},
                PublicationState.TRANSFERRING: {PublicationState.METADATA_PENDING, PublicationState.UNCERTAIN, PublicationState.BLOCKED, PublicationState.MISSING},
                PublicationState.METADATA_PENDING: {PublicationState.PUBLISHED, PublicationState.BLOCKED, PublicationState.MISSING},
                PublicationState.PUBLISHED: set(),
                PublicationState.UNCERTAIN: {PublicationState.METADATA_PENDING, PublicationState.BLOCKED, PublicationState.MISSING},
                PublicationState.BLOCKED: set(),
                PublicationState.MISSING: set(),
                PublicationState.LOCAL_REMOVED: set(),
            }
            if old.state in {PublicationState.BLOCKED, PublicationState.MISSING} and state is not old.state:
                resume_target = {
                    ResumeIntent.POST.value: PublicationState.QUEUED,
                    ResumeIntent.RECONCILE.value: PublicationState.UNCERTAIN,
                    ResumeIntent.PATCH.value: PublicationState.METADATA_PENDING,
                }.get(old.resume_intent)
                if state is not resume_target:
                    raise self._transition_error("state transition does not match the saved resume intent")
            if state is not old.state and state not in allowed[old.state]:
                raise self._transition_error("publication state transition is not allowed")
            if state is PublicationState.QUEUED and old.remote_recording_id is not None:
                raise self._transition_error("known remote jobs cannot be queued for POST")
            remote = old.remote_recording_id if remote_recording_id is None else remote_recording_id
            if state in {PublicationState.METADATA_PENDING, PublicationState.PUBLISHED} and remote is None:
                raise ValueError("known-record states require a remote recording ID")
            if state in {PublicationState.QUEUED, PublicationState.TRANSFERRING, PublicationState.UNCERTAIN}:
                remote = None
            next_token = reconciliation_token or old.reconciliation_token
            if state is PublicationState.QUEUED:
                next_token = None
            if state in {PublicationState.TRANSFERRING, PublicationState.UNCERTAIN} and next_token is None:
                next_token = uuid4().hex
            next_error_code = error_code
            next_http_status = http_status
            if state is PublicationState.UNCERTAIN and old.state is PublicationState.TRANSFERRING and next_error_code is None:
                next_error_code = "lease_expired"
            if state in {PublicationState.PUBLISHED, PublicationState.METADATA_PENDING} and next_error_code is None:
                next_http_status = None
            # Terminal local states retain the phase that an operator may resume later.
            if state in {PublicationState.BLOCKED, PublicationState.MISSING}:
                operation = PublicationOperation.NONE.value
                intent = old.resume_intent
            elif state is PublicationState.QUEUED:
                operation = PublicationOperation.POST.value
                intent = ResumeIntent.POST.value
            elif state is PublicationState.TRANSFERRING:
                operation = PublicationOperation.NONE.value
                intent = ResumeIntent.POST.value
            elif state is PublicationState.METADATA_PENDING or state is PublicationState.PUBLISHED:
                operation = PublicationOperation.PATCH.value if state is PublicationState.METADATA_PENDING else PublicationOperation.NONE.value
                intent = ResumeIntent.PATCH.value if state is PublicationState.METADATA_PENDING else ResumeIntent.NONE.value
            else:
                # A conclusive ambiguous result is terminal until an operator retries it.
                operation = requested_operation or PublicationOperation.RECONCILE.value
                if operation not in {PublicationOperation.NONE.value, PublicationOperation.RECONCILE.value}:
                    raise self._transition_error("uncertain jobs require reconciliation or terminal operation")
                intent = ResumeIntent.RECONCILE.value
            attempt = old.attempt_count
            timestamps = {
                "transfer_started_at_ms": old.transfer_started_at_ms,
                "accepted_at_ms": old.accepted_at_ms,
                "published_at_ms": old.published_at_ms,
                "uncertain_at_ms": old.uncertain_at_ms,
                "blocked_at_ms": old.blocked_at_ms,
                "missing_at_ms": old.missing_at_ms,
                "local_removed_at_ms": old.local_removed_at_ms,
            }
            if state is PublicationState.TRANSFERRING:
                timestamps["transfer_started_at_ms"] = event_now
            if state is PublicationState.METADATA_PENDING and old.accepted_at_ms is None:
                timestamps["accepted_at_ms"] = event_now
            if state is PublicationState.PUBLISHED:
                timestamps["published_at_ms"] = event_now
            if state is PublicationState.UNCERTAIN:
                timestamps["uncertain_at_ms"] = event_now
            if state is PublicationState.BLOCKED:
                timestamps["blocked_at_ms"] = event_now
            if state is PublicationState.MISSING:
                timestamps["missing_at_ms"] = event_now
            next_private_path = old.private_path if path is None else path
            new = replace(
                old, state=state, operation=operation, resume_intent=intent,
                private_path=next_private_path,
                reconciliation_token=next_token, remote_recording_id=remote,
                attempt_count=attempt, last_error_code=next_error_code,
                last_http_status=next_http_status, lease_owner=(old.lease_owner if state is PublicationState.TRANSFERRING else None),
                lease_expires_at_ms=(old.lease_expires_at_ms if state is PublicationState.TRANSFERRING else None),
                transfer_started_at_ms=timestamps["transfer_started_at_ms"],
                accepted_at_ms=timestamps["accepted_at_ms"],
                published_at_ms=timestamps["published_at_ms"],
                uncertain_at_ms=timestamps["uncertain_at_ms"],
                blocked_at_ms=timestamps["blocked_at_ms"],
                missing_at_ms=timestamps["missing_at_ms"],
                local_removed_at_ms=timestamps["local_removed_at_ms"],
                updated_at_ms=event_now,
            )
            return self._update_job(connection, old, new)

        return self._write(apply)  # type: ignore[return-value]

    def claim_one(
        self,
        worker_id: str,
        reference: PublicationKey | PublicationJob | str | None = None,
        *,
        lease_ms: int = _DEFAULT_LEASE_MS,
        now_ms: int | None = None,
    ) -> PublicationJob | None:
        """Claim one eligible job in an IMMEDIATE transaction with a new lease generation."""
        validated_worker, _ = self._owner_generation(worker_id, 1)
        if validated_worker is None:
            raise AssertionError("validated worker unexpectedly missing")
        worker_id = validated_worker
        if type(lease_ms) is not int or not 1 <= lease_ms <= 86_400_000:
            raise ValueError("lease duration is invalid")
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "publication clock")

        def claim(connection: sqlite3.Connection) -> PublicationJob | None:
            self._recover_expired(connection, now)
            if reference is None:
                row = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM publications "
                    "WHERE ((state = 'queued' AND operation = 'post' AND resume_intent = 'post' AND remote_recording_id IS NULL) "
                    "OR (state = 'metadata_pending' AND operation = 'patch' AND resume_intent = 'patch' AND remote_recording_id IS NOT NULL) "
                    "OR (state = 'uncertain' AND operation = 'reconcile' AND resume_intent = 'reconcile' AND remote_recording_id IS NULL)) "
                    "AND lease_owner IS NULL AND cleanup_lease_owner IS NULL AND next_attempt_at_ms <= ? "
                    "ORDER BY next_attempt_at_ms, created_at_ms, job_id LIMIT 1", (now,),
                ).fetchone()
            else:
                row = self._fetch_ref(connection, reference)
                if row is not None:
                    candidate = self._row_to_job(row)
                    eligible = (
                        candidate.http_method in {"POST", "PATCH"}
                        or candidate.reconciliation_eligible
                    )
                    if (not eligible or candidate.lease_owner is not None
                            or candidate.cleanup_lease_owner is not None
                            or candidate.next_attempt_at_ms > now):
                        row = None
            if row is None:
                return None
            old = self._row_to_job(row)
            new = replace(
                old, attempt_count=old.attempt_count + 1, lease_owner=worker_id,
                lease_generation=old.lease_generation + 1,
                lease_expires_at_ms=now + lease_ms, updated_at_ms=max(now, old.created_at_ms),
            )
            return self._update_job(connection, old, new)

        return self._write(claim)  # type: ignore[return-value]

    def claim_for_action(
        self,
        reference: PublicationKey | PublicationJob | str,
        worker_id: str,
        lease_ttl_ms: int,
        *,
        instance_url: str | None = None,
    ) -> PublicationJob | None:
        """Claim one referenced eligible job regardless of its due deadline.

        Expired leases are recovered first. An unexpired lease, ineligible
        operation, or origin mismatch leaves the job untouched.
        """
        # Validate the worker fence and bounded lease before opening a transaction.
        validated_worker, _ = self._owner_generation(worker_id, 1)
        if validated_worker is None:
            raise AssertionError("validated worker unexpectedly missing")
        if type(lease_ttl_ms) is not int or not 1 <= lease_ttl_ms <= 86_400_000:
            raise ValueError("lease duration is invalid")
        # Normalize an optional origin so credentials cannot cross instance boundaries.
        origin = None if instance_url is None else normalize_speakr_url(instance_url)
        now = self._time_ms()

        def claim(connection: sqlite3.Connection) -> PublicationJob | None:
            # Recover expired work before evaluating the referenced row.
            self._recover_expired(connection, now)
            row = self._fetch_ref(connection, reference)
            if row is None:
                return None
            candidate = self._row_to_job(row)

            # Operator actions still honor origin, operation, and live lease rules.
            if origin is not None and candidate.key.instance_url != origin:
                return None
            eligible = candidate.http_method in {"POST", "PATCH"} or candidate.reconciliation_eligible
            if (not eligible or candidate.lease_owner is not None
                    or candidate.cleanup_lease_owner is not None):
                return None
            # Increment the informational attempt and fence the new lease generation.
            new = replace(
                candidate, attempt_count=candidate.attempt_count + 1,
                lease_owner=validated_worker,
                lease_generation=candidate.lease_generation + 1,
                lease_expires_at_ms=now + lease_ttl_ms,
                updated_at_ms=max(now, candidate.created_at_ms),
            )
            return self._update_job(connection, candidate, new)

        return self._write(claim)  # type: ignore[return-value]

    def claim_due(
        self, worker_id: str, *, limit: int = 1, lease_ms: int = _DEFAULT_LEASE_MS, now_ms: int | None = None,
    ) -> List[PublicationJob]:
        """Claim a bounded due batch atomically without holding a command lock."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("claim limit is invalid")
        validated_worker, _ = self._owner_generation(worker_id, 1)
        if validated_worker is None:
            raise AssertionError("validated worker unexpectedly missing")
        worker_id = validated_worker
        if type(lease_ms) is not int or not 1 <= lease_ms <= 86_400_000:
            raise ValueError("lease duration is invalid")
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "publication clock")

        def claim(connection: sqlite3.Connection) -> list[PublicationJob]:
            self._recover_expired(connection, now)
            rows = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM publications "
                "WHERE ((state = 'queued' AND operation = 'post' AND resume_intent = 'post' AND remote_recording_id IS NULL) "
                "OR (state = 'metadata_pending' AND operation = 'patch' AND resume_intent = 'patch' AND remote_recording_id IS NOT NULL) "
                "OR (state = 'uncertain' AND operation = 'reconcile' AND resume_intent = 'reconcile' AND remote_recording_id IS NULL)) "
                "AND lease_owner IS NULL AND cleanup_lease_owner IS NULL AND next_attempt_at_ms <= ? "
                "ORDER BY next_attempt_at_ms, created_at_ms, job_id LIMIT ?", (now, limit),
            ).fetchall()
            jobs: List[PublicationJob] = []
            for row in rows:
                old = self._row_to_job(row)
                new = replace(
                    old, attempt_count=old.attempt_count + 1, lease_owner=worker_id,
                    lease_generation=old.lease_generation + 1,
                    lease_expires_at_ms=now + lease_ms, updated_at_ms=max(now, old.created_at_ms),
                )
                jobs.append(self._update_job(connection, old, new))
            return jobs

        return self._write(claim)  # type: ignore[return-value]

    def _recover_expired(self, connection: sqlite3.Connection, now: int) -> None:
        rows = connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM publications WHERE lease_expires_at_ms IS NOT NULL AND lease_expires_at_ms <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            old = self._row_to_job(row)
            if old.state is PublicationState.TRANSFERRING:
                # An upload without a durable remote ID must be reconciled, never resent.
                new = replace(
                    old, state=PublicationState.UNCERTAIN,
                    operation=PublicationOperation.RECONCILE.value,
                    resume_intent=ResumeIntent.RECONCILE.value,
                    reconciliation_token=old.reconciliation_token or uuid4().hex,
                    lease_owner=None, lease_expires_at_ms=None,
                    last_error_code="lease_expired", last_http_status=None,
                    uncertain_at_ms=now, next_attempt_at_ms=now,
                    updated_at_ms=max(now, old.created_at_ms),
                )
            else:
                # Lease expiry is the recovery deadline for eligible work, even when its retry backoff was later.
                recovery_due = (
                    min(old.next_attempt_at_ms, now)
                    if old.http_method in {"POST", "PATCH"} or old.reconciliation_eligible
                    else old.next_attempt_at_ms
                )
                new = replace(
                    old, lease_owner=None, lease_expires_at_ms=None,
                    next_attempt_at_ms=recovery_due, updated_at_ms=max(now, old.created_at_ms),
                )
            self._update_job(connection, old, new)

    def renew(
        self, reference: PublicationKey | PublicationJob | str, owner: str, generation: int, *,
        lease_ms: int = _DEFAULT_LEASE_MS, now_ms: int | None = None,
    ) -> PublicationJob:
        """Extend one lease only when its owner and generation still match."""
        if type(lease_ms) is not int or not 1 <= lease_ms <= 86_400_000:
            raise ValueError("lease duration is invalid")
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "publication clock")

        def update(connection: sqlite3.Connection) -> PublicationJob:
            old = self._job_from_reference(connection, reference)
            self._fence(old, owner, generation, now)
            new = replace(old, lease_expires_at_ms=now + lease_ms, updated_at_ms=max(now, old.created_at_ms))
            return self._update_job(connection, old, new)

        return self._write(update)  # type: ignore[return-value]

    def release(
        self, reference: PublicationKey | PublicationJob | str, owner: str, generation: int, *,
        next_attempt_at_ms: int | None = None, now_ms: int | None = None,
    ) -> PublicationJob:
        """Release one fenced lease and optionally set its next due time."""
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "publication clock")
        if next_attempt_at_ms is not None:
            next_attempt_at_ms = _validate_nonnegative_int(next_attempt_at_ms, "next attempt time")

        def update(connection: sqlite3.Connection) -> PublicationJob:
            old = self._job_from_reference(connection, reference)
            self._fence(old, owner, generation, now)
            new = replace(
                old, lease_owner=None, lease_expires_at_ms=None,
                next_attempt_at_ms=old.next_attempt_at_ms if next_attempt_at_ms is None else next_attempt_at_ms,
                updated_at_ms=max(now, old.created_at_ms),
            )
            return self._update_job(connection, old, new)

        return self._write(update)  # type: ignore[return-value]

    def schedule(
        self, reference: PublicationKey | PublicationJob | str, owner: str, generation: int, *,
        next_attempt_at_ms: int, error_code: str | None = None, http_status: int | None = None,
        now_ms: int | None = None,
    ) -> PublicationJob:
        """Record retry timing while retaining the job's informational attempt count."""
        next_attempt_at_ms = _validate_nonnegative_int(next_attempt_at_ms, "next attempt time")
        if error_code is not None:
            _validate_error_code(error_code)
        if http_status is not None:
            _validate_http_status(http_status)
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "publication clock")

        def update(connection: sqlite3.Connection) -> PublicationJob:
            old = self._job_from_reference(connection, reference)
            self._fence(old, owner, generation, now)
            if old.state is PublicationState.TRANSFERRING:
                # A worker that began a POST cannot schedule another POST directly.
                new = replace(
                    old, state=PublicationState.UNCERTAIN,
                    operation=PublicationOperation.RECONCILE.value,
                    resume_intent=ResumeIntent.RECONCILE.value,
                    reconciliation_token=old.reconciliation_token or uuid4().hex,
                    uncertain_at_ms=now, last_error_code=error_code or "transfer_unknown",
                    last_http_status=http_status, next_attempt_at_ms=next_attempt_at_ms,
                    lease_owner=None, lease_expires_at_ms=None, updated_at_ms=max(now, old.created_at_ms),
                )
            else:
                new = replace(
                    old, last_error_code=error_code, last_http_status=http_status,
                    next_attempt_at_ms=next_attempt_at_ms,
                    lease_owner=None, lease_expires_at_ms=None, updated_at_ms=max(now, old.created_at_ms),
                )
            return self._update_job(connection, old, new)

        return self._write(update)  # type: ignore[return-value]

    def retry(
        self, reference: PublicationKey | PublicationJob | str, *, owner: str | None = None,
        generation: int | None = None, next_attempt_at_ms: int | None = None, now_ms: int | None = None,
    ) -> PublicationJob:
        """Explicitly resume a blocked, missing, or uncertain job by its saved intent."""
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "publication clock")
        due = now if next_attempt_at_ms is None else _validate_nonnegative_int(next_attempt_at_ms, "next attempt time")

        def update(connection: sqlite3.Connection) -> PublicationJob:
            old = self._job_from_reference(connection, reference)
            self._fence(old, owner, generation, now)
            if old.state not in {PublicationState.BLOCKED, PublicationState.MISSING, PublicationState.UNCERTAIN, PublicationState.QUEUED, PublicationState.METADATA_PENDING}:
                raise self._transition_error("publication job is not retryable")
            target: PublicationState | None
            # Only an explicit operator retry may authorize a possible duplicate POST.
            if old.state is PublicationState.UNCERTAIN:
                target = PublicationState.QUEUED
            elif old.state in {PublicationState.BLOCKED, PublicationState.MISSING}:
                target = {
                    ResumeIntent.POST.value: PublicationState.QUEUED,
                    ResumeIntent.RECONCILE.value: PublicationState.UNCERTAIN,
                    ResumeIntent.PATCH.value: PublicationState.METADATA_PENDING,
                }.get(old.resume_intent)
                if target is None:
                    raise self._transition_error("publication job has no resumable intent")
            else:
                target = old.state
            if target is PublicationState.QUEUED and old.private_path is None:
                raise self._transition_error("queued retry requires a private media path")
            if target is PublicationState.METADATA_PENDING and old.remote_recording_id is None:
                raise self._transition_error("metadata retry requires a remote recording ID")
            operation = {
                PublicationState.QUEUED: PublicationOperation.POST.value,
                PublicationState.METADATA_PENDING: PublicationOperation.PATCH.value,
                PublicationState.UNCERTAIN: PublicationOperation.RECONCILE.value,
            }.get(target, PublicationOperation.NONE.value)
            intent = {
                PublicationState.QUEUED: ResumeIntent.POST.value,
                PublicationState.METADATA_PENDING: ResumeIntent.PATCH.value,
                PublicationState.UNCERTAIN: ResumeIntent.RECONCILE.value,
            }.get(target, old.resume_intent)
            next_token = old.reconciliation_token
            if target is PublicationState.QUEUED:
                # An operator-authorized uncertain retry is the only path back to POST.
                next_token = None
            elif target is PublicationState.UNCERTAIN and next_token is None:
                next_token = uuid4().hex
            new = replace(
                old, state=target, operation=operation, resume_intent=intent,
                reconciliation_token=next_token,
                last_error_code=None, last_http_status=None, next_attempt_at_ms=due,
                lease_owner=None, lease_expires_at_ms=None, updated_at_ms=max(now, old.created_at_ms),
            )
            return self._update_job(connection, old, new)

        return self._write(update)  # type: ignore[return-value]

    def relink(
        self, reference: PublicationKey | PublicationJob | str, private_path: bytes | str | os.PathLike[str], *,
        owner: str | None = None, generation: int | None = None, now_ms: int | None = None,
    ) -> PublicationJob:
        """Replace only the private path, preserving identity and public state."""
        path = _path_bytes(private_path)
        now = self._time_ms() if now_ms is None else _validate_nonnegative_int(now_ms, "publication clock")

        def update(connection: sqlite3.Connection) -> PublicationJob:
            old = self._job_from_reference(connection, reference)
            self._fence(old, owner, generation, now)
            new = replace(old, private_path=path, updated_at_ms=max(now, old.created_at_ms))
            # Only a missing local file is restored; blocked rows remain action-required.
            if old.state is PublicationState.MISSING:
                target = {
                    ResumeIntent.POST.value: PublicationState.QUEUED,
                    ResumeIntent.RECONCILE.value: PublicationState.UNCERTAIN,
                    ResumeIntent.PATCH.value: PublicationState.METADATA_PENDING,
                }.get(old.resume_intent)
                if target is None:
                    return self._update_job(connection, old, new)
                if target is PublicationState.METADATA_PENDING and old.remote_recording_id is None:
                    raise self._transition_error("metadata relink requires a remote recording ID")
                new = replace(
                    new,
                    state=target,
                    operation={
                        PublicationState.QUEUED: PublicationOperation.POST.value,
                        PublicationState.METADATA_PENDING: PublicationOperation.PATCH.value,
                        PublicationState.UNCERTAIN: PublicationOperation.RECONCILE.value,
                    }[target],
                    resume_intent={
                        PublicationState.QUEUED: ResumeIntent.POST.value,
                        PublicationState.METADATA_PENDING: ResumeIntent.PATCH.value,
                        PublicationState.UNCERTAIN: ResumeIntent.RECONCILE.value,
                    }[target],
                    reconciliation_token=(None if target is PublicationState.QUEUED else old.reconciliation_token or uuid4().hex),
                    next_attempt_at_ms=now,
                )
            return self._update_job(connection, old, new)

        return self._write(update)  # type: ignore[return-value]

    def update_path(
        self,
        old_private_path: bytes | str | os.PathLike[str],
        new_private_path: bytes | str | os.PathLike[str],
        identity: MediaIdentity,
        *,
        limit: int = 100,
    ) -> int:
        """Best-effort rename callback that skips leased rows and exposes only a count."""
        old_path = _absolute_path_bytes(old_private_path, "old private path")
        new_path = _absolute_path_bytes(new_private_path, "new private path")
        if not isinstance(identity, MediaIdentity):
            raise TypeError("media identity is invalid")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("update limit is invalid")

        def update(connection: sqlite3.Connection) -> int:
            # App-managed callers prove the move; the store compares bytes and identity only.
            rows = connection.execute(
                "SELECT job_id FROM publications WHERE private_path = ? "
                "AND media_device = ? AND media_inode = ? AND media_size = ? AND source_mtime_ns = ? "
                "AND lease_owner IS NULL AND cleanup_lease_owner IS NULL ORDER BY job_id LIMIT ?",
                (old_path, identity.device, identity.inode, identity.size, identity.mtime_ns, limit),
            ).fetchall()
            changed = 0
            for (job_id,) in rows:
                cursor = connection.execute(
                    "UPDATE publications SET private_path = ? WHERE job_id = ? "
                    "AND private_path = ? AND media_device = ? AND media_inode = ? "
                    "AND media_size = ? AND source_mtime_ns = ? "
                    "AND lease_owner IS NULL AND cleanup_lease_owner IS NULL",
                    (new_path, job_id, old_path, identity.device, identity.inode, identity.size, identity.mtime_ns),
                )
                changed += cursor.rowcount
            return changed

        return self._write(update)  # type: ignore[return-value]

    def forget(self, reference: PublicationKey | PublicationJob | str) -> None:
        """Delete only the local row after explicit operator authorization."""
        def delete(connection: sqlite3.Connection) -> None:
            old = self._job_from_reference(connection, reference)
            if old.lease_owner is not None or old.cleanup_lease_owner is not None:
                raise self._transition_error("leased publication jobs cannot be forgotten")
            connection.execute("DELETE FROM publications WHERE job_id = ?", (old.job_id,))

        self._write(delete)

    def _job_from_reference(self, connection: sqlite3.Connection, reference: PublicationKey | PublicationJob | str) -> PublicationJob:
        row = self._fetch_ref(connection, reference)
        if row is None:
            raise self._transition_error("publication job does not exist")
        return self._row_to_job(row)

    def accept_transfer(
        self, reference: PublicationKey | PublicationJob | str, remote_recording_id: int, status: int = 202,
        *, owner: str | None = None, generation: int | None = None,
    ) -> PublicationJob:
        if status != 202:
            raise ValueError("only HTTP 202 is an accepted transfer")
        return self.transition(
            reference, PublicationState.METADATA_PENDING, owner=owner, generation=generation,
            remote_recording_id=remote_recording_id,
        )

    def mark_metadata_pending(
        self, reference: PublicationKey | PublicationJob | str, error_code: str, status: int | None = None,
        *, owner: str | None = None, generation: int | None = None,
    ) -> PublicationJob:
        if status is not None:
            _validate_http_status(status)
        return self.transition(
            reference, PublicationState.METADATA_PENDING, owner=owner, generation=generation,
            error_code=_validate_error_code(error_code), http_status=status,
        )

    def mark_published(
        self, reference: PublicationKey | PublicationJob | str, *, owner: str | None = None, generation: int | None = None,
    ) -> PublicationResult:
        job = self.get(reference)
        if job is None:
            raise self._transition_error("publication job does not exist")
        if job.state is PublicationState.PUBLISHED:
            return PublicationResult(job, True)
        return PublicationResult(
            self.transition(reference, PublicationState.PUBLISHED, owner=owner, generation=generation), False,
        )


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
